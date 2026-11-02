"""Exercise the real GTK widgets and video sinks without a recorder or credentials."""

import argparse
import faulthandler
from pathlib import Path
import tempfile
import traceback
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import viewer
from viewer import Adw, GLib, Gtk


class Verification(viewer.Viewer):
    def __init__(self, directory):
        super().__init__(argparse.Namespace(demo=True, smoke_test=False))
        self.directory = Path(directory)
        self.failed = False

    def do_activate(self):
        super().do_activate()
        self.preview = Gtk.WidgetPaintable.new(self.window)
        GLib.timeout_add_seconds(3, self.check)

    def capture(self, name):
        snapshot = Gtk.Snapshot()
        self.preview.snapshot(snapshot, self.window.get_width(), self.window.get_height())
        node = snapshot.to_node()
        if node:
            self.window.get_renderer().render_texture(node, None).save_to_png(str(self.directory / name))

    def fail(self):
        self.failed = True
        traceback.print_exc()
        self.disconnect()
        self.quit()

    def check(self):
        try:
            assert all(camera.check_frame() for camera in self.cameras)
            for camera in self.cameras:
                sinks = camera.pipeline.iterate_sinks()
                result, sink = sinks.next()
                assert result == viewer.Gst.IteratorResult.OK
                assert sink.get_property("sync"), "Video presentation must follow the playback clock"
            self.capture("overview.png")
            self.select_view(2)
            assert self.focused is self.cameras[1]
            assert self.cameras[0].get_parent() is None
            def descendants(widget):
                yield widget
                child = widget.get_first_child()
                while child:
                    yield from descendants(child)
                    child = child.get_next_sibling()
            icons = [widget.get_icon_name() for widget in descendants(self.window) if isinstance(widget, Gtk.Button)]
            assert icons.count("camera-photo-symbolic") == 1, "Focused view should have one snapshot button"
            assert "view-fullscreen-symbolic" not in icons, "No fullscreen button in the header"
            self.cameras[0].volume_button.set_value(.3)
            self.cameras[1].volume_button.set_value(.4)
            assert self.cameras[0].audio_volume == 0
            assert self.cameras[1].audio_volume == .4
            self.select_view(3)
            assert all(camera.audio_volume == 0 for camera in self.cameras)
            self.select_view(2)
            path = self.save_snapshot(self.focused, self.directory)
            assert path and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            self.select_view(0)
            self.preferences()
            dialog = self.window.get_visible_dialog()
            assert isinstance(dialog, Adw.PreferencesDialog)
            dialog.close()
            self.connection_dialog()
            dialog = self.window.get_visible_dialog()
            assert isinstance(dialog, Adw.Dialog)
            dialog.close()
            print("PASS: four video sinks, camera navigation, PNG snapshot, preferences, connection dialog")
            self.window.set_default_size(520, 800)
            GLib.timeout_add_seconds(2, self.check_narrow)
        except Exception:
            self.fail()
        return False

    def check_narrow(self):
        try:
            assert self.split.get_collapsed() and self.narrow
            self.capture("narrow.png")
            camera = self.cameras[0]
            camera.stop()
            camera.show_status("error", "Camera unavailable", "Check the connection and try again.")
            self.update_frames()
            assert self.banner.get_revealed() and camera.retry_button.get_visible()
            self.retry_camera(camera)
            GLib.timeout_add_seconds(2, self.finish)
        except Exception:
            self.fail()
        return False

    def finish(self):
        try:
            assert all(camera.check_frame() for camera in self.cameras)
            self.update_frames()
            assert not self.banner.get_revealed()
            self.disconnect()
            assert all(camera.pipeline is None for camera in self.cameras)
            assert self.content_stack.get_visible_child_name() == "empty"
            assert "password" not in viewer.CONFIG.read_text().lower()
            print("PASS: narrow layout, error recovery, disconnect, and preferences contain no password")
            self.window.close()
        except Exception:
            self.fail()
        return False


if __name__ == "__main__":
    faulthandler.dump_traceback_later(25, exit=True)
    with tempfile.TemporaryDirectory(prefix="cctv-ui-check-") as directory:
        viewer.CONFIG = Path(directory) / "settings.json"
        app = Verification(directory)
        app.run(["cctv-ui-check"])
        raise SystemExit(1 if app.failed else 0)
