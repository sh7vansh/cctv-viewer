#!/usr/bin/env python3
"""Prepare a signed Flatpak repository and GitHub Pages installer files."""

import argparse
import base64
import hashlib
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.local.CCTVViewer"
HOMEPAGE = "https://sh7vansh.github.io/cctv-viewer/"


def run(*arguments):
    subprocess.run([str(argument) for argument in arguments], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signing-key", required=True, help="Existing release signing-key fingerprint")
    parser.add_argument("--gpg-homedir", type=Path, required=True, help="Private key directory outside the checkout")
    parser.add_argument("--output", type=Path, default=ROOT / "build/public", help="Pages checkout to populate")
    options = parser.parse_args()
    key_home = options.gpg_homedir.expanduser().resolve()
    if key_home == ROOT or ROOT in key_home.parents:
        parser.error("Keep private signing keys outside the source checkout")
    repository = ROOT / "flatpak-repo"
    if not (repository / "config").is_file():
        parser.error("Build the Flatpak first with tools/build_flatpak.py")
    public = options.output.resolve()
    if public == ROOT or public == repository or public in repository.parents or repository in public.parents:
        parser.error("The Pages output must be separate from the source and build repository")
    if public == key_home or public in key_home.parents or key_home in public.parents:
        parser.error("Keep Pages output separate from private signing keys")
    public.mkdir(parents=True, exist_ok=True)
    key = subprocess.check_output(["gpg", "--homedir", str(key_home), "--export", options.signing_key])
    if not key:
        parser.error("No public key matched the signing fingerprint")
    (public / "signing-key.gpg").write_bytes(key)
    signing = [f"--gpg-sign={options.signing_key}", f"--gpg-homedir={key_home}"]
    run("flatpak", "build-sign", *signing, repository, APP_ID, "stable")
    run("flatpak", "build-update-repo", *signing, f"--gpg-import={public / 'signing-key.gpg'}",
        "--title=CCTV Viewer", "--default-branch=stable", f"--homepage={HOMEPAGE}",
        "--generate-static-deltas", repository)
    shutil.copytree(repository, public / "repo", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("tmp", "lock", ".lock"))
    common = (f"Title=CCTV Viewer\nComment=Live video from your home recorder\n"
              f"Description=A GTK4 and libadwaita viewer for CP PLUS recorders.\n"
              f"Url={HOMEPAGE}repo/\nHomepage={HOMEPAGE}\nIcon={HOMEPAGE}icon.svg\n"
              f"GPGKey={base64.b64encode(key).decode('ascii')}\n")
    (public / "cctv-viewer.flatpakrepo").write_text("[Flatpak Repo]\n" + common)
    (public / "cctv-viewer.flatpakref").write_text(
        f"[Flatpak Ref]\nName={APP_ID}\nBranch=stable\nIsRuntime=false\n"
        "SuggestRemoteName=cctv-viewer\n"
        "RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo\n" + common)
    shutil.copy2(ROOT / f"data/icons/hicolor/scalable/apps/{APP_ID}.svg", public / "icon.svg")
    shutil.copy2(ROOT / "packaging/site/index.html", public / "index.html")
    (public / ".nojekyll").touch()
    architecture = subprocess.check_output(["flatpak", "--default-arch"], text=True).strip()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    bundle = dist / f"cctv-viewer-0.2.0-{architecture}.flatpak"
    run("flatpak", "build-bundle", f"--repo-url={HOMEPAGE}repo/",
        f"--gpg-keys={public / 'signing-key.gpg'}",
        "--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo",
        repository, bundle, APP_ID, "stable")
    for name in ("cctv-viewer.flatpakref", "cctv-viewer.flatpakrepo", "signing-key.gpg"):
        shutil.copy2(public / name, dist / name)
    for artifact in (bundle, dist / "cctv-viewer.flatpakref", dist / "cctv-viewer.flatpakrepo", dist / "signing-key.gpg"):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_name(artifact.name + ".sha256").write_text(f"{digest}  {artifact.name}\n")
    print(f"Signed Pages content prepared in {public}")
    print("Review, commit, and push this directory to the gh-pages branch.")


if __name__ == "__main__":
    main()
