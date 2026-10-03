"""Web (Pyodide) driver for taken's real check pipeline.

This runs taken's ACTUAL checks.py / verdict.py in the visitor's browser.
The only thing swapped out is the transport: the real CLI shells out to
the `gh` binary, which cannot exist in a browser, so gh_api() is replaced
with a synchronous XMLHttpRequest against api.github.com. Every check,
every heuristic, every verdict rule is the real code.

Notes for maintainers:
- checks.py / verdict.py / budget.py in this directory are byte-copies of
  main at the time of the last refresh (see git log for
  docs/console/py/checks.py). Re-copy them when the pipeline changes;
  verify with diff.
- MAX_SCAN_PAGES is capped at 1 here to respect GitHub's unauthenticated
  budget (60 req/hour per visitor). The CLI scans deeper.
- Live discover mirrors taken/discover.py but sequential and budget-capped:
  2 labels x 1 search page each, verify up to 3 candidates (~9 requests
  each). Scoring logic is the same (maintainer replied +3, updated in
  last 7 days +2, repo pushed in last 7 days +1).
- The file cache is disabled; there is no persistent disk in the page.
- checks.py is a byte-copy of taken/checks.py, so it does
  `from taken import budget` and `from taken.verdict import ...`. There is no
  taken package in the browser; this module synthesizes one (below) whose
  path is this directory, so taken.budget and taken.verdict resolve to the
  vendored budget.py / verdict.py.
"""

import importlib
import json
import os
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# checks.py is a byte-copy of taken/checks.py, so it does
# `from taken import budget` and `from taken.verdict import ...`, but there is
# no taken package in the Pyodide filesystem (only these flat vendored files).
# Synthesize a taken package pointing at this directory before loading checks,
# so taken.budget / taken.verdict resolve to the vendored budget.py /
# verdict.py. importlib is used instead of plain imports because the loading
# must happen after this block (ruff E402).
_taken_pkg = types.ModuleType("taken")
_taken_pkg.__path__ = [os.path.dirname(os.path.abspath(__file__))]
sys.modules["taken"] = _taken_pkg

checks = importlib.import_module("checks")
_verdict = importlib.import_module("taken.verdict")
GO = _verdict.GO
decide = _verdict.decide

URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
SHORT_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")
PATH_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
REPO_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)$")

# Web console default: 5 issues per repo scan (each issue costs ~8 API
# requests; visitors get 60/hour unauthenticated). --limit can raise it
# to 10.
WEB_SCAN_DEFAULT = 5
WEB_SCAN_MAX = 10


def _http_get(url):
    """Synchronous GET via XHR. Returns (status, text).

    Synchronous XHR is deprecated but still works in every major browser,
    and it is the only way to keep taken's synchronous pipeline 100%
    unmodified in Pyodide.
    """
    from js import XMLHttpRequest

    xhr = XMLHttpRequest.new()
    xhr.open("GET", url, False)
    xhr.setRequestHeader("Accept", "application/vnd.github+json")
    xhr.send()
    return xhr.status, xhr.responseText


def _web_gh_api(endpoint, params=None):
    url = "https://api.github.com/" + endpoint.lstrip("/")
    if params:
        url += "?" + urlencode(params)
    status, text = _http_get(url)
    if status == 200:
        return json.loads(text)
    if status == 404:
        raise checks.NotFoundError(f"not found: {endpoint}")
    if status == 403:
        raise checks.TakenError(
            "GitHub API rate limit reached (60/hour for visitors without login). Try again later."
        )
    raise checks.TakenError(f"GitHub API returned HTTP {status} for {endpoint}")


checks.gh_api = _web_gh_api
checks._CACHE_ENABLED = False
checks.MAX_SCAN_PAGES = 1


