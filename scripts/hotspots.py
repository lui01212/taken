#!/usr/bin/env python3
"""Hotspot metrics for this repo: hotspot = complexity x churn.

A hotspot is a file that is BOTH complex and frequently changed: the files
where refactoring effort pays off most. Complexity is cyclomatic complexity
summed per file (via radon); churn is the number of commits touching the file
in git history.

Usage:
    python scripts/hotspots.py           # regenerate badge + print table
    python scripts/hotspots.py --check   # read-only: print the table, exit 1
                                         # if the badge file would change

Regenerates docs/badges/hotspot.json (shields.io endpoint schema) and prints
a ranked table to stdout. Needs `radon` installed and a full git history.
Output is deterministic: sorted, no timestamps. Use --check for exploratory
runs so looking at the table does not dirty the git tree.

A file counts as a hotspot needing attention when it meets both thresholds
below. Thresholds are documented here rather than tuned to look good; change
them with a normal PR if they stop being useful.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = "taken"
BADGE_PATH = REPO_ROOT / "docs" / "badges" / "hotspot.json"

# A file needs attention when it is both this complex and this churned.
MIN_CC = 50
MIN_COMMITS = 5


def run(*args: str) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT)
    if proc.returncode != 0:
        print(f"hotspots: command failed: {' '.join(args)}\n{proc.stderr}", file=sys.stderr)
        sys.exit(2)
    return proc.stdout


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank hotspot files (complexity x churn) and print the table. "
        "Without flags, regenerates docs/badges/hotspot.json.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only mode: print the table but never write the badge file; "
        "exit 1 when the badge file would change, 0 when it is up to date",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import radon  # noqa: F401  (imported for its CLI; version-agnostic)
    except ImportError:
        print("hotspots: 'radon' is not installed (pip install radon)", file=sys.stderr)
        return 2
    del radon

    # List the package directory and filter in Python: recursive by
    # construction, so a future subpackage cannot silently drop out of the
    # metric the way a non-recursive "taken/*.py" glob would allow.
    files = [f for f in run("git", "ls-files", PACKAGE_DIR).split() if f.endswith(".py")]
    if not files:
        print("hotspots: no tracked files found", file=sys.stderr)
        return 2

    cc_raw = json.loads(run("radon", "cc", "-s", "-j", *files))
    # Radon omits files with no complexity blocks from its output, so a
    # missing key legitimately means cc=0. But any key we did not ask about
    # means radon's path reporting drifted (absolute paths, normalization),
    # which would silently zero every file's complexity: fail loud instead.
    unexpected = set(cc_raw) - set(files)
    if unexpected:
        print(
            f"hotspots: radon returned paths not in the file list: {sorted(unexpected)}",
            file=sys.stderr,
        )
        return 2

    rows = []
    for path in sorted(files):
        cc_sum = sum(block["complexity"] for block in cc_raw.get(path, []))
        # Default history simplification excludes merge commits from the
        # count. That is intentional: each PR's commits are already counted
        # through the merge's parents, so counting the merge itself would
        # double-count the same changes. Blind spot: edits made only inside a
        # merge commit (conflict resolution) are invisible to this metric.
        commits = len(run("git", "log", "--follow", "--format=%H", "--", path).split())
        rows.append(
            {
                "file": path,
                "cc": cc_sum,
                "commits": commits,
                "hotspot": cc_sum * commits,
            }
        )
    rows.sort(key=lambda r: r["hotspot"], reverse=True)

    flagged = [r for r in rows if r["cc"] >= MIN_CC and r["commits"] >= MIN_COMMITS]
    if not flagged:
        message, color = "all clear", "brightgreen"
    elif len(flagged) <= 2:
        message, color = f"{len(flagged)} need attention", "yellow"
    else:
        message, color = f"{len(flagged)} need attention", "orange"

    badge = {
        "schemaVersion": 1,
        "label": "hotspots",
        "message": message,
        "color": color,
        "files": [
            {"file": r["file"], "cc": r["cc"], "commits": r["commits"], "hotspot": r["hotspot"]}
            for r in flagged
        ],
    }
    rendered = json.dumps(badge, indent=2) + "\n"

    print(f"{'file':<28} {'cc':>6} {'commits':>8} {'hotspot':>9}  attention")
    for r in rows:
        mark = " <--" if r in flagged else ""
        print(f"{r['file']:<28} {r['cc']:>6} {r['commits']:>8} {r['hotspot']:>9}{mark}")

    try:
        badge_rel = BADGE_PATH.relative_to(REPO_ROOT)
    except ValueError:
        badge_rel = BADGE_PATH
    if args.check:
        current = BADGE_PATH.read_text() if BADGE_PATH.exists() else None
        if current != rendered:
            print(f"\ncheck: {badge_rel} would change (not written)")
            return 1
        print(f"\ncheck: {badge_rel} is up to date")
        return 0

    BADGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BADGE_PATH.write_text(rendered)
    print(f"\nwrote {badge_rel}: {message} ({color})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
