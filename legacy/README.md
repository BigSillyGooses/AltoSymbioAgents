# legacy/

This directory preserves the pre-Pinokio bootstrap pipeline for one release
in case a fast revert is needed. Nothing here is built, packaged, or
referenced by any active script — the runtime install path is the
Miniconda + sidecar-venv flow in `desktop-shell/bootstrap/` and the build
path is the collapsed `dev/build-installer.bat`.

Contents:

- `backend/pyinstaller.spec` — onedir spec that previously produced
  `backend/dist/server/server.exe`. Mirrored into `branding/sidecar-bundle/`
  by `build-sidecar.cjs`.
- `build-scripts/build-sidecar.bat` / `.sh` — platform launchers that ran
  `python -m PyInstaller pyinstaller.spec --noconfirm --clean`.
- `build-scripts/build-sidecar.cjs` — node entry that delegated to the
  platform script, then `cpSync`'d the output into
  `branding/sidecar-bundle/` for electron-builder's `extraResources`.

These files will be deleted in **v1.0.1** after the v1.0.0-test.2 → v1.0.0
end-to-end VM gate has confirmed the new pipeline works on a clean
Windows installation. Until then, restoring the old flow takes three
steps:

1. `git mv legacy/backend/pyinstaller.spec backend/pyinstaller.spec`
2. `git mv legacy/build-scripts/build-sidecar.{bat,sh,cjs} build-scripts/`
3. Re-add the `branding/sidecar-bundle → backend` entry to
   `electron-builder.yml` and the venv + `build:sidecar` blocks to
   `.github/workflows/release.yml`.

Tracked at the v1.0.1 milestone (delete this directory).
