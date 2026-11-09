#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""設定したコマンドでビルドし、結果を JSON 1つで標準出力に返す。

使い方：cross_build.py run|check|init [--workdir 作業フォルダ]
  run    ビルドする
  check  設定を確かめる（何も作らない）
  init   設定ファイルをひな形から作る
終了コード：0＝成功、1＝ビルドの失敗、2＝設定や引数の問題。
設定は <作業フォルダ>/.<このスキルのフォルダの名前>/config.ini、ログは同じフォルダの logs/ に置く。
Python 3.8 以上の標準ライブラリだけで動く。
"""

import codecs
import collections
import configparser
import datetime
import json
import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.basename(SKILL_DIR)  # フォルダの名前を変えてコピーしたら、その名前を使う
TEMPLATE = os.path.join(SKILL_DIR, "config-template.ini")
IS_WINDOWS = os.name == "nt"

KEYS = ("source_dir", "sdk_env", "shell", "build_command", "outputs", "timeout_seconds")
SHELLS = ("auto", "bash", "cmd", "powershell")
SDK_SUFFIXES = {"cmd": (".bat", ".cmd"), "powershell": (".ps1",)}
DEFAULT_TIMEOUT = 600.0
MAX_LINES = 20       # error_lines と warning_lines の最大の行数
TAIL_LINES = 30      # tail の行数
LINE_CHARS = 300     # 結果に入れる1行の最大の文字数（長いリンクの行で JSON が読みにくくならないように）
DRAIN_SECONDS = 5    # シェルが終わった後に出力を待つ秒数（出力をつかんだまま残る子を待ち続けないため）
TERM_SECONDS = 5     # 時間切れのとき、SIGTERM の後に SIGKILL まで待つ秒数（make が作りかけのファイルを消せるように）
ENV = "SKILL_BUILD_"  # シェルに値を渡す環境変数の名前の頭

SHELL_MISSING = {
    "cmd": "shell の cmd は Windows でだけ使えます。auto か bash にしてください。",
    "powershell": "PowerShell（pwsh か powershell）が見つかりません。PowerShell を入れるか、shell を変えてください。",
    "bash": ("bash が見つかりません。Git for Windows を入れるか、shell を cmd か powershell にしてください"
             "（WSL の bash は使えません）。" if IS_WINDOWS else
             "bash が見つかりません。bash を入れるか、shell を変えてください。"),
}
# check で、PATH を探さない語（シェルの中のコマンドなど）
BUILTINS = {
    "bash": {".", "cd", "source", "export", "set", "if", "for", "while", "case", "exec", "eval",
             "time", "pushd", "{", "[["},
    "cmd": {"call", "cd", "chdir", "pushd", "set", "setlocal", "if", "for", "echo", "start"},
    "powershell": {"cd", "set-location", "push-location", "invoke-expression", "iex", "if",
                   "foreach", "echo", "write-output", "write-host"},
}

# エラーと警告らしい行。make が表示するコマンドの -Werror や error.c は拾わない
ERROR_RE = re.compile(r"\berror\b\s*(?::|\[|[a-z]{0,4}\d+)|undefined reference|multiple definition"
                      r"|No such file or directory|^FAILED:|CMake Error|エラー", re.IGNORECASE)
WARNING_RE = re.compile(r"\bwarning\b\s*(?::|\[|[a-z]{0,4}\d+)|CMake Warning|警告", re.IGNORECASE)
CONTEXT_RE = re.compile(r"^\s*\d*\s*\|")  # gcc や clang が添えるソースの行（例：「  12 | error: ...」）
# bash・cmd・PowerShell（英語と日本語）の「コマンドが見つからない」の文
NOT_FOUND_RE = re.compile(r"command not found|: not found|is not recognized|コマンドが見つかりません"
                          r"|内部コマンドまたは外部コマンド|認識されません", re.IGNORECASE)
ELF_MACHINES = {2: "SPARC", 3: "x86", 8: "MIPS", 20: "PowerPC", 21: "PowerPC64", 40: "ARM",
                42: "SuperH", 43: "SPARC V9", 62: "x86-64", 94: "Xtensa", 183: "AArch64",
                243: "RISC-V", 258: "LoongArch"}

# bash：sdk_env を読み込み、source_dir に戻ってから、目印のファイルを作る。その後にコマンドを足す。
# pipefail は「make | tee build.log」の make の失敗も失敗にするため。
BASH_SCRIPT = """\
if [ -n "${SKILL_BUILD_SDK_ENV-}" ]; then
  . "$SKILL_BUILD_SDK_ENV" || exit $?
  cd "$SKILL_BUILD_DIR" || exit $?
  : > "$SKILL_BUILD_LOADED"
