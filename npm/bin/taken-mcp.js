#!/usr/bin/env node
"use strict";
// npm shim for the real `taken-mcp` server (installed by the postinstall step
// via pip). Finds the binary of the same name on PATH, verifies it really is
// taken's MCP server, and forwards everything to it: arguments,
// stdin/stdout/stderr, signals, and the exit code.
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const NAME = path.basename(__filename, ".js");
// Real path of this shim, so findOnPath can skip it: when npm links the shim
// itself onto PATH as `taken-mcp`, a naive lookup would find the shim first
// and recurse forever.
const SELF_REALPATH = fs.realpathSync(__filename);
// The real `taken-mcp` is pip's console-script stub for taken's MCP entry
// point (`taken.mcp_server:main`). It has no --version flag (it speaks
// JSON-RPC on stdio), so identity is verified by the entry-point marker in
// the stub itself instead of a version probe.
const ENTRY_POINT_RE = /taken\.mcp_server/;

// A same-named executable is only trusted when it is taken's MCP console
// script. PATH order can change after install, so a different program named
// `taken-mcp` may shadow the real binary; forwarding to it would silently run
// a stranger program with the user's arguments.
function isTakenMcpBinary(candidate) {
  try {
    return ENTRY_POINT_RE.test(fs.readFileSync(candidate, "utf8"));
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
      // Skip imposters: keep scanning until a verified binary turns up.
      if (!isTakenMcpBinary(candidate)) continue;
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
