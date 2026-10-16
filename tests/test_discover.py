"""Discover tests: search -> verify -> rank."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from taken import checks, discover
from taken.cli import main
from taken.verdict import TAKEN, decide


def search_item(number, days_ago):
    updated = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "number": number,
        "title": f"issue {number}",
        "user": {"login": "alice"},
        "repository_url": "https://api.github.com/repos/octo/repo",
        "updated_at": updated,
        "html_url": f"https://github.com/octo/repo/issues/{number}",
    }


def comment(login, body="looks good, thanks", author_association="MEMBER"):
    return {
        "user": {"login": login},
        "body": body,
        "author_association": author_association,
        "created_at": "2026-09-20T00:00:00Z",
        "html_url": "https://github.com/octo/repo/issues/1#issuecomment-1",
    }


def make_fake(items, states, comments_map, labels_map=None, contributing=False):
    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            if endpoint.endswith("/comments"):
                return comments_map.get(number, [])
            if endpoint.endswith("/timeline"):
                return []
            kind = states.get(number, "go")
            labels = [{"name": name} for name in (labels_map or {}).get(number, [])]
            return {
                "state": "open",
                "title": f"issue {number}",
                "labels": labels,
                "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
                "comments": len(comments_map.get(number, [])),
                "user": {"login": "alice"},
                "html_url": f"https://github.com/octo/repo/issues/{number}",
                "created_at": "2026-01-01T00:00:00Z",
            }
        if "/contents/" in endpoint:
            if contributing and endpoint.endswith("/CONTRIBUTING.md"):
                text = base64.b64encode(b"# Contributing\nBe kind, write tests.").decode()
                return {"content": text}
            raise checks.NotFoundError(endpoint)
        if endpoint.startswith("repos/octo/repo/pulls"):
            return []
        if endpoint.startswith("repos/octo/repo/commits"):
            return []
        if endpoint == "repos/octo/repo":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"pushed_at": now, "stargazers_count": 4}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return fake


@pytest.fixture
def faked(monkeypatch):
    items = [search_item(1, 1), search_item(2, 2), search_item(3, 40)]
    states = {1: "go", 2: "taken", 3: "go"}
    comments_map = {1: [comment("maintainer-bob")]}
    monkeypatch.setattr(checks, "gh_api", make_fake(items, states, comments_map))


def test_discover_ranks_verified_candidates(faked, capsys):
    assert main(["--discover", "--label", "good first issue"]) == 0
    lines = capsys.readouterr().out.strip("\n").splitlines()
    assert len(lines) == 2  # #2 is TAKEN, filtered out
    assert lines[0].startswith("  6  octo/repo#1")
    assert "maintainer replied" in lines[0]
    assert "updated 1d ago" in lines[0]
    assert lines[1].startswith("  1  octo/repo#3")


def test_discover_searches_all_labels_by_default(faked, capsys):
    assert main(["--discover"]) == 0
    lines = capsys.readouterr().out.strip("\n").splitlines()
    assert len(lines) == 2  # deduped across the label searches


def test_discover_min_contributors_filters_everything(faked, capsys):
    assert main(["--discover", "--label", "good first issue", "--min-contributors", "100"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == ""
    assert "no candidates passed verification" in out.err


def test_discover_json(faked, capsys):
    assert main(["--discover", "--label", "good first issue", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(r["target"], r["score"]) for r in data] == [
        ("octo/repo#1", 6),
        ("octo/repo#3", 1),
    ]
    assert all(r["verdict"] == "GO" for r in data)
    assert all(r["friendly_labels"] == [] for r in data)
    assert all(r["welcoming"] == [] for r in data)


def test_discover_marks_friendly_and_welcoming(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(
        items,
        {1: "go"},
        {},
        labels_map={1: ["good first issue", "bug"]},
        contributing=True,
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue"]) == 0
    line = capsys.readouterr().out.strip("\n").splitlines()[0]
    assert "[good first issue]" in line
    assert "[has CONTRIBUTING.md]" in line
    assert "[bug]" not in line


def test_discover_json_carries_friendly_and_welcoming(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(
        items,
        {1: "go"},
        {},
        labels_map={1: ["Help Wanted"]},
        contributing=True,
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["friendly_labels"] == ["Help Wanted"]
    assert data[0]["welcoming"] == ["has CONTRIBUTING.md"]


def test_discover_tie_break_prefers_recent(monkeypatch):
    # Two equal-score candidates: the more recently updated one ranks first.
    items = [search_item(1, 1), search_item(2, 2)]
    monkeypatch.setattr(checks, "gh_api", make_fake(items, {1: "go", 2: "go"}, {}))
    results = discover.discover(discover.DiscoverOptions(label="good first issue"))
    assert [r["target"] for r in results] == ["octo/repo#1", "octo/repo#2"]
    assert results[0]["score"] == results[1]["score"]


def test_discover_uses_single_ord_label_search(monkeypatch, capsys):
    # One search/issues call covers every label: comma-separated values in a
    # single label: qualifier are OR'd by GitHub. jobs=1 keeps the ranking
    # deterministic (parallel completion order is arbitrary).
    items = [search_item(n, 1) for n in range(1, 6)]
    for item in items:
        item["updated_at"] = "2026-09-25T00:00:00Z"  # identical: ties keep pool order
    base = make_fake([], {}, {})
    search_calls = []

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            q = (params or {}).get("q", "")
            search_calls.append(q)
            return {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    searched = []
    # limit=50 returns the whole verify pool.
    results = discover.discover(
        discover.DiscoverOptions(jobs=1, limit=50, on_searched=searched.append)
    )
    targets = [r["target"] for r in results]
    assert len(search_calls) == 1
    for lab in discover.SEARCH_LABELS:
        assert f'"{lab}"' in search_calls[0]
    assert len(targets) <= discover.VERIFY_POOL
    assert "octo/repo#1" in targets
    assert searched == [[(", ".join(discover.SEARCH_LABELS), 5)]]

    # The CLI reports the combined search on stderr.
    assert main(["--discover"]) == 0
    err = capsys.readouterr().err
    assert "searched: good first issue, good-first-issue, beginner friendly, help wanted (5)" in err


def test_discover_with_targets_is_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--discover", "octo/repo#1"])
    assert exc.value.code == 3


def test_build_query():
    q = discover.build_query("good first issue", language="python", updated_after="2026-08-26")
    assert q == (
        'is:open is:issue no:assignee label:"good first issue" updated:>=2026-08-26 language:python'
    )
    assert discover.build_query("help wanted") == 'is:open is:issue no:assignee label:"help wanted"'
    assert discover.build_query(["good first issue", "help wanted"]) == (
        'is:open is:issue no:assignee label:"good first issue","help wanted"'
    )


def test_maintainer_engaged():
    issue = {"author": "alice"}
    assert discover.maintainer_engaged(issue, [comment("maintainer-bob")]) is True
    assert discover.maintainer_engaged(issue, [comment("alice")]) is False
    assert discover.maintainer_engaged(issue, [comment("some[bot]")]) is False
    assert discover.maintainer_engaged(issue, []) is False


def test_maintainer_engaged_uses_author_association():
    issue = {"author": "alice"}
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert (
            discover.maintainer_engaged(issue, [comment("pat", author_association=assoc)]) is True
        )
    for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", None):
        assert (
            discover.maintainer_engaged(issue, [comment("pat", author_association=assoc)]) is False
        )


def test_maintainer_engaged_ignores_me():
    issue = {"author": "alice"}
    mine = [comment("me", author_association="MEMBER")]
    assert discover.maintainer_engaged(issue, mine, me="me") is False
    assert discover.maintainer_engaged(issue, mine) is True
    assert (
        discover.maintainer_engaged(
            issue, mine + [comment("owner-amy", author_association="OWNER")], me="me"
        )
        is True
    )


def test_discover_me_comment_not_counted_as_maintainer(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(items, {1: "go"}, {1: [comment("me", author_association="MEMBER")]})
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue", "--me", "me"]) == 0
    line = capsys.readouterr().out.strip("\n").splitlines()[0]
    assert "maintainer replied" not in line


def test_discover_all_errors_reported_distinctly(monkeypatch, capsys):
    items = [search_item(1, 1), search_item(2, 2)]
    base = make_fake(items, {1: "go", 2: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 2, "incomplete_results": False, "items": items}
        if "/issues/" in endpoint:
            raise checks.TakenError("network down")
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue"]) == 3
    err = capsys.readouterr().err
    assert "no candidates passed verification: all 2 errored" in err
    assert "gh auth status" in err


def test_discover_partial_errors_reported_with_counts(monkeypatch, capsys):
    items = [search_item(1, 1), search_item(2, 2)]
    base = make_fake(items, {1: "taken", 2: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 2, "incomplete_results": False, "items": items}
        if endpoint == "repos/octo/repo/issues/2":
            raise checks.TakenError("network down")
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue"]) == 0
    err = capsys.readouterr().err
    assert "no candidates passed verification (1 of 2 candidates failed with errors)" in err


def test_discover_results_carry_error_stats(monkeypatch):
    items = [search_item(1, 1)]

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 1, "incomplete_results": False, "items": items}
        raise checks.TakenError("network down")

    monkeypatch.setattr(checks, "gh_api", fake)
    results = discover.discover(discover.DiscoverOptions(label="good first issue", jobs=1))
    assert results == []
    assert results.errors == 1
    assert results.total == 1


def test_discover_comments_fetch_failure_is_per_candidate_error(monkeypatch):
    """A failing comments fetch for one candidate must not abort the run."""
    items = [search_item(1, 1), search_item(2, 2)]
    monkeypatch.setattr(checks, "gh_api", make_fake(items, {1: "go", 2: "go"}, {}))
    real_fetch_comments = checks.fetch_comments
    calls = []

    def flaky_fetch_comments(owner, repo, number):
        calls.append(number)
        # Fail the claimant-scan comments fetch for issue 1. Comment pages
        # are fetched once and reused for engagement scoring, so this is
        # the only comments fetch; the failure must surface as a
        # per-candidate error, not abort the run.
        if number == 1 and calls.count(1) == 1:
            raise checks.TakenError("comments endpoint 500")
        return real_fetch_comments(owner, repo, number)

    monkeypatch.setattr(checks, "fetch_comments", flaky_fetch_comments)
    results = discover.discover(discover.DiscoverOptions(label="good first issue", jobs=1))
    assert [r["target"] for r in results] == ["octo/repo#2"]
    assert results.errors == 1
    assert results.total == 2


def test_repo_of():
    assert discover.repo_of({"repository_url": "https://api.github.com/repos/octo/repo"}) == (
        "octo",
        "repo",
    )
    assert discover.repo_of({}) is None


def test_search_issues_rejects_unexpected(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", lambda endpoint, params=None: {"items": "nope"})
    with pytest.raises(checks.TakenError):
        checks.search_issues("is:open")


def test_score_candidate_explains(monkeypatch):
    findings = {"repo_health": {"pushed_at": "2026-09-26"}}
    monkeypatch.setattr(discover, "_days_ago", lambda ts: 1)
    points, why = discover.score_candidate(findings, "2026-09-25T00:00:00Z", True)
    assert points == 6
    assert why == ["maintainer replied", "updated 1d ago", "repo pushed 1d ago"]


def test_discover_parallel_matches_sequential(faked):
    seq = discover.discover(discover.DiscoverOptions(label="good first issue", jobs=1))
    par = discover.discover(discover.DiscoverOptions(label="good first issue", jobs=8))
    assert [(r["target"], r["score"]) for r in par] == [(r["target"], r["score"]) for r in seq]
    assert len(par) == 2


def test_discover_progress_callback(faked):
    calls = []

    def track(done, total):
        calls.append((done, total))

    results = discover.discover(
        discover.DiscoverOptions(label="good first issue", jobs=4, on_progress=track)
    )
    assert results  # sanity: the fake still yields candidates
    total = calls[0][1]
    assert total == 3  # three candidates enter the pool
    assert calls[0] == (0, total)
    dones = [done for done, _ in calls[1:]]
    assert sorted(dones) == [1, 2, 3]
    assert all(t == total for _, t in calls)


def test_discover_jobs_flag_rejected_when_zero(faked, capsys):
    assert main(["--discover", "--label", "good first issue", "--jobs", "0"]) == 3
    assert "--jobs must be at least 1" in capsys.readouterr().err


def test_discover_negative_limit_yields_empty_not_truncated(monkeypatch):
    """A negative limit must not slice off the top candidate (ranked[:-1])."""
    items = [search_item(1, 1), search_item(2, 2)]
    monkeypatch.setattr(checks, "gh_api", make_fake(items, {1: "go", 2: "go"}, {}))
    results = discover.discover(
        discover.DiscoverOptions(label="good first issue", limit=-1, jobs=1)
    )
    assert results == []
    assert results.total == 2


def test_discover_cli_rejects_negative_limit(capsys):
    # A negative --limit used to exit 0 with "no candidates" (issue #339).
    with pytest.raises(SystemExit) as exc_info:
        main(["--discover", "--label", "good first issue", "--limit", "-1"])
    assert exc_info.value.code == 3
    assert "argument --limit: must be non-negative" in capsys.readouterr().err


def test_maintainer_engaged_ignores_me_case_insensitively():
    issue = {"author": "alice"}
    mine = [comment("RogueAlg0", author_association="MEMBER")]
    assert discover.maintainer_engaged(issue, mine, me="roguealg0") is False
    assert discover.maintainer_engaged(issue, mine, me="ROGUEALG0") is False
    assert discover.maintainer_engaged(issue, mine, me="someone-else") is True


def test_discover_me_comment_case_insensitive(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(items, {1: "go"}, {1: [comment("RogueAlg0", author_association="MEMBER")]})
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue", "--me", "roguealg0"]) == 0
    line = capsys.readouterr().out.strip("\n").splitlines()[0]
    assert "maintainer replied" not in line


def test_discover_fallback_keeps_partial_candidates(monkeypatch, capsys):
    # Combined search dies; one label's fallback search dies too. The run
    # must keep the candidates from the surviving labels and report the
    # failure, not abort with zero candidates. jobs=1 keeps the ranking
    # deterministic (parallel completion order is arbitrary).
    items = [search_item(1, 1), search_item(2, 2)]
    for item in items:
        item["updated_at"] = "2026-09-25T00:00:00Z"  # identical: ties keep pool order
    base = make_fake([], {}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            q = (params or {}).get("q", "")
            if '","' in q:
                raise checks.TakenError("secondary rate limit")
            if '"good first issue"' in q:
                raise checks.TakenError("boom")
            return {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    searched = []
    results = discover.discover(
        discover.DiscoverOptions(jobs=1, limit=50, on_searched=searched.append)
    )
    targets = [r["target"] for r in results]
    assert "octo/repo#1" in targets
    assert "octo/repo#2" in targets
    # the failed label is reported with its error ...
    assert results.search_errors == [("good first issue", "boom")]
    # ... and on_searched carries one entry per label that succeeded.
    assert len(searched) == 1
    assert sorted(lab for lab, _ in searched[0]) == sorted(
        lab for lab in discover.SEARCH_LABELS if lab != "good first issue"
    )


def test_discover_fallback_reports_partial_results_on_cli(monkeypatch, capsys):
    # The CLI must say the results are partial so a human (or a script
    # reading stderr) does not mistake them for a full run.
    items = [search_item(1, 1)]
    base = make_fake([], {}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            q = (params or {}).get("q", "")
            if '","' in q or '"help wanted"' in q:
                raise checks.TakenError("boom")
            return {"total_count": len(items), "incomplete_results": False, "items": items}
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--no-progress"]) == 0
    err = capsys.readouterr().err
    assert "partial results" in err
    assert '"help wanted"' in err


def test_discover_raises_when_every_label_search_fails(monkeypatch):
    # Total failure must stay an error, never a silent empty success.
    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            raise checks.TakenError("boom")
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(checks, "gh_api", fake)
    options = discover.DiscoverOptions(jobs=1)
    with pytest.raises(checks.TakenError, match="boom"):
        discover.discover(options)


def test_discover_combined_search_success_reports_no_search_errors(monkeypatch):
    # No behavior change on the happy path: one search call, no errors.
    items = [search_item(1, 1)]
    base = make_fake([], {}, {})
    search_calls = []

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            search_calls.append((params or {}).get("q", ""))
            return {"total_count": len(items), "incomplete_results": False, "items": items}
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    results = discover.discover(discover.DiscoverOptions(jobs=1, limit=50))
    assert len(search_calls) == 1
    assert results.search_errors == []
    assert [r["target"] for r in results] == ["octo/repo#1"]


def full_search_item(number, days_ago):
    """A search item carrying every field check_issue() extracts.

    Mirrors make_fake's issue-endpoint response for a "go" issue, so the
    payload path and the re-fetch path must produce identical facts.
    """
    item = search_item(number, days_ago)
    item.update(
        {
            "state": "open",
            "labels": [],
            "assignees": [],
            "comments": 0,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    return item


def test_discover_skips_issue_get_with_complete_search_item(monkeypatch):
    """A complete search item must skip the per-issue GET (issue #153)."""
    items = [full_search_item(1, 1)]
    issue_gets = []
    base = make_fake(items, {1: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues/1":
            issue_gets.append(endpoint)
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    results = discover.discover(discover.DiscoverOptions(label="good first issue", jobs=1))
    assert [r["target"] for r in results] == ["octo/repo#1"]
    assert issue_gets == []  # fields came from the search item, not a GET


def test_payload_issue_facts_match_refetch(monkeypatch):
    """Payload-derived facts must equal the re-fetched facts."""
    items = [full_search_item(1, 1)]
    monkeypatch.setattr(checks, "gh_api", make_fake(items, {1: "go"}, {}))
    assert checks.check_issue("octo", "repo", 1, payload=items[0]) == checks.check_issue(
        "octo", "repo", 1
    )


def test_incomplete_payload_falls_back_to_get(monkeypatch):
    """A search item missing a needed field must take the plain GET path."""
    items = [search_item(1, 1)]  # sparse: no state/labels/assignees/comments
    issue_gets = []
    base = make_fake(items, {1: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues/1":
            issue_gets.append(endpoint)
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1, payload=items[0])
    assert issue_gets == ["repos/octo/repo/issues/1"]  # exactly one GET
    assert findings["issue"]["state"] == "open"


def test_plain_run_checks_still_fetches_once(monkeypatch):
    """The no-payload path (plain CLI) must be unchanged: exactly one GET."""
    items = [search_item(1, 1)]
    issue_gets = []
    base = make_fake(items, {1: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues/1":
            issue_gets.append(endpoint)
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    assert issue_gets == ["repos/octo/repo/issues/1"]
    assert findings["issue"]["state"] == "open"


def test_payload_closed_state_verdicts_taken_without_get(monkeypatch):
    """The payload's own fields drive the verdict: closed -> TAKEN, zero GETs."""
    item = full_search_item(1, 1)
    item["state"] = "closed"

    def fake(endpoint, params=None):
        raise AssertionError(f"no API calls expected, got: {endpoint}")

    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1, payload=item)
    verdict, reasons = decide(findings)
    assert verdict == TAKEN
    assert any("closed" in reason for reason in reasons)
