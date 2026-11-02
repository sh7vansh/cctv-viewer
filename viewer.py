#!/usr/bin/env python3
"""A small, local GTK4/libadwaita viewer for a four-channel RTSP recorder."""

import argparse
from datetime import datetime
import ipaddress
import json
import os
import subprocess
from pathlib import Path
import sys
import threading
from urllib.parse import urlencode

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk
from credentials import Credentials
import discovery


CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cctv-viewer/settings.json"
DEFAULTS = {"host": "", "port": 554, "username": "",
            "substream": True, "names": [f"Camera {i}" for i in range(1, 5)],
            "appearance": 0, "remember": False}


def read_settings():
    try:
        saved = json.loads(CONFIG.read_text())
        result = DEFAULTS.copy()
        for key, default in DEFAULTS.items():
            value = saved.get(key, default)
            if type(value) is type(default):
                result[key] = value
        if len(result["names"]) != 4 or not all(isinstance(n, str) and n.strip() for n in result["names"]):
            result["names"] = DEFAULTS["names"].copy()
        if not 1 <= result["port"] <= 65535:
            result["port"] = 554
        if result["appearance"] not in (0, 1, 2):
            result["appearance"] = 0
        return result
    except (OSError, ValueError, AttributeError):
        return DEFAULTS.copy()