fi
set -o pipefail
"""
# PowerShell：-Command で渡すので、Windows のコマンドラインで崩れる「"」と「\」を使わない。
# コマンドは環境変数で受け取り、成功したかを、コマンドの最後の文の直後の $? で見る。
# Windows PowerShell 5.1 は、ネイティブのコマンドの標準エラー出力を NativeCommandError として
# 記録することがある。それだけなら失敗にしない。見つからないコマンドは 127 で終える。
PS_SCRIPT = """\
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
if ($env:SKILL_BUILD_SDK_ENV) {
    $global:LASTEXITCODE = 0
    try { . $env:SKILL_BUILD_SDK_ENV } catch { Write-Output $_; exit 1 }
    if ($global:LASTEXITCODE) { exit $global:LASTEXITCODE }
    Set-Location -LiteralPath $env:SKILL_BUILD_DIR
    [IO.File]::WriteAllText($env:SKILL_BUILD_LOADED, '')
}
$global:LASTEXITCODE = 0
$Error.Clear()
$global:SkillBuildOk = $false
try {
    Invoke-Expression ($env:SKILL_BUILD_COMMAND + [char]10 + '$global:SkillBuildOk = $?')
} catch {
    Write-Output $_
}
if ($global:LASTEXITCODE) { exit $global:LASTEXITCODE }
if ($global:SkillBuildOk) { exit 0 }
$e = @($Error | Where-Object { [string]$_.FullyQualifiedErrorId -notlike 'NativeCommandError*' })
if ($e.Count -eq 0) { exit 0 }
if ([string]$e[0].FullyQualifiedErrorId -like 'CommandNotFoundException*') { exit 127 }
exit 1
"""
PS_OPTIONS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]


class Problem(Exception):
    """設定や引数の問題（終了コード 2）。"""

    def __init__(self, problems):
        super().__init__(problems[0])
        self.problems = problems


def result(ok, message, config=None, log=None, problems=(), **extra):
    data = {"ok": ok, "message": message, "problems": list(problems), "config": config, "log": log}
    data.update(extra)
    return data


def parse_args(argv):
    usage = "使い方：cross_build.py run|check|init [--workdir 作業フォルダ]"
    command, workdir, i = None, None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--workdir":
            if i + 1 >= len(argv):
                raise Problem(["--workdir の後に作業フォルダを書いてください。" + usage])
            workdir = argv[i + 1]
            i += 1
        elif arg.startswith("--workdir="):
            workdir = arg.split("=", 1)[1]
        elif arg in ("run", "check", "init") and command is None:
            command = arg
        else:
            raise Problem(["引数 {} は使えません。{}".format(arg, usage)])
        i += 1
    if command is None:
        raise Problem(["run・check・init のどれかを指定してください。" + usage])
    return command, os.path.abspath(os.path.expanduser(workdir or os.getcwd()))


def data_dir(workdir):
    return os.path.join(workdir, "." + SKILL)


def prepare_dir(path):
    """フォルダを作り、中身を Git に入れないための .gitignore を、ほかのものより先に置く。"""
    os.makedirs(path, exist_ok=True)
    ignore = os.path.join(path, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as f:
            f.write("*\n")


def unquote(value):
    """パスを囲む引用符を外す（INI では、引用符も値の一部になるため）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def find_shell(shell):
    """シェルの実行ファイルの場所。見つからなければ None。"""
    if shell == "cmd":
        return (os.environ.get("ComSpec") or shutil.which("cmd")) if IS_WINDOWS else None
    if shell == "powershell":
        return shutil.which("pwsh") or shutil.which("powershell")
    if not IS_WINDOWS:
        return shutil.which("bash")
    # System32 と WindowsApps の bash.exe は WSL を起動するもので、環境変数も Windows のパスも
    # 渡らない。Git for Windows などの bash を、PATH とふつうの入れ場所から探す。
    places = os.environ.get("PATH", "").split(os.pathsep)
    for var, sub in (("ProgramW6432", "Git"), ("ProgramFiles", "Git"),
                     ("LOCALAPPDATA", os.path.join("Programs", "Git"))):
        if os.environ.get(var):
            places.append(os.path.join(os.environ[var], sub, "bin"))
    system32 = os.path.normcase(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"))
    for place in places:
        exe = os.path.join(place.strip('" '), "bash.exe")
        key = os.path.normcase(os.path.abspath(exe))
        if os.path.isfile(exe) and not key.startswith(system32) and "windowsapps" not in key:
            return exe
    return None


def load(config, workdir):
    """設定を読み、(値, 問題のリスト) を返す。設定ファイルを読めなければ Problem。"""
    if not os.path.isfile(config):
        raise Problem(["設定ファイル {} がありません。init で作ってください。".format(config)])
    parser = configparser.ConfigParser(interpolation=None)  # 値の中の % をそのまま扱う
    try:
        with open(config, encoding="utf-8-sig") as f:  # BOM があっても読める
            parser.read_file(f)
    except UnicodeDecodeError:
        raise Problem(["設定ファイルを UTF-8 として読めません。UTF-8 で保存し直してください。"])
    except configparser.DuplicateOptionError as e:
        raise Problem(["設定ファイルの {} 行目：{} が2回書かれています。".format(e.lineno, e.option)])
    except configparser.MissingSectionHeaderError:
        raise Problem(["設定ファイルの最初の項目より前に、[settings] の行を書いてください。"])
    except configparser.ParsingError as e:
        lines = "、".join(str(n) for n, _ in e.errors)
        raise Problem(["設定ファイルの {} 行目を読めません。「項目 = 値」の形で書いてください。".format(lines)])
    except (configparser.Error, OSError) as e:
        raise Problem(["設定ファイルを読めません（{}）。".format(e)])
    if not parser.has_section("settings"):
        raise Problem(["設定ファイルに [settings] の節がありません。"])
    problems = ["[{}] の節は使いません。項目は [settings] に書いてください。".format(name)
                for name in parser.sections() if name != "settings"]
    section = parser["settings"]
    problems += ["{} という項目はありません。名前を確かめてください。".format(key)
                 for key in section if key not in KEYS]

    def get(key):
        return section.get(key, "").strip()

    def path(value):
        return os.path.normpath(os.path.join(workdir, os.path.expanduser(unquote(value))))

    s = {"source_dir": path(get("source_dir") or ".")}
    if not os.path.isdir(s["source_dir"]):
        problems.append("source_dir のフォルダ {} がありません。".format(s["source_dir"]))

    shell = get("shell") or "auto"
    if shell not in SHELLS:
        problems.append("shell の {} は使えません。auto・bash・cmd・powershell のどれかにしてください。".format(shell))
        shell = "auto"
    if shell == "auto":
        shell = "cmd" if IS_WINDOWS else "bash"
    s["shell"] = shell
    s["shell_program"] = find_shell(shell)
    if not s["shell_program"]:
        problems.append(SHELL_MISSING[shell])

    s["sdk_env"] = path(get("sdk_env")) if get("sdk_env") else ""
    if s["sdk_env"] and not os.path.isfile(s["sdk_env"]):
        problems.append("sdk_env のファイル {} がありません。".format(s["sdk_env"]))
    elif s["sdk_env"] and shell in SDK_SUFFIXES and not s["sdk_env"].lower().endswith(SDK_SUFFIXES[shell]):
        problems.append("sdk_env は、shell が {} なので、{} のファイルにしてください。".format(
            shell, " か ".join(SDK_SUFFIXES[shell])))

    s["build_command"] = get("build_command")
    if not s["build_command"]:
        problems.append("build_command が空です。ビルドのコマンドを書いてください。")
    elif "\n" in s["build_command"]:
        problems.append("build_command は1行で書いてください。")

    s["outputs"] = [unquote(line.strip()) for line in get("outputs").splitlines() if line.strip()]
    if not s["outputs"]:
        problems.append("outputs が空です。ビルドでできるファイルを書いてください。")

    try:
        s["timeout_seconds"] = float(get("timeout_seconds") or DEFAULT_TIMEOUT)
    except ValueError:
        s["timeout_seconds"] = float("nan")
    if not 0 < s["timeout_seconds"] < float("inf"):
        problems.append("timeout_seconds は、0 より大きい秒数にしてください。")
        s["timeout_seconds"] = DEFAULT_TIMEOUT
    return s, problems


def first_word(command, shell):
    """コマンドの最初の語（bash の「X=1」や記号は飛ばす）。引用符が閉じていなければ ValueError。"""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    if shell != "bash":
        lex.escape = ""  # Windows のパスの「\」をそのまま読む
        if shell == "cmd":
            lex.quotes = '"'
    for word in list(lex):  # 先に全部を読み、引用符の閉じ忘れを見つける
        if not word.strip("();<>|&"):
            continue
        if shell == "bash" and re.match(r"[A-Za-z_]\w*=", word):
            continue
        if shell == "powershell" and word == ".":
            continue
        return word.lstrip("@") if shell == "cmd" else word
    return None


def command_exists(word, s):
    """最初の語が、PATH か source_dir にあるか。シェルの中のコマンドなど、調べられないものは True。"""
    shell = s["shell"]
    if word.lower() in BUILTINS[shell]:
        return True
    if shell == "powershell" and (word.startswith("$") or re.match(r"^[A-Za-z]+-[A-Za-z]+$", word)):
        return True  # 変数か、Get-Item のような PowerShell のコマンド
    if "/" in word or (IS_WINDOWS and "\\" in word):
        return os.path.exists(os.path.join(s["source_dir"], os.path.expanduser(word)))
    if shutil.which(word):
        return True
    return shell == "cmd" and shutil.which(word, path=s["source_dir"]) is not None


def cmd_init(workdir):
    data = data_dir(workdir)
    config = os.path.join(data, "config.ini")
    if not os.path.isdir(workdir):
        raise Problem(["作業フォルダ {} がありません。".format(workdir)])
    if os.path.exists(config):
        return 0, result(True, "設定ファイルはもうあります。変えていません。", config, created=False)
    prepare_dir(data)
    shutil.copyfile(TEMPLATE, config)
    return 0, result(True, "設定ファイルを作りました。build_command と outputs を書いてから、check で確かめてください。",
                     config, created=True)


def cmd_check(workdir):
    config = os.path.join(data_dir(workdir), "config.ini")
    s, problems = load(config, workdir)
    notes = []
    if s["build_command"]:
        try:
            word = first_word(s["build_command"], s["shell"])
        except ValueError:
            problems.append("build_command の引用符が閉じていません。")
            word = None
        if word and not command_exists(word, s):
            if s["sdk_env"]:
                notes.append("build_command の {} は、今の PATH にありません。sdk_env が PATH に足すなら、"
                             "このままで動きます。".format(word))
            else:
                problems.append("build_command の {} が見つかりません。PC に入っているかと、PATH を確かめてください。".format(word))
    extra = {"notes": notes, "settings": s}
    if problems:
        return 2, result(False, "設定に問題があります。" + "".join(problems), config, problems=problems, **extra)
    return 0, result(True, "設定に問題はありません。" + "".join(notes), config, **extra)


def fallback_encoding():
    """UTF-8 で読めない行に使う、OS の文字コード（日本語の Windows では cp932）。
    3.11 からは locale.getencoding() を使う。getpreferredencoding() は、Python の UTF-8 モードでは
    UTF-8 を返してしまい、cp932 の出力が化けるため。"""
    get = getattr(locale, "getencoding", None)
    name = get() if get else locale.getpreferredencoding(False)
    try:
        return codecs.lookup(name).name
    except (LookupError, TypeError):
        return "utf-8"


FALLBACK = fallback_encoding()


def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(FALLBACK, "replace")


class Scanner:
    """出力を1行ずつ読んでログに書き、エラー・警告らしい行と最後の行を集める。"""

    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.stopped = False
        self.errors, self.warnings = [], []
        self.tail = collections.deque(maxlen=TAIL_LINES)

    def read(self, pipe):
        for raw in iter(pipe.readline, b""):
            line = decode(raw).rstrip("\r\n")
            with self.lock:
                if self.stopped:
                    return
                self.log.write(line + "\n")
                self.add(line)

    def add(self, line):
        short = line.strip()[:LINE_CHARS]
        self.tail.append(short)
        if CONTEXT_RE.match(line):
            return
        if ERROR_RE.search(line):
            if len(self.errors) < MAX_LINES:
                self.errors.append(short)
        elif WARNING_RE.search(line) and len(self.warnings) < MAX_LINES:
            self.warnings.append(short)

    def stop(self):
        with self.lock:
            self.stopped = True


def shell_path(s, path):
    """シェルに渡すパス。Windows の bash には「/」で区切って渡す。"""
    return path.replace("\\", "/") if IS_WINDOWS and s["shell"] == "bash" else path


def shell_command(s, tmp, loaded):
    """シェルを起動する引数と環境変数。sdk_env を読み込めたら、loaded のファイルができる。"""
    env = dict(os.environ)
    env.pop(ENV + "SDK_ENV", None)
    env[ENV + "DIR"] = shell_path(s, s["source_dir"])
    env[ENV + "LOADED"] = shell_path(s, loaded)
    if s["sdk_env"]:
        env[ENV + "SDK_ENV"] = shell_path(s, s["sdk_env"])
    command = s["build_command"]
    if s["shell"] == "bash":
        script = os.path.join(tmp, "build.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:  # CRLF だと bash が読めない
            f.write(BASH_SCRIPT + command + "\n")
        return [s["shell_program"], shell_path(s, script)], env
    if s["shell"] == "powershell":
        env[ENV + "COMMAND"] = command
        return [s["shell_program"]] + PS_OPTIONS + [PS_SCRIPT], env
    # cmd：1行で動かす。/s は、/c の後ろの最初と最後の「"」だけを外し、残りを打ち込んだとおりに動かす
    if s["sdk_env"]:
        command = ('call "%{0}SDK_ENV%" && cd /d "%{0}DIR%" && type nul > "%{0}LOADED%" && {1}'
                   .format(ENV, command))
    return '"{}" /d /s /c "{}"'.format(s["shell_program"], command), env


def kill_tree(proc):
    """シェルと、その子や孫のプロセスをすべて止める。"""
    if IS_WINDOWS:
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)  # start_new_session で、シェルがグループの頭になっている
            except OSError:
                pass
            if sig == signal.SIGTERM:
                try:
                    proc.wait(TERM_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
    proc.wait()


def execute(s, log):
    """sdk_env とビルドのコマンドを1つのシェルで動かし、出力をログに書く。"""
    tmp = tempfile.mkdtemp(prefix="build-")
    try:
        loaded = os.path.join(tmp, "loaded")
        argv, env = shell_command(s, tmp, loaded)
        scan = Scanner(log)
        options = {} if IS_WINDOWS else {"start_new_session": True}
        start = time.monotonic()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, cwd=s["source_dir"], env=env, **options)
        except OSError as e:
            return {"start_error": str(e), "exit_code": None, "timed_out": False, "loaded": False,
                    "duration": 0.0, "scan": scan}
        reader = threading.Thread(target=scan.read, args=(proc.stdout,), daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=s["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
        except BaseException:
            kill_tree(proc)
            raise
        duration = time.monotonic() - start
        reader.join(DRAIN_SECONDS)
        scan.stop()
        return {"start_error": None, "exit_code": None if timed_out else proc.returncode,
                "timed_out": timed_out, "loaded": os.path.exists(loaded), "duration": duration,
                "scan": scan}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stat_of(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def elf_machine(path):
    """ELF のファイルなら、どの CPU 向けか（例：ARM）。ELF でなければ None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b"\x7fELF" or head[5] not in (1, 2):
        return None
    number = int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")
    return ELF_MACHINES.get(number, "e_machine {}".format(number))


def new_log(workdir):
    data = data_dir(workdir)
    prepare_dir(data)
    logs = os.path.join(data, "logs")
    os.makedirs(logs, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        path = os.path.join(logs, stamp + ("" if n == 1 else "-{}".format(n)) + ".log")
        try:
            return path, open(path, "x", encoding="utf-8", buffering=1)
        except FileExistsError:
            n += 1


def run_message(s, r, names, outputs):
    scan = r["scan"]
    rc = r["exit_code"]
    if r["start_error"]:
        return "シェル {} を起動できませんでした（{}）。shell を確かめてください。".format(
            s["shell_program"], r["start_error"])
    if r["timed_out"]:
        during = "sdk_env の読み込みの途中でした。" if s["sdk_env"] and not r["loaded"] else ""
        return ("ビルドが {:g} 秒で終わらなかったので、子のプロセスも含めて止めました。{}"
                "入力を待っていないか、timeout_seconds が短すぎないかを確かめてください。").format(
                    s["timeout_seconds"], during)
    if s["sdk_env"] and not r["loaded"]:
        if rc == 0:
            return ("sdk_env（{}）の読み込みの途中で、シェルが終わりました。sdk_env の中で exit していないかを"
                    "確かめてください。").format(s["sdk_env"])
        return ("sdk_env（{}）の読み込みに失敗しました（終了コード {}）。SDK が入っているかと、"
                "sdk_env の最後のコマンドが 0 以外で終わっていないかを確かめてください。").format(s["sdk_env"], rc)
    if rc != 0:
        not_found = [line for line in scan.tail if NOT_FOUND_RE.search(line)]
        if not_found or rc == 127:
            line = not_found[0] if not_found else ([l for l in scan.tail if l] or ["終了コード 127"])[-1]
            return "コマンドが見つかりません：「{}」。PC に入っているかと、PATH と sdk_env を確かめてください。".format(line)
        detail = ("最初のエラーは「{}」です。".format(scan.errors[0]) if scan.errors
                  else "ログの最後（tail）を確かめてください。")
        return "ビルドが失敗しました（終了コード {}）。{}".format(rc, detail)
    missing = [n for n, o in zip(names, outputs) if not o["exists"]]
    if missing:
        return "ビルドのコマンドは成功しましたが、{} ができていません。outputs と build_command を確かめてください。".format(
            "、".join(missing))
    files = ["{}（{}{:,} バイト）".format(n, o["elf_machine"] + "、" if o["elf_machine"] else "", o["size"])
             for n, o in zip(names, outputs)]
    msg = "ビルドに成功しました（{:.1f} 秒）。ファイル：{}。".format(r["duration"], "、".join(files))
    stale = [n for n, o in zip(names, outputs) if not o["updated"]]
    if stale:
        msg += "ただし、{} は更新されていません（前のビルドのままです）。".format("、".join(stale))
    return msg


def cmd_run(workdir):
    config = os.path.join(data_dir(workdir), "config.ini")
    s, problems = load(config, workdir)
    if problems:
        return 2, result(False, "設定に問題があるため、ビルドしませんでした。" + "".join(problems), config,
                         problems=problems)
    names = s["outputs"]
    paths = [os.path.normpath(os.path.join(s["source_dir"], os.path.expanduser(n))) for n in names]
    before = [stat_of(p) for p in paths]
    log_path, log = new_log(workdir)
    with log:
        log.write("\n".join([
            "{} のログ".format(SKILL),
            "開始：{}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "設定ファイル：{}".format(config),
            "source_dir：{}".format(s["source_dir"]),
            "shell：{}（{}）".format(s["shell"], s["shell_program"]),
            "sdk_env：{}".format(s["sdk_env"] or "なし"),
            "build_command：{}".format(s["build_command"]),
            "outputs：{}".format("、".join(names)),
            "timeout_seconds：{:g}".format(s["timeout_seconds"]),
            "---- ビルドの出力 ----",
            ""]))
        r = execute(s, log)
        outputs = []
        for p, old in zip(paths, before):
            now = stat_of(p)
            outputs.append({"path": p, "exists": now is not None, "updated": now is not None and now != old,
                            "size": now[1] if now else None, "elf_machine": elf_machine(p) if now else None})
        message = run_message(s, r, names, outputs)
        ok = (r["exit_code"] == 0 and (r["loaded"] or not s["sdk_env"])
              and all(o["exists"] for o in outputs))
        log.write("---- ここまで ----\n終了コード：{}　{:.1f} 秒{}\n".format(
            r["exit_code"], r["duration"], "　時間切れ" if r["timed_out"] else ""))
        for o in outputs:
            log.write("{}：{}\n".format(o["path"], "{} バイト　{}".format(o["size"], o["elf_machine"] or "")
                                         if o["exists"] else "ありません"))
        log.write("結果：{}\n".format(message))
    scan = r["scan"]
    return (0 if ok else 1), result(
        ok, message, config, log_path, exit_code=r["exit_code"],
        duration_seconds=round(r["duration"], 1), timed_out=r["timed_out"], error_lines=scan.errors,
        warning_lines=scan.warnings, tail=list(scan.tail), outputs=outputs)


COMMANDS = {"run": cmd_run, "check": cmd_check, "init": cmd_init}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = None
    try:
        command, workdir = parse_args(argv)
        config = os.path.join(data_dir(workdir), "config.ini")
        code, data = COMMANDS[command](workdir)
    except Problem as e:
        code, data = 2, result(False, "".join(e.problems), config, problems=e.problems)
    except KeyboardInterrupt:
        code, data = 1, result(False, "途中で止められました。ビルドは終わっていません。", config)
    except OSError as e:
        code, data = 1, result(False, "ファイルを読み書きできませんでした（{}）。フォルダの権限を確かめてください。".format(e),
                               config)
    except Exception as e:
        traceback.print_exc()
        code, data = 1, result(False, "スクリプトの中で思わぬエラーが起きました（{}: {}）。詳しくは標準エラー出力を"
                               "見てください。".format(type(e).__name__, e), config)
    # ASCII だけで書く（日本語は \uXXXX）。PowerShell からも bash からも化けずに読める
    sys.stdout.write(json.dumps(data, ensure_ascii=True, indent=2) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
