"use strict";
// Postinstall for taken-gh: make sure the real `taken` Python CLI is
// available, since the bin shims forward to it. Never fails the npm install:
// every error path prints guidance and exits 0.
const { spawnSync } = require("node:child_process");
const pkg = require("./package.json");
const VERSION = pkg.version;

function manualInstallHint() {
  return (
    "You can install it by hand with:\n" +
    `  python3 -m pip install --user "taken-gh==${VERSION}"`
  );
}

// `taken --version` prints e.g. "taken 0.7.4". Returns the version string, or
// null when taken is missing or its output cannot be parsed.
function installedVersion() {
  const existing = spawnSync("taken", ["--version"], { encoding: "utf8" });
  if (existing.status !== 0 || existing.error) return null;
  const m = /taken (\d+\.\d+\.\d+[^\s]*)/.exec(existing.stdout || "");
  return m ? m[1] : null;
}

function pipInstall(extraArgs) {
  const args = [
    "-m",
    "pip",
    "install",
    "--user",
    ...extraArgs,
    `taken-gh==${VERSION}`,
  ];
  // stdout stays inherited so pip's progress still shows; stderr is captured
  // so we can spot PEP 668's externally-managed-environment and retry.
  return spawnSync("python3", args, {
    encoding: "utf8",
    stdio: ["inherit", "inherit", "pipe"],
  });
}

// Compare two version strings of the form "1.2.3" with an optional suffix
// (e.g. "1.2.3a1"). Returns -1, 0, or 1, or null when either side cannot be
// parsed. A bare release sorts above the same numbers with a suffix.
function compareVersions(a, b) {
  const parts = (v) => {
    const m = /^(\d+)\.(\d+)\.(\d+)(.*)$/.exec(v);
    return m ? [Number(m[1]), Number(m[2]), Number(m[3]), m[4]] : null;
  };
  const pa = parts(a);
  const pb = parts(b);
  if (!pa || !pb) return null;
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
  }
  if (pa[3] === pb[3]) return 0;
  if (pa[3] === "") return 1;
  if (pb[3] === "") return -1;
  return pa[3] < pb[3] ? -1 : 1;
}

function main() {
  try {
    const have = installedVersion();
    if (have === null) {
      console.log(`Installing taken-gh ${VERSION} with pip...`);
    } else {
      const cmp = compareVersions(have, VERSION);
      if (cmp !== null && cmp > 0) {
        console.log(
          `taken ${have} is already installed, newer than this package's ${VERSION}; skipping the pip install.`
        );
        return;
      }
      if (cmp === 0) {
        console.log(
          `taken ${VERSION} is already installed, skipping the pip install.`
        );
        return;
      }
      console.log(
        `Found taken ${have}, but this package needs ${VERSION}; reinstalling with pip...`
      );
    }

    let result = pipInstall([]);
    if (result.error && result.error.code === "ENOENT") {
      console.error(
        "python3 was not found on PATH, so the pip install was skipped.\n" +
          manualInstallHint()
      );
      return;
    }

    if (
      result.status !== 0 &&
      /externally-managed-environment/.test(result.stderr || "")
    ) {
      console.log(
        "pip refused the user install (externally-managed-environment); " +
          "retrying once with --break-system-packages..."
      );
      result = pipInstall(["--break-system-packages"]);
    }

    if (result.status !== 0) {
      console.error(
        "The pip install did not finish cleanly, so the taken command may not work yet.\n" +
          manualInstallHint()
      );
      return;
    }

    console.log("taken installed. Run `taken --help` to get started.");
  } catch (err) {
    console.error(`Postinstall hit a snag and skipped the pip install: ${err.message}`);
    console.error(manualInstallHint());
  }
  process.exit(0);
}

main();
