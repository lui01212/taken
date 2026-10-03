"""Tests for the opt-in GraphQL fetch paths (taken/graphql.py)."""

import argparse
import http.client
import json
import os
import subprocess
from unittest import mock

import pytest

from taken import checks, graphql
from taken.verdict import decide


def _issue_node(**over):
    node = {
        "state": "OPEN",
        "title": "Some issue",
        "url": "https://github.com/o/r/issues/1",
        "createdAt": "2026-09-01T00:00:00Z",
        "author": {"login": "someone"},
        "assignees": {"nodes": [{"login": "dev"}]},
        "labels": {"nodes": [{"name": "good first issue"}]},
        "comments": {
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "author": {"login": "volunteer"},
                    "body": "I would like to work on this",
                    "createdAt": "2026-09-02T00:00:00Z",
                    "url": "https://github.com/o/r/issues/1#c1",
                }
            ],
        },
        "timelineItems": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "__typename": "CrossReferencedEvent",
                    "source": {
                        "__typename": "PullRequest",
                        "number": 7,
                        "title": "Fix thing",
                        "state": "OPEN",
                        "mergedAt": None,
                        "url": "https://github.com/o/r/pull/7",
                        "author": {"login": "dev"},
                        "repository": {"nameWithOwner": "o/r"},
                    },
                }
            ],
        },
    }
    node.update(over)
    return node


def _repo_node(**over):
    repo = {
        "pushedAt": "2026-09-26T00:00:00Z",
        "issue": _issue_node(),
        "ai1": {"text": "This project does not accept AI-generated contributions."},
        "ai2": None,
        "ai3": None,
        "ai4": None,
        "mergedPRs": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"mergedAt": "2026-09-20T00:00:00Z"}],
        },
        "defaultBranchRef": {
            "target": {
                "history": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {"author": {"user": {"login": "dev"}, "email": "d@x"}},
                        {"author": {"user": {"login": "dev"}, "email": "d@x"}},
                        {"author": {"user": None, "email": "anon@x"}},
                    ],
                }
            }
        },
    }
    repo.update(over)
    return repo


def _payload(repo=None):
    return {
        "data": {
            "repository": _repo_node() if repo is None else repo,
            "rateLimit": {"limit": 5000, "cost": 1},
        }
    }


# --- mode resolution -------------------------------------------------------


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """GraphQL transport tests must not touch the real cache."""
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)


def _args(**kw):
    args = argparse.Namespace(graphql=False, persistent_session=False, rest=False)
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def _logged_in(monkeypatch):
    """Pretend the invoker is authenticated to GitHub."""
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")


def _anonymous(monkeypatch):
    """Pretend the invoker has no GitHub credentials."""
    monkeypatch.setattr(checks, "_github_identity", lambda: None)


def test_fetch_mode_rest_when_anonymous(monkeypatch):
    _anonymous(monkeypatch)
    assert graphql.fetch_mode(_args()) == "rest"
    assert graphql.fetch_mode(None) == "rest"


def test_fetch_mode_graphql_when_authenticated(monkeypatch):
    _logged_in(monkeypatch)
    assert graphql.fetch_mode(_args()) == "graphql"
    assert graphql.fetch_mode(None) == "graphql"


def test_fetch_mode_flag_and_env(monkeypatch):
    _anonymous(monkeypatch)
    assert graphql.fetch_mode(_args(graphql=True)) == "graphql"
    with mock.patch.dict(os.environ, {"TAKEN_GRAPHQL": "1"}):
        assert graphql.fetch_mode(_args()) == "graphql"


def test_fetch_mode_rest_escape_hatch(monkeypatch):
    _logged_in(monkeypatch)
    # --rest beats the authenticated default and beats --graphql, so there
    # is always a way to force the REST path.
    assert graphql.fetch_mode(_args(rest=True)) == "rest"
    assert graphql.fetch_mode(_args(graphql=True, rest=True)) == "rest"
    with mock.patch.dict(os.environ, {"TAKEN_REST": "1"}):
        assert graphql.fetch_mode(_args()) == "rest"
        assert graphql.fetch_mode(_args(graphql=True)) == "rest"


