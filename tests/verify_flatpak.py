"""Refresh AppStream from a signed repository using an isolated Flatpak client."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=(ROOT / "build/public/repo").as_uri())
    parser.add_argument("--key", type=Path, default=ROOT / "build/public/signing-key.gpg")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cctv-flatpak-check-") as directory:
        environment = {**os.environ, "FLATPAK_USER_DIR": f"{directory}/flatpak",
                       "XDG_DATA_HOME": f"{directory}/data", "XDG_CACHE_HOME": f"{directory}/cache"}
        subprocess.run(["flatpak", "remote-add", "--user", f"--gpg-import={options.key}",
                        "cctv-check", options.url], env=environment, check=True)
        subprocess.run(["flatpak", "update", "--user", "--appstream", "cctv-check"],
                       env=environment, check=True)
    print("PASS: AppStream refresh succeeds with GPG verification enabled")


if __name__ == "__main__":
    main()
