# Flatpak publishing

The public repository is hosted on the `gh-pages` branch:

- Site: https://sh7vansh.github.io/cctv-viewer/
- Installer: https://sh7vansh.github.io/cctv-viewer/cctv-viewer.flatpakref
- Repository definition: https://sh7vansh.github.io/cctv-viewer/cctv-viewer.flatpakrepo
- OSTree repository: https://sh7vansh.github.io/cctv-viewer/repo/

The app ref is `app/io.local.CCTVViewer/x86_64/stable`. App commits and repository
metadata are GPG signed. The installer embeds the public key, so installations
verify future updates without disabling signature checks.

Public signing fingerprint:

```text
4209C4FE6B7E9A8EA0B7CB10B85985F08B06A064
```

Keep the existing private key, its backups, and revocation certificate outside
the source and Pages checkouts. The release key directory on the maintainer's
machine is `~/.local/share/cctv-viewer-release-keys`. Never commit or upload that
directory. The publisher exports only the public key.

## Prepare an update

Preserve the `gh-pages` Git history and the existing signing key. On a fresh
source checkout, clone the published branch into the ignored build directory:

```sh
mkdir -p build
git clone --single-branch --branch gh-pages git@github.com:sh7vansh/cctv-viewer.git build/public
```

If the local `flatpak-repo` does not exist yet, seed it with the published repo
before building, so earlier commits and deltas remain available:

```sh
cp -a build/public/repo flatpak-repo
```

Update the application and package version for a new release, then build and
sign using the same key:

```sh
python3 tools/build_flatpak.py
python3 tools/publish_flatpak.py \
  --signing-key 4209C4FE6B7E9A8EA0B7CB10B85985F08B06A064 \
  --gpg-homedir "$HOME/.local/share/cctv-viewer-release-keys"
python3 tests/verify_flatpak.py
git -C build/public add .
git -C build/public commit -m "Update Flatpak repository"
git -C build/public push origin gh-pages
```

The publisher generates static deltas, signed metadata, `.flatpakref` and
`.flatpakrepo` installers, a public key, and the landing page. It also refreshes
the signed bundle and artifact checksums in `dist/`. It preserves the Pages
checkout's `.git` directory and does not push automatically.
Publishing requires Python bindings for OSTree. The publisher explicitly signs
both AppStream branches, including reused commits, before updating the summary.
It regenerates AppStream deltas so their embedded signatures match the commits.
The verification check serves the local repository over HTTP and refreshes
AppStream using an isolated Flatpak client with
GPG verification enabled; it leaves installed apps and remotes untouched.

GitHub Pages deploys the branch after the push. Verify the public URL before
announcing an update:

```sh
flatpak remote-info --user cctv-viewer io.local.CCTVViewer//stable
python3 tests/verify_flatpak.py --url https://sh7vansh.github.io/cctv-viewer/repo/
```