def test_fetch_mode_persistent_wins(monkeypatch):
    _logged_in(monkeypatch)
    assert graphql.fetch_mode(_args(persistent_session=True)) == "persistent"
    with mock.patch.dict(os.environ, {"TAKEN_PERSISTENT_SESSION": "1"}):
        assert graphql.fetch_mode(_args(graphql=True)) == "persistent"
    # Persistent still wins over the rest escape hatch: it is the most
    # explicit opt-in.
    with mock.patch.dict(os.environ, {"TAKEN_PERSISTENT_SESSION": "1", "TAKEN_REST": "1"}):
        assert graphql.fetch_mode(_args()) == "persistent"


# --- error handling --------------------------------------------------------


def test_raise_for_errors_none():
    graphql._raise_for_errors({"data": {}}, "x")  # no raise


def test_raise_for_errors_names_type():
    with pytest.raises(checks.TakenError, match="NOT_FOUND"):
        graphql._raise_for_errors(
            {"errors": [{"type": "NOT_FOUND", "message": "nope"}], "data": None}, "x"
        )


def test_raise_for_errors_partial_data_fails_closed():
    with pytest.raises(checks.TakenError):
        graphql._raise_for_errors(
            {"errors": [{"type": "FORBIDDEN", "message": "x"}], "data": {"repository": {}}},
            "x",
        )


def test_raise_for_errors_rate_limited():
    with pytest.raises(checks.RateLimitError):
        graphql._raise_for_errors(
            {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}], "data": None},
            "x",
        )


# --- subprocess transport (path B) -----------------------------------------


def _run_result(stdout, rc=0, stderr=""):
    proc = mock.Mock()
    proc.stdout = stdout
    proc.returncode = rc
    proc.stderr = stderr
    return proc


def test_graphql_via_gh_posts_query():
    payload = _payload()
    with mock.patch.object(subprocess, "run", return_value=_run_result(json.dumps(payload))) as run:
        out = graphql.graphql_via_gh("query Q { x }", {"number": 1})
    assert out == payload
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["gh", "api", "graphql"]
    assert "-f" in cmd
    assert any(a.startswith("query=") for a in cmd)


def test_graphql_via_gh_errors_fail_closed():
    bad = {"errors": [{"type": "NOT_FOUND", "message": "x"}]}
    with mock.patch.object(subprocess, "run", return_value=_run_result(json.dumps(bad))):
        with pytest.raises(checks.TakenError):
            graphql.graphql_via_gh("query Q { x }", {})


def test_graphql_via_gh_rate_limited_retries_then_raises():
    bad = {"errors": [{"type": "RATE_LIMITED", "message": "slow"}]}
    with mock.patch.object(subprocess, "run", return_value=_run_result(json.dumps(bad))):
        with mock.patch("time.sleep"):
            with pytest.raises(checks.RateLimitError):
                graphql.graphql_via_gh("query Q { x }", {})


def test_graphql_via_gh_missing_binary():
    with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(checks.TakenError, match="`gh` CLI"):
            graphql.graphql_via_gh("query Q { x }", {})


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("transport closed", "transport closed"), ("", "exit status 2")],
)
def test_graphql_via_gh_failure_without_stderr_has_a_diagnostic(stdout, expected):
    with mock.patch.object(subprocess, "run", return_value=_run_result(stdout, rc=2)):
        with pytest.raises(checks.TakenError, match=expected):
            graphql.graphql_via_gh("query Q { x }", {})


def test_graphql_via_gh_failure_prefers_stderr_over_stdout():
    result = _run_result("less useful stdout", rc=2, stderr="specific stderr")
    with mock.patch.object(subprocess, "run", return_value=result):
        with pytest.raises(checks.TakenError, match="specific stderr"):
            graphql.graphql_via_gh("query Q { x }", {})


