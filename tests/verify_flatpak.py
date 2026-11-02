"""Refresh AppStream from a signed repository using an isolated Flatpak client."""

import argparse
from contextlib import ExitStack
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Hosted repository URL; defaults to serving the local publish directory over HTTP")
    parser.add_argument("--key", type=Path, default=ROOT / "build/public/signing-key.gpg")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cctv-flatpak-check-") as directory, ExitStack() as resources:
        url = options.url
        if url is None:
            handler = partial(RepositoryHandler, directory=str(ROOT / "build/public/repo"))
            server = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), handler))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            resources.callback(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}/"
        environment = {**os.environ, "FLATPAK_USER_DIR": f"{directory}/flatpak",
                       "XDG_DATA_HOME": f"{directory}/data", "XDG_CACHE_HOME": f"{directory}/cache"}
        subprocess.run(["flatpak", "remote-add", "--user", f"--gpg-import={options.key}",
                        "cctv-check", url], env=environment, check=True)
        subprocess.run(["flatpak", "update", "--user", "--appstream", "cctv-check"],
                       env=environment, check=True)
    print("PASS: AppStream refresh succeeds with GPG verification enabled")


if __name__ == "__main__":
    main()
