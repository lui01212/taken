"""Tests for issue #187: maintainer-facing repo health overview."""

from datetime import datetime, timedelta, timezone

from taken import checks, health
from taken.health import HealthOptions, repo_health

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_comment(login, body, days_ago, association="NONE"):
    return {
        "user": {"login": login},
        "body": body,
        "created_at": ts(days_ago),
        "author_association": association,
        "html_url": "https://github.com/o/r/issues/1#issuecomment-9",
    }


def make_issue(number, updated_days_ago, labels=(), comment_count=0, created_days_ago=None):
    created = created_days_ago if created_days_ago is not None else updated_days_ago
    return {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": name} for name in labels],
        "comments": comment_count,
        "updated_at": ts(updated_days_ago),
        "created_at": ts(created),
    }


def make_pr(number, updated_days_ago, draft=False):
    return {
        "number": number,
        "title": f"pr {number}",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "user": {"login": "contrib"},
        "updated_at": ts(updated_days_ago),
        "draft": draft,
        "state": "open",
    }


def make_gh(issues=(), comments_map=None, pulls=()):
    """Fake checks.gh_api serving the health endpoints."""

    def fake(endpoint, params=None):
        params = params or {}
        if params.get("page", "1") != "1":
            return []
        if endpoint.endswith("/comments"):
            number = int(endpoint.split("/")[-2])
            return list((comments_map or {}).get(number, []))
        if endpoint == "repos/o/r/pulls":
            return list(pulls)
        if endpoint == "repos/o/r/issues":
            return list(issues)
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return fake


def run(issues=(), comments_map=None, pulls=(), options=None, me=None):
    return repo_health("o", "r", options=options or HealthOptions(), me=me, now=NOW)


# Waiting claims


def test_waiting_claim_listed_oldest_first(monkeypatch):
    issues = (
        make_issue(1, 10, comment_count=1),
        make_issue(2, 20, comment_count=1),
    )
    comments = {
        1: [make_comment("alice", "I'd like to take this", 10)],
        2: [make_comment("bob", "please assign me", 20)],
    }
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    report = run(issues, comments)
    assert [c.number for c in report.waiting_claims] == [2, 1]
    assert report.waiting_claims[0].claimant == "bob"
    assert report.waiting_claims[0].age_days == 20


