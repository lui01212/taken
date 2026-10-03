"""Truncation tests (issue #118): scans that stop early at the page cap must
never produce a silent GO. A truncated timeline or comment scan downgrades
the verdict to CAUTION on both the REST and GraphQL fetch paths."""

from taken import budget, checks, graphql
from taken.verdict import CAUTION, GO, decide


def _issue_dict():
    return {
        "state": "open",
        "title": "Some issue",
        "labels": [],
        "assignees": [],
        "comments": 0,
        "user": {"login": "someone"},
        "html_url": "https://github.com/o/r/issues/1",
        "created_at": "2026-09-01T00:00:00Z",
    }


def _repo_dict():
    return {"pushed_at": "2026-09-26T00:00:00Z", "stargazers_count": 1}


def _quiet_run_checks_fake(monkeypatch, *, timeline_pages=1, comment_pages=1):
    """Fake gh_api: busy scans but no verdict signals anywhere.

    timeline_pages / comment_pages full pages of inert events; everything
    else quiet (open issue, no assignees, no policy, recent push).
    """
    pages_seen = {"timeline": 0, "comments": 0}

    def fake(endpoint, params=None):
        if endpoint.endswith("/timeline"):
            pages_seen["timeline"] += 1
            if pages_seen["timeline"] <= timeline_pages:
                return [{"event": "labeled"}] * 100
            return []
        if endpoint.endswith("/comments"):
            pages_seen["comments"] += 1
            if pages_seen["comments"] <= comment_pages:
                return [
                    {
                        "user": {"login": f"user{i}"},
                        "body": "nice idea, thanks",
                        "created_at": "2026-09-01T00:00:00Z",
                    }
                    for i in range(100)
                ]
            return []
        if "/contents/" in endpoint:
            raise checks.NotFoundError(endpoint)
        if endpoint.endswith("/pulls"):
            return []
        if endpoint.endswith("/commits"):
            return []
        if endpoint == "repos/o/r":
            return _repo_dict()
        if endpoint == "repos/o/r/issues/1":
            return _issue_dict()
        raise AssertionError("unexpected endpoint " + endpoint)

    monkeypatch.setattr(checks, "gh_api", fake)
    return pages_seen


def _go_findings(**over):
    findings = {
        "target": "o/r#1",
        "issue": _issue_dict(),
        "linked_prs": [],
        "claimants": [],
        "ai_policy": {"verdict": "none-found", "snippet": "", "source": None},
        "repo_health": {
            "pushed_at": "2026-09-26",
            "pushed_recently": True,
            "recent_merges": 1,
            "contributors": 3,
            "contributors_window_days": 30,
        },
    }
    findings.update(over)
    return findings


def test_five_full_timeline_pages_flag_truncation(monkeypatch):
    pages_seen = _quiet_run_checks_fake(monkeypatch, timeline_pages=5)
    linked, truncated = checks.check_timeline("o", "r", 1)
    assert linked == []
    assert truncated is True
    assert pages_seen["timeline"] == 5  # stopped at the cap, never asked for page 6


def test_five_full_comment_pages_flag_truncation(monkeypatch):
    pages_seen = _quiet_run_checks_fake(monkeypatch, comment_pages=5)
    hits, _, truncated = checks.check_claimants("o", "r", 1)
    assert hits == []
    assert truncated is True
    assert pages_seen["comments"] == 5


def test_short_page_is_not_truncation(monkeypatch):
    _quiet_run_checks_fake(monkeypatch, timeline_pages=2, comment_pages=1)
    _, timeline_truncated = checks.check_timeline("o", "r", 1)
    _, _, comments_truncated = checks.check_claimants("o", "r", 1)
    assert timeline_truncated is False
    assert comments_truncated is False


def test_truncated_timeline_downgrades_go_to_caution(monkeypatch):
    _quiet_run_checks_fake(monkeypatch, timeline_pages=5)
    findings = checks.run_checks("o", "r", 1)
    assert findings["scan_truncated"] == {"timeline": True, "comments": False, "labels": False}
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("timeline scan stopped early" in r for r in reasons)


def test_truncated_comments_downgrade_go_to_caution(monkeypatch):
    _quiet_run_checks_fake(monkeypatch, comment_pages=5)
    findings = checks.run_checks("o", "r", 1)
    assert findings["scan_truncated"] == {"timeline": False, "comments": True, "labels": False}
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("comment scan hit the page cap" in r for r in reasons)