# --- persistent session (path C) --------------------------------------------


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    instances = []

    def __init__(self, *args, **kwargs):
        self.requests = []
        self.response = _FakeResponse(200, json.dumps(_payload()).encode())
        _FakeConn.instances.append(self)

    def request(self, method, url, body=None, headers=None):
        self.requests.append((method, url, body, headers))

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel = (host, port)

    def getresponse(self):
        return self.response

    def close(self):
        pass


def test_persistent_session_reuses_connection_and_token():
    _FakeConn.instances.clear()
    provider = mock.Mock(return_value="sekret-token")
    with mock.patch.object(http.client, "HTTPSConnection", _FakeConn):
        sess = graphql.PersistentGraphQLSession(token_provider=provider)
        out1 = sess.query("query Q { x }", {"a": 1})
        out2 = sess.query("query Q { x }", {"a": 1})
    assert out1["data"]["repository"]["pushedAt"]
    assert out2["data"]["repository"]["pushedAt"]
    assert provider.call_count == 1  # token fetched once, held in memory
    assert len(_FakeConn.instances) == 1  # one keep-alive connection
    assert len(_FakeConn.instances[0].requests) == 2
    _method, _url, _body, headers = _FakeConn.instances[0].requests[0]
    assert headers["Authorization"] == "Bearer sekret-token"


def test_persistent_session_never_logs_token(capfd):
    _FakeConn.instances.clear()
    with mock.patch.object(http.client, "HTTPSConnection", _FakeConn):
        sess = graphql.PersistentGraphQLSession(token_provider=lambda: "sekret-token")
        sess.query("query Q { x }", {})
    out, err = capfd.readouterr()
    assert "sekret-token" not in out
    assert "sekret-token" not in err


def test_persistent_session_auth_token_failure():
    with mock.patch.object(graphql, "_gh_auth_token", side_effect=checks.TakenError("no gh")):
        sess = graphql.PersistentGraphQLSession()
        with pytest.raises(checks.TakenError):
            sess.query("query Q { x }", {})


def test_persistent_session_429_becomes_rate_limit_error():
    _FakeConn.instances.clear()

    class _Conn429(_FakeConn):
        def getresponse(self):
            return _FakeResponse(429, b"{}")

    with mock.patch.object(http.client, "HTTPSConnection", _Conn429):
        sess = graphql.PersistentGraphQLSession(token_provider=lambda: "t")
        with pytest.raises(checks.RateLimitError):
            sess.query("query Q { x }", {})


def test_persistent_session_dropped_connection_retries_once():
    """A proxy-closed idle connection reconnects and the query is retried."""
    _FakeConn.instances.clear()

    class _ConnFlaky(_FakeConn):
        calls = 0

        def getresponse(self):
            type(self).calls += 1
            if type(self).calls == 1:
                raise http.client.RemoteDisconnected("boom")
            return self.response

    with mock.patch.object(http.client, "HTTPSConnection", _ConnFlaky):
        sess = graphql.PersistentGraphQLSession(token_provider=lambda: "t")
        out = sess.query("query Q { x }", {})
        assert out["data"]["repository"]["pushedAt"]
        assert sess.calls == 2  # failed attempt + one retry


def test_persistent_session_persistent_failure_raises_taken_error():
    _FakeConn.instances.clear()

    class _ConnDead(_FakeConn):
        def getresponse(self):
            raise http.client.RemoteDisconnected("always")

    with mock.patch.object(http.client, "HTTPSConnection", _ConnDead):
        sess = graphql.PersistentGraphQLSession(token_provider=lambda: "t")
        with pytest.raises(checks.TakenError, match="connection failed"):
            sess.query("query Q { x }", {})


# --- findings mapping -------------------------------------------------------


def _run_with_fake_transport(monkeypatch, payload, mode="graphql"):
    if mode == "persistent":
        fake = mock.Mock()
        fake.query.return_value = payload
        monkeypatch.setattr(graphql, "get_session", lambda: fake)
    else:
        monkeypatch.setattr(graphql, "graphql_via_gh", lambda q, v: payload)
    return graphql.run_checks_graphql("o", "r", 1, mode=mode)


