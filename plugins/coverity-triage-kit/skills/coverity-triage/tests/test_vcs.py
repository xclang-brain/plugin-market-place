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

    def test_base_is_the_revision_of_the_working_copy_and_more_slots_are_folder_copies(self):
        repo = vcs.open_repo(support.make_repo("svn", self.top))
        self.assertEqual(repo.base(""), ("", "1", ""))
        w1, w2 = self.top / "work" / "w1", self.top / "work" / "w2"
        repo.make_copy(w1, "1")
        (w1 / "src" / "a.c").write_bytes(b"changed by a worker\n")
        (self.top / "svnrepo").rename(self.top / "unreachable")  # a second checkout would fail now
        repo.make_copy(w2, "1")
        self.assertTrue(repo.is_copy(w2))
        self.assertEqual((w2 / "src" / "a.c").read_bytes(), support.FILES["src/a.c"], "the copy is clean")
        self.assertEqual(repo.diff(w2, "1"), ([], b""))


if __name__ == "__main__":
    unittest.main()
