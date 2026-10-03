#!/usr/bin/env node
"use strict";
// npm shim for the real `taken` CLI (installed by the postinstall step via pip).
// Finds the binary of the same name on PATH, verifies it really is taken, and
// forwards everything to it: arguments, stdin/stdout/stderr, signals, and the
// exit code.
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const NAME = path.basename(__filename, ".js");
// Real path of this shim, so findOnPath can skip it: when npm links the shim
// itself onto PATH as `taken`, a naive lookup would find the shim first and
// recurse forever.
const SELF_REALPATH = fs.realpathSync(__filename);
// `taken --version` prints e.g. "taken 0.7.4". The same regex install.js uses
// to recognize an installed taken.
const VERSION_RE = /taken (\d+\.\d+\.\d+[^\s]*)/;

// A same-named executable is only trusted when it answers --version with a
// taken version string. PATH order can change after install, so a different
// program named `taken` may shadow the real binary; forwarding to it would
// silently run a stranger program with the user's arguments.
function isTakenBinary(candidate) {
  try {
    const probed = spawnSync(candidate, ["--version"], {
      encoding: "utf8",
      timeout: 5000,
    });
    return (
      probed.status === 0 &&
      !probed.error &&
      VERSION_RE.test(probed.stdout || "")
    );
  } catch {
    return false;
  }
}

function findOnPath(name) {
  const pathEnv = process.env.PATH || "";
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, name);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      if (!fs.statSync(candidate).isFile()) continue;
      // Skip this very shim if npm put it on PATH ahead of the real binary.
      if (fs.realpathSync(candidate) === SELF_REALPATH) continue;
      // Skip imposters: keep scanning until a verified taken binary turns up.
      if (!isTakenBinary(candidate)) continue;
      return candidate;
    } catch {
      // not in this directory, keep looking
    }
  }
  return null;
}

const target = findOnPath(NAME);
if (!target) {
  console.error(
    `${NAME} is not installed.\n` +
      "The npm postinstall step normally installs it with pip. " +
      "To install it by hand, run:\n" +
      "  python3 -m pip install --user taken-gh"
  );
  process.exit(1);
}

const child = spawn(target, process.argv.slice(2), { stdio: "inherit" });

for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(sig, () => child.kill(sig));
}

child.on("error", (err) => {
  console.error(`Could not start ${NAME}: ${err.message}`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    // Re-raise so the caller's exit status reflects the signal.
    process.kill(process.pid, signal);
  } else {
    process.exit(code === null ? 1 : code);
  }
});
