"""vcs.py on git and svn: base revision, work copies, reset, diff to patch bytes, apply."""

import shutil
import tempfile
import unittest
from pathlib import Path

import support
from support import GIT, call
import vcs


class Case(unittest.TestCase):
    kind = "git"

    def setUp(self):
        if self.kind == "svn" and not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn コマンドがありません")
        self.top = Path(tempfile.mkdtemp(prefix="triage-test-")).resolve()
        self.addCleanup(vcs.remove_tree, self.top)


class Copies:
    """The same behaviour on git and on svn."""

    def test_copy_diff_reset_apply_remove(self):
        repo = vcs.open_repo(support.make_repo(self.kind, self.top))
        self.assertEqual(repo.kind, self.kind)
        _branch, revision, _note = repo.base("")
        w1, w2 = self.top / "work" / "w1", self.top / "work" / "w2"
        repo.make_copy(w1, revision)
        repo.make_copy(w1, revision)  # nothing to do the second time
        self.assertTrue(repo.is_copy(w1) and not repo.is_copy(w2))
        self.assertEqual(repo.diff(w1, revision), ([], b""))

        (w1 / "src" / "a.c").write_bytes(b"int a(int *p)\n{\n    return p ? *p : 0;\n}\n")
        (w1 / "src" / "b.c").unlink()
        support.write(w1, {"src/sub/new.c": b"int n;\n"})
        files, patch = repo.diff(w1, revision)
        self.assertEqual(files, ["src/a.c", "src/b.c", "src/sub/new.c"])
        self.assertIn(b"+    return p ? *p : 0;", patch)
        patch_file = self.top / "fix.patch"
        patch_file.write_bytes(patch)

        repo.reset(w1, revision)
        self.assertEqual({p.name: p.read_bytes() for p in (w1 / "src").iterdir()}, {
            name[4:]: data for name, data in support.FILES.items()})
        self.assertEqual(repo.diff(w1, revision), ([], b""))

        repo.make_copy(w2, revision)
        (w2 / "src" / "a.c").write_bytes(b"int a(int *p)\n{\n    return 42;\n}\n")  # the patch does not fit here
        self.assertNotEqual(repo.apply(w2, patch_file), "")
        self.assertFalse((w2 / "src" / "sub").exists() or list(w2.rglob("*.rej")), "nothing is half applied")
        repo.reset(w2, revision)
        self.assertEqual(repo.apply(w2, patch_file), "")
        self.assertEqual((w2 / "src" / "sub" / "new.c").read_bytes(), b"int n;\n")
        self.assertFalse((w2 / "src" / "b.c").exists())

        repo.remove(self.top / "work")
        self.assertFalse((self.top / "work").exists())
        self.assertEqual(support.user_tree(self.kind, self.top / "repo"), b"")
        with self.assertRaisesRegex(vcs.VcsError, "フォルダーがありません"):  # not: the command is not installed
            repo.diff(w1, revision)

    def test_a_deleted_file_with_an_at_sign_and_the_clean_bytes_of_a_file(self):
        repo = vcs.open_repo(support.make_repo(self.kind, self.top, {**support.FILES, "src/icon@2x.c": b"int i;\n"}))
        revision, copy = repo.base("")[1], self.top / "work" / "w1"
        repo.make_copy(copy, revision)
        (copy / "src" / "icon@2x.c").unlink()
        (copy / "src" / "sjis.c").write_bytes(b"changed\n")
        self.assertEqual(repo.diff(copy, revision)[0], ["src/icon@2x.c", "src/sjis.c"])
        self.assertEqual(repo.original(copy, revision, "src/sjis.c"), support.SJIS)
        self.assertEqual(repo.original(copy, revision, "src/icon@2x.c"), b"int i;\n")
        self.assertIsNone(repo.original(copy, revision, "src/none.c"))

    def test_a_copy_that_a_killed_job_left_half_made_is_made_again(self):
        repo = vcs.open_repo(support.make_repo(self.kind, self.top))
        revision, copy = repo.base("")[1], self.top / "work" / "w1"
        if self.kind == "git":  # what a killed git worktree add leaves: registered and locked, the files not there
            call(*GIT, "worktree", "add", "-q", "--detach", "--no-checkout", copy, revision, cwd=repo.repo)
            (repo.repo / ".git" / "worktrees" / "w1" / "locked").write_bytes(b"initializing")
        else:  # what a killed svn checkout leaves: a working copy without its files
            call("svn", "checkout", "-q", "--depth", "empty", (self.top / "svnrepo").as_uri(), copy, cwd=self.top)
        self.assertFalse(repo.is_copy(copy))
        repo.make_copy(copy, revision)
        self.assertTrue(repo.is_copy(copy))
        self.assertEqual((copy / "src" / "a.c").read_bytes(), support.FILES["src/a.c"])