def test_untruncated_scan_stays_go(monkeypatch):
    _quiet_run_checks_fake(monkeypatch)
    findings = checks.run_checks("o", "r", 1)
    assert findings["scan_truncated"] == {"timeline": False, "comments": False, "labels": False}
    verdict, _ = decide(findings)
    assert verdict == GO


def test_decide_without_truncation_key_stays_compatible():
    # Findings built before this change (no scan_truncated key) still work.
    verdict, _ = decide(_go_findings())
    assert verdict == GO


def test_decide_truncation_flag_causes_caution():
    findings = _go_findings(scan_truncated={"timeline": True, "comments": False})
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("timeline scan stopped early" in r for r in reasons)


def test_graphql_paginate_flags_truncation():
    conn = {"nodes": [{"n": 1}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}

    def fetch_next(variables):
        return {"nodes": [{"n": 2}], "pageInfo": {"hasNextPage": True, "endCursor": "c2"}}

    nodes, truncated = graphql._paginate(conn, fetch_next, {}, "after", 3)
    assert len(nodes) == 3
    assert truncated is True


def test_graphql_paginate_exhausted_is_not_truncation():
    conn = {"nodes": [{"n": 1}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}

    def fetch_next(variables):
        return {"nodes": [{"n": 2}], "pageInfo": {"hasNextPage": False, "endCursor": None}}

    nodes, truncated = graphql._paginate(conn, fetch_next, {}, "after", 3)
    assert len(nodes) == 2
    assert truncated is False


def test_graphql_findings_flag_truncated_timeline(monkeypatch):
    """A timeline connection that never runs out of pages flags truncation."""
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)

    def payload():
        issue = {
            "state": "OPEN",
            "title": "Some issue",
            "url": "https://github.com/o/r/issues/1",
            "createdAt": "2026-09-01T00:00:00Z",
            "author": {"login": "someone"},
            "assignees": {"nodes": []},
            "labels": {"nodes": []},
            "comments": {
                "totalCount": 0,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            },
            "timelineItems": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                "nodes": [{"__typename": "LabeledEvent"}],
            },
        }
        return {
            "data": {
                "repository": {
                    "pushedAt": "2026-09-26T00:00:00Z",
                    "issue": issue,
                    "ai1": None,
                    "ai2": None,
                    "ai3": None,
                    "ai4": None,
                    "mergedPRs": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    },
                    "defaultBranchRef": {
                        "target": {
                            "history": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    },
                },
                "rateLimit": {"limit": 5000, "cost": 1},
            }
        }

    monkeypatch.setattr(graphql, "graphql_via_gh", lambda q, v: payload())
    findings = graphql.run_checks_graphql("o", "r", 1, mode="graphql")
    assert findings["scan_truncated"] == {"timeline": True, "comments": False, "labels": False}
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("timeline scan stopped early" in r for r in reasons)


def test_ten_full_timeline_pages_flag_truncation_when_authenticated(monkeypatch):
    """In authenticated tier (cap=10), 10 full timeline pages flag truncation."""
    budget.activate(identity="someone")
    pages_seen = _quiet_run_checks_fake(monkeypatch, timeline_pages=10)
    linked, truncated = checks.check_timeline("o", "r", 1)
    assert linked == []
    assert truncated is True
    assert pages_seen["timeline"] == 10


def test_ten_full_comment_pages_flag_truncation_when_authenticated(monkeypatch):
    """In authenticated tier (cap=10), 10 full comment pages flag truncation."""
    budget.activate(identity="someone")
    pages_seen = _quiet_run_checks_fake(monkeypatch, comment_pages=10)
    hits, _, truncated = checks.check_claimants("o", "r", 1)
    assert hits == []
    assert truncated is True
    assert pages_seen["comments"] == 10


def test_nine_full_pages_not_truncated_when_authenticated(monkeypatch):
    """In authenticated tier (cap=10), 9 pages followed by an empty page is not truncated."""
    budget.activate(identity="someone")
    pages_seen = _quiet_run_checks_fake(monkeypatch, timeline_pages=9)
    linked, truncated = checks.check_timeline("o", "r", 1)
    assert linked == []
    assert truncated is False
    assert pages_seen["timeline"] == 10