def test_findings_shape_matches_rest_contract(monkeypatch):
    findings = _run_with_fake_transport(monkeypatch, _payload())
    assert set(findings) == {
        "target",
        "issue",
        "linked_prs",
        "claimants",
        "thresholds",
        "ai_policy",
        "repo_health",
        "scan_truncated",
        "stages_skipped",
    }
    assert findings["thresholds"] == checks.default_thresholds()
    assert findings["stages_skipped"] == []
    assert set(findings["issue"]) == {
        "number",
        "state",
        "title",
        "labels",
        "assignees",
        "comment_count",
        "author",
        "url",
        "created_at",
    }
    assert findings["issue"]["state"] == "open"
    assert findings["issue"]["assignees"] == ["dev"]
    assert findings["issue"]["labels"] == ["good first issue"]
    assert findings["linked_prs"][0]["number"] == 7
    assert findings["linked_prs"][0]["state"] == "open"
    assert findings["claimants"][0]["author"] == "volunteer"
    assert findings["ai_policy"]["verdict"] == "ban"
    assert findings["ai_policy"]["source"] == "CONTRIBUTING.md"
    assert findings["repo_health"]["recent_merges"] == 1
    assert findings["repo_health"]["contributors"] == 2  # dev + anon@x


def test_verdict_matches_rest_findings(monkeypatch):
    findings = _run_with_fake_transport(monkeypatch, _payload())
    rest_findings = {
        "target": "o/r#1",
        "issue": {
            "number": 1,
            "state": "open",
            "title": "Some issue",
            "labels": ["good first issue"],
            "assignees": ["dev"],
            "comment_count": 1,
            "author": "someone",
            "url": "https://github.com/o/r/issues/1",
            "created_at": "2026-09-01T00:00:00Z",
        },
        "linked_prs": [
            {
                "number": 7,
                "title": "Fix thing",
                "state": "open",
                "merged": False,
                "author": "dev",
                "url": "https://github.com/o/r/pull/7",
                "updated_at": None,
                "idle_days": None,
                "age_label": "open PR #7, last activity date unknown",
            }
        ],
        "claimants": [
            {
                "author": "volunteer",
                "date": "2026-09-02",
                "url": "u",
                "pattern": "work on",
                "snippet": "I would like to work on this",
                "age_days": None,
                "days_since_claimant_activity": None,
                "age_label": "expressed interest date unknown",
            }
        ],
        "thresholds": checks.default_thresholds(),
        "ai_policy": {"verdict": "ban", "snippet": "", "source": "CONTRIBUTING.md"},
        "repo_health": {
            "pushed_at": "2026-09-26",
            "pushed_recently": True,
            "recent_merges": 1,
            "contributors": 2,
            "contributors_window_days": 90,
        },
    }
    assert decide(findings) == decide(rest_findings)


def test_merged_pr_state_normalizes_to_closed(monkeypatch):
    """GraphQL MERGED enum maps to REST's state "closed" + merged flag."""
    repo = _repo_node()
    src = repo["issue"]["timelineItems"]["nodes"][0]["source"]
    src["state"] = "MERGED"
    src["mergedAt"] = "2026-09-10T00:00:00Z"
    findings = _run_with_fake_transport(monkeypatch, _payload(repo))
    pr = findings["linked_prs"][0]
    assert pr["state"] == "closed"
    assert pr["merged"] is True


def test_missing_issue_raises_not_found(monkeypatch):
    repo = _repo_node(issue=None)
    payload = _payload(repo)
    with pytest.raises(checks.NotFoundError):
        _run_with_fake_transport(monkeypatch, payload)


def test_missing_repo_raises_not_found(monkeypatch):
    with pytest.raises(checks.NotFoundError):
        _run_with_fake_transport(monkeypatch, {"data": {"repository": None}})


