"""Exercise benchmark startup with a synthetic password and forbid keyring writes."""

import argparse
import faulthandler
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import viewer
from tools.benchmark import Benchmark
from viewer import GLib, Viewer


class ProbeCredentials:
    available = True

    def __init__(self):
        self.writes = []

    def store(self, *_args):
        self.writes.append("store")

    def clear(self, *_args):
        self.writes.append("clear")


class Check(Benchmark):
    def __init__(self):
        options = argparse.Namespace(quality=None, duration=1, clock_sync=False,
                                     unsynchronized=False, snapshot_check=False, require_smooth=False)
        super().__init__(options)
        self.settings.update(host="example.invalid", username="test-user", remember=True)
        self.credentials = ProbeCredentials()

    def connect_cameras(self, demo=None):
        # Drive the real environment-password startup branch, with local test video.
        Viewer.connect_cameras(self, demo=True)

    def do_activate(self):
        super().do_activate()
        GLib.timeout_add(700, self.verify)

    def verify(self):
        try:
            assert self.password, "Startup did not consume the test credential"
            assert all(camera.pipeline for camera in self.cameras)
            assert not self.credentials.writes, "Benchmark startup persisted credentials"
            self.settings["remember"] = False
            self.save_login()
            self.forget_login()
            assert not self.credentials.writes, "Benchmark deleted a saved login"
            print("PASS: benchmark startup cannot store or delete keyring credentials", flush=True)
        except AssertionError as error:
            self.exit_code = 1
            print(f"FAIL: {error}", file=sys.stderr)
        finally:
            GLib.source_remove(self.heartbeat)
            self.window.close()
        return False


if __name__ == "__main__":
    faulthandler.dump_traceback_later(15, exit=True)
    with tempfile.TemporaryDirectory(prefix="cctv-benchmark-check-") as directory:
        viewer.CONFIG = Path(directory) / "settings.json"
        with patch.dict(os.environ, {"CCTV_PASSWORD": "synthetic-test-value"}):
            app = Check()
            app.run(["benchmark-regression-check"])
        raise SystemExit(app.exit_code)
