"""Install and launch real desktop entries from unusual checkout paths."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import gi

gi.require_version("GioUnix", "2.0")
from gi.repository import Gio, GioUnix


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_installed_launcher_preserves_checkout_path(self):
        for name in ('space name', 'dollar$name', 'quote"name', 'back`tick', 'back\\name', 'percent%name'):
            with self.subTest(checkout=name), tempfile.TemporaryDirectory(prefix="cctv-install-") as directory:
                base = Path(directory)
                checkout = base / name
                (checkout / "tools").mkdir(parents=True)
                shutil.copy2(ROOT / "tools/install.py", checkout / "tools/install.py")
                shutil.copytree(ROOT / "data/icons", checkout / "data/icons")
                marker = base / "launched.txt"
                script = checkout / "viewer.py"
                script.write_text(
                    "from pathlib import Path\nimport sys\n"
                    f"Path({str(marker)!r}).write_text(sys.argv[0])\n"
                )
                data_home = base / "data"
                subprocess.run([sys.executable, str(checkout / "tools/install.py")], check=True,
                               env={**os.environ, "XDG_DATA_HOME": str(data_home)}, capture_output=True)
                launcher = data_home / "applications/io.local.CCTVViewer.desktop"
                application = GioUnix.DesktopAppInfo.new_from_filename(str(launcher))
                self.assertIsNotNone(application, "Installed launcher must load in GLib")
                application.launch([], Gio.AppLaunchContext())
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "Installed launcher must run the viewer")
                self.assertEqual(marker.read_text(), str(script))


if __name__ == "__main__":
    unittest.main()