def test_persistent_mode_routes_to_session(monkeypatch):
    findings = _run_with_fake_transport(monkeypatch, _payload(), mode="persistent")
    assert findings["issue"]["number"] == 1


def test_comment_pagination_follows_cursor(monkeypatch):
    page1 = _repo_node()
    page1["issue"]["comments"] = {
        "totalCount": 2,
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": page1["issue"]["comments"]["nodes"],
    }
    page2 = _repo_node()
    page2["issue"]["comments"] = {
        "totalCount": 2,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {
                "author": {"login": "second"},
                "body": "I'll take this on",
                "createdAt": "2026-09-03T00:00:00Z",
                "url": "https://github.com/o/r/issues/1#c2",
            }
        ],
    }
    calls = []

    def fake_fetch(q, v):
        calls.append(v.get("commentsAfter"))
        return {"data": {"repository": page2 if v.get("commentsAfter") else page1}}

    monkeypatch.setattr(graphql, "graphql_via_gh", fake_fetch)
    findings = graphql.run_checks_graphql("o", "r", 1, mode="graphql")
    assert calls == [None, "c1"]
    assert {c["author"] for c in findings["claimants"]} == {"volunteer", "second"}


def _repo_node_no_taken(**over):
    """Repo fixture with no TAKEN signals, so CAUTION reasons surface."""
    repo = _repo_node(**over)
    repo["issue"]["timelineItems"] = {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    }
    repo["issue"]["assignees"] = {"nodes": []}
    return repo


def test_label_pagination_follows_cursor(monkeypatch):
    page1 = _repo_node_no_taken()
    page1["issue"]["labels"] = {
        "totalCount": 3,
        "pageInfo": {"hasNextPage": True, "endCursor": "l1"},
        "nodes": [{"name": "good first issue"}, {"name": "bug"}],
    }
    page2 = _repo_node_no_taken()
    page2["issue"]["labels"] = {
        "totalCount": 3,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"name": "needs design"}],
    }
    calls = []

    def fake_fetch(q, v):
        calls.append(v.get("labelsAfter"))
        return {"data": {"repository": page2 if v.get("labelsAfter") else page1}}

    monkeypatch.setattr(graphql, "graphql_via_gh", fake_fetch)
    findings = graphql.run_checks_graphql("o", "r", 1, mode="graphql")
    assert calls == [None, "l1"]
    assert findings["issue"]["labels"] == ["good first issue", "bug", "needs design"]
    assert findings["scan_truncated"]["labels"] is False
    # The design label past the first page is CAUTION context that used to
    # be silently dropped.
    _verdict, reasons = decide(findings)
    assert any("needs design" in r for r in reasons)


def test_label_truncation_is_surfaced(monkeypatch):
    monkeypatch.setattr(graphql, "_MAX_LABEL_PAGES", 1)
    repo = _repo_node_no_taken()
    repo["issue"]["labels"] = {
        "totalCount": 250,
        "pageInfo": {"hasNextPage": True, "endCursor": "l1"},
        "nodes": [{"name": "good first issue"}],
    }
    findings = _run_with_fake_transport(monkeypatch, _payload(repo))
    assert findings["scan_truncated"]["labels"] is True
    _verdict, reasons = decide(findings)
    assert any("label scan hit the page cap" in r for r in reasons)


# --- CLI / MCP plumbing -----------------------------------------------------


def _minimal_findings():
    return {
        "target": "o/r#1",
        "issue": {
            "number": 1,
            "state": "open",
            "title": "t",
            "labels": [],
            "assignees": [],
            "comment_count": 0,
            "author": None,
            "url": "u",
            "created_at": "2026-09-01T00:00:00Z",
        },
        "linked_prs": [],
        "claimants": [],
        "ai_policy": {"verdict": "none-found", "snippet": "", "source": None},
        "repo_health": {
            "pushed_at": "2026-09-26",
            "pushed_recently": True,
            "recent_merges": 1,
            "contributors": 1,
            "contributors_window_days": 90,
        },
    }