def validate_host(value):
    """Accept a hostname, IPv4, or IPv6 address; never accept URL credentials."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value)
        return f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        import re

        if len(value) > 253 or not re.fullmatch(
            r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value
        ):
            raise ValueError("Enter an IP address or hostname, without a URL or port.")
        return value


class Camera(Gtk.Box):
    def __init__(self, channel, on_focus, on_snapshot, on_retry, on_audio):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.channel = channel
        self.pipeline = None
        self.bus = None
        self.bus_handler = None
        self.source_handler = None
        self.password = ""
        self.username = ""
        self.first_frame = False
        self.state = "disconnected"
        self.started_at = 0
        self.audio_volume = 0.0
        self.add_css_class("card")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_hexpand(True)
        self.set_vexpand(True)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("camera-bar")
        self.name_label = Gtk.Label(label=f"Camera {channel}", xalign=0, hexpand=True, ellipsize=3)
        self.name_label.add_css_class("heading")
        bar.append(self.name_label)
        self.status = Gtk.Label(label="Disconnected")
        self.status.add_css_class("dim-label")
        bar.append(self.status)
        self.volume_button = Gtk.VolumeButton(sensitive=False, tooltip_text="Camera audio")
        self.volume_button.set_value(0)
        self.volume_button.connect("value-changed", lambda _button, value: on_audio(self, value))
        bar.append(self.volume_button)
        self.snapshot_button = Gtk.Button(icon_name="camera-photo-symbolic", tooltip_text="Save a snapshot", sensitive=False)
        self.snapshot_button.add_css_class("flat")
        self.snapshot_button.connect("clicked", lambda *_: on_snapshot(self))
        bar.append(self.snapshot_button)
        menu = Gio.Menu()
        for label, action in (("Focus Camera", "focus"), ("Reconnect Camera", "retry")):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("app." + action, GLib.Variant("i", channel))
            menu.append_item(item)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Camera options")
        more.add_css_class("flat")
        bar.append(more)
        self.append(bar)

        self.video_stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.picture = Gtk.Picture(hexpand=True, vexpand=True, can_shrink=True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.add_css_class("video")
        self.video_stack.add_named(self.picture, "video")
        self.placeholder = Adw.StatusPage(icon_name="camera-video-symbolic", title="Disconnected",
                                           description="Connect to your recorder to view this camera.")
        self.placeholder.add_css_class("compact")
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(width_request=24, height_request=24, visible=False)
        controls.append(self.spinner)
        self.retry_button = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER, visible=False)
        self.retry_button.connect("clicked", lambda *_: on_retry(self))
        controls.append(self.retry_button)
        self.placeholder.set_child(controls)
        self.video_stack.add_named(self.placeholder, "status")
        self.append(self.video_stack)
        click = Gtk.GestureClick()
        click.connect("released", lambda _g, n, _x, _y: on_focus(self) if n == 2 else None)
        self.picture.add_controller(click)

    def start(self, settings, password, demo=False):
        self.stop()
        self.first_frame = False
        self.username, self.password = settings["username"], password
        sink = Gst.ElementFactory.make("gtk4paintablesink")
        # Present by timestamp: RTSP/TCP delivers bursts even at a steady 25 fps.
        sink.set_property("sync", True)
        self.picture.set_paintable(sink.get_property("paintable"))
        if demo:
            self.pipeline = Gst.Pipeline.new(None)
            source = Gst.ElementFactory.make("videotestsrc")
            source.set_property("is-live", True)
            source.set_property("pattern", self.channel - 1)
            convert = Gst.ElementFactory.make("videoconvert")
            for element in (source, convert, sink):
                self.pipeline.add(element)
            source.link(convert)
            convert.link(sink)
        else:
            self.pipeline = Gst.ElementFactory.make("playbin")
            query = urlencode({"channel": self.channel, "subtype": int(settings["substream"])})
            host = validate_host(settings["host"])
            self.pipeline.set_property("uri", f"rtsp://{host}:{settings['port']}/cam/realmonitor?{query}")
            self.pipeline.set_property("video-sink", sink)
            self.pipeline.set_property("flags", 3)  # Video and audio, initially muted.
            self.pipeline.set_property("mute", True)
            self.pipeline.set_property("volume", 0.0)
            self.source_handler = self.pipeline.connect("source-setup", self.configure_source)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus_handler = self.bus.connect("message", self.on_message)
        self.started_at = GLib.get_monotonic_time()
        self.show_status("connecting", "Connecting…", "Waiting for video from the recorder.")
        self.pipeline.set_state(Gst.State.PLAYING)

    def configure_source(self, _playbin, source):
        if source.find_property("user-id"):
            source.set_property("user-id", self.username)
            source.set_property("user-pw", self.password)
        if source.find_property("protocols"):
            source.set_property("protocols", 4)  # GstRtsp.RTSPLowerTrans.TCP
        if source.find_property("latency"):
            source.set_property("latency", 200)
        if source.find_property("tcp-timeout"):
            source.set_property("tcp-timeout", 8_000_000)

    def on_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            # Never display raw GStreamer errors: some include authenticated URLs.
            unauthorized = any(s in error.message.lower() for s in ("401", "unauthorized", "not authorized"))
            text = ("Check your username, password, and camera permissions." if unauthorized else
                    "Connection failed. Check the recorder address and network, then reconnect.")
            self.stop()
            self.show_status("error", "Camera unavailable", text)
        elif message.type == Gst.MessageType.EOS:
            self.stop()
            self.show_status("error", "Stream ended", "Reconnect to resume viewing.")

    def show_status(self, state, title, description=""):
        self.state = state
        self.status.set_label({"live": "Live", "error": "Unavailable", "connecting": "Connecting…",
                               "disconnected": "Disconnected"}[state])
        self.placeholder.set_title(title)
        self.placeholder.set_description(description)
        self.placeholder.set_icon_name("network-error-symbolic" if state == "error" else "camera-video-symbolic")
        self.spinner.set_visible(state == "connecting")
        self.retry_button.set_visible(state == "error")
        self.snapshot_button.set_sensitive(state == "live")
        self.video_stack.set_visible_child_name("video" if state == "live" else "status")

    def check_frame(self):
        paintable = self.picture.get_paintable()
        if self.pipeline and not self.first_frame and paintable and paintable.get_intrinsic_width() > 0:
            self.first_frame = True
            self.show_status("live", "Live")
        elif self.pipeline and not self.first_frame and GLib.get_monotonic_time() - self.started_at > 20_000_000:
            self.stop()
            self.show_status("error", "No video received", "Check the camera channel and network, then try again.")
        has_audio = bool(self.pipeline and self.pipeline.find_property("n-audio")
                         and self.pipeline.get_property("n-audio") > 0)
        self.volume_button.set_sensitive(self.state == "live" and has_audio)
        self.volume_button.set_tooltip_text("Camera audio" if has_audio else "No audio track available")
        return self.first_frame

    def set_volume(self, value):
        self.audio_volume = value
        if self.volume_button.get_value() != value:
            self.volume_button.set_value(value)
        if self.pipeline and self.pipeline.find_property("mute"):
            self.pipeline.set_property("volume", value)
            self.pipeline.set_property("mute", value == 0)

    def stop(self):
        self.set_volume(0)
        self.volume_button.set_sensitive(False)
        if self.bus:
            self.bus.disconnect(self.bus_handler)
            self.bus.remove_signal_watch()
            self.bus = None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            if self.source_handler is not None:
                self.pipeline.disconnect(self.source_handler)
                self.source_handler = None
            self.pipeline = None
        self.password = ""
        self.first_frame = False
        self.picture.set_paintable(None)
        self.show_status("disconnected", "Disconnected", "Connect to your recorder to view this camera.")


class Viewer(Adw.Application):
    def __init__(self, options, *, persist_credentials=True):
        super().__init__(application_id="io.local.CCTVViewer", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.options = options
        self.persist_credentials = persist_credentials
        self.settings = read_settings()
        self.password = ""
        self.focused = None
        self.exit_code = 0
        self.connected = False
        self.demo = options.demo
        self.narrow = False
        self.last_summary = None
        self.credentials = Credentials()
        self.discovery_cancel = None
        self.closing = False
        self.changing_audio = False
        self.login_generation = 0

    def add_action(self, name, callback, parameter=None, shortcuts=()):
        action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameter) if parameter else None)
        action.connect("activate", callback)
        Gio.ActionMap.add_action(self, action)
        if shortcuts:
            self.set_accels_for_action("app." + name, shortcuts)
        return action

    def do_activate(self):
        if self.get_active_window():
            self.get_active_window().present()
            return
        Gst.init(None)
        if ((Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 16)
                or (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6)
                or Gst.version()[:2] < (1, 24)):
            print("CCTV Viewer requires GTK 4.16+, libadwaita 1.6+, and GStreamer 1.24+.", file=sys.stderr)
            self.exit_code = 1
            self.quit()
            return
        # Prefer the Linux display GPU's VA decoder. On hybrid laptops, the
        # default NVIDIA path can download every frame back into system memory.
        # Leave explicit GStreamer rank overrides intact for troubleshooting.
        if "GST_PLUGIN_FEATURE_RANK" not in os.environ:
            decoder = Gst.ElementFactory.find("vah264dec")
            if decoder:
                decoder.set_rank(max(decoder.get_rank(), int(Gst.Rank.PRIMARY) + 64))
        required = ("gtk4paintablesink", "playbin", "rtspsrc", "rtph264depay", "h264parse", "avdec_h264")
        missing = [name for name in required if not Gst.ElementFactory.find(name)]
        if missing:
            print("Missing GStreamer plugins: " + ", ".join(missing), file=sys.stderr)
            self.exit_code = 1
            self.quit()
            return
        self.apply_appearance()
        icons = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icons.add_search_path(str(Path(__file__).resolve().parent / "data/icons"))
        Gtk.Window.set_default_icon_name("io.local.CCTVViewer")
        css = Gtk.CssProvider()
        css.load_from_string("""
            .camera-bar { padding: 6px 6px 6px 12px; }
            .video { background: black; }
            .camera-navigation { padding: 6px; }
            .camera-navigation row { padding: 10px 12px; }
            .recorder-summary { padding: 18px; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Adw.ApplicationWindow(application=self, title="CCTV Viewer", default_width=1180, default_height=780,
                                            width_request=360, height_request=360)
        self.window.connect("close-request", self.close)
        self.add_action("connection", lambda *_: self.connection_dialog(), shortcuts=["<Control>o"])
        self.add_action("discover", lambda *_: self.discovery_dialog())
        self.add_action("forget-login", lambda *_: self.forget_login())
        self.add_action("reconnect", lambda *_: self.reconnect(), shortcuts=["<Control>r"])
        self.add_action("disconnect", lambda *_: self.disconnect())
        self.add_action("preferences", lambda *_: self.preferences(), shortcuts=["<Control>comma"])
        self.add_action("about", lambda *_: self.about())
        self.add_action("quit", lambda *_: self.window.close(), shortcuts=["<Control>q"])
        self.add_action("fullscreen", lambda *_: self.toggle_fullscreen(), shortcuts=["F11"])
        self.add_action("overview", lambda *_: self.select_view(0), shortcuts=["<Alt>0"])
        self.add_action("focus", lambda _a, p: self.select_view(p.get_int32()), "i")
        self.add_action("retry", lambda _a, p: self.retry_camera(self.cameras[p.get_int32() - 1]), "i")
        self.add_action("snapshot", lambda *_: self.save_snapshot(self.focused), shortcuts=["<Control>s"])
        self.add_action("snapshots", lambda *_: self.open_snapshots())
        for channel in range(1, 5):
            self.set_accels_for_action(f"app.focus({channel})", [f"<Alt>{channel}"])

        self.split = Adw.OverlaySplitView(min_sidebar_width=220, max_sidebar_width=260)
        self.toast_overlay = Adw.ToastOverlay(child=self.split)
        self.window.set_content(self.toast_overlay)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 800sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        breakpoint.connect("apply", lambda *_: self.set_narrow(True))
        breakpoint.connect("unapply", lambda *_: self.set_narrow(False))
        self.window.add_breakpoint(breakpoint)

        self.build_sidebar()
        self.toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_decoration_layout(":close")
        self.title = Adw.WindowTitle(title="All Cameras", subtitle="Not connected")
        header.set_title_widget(self.title)
        sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show or hide cameras")
        self.split.bind_property("show-sidebar", sidebar_toggle, "active", 3)  # BIDIRECTIONAL | SYNC_CREATE
        header.pack_start(sidebar_toggle)
        self.grid_button = Gtk.Button(icon_name="view-grid-symbolic", tooltip_text="All cameras (Alt+0)", visible=False,
                                       action_name="app.overview")
        header.pack_start(self.grid_button)
        self.toolbar.add_top_bar(header)

        self.banner = Adw.Banner(title="", revealed=False, button_label="Reconnect")
        self.banner.connect("button-clicked", lambda *_: self.reconnect())
        self.content_stack = Gtk.Stack()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.banner)
        body.append(self.content_stack)
        self.content_stack.set_vexpand(True)
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True, row_homogeneous=True)
        for setter in (self.grid.set_margin_top, self.grid.set_margin_bottom, self.grid.set_margin_start, self.grid.set_margin_end):
            setter(12)
        self.cameras = [Camera(i, self.focus_camera, self.save_snapshot, self.retry_camera, self.camera_audio) for i in range(1, 5)]
        self.layout()
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid)
        self.content_stack.add_named(scroller, "cameras")
        self.empty = Adw.StatusPage(icon_name="io.local.CCTVViewer", title="Your Cameras, Together",
                                    description="Connect to your recorder to watch your four cameras.")
        connect = Gtk.Button(label="Connect to Recorder", halign=Gtk.Align.CENTER, action_name="app.connection")
        connect.add_css_class("suggested-action")
        connect.add_css_class("pill")
        empty_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        empty_actions.append(connect)
        empty_actions.append(Gtk.Button(label="Find Cameras on Network", action_name="app.discover"))
        self.empty.set_child(empty_actions)
        self.content_stack.add_named(self.empty, "empty")
        self.content_stack.set_visible_child_name("empty")
        self.toolbar.set_content(body)
        self.split.set_content(self.toolbar)
        self.navigation.select_row(self.navigation.get_row_at_index(0))
        self.update_names()
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key_pressed)
        self.window.add_controller(keys)
        self.window.present()
        self.frame_timer = GLib.timeout_add(250, self.update_frames)
        if self.options.demo:
            self.connect_cameras(demo=True)
        elif os.environ.get("CCTV_PASSWORD"):
            # Optional session credential, also used by integration checks.
            self.password = os.environ.pop("CCTV_PASSWORD", "")
            self.connect_cameras()
            if not self.options.smoke_test:
                self.save_login()
        elif self.settings["remember"] and self.credentials.available:
            self.restore_login()
        elif self.options.smoke_test:
            self.connect_cameras()
        if self.options.smoke_test:
            GLib.timeout_add_seconds(12, self.smoke_result)

    def build_sidebar(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_decoration_layout(":close")
        header.set_title_widget(Gtk.Label(label="CCTV Viewer", css_classes=["title"]))
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Connection…", "app.connection")
        section.append("Find Cameras…", "app.discover")
        section.append("Reconnect", "app.reconnect")
        section.append("Disconnect", "app.disconnect")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Snapshots", "app.snapshots")
        section.append("Preferences", "app.preferences")
        section.append("Forget Saved Login", "app.forget-login")
        section.append("About CCTV Viewer", "app.about")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.navigation = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.navigation.add_css_class("navigation-sidebar")
        self.navigation.add_css_class("camera-navigation")
        self.nav_labels, self.nav_status = [], []
        for channel in range(5):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image.new_from_icon_name("view-grid-symbolic" if channel == 0 else "camera-video-symbolic"))
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            name = Gtk.Label(label="All Cameras" if channel == 0 else self.settings["names"][channel - 1],
                             xalign=0, ellipsize=3)
            labels.append(name)
            status = Gtk.Label(label="4 cameras" if channel == 0 else "Disconnected", xalign=0)
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            labels.append(status)
            box.append(labels)
            row.set_child(box)
            self.navigation.append(row)
            self.nav_labels.append(name)
            self.nav_status.append(status)
        self.navigation.connect("row-selected", self.on_selected)
        content.append(self.navigation)
        spacer = Gtk.Box(vexpand=True)
        content.append(spacer)
        summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        summary.add_css_class("recorder-summary")
        self.recorder_label = Gtk.Label(label="Home recorder", xalign=0, css_classes=["heading"])
        self.address_label = Gtk.Label(label=self.settings["host"], xalign=0, ellipsize=3, css_classes=["dim-label", "caption"])
        summary.append(self.recorder_label)
        summary.append(self.address_label)
        content.append(Gtk.Separator())
        content.append(summary)
        toolbar.set_content(content)
        self.split.set_sidebar(toolbar)

    def on_selected(self, _list, row):
        if row is None:
            return
        index = row.get_index()
        self.focused = self.cameras[index - 1] if index else None
        # Leaving a camera's focused view must not leave its audio playing hidden.
        for camera in self.cameras:
            if index and camera != self.focused:
                camera.set_volume(0)
        self.layout()
        self.grid_button.set_visible(bool(index))
        self.title.set_title(self.settings["names"][index - 1] if index else "All Cameras")
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def select_view(self, channel):
        self.navigation.select_row(self.navigation.get_row_at_index(channel))

    def camera_audio(self, camera, value):
        if self.changing_audio:
            return
        self.changing_audio = True
        try:
            if value > 0:
                for other in self.cameras:
                    if other != camera:
                        other.set_volume(0)
            camera.set_volume(value)
        finally:
            self.changing_audio = False

    def set_narrow(self, narrow):
        self.narrow = narrow
        for camera in self.cameras:
            camera.set_size_request(-1, 200 if narrow else 180)
        self.layout()

    def update_names(self):
        for i, camera in enumerate(self.cameras):
            camera.name_label.set_label(self.settings["names"][i])
            self.nav_labels[i + 1].set_label(self.settings["names"][i])
        self.address_label.set_label(self.settings["host"])
        self.title.set_title(self.settings["names"][self.focused.channel - 1] if self.focused else "All Cameras")

    def layout(self):
        for camera in self.cameras:
            if camera.get_parent():
                self.grid.remove(camera)
        visible = [self.focused] if self.focused else self.cameras
        for i, camera in enumerate(visible):
            columns = 1 if self.narrow or self.focused else 2
            self.grid.attach(camera, i % columns, i // columns, 1, 1)

    def focus_camera(self, camera):
        self.select_view(0 if self.focused == camera else camera.channel)

    def toggle_fullscreen(self):
        if self.window.is_fullscreen():
            self.window.unfullscreen()
        else:
            self.window.fullscreen()

    def key_pressed(self, _controller, key, _code, state):
        if key == Gdk.KEY_Escape:
            self.window.unfullscreen()
            self.select_view(0)
        else:
            return False
        return True

    def connection_dialog(self):
        existing = self.window.get_visible_dialog()
        if existing:
            return False
        dialog = Adw.Dialog(title="Connect to Recorder", content_width=440)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for setter in (content.set_margin_start, content.set_margin_end, content.set_margin_bottom):
            setter(24)
        group = Adw.PreferencesGroup()
        host = Adw.EntryRow(title="Recorder address", text=self.settings["host"])
        username = Adw.EntryRow(title="Username", text=self.settings["username"])
        password = Adw.PasswordEntryRow(title="Password", text=self.password)
        port = Adw.SpinRow.new_with_range(1, 65535, 1)
        port.set_title("RTSP Port")
        port.set_value(self.settings["port"])
        remember = Adw.SwitchRow(title="Remember Login", subtitle="Save your password in GNOME Keyring",
                                  active=self.settings["remember"], sensitive=self.credentials.available)
        if not self.credentials.available:
            remember.set_subtitle("Install libsecret to save passwords securely")
        for row in (host, port, username, password, remember):
            group.add(row)
        content.append(group)
        error = Gtk.Label(wrap=True, xalign=0, visible=False)
        error.add_css_class("error")
        content.append(error)
        connect = Gtk.Button(label="Connect", halign=Gtk.Align.CENTER)
        connect.add_css_class("suggested-action")
        connect.add_css_class("pill")

        def apply(*_args):
            try:
                validate_host(host.get_text())
                if not username.get_text().strip() or not password.get_text():
                    raise ValueError("Enter your recorder username and password.")
            except ValueError as exc:
                error.set_label(str(exc))
                error.set_visible(True)
                return
            self.settings.update(host=host.get_text().strip(), username=username.get_text().strip(), port=int(port.get_value()))
            self.settings["remember"] = remember.get_active() and self.credentials.available
            self.password = password.get_text()
            self.save_settings()
            self.save_login()
            self.update_names()
            password.set_text("")
            dialog.close()
            self.connect_cameras(demo=False)

        connect.connect("clicked", apply)
        password.connect("entry-activated", apply)
        content.append(connect)
        find = Gtk.Button(label="Find Cameras on Network", halign=Gtk.Align.CENTER)
        content.append(find)
        search_requested = [False]
        def find_cameras(*_args):
            search_requested[0] = True
            dialog.close()
        find.connect("clicked", find_cameras)
        dialog.connect("closed", lambda *_: GLib.idle_add(self.discovery_dialog) if search_requested[0] else None)
        toolbar.set_content(content)
        dialog.set_child(toolbar)
        dialog.set_default_widget(connect)
        dialog.set_focus(password if self.settings["username"] else username)
        dialog.present(self.window)
        if self.settings["remember"] and self.credentials.available and not password.get_text():
            account = self.settings.copy()
            def fill_saved_login(value, _error):
                if (value and not self.closing and self.window.get_visible_dialog() == dialog
                        and host.get_text().strip() == account["host"]
                        and username.get_text().strip() == account["username"]
                        and int(port.get_value()) == account["port"] and not password.get_text()):
                    password.set_text(value)
            self.credentials.lookup(account, fill_saved_login)
        return False

    def connect_cameras(self, demo=None):
        if demo is not None:
            self.demo = demo
        self.connected = True
        self.content_stack.set_visible_child_name("cameras")
        self.banner.set_revealed(False)
        for camera in self.cameras:
            camera.start(self.settings, self.password, self.demo)
        self.title.set_subtitle("Connecting…")
        self.last_summary = None

    def reconnect(self):
        if self.password or self.demo:
            self.connect_cameras()
        else:
            self.connection_dialog()

    def retry_camera(self, camera):
        if not self.password and not self.demo:
            self.connection_dialog()
            return
        self.connected = True
        self.content_stack.set_visible_child_name("cameras")
        camera.start(self.settings, self.password, self.demo)

    def disconnect(self):
        self.login_generation += 1
        for camera in self.cameras:
            camera.stop()
        self.password = ""
        self.connected = False
        self.content_stack.set_visible_child_name("empty")
        self.title.set_subtitle("Not connected")
        self.banner.set_revealed(False)
        self.last_summary = None

    def update_frames(self):
        for camera in self.cameras:
            camera.check_frame()
            self.nav_status[camera.channel].set_label(camera.status.get_label())
        live = sum(camera.state == "live" for camera in self.cameras)
        failed = sum(camera.state == "error" for camera in self.cameras)
        dimensions = ""
        if self.focused and self.focused.state == "live":
            paintable = self.focused.picture.get_paintable()
            dimensions = f"{paintable.get_intrinsic_width()} × {paintable.get_intrinsic_height()}"
        summary = (live, failed, self.connected, self.focused.channel if self.focused else 0,
                   self.settings["substream"], dimensions)
        if summary != self.last_summary:
            self.last_summary = summary
            self.nav_status[0].set_label(f"{live} of 4 live" if self.connected else "4 cameras")
            quality = "Low bandwidth" if self.settings["substream"] else "High quality"
            subtitle = f"{live} of 4 live · {quality}"
            if dimensions:
                subtitle = f"Live · {dimensions} · {quality}"
            self.title.set_subtitle(subtitle if self.connected else "Not connected")
            self.recorder_label.set_label("Demo recorder" if self.demo else "Home recorder")
            self.banner.set_title(f"{failed} camera{'s' if failed != 1 else ''} unavailable")
            self.banner.set_revealed(self.connected and failed > 0)
        return True

    def toast(self, title):
        toast = Adw.Toast.new(title)
        toast.set_use_markup(False)
        self.toast_overlay.add_toast(toast)
        return toast

    def save_settings(self):
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.settings, indent=2) + "\n")
            temporary.replace(CONFIG)
        except OSError:
            self.toast("Could not save preferences. Changes apply for this session.")

    def restore_login(self):
        self.login_generation += 1
        generation = self.login_generation
        attributes = self.credentials.attributes(self.settings)
        def restored(password, error):
            if (generation != self.login_generation or self.closing or not self.settings["remember"]
                    or attributes != self.credentials.attributes(self.settings) or self.connected):
                return
            if error:
                self.toast("Could not access the saved login. Connect manually.")
            elif password:
                self.password = password
                if not self.window.get_visible_dialog():
                    self.connect_cameras()
        self.credentials.lookup(self.settings, restored)

    def save_login(self):
        if not self.persist_credentials:
            return
        if self.settings["remember"] and self.password:
            self.credentials.store(self.settings, self.password,
                lambda success: self.toast("Could not save your password in the keyring.") if not success and not self.closing else None)
        elif not self.settings["remember"]:
            self.credentials.clear(self.settings,
                lambda success: self.toast("Could not remove the saved login.") if not success and not self.closing else None)

    def forget_login(self):
        if not self.persist_credentials:
            self.toast("Saved logins cannot be changed during a benchmark.")
            return
        self.login_generation += 1
        def cleared(success):
            if self.closing:
                return
            if success:
                self.settings["remember"] = False
                self.save_settings()
            self.toast("Saved login removed" if success else "Could not remove the saved login.")
        self.credentials.clear(self.settings, cleared)

    def discovery_dialog(self):
        if self.window.get_visible_dialog():
            return False
        dialog = Adw.Dialog(title="Find Cameras", content_width=480, content_height=520)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for setter in (box.set_margin_top, box.set_margin_bottom, box.set_margin_start, box.set_margin_end):
            setter(24)
        description = Gtk.Label(label="Finding your home network…", xalign=0, wrap=True)
        description.add_css_class("dim-label")
        box.append(description)
        progress = Gtk.ProgressBar()
        box.append(progress)
        results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        results.add_css_class("boxed-list")
        results.set_visible(False)
        box.append(results)
        status = Gtk.Label(label="Scanning for RTSP video services…", wrap=True, xalign=0)
        box.append(status)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=box)
        toolbar.set_content(scroller)
        dialog.set_child(toolbar)
        cancelled = threading.Event()
        self.discovery_cancel = cancelled
        selected = [None]
        found = []

        def choose(_row, device):
            selected[0] = device
            dialog.close()

        def closed(*_args):
            cancelled.set()
            if selected[0] and not self.closing:
                self.disconnect()
                self.settings.update(host=selected[0]["host"], port=selected[0]["port"])
                self.password = ""
                self.save_settings()
                self.update_names()
                GLib.idle_add(self.connection_dialog)
        dialog.connect("closed", closed)

        def publish(callback, *args):
            def deliver():
                if not cancelled.is_set() and not self.closing:
                    callback(*args)
                return False
            GLib.idle_add(deliver)

        def add_device(device):
            found.append(device)
            row = Adw.ActionRow(title=f"{device['host']}:{device['port']}",
                                subtitle="RTSP service detected" if device["verified"] else "Open port · RTSP not confirmed",
                                activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", choose, device)
            results.append(row)
            results.set_visible(True)

        def complete():
            progress.set_fraction(1)
            status.set_label(f"Found {len(found)} service{'s' if len(found) != 1 else ''}. Select one to connect."
                             if found else "No RTSP services found. Check that your recorder is on the same home network.")

        def worker():
            try:
                network, own_address = discovery.home_network()
                publish(description.set_label, f"Searching {network} on ports 554, 8554, and 10554.")
                discovery.scan(network, own_address, cancelled,
                               lambda device: publish(add_device, device),
                               lambda fraction: publish(progress.set_fraction, fraction))
                publish(complete)
            except (ValueError, OSError, GLib.Error, TimeoutError, subprocess.SubprocessError):
                publish(status.set_label, "Could not scan this network. Connect to home Wi-Fi or Ethernet and try again.")
        dialog.present(self.window)
        threading.Thread(target=worker, name="camera-discovery", daemon=True).start()
        return False

    def apply_appearance(self):
        schemes = [Adw.ColorScheme.DEFAULT, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK]
        Adw.StyleManager.get_default().set_color_scheme(schemes[self.settings["appearance"]])

    def preferences(self):
        if self.window.get_visible_dialog():
            return
        dialog = Adw.PreferencesDialog(title="Preferences", search_enabled=False)
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        display = Adw.PreferencesGroup(title="Viewing")
        quality = Adw.ComboRow(title="Video Quality", subtitle="Applies to all four cameras",
                               model=Gtk.StringList.new(["Low bandwidth", "High quality"]),
                               selected=0 if self.settings["substream"] else 1)
        display.add(quality)
        appearance = Adw.ComboRow(title="Appearance", model=Gtk.StringList.new(["Follow system", "Light", "Dark"]),
                                  selected=self.settings["appearance"])
        display.add(appearance)
        page.add(display)
        cameras = Adw.PreferencesGroup(title="Camera Names", description="Name cameras by their location.")
        names = []
        for i in range(4):
            row = Adw.EntryRow(title=f"Channel {i + 1}", text=self.settings["names"][i])
            cameras.add(row)
            names.append(row)
        page.add(cameras)
        shortcuts = Adw.PreferencesGroup(title="Keyboard Shortcuts")
        for title, key in (("All Cameras", "Alt+0"), ("Select Camera", "Alt+1–4"),
                           ("Save Snapshot", "Ctrl+S"), ("Reconnect", "Ctrl+R"),
                           ("Fullscreen", "F11"), ("Leave Fullscreen / All Cameras", "Esc")):
            row = Adw.ActionRow(title=title)
            label = Gtk.Label(label=key, css_classes=["dim-label"])
            row.add_suffix(label)
            shortcuts.add(row)
        page.add(shortcuts)
        dialog.add(page)

        def update_appearance(*_args):
            self.settings["appearance"] = appearance.get_selected()
            self.apply_appearance()
            self.save_settings()

        appearance.connect("notify::selected", update_appearance)

        def save(*_args):
            previous_quality = self.settings["substream"]
            self.settings["substream"] = quality.get_selected() == 0
            self.settings["names"] = [row.get_text().strip() or f"Camera {i + 1}" for i, row in enumerate(names)]
            self.save_settings()
            self.update_names()
            if self.connected and previous_quality != self.settings["substream"]:
                self.connect_cameras()

        dialog.connect("closed", save)
        dialog.present(self.window)

    def snapshot_directory(self):
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        return Path(pictures or Path.home() / "Pictures") / "CCTV"

    def save_snapshot(self, camera, directory=None):
        if camera is None or camera.state != "live":
            self.toast("Select a live camera to save a snapshot.")
            return None
        paintable = camera.picture.get_paintable()
        width, height = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
        try:
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, width, height)
            node = snapshot.to_node()
            if node is None:
                raise ValueError("No current video frame")
            texture = self.window.get_renderer().render_texture(node, None)
            destination = Path(directory) if directory else self.snapshot_directory()
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / f"camera-{camera.channel}-{datetime.now():%Y%m%d-%H%M%S-%f}.png"
            if not texture.save_to_png(str(path)):
                raise OSError("Could not write snapshot")
        except (OSError, ValueError, GLib.Error, TypeError):
            self.toast("Could not save the snapshot.")
            return None
        toast = self.toast("Snapshot saved to Pictures/CCTV")
        toast.set_button_label("Open")
        toast.connect("button-clicked", lambda *_: self.open_uri(path.as_uri()))
        return path

    def open_uri(self, uri):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, self.window.get_display().get_app_launch_context())
        except GLib.Error:
            self.toast("Could not open the file or folder.")

    def open_snapshots(self):
        directory = self.snapshot_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.open_uri(directory.as_uri())
        except OSError:
            self.toast("Could not open the snapshots folder.")

    def about(self):
        dialog = Adw.AboutDialog(application_name="CCTV Viewer", application_icon="io.local.CCTVViewer",
                                  version="0.2.1", comments="A native viewer for your home recorder.",
                                  license_type=Gtk.License.MIT_X11)
        dialog.present(self.window)

    def smoke_result(self):
        results = [camera.check_frame() for camera in self.cameras]
        print(json.dumps({"frames_received": dict(zip(range(1, 5), results))}), flush=True)
        self.exit_code = 0 if all(results) else 1
        self.window.close()
        return False

    def close(self, *_args):
        self.closing = True
        if self.discovery_cancel:
            self.discovery_cancel.set()
        GLib.source_remove(self.frame_timer)
        self.disconnect()
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Show four test patterns without a recorder")
    parser.add_argument("--smoke-test", action="store_true", help="Verify all four streams for 12 seconds, then exit")
    options = parser.parse_args()
    app = Viewer(options)
    app.run([sys.argv[0]])
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