def format_human(findings, verdict, reasons):
    """Same human output as the CLI (copied from taken/cli.py)."""
    issue = findings["issue"]
    health = findings["repo_health"]
    policy = findings["ai_policy"]
    # Stages skipped by the cheapest-decisive-first early stop (#125) are
    # reported as not checked, never as observed facts.
    skipped = set(findings.get("stages_skipped") or [])
    not_checked = "not checked (verdict already decided)"
    lines = [
        f"taken? {findings['target']}",
        f"verdict: {verdict}",
        "",
        f'  issue: {issue["state"]}, "{issue["title"]}"',
        f"         {issue['url']} ({issue['comment_count']} comments)",
    ]
    if "timeline" in skipped:
        lines.append(f"  linked PRs: {not_checked}")
    elif findings["linked_prs"]:
        for pr in findings["linked_prs"]:
            if pr["state"] == "open":
                status = "open"
            elif pr["merged"]:
                status = "merged"
            else:
                status = "closed"
            lines.append(f'  linked PR: #{pr["number"]} "{pr["title"]}" ({status})')
            lines.append(f"             {pr['url']}")
    else:
        lines.append("  linked PRs: none found in timeline")
    if issue["assignees"]:
        lines.append(f"  assignees: {', '.join(issue['assignees'])}")
    else:
        lines.append("  assignees: none")
    if "claimants" in skipped:
        lines.append(f"  claimants: {not_checked}")
    elif findings["claimants"]:
        for hit in findings["claimants"]:
            lines.append(
                f'  claimant: {hit["author"]} on {hit["date"]} (matched "{hit["pattern"]}")'
            )
            lines.append(f'            "{hit["snippet"]}"')
    else:
        lines.append("  claimants: none found in comments")
    if "ai_policy" in skipped:
        lines.append(f"  AI policy: {not_checked}")
    elif policy["source"]:
        lines.append(f"  AI policy: {policy['verdict']} ({policy['source']})")
        if policy["snippet"]:
            lines.append(f'             "{policy["snippet"]}"')
    else:
        lines.append("  AI policy: none found (no CONTRIBUTING file)")
    if "repo_health" in skipped:
        lines.append(f"  repo health: {not_checked}")
    else:
        lines.append(
            f"  repo health: pushed {health['pushed_at'] or 'unknown'}, "
            f"{health['recent_merges']} PRs merged in last 30 days, "
            f"{health['contributors']} contributors in last 90 days"
        )
    friendly = checks.friendly_labels(findings)
    if friendly:
        lines.append(f"  first-time friendly: {', '.join(friendly)}")
    welcoming = checks.welcoming_signals(findings)
    if welcoming:
        lines.append(f"  welcoming: {', '.join(welcoming)}")
    lines.append("")
    lines.append("why:")
    for reason in reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)


HELP = """usage: taken [owner/repo#123 | issue URL | owner/repo] [--me login] [--limit N]

This console runs taken's real Python code in your browser (Pyodide),
doing live checks against GitHub's public API. No login, nothing installed.

  taken owner/repo#123
  taken https://github.com/owner/repo/issues/123
  taken owner/repo#123 --me mylogin   (ignore your own comments)
  taken owner/repo                (scan open issues, recommend GO ones)
  taken owner/repo --limit 3 --label "good first issue"

  taken --discover --limit 3   (live search + full verification)
  taken --discover --language python --min-contributors 3

offline (no API calls):
  taken --version
  clear

Repo scans check each issue live (~8 API requests each). Visitors get
60 requests/hour, so scans default to 5 issues (max 10)."""

# Web live discover: 2 labels x 1 search page each, verify up to 3
# candidates (~9 requests each). ~30 requests total, inside the 60/hour
# visitor budget. Scoring mirrors taken/discover.py.
WEB_DISCOVER_LABELS = ["good first issue", "help wanted"]
WEB_DISCOVER_PER_LABEL = 5
WEB_DISCOVER_DEFAULT = 3
WEB_DISCOVER_MAX = 5


def _days_ago(iso_ts):
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


# Mirrors taken/discover.py::MAINTAINER_ASSOCIATIONS: only these comment
# author_associations count as maintainer engagement.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _maintainer_engaged(issue, comments, me=None):
    """A maintainer (OWNER/MEMBER/COLLABORATOR), not the author, bot, or --me, commented."""
    author = issue.get("author")
    for comment in comments:
        login = (comment.get("user") or {}).get("login") or ""
        if not login or login == author or login.endswith("[bot]"):
            continue
        if me and login.lower() == me.lower():
            continue
        if comment.get("author_association") in MAINTAINER_ASSOCIATIONS:
            return True
    return False


