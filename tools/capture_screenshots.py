#!/usr/bin/env python3
"""Capture the real application with synthetic feeds and temporary settings."""

import argparse
import faulthandler
from pathlib import Path
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import viewer
from viewer import GLib, Gst, Gtk, Viewer


class Screenshots(Viewer):
    def __init__(self, output):
        super().__init__(argparse.Namespace(demo=True, smoke_test=False))
        self.output = output
        self.settings.update(host="demo.local", username="", remember=False, appearance=1,
                             names=["Entrance", "Driveway", "Garden", "Garage"])

    def do_activate(self):
        super().do_activate()
        self.window.set_default_size(1280, 820)
        self.paintable = Gtk.WidgetPaintable.new(self.window)
        for camera, pattern in zip(self.cameras, (0, 11, 23, 24)):
            status, source = camera.pipeline.iterate_sources().next()
            if status == Gst.IteratorResult.OK:
                source.set_property("pattern", pattern)
        GLib.timeout_add_seconds(3, self.overview)

    def capture(self, name):
        snapshot = Gtk.Snapshot()
        self.paintable.snapshot(snapshot, self.window.get_width(), self.window.get_height())
        node = snapshot.to_node()
        if node is None:
            raise RuntimeError("The application window has not rendered")
        texture = self.window.get_renderer().render_texture(node, None)
        if not texture.save_to_png(str(self.output / name)):
            raise OSError("Could not save screenshot")
        print(f"Captured {name}", flush=True)

    def step(self, operation):
        try:
            operation()
        except Exception:
            traceback.print_exc()
            self.exit_code = 1
            self.quit()
        return False

    def overview(self):
        def operation():
            assert all(camera.check_frame() for camera in self.cameras)
            self.capture("overview.png")
            self.settings["appearance"] = 2
            self.apply_appearance()
            self.select_view(1)
            GLib.timeout_add_seconds(1, self.focused_shot)
        return self.step(operation)

    def focused_shot(self):
        def operation():
            self.capture("focused.png")
            self.preferences()
            GLib.timeout_add_seconds(1, self.preferences_shot)
        return self.step(operation)

    def preferences_shot(self):
        def operation():
            self.capture("preferences.png")
            self.window.get_visible_dialog().close()
            self.window.close()
        return self.step(operation)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/screenshots")
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    faulthandler.dump_traceback_later(25, exit=True)
    with tempfile.TemporaryDirectory(prefix="cctv-screenshots-") as directory:
        viewer.CONFIG = Path(directory) / "settings.json"
        app = Screenshots(options.output)
        app.run(["cctv-screenshots"])
        raise SystemExit(app.exit_code)
