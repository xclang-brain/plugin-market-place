"""Tests of the Coverity Connect emulator and the mock analysis commands (``python3 -m unittest``).

The emulator runs as a subprocess (``python3 -m coverity_emulator.server --port 0``). The pipeline
tests run seed.py (git, svn, clang, gcc, cppcheck); they are skipped with the reason when a tool is
missing.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
BIN = HERE / "coverity_emulator" / "bin"
FIXTURES = HERE.parents[1] / "fixtures"
V9 = "http://ws.coverity.com/v9"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"


class Emulator:
    def __init__(self, data: str, *args: str):
        self.data = data
        self.proc = subprocess.Popen([sys.executable, "-m", "coverity_emulator.server", "--port", "0", "--data", data,
                                      *args], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the emulator did not start: " + self.proc.stderr.read())
        self.info = json.loads(line)
        self.url = self.info["url"]

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()

    def request(self, method: str, path: str, body: bytes | str | dict | None = None, auth: tuple[str, str] | None = None,
                headers: dict | None = None, timeout: float = 30) -> tuple[int, dict, bytes]:
        parts = urlsplit(self.url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        hdrs = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        if isinstance(body, str):
            body = body.encode("utf-8")
        if auth:
            hdrs["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        conn.close()
        return out

    def admin(self, method: str, name: str, body: dict | None = None) -> dict:
        status, _, data = self.request(method, f"/__emulator__/{name}", body)
        if status != 200:
            raise AssertionError(f"admin {name}: {status} {data!r}")
        return json.loads(data)

    def config(self, **changes) -> dict:
        return self.admin("POST", "config", changes)


ADMIN = ("admin", "emu-admin-key")
SEED = {"users": [{"name": "admin", "password": "coverity", "keys": ["emu-admin-key"], "fullName": "Administrator"},
                  {"name": "viewer", "password": "v", "keys": ["emu-viewer-key"], "webservices": False}],
        "triageStores": ["Release Triage Store"],
        "projects": ["testcpp"],
        "streams": [{"name": "libev-main", "project": "testcpp"},
                    {"name": "libev-release", "project": "testcpp", "triageStore": "Release Triage Store"}]}


def warning(checker: str, function: str | None, extra: str, line: int, impact: str = "Medium",
            category: str = "Null pointer dereferences", cwe: str = "476", sub: str = "none", file: str = "/b/ev.c",
            nested: bool = False) -> dict:
    events = [{"eventDescription": f'Condition "x{line}", taking false branch.', "eventTag": "path", "eventNumber": 1,
               "eventTreePosition": "1", "main": False, "filePathname": file, "lineNumber": line - 2, "events": None},
              {"eventDescription": f'Dereferencing "{extra}".', "eventTag": "dereference", "eventNumber": 2,
               "eventTreePosition": "2", "main": True, "filePathname": file, "lineNumber": line, "events": None}]
    if nested:
        events[0]["events"] = [{"eventDescription": '"f" frees "p".', "eventTag": "freed_arg", "eventNumber": 1,
                                "eventTreePosition": "1.1", "main": False, "filePathname": file, "lineNumber": 5,
                                "events": None}]
    return {"checkerName": checker, "subcategory": sub, "type": checker.lower(), "subtype": sub, "extra": extra,
            "functionDisplayName": function, "functionMangledName": function, "mainEventFilePathname": file,
            "strippedMainEventFilePathname": file, "mainEventLineNumber": line, "domain": "STATIC_C", "language": "C",
            "events": events,
            "checkerProperties": {"category": category, "categoryDescription": category, "cweCategory": cwe,
                                  "issueKinds": ["QUALITY"], "eventSetCaptions": [], "impact": impact,
                                  "impactDescription": impact, "subcategoryLocalEffect": "effect",
                                  "subcategoryShortDescription": "Dereference null return value",
                                  "subcategoryLongDescription": "long"}}


def search_body(stream: str = "libev-main", extra_filters: list | None = None, columns: list | None = None,
                scope: str = "last()") -> dict:
    return {"filters": [{"columnKey": "streams", "matchMode": "oneOrMoreMatch",
                         "matchers": [{"class": "Stream", "name": stream, "type": "nameMatcher"}]}] + (extra_filters or []),
            "columns": columns or ["cid", "checker", "displayFile", "lineNumber", "displayFunction", "displayImpact",
                                   "displayCategory", "cwe", "mergeKey", "classification", "action", "severity", "status"],
            "snapshotScope": {"show": {"scope": scope, "includeOutdatedSnapshots": False}}}


SEARCH_QUERY = ("/api/v2/issues/search?includeColumnLabels=true&locale=en_us&offset={offset}&queryType=bySnapshot"
                "&rowCount={rows}&sortOrder=asc")


def envelope(op: str, inner: str, user: str = "admin", password: str = "emu-admin-key", must: bool = False,
             child_ns: bool = False) -> str:
    mu = ' soapenv:mustUnderstand="1"' if must else ""
    if child_ns:
        inner = inner.replace("<", "<ws:").replace("<ws:/", "</ws:")
    return (f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:ws="{V9}"><soapenv:Header>'
            '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"'
            f"{mu}><wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">'
            f"{password}</wsse:Password></wsse:UsernameToken></wsse:Security></soapenv:Header>"
            f"<soapenv:Body><ws:{op}>{inner}</ws:{op}></soapenv:Body></soapenv:Envelope>")


def stream_defects(cid: int, stream: str = "libev-main", extra: str = "") -> str:
    return (f"<mergedDefectIdDataObjs><cid>{cid}</cid></mergedDefectIdDataObjs><filterSpec>"
            f"<includeDefectInstances>true</includeDefectInstances><includeHistory>false</includeHistory>{extra}"
            f"<streamIdList><name>{stream}</name></streamIdList></filterSpec>")


class ServerTestCase(unittest.TestCase):
    emu: Emulator

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="covemu-")
        cls.emu = Emulator(os.path.join(cls.tmp, "data"))
        cls.emu.admin("POST", "seed", SEED)
        first = [warning("USE_AFTER_FREE", "iouring_sqe_get", "iouring_sq_ring,loop", 261, "High",
                         "Memory - illegal accesses", "416", nested=True),
                 warning("NULL_RETURNS", "ev_run", "malloc", 40),
                 warning("CHECKED_RETURN", "fd_intern", "fcntl", 2563, cwe="252", category="Error handling issues"),
                 warning("CHECKED_RETURN", "fd_intern", "fcntl", 2564, cwe="252", category="Error handling issues"),
                 warning("PW.SET_BUT_NOT_USED", None, "    int x;\n", 7, "Low", "Parse warnings", "398"),
                 warning("UNINIT", "loop_init", "fd", 90, "High", "Uninitialized variables", "457")]
        cls.snap1 = cls.emu.admin("POST", "issues", {"stream": "libev-main", "snapshot": {"sourceVersion": "abc123"},
                                                     "issues": first})
        cls.emu.admin("POST", "issues", {"stream": "libev-release", "snapshot": {"sourceVersion": "abc123"},
                                         "issues": first})
        second = [w for w in first if w["checkerName"] != "UNINIT"]
        second[1] = dict(second[1], mainEventLineNumber=44)  # moved lines keep the mergeKey and the CID
        cls.snap2 = cls.emu.admin("POST", "issues", {"stream": "libev-main", "snapshot": {"sourceVersion": "def456"},
                                                     "issues": second})

    @classmethod
    def tearDownClass(cls) -> None:
        cls.emu.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self) -> None:
        self.emu.config(reset=True)

    def search(self, body: dict, offset: int = 0, rows: int = 200, auth=ADMIN, query: str | None = None):
        path = query or SEARCH_QUERY.format(offset=offset, rows=rows)
        status, headers, data = self.emu.request("POST", path, body, auth=auth)
        return status, headers, (json.loads(data) if data and headers.get("content-type", "").startswith("application/json")
                                 else data)

    def cid_of(self, checker: str) -> int:
        _, _, data = self.search(search_body(scope="first()..last()"))
        for row in data["rows"]:
            values = {c["key"]: c["value"] for c in row}
            if values["checker"] == checker:
                return int(values["cid"])
        raise AssertionError(f"no {checker}")


class RestTests(ServerTestCase):
    def test_columns_shape(self):
        status, headers, data = self.emu.request("GET", "/api/v2/issues/columns?queryType=bySnapshot&retrieveGroupByColumns=false",
                                                 auth=ADMIN)
        self.assertEqual(status, 200)
        cols = json.loads(data)
        self.assertIsInstance(cols, list)
        self.assertEqual(cols[0], {"columnKey": "cid", "name": "CID"})
        keys = {c["columnKey"] for c in cols}
        self.assertTrue({"checker", "displayImpact", "mergeKey", "lineNumber", "displayFile", "status"} <= keys)
        self.assertNotIn("streams", keys)
        self.assertIn("set-cookie", headers)

    def test_auth_without_header_is_302_to_login(self):
        status, headers, data = self.emu.request("GET", "/api/v2/issues/columns")
        self.assertEqual(status, 302)
        self.assertTrue(headers["location"].endswith("/login/login.htm"))
        self.assertIn(b"Redirecting to", data)
        status, _, _ = self.emu.request("GET", "/api/v2/nonexistent")
        self.assertEqual(status, 302)  # authentication comes before the path (measured on scan8)

    def test_auth_wrong_credentials_is_401(self):
        status, headers, data = self.emu.request("POST", "/api/v2/issues/search", {}, auth=("admin", "wrong"))
        self.assertEqual(status, 401)
        self.assertEqual(data, b"Authentication failed.\n")
        self.assertNotIn("content-type", headers)
        self.assertNotIn("www-authenticate", headers)
        self.assertIn("COVJSESSIONID-build=", headers["set-cookie"])
        status, _, _ = self.emu.request("GET", "/api/v2/issues/columns", auth=("nobody", "x"))
        self.assertEqual(status, 401)

    def test_password_also_authenticates(self):
        status, _, _ = self.emu.request("GET", "/api/v2/issues/columns", auth=("admin", "coverity"))
        self.assertEqual(status, 200)

    def test_no_permission(self):
        status, _, data = self.emu.request("GET", "/api/v2/issues/columns", auth=("viewer", "emu-viewer-key"))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(data)["code"], 1003)
        self.emu.config(rest_no_permission=401)
        status, _, _ = self.emu.request("GET", "/api/v2/issues/columns", auth=("viewer", "emu-viewer-key"))
        self.assertEqual(status, 401)

    def test_search_official_shape(self):
        body = {"filters": [{"columnKey": "project", "matchMode": "oneOrMoreMatch",
                             "matchers": [{"class": "Project", "name": "testcpp", "type": "nameMatcher"}]},
                            {"columnKey": "displayImpact", "matchMode": "oneOrMoreMatch",
                             "matchers": [{"key": "High", "type": "keyMatcher"}]}],
                "columns": ["action", "cid", "column_standard_DISA-STIG V4R10", "cwe", "displayImpact"]}
        status, _, data = self.search(body, query="/api/v2/issues/search?includeColumnLabels=true&locale=en_us&offset=0"
                                                  "&queryType=byProject&rowCount=200&sortOrder=asc")
        self.assertEqual(status, 200)
        self.assertEqual(list(data), ["offset", "totalRows", "columns", "rows"])
        self.assertEqual(data["columns"], body["columns"])
        self.assertEqual(data["totalRows"], 2)  # USE_AFTER_FREE and UNINIT (High)
        for row in data["rows"]:
            self.assertEqual([c["key"] for c in row], body["columns"])
            for c in row:
                self.assertIsInstance(c["value"], str)
            self.assertEqual(row[0]["value"], "Undecided")

    def test_search_plugin_shape_and_values(self):
        status, _, data = self.search(search_body())
        self.assertEqual(status, 200)
        rows = [{c["key"]: c["value"] for c in r} for r in data["rows"]]
        self.assertEqual(data["totalRows"], 4)  # UNINIT is not in the latest snapshot
        null = next(r for r in rows if r["checker"] == "NULL_RETURNS")
        self.assertEqual(null["lineNumber"], "44")
        self.assertEqual(null["status"], "New")
        self.assertEqual(null["classification"], "Unclassified")
        self.assertEqual(null["severity"], "Unspecified")
        self.assertEqual(len(null["mergeKey"]), 32)
        pw = next(r for r in rows if r["checker"] == "PW.SET_BUT_NOT_USED")
        self.assertEqual(pw["displayFunction"], "")
        self.assertEqual([int(r["cid"]) for r in rows], sorted(int(r["cid"]) for r in rows))

    def test_two_occurrences_one_cid(self):
        status, _, data = self.search(search_body(columns=["cid", "checker", "occurrenceCount", "lineNumber"]))
        rows = [{c["key"]: c["value"] for c in r} for r in data["rows"]]
        checked = [r for r in rows if r["checker"] == "CHECKED_RETURN"]
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0]["occurrenceCount"], "2")
        self.assertEqual(checked[0]["lineNumber"], "2563")
        self.emu.config(rest_line_from_occurrence="last")
        _, _, data = self.search(search_body(columns=["cid", "checker", "lineNumber"]))
        rows = [{c["key"]: c["value"] for c in r} for r in data["rows"]]
        self.assertEqual(next(r for r in rows if r["checker"] == "CHECKED_RETURN")["lineNumber"], "2564")

    def test_paging(self):
        seen, total = [], None
        for offset in range(0, 10, 2):
            status, _, data = self.search(search_body(), offset=offset, rows=2)
            self.assertEqual(status, 200)
            self.assertEqual(data["offset"], offset)
            total = data["totalRows"]
            seen += [r[0]["value"] for r in data["rows"]]
            if offset + 2 >= total:
                break
        self.assertEqual(total, 4)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4)
        self.emu.config(page_overlap=1)
        _, _, page = self.search(search_body(), offset=2, rows=2)
        self.assertEqual(page["rows"][0][0]["value"], seen[1])  # starts one row early
        self.emu.config(omit_total_rows=True)
        _, _, page = self.search(search_body(), offset=0, rows=2)
        self.assertNotIn("totalRows", page)

    def test_rowcount_minus1(self):
        _, _, data = self.search(search_body(), rows=-1)
        self.assertEqual(len(data["rows"]), 4)
        self.emu.config(rowcount_minus1="400")
        status, _, _ = self.search(search_body(), rows=-1)
        self.assertEqual(status, 400)

    def test_unknown_key(self):
        flt = [{"columnKey": "classification", "matchMode": "oneOrMoreMatch",
                "matchers": [{"type": "keyMatcher", "key": "Tested Elsewhere"}]}]
        status, _, data = self.search(search_body(extra_filters=flt))
        self.assertEqual(status, 500)
        self.assertEqual(data["message"], "Invalid key provided: Tested Elsewhere")
        flt = [{"columnKey": "displayImpact", "matchMode": "oneOrMoreMatch",
                "matchers": [{"type": "keyMatcher", "key": "Unspecified"}]}]
        status, _, data = self.search(search_body(extra_filters=flt))
        self.assertEqual((status, data["message"]), (500, "Invalid key provided: Unspecified"))
        self.emu.config(error_body="text")
        status, headers, data = self.search(search_body(extra_filters=flt))
        self.assertEqual((status, data), (500, b"Invalid key provided: Unspecified"))
        self.emu.config(error_body="json", search_unknown_key="200_empty")
        status, _, data = self.search(search_body(extra_filters=flt))
        self.assertEqual((status, data["totalRows"]), (200, 0))
        self.emu.config(test_advisor=True, search_unknown_key="500")
        flt[0]["columnKey"], flt[0]["matchers"][0]["key"] = "classification", "Tested Elsewhere"
        status, _, _ = self.search(search_body(extra_filters=flt))
        self.assertEqual(status, 200)

    def test_empty_values_switch(self):
        cols = ["cid", "checker", "displayFunction"]
        self.emu.config(search_empty_value="null")
        _, _, data = self.search(search_body(columns=cols))
        pw = next(r for r in data["rows"] if r[1]["value"] == "PW.SET_BUT_NOT_USED")
        self.assertIsNone(pw[2]["value"])
        self.emu.config(search_empty_value="omit")
        _, _, data = self.search(search_body(columns=cols))
        pw = next(r for r in data["rows"] if r[1]["value"] == "PW.SET_BUT_NOT_USED")
        self.assertEqual(len(pw), 2)

    def test_unknown_stream_switch(self):
        status, _, data = self.search(search_body(stream="no-such-stream"))
        self.assertEqual((status, data["totalRows"]), (200, 0))
        self.emu.config(unknown_stream="404")
        status, _, data = self.search(search_body(stream="no-such-stream"))
        self.assertEqual((status, data["code"]), (404, 1300))

    def test_scope_and_fixed(self):
        _, _, data = self.search(search_body(scope="first()..last()"))
        rows = [{c["key"]: c["value"] for c in r} for r in data["rows"]]
        uninit = next(r for r in rows if r["checker"] == "UNINIT")
        self.assertEqual(uninit["status"], "Fixed")
        _, _, data = self.search(search_body(scope=str(self.snap1["snapshotId"])))
        self.assertEqual(data["totalRows"], 5)

    def test_triage_write_and_history(self):
        cid = self.cid_of("NULL_RETURNS")
        body = {"cids": [cid], "attributeValuesList": [{"attributeName": "Classification", "attributeValue": "Bug"},
                                                       {"attributeName": "Action", "attributeValue": "Fix Required"},
                                                       {"attributeName": "Comment", "attributeValue": "from test"}]}
        status, _, data = self.emu.request("PUT", "/api/v2/issues/triage?triageStoreName=Default%20Triage%20Store", body,
                                           auth=ADMIN)
        self.assertEqual((status, data), (200, b""))
        flt = [{"columnKey": "cid", "matchMode": "oneOrMoreMatch", "matchers": [{"type": "idMatcher", "id": str(cid)}]}]
        _, _, res = self.search(search_body(extra_filters=flt, columns=["cid", "classification", "status", "lastTriageComment"]))
        values = {c["key"]: c["value"] for c in res["rows"][0]}
        self.assertEqual((values["classification"], values["status"], values["lastTriageComment"]),
                         ("Bug", "Triaged", "from test"))
        # another triage store: the release stream is untouched
        _, _, res = self.search(search_body(stream="libev-release", extra_filters=flt, columns=["cid", "classification"]))
        self.assertEqual(res["rows"][0][1]["value"], "Unclassified")
        status, _, data = self.emu.request("GET", f"/api/v2/issues/triageHistory?cid={cid}&triageStoreNames=Default%20Triage%20Store",
                                           auth=ADMIN)
        hist = json.loads(data)
        self.assertEqual(list(hist), ["triageHistories", "code", "message"])
        names = [p["attributeName"] for p in hist["triageHistories"][0]["attributeValuesList"]]
        self.assertIn("classification", names)
        self.assertIn("dateCreated", names)
        self.assertEqual(hist["triageHistories"][-1]["attributeValuesList"][-1]["attributeValue"], "0001-01-01T00:00:00.000-0700")
        writes = self.emu.admin("GET", "triage-writes")["writes"]
        self.assertEqual(writes[-1]["cids"], [cid])

    def test_triage_errors_and_switches(self):
        cid = self.cid_of("CHECKED_RETURN")
        put = lambda body, store="Default%20Triage%20Store": self.emu.request(  # noqa: E731
            "PUT", f"/api/v2/issues/triage?triageStoreName={store}", body, auth=ADMIN)
        attrs = [{"attributeName": "Classification", "attributeValue": "Intentional"}]
        status, _, data = put({"cids": [cid], "attributeValuesList": attrs}, "No%20Such%20Store")
        self.assertEqual((status, json.loads(data)["code"]), (404, 2000))
        status, _, data = put({"cids": [cid], "attributeValuesList": [{"attributeName": "Colour", "attributeValue": "x"}]})
        self.assertEqual(json.loads(data)["code"], 1200)
        status, _, data = put({"cids": [cid], "attributeValuesList": [{"attributeName": "Action", "attributeValue": "Review"}]})
        self.assertEqual((status, json.loads(data)["message"]), (500, "Invalid key provided: Review"))
        self.emu.config(triage_require_comment=True)
        status, _, data = put({"cids": [cid], "attributeValuesList": attrs})
        self.assertEqual(status, 400)
        self.assertIn("A comment is required when modifying triage attributes", json.loads(data)["message"])
        self.emu.config(triage_require_comment=False, triage_max_cids=1)
        status, _, _ = put({"cids": [cid, cid + 1], "attributeValuesList": attrs})
        self.assertEqual(status, 400)
        self.emu.config(triage_max_cids=0, triage_success="204")
        status, _, _ = put({"cids": [cid], "attributeValuesList": attrs})
        self.assertEqual(status, 204)
        self.emu.config(server_version="2021.12.0")
        status, _, _ = put({"cids": [cid], "attributeValuesList": attrs})
        self.assertEqual(status, 404)

    def test_csrf_switch(self):
        cid = self.cid_of("CHECKED_RETURN")
        self.emu.config(csrf_on_session_cookie=True)
        body = {"cids": [cid], "attributeValuesList": [{"attributeName": "Severity", "attributeValue": "Minor"}]}
        status, _, _ = self.emu.request("PUT", "/api/v2/issues/triage?triageStoreName=Default%20Triage%20Store", body,
                                        auth=ADMIN, headers={"Cookie": "COVJSESSIONID-build=ABC"})
        self.assertEqual(status, 403)

    def test_other_endpoints(self):
        status, _, data = self.emu.request("GET", "/api/v2/serverInfo/version", auth=ADMIN)
        self.assertEqual(json.loads(data), {"internalVersion": "e0e0e0e0e0 im-2024.12-push-1",
                                            "externalVersion": "2024.12.1", "code": None, "message": None})
        status, _, data = self.emu.request("GET", "/api/v2/streams/libev-release?locale=en_us", auth=ADMIN)
        self.assertEqual(json.loads(data)["streams"][0]["triageStoreName"], "Release Triage Store")
        status, _, data = self.emu.request("GET", "/api/v2/issueAttributes/status", auth=ADMIN)
        self.assertEqual(json.loads(data), ["New", "Triaged", "Dismissed", "Fixed", "Absent Dismissed"])
        status, _, data = self.emu.request("GET", f"/api/v2/snapshots/{self.snap2['snapshotId']}", auth=ADMIN)
        self.assertEqual(json.loads(data)["sourceVersion"], "def456")
        status, _, data = self.emu.request("GET", "/api/v2/streams/stream/snapshots?idType=byName&name=libev-main", auth=ADMIN)
        self.assertEqual(len(json.loads(data)["snapshotsForStream"]), 2)
        status, _, data = self.emu.request("GET", "/api/v2/triageStores/Release%20Triage%20Store", auth=ADMIN)
        self.assertEqual(json.loads(data)["streamNames"], ["libev-release"])
        status, _, data = self.emu.request("GET", "/api/v2/triageAttributes", auth=ADMIN)
        self.assertEqual(json.loads(data)["attributes"][0]["name"], "Classification")
        self.emu.config(columns_names="ja")
        _, _, data = self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.assertEqual(json.loads(data)[1]["name"], "チェッカー")


class SoapTests(ServerTestCase):
    def soap(self, service: str, body: str) -> tuple[int, dict, ET.Element]:
        status, headers, data = self.emu.request("POST", f"/ws/v9/{service}", body,
                                                 headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""})
        return status, headers, ET.fromstring(data)

    def test_get_stream_defects_shape(self):
        cid = self.cid_of("USE_AFTER_FREE")
        status, headers, root = self.soap("defectservice", envelope("getStreamDefects", stream_defects(cid)))
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/xml;charset=utf-8")
        resp = root.find(f"{{{SOAP_ENV}}}Body/{{{V9}}}getStreamDefectsResponse")
        self.assertIsNotNone(resp)
        ret = resp.find("return")  # children have no namespace
        self.assertEqual(ret.findtext("cid"), str(cid))
        inst = ret.find("defectInstances")
        events = inst.findall("events")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].find("events").findtext("eventTag"), "freed_arg")  # nested
        self.assertEqual(events[0].findtext("eventKind"), "MODEL")
        self.assertEqual(events[1].findtext("main"), "true")
        self.assertEqual(inst.findtext("function/functionDisplayName"), "iouring_sqe_get")
        self.assertEqual(inst.findtext("impact/name"), "High")
        self.assertEqual([e.tag for e in ret][:2], ["checkerName", "cid"])
        attrs = {a.findtext("attributeDefinitionId/name"): a.findtext("attributeValueId/name")
                 for a in ret.findall("defectStateAttributeValues")}
        self.assertEqual(attrs["DefectStatus"], "New")

    def test_auth_fault(self):
        status, headers, data = self.emu.request("POST", "/ws/v9/defectservice",
                                                 envelope("getStreamDefects", stream_defects(1), password="bad"))
        self.assertEqual(status, 401)
        self.assertTrue(data.startswith(b"<?xml version='1.0' encoding='UTF-8'?>"))
        root = ET.fromstring(data)
        self.assertEqual(root.findtext(f".//{{{SOAP_ENV}}}Fault/faultstring"), "User authentication failed.")
        self.assertIsNone(root.find(f".//{{{SOAP_ENV}}}Fault/detail"))

    def test_malformed_envelope(self):
        status, _, data = self.emu.request("POST", "/ws/v9/defectservice", "<x/>")
        self.assertEqual(status, 500)
        root = ET.fromstring(data)
        self.assertEqual(root.findtext(f".//{{{SOAP_ENV}}}Fault/faultcode"), "S:Client")

    def test_snapshots(self):
        status, _, root = self.soap("configurationservice",
                                    envelope("getSnapshotsForStream", "<streamId><name>libev-main</name></streamId>"))
        ids = [r.findtext("id") for r in root.iter("return")]
        self.assertEqual(ids, [str(self.snap1["snapshotId"]), str(self.snap2["snapshotId"])])
        status, _, root = self.soap("configurationservice", envelope(
            "getSnapshotInformation", f"<snapshotIds><id>{ids[-1]}</id></snapshotIds>"))
        ret = root.find(".//return")
        self.assertEqual(ret.findtext("sourceVersion"), "def456")
        self.assertEqual(ret.findtext("snapshotId/id"), ids[-1])
        status, _, root = self.soap("configurationservice",
                                    envelope("getSnapshotsForStream", "<streamId><name>nope</name></streamId>"))
        self.assertEqual(status, 500)
        self.assertEqual(root.findtext(f".//{{{V9}}}CoverityFault/errorCode"), "1300")

    def test_switches(self):
        cid = self.cid_of("NULL_RETURNS")
        many = "".join(f"<mergedDefectIdDataObjs><cid>{c}</cid></mergedDefectIdDataObjs>" for c in range(101))
        status, _, root = self.soap("defectservice", envelope("getStreamDefects", many))
        self.assertEqual((status, root.findtext(f".//{{{V9}}}CoverityFault/errorCode")), (500, "1007"))
        status, _, root = self.soap("defectservice", envelope("getStreamDefects", stream_defects(99999)))
        self.assertEqual((status, root.find(".//return")), (200, None))
        self.emu.config(soap_cid_not_in_stream="fault")
        status, _, root = self.soap("defectservice", envelope("getStreamDefects", stream_defects(99999)))
        self.assertEqual(root.findtext(f".//{{{V9}}}CoverityFault/errorCode"), "1400")
        self.emu.config(soap_max_instances_missing="none")
        _, _, root = self.soap("defectservice", envelope("getStreamDefects", stream_defects(cid)))
        self.assertIsNone(root.find(".//defectInstances"))
        _, _, root = self.soap("defectservice", envelope("getStreamDefects", stream_defects(
            cid, extra="<includeTotalDefectInstanceCount>true</includeTotalDefectInstanceCount><maxDefectInstances>1</maxDefectInstances>")))
        self.assertIsNotNone(root.find(".//defectInstances"))
        self.emu.config(soap_max_instances_missing="all", soap_require_must_understand=True)
        status, _, _ = self.soap("defectservice", envelope("getStreamDefects", stream_defects(cid)))
        self.assertEqual(status, 401)
        status, _, _ = self.soap("defectservice", envelope("getStreamDefects", stream_defects(cid), must=True))
        self.assertEqual(status, 200)
        self.emu.config(soap_require_must_understand=False, soap_strict_namespace=True)
        _, _, root = self.soap("defectservice", envelope("getStreamDefects", stream_defects(cid), child_ns=True))
        self.assertIsNone(root.find(".//return"))

    def test_wsdl(self):
        status, _, data = self.emu.request("GET", "/ws/v9/defectservice?wsdl")
        self.assertEqual(status, 200)
        self.assertIn(f'location="{self.emu.url}/ws/v9/defectservice"'.encode(), data)
        status, _, data = self.emu.request("GET", "/ws/v9/defectservice?xsd=1")
        self.assertIn(b'name="streamDefectFilterSpecDataObj"', data)


class InjectionTests(ServerTestCase):
    def test_status_for_one_window_once(self):
        self.emu.config(inject=[{"path": "^/api/v2/issues/search$", "status": 503, "count": 1}])
        status, _, _ = self.search(search_body())
        self.assertEqual(status, 503)
        status, _, _ = self.search(search_body())
        self.assertEqual(status, 200)

    def test_delay_html_fault_redirect_drop(self):
        self.emu.config(inject=[{"path": "columns", "delay": 0.6}])
        started = time.monotonic()
        status, _, _ = self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.assertGreaterEqual(time.monotonic() - started, 0.6)
        self.emu.config(inject=[{"path": "columns", "html": True}])
        status, headers, data = self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.assertEqual((status, headers["content-type"]), (200, "text/html;charset=UTF-8"))
        self.emu.config(inject=[{"operation": "getStreamDefects", "soap_fault": "Internal error", "code": 1004}])
        status, _, data = self.emu.request("POST", "/ws/v9/defectservice", envelope("getStreamDefects", stream_defects(1)))
        self.assertEqual(status, 500)
        self.assertIn(b"<errorCode>1004</errorCode>", data)
        self.emu.config(inject=[{"path": "columns", "redirect": 301}])
        status, headers, _ = self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.assertEqual(status, 301)
        self.assertTrue(headers["location"].startswith("https://"))
        self.emu.config(inject=[{"path": "columns", "drop": "before_response"}])
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError, socket.timeout)):
            self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.emu.config(inject=[{"path": "columns", "drop": "mid_body"}])
        with self.assertRaises((http.client.IncompleteRead, ConnectionError)):
            self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.emu.config(inject=[{"path": "search", "invalid_key": "Foo"}])
        status, _, data = self.search(search_body())
        self.assertEqual((status, data["message"]), (500, "Invalid key provided: Foo"))

    def test_latency_switch(self):
        self.emu.config(latency_seconds=0.5)
        started = time.monotonic()
        self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)

    def test_request_log_masks_credentials(self):
        self.emu.request("DELETE", "/__emulator__/requests")
        self.emu.request("GET", "/api/v2/issues/columns", auth=ADMIN)
        self.emu.request("POST", "/ws/v9/defectservice", envelope("getStreamDefects", stream_defects(1)))
        log = self.emu.admin("GET", "requests")["requests"]
        self.assertEqual(log[0]["auth"], {"type": "basic", "user": "admin", "secret": "****"})
        self.assertEqual(log[1]["soap_operation"], "getStreamDefects")
        self.assertNotIn("emu-admin-key", json.dumps(log))
        self.assertIn("****", log[1]["body"])


class PersistenceTests(unittest.TestCase):
    def test_state_survives_restart(self):
        tmp = tempfile.mkdtemp(prefix="covemu-")
        try:
            emu = Emulator(os.path.join(tmp, "data"))
            emu.admin("POST", "seed", SEED)
            snap = emu.admin("POST", "issues", {"stream": "libev-main", "issues": [warning("UNINIT", "f", "x", 3)]})
            emu.config(latency_seconds=0.01)
            emu.stop()
            emu = Emulator(os.path.join(tmp, "data"))
            try:
                status, _, data = emu.request("GET", f"/api/v2/snapshots/{snap['snapshotId']}", auth=ADMIN)
                self.assertEqual(status, 200)
                self.assertEqual(emu.admin("GET", "config")["latency_seconds"], 0.01)
            finally:
                emu.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tls(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is not installed (needed for the self-signed certificate)")
        tmp = tempfile.mkdtemp(prefix="covemu-")
        emu = Emulator(os.path.join(tmp, "data"), "--tls")
        try:
            import ssl
            self.assertTrue(emu.url.startswith("https://"))
            ctx = ssl.create_default_context(cafile=emu.info["ca_file"])
            parts = urlsplit(emu.url)
            conn = http.client.HTTPSConnection(parts.hostname, parts.port, context=ctx, timeout=10)
            conn.request("GET", "/api/v2/issues/columns")
            self.assertEqual(conn.getresponse().status, 302)
            plain = urlsplit(emu.info["http_url"])
            conn = http.client.HTTPConnection(plain.hostname, plain.port, timeout=10)
            conn.request("POST", "/__emulator__/config", body=json.dumps({"http_to_https_redirect": 302}),
                         headers={"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)  # administration answers on the plain port too
            conn = http.client.HTTPConnection(plain.hostname, plain.port, timeout=10)
            conn.request("GET", "/api/v2/issues/columns")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 302)
            self.assertEqual(resp.getheader("Location"), emu.url + "/api/v2/issues/columns")
        finally:
            emu.stop()
            shutil.rmtree(tmp, ignore_errors=True)


MISSING_TOOLS = [t for t in ("clang", "gcc", "cppcheck", "git", "svn", "svnadmin", "make") if not shutil.which(t)]


@unittest.skipIf(MISSING_TOOLS, f"tools needed by the mock analysis are missing: {MISSING_TOOLS}")
class PipelineTests(unittest.TestCase):
    """seed.py on the envmon fixture, then a fix, a new analysis and a new commit."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="covemu-pipe-")
        cls.emu = Emulator(os.path.join(cls.tmp, "data"))
        proc = subprocess.run([sys.executable, "-m", "coverity_emulator.seed", "--url", cls.emu.url, "--workdir",
                               os.path.join(cls.tmp, "work")], cwd=HERE, capture_output=True, text=True)
        if proc.returncode != 0:
            cls.emu.stop()
            raise RuntimeError(proc.stdout + proc.stderr)
        cls.summary = json.loads(proc.stdout)
        cls.env = dict(os.environ, PATH=str(BIN) + os.pathsep + os.environ["PATH"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.emu.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def cov(self, *args: str, cwd: str | Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(list(args), cwd=cwd or self.tmp, env=env or self.env, capture_output=True, text=True)

    def issues(self, stream: str = "envmon-main") -> list[dict]:
        return self.emu.admin("GET", f"issues?stream={stream}")["issues"]

    def test_seed_registered_the_truth(self):
        truth = self.summary["truth"]
        self.assertEqual(truth["total"], 35)
        self.assertEqual(truth["found_count"], 35, truth["missing"])
        per = self.summary["envmon_main"]["per_checker"]
        for checker in ("NULL_RETURNS", "RESOURCE_LEAK", "OVERRUN", "UNINIT", "FORWARD_NULL", "USE_AFTER_FREE",
                        "MISRA C-2012 Rule 10.4", "CHECKED_RETURN", "NEGATIVE_RETURNS"):
            self.assertIn(checker, per)
        files = {i["file"] for i in self.issues()}
        self.assertTrue(all(f.startswith("/build/agent/work/envmon/") for f in files), files)

    def test_snapshot_version_is_the_analyzed_commit(self):
        sid = self.summary["snapshots"]["envmon-main"]
        status, _, data = self.emu.request("GET", f"/api/v2/snapshots/{sid}", auth=("admin", "emu-admin-key-0001"))
        self.assertEqual(json.loads(data)["sourceVersion"], self.summary["git"]["analyzed"])
        self.assertNotEqual(self.summary["git"]["analyzed"], self.summary["git"]["latest"])

    def test_format_errors_v7(self):
        out = os.path.join(self.tmp, "v7.json")
        proc = self.cov("cov-format-errors", "--dir", self.summary["agent"]["idir"], "--json-output-v7", out)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        doc = json.loads(Path(out).read_text())
        self.assertEqual(list(doc), ["type", "formatVersion", "suppressedIssueCount", "issues", "desktopAnalysisSettings",
                                     "error", "warnings"])
        issue = next(i for i in doc["issues"] if i["checkerName"] == "RESOURCE_LEAK")
        self.assertEqual(len(issue), 23)
        self.assertEqual(list(issue["events"][0]), ["covLStrEventDescription", "eventDescription", "eventNumber",
                                                    "eventTreePosition", "eventSet", "eventTag", "filePathname",
                                                    "strippedFilePathname", "lineNumber", "main", "moreInformationId",
                                                    "remediation", "events"])
        pw = next(i for i in doc["issues"] if i["checkerName"].startswith("PW."))
        self.assertNotIn("subtype", pw)
        self.assertIsNone(pw["functionDisplayName"])
        misra = next(i for i in doc["issues"] if i["checkerName"] == "MISRA C-2012 Rule 10.4")
        self.assertEqual(misra["checkerProperties"]["MISRACategory"], "Required")
        self.assertEqual(misra["events"][0]["eventTag"], "misra_c_2012_rule_10_4_violation")
        nested = next(i for i in doc["issues"] if i["checkerName"] == "OVERRUN" and i["extra"] == "wire")
        self.assertIsNotNone(nested["events"][0]["events"])
        self.assertEqual(nested["events"][0]["events"][0]["eventTreePosition"], "1.1")
        # zero issues: exit code switch
        proc = self.cov("cov-format-errors", "--dir", self.summary["agent"]["idir"], "--json-output-v7", out,
                        "--checker-regex", "^NOTHING$")
        self.assertEqual(proc.returncode, 0)
        proc = self.cov("cov-format-errors", "--dir", self.summary["agent"]["idir"], "--json-output-v7", out,
                        "--checker-regex", "^NOTHING$", env=dict(self.env, COV_EMU_FORMAT_ERRORS_ZERO_EXIT="1"))
        self.assertEqual(proc.returncode, 1)

    def test_analysis_is_deterministic(self):
        idir = self.summary["agent"]["idir"]
        work = self.summary["agent"]["work"]
        before = Path(idir, "output", "emulator-issues.json").read_bytes()
        configs = Path(self.tmp, "work", "agent", "config")
        proc = self.cov("cov-analyze", "--dir", idir, "--all", "--ticker-mode", "none", "--coding-standard-config",
                        str(configs / "misra.config"), "--coding-standard-config", str(configs / "cert-c.config"), cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(Path(idir, "output", "emulator-issues.json").read_bytes(), before)

    def test_fix_then_reanalyze_makes_cids_fixed(self):
        work = Path(self.tmp, "fixwork", "envmon")
        shutil.copytree(self.summary["agent"]["work"], work, ignore=shutil.ignore_patterns("build"))
        subprocess.run(["git", "apply", str(FIXTURES / "envmon-latest.patch")], cwd=work, check=True)
        before = {(i["checker"], i["function"]): i for i in self.issues()}
        leak = before[("RESOURCE_LEAK", "flash_store_load")]
        strcpy = before[("STRING_OVERFLOW", "config_apply")]
        null = before[("NULL_RETURNS", "config_apply")]
        put = self.emu.request("PUT", "/api/v2/issues/triage?triageStoreName=Default%20Triage%20Store",
                               {"cids": [null["cid"]], "attributeValuesList": [
                                   {"attributeName": "Classification", "attributeValue": "Bug"}]},
                               auth=("admin", "emu-admin-key-0001"))
        self.assertEqual(put[0], 200)
        idir = Path(self.tmp, "fixwork", "idir")
        env = dict(self.env, COV_EMU_PATH_MAP=f"{work}=/build/agent/work/envmon")
        proc = self.cov("cov-build", "--dir", str(idir), "make", "-j4", cwd=work, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertIn("The cov-build utility completed successfully.", proc.stdout)
        proc = self.cov("cov-analyze", "--dir", str(idir), "--all", "--ticker-mode", "none", cwd=work, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        proc = self.cov("cov-commit-defects", "--dir", str(idir), "--url", self.emu.url, "--stream", "envmon-main",
                        "--user", "admin", "--version", "fixed", cwd=work,
                        env=dict(env, COVERITY_PASSPHRASE="emu-admin-key-0001"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(proc.stdout, r"New snapshot ID \d+ added\.")
        after = {i["cid"]: i for i in self.issues()}
        self.assertEqual(after[leak["cid"]]["status"], "Fixed")
        self.assertEqual(after[strcpy["cid"]]["status"], "Fixed")
        self.assertEqual(after[null["cid"]]["status"], "Triaged")   # same CID, triage kept
        self.assertEqual(after[null["cid"]]["line"], null["line"] + 5)  # the patch added 5 lines above it

    def test_commit_failures_and_switches(self):
        idir = self.summary["agent"]["idir"]
        base = ["cov-commit-defects", "--dir", idir, "--url", self.emu.url, "--stream", "envmon-release", "--user", "admin"]
        count = len(self.emu.admin("GET", "state")["snapshots"])
        proc = self.cov(*base, env=dict(self.env, COVERITY_PASSPHRASE="wrong"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(len(self.emu.admin("GET", "state")["snapshots"]), count)
        proc = self.cov(*base, env=dict(self.env, COVERITY_PASSPHRASE="emu-admin-key-0001", COV_EMU_COMMIT_RESULT="3"))
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(len(self.emu.admin("GET", "state")["snapshots"]), count + 1)
        key = Path(self.tmp, "ak.json")
        key.write_text(json.dumps({"key": "emu-admin-key-0001", "id": 1, "type": "Coverity authentication key",
                                   "username": "admin", "domain": "local", "version": 2}))
        key.chmod(0o644)
        proc = self.cov("cov-commit-defects", "--dir", idir, "--url", self.emu.url, "--stream", "envmon-release",
                        "--auth-key-file", str(key))
        self.assertEqual(proc.returncode, 2)  # readable by others: refused
        key.chmod(0o600)
        sid = Path(self.tmp, "sid.txt")
        proc = self.cov("cov-commit-defects", "--dir", idir, "--url", self.emu.url, "--stream", "envmon-release",
                        "--auth-key-file", str(key), "--snapshot-id-file", str(sid))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(sid.read_text().isdigit())

    def test_build_and_analyze_exit_codes(self):
        idir = Path(self.tmp, "exit-idir")
        proc = self.cov("cov-build", "--dir", str(idir), "sh", "-c", "exit 57")
        self.assertEqual(proc.returncode, 57)
        self.assertIn("No files were emitted", proc.stdout)
        proc = self.cov("cov-analyze", "--dir", str(idir))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("intermediate directory contains no translation units", proc.stdout)
        proc = self.cov("cov-build", "--bogus")
        self.assertEqual(proc.returncode, 16)
        src = Path(self.tmp, "emitfail")
        src.mkdir()
        (src / "a.c").write_text("int a(void) { return 1; }\n")
        (src / "b.c").write_text("int b(void) { return 2; }\n")
        env = dict(self.env, COV_EMU_EMIT_FAIL="b.c")
        proc = self.cov("cov-build", "--dir", str(src / "idir"), "gcc", "-c", "a.c", "b.c", cwd=src, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1 C/C++ compilation units (50%) are ready for analysis", proc.stdout)
        proc = self.cov("cov-build", "--dir", str(src / "idir2"), "--return-emit-failures", "gcc", "-c", "a.c", "b.c",
                        cwd=src, env=env)
        self.assertEqual(proc.returncode, 8)


if __name__ == "__main__":
    unittest.main()