class GitTest(Copies, Case):

    def test_base_is_the_fetched_branch_of_origin(self):
        origin = support.make_repo("git", self.top / "origin")
        user = self.top / "user"
        call(*GIT, "clone", "-q", origin, user, cwd=self.top)
        support.write(origin, {"src/c.c": b"int c;\n"})  # origin moves on after the clone
        call(*GIT, "add", "-A", cwd=origin)
        call(*GIT, "commit", "-q", "-m", "more", cwd=origin)
        head = call(*GIT, "rev-parse", "HEAD", cwd=origin).decode().strip()
        repo = vcs.open_repo(user)
        self.assertEqual(repo.base(""), ("main", head, ""))
        repo.make_copy(self.top / "w1", head)
        self.assertTrue((self.top / "w1" / "src" / "c.c").is_file(), "the copy is at the fetched revision")
        self.assertEqual(len(call(*GIT, "worktree", "list", cwd=user).splitlines()), 2)
        repo.remove(self.top / "w1")
        self.assertEqual(len(call(*GIT, "worktree", "list", cwd=user).splitlines()), 1)

        origin.rename(self.top / "gone")  # origin cannot be reached: the revision fetched last, and a note
        branch, revision, note = repo.base("main")
        self.assertEqual((branch, revision), ("main", head))
        self.assertIn("取得できなかった", note)

    def test_base_is_the_branch_checked_out_and_with_a_detached_head_the_default_of_origin(self):
        origin = support.make_repo("git", self.top / "origin")
        call(*GIT, "branch", "develop", cwd=origin)
        user = self.top / "user"
        call(*GIT, "clone", "-q", origin, user, cwd=self.top)
        call(*GIT, "checkout", "-q", "develop", cwd=user)
        call(*GIT, "checkout", "-q", "develop", cwd=origin)
        support.write(origin, {"src/d.c": b"int d;\n"})  # origin's develop moves on after the clone
        call(*GIT, "add", "-A", cwd=origin)
        call(*GIT, "commit", "-q", "-m", "develop", cwd=origin)
        pushed = call(*GIT, "rev-parse", "HEAD", cwd=origin).decode().strip()
        call(*GIT, "checkout", "-q", "main", cwd=origin)
        repo = vcs.open_repo(user)
        self.assertEqual(repo.base(""), ("develop", pushed, ""), "the pushed state of the branch checked out")
        call(*GIT, "checkout", "-q", "-b", "mine", cwd=user)  # a branch origin does not have: the local one
        self.assertEqual(repo.base(""), ("mine", call(*GIT, "rev-parse", "HEAD", cwd=user).decode().strip(), ""))
        call(*GIT, "checkout", "-q", "--detach", cwd=user)
        main = call(*GIT, "rev-parse", "main", cwd=origin).decode().strip()
        self.assertEqual((repo.checked_out(), repo.base("")), ("main", ("main", main, "")))

    def test_base_without_origin_is_the_current_branch(self):
        repo = vcs.open_repo(support.make_repo("git", self.top))
        head = call(*GIT, "rev-parse", "HEAD", cwd=repo.repo).decode().strip()
        self.assertEqual(repo.base(""), ("main", head, ""))
        with self.assertRaisesRegex(vcs.VcsError, "ブランチ nowhere が見つかりません"):
            repo.base("nowhere")

    def test_a_folder_that_is_no_repository(self):
        with self.assertRaisesRegex(vcs.VcsError, "git または svn のリポジトリではありません"):
            vcs.open_repo(self.top)


