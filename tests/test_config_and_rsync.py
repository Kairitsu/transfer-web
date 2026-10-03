import unittest
from pathlib import Path

from transfer_web import rsync
from transfer_web.config import ConfigError, Endpoint, load_config
from transfer_web.endpoints import EndpointClient
from transfer_web.errors import AppError

from .helpers import TempDirTestCase

REPO = Path(__file__).resolve().parent.parent


class ConfigTests(TempDirTestCase):
    def write(self, text):
        path = self.tmp / "config.ini"
        path.write_text(text, encoding="utf-8")
        return path

    def test_example_config_loads(self):
        config = load_config(REPO / "config.example.ini")
        self.assertEqual(len(config.endpoints), 2)
        self.assertNotEqual(config.left, config.right)
        self.assertTrue(config.require_tailscale)

    def test_tailscale_is_required_by_default(self):
        endpoints = "[endpoint:a]\nroot = /a\n[endpoint:b]\nroot = /b\n"
        self.assertTrue(load_config(self.write(endpoints)).require_tailscale)
        for listen in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(listen):
                config = load_config(self.write(f"[server]\nlisten = {listen}\n" + endpoints))
                self.assertTrue(config.require_tailscale)
        with self.assertRaises(ConfigError):
            load_config(self.write("[server]\nlisten = 0.0.0.0\n" + endpoints))
        config = load_config(self.write("[server]\nlisten = 0.0.0.0\nrequire_tailscale = no\n" + endpoints))
        self.assertFalse(config.require_tailscale)
        with self.assertRaises(ConfigError):
            load_config(self.write("[server]\nrequire_tailscale = maybe\n" + endpoints))

    def test_names_and_layout_come_from_config(self):
        config = load_config(self.write("""
[ui]
title = 文件传输
left = remote
right = here

[endpoint:here]
name = 首尔
type = local
root = /srv/data/

[endpoint:remote]
name = 硅谷
type = ssh
host = us-box
user = ubuntu
root = /home/ubuntu
"""))
        self.assertEqual(config.title, "文件传输")
        self.assertEqual((config.left, config.right), ("remote", "here"))
        self.assertEqual(config.endpoints["here"].root, "/srv/data")
        self.assertEqual(config.endpoints["remote"].ssh_target, "ubuntu@us-box")

    def test_relative_state_dirs_resolve_next_to_config(self):
        config = load_config(self.write("""
[server]
state_dir = state
[endpoint:a]
root = /a
[endpoint:b]
root = /b
"""))
        self.assertEqual(config.state_dir, (self.tmp / "state").resolve())

    def test_invalid_configs(self):
        cases = {
            "one endpoint": "[endpoint:a]\nroot = /a\n",
            "relative root": "[endpoint:a]\nroot = a\n[endpoint:b]\nroot = /b\n",
            "ssh without host": "[endpoint:a]\nroot = /a\n[endpoint:b]\ntype = ssh\nroot = /b\n",
            "bad id": "[endpoint:a b]\nroot = /a\n[endpoint:b]\nroot = /b\n",
            "same sides": "[ui]\nleft = a\nright = a\n[endpoint:a]\nroot = /a\n[endpoint:b]\nroot = /b\n",
            "owner on ssh": "[endpoint:a]\nroot = /a\n[endpoint:b]\ntype = ssh\nhost = h\nowner = x\nroot = /b\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                load_config(self.write(text))


def client(endpoint_id, kind="local", **extra):
    return EndpointClient(Endpoint(id=endpoint_id, name=endpoint_id, type=kind, root=f"/{endpoint_id}", **extra))


class RsyncTests(unittest.TestCase):
    def test_command_never_uses_append_mode(self):
        cmd = rsync.build_command(client("a"), client("b"), ["x"], "")
        self.assertFalse(any(arg.startswith("--append") for arg in cmd))
        self.assertIn("--partial-dir=.rsync-partial", cmd)

    def test_remote_source_uses_ssh_and_one_invocation(self):
        remote = client("r", "ssh", host="h", user="u", port=2200)
        cmd = rsync.build_command(remote, client("l"), ["dir a", "f;1.txt"], "in")
        self.assertIn("-e", cmd)
        self.assertIn("-p 2200", cmd[cmd.index("-e") + 1])
        self.assertEqual(cmd[-3:], ["u@h:/r/dir a", "u@h:/r/f;1.txt", "/l/in/"])

    def test_options(self):
        owned = EndpointClient(Endpoint(id="o", name="o", type="local", root="/o", owner="azureuser"))
        cmd = rsync.build_command(client("a"), owned, ["x"], "", skip_existing=True, dry_run=True)
        self.assertIn("--ignore-existing", cmd)
        self.assertIn("--chown=azureuser", cmd)
        self.assertIn("--dry-run", cmd)

    def test_remote_to_remote_and_same_endpoint_are_refused(self):
        r1 = client("r1", "ssh", host="h1")
        r2 = client("r2", "ssh", host="h2")
        with self.assertRaises(AppError):
            rsync.build_command(r1, r2, ["x"], "")
        with self.assertRaises(AppError):
            rsync.build_command(r1, r1, ["x"], "")

    def test_parse_progress(self):
        line = "      3,000,004  99%   45.41MB/s    0:00:03 (xfr#2, to-chk=2/5)"
        self.assertEqual(rsync.parse_progress(line), {"bytes": 3000004, "percent": 99, "speed": "45.41MB/s", "eta": "0:00:03"})
        self.assertIsNone(rsync.parse_progress("dir a/big.bin"))

    def test_parse_preview(self):
        output = "\n".join([
            ">f+++++++++ new.txt",
            "cd+++++++++ dir/",
            ">f.st...... dir/changed.txt",
            "<f..t...... pushed.txt",
            ".d..t...... old/",
            "cL+++++++++ link -> target",
            "",
            "Number of files: 6 (reg: 3, dir: 2, link: 1)",
            "Total file size: 1,234 bytes",
            "Total transferred file size: 1,000 bytes",
        ])
        preview = rsync.parse_preview(output)
        self.assertEqual(preview["newFiles"], 1)
        self.assertEqual(preview["newDirs"], 1)
        self.assertEqual(preview["updatedCount"], 2)
        self.assertEqual(preview["updated"], ["dir/changed.txt", "pushed.txt"])
        self.assertEqual((preview["transferSize"], preview["totalSize"], preview["files"]), (1000, 1234, 6))


if __name__ == "__main__":
    unittest.main()
