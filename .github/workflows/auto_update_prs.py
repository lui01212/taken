"""Keep open PR branches up to date with main.

Runs on every push to main. For each open, non-draft PR whose branch is
behind main but merges cleanly, merges main into the PR branch via the
update-branch API. PRs with real content conflicts are left alone for
their author.

Every open PR gets exactly one disposition line in the log (updated,
or skipped with a reason), so the log answers "why was this branch
not updated" without code archaeology.

Every failure mode is a skip, never an error: a PR that cannot be updated
(maintainer edits disabled on the fork, the author pushed concurrently,
the fork was deleted, mergeability still unknown) is simply retried on
the next push to main. Skips are always logged and summarized at the end,
so no failure passes silently.
"""

import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
PER_PAGE = 100
# GitHub computes mergeability asynchronously; right after a push to main
# open PRs report "unknown" for a short while.
MERGEABILITY_RETRIES = 6
MERGEABILITY_SLEEP = 10


def _repo():
    # Read lazily so importing this module has no side effects and needs
    # no environment (the test suite imports it directly).
    return os.environ["GITHUB_REPOSITORY"]


def _token():
    return os.environ["GH_TOKEN"]


def api(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            detail = json.loads(raw).get("message", raw)
        except ValueError:
            detail = raw
        return exc.code, {"error": detail}


def list_open_prs():
    """Yield every open PR, walking pages of PER_PAGE until a short page."""
    page = 1
    while True:
        status, prs = api(
            "GET", f"/repos/{_repo()}/pulls?state=open&per_page={PER_PAGE}&page={page}"
        )
        if status != 200:
            raise SystemExit(f"could not list PRs: {status} {prs}")
        yield from prs
        if len(prs) < PER_PAGE:
            break
        page += 1


def behind_prs():
    """Return (behind, skipped).

    behind is a list of (number, head_sha) for open, non-draft PRs behind
    main. skipped is a list of (number, reason) for every other open PR;
    every skip is logged as it happens and summarized by main().
    """
    behind, skipped = [], []
    for pr in list_open_prs():
        number = pr["number"]
        if pr.get("draft"):
            reason = "draft"
            print(f"PR #{number}: skipped ({reason})")
            skipped.append((number, reason))
            continue
        state, head_sha = "unknown", None
        for _ in range(MERGEABILITY_RETRIES):
            status, full = api("GET", f"/repos/{_repo()}/pulls/{number}")
            if status != 200:
                reason = f"unreadable ({status})"
                print(f"PR #{number}: skipped ({reason})")
                skipped.append((number, reason))
                break
            state = full.get("mergeable_state")
            head_sha = full["head"]["sha"]
            if state != "unknown":
                break
            time.sleep(MERGEABILITY_SLEEP)
        else:
            reason = "mergeability still unknown"
            print(f"PR #{number}: skipped ({reason})")
            skipped.append((number, reason))
            continue
        if state == "behind":
            behind.append((number, head_sha))
        else:
            # "clean" is up to date; "dirty"/"blocked" stay with the author.
            reason = f"mergeable_state={state}"
            print(f"PR #{number}: skipped ({reason})")
            skipped.append((number, reason))
    return behind, skipped


def summarize(updated, skipped):
    """Log the final tally; returns the summary line for tests."""
    parts = [f"done: {updated} PR branch(es) updated"]
    if skipped:
        parts.append("skipped: " + ", ".join(f"#{n} ({r})" for n, r in skipped))
    summary = "; ".join(parts)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write(f"### Auto-update PR branches\n\n{summary}\n")
    return summary


def main():
    behind, skipped = behind_prs()
    updated = 0
    for number, head_sha in behind:
        # expected_head_sha makes a concurrent author push fail the call
        # instead of racing it; that PR is retried on the next push.
        status, resp = api(
            "PUT",
            f"/repos/{_repo()}/pulls/{number}/update-branch",
            {"expected_head_sha": head_sha},
        )
        if status in (200, 202):
            print(f"PR #{number}: branch updated to main")
            updated += 1
        else:
            reason = f"{status}: {(resp or {}).get('error')}"
            print(f"PR #{number}: skipped ({reason})")
            skipped.append((number, reason))
    summarize(updated, skipped)


if __name__ == "__main__":
    main()
