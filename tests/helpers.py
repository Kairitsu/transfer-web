import shutil
import tempfile
import unittest
from pathlib import Path

from transfer_web.config import Config, Endpoint

HAS_RSYNC = shutil.which("rsync") is not None
needs_rsync = unittest.skipUnless(HAS_RSYNC, "rsync is not installed")


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


def local_config(base, **overrides):
    """Two local endpoints under ``base`` (enough to exercise real rsync)."""
    base = Path(base)
    (base / "a").mkdir(exist_ok=True)
    (base / "b").mkdir(exist_ok=True)
    config = Config(
        path=base / "config.ini",
        listen="127.0.0.1",
        port=0,
        state_dir=base / "state",
        log_dir=base / "logs",
        endpoints={
            "a": Endpoint(id="a", name="甲", type="local", root=str(base / "a")),
            "b": Endpoint(id="b", name="乙", type="local", root=str(base / "b")),
        },
        left="a",
        right="b",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config
