"""Maintainer-facing repo health overview (issue #187).

Read-only report answering "what needs a maintainer's attention":

- claim comments with no maintainer reply (oldest first), plus the
  reverse: claims gone quiet after a maintainer replied (reuses the
  stale-claim decay notion from issue #83)
- open PRs past the stale-PR bands (7d / 14d / 30d+)
- beginner labels (good first issue, hacktoberfest) on untouched issues
- untriaged issues: no labels and no maintainer comment at all

Every signal comes from data taken already fetches; verdict logic is
untouched. A maintainer is anyone whose comment carries an
OWNER/MEMBER/COLLABORATOR author_association (the same convention as
discover's maintainer-engagement heuristic).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from taken import budget, checks
from taken.discover import MAINTAINER_ASSOCIATIONS
from taken.verdict import age_phrase

DEFAULT_CLAIM_WAIT_DAYS = 7
DEFAULT_PR_STALE_DAYS = 14
DEFAULT_GFI_STALE_DAYS = 30
DEFAULT_ISSUE_LIMIT = 100

# How many real API calls below the hourly budget the per-issue comment
# loop stops at, so the PR scan and any later sections still have room.
HEALTH_BUDGET_RESERVE = 5

# Beginner labels the stale-label scan watches (case-insensitive).
BEGINNER_LABELS = frozenset({"good first issue", "good-first-issue", "hacktoberfest"})

# PR idle-age bands: (minimum idle days, band label). A PR lands in the
# highest band it qualifies for.
PR_AGE_BANDS = ((30, "a month+"), (14, "two weeks+"), (7, "a week+"))


@dataclass
class HealthOptions:
    """Thresholds for the health report; all in whole days."""

    claim_wait_days: int = DEFAULT_CLAIM_WAIT_DAYS
    pr_stale_days: int = DEFAULT_PR_STALE_DAYS
    gfi_stale_days: int = DEFAULT_GFI_STALE_DAYS
    issue_limit: int = DEFAULT_ISSUE_LIMIT


@dataclass
class WaitingClaim:
    number: int
    title: str
    url: str
    claimant: str
    claim_date: str  # YYYY-MM-DD
    age_days: int
    snippet: str


@dataclass
class QuietClaim:
    number: int
    title: str
    url: str
    claimant: str
    maintainer: str
    reply_date: str  # YYYY-MM-DD
    quiet_days: int


@dataclass
class StalePR:
    number: int
    title: str
    url: str
    author: str
    idle_days: int
    band: str
    draft: bool


@dataclass
class StaleBeginnerLabel:
    number: int
    title: str
    url: str
    labels: list  # matching beginner labels, original casing
    idle_days: int


@dataclass
class UntriagedIssue:
    number: int
    title: str
    url: str
    age_days: int | None


@dataclass
class RepoHealth:
    owner: str
    repo: str
    waiting_claims: list = field(default_factory=list)
    quiet_claims: list = field(default_factory=list)
    stale_prs: list = field(default_factory=list)
    stale_beginner_labels: list = field(default_factory=list)
    untriaged: list = field(default_factory=list)
    truncated: bool = False  # the issue listing hit issue_limit; more may exist
    # Degradation accounting: comment fetch failures fail that issue
    # closed (no claim/untriaged data for it) while the rest of the
    # report keeps building; budget skips stop the comment loop early
    # but leave every listing-fed section intact.
    comment_fetch_errors: list = field(default_factory=list)  # issue numbers
    comment_fetch_skipped: list = field(default_factory=list)  # issue numbers
    pr_scan_failed: bool = False

    def summary(self):
        oldest = max((c.age_days for c in self.waiting_claims), default=None)
        return {
            "waiting_claims": len(self.waiting_claims),
            "oldest_wait_days": oldest,
            "quiet_claims": len(self.quiet_claims),
            "stale_prs": len(self.stale_prs),
            "stale_beginner_labels": len(self.stale_beginner_labels),
            "untriaged": len(self.untriaged),
        }


def _comment_events(comments):
    """Normalize raw comments to (timestamp, author, association, body) dicts.

    Comments without a parseable timestamp or author carry no ordering
    information and are dropped.
    """
    events = []
    for comment in comments or []:
        ts = checks._parse_ts(comment.get("created_at"))
        author = (comment.get("user") or {}).get("login") or ""
        if ts is None or not author:
            continue
        events.append(
            {
                "ts": ts,
                "author": author,
                "association": comment.get("author_association") or "NONE",
                "body": comment.get("body") or "",
                "bot": author.endswith("[bot]"),
            }
        )
    return events


def _is_maintainer(event):
    return event["association"] in MAINTAINER_ASSOCIATIONS and not event["bot"]


def _claim_events(events, me=None):
    """Latest claim comment per claimant, keyed by lowercase login."""
    me_lower = (me or "").lower()
    claims = {}
    for event in events:
        if me_lower and event["author"].lower() == me_lower:
            continue
        lowered = event["body"].lower()
        matched = next((p for p in checks.CLAIMANT_PATTERNS if p in lowered), None)
        if matched is None:
            continue
        key = event["author"].lower()
        if key not in claims or event["ts"] > claims[key]["ts"]:
            claims[key] = {"ts": event["ts"], "event": event}
    return claims


def _maintainer_replies(events, key, claim_ts):
    """Maintainer replies to one claim, excluding the claimant themself."""
    return [
        ev
        for ev in events
        if ev["ts"] > claim_ts and _is_maintainer(ev) and ev["author"].lower() != key
    ]


def _waiting_entry(number, title, url, event, claim_ts, now, wait_days):
    """A WaitingClaim for an unanswered claim old enough, else None."""
    age_days = max(0, (now - claim_ts).days)
    if age_days < wait_days:
        return None
    snippet = " ".join(event["body"].split())[:160]
    return WaitingClaim(
        number=number,
        title=title,
        url=url,
        claimant=event["author"],
        claim_date=claim_ts.strftime("%Y-%m-%d"),
        age_days=age_days,
        snippet=snippet,
    )


def _quiet_entry(number, title, url, claimant, key, events, reply, now, wait_days):
    """A QuietClaim when the claimant went silent after a maintainer reply."""
    answered = any(ev["ts"] > reply["ts"] and ev["author"].lower() == key for ev in events)
    quiet_days = max(0, (now - reply["ts"]).days)
    if answered or quiet_days < wait_days:
        return None
    return QuietClaim(
        number=number,
        title=title,
        url=url,
        claimant=claimant,
        maintainer=reply["author"],
        reply_date=reply["ts"].strftime("%Y-%m-%d"),
        quiet_days=quiet_days,
    )


def _scan_issue_claims(item, events, options, me, now):
    """(waiting, quiet) claim entries for one open issue.

    A claim with no maintainer reply older than claim_wait_days is
    waiting on the maintainer. A claim where a maintainer replied and the
    claimant has said nothing since (for at least claim_wait_days) has
    gone quiet the other way. A maintainer claiming an issue is
    assignment, not a plea awaiting a reply, so those are skipped.
    """
    waiting, quiet = [], []
    number = item["number"]
    title = item.get("title") or ""
    url = item.get("html_url") or ""
    for key, claim in _claim_events(events, me=me).items():
        event = claim["event"]
        if event["association"] in MAINTAINER_ASSOCIATIONS:
            continue
        replies = _maintainer_replies(events, key, claim["ts"])
        if not replies:
            entry = _waiting_entry(
                number,
                title,
                url,
                event,
                claim["ts"],
                now,
                options.claim_wait_days,
            )
            if entry is not None:
                waiting.append(entry)
        else:
            reply = max(replies, key=lambda ev: ev["ts"])
            entry = _quiet_entry(
                number,
                title,
                url,
                event["author"],
                key,
                events,
                reply,
                now,
                options.claim_wait_days,
            )
            if entry is not None:
                quiet.append(entry)
    return waiting, quiet


def _pr_band(idle_days):
    for minimum, label in PR_AGE_BANDS:
        if idle_days >= minimum:
            return label
    return f"{idle_days}d idle"


def _scan_prs(owner, repo, options, now):
    """Open PRs idle for at least pr_stale_days, stalest first.

    Listed oldest-updated first so the scan cap keeps the stalest PRs,
    not the freshest.
    """
    endpoint = f"repos/{owner}/{repo}/pulls"
    prs, _truncated = checks._paged_list(
        endpoint, {"state": "open", "sort": "updated", "direction": "asc"}
    )
    stale = []
    for pr in prs:
        idle_days = checks.days_since(pr.get("updated_at"), now)
        if idle_days is None or idle_days < options.pr_stale_days:
            continue
        stale.append(
            StalePR(
                number=pr.get("number"),
                title=pr.get("title") or "",
                url=pr.get("html_url") or "",
                author=(pr.get("user") or {}).get("login") or "",
                idle_days=idle_days,
                band=_pr_band(idle_days),
                draft=bool(pr.get("draft")),
            )
        )
    return stale


def _scan_beginner_labels(issues, options, now):
    """Beginner-labeled issues untouched for at least gfi_stale_days."""
    stale = []
    for item in issues:
        labels = [label.get("name", "") for label in item.get("labels") or []]
        matched = [label for label in labels if label.lower() in BEGINNER_LABELS]
        if not matched:
            continue
        idle_days = checks.days_since(item.get("updated_at"), now)
        if idle_days is None or idle_days < options.gfi_stale_days:
            continue
        stale.append(
            StaleBeginnerLabel(
                number=item.get("number"),
                title=item.get("title") or "",
                url=item.get("html_url") or "",
                labels=matched,
                idle_days=idle_days,
            )
        )
    stale.sort(key=lambda entry: -entry.idle_days)
    return stale


def _scan_untriaged(item, events, now):
    """An untriaged entry for one issue, or None.

    Untriaged means no labels and no maintainer comment. Issues with no
    comments at all qualify without further evidence.
    """
    if item.get("labels"):
        return None
    if item.get("comments") and any(_is_maintainer(ev) for ev in events):
        return None
    return UntriagedIssue(
        number=item["number"],
        title=item.get("title") or "",
        url=item.get("html_url") or "",
        age_days=checks.days_since(item.get("created_at"), now),
    )


def _api_calls_used():
    """Real `gh api` calls made so far this run (cache hits excluded)."""
    return sum(checks.api_stats_data()["calls"].values())


def _comment_budget_spent():
    """True when the hourly API budget is nearly spent.

    The in-process counter only sees this run while the 60/hr tier is a
    rolling window, so this is a conservative guard, not a guarantee:
    GitHub's own rate-limit error still arrives as TakenError, which the
    per-issue isolation turns into a degraded issue instead of a dead
    report.
    """
    hourly = budget.current().hourly_requests
    return _api_calls_used() >= max(hourly - HEALTH_BUDGET_RESERVE, 0)


def repo_health(owner, repo, options=None, me=None, now=None):
    """Build the maintainer health report for a repo. Read-only.

    Fetches the open-issue listing once, then one comment listing per
    issue with comments (shared by the claim scan and the untriaged
    scan), plus the open-PR listing. Issues whose listing already shows
    zero comments skip the fetch. A TakenError on one issue's comments
    fails that issue closed without aborting the report, the way
    _verify_candidate does per candidate. When the API budget is nearly
    spent the comment loop stops early and the report says which issues
    were skipped. `me` excludes the invoker's own comments from the
    claimant scan; `now` is a test override for the reference time.
    """
    options = options or HealthOptions()
    now = now or datetime.now(timezone.utc)
    issues = checks.list_open_issues(owner, repo, limit=options.issue_limit)
    report = RepoHealth(
        owner=owner,
        repo=repo,
        truncated=len(issues) >= options.issue_limit,
    )
    for item in issues:
        number = item["number"]
        if _comment_budget_spent():
            # Degrade gracefully: skip the remaining comment fetches but
            # keep every section the listing alone can feed.
            report.comment_fetch_skipped.append(number)
            continue
        if not item.get("comments"):
            # The listing already says zero comments: no fetch needed.
            events = []
        else:
            try:
                comments, _truncated = checks.fetch_comments(owner, repo, number)
            except checks.TakenError:
                # Fail that issue closed; keep the rest of the report.
                report.comment_fetch_errors.append(number)
                continue
            events = _comment_events(comments)
        waiting, quiet = _scan_issue_claims(item, events, options, me, now)
        report.waiting_claims.extend(waiting)
        report.quiet_claims.extend(quiet)
        untriaged = _scan_untriaged(item, events, now)
        if untriaged is not None:
            report.untriaged.append(untriaged)
    report.waiting_claims.sort(key=lambda c: -c.age_days)
    report.quiet_claims.sort(key=lambda c: -c.quiet_days)
    report.untriaged.sort(key=lambda e: -(e.age_days or 0))
    try:
        report.stale_prs = _scan_prs(owner, repo, options, now)
    except checks.TakenError:
        # The PR scan is one call chain: lose it, not the report.
        report.pr_scan_failed = True
    report.stale_beginner_labels = _scan_beginner_labels(issues, options, now)
    return report


def health_to_dict(report):
    """JSON-serializable dict of the report."""
    return {
        "owner": report.owner,
        "repo": report.repo,
        "truncated": report.truncated,
        "summary": report.summary(),
        "waiting_claims": [asdict(c) for c in report.waiting_claims],
        "quiet_claims": [asdict(c) for c in report.quiet_claims],
        "stale_prs": [asdict(p) for p in report.stale_prs],
        "stale_beginner_labels": [asdict(e) for e in report.stale_beginner_labels],
        "untriaged": [asdict(e) for e in report.untriaged],
        "comment_fetch_errors": list(report.comment_fetch_errors),
        "comment_fetch_skipped": list(report.comment_fetch_skipped),
        "pr_scan_failed": report.pr_scan_failed,
    }


def format_health_human(report):
    """Human-readable report: summary counts first, details below."""
    summary = report.summary()
    oldest = summary["oldest_wait_days"]
    wait_part = f"{summary['waiting_claims']} claims waiting"
    if oldest is not None:
        wait_part += f" (oldest {age_phrase(oldest)})"
    lines = [
        f"health: {report.owner}/{report.repo}",
        f"summary: {wait_part}, {summary['quiet_claims']} quiet claims, "
        f"{summary['stale_prs']} stale PRs, "
        f"{summary['stale_beginner_labels']} stale beginner labels, "
        f"{summary['untriaged']} untriaged",
        "",
    ]
    if report.truncated:
        lines.append("note: issue scan hit the listing cap; more issues may exist")
        lines.append("")
    if report.comment_fetch_errors:
        nums = ", ".join(f"#{n}" for n in report.comment_fetch_errors)
        lines.append(
            f"note: comment fetch failed for {len(report.comment_fetch_errors)} "
            f"issue(s) ({nums}); claim data for them is missing"
        )
        lines.append("")
    if report.comment_fetch_skipped:
        lines.append(
            f"note: API budget nearly spent; skipped comment fetches for "
            f"{len(report.comment_fetch_skipped)} issue(s)"
        )
        lines.append("")
    if report.pr_scan_failed:
        lines.append("note: PR scan failed; stale PR data is unavailable")
        lines.append("")

    def section(title, entries, render):
        lines.append(f"{title} ({len(entries)}):")
        if entries:
            for entry in entries:
                lines.extend(render(entry))
        else:
            lines.append("  none")
        lines.append("")

    section(
        "claims waiting on you",
        report.waiting_claims,
        lambda c: [
            f'  #{c.number} "{c.title}" — @{c.claimant} claimed {age_phrase(c.age_days)}, '
            "no maintainer reply",
            f"      {c.url}",
            f'      "{c.snippet}"',
        ],
    )
    section(
        "quiet claims",
        report.quiet_claims,
        lambda c: [
            f'  #{c.number} "{c.title}" — @{c.claimant} went quiet '
            f"{age_phrase(c.quiet_days)} after @{c.maintainer} replied",
            f"      {c.url}",
        ],
    )
    section(
        "stale PRs",
        report.stale_prs,
        lambda p: [
            f'  [{p.band}] #{p.number} "{p.title}" by @{p.author} — '
            f"idle {age_phrase(p.idle_days)}" + (" (draft)" if p.draft else ""),
            f"      {p.url}",
        ],
    )
    section(
        "stale beginner labels",
        report.stale_beginner_labels,
        lambda e: [
            f'  #{e.number} "{e.title}" — labels: {", ".join(e.labels)} — '
            f"untouched {age_phrase(e.idle_days)}",
            f"      {e.url}",
        ],
    )
    section(
        "untriaged",
        report.untriaged,
        lambda e: [
            f'  #{e.number} "{e.title}" — opened {age_phrase(e.age_days)}, '
            "no labels, no maintainer comment",
            f"      {e.url}",
        ],
    )
    return "\n".join(lines).rstrip("\n")