def test_claim_below_wait_threshold_ignored(monkeypatch):
    issues = (make_issue(1, 3, comment_count=1),)
    comments = {1: [make_comment("alice", "I'd like to take this", 3)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    assert run(issues, comments).waiting_claims == []


def test_claim_wait_threshold_configurable(monkeypatch):
    issues = (make_issue(1, 3, comment_count=1),)
    comments = {1: [make_comment("alice", "I'd like to take this", 3)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    report = run(issues, comments, options=HealthOptions(claim_wait_days=3))
    assert len(report.waiting_claims) == 1


def test_maintainer_claim_is_not_waiting(monkeypatch):
    issues = (make_issue(1, 10, comment_count=1),)
    comments = {1: [make_comment("owner", "I'll take this", 10, association="OWNER")]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    assert run(issues, comments).waiting_claims == []


def test_own_claim_ignored_with_me(monkeypatch):
    issues = (make_issue(1, 10, comment_count=1),)
    comments = {1: [make_comment("alice", "I'd like to take this", 10)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    assert run(issues, comments, me="alice").waiting_claims == []


def test_latest_claim_per_claimant_wins(monkeypatch):
    issues = (make_issue(1, 20, comment_count=2),)
    comments = {
        1: [
            make_comment("alice", "please assign me", 20),
            make_comment("alice", "still interested, I'd like to take this", 10),
        ]
    }
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    report = run(issues, comments)
    assert len(report.waiting_claims) == 1
    assert report.waiting_claims[0].age_days == 10


# Quiet claims


def test_quiet_claim_after_maintainer_reply(monkeypatch):
    issues = (make_issue(1, 20, comment_count=2),)
    comments = {
        1: [
            make_comment("alice", "I'd like to take this", 20),
            make_comment("owner", "go for it", 9, association="OWNER"),
        ]
    }
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    report = run(issues, comments)
    assert report.waiting_claims == []
    assert len(report.quiet_claims) == 1
    quiet = report.quiet_claims[0]
    assert quiet.claimant == "alice"
    assert quiet.maintainer == "owner"
    assert quiet.quiet_days == 9


def test_claimant_answers_after_reply_is_not_quiet(monkeypatch):
    issues = (make_issue(1, 20, comment_count=3),)
    comments = {
        1: [
            make_comment("alice", "I'd like to take this", 20),
            make_comment("owner", "go for it", 9, association="OWNER"),
            make_comment("alice", "working on this now", 2),
        ]
    }
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    report = run(issues, comments)
    assert report.quiet_claims == []
    assert report.waiting_claims == []


def test_recent_maintainer_reply_is_neither_waiting_nor_quiet(monkeypatch):
    issues = (make_issue(1, 20, comment_count=2),)
    comments = {
        1: [
            make_comment("alice", "I'd like to take this", 20),
            make_comment("owner", "go for it", 3, association="OWNER"),
        ]
    }
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    report = run(issues, comments)
    assert report.quiet_claims == []
    assert report.waiting_claims == []


# Stale PRs


def test_pr_age_bands_and_threshold(monkeypatch):
    pulls = (make_pr(1, 45), make_pr(2, 16), make_pr(3, 10))
    monkeypatch.setattr(checks, "gh_api", make_gh(pulls=pulls))
    report = run(pulls=pulls)
    assert [(p.number, p.band) for p in report.stale_prs] == [
        (1, "a month+"),
        (2, "two weeks+"),
    ]


def test_pr_stale_threshold_configurable(monkeypatch):
    pulls = (make_pr(1, 10),)
    monkeypatch.setattr(checks, "gh_api", make_gh(pulls=pulls))
    report = run(pulls=pulls, options=HealthOptions(pr_stale_days=7))
    assert [(p.number, p.band) for p in report.stale_prs] == [(1, "a week+")]


def test_pr_draft_flagged(monkeypatch):
    pulls = (make_pr(1, 20, draft=True),)
    monkeypatch.setattr(checks, "gh_api", make_gh(pulls=pulls))
    report = run(pulls=pulls)
    assert report.stale_prs[0].draft is True
    assert "(draft)" in health.format_health_human(report)


# Stale beginner labels


def test_stale_beginner_labels(monkeypatch):
    issues = (
        make_issue(1, 40, labels=["good first issue"]),
        make_issue(2, 35, labels=["hacktoberfest"]),
        make_issue(3, 5, labels=["good first issue"]),
        make_issue(4, 90, labels=["docs"]),
    )
    monkeypatch.setattr(checks, "gh_api", make_gh(issues))
    report = run(issues)
    assert [e.number for e in report.stale_beginner_labels] == [1, 2]


def test_beginner_label_threshold_configurable(monkeypatch):
    issues = (make_issue(1, 10, labels=["good first issue"]),)
    monkeypatch.setattr(checks, "gh_api", make_gh(issues))
    report = run(issues, options=HealthOptions(gfi_stale_days=7))
    assert [e.number for e in report.stale_beginner_labels] == [1]


# Untriaged


def test_untriaged_without_comments(monkeypatch):
    issues = (make_issue(1, 12, comment_count=0, created_days_ago=12),)
    monkeypatch.setattr(checks, "gh_api", make_gh(issues))
    report = run(issues)
    assert [e.number for e in report.untriaged] == [1]
    assert report.untriaged[0].age_days == 12


def test_untriaged_with_only_outsider_comments(monkeypatch):
    issues = (make_issue(1, 12, comment_count=1),)
    comments = {1: [make_comment("alice", "looks broken", 5)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    assert [e.number for e in run(issues, comments).untriaged] == [1]


def test_untriaged_excludes_maintainer_commented(monkeypatch):
    issues = (make_issue(1, 12, comment_count=1),)
    comments = {1: [make_comment("owner", "triaging", 5, association="MEMBER")]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    assert run(issues, comments).untriaged == []


def test_untriaged_excludes_labeled(monkeypatch):
    issues = (make_issue(1, 12, labels=["bug"], comment_count=0),)
    monkeypatch.setattr(checks, "gh_api", make_gh(issues))
    assert run(issues).untriaged == []


# Report shape


def test_summary_counts(monkeypatch):
    issues = (
        make_issue(1, 20, comment_count=1),
        make_issue(2, 40, labels=["good first issue"]),
        make_issue(3, 12, comment_count=0),
    )
    comments = {1: [make_comment("alice", "please assign me", 20)]}
    pulls = (make_pr(9, 45),)
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments, pulls))
    summary = run(issues, comments, pulls).summary()
    assert summary == {
        "waiting_claims": 1,
        "oldest_wait_days": 20,
        "quiet_claims": 0,
        "stale_prs": 1,
        "stale_beginner_labels": 1,
        "untriaged": 2,  # #1 (no labels, outsider comment only) and #3
    }


def test_human_format_sections(monkeypatch):
    issues = (make_issue(1, 20, comment_count=1),)
    comments = {1: [make_comment("alice", "please assign me", 20)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    text = health.format_health_human(run(issues, comments))
    assert text.startswith("health: o/r\n")
    assert "summary: 1 claims waiting (oldest 20 days ago)" in text
    assert "claims waiting on you (1):" in text
    assert "@alice claimed 20 days ago, no maintainer reply" in text
    assert "quiet claims (0):\n  none" in text
    assert "stale PRs (0):\n  none" in text


def test_human_format_all_clear(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_gh())
    text = health.format_health_human(run())
    assert "0 claims waiting" in text
    assert "untriaged (0):\n  none" in text


def test_to_dict_keys(monkeypatch):
    issues = (make_issue(1, 20, comment_count=1),)
    comments = {1: [make_comment("alice", "please assign me", 20)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    data = health.health_to_dict(run(issues, comments))
    assert data["owner"] == "o"
    assert data["repo"] == "r"
    assert data["truncated"] is False
    assert set(data) == {
        "owner",
        "repo",
        "truncated",
        "summary",
        "waiting_claims",
        "quiet_claims",
        "stale_prs",
        "stale_beginner_labels",
        "untriaged",
        "comment_fetch_errors",
        "comment_fetch_skipped",
        "pr_scan_failed",
    }
    assert data["waiting_claims"][0]["claimant"] == "alice"
    assert data["comment_fetch_errors"] == []
    assert data["comment_fetch_skipped"] == []
    assert data["pr_scan_failed"] is False


def test_truncated_when_issue_limit_hit(monkeypatch):
    issues = (make_issue(1, 1), make_issue(2, 2))
    monkeypatch.setattr(checks, "gh_api", make_gh(issues))
    assert run(issues, options=HealthOptions(issue_limit=2)).truncated is True
    assert run(issues, options=HealthOptions(issue_limit=5)).truncated is False


# Failure isolation (issue #331)


def make_failing_gh(issues=(), comments_map=None, pulls=(), fail_comments=(), fail_pulls=False):
    """Fake checks.gh_api that raises TakenError on chosen endpoints."""

    def fake(endpoint, params=None):
        params = params or {}
        if params.get("page", "1") != "1":
            return []
        if endpoint.endswith("/comments"):
            number = int(endpoint.split("/")[-2])
            if number in fail_comments:
                raise checks.TakenError("boom")
            return list((comments_map or {}).get(number, []))
        if endpoint == "repos/o/r/pulls":
            if fail_pulls:
                raise checks.TakenError("boom")
            return list(pulls)
        if endpoint == "repos/o/r/issues":
            return list(issues)
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return fake


def test_comment_fetch_error_isolated_per_issue(monkeypatch):
    """One bad comment fetch fails that issue closed; the report survives."""
    issues = (
        make_issue(1, 20, comment_count=1),
        make_issue(2, 20, comment_count=1),
        make_issue(3, 40, labels=["good first issue"], comment_count=2),
    )
    comments = {
        1: [make_comment("alice", "please assign me", 20)],
        3: [make_comment("carol", "looks fun", 40)],
    }
    monkeypatch.setattr(checks, "gh_api", make_failing_gh(issues, comments, fail_comments=(2,)))
    report = run(issues, comments)
    assert [c.number for c in report.waiting_claims] == [1]
    assert report.comment_fetch_errors == [2]
    # Label-fed sections never needed the comments: still complete.
    assert [e.number for e in report.stale_beginner_labels] == [3]
    text = health.format_health_human(report)
    assert "comment fetch failed for 1 issue(s) (#2)" in text
    data = health.health_to_dict(report)
    assert data["comment_fetch_errors"] == [2]


def test_zero_comment_issues_skip_fetch(monkeypatch):
    """Issues whose listing shows zero comments never hit the comments API."""
    issues = (
        make_issue(1, 20, comment_count=0),
        make_issue(2, 40, labels=["good first issue"], comment_count=0),
    )

    def no_comments_allowed(endpoint, params=None):
        if endpoint.endswith("/comments"):
            raise AssertionError("comment fetch must be skipped")
        return make_gh(issues)(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", no_comments_allowed)
    report = run(issues)
    assert [e.number for e in report.untriaged] == [1]  # #2 carries a label
    assert [e.number for e in report.stale_beginner_labels] == [2]
    assert report.comment_fetch_errors == []


def test_budget_exhaustion_skips_comment_fetches(monkeypatch):
    """A spent budget degrades to listing-fed sections instead of dying."""
    issues = (
        make_issue(1, 20, comment_count=3),
        make_issue(2, 40, labels=["good first issue"], comment_count=1),
    )

    def no_comments_allowed(endpoint, params=None):
        if endpoint.endswith("/comments"):
            raise AssertionError("budget guard must skip comment fetches")
        return make_gh(issues)(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", no_comments_allowed)
    monkeypatch.setattr(health, "_api_calls_used", lambda: 60)
    report = run(issues)
    assert report.comment_fetch_skipped == [1, 2]
    assert report.comment_fetch_errors == []
    # Listing-fed sections still built from the same data; comment-fed
    # sections stay empty for skipped issues (fail-closed).
    assert [e.number for e in report.stale_beginner_labels] == [2]
    assert report.untriaged == []
    assert report.waiting_claims == []
    text = health.format_health_human(report)
    assert "API budget nearly spent; skipped comment fetches for 2 issue(s)" in text
    data = health.health_to_dict(report)
    assert data["comment_fetch_skipped"] == [1, 2]


def test_budget_guard_ignores_cache_warm_runs(monkeypatch):
    """The guard counts real calls, so it stays quiet well under budget."""
    issues = (make_issue(1, 20, comment_count=1),)
    comments = {1: [make_comment("alice", "please assign me", 20)]}
    monkeypatch.setattr(checks, "gh_api", make_gh(issues, comments))
    monkeypatch.setattr(health, "_api_calls_used", lambda: 12)
    report = run(issues, comments)
    assert report.comment_fetch_skipped == []
    assert [c.number for c in report.waiting_claims] == [1]


def test_pr_scan_failure_degrades(monkeypatch):
    """A failing PR scan loses the stale-PR section, not the report."""
    issues = (make_issue(1, 20, comment_count=1),)
    comments = {1: [make_comment("alice", "please assign me", 20)]}
    monkeypatch.setattr(checks, "gh_api", make_failing_gh(issues, comments, fail_pulls=True))
    report = run(issues, comments)
    assert report.pr_scan_failed is True
    assert report.stale_prs == []
    assert [c.number for c in report.waiting_claims] == [1]
    text = health.format_health_human(report)
    assert "PR scan failed; stale PR data is unavailable" in text
    assert health.health_to_dict(report)["pr_scan_failed"] is True


def test_cli_health_flags():
    from taken.cli import build_parser

    args = build_parser().parse_args(["--health", "o/r", "--claim-wait-days", "3", "--json"])
    assert args.health is True
    assert args.claim_wait_days == 3
    assert args.pr_stale_days == health.DEFAULT_PR_STALE_DAYS
    assert args.gfi_stale_days == health.DEFAULT_GFI_STALE_DAYS
    assert args.json is True