def test_cli_check_one_routes_modes(monkeypatch):
    from taken import cli

    with mock.patch.object(graphql, "run_checks_graphql") as rg:
        rg.return_value = _minimal_findings()
        cli.check_one("o", "r", 1, None, mode="graphql")
        assert rg.call_count == 1
    with mock.patch.object(checks, "run_checks") as rc:
        rc.return_value = _minimal_findings()
        cli.check_one("o", "r", 1, None, mode="rest")
        assert rc.call_count == 1


def test_mcp_check_issue_graphql_param(monkeypatch):
    from taken import mcp_server

    with mock.patch.object(graphql, "run_checks_graphql") as rg:
        rg.return_value = _minimal_findings()
        payload = mcp_server.check_issue("o", "r", 1, graphql=True)
        assert rg.call_count == 1
        assert payload["target"] == "o/r#1"


def test_mcp_check_issue_persistent_param(monkeypatch):
    from taken import mcp_server

    with mock.patch.object(graphql, "run_checks_graphql") as rg:
        rg.return_value = _minimal_findings()
        mcp_server.check_issue("o", "r", 1, persistent_session=True)
        _, kwargs = rg.call_args
        assert kwargs["mode"] == "persistent"


# --- transport fallback ----------------------------------------------------


def _findings_with_transport(**over):
    findings = _minimal_findings()
    findings.update(over)
    return findings


def test_wrapper_rest_passthrough_sets_transport(monkeypatch):
    with mock.patch.object(checks, "run_checks") as rc:
        rc.return_value = _findings_with_transport()
        findings = graphql.run_checks_with_fallback("o", "r", 1, mode="rest")
        assert rc.call_count == 1
        assert findings["transport"] == "rest"
        assert "transport_fallback" not in findings


def test_wrapper_graphql_success_records_transport(monkeypatch):
    findings = _findings_with_transport()
    with mock.patch.object(graphql, "run_checks_graphql", return_value=findings):
        out = graphql.run_checks_with_fallback("o", "r", 1, mode="graphql")
        assert out["transport"] == "graphql"
        assert "transport_fallback" not in out


def test_wrapper_falls_back_to_rest_on_graphql_failure(monkeypatch):
    rest_findings = _findings_with_transport()
    with mock.patch.object(graphql, "run_checks_graphql", side_effect=checks.TakenError("boom")):
        with mock.patch.object(checks, "run_checks", return_value=rest_findings) as rc:
            out = graphql.run_checks_with_fallback("o", "r", 1, mode="graphql")
            assert rc.call_count == 1
            assert out["transport"] == "rest"
            assert "graphql" in out["transport_fallback"]
            assert "REST" in out["transport_fallback"]
            # The fallback verdict is the REST evidence's verdict: the
            # transport keys must not change what decide() concludes.
            assert decide(out)[0] == "GO"


def test_wrapper_does_not_fall_back_on_not_found(monkeypatch):
    with mock.patch.object(graphql, "run_checks_graphql", side_effect=checks.NotFoundError("gone")):
        with mock.patch.object(checks, "run_checks") as rc:
            with pytest.raises(checks.NotFoundError):
                graphql.run_checks_with_fallback("o", "r", 1, mode="graphql")
            assert rc.call_count == 0


def test_cli_check_one_records_fallback_in_human_output(monkeypatch, capsys):
    from taken import cli

    rest_findings = _findings_with_transport()
    rest_findings["transport_fallback"] = "graphql transport failed (boom); fell back to REST"
    with mock.patch.object(graphql, "run_checks_graphql", side_effect=checks.TakenError("boom")):
        with mock.patch.object(checks, "run_checks", return_value=rest_findings):
            args = argparse.Namespace(
                me=None, json=False, graphql=True, persistent_session=False, rest=False
            )
            cli.run_single("o/r#1", args)
    out = capsys.readouterr().out
    assert "fell back to REST" in out


# --- verdict parity --------------------------------------------------------


