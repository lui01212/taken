#!/usr/bin/env python3
"""Render the three WinGet manifests for a released taken version.

Usage:
    python packaging/winget/generate.py 0.9.0 path/to/taken_0.9.0_windows-x64.zip

Reads the zip's SHA256 locally (never trust a sidecar), fills the
templates, and writes them to packaging/winget/<version>/ in the layout
microsoft/winget-pkgs expects:

    manifests/r/RogueAlg0/taken/<version>/
        RogueAlg0.taken.yaml
        RogueAlg0.taken.installer.yaml
        RogueAlg0.taken.locale.en-US.yaml

Validate on Windows before submitting:
    winget validate --manifest packaging\\winget\\<version>
Submit with:
    wingetcreate submit --token <github-token> packaging\\winget\\<version>
or open a manual PR against microsoft/winget-pkgs.
"""

import hashlib
import os
import sys
from string import Template

IDENTIFIER = "RogueAlg0.taken"
REPO = "https://github.com/RogueAlg0/taken"

VERSION_TEMPLATE = """PackageIdentifier: $identifier
PackageVersion: $version
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.10.0
"""

INSTALLER_TEMPLATE = """# yaml-language-server: $$schema=https://aka.ms/winget-manifest.installer.1.10.0.schema.json
PackageIdentifier: $identifier
PackageVersion: $version
InstallerType: zip
NestedInstallerType: portable
ReleaseDate: $date
Installers:
- Architecture: x64
  InstallerUrl: $url
  InstallerSha256: $sha256
  NestedInstallerFiles:
  - RelativeFilePath: taken.exe
    PortableCommandAlias: taken
  - RelativeFilePath: taken-mcp.exe
    PortableCommandAlias: taken-mcp
ManifestType: installer
ManifestVersion: 1.10.0
"""

LOCALE_TEMPLATE = """# yaml-language-server: $$schema=https://aka.ms/winget-manifest.defaultLocale.1.10.0.schema.json
PackageIdentifier: $identifier
PackageVersion: $version
PackageLocale: en-US
Publisher: RogueAlg0
PublisherUrl: https://github.com/RogueAlg0
PackageName: taken
PackageUrl: https://roguealg0.github.io/taken/
License: MIT
LicenseUrl: $repo/blob/main/LICENSE
ShortDescription: Check whether a GitHub issue is already taken before you volunteer for it
Description: taken answers 'taken?' for any GitHub issue with GO, TAKEN, or
  CAUTION verdicts drawn from git history, linked PRs, and maintainer activity,
  so you never start work someone else already claimed. Ships a CLI and
  taken-mcp, an MCP server exposing the same checks to AI assistants.
Moniker: taken
Tags:
- github
- cli
- open-source
- developer-tools
ReleaseNotesUrl: $repo/releases/tag/v$version
ManifestType: defaultLocale
ManifestVersion: 1.10.0
"""


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    version, zip_path = sys.argv[1], sys.argv[2]
    if not os.path.isfile(zip_path):
        print(f"zip not found: {zip_path}", file=sys.stderr)
        return 1
    with open(zip_path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest().upper()
    url = f"{REPO}/releases/download/v{version}/taken_{version}_windows-x64.zip"
    date = os.environ.get("RELEASE_DATE", "")
    subs = {
        "identifier": IDENTIFIER,
        "version": version,
        "url": url,
        "sha256": sha256,
        "date": date,
        "repo": REPO,
    }
    outdir = os.path.join("packaging", "winget", version)
    os.makedirs(outdir, exist_ok=True)
    files = {
        f"{IDENTIFIER}.yaml": VERSION_TEMPLATE,
        f"{IDENTIFIER}.installer.yaml": INSTALLER_TEMPLATE,
        f"{IDENTIFIER}.locale.en-US.yaml": LOCALE_TEMPLATE,
    }
    for name, tmpl in files.items():
        with open(os.path.join(outdir, name), "w") as f:
            f.write(Template(tmpl).substitute(subs))
        print("wrote", os.path.join(outdir, name))
    print("SHA256:", sha256)
    return 0


if __name__ == "__main__":
    sys.exit(main())