class SvnTest(Copies, Case):
    kind = "svn"

    def test_base_is_the_highest_revision_of_the_working_copy_and_more_slots_are_folder_copies(self):
        user = support.make_repo("svn", self.top)
        repo = vcs.open_repo(user)
        self.assertEqual(repo.base(""), ("", "1", ""))
        mine = b"int a(int *p)\n{\n    return p ? *p : 0;\n}\n"
        (user / "src" / "a.c").write_bytes(mine)  # the person commits and does not update: the folder stays at 1
        call("svn", "commit", "-q", "-m", "mine", cwd=user)
        (user / "src" / "b.c").write_bytes(b"not committed\n")
        self.assertEqual(call("svnversion", cwd=user).strip(), b"1:2M")
        self.assertEqual(repo.base(""), ("", "2", ""))
        w1, w2 = self.top / "work" / "w1", self.top / "work" / "w2"
        repo.make_copy(w1, "2")
        self.assertEqual((w1 / "src" / "a.c").read_bytes(), mine, "the copy has what the person has committed")
        (w1 / "src" / "a.c").write_bytes(b"changed by a worker\n")
        (self.top / "svnrepo").rename(self.top / "unreachable")  # a second checkout would fail now
        repo.make_copy(w2, "2")
        self.assertTrue(repo.is_copy(w2))
        self.assertEqual((w2 / "src" / "a.c").read_bytes(), mine, "the copy is clean")
        self.assertEqual(repo.diff(w2, "2"), ([], b""))

    def test_files_of_an_external_are_no_part_of_a_fix(self):
        url = (self.top / "svnrepo").as_uri()
        call("svnadmin", "create", self.top / "svnrepo", cwd=self.top)
        call("svn", "mkdir", "-q", "-m", "folders", f"{url}/proj", f"{url}/lib", cwd=self.top)
        for name, files in (("lib", {"util.c": b"int u;\n"}), ("proj", {"src/a.c": b"int a;\n"})):
            call("svn", "checkout", "-q", f"{url}/{name}", self.top / name, cwd=self.top)
            support.write(self.top / name, files)
            call("svn", "add", "-q", "--force", ".", cwd=self.top / name)
            if name == "proj":
                call("svn", "propset", "-q", "svn:externals", "^/lib lib", ".", cwd=self.top / name)
            call("svn", "commit", "-q", "-m", name, cwd=self.top / name)
        call("svn", "update", "-q", cwd=self.top / "proj")
        repo, copy = vcs.open_repo(self.top / "proj"), self.top / "work" / "w1"
        revision = repo.base("")[1]
        repo.make_copy(copy, revision)
        support.write(copy, {"lib/util.c": b"int u2;\n", "lib/new.c": b"int n;\n", "src/a.c": b"int a2;\n"})
        with self.assertRaisesRegex(vcs.VcsError, "lib/new.c、lib/util.c は svn:externals で取り込んだ別の場所のファイル"):
            repo.diff(copy, revision)
        repo.reset(copy, revision)  # also inside the external
        self.assertEqual((copy / "lib" / "util.c").read_bytes(), b"int u;\n")
        self.assertFalse((copy / "lib" / "new.c").exists())
        self.assertEqual(repo.original(copy, revision, "lib/util.c"), b"int u;\n", "it can be read and be evidence")
        (copy / "src" / "a.c").write_bytes(b"int a2;\n")
        self.assertEqual(repo.diff(copy, revision)[0], ["src/a.c"])


if __name__ == "__main__":
    unittest.main()