def _go_repo_node():
    """Fixture with no TAKEN signals: open issue, no PRs, no claimants."""
    repo = _repo_node_no_taken()
    issue = repo["issue"]
    issue["comments"] = {
        "totalCount": 1,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {
                "author": {"login": "maintainer"},
                "body": "Thanks for the report, we will look into it.",
                "createdAt": "2026-09-02T00:00:00Z",
                "url": "https://github.com/o/r/issues/1#c1",
            }
        ],
    }
    repo["ai1"] = None  # no CONTRIBUTING: no AI-policy signal
    return repo


def test_verdict_parity_on_go_candidate(monkeypatch):
    """REST findings vs GraphQL findings on the same GO-shaped evidence."""
    findings = _run_with_fake_transport(monkeypatch, _payload(_go_repo_node()))
    rest_findings = {
        "target": "o/r#1",
        "issue": {
            "number": 1,
            "state": "open",
            "title": "Some issue",
            "labels": ["good first issue"],
            "assignees": [],
            "comment_count": 1,
            "author": "someone",
            "url": "https://github.com/o/r/issues/1",
            "created_at": "2026-09-01T00:00:00Z",
        },
        "linked_prs": [],
        "claimants": [],
        "ai_policy": {"verdict": "none-found", "snippet": "", "source": None},
        "repo_health": {
            "pushed_at": "2026-09-26",
            "pushed_recently": True,
            "recent_merges": 1,
            "contributors": 2,
            "contributors_window_days": 90,
        },
        "scan_truncated": {"timeline": False, "comments": False, "labels": False},
        "stages_skipped": [],
    }
    assert decide(findings) == decide(rest_findings)
    assert decide(findings)[0] == "GO"


def test_gh_auth_token_timeout_becomes_taken_error():
    with mock.patch.object(
        subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["gh", "auth", "token"], 30),
    ):
        with pytest.raises(checks.TakenError, match="timed out"):
            graphql._gh_auth_token()


def test_gh_auth_token_oserror_becomes_taken_error():
    with mock.patch.object(subprocess, "run", side_effect=OSError("exec failed")):
        with pytest.raises(checks.TakenError, match="could not run"):
            graphql._gh_auth_token()


def test_gh_auth_token_missing_gh_still_taken_error():
    with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("gh")):
        with pytest.raises(checks.TakenError, match="not installed"):
            graphql._gh_auth_token()


def test_get_session_singleton_under_concurrency(monkeypatch):
    """Two threads racing first use must get one session, not two (#344)."""
    import threading
    import time

    monkeypatch.setattr(graphql, "_SESSION", None)
    constructed = []
    real = graphql.PersistentGraphQLSession

    def slow_ctor(*args, **kwargs):
        time.sleep(0.02)  # widen the race window; the lock must still serialize
        session = real(*args, **kwargs)
        constructed.append(session)
        return session

    monkeypatch.setattr(graphql, "PersistentGraphQLSession", slow_ctor)
    results = []

    def fetch_session():
        results.append(graphql.get_session())

    threads = [threading.Thread(target=fetch_session) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(constructed) == 1
    assert len(results) == 16
    assert all(result is results[0] for result in results)


def test_close_persistent_sessions_at_exit(monkeypatch):
    fake = mock.Mock()
    monkeypatch.setattr(graphql, "_SESSION", fake)
    graphql._close_persistent_sessions()
    fake.close.assert_called_once_with()
    assert graphql._SESSION is None


def test_close_persistent_sessions_at_exit_without_session(monkeypatch):
    monkeypatch.setattr(graphql, "_SESSION", None)
    graphql._close_persistent_sessions()  # must not raise
    assert graphql._SESSION is None


def test_close_persistent_sessions_at_exit_swallows_errors(monkeypatch):
    fake = mock.Mock()
    fake.close.side_effect = RuntimeError("shutdown chaos")
    monkeypatch.setattr(graphql, "_SESSION", fake)
    graphql._close_persistent_sessions()  # a traceback at exit would mask the real result
    assert graphql._SESSION is None