def _score_candidate(findings, updated_at, engaged):
    """Same explainable score as taken/discover.py."""
    points = 0
    why = []
    if engaged:
        points += 3
        why.append("maintainer replied")
    age_days = _days_ago(updated_at)
    if age_days is not None and age_days <= 7:
        points += 2
        why.append(f"updated {age_days}d ago")
    push_days = _days_ago(findings["repo_health"].get("pushed_at") or "")
    if push_days is not None and push_days <= 7:
        points += 1
        why.append(f"repo pushed {push_days}d ago")
    if not why:
        why.append("passed verification")
    return points, why


def run_discover_web(limit, language, label, min_contributors, me):
    """Live candidate discovery: search GitHub, verify each, rank GO ones."""
    updated_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    labels = [label] if label else WEB_DISCOVER_LABELS
    candidates, seen = [], set()
    search_errors = []
    searched = 0
    for lab in labels:
        query = f'is:open is:issue no:assignee label:"{lab}" updated:>={updated_after}'
        if language:
            query += f" language:{language}"
        try:
            items = checks.search_issues(query, per_page=WEB_DISCOVER_PER_LABEL)
        except checks.TakenError as exc:
            # Record and continue: one failed label search must not abandon
            # the candidates already gathered (mirrors the engine's #162
            # partial-results fallback in taken/discover.py). On the
            # 60 req/hr visitor budget a rate-limited search is the expected
            # failure mode, not a corner case.
            search_errors.append((lab, str(exc)))
            continue
        searched += 1
        for item in items:
            url = (item.get("repository_url") or "").rstrip("/").split("/")
            if len(url) < 2:
                continue
            owner, repo = url[-2], url[-1]
            key = (owner, repo, item.get("number"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((owner, repo, item.get("number"), item.get("updated_at") or ""))
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break
    if not candidates and search_errors and not searched:
        # Every label search failed: total failure stays an error, never a
        # silent empty success.
        return f"error: live discover failed: {search_errors[0][1]}"
    ranked = []
    for owner, repo, number, updated_at in candidates:
        try:
            findings = checks.run_checks(owner, repo, number, me=me)
        except checks.TakenError:
            continue  # fail-closed per issue; keep scanning the rest
        verdict, _reasons = decide(findings)
        if verdict != GO:
            continue
        if (findings["repo_health"].get("contributors") or 0) < min_contributors:
            continue
        try:
            # Truncation here only affects engagement scoring, not the verdict.
            comments, _ = checks.fetch_comments(owner, repo, number)
        except checks.TakenError:
            comments = []
        engaged = _maintainer_engaged(findings["issue"], comments, me=me)
        points, why = _score_candidate(findings, updated_at, engaged)
        markers = "".join(
            f" [{m}]" for m in checks.friendly_labels(findings) + checks.welcoming_signals(findings)
        )
        ranked.append((points, updated_at, f"{owner}/{repo}#{number}", why, markers))
    if not ranked:
        out = "no GO candidates found live. Try again later or widen with --language/--label."
        notes = _search_error_notes(search_errors)
        return out + ("\n" + "\n".join(notes) if notes else "")
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    lines = [
        "taken? --discover",
        "live search + full verification, ranked by maintainer responsiveness.",
        "",
    ]
    for points, _updated_at, target, why, markers in ranked:
        lines.append(f"{points:3}  {target}  {'; '.join(why)}{markers}")
    lines.append("")
    lines.append(f"{len(ranked)} GO candidate(s). Only GO verdicts are ranked.")
    lines.extend(_search_error_notes(search_errors))
    return "\n".join(lines)


def _search_error_notes(search_errors):
    """One output line per failed label search, [] when none failed."""
    return [
        f'note: search for label "{lab}" failed ({err}); showing partial results.'
        for lab, err in search_errors
    ]


def _parse_target(text):
    text = text.strip()
    for pattern in (URL_RE, SHORT_RE, PATH_RE):
        match = pattern.match(text)
        if match:
            owner, repo, number = match.groups()
            return ("issue", owner, repo, int(number))
    match = REPO_RE.match(text)
    if match:
        owner, repo = match.groups()
        return ("repo", owner, repo)
    return None


def run_repo_scan(owner, repo, limit, label, me):
    """Scan a repo's open issues and recommend the GO ones."""
    try:
        issues = checks.list_open_issues(owner, repo, limit=limit, label=label)
    except checks.TakenError as exc:
        return f"error: {owner}/{repo}: {exc}"
    if not issues:
        return f"note: {owner}/{repo}: no open issues found"
    lines = [
        f"taken? {owner}/{repo}  (scanning {len(issues)} most recently updated open issues)",
        "",
    ]
    gos = []
    for item in issues:
        number = item["number"]
        try:
            # The listing already fetched this issue: reuse it instead of
            # refetching per issue (issue #211).
            findings = checks.run_checks(owner, repo, number, me=me, payload=item)
        except checks.TakenError as exc:
            lines.append(f"ERROR   {owner}/{repo}#{number}  {exc}")
            continue
        verdict, reasons = decide(findings)
        first = reasons[0] if reasons else ""
        lines.append(f"{verdict:7} {owner}/{repo}#{number}  {first}")
        if verdict == "GO":
            gos.append((f"{owner}/{repo}#{number}", checks.friendly_labels(findings)))
    lines.append("")
    if gos:
        # First-time-friendly issues first: the safest ones to adopt.
        gos.sort(key=lambda item: (not item[1], item[0]))
        noun = "candidate" if len(gos) == 1 else "candidates"
        parts = [f"{target} ({', '.join(labels)})" if labels else target for target, labels in gos]
        lines.append(f"{len(gos)} GO {noun}: " + ", ".join(parts))
    else:
        lines.append("no GO candidates in this scan.")
    return "\n".join(lines)


def run_command(line):
    """Run one console line; return the text to print."""
    line = line.strip()
    if not line:
        return ""
    low = line.lower()
    if low in ("taken --help", "help"):
        return HELP
    if low == "taken --version":
        return "taken 0.8.0 (Pyodide build: taken's real Python code, running in your browser)"
    if low.startswith("taken --discover"):
        discover_rest = low[len("taken --discover") :].strip()
        dlimit = WEB_DISCOVER_DEFAULT
        dlimit_match = re.search(r"--limit\s+(\d+)", discover_rest)
        if dlimit_match:
            dlimit = min(int(dlimit_match.group(1)), WEB_DISCOVER_MAX)
        dlang = None
        dlang_match = re.search(r"--language\s+(\S+)", discover_rest)
        if dlang_match:
            dlang = dlang_match.group(1)
        dlabel = None
        dlabel_match = re.search(r'--label\s+"([^"]+)"|--label\s+(\S+)', discover_rest)
        if dlabel_match:
            dlabel = dlabel_match.group(1) or dlabel_match.group(2)
        dcontributors = 0
        dcontributors_match = re.search(r"--min-contributors\s+(\d+)", discover_rest)
        if dcontributors_match:
            dcontributors = int(dcontributors_match.group(1))
        dme = None
        dme_match = re.search(r"--me\s+(\S+)", discover_rest)
        if dme_match:
            dme = dme_match.group(1)
        return run_discover_web(dlimit, dlang, dlabel, dcontributors, dme)
    m = re.match(r"^taken\s+(.+)$", line, re.I)
    if not m:
        return 'unknown command. Try "taken --help".'
    rest = m.group(1).strip()
    me = None
    me_match = re.search(r"--me\s+(\S+)", rest, re.I)
    if me_match:
        me = me_match.group(1)
        rest = re.sub(r"--me\s+\S+", "", rest, flags=re.I).strip()
    limit = WEB_SCAN_DEFAULT
    limit_match = re.search(r"--limit\s+(\d+)", rest, re.I)
    if limit_match:
        limit = min(int(limit_match.group(1)), WEB_SCAN_MAX)
        rest = re.sub(r"--limit\s+\d+", "", rest, flags=re.I).strip()
    label = None
    label_match = re.search(r'--label\s+"([^"]+)"|--label\s+(\S+)', rest, re.I)
    if label_match:
        label = label_match.group(1) or label_match.group(2)
        rest = re.sub(r'--label\s+("[^"]+"|\S+)', "", rest, flags=re.I).strip()
    target = _parse_target(rest)
    if not target:
        return "need an issue target: taken owner/repo#123 (or taken owner/repo to scan)"
    if target[0] == "repo":
        _, owner, repo = target
        return run_repo_scan(owner, repo, limit, label, me)
    _, owner, repo, number = target
    try:
        findings = checks.run_checks(owner, repo, number, me=me)
    except checks.TakenError as e:
        return f"error: {e}"
    verdict, reasons = decide(findings)
    return format_human(findings, verdict, reasons)
