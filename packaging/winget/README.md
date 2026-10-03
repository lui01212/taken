# WinGet distribution

`winget install RogueAlg0.taken` works through a manifest in
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs), which this
project does not own. Nothing here submits anything automatically: publishing
to WinGet means opening a pull request against that repository, and that step
stays manual.

## How it works

1. On every `v*` tag, the `build-windows` job in `.github/workflows/publish.yml`
   builds one-file `taken.exe` and `taken-mcp.exe` with PyInstaller on
   `windows-latest`, zips them as `taken_<ver>_windows-x64.zip`, and attaches
   the zip to the tag's GitHub Release next to the `.deb`.
2. After the release, render the three manifests for the new version:
   `python packaging/winget/generate.py <version> taken_<version>_windows-x64.zip`
   (download the zip from the release first). The script hashes the zip
   locally and writes `packaging/winget/<version>/` in the layout
   winget-pkgs expects.
3. On Windows, validate: `winget validate --manifest packaging\winget\<version>`
   (`winget validate` needs Windows; it cannot run on Linux or macOS).
4. Submit: `wingetcreate submit --token <github-token> packaging\winget\<version>`,
   or copy the directory to `manifests/r/RogueAlg0/taken/<version>/` in a fork
   of winget-pkgs and open a PR by hand.

## Manifest notes

- `InstallerType: zip` with `NestedInstallerType: portable`: WinGet extracts
  the zip per-user under `%LOCALAPPDATA%` and shims `taken` and `taken-mcp`
  onto PATH. No admin rights needed, no `Scope` key (portable does not
  support it).
- Every version needs its own `InstallerUrl` and `InstallerSha256` (uppercase);
  the hash is of the zip, computed locally by the generator.
- The first submission is manually reviewed by Microsoft (typically 1-3 days);
  later version bumps go through faster.

## Gotchas

- PyInstaller one-file executables sometimes trip Windows Defender heuristics
  on first release. If SmartScreen or Defender flags the exe, the fix is a
  code-signing certificate, which is out of scope for now.
- The exe is unsigned; `winget install` still works, it just shows the
  standard unknown-publisher prompt on first run outside WinGet.
- Keep the zip layout flat (`taken.exe` at the root): the manifest's
  `RelativeFilePath` entries point at it.
