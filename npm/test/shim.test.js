"use strict";
// Tests for the bin shims: they must forward args, stdin, and exit codes to
// the real binary found on PATH, verify the binary really is taken before
// forwarding, and fail helpfully when it is absent.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SHIMS = {
  taken: path.join(__dirname, "..", "bin", "taken.js"),
  "taken-mcp": path.join(__dirname, "..", "bin", "taken-mcp.js"),
};

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "taken-shim-"));
}

function writeExecutable(dir, name, body) {
  const p = path.join(dir, name);
  fs.writeFileSync(p, body);
  fs.chmodSync(p, 0o755);
  return p;
}

// A fake that passes the shim's identity check and then behaves like the
// forwarding tests expect. The `taken` fake answers --version the way the
// real CLI does; the `taken-mcp` fake carries the console-script entry-point
// marker the real pip stub has.
function realFakeBody(name) {
  if (name === "taken-mcp") {
    return (
      "#!/bin/sh\n" +
      "# console-script stub for taken.mcp_server:main\n" +
      'echo "ARGS:$*"\n' +
      "cat\n" +
      "exit 0\n"
    );
  }
  return (
    "#!/bin/sh\n" +
    'if [ "$1" = "--version" ]; then echo "taken 0.0.0-test"; exit 0; fi\n' +
    'echo "ARGS:$*"\n' +
    "cat\n" +
    "exit 0\n"
  );
}

function exitFakeBody(name, code) {
  if (name === "taken-mcp") {
    return (
      "#!/bin/sh\n" + "# console-script stub for taken.mcp_server:main\n" + `exit ${code}\n`
    );
  }
  return (
    "#!/bin/sh\n" +
    'if [ "$1" = "--version" ]; then echo "taken 0.0.0-test"; exit 0; fi\n' +
    `exit ${code}\n`
  );
}

// Same name on PATH, but nothing proving it is taken: the --version probe
// gets "not taken" (or a failure) and the entry-point marker is absent.
function imposterBody() {
  return '#!/bin/sh\necho "not taken"\nexit 0\n';
}

function failingVersionBody() {
  return "#!/bin/sh\nexit 1\n";
}

function runShim(shimPath, binDir, args, input) {
  return spawnSync(process.execPath, [shimPath, ...args], {
    input,
    encoding: "utf8",
    // Prepend: the fake binary wins, but system tools (cat, sh) still resolve.
    env: { ...process.env, PATH: binDir + path.delimiter + (process.env.PATH || "") },
  });
}

for (const [name, shimPath] of Object.entries(SHIMS)) {
  describe(`shim ${name}`, () => {
    it("forwards args and stdin and keeps a zero exit code", () => {
      const dir = makeTempDir();
      writeExecutable(dir, name, realFakeBody(name));
      const r = runShim(shimPath, dir, ["check", "owner/repo#1"], "hello\n");
      assert.equal(r.status, 0);
      assert.match(r.stdout, /ARGS:check owner\/repo#1/);
      assert.match(r.stdout, /hello/);
    });

    it("propagates a non-zero exit code", () => {
      const dir = makeTempDir();
      writeExecutable(dir, name, exitFakeBody(name, 42));
      const r = runShim(shimPath, dir, []);
      assert.equal(r.status, 42);
    });

    it("exits 1 with install guidance when the binary is absent", () => {
      const dir = makeTempDir(); // empty: no binary on PATH
      const r = runShim(shimPath, dir, []);
      assert.equal(r.status, 1);
      assert.match(r.stderr, new RegExp(`${name} is not installed`));
      assert.match(r.stderr, /python3 -m pip install --user taken-gh/);
    });

    it("skips imposters earlier on PATH and forwards to the verified binary", () => {
      // An imposter whose --version output is wrong, one whose --version
      // fails, and then the real binary: the shim must skip both imposters.
      const imposterDir1 = makeTempDir();
      writeExecutable(imposterDir1, name, imposterBody());
      const imposterDir2 = makeTempDir();
      writeExecutable(imposterDir2, name, failingVersionBody());
      const realDir = makeTempDir();
      writeExecutable(realDir, name, realFakeBody(name));
      const r = spawnSync(process.execPath, [shimPath, "check", "x"], {
        encoding: "utf8",
        timeout: 15000,
        env: {
          ...process.env,
          PATH:
            imposterDir1 +
            path.delimiter +
            imposterDir2 +
            path.delimiter +
            realDir +
            path.delimiter +
            (process.env.PATH || ""),
        },
      });
      assert.equal(r.status, 0);
      assert.match(r.stdout, /ARGS:check x/);
      assert.doesNotMatch(r.stdout, /not taken/);
    });

    it("exits 1 with install guidance when only an imposter is on PATH", () => {
      const imposterDir = makeTempDir();
      writeExecutable(imposterDir, name, imposterBody());
      const r = spawnSync(process.execPath, [shimPath], {
        encoding: "utf8",
        timeout: 15000,
        env: {
          ...process.env,
          PATH: imposterDir + path.delimiter + "/nonexistent-taken-test",
        },
      });
      assert.equal(r.status, 1);
      assert.match(r.stderr, new RegExp(`${name} is not installed`));
      assert.match(r.stderr, /python3 -m pip install --user taken-gh/);
    });

    it("skips itself when npm links the shim onto PATH", () => {
      // Real-world layout: npm puts a `taken` symlink to this very shim on
      // PATH. The shim must not find itself and recurse; it must find the
      // real binary in the later directory.
      const selfDir = makeTempDir();
      fs.symlinkSync(shimPath, path.join(selfDir, name));
      const realDir = makeTempDir();
      writeExecutable(realDir, name, realFakeBody(name));
      const r = spawnSync(process.execPath, [shimPath, "check", "x"], {
        encoding: "utf8",
        timeout: 15000,
        env: {
          ...process.env,
          PATH:
            selfDir +
            path.delimiter +
            realDir +
            path.delimiter +
            (process.env.PATH || ""),
        },
      });
      assert.equal(r.status, 0);
      assert.match(r.stdout, /ARGS:check x/);
    });

    it("does not recurse when only itself is on PATH", () => {
      const selfDir = makeTempDir();
      fs.symlinkSync(shimPath, path.join(selfDir, name));
      const r = spawnSync(process.execPath, [shimPath], {
        encoding: "utf8",
        timeout: 15000,
        env: {
          ...process.env,
          PATH: selfDir + path.delimiter + "/nonexistent-taken-test",
        },
      });
      assert.equal(r.status, 1);
      assert.match(r.stderr, new RegExp(`${name} is not installed`));
    });
  });
}
