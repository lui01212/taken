"use strict";
// Tests for install.js: skip pip when taken already works, pip-install when
// it does not, and never fail the npm install no matter what goes wrong.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const INSTALL = path.join(__dirname, "..", "install.js");
const PKG = require("../package.json");

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "taken-install-"));
}

function writeExecutable(dir, name, body) {
  const p = path.join(dir, name);
  fs.writeFileSync(p, body);
  fs.chmodSync(p, 0o755);
  return p;
}

function runInstall(env) {
  return spawnSync(process.execPath, [INSTALL], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

describe("install.js", () => {
  it("skips pip when `taken --version` reports the matching version", () => {
    const dir = makeTempDir();
    writeExecutable(
      dir,
      "taken",
      `#!/bin/sh\nif [ "$1" = "--version" ]; then echo "taken ${PKG.version}"; exit 0; fi\nexit 1\n`
    );
    // A python3 that must never be called: it would fail loudly if invoked.
    writeExecutable(dir, "python3", '#!/bin/sh\necho "pip should not have run" >&2\nexit 99\n');
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout, /already installed, skipping/);
    assert.doesNotMatch(r.stdout + r.stderr, /pip should not have run/);
  });

  it("skips pip when the installed taken is newer than the package", () => {
    const dir = makeTempDir();
    const [maj, min, pat] = PKG.version.split(".").map(Number);
    const newer = `${maj}.${min}.${pat + 1}`;
    writeExecutable(
      dir,
      "taken",
      `#!/bin/sh\nif [ "$1" = "--version" ]; then echo "taken ${newer}"; exit 0; fi\nexit 1\n`
    );
    // A python3 that must never be called: it would fail loudly if invoked.
    writeExecutable(dir, "python3", '#!/bin/sh\necho "pip should not have run" >&2\nexit 99\n');
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(
      r.stdout,
      new RegExp(`taken ${newer} is already installed, newer than this package's`)
    );
    assert.match(r.stdout, /skipping the pip install/);
    assert.doesNotMatch(r.stdout + r.stderr, /pip should not have run/);
  });

  it("treats a prerelease suffix as older than the bare release", () => {
    const dir = makeTempDir();
    // taken reports "0.8.0a1" style output while the package is the bare
    // release: the installed copy is older, so pip should run.
    const marker = path.join(dir, "pip-args.txt");
    writeExecutable(
      dir,
      "taken",
      `#!/bin/sh\nif [ "$1" = "--version" ]; then echo "taken ${PKG.version}a1"; exit 0; fi\nexit 1\n`
    );
    writeExecutable(
      dir,
      "python3",
      `#!/bin/sh\necho "$*" > "${marker}"\nexit 0\n`
    );
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    const args = fs.readFileSync(marker, "utf8").trim();
    assert.equal(args, `-m pip install --user taken-gh==${PKG.version}`);
  });

  it("runs pip when the installed taken version mismatches the package", () => {
    const dir = makeTempDir();
    writeExecutable(
      dir,
      "taken",
      '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "taken 0.0.1"; exit 0; fi\nexit 1\n'
    );
    const marker = path.join(dir, "pip-args.txt");
    writeExecutable(
      dir,
      "python3",
      `#!/bin/sh\necho "$*" > "${marker}"\nexit 0\n`
    );
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout, /reinstalling with pip/);
    const args = fs.readFileSync(marker, "utf8").trim();
    assert.equal(args, `-m pip install --user taken-gh==${PKG.version}`);
  });

  it("runs pip when `taken --version` output is unparseable", () => {
    const dir = makeTempDir();
    writeExecutable(
      dir,
      "taken",
      '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "hello world"; exit 0; fi\nexit 1\n'
    );
    const marker = path.join(dir, "pip-args.txt");
    writeExecutable(
      dir,
      "python3",
      `#!/bin/sh\necho "$*" > "${marker}"\nexit 0\n`
    );
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    const args = fs.readFileSync(marker, "utf8").trim();
    assert.equal(args, `-m pip install --user taken-gh==${PKG.version}`);
  });

  it("retries with --break-system-packages on externally-managed-environment", () => {
    const dir = makeTempDir();
    const marker = path.join(dir, "pip-args.txt");
    // no taken on PATH here; python3 fails the first pip attempt with the
    // PEP 668 error and succeeds once --break-system-packages is passed.
    writeExecutable(
      dir,
      "python3",
      `#!/bin/sh\necho "$*" >> "${marker}"\ncase "$*" in\n  *break-system-packages*) exit 0 ;;\n  *) echo "error: externally-managed-environment" >&2; exit 1 ;;\nesac\n`
    );
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout, /externally-managed-environment/);
    assert.match(r.stdout, /--break-system-packages/);
    const lines = fs.readFileSync(marker, "utf8").trim().split("\n");
    assert.equal(lines.length, 2);
    assert.equal(lines[0], `-m pip install --user taken-gh==${PKG.version}`);
    assert.equal(
      lines[1],
      `-m pip install --user --break-system-packages taken-gh==${PKG.version}`
    );
  });

  it("pip-installs the pinned version when taken is missing", () => {
    const dir = makeTempDir();
    const marker = path.join(dir, "pip-args.txt");
    writeExecutable(
      dir,
      "python3",
      `#!/bin/sh\necho "$*" > "${marker}"\nexit 0\n`
    );
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    const args = fs.readFileSync(marker, "utf8").trim();
    assert.equal(args, `-m pip install --user taken-gh==${PKG.version}`);
  });

  it("exits 0 with guidance when python3 is missing entirely", () => {
    const dir = makeTempDir(); // empty: no taken, no python3
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout + r.stderr, /python3 was not found/);
    assert.match(r.stdout + r.stderr, new RegExp(`taken-gh==${PKG.version}`));
  });

  it("exits 0 with guidance when pip itself fails", () => {
    const dir = makeTempDir();
    writeExecutable(dir, "python3", "#!/bin/sh\nexit 1\n");
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout + r.stderr, /did not finish cleanly/);
  });
});
