#!/usr/bin/env python3
"""Install the launcher and icons for this checkout, without administrator access."""

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.local.CCTVViewer"


def exec_argument(value):
    # Desktop Entry Exec quoting is different from shell quoting.
    value = str(value).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def main():
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    theme = data_home / "icons/hicolor"
    icon_files = [f"scalable/apps/{APP_ID}.svg", f"symbolic/apps/{APP_ID}-symbolic.svg"]
    for relative in icon_files:
        destination = theme / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "data/icons/hicolor" / relative, destination)

    applications = data_home / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    launcher = applications / f"{APP_ID}.desktop"
    launcher.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CCTV Viewer\n"
        "Comment=View cameras from your home recorder\n"
        f"Exec=/usr/bin/python3 {exec_argument(ROOT / 'viewer.py')}\n"
        f"Icon={theme / icon_files[0]}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;GTK;\n"
        "StartupNotify=true\n"
    )
    for command in (["gtk-update-icon-cache", "-f", "-t", str(theme)],
                    ["update-desktop-database", str(applications)]):
        if shutil.which(command[0]):
            subprocess.run(command, check=True)
    print(f"Installed launcher: {launcher}")
    print(f"Source checkout: {ROOT}")


if __name__ == "__main__":
    main()
