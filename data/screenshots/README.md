# Screenshots

These screenshots show the real GTK4/libadwaita application using GStreamer test
patterns. The names and `demo.local` address are demonstration values. No private
camera footage, recorder configuration, or credentials are used.

- `overview.png`: four-camera grid and sidebar in light appearance.
- `focused.png`: individual camera view in dark appearance.
- `preferences.png`: video quality, appearance, and camera naming preferences.

Regenerate them from a graphical desktop session:

```sh
python3 tools/capture_screenshots.py
```

The capture tool uses temporary settings and does not access saved logins.
Screenshots are licensed under the project's MIT license.
