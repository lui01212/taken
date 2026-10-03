"""Regression tests for issue #133.

`--graphql` / `--persistent-session` must reach `--discover` candidate
verification instead of being silently ignored. These tests fail on the
pre-fix code: `discover.discover` did not accept `mode`, and
`run_discover` never passed it.
"""

import pytest

from taken import checks, discover, graphql
from taken.cli import main


def _go_findings():
    return {
        "target": "o/r#1",
        "issue": {
            "state": "open",
            "title": "issue 1",
            "labels": [],
            "assignees": [],
            "comment_count": 0,
            "author": "alice",
            "url": "https://github.com/o/r/issues/1",
            "created_at": "2026-01-01T00:00:00Z",
        },
        "linked_prs": [],
        "claimants": [],
        "ai_policy": {"verdict": "none-found", "snippet": "", "source": None},
        "repo_health": {
            "pushed_at": "2026-09-26",
            "pushed_recently": True,
            "recent_merges": 3,
            "contributors": 5,
            "contributors_window_days": 90,
        },
    }


@pytest.fixture
def stubbed(monkeypatch):
    """One fake candidate; GraphQL fetch spied, REST fetch made to explode."""
    calls = []

    def fake_collect(labels, language, updated_after):
        item = {"number": 1, "updated_at": "2026-09-26T00:00:00Z"}
        return ([("o", "r", 1, item)], [("good first issue", 1)], [])

    def fake_graphql(owner, repo, number, me=None, mode="graphql", session=None, thresholds=None):
        calls.append(
            {"owner": owner, "repo": repo, "number": number, "mode": mode, "session": session}
        )
        return _go_findings()

    def fake_rest(*args, **kwargs):
        raise AssertionError("REST fetch used while a GraphQL mode was requested")

    monkeypatch.setattr(discover, "_collect_candidates", fake_collect)
    monkeypatch.setattr(graphql, "run_checks_graphql", fake_graphql)
    monkeypatch.setattr(checks, "run_checks", fake_rest)
    monkeypatch.setattr(checks, "fetch_comments", lambda *args, **kwargs: ([], False))
    return calls


def test_discover_graphql_mode_reaches_verification(stubbed, capsys):
    results = discover.discover(discover.DiscoverOptions(mode="graphql", jobs=1))
    assert [r["target"] for r in results] == ["o/r#1"]
    assert len(stubbed) == 1
    assert stubbed[0]["mode"] == "graphql"
    assert stubbed[0]["session"] is None


def test_discover_persistent_mode_uses_thread_local_session(stubbed):
    item = {"number": 1, "updated_at": "2026-09-26T00:00:00Z"}
    entry1, err1 = discover._verify_candidate("o", "r", 1, item, 0, None, mode="persistent")
    entry2, err2 = discover._verify_candidate("o", "r", 1, item, 0, None, mode="persistent")
    assert err1 is None
    assert err2 is None
    assert entry1["target"] == "o/r#1"
    sessions = [c["session"] for c in stubbed]
    assert len(sessions) == 2
    assert all(isinstance(s, graphql.PersistentGraphQLSession) for s in sessions)
    # Same thread reuses its session instead of opening a new connection.
    assert sessions[0] is sessions[1]


def _record_discover(monkeypatch):
    seen = {}

    def fake_discover(options=None):
        seen["options"] = options
        return discover.DiscoverResults()

    monkeypatch.setattr(discover, "discover", fake_discover)
    return seen


def test_cli_discover_graphql_flag_plumbed(monkeypatch, capsys):
    seen = _record_discover(monkeypatch)
    assert main(["--discover", "--graphql", "--label", "good first issue"]) == 0
    assert seen["options"].mode == "graphql"


def test_cli_discover_persistent_flag_plumbed(monkeypatch, capsys):
    seen = _record_discover(monkeypatch)
    assert main(["--discover", "--persistent-session", "--label", "good first issue"]) == 0
    assert seen["options"].mode == "persistent"


def test_cli_discover_defaults_to_rest(monkeypatch, capsys):
    seen = _record_discover(monkeypatch)
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    assert main(["--discover", "--label", "good first issue"]) == 0
    assert seen["options"].mode == "rest"


def test_cli_discover_defaults_to_graphql_when_authenticated(monkeypatch, capsys):
    seen = _record_discover(monkeypatch)
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")
    assert main(["--discover", "--label", "good first issue"]) == 0
    assert seen["options"].mode == "graphql"


def test_cli_discover_rest_override_when_authenticated(monkeypatch, capsys):
    seen = _record_discover(monkeypatch)
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")
    assert main(["--discover", "--rest", "--label", "good first issue"]) == 0
    assert seen["options"].mode == "rest"
