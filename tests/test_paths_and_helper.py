import os
import unittest

from transfer_web import helper
from transfer_web.errors import AppError
from transfer_web.paths import validate_rel

from .helpers import TempDirTestCase


class ValidateRelTests(unittest.TestCase):
    def test_root_aliases(self):
        for value in (None, "", "."):
            self.assertEqual(validate_rel(value), "")

    def test_accepts_names_the_old_blacklist_refused(self):
        for value in ("a..b.txt", "f;1.txt", "a|b", "name$(x).txt", "back\\slash", "dir a/x_y.txt"):
            self.assertEqual(validate_rel(value), value)

    def test_rejects_traversal_and_bad_segments(self):
        for value in ("/etc", "..", "a/../b", "a//b", "a/./b", "a/", "line\nbreak", "nul\x00"):
            with self.assertRaises(AppError, msg=value):
                validate_rel(value)


class HelperTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        (self.root / "dir").mkdir(parents=True)
        (self.root / "dir" / "inner_file.txt").write_text("x")
        (self.root / ".hidden").write_text("h")
        (self.root / "file_a.txt").write_text("a")
        (self.root / "link").symlink_to(self.root / "file_a.txt")
        (self.tmp / "outside").mkdir()
        (self.root / "escape").symlink_to(self.tmp / "outside")

    def run_op(self, **request):
        return helper.run(dict(request, root=str(self.root)))

    def test_list_hides_dotfiles_unless_asked(self):
        names = [entry["name"] for entry in self.run_op(op="list", path="")["entries"]]
        self.assertNotIn(".hidden", names)
        names = [entry["name"] for entry in self.run_op(op="list", path="", showHidden=True)["entries"]]
        self.assertIn(".hidden", names)

    def test_list_marks_symlinks_unsafe_and_sorts_dirs_first(self):
        entries = self.run_op(op="list", path="")["entries"]
        self.assertEqual(entries[0]["name"], "dir")
        link = next(entry for entry in entries if entry["name"] == "link")
        self.assertFalse(link["safe"])
        self.assertEqual(link["type"], "symlink")

    def test_newline_names_are_listed_but_unsafe(self):
        (self.root / "bad\nname").write_text("x")
        entry = next(e for e in self.run_op(op="list", path="")["entries"] if e["name"] == "bad\nname")
        self.assertFalse(entry["safe"])
        self.assertEqual(entry["path"], "")

    def test_symlink_escape_is_refused(self):
        with self.assertRaises(helper.HelperError):
            self.run_op(op="list", path="escape")
        with self.assertRaises(helper.HelperError):
            self.run_op(op="mkdir", path="escape/new")
        self.assertFalse((self.tmp / "outside" / "new").exists())

    def test_check_reports_each_source(self):
        results = self.run_op(op="check", paths=["dir", "file_a.txt", "missing", "link"])["results"]
        self.assertEqual([item["ok"] for item in results], [True, True, False, False])

    def test_mkdir_and_stat(self):
        self.run_op(op="mkdir", path="new/deep")
        self.assertTrue((self.root / "new" / "deep").is_dir())
        self.assertEqual(self.run_op(op="stat", path="new/deep"), {"path": "new/deep", "exists": True, "type": "dir"})
        self.assertFalse(self.run_op(op="stat", path="nope")["exists"])

    def test_remote_script_round_trip(self):
        """The script streamed to remote servers runs standalone and prints one JSON line."""
        import json
        import subprocess
        import sys

        script = helper.remote_script({"op": "list", "path": "dir", "root": str(self.root)})
        proc = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True, check=True,
                              env=dict(os.environ, LANG="C", LC_ALL="C"))
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertEqual([entry["name"] for entry in payload["entries"]], ["inner_file.txt"])


if __name__ == "__main__":
    unittest.main()
