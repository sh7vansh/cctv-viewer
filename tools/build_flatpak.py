#!/usr/bin/env python3
"""Build an installable Flatpak bundle from this checkout."""

from pathlib import Path
import hashlib
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.local.CCTVViewer"


def main():
    if shutil.which("flatpak-builder"):
        builder = ["flatpak-builder"]
    elif subprocess.run(["flatpak", "info", "org.flatpak.Builder"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        builder = ["flatpak", "run", "org.flatpak.Builder"]
    else:
        raise SystemExit("Install flatpak-builder, or the org.flatpak.Builder Flatpak, first.")
    subprocess.run(builder + ["--force-clean", "--user", "--install-deps-from=flathub",
                             "--repo=flatpak-repo", "build-flatpak", f"{APP_ID}.json"], cwd=ROOT, check=True)
    architecture = subprocess.check_output(["flatpak", "--default-arch"], text=True).strip()
    output = ROOT / "dist" / f"cctv-viewer-0.2.0-{architecture}.flatpak"
    output.parent.mkdir(exist_ok=True)
    subprocess.run(["flatpak", "build-bundle", "--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo",
                    "flatpak-repo", str(output), APP_ID, "stable"], cwd=ROOT, check=True)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".flatpak.sha256").write_text(f"{checksum}  {output.name}\n")
    print(f"Bundle: {output}")


if __name__ == "__main__":
    main()
