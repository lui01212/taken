"""Tests for parallel scan/batch checks (issue #212).

run_batch (CLI) and scan_repo (MCP) verify targets through a worker pool
sized by the budget tier instead of one at a time. The anonymous tier stays
sequential (1 worker); the authenticated tier uses 8. Output order and
verdicts must be identical to the sequential run.
"""

import argparse
import threading
import time

import pytest

from taken import budget, checks, cli, graphql, mcp_server


@pytest.fixture(autouse=True)
def _reset_budget():
    budget.reset()
    yield
    budget.reset()


def _args(**kw):
    base = dict(
        me=None,
        json=False,
        graphql=False,
        persistent_session=False,
        rest=False,
        limit=20,
        label=None,
        verbose=False,
        debug=False,
        no_progress=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _mute_transport(monkeypatch):
    """Keep fetch_mode deterministic: never probe the ambient environment."""
    monkeypatch.setattr(graphql, "fetch_mode", lambda *a, **k: "rest")


# --- budget tier worker counts ------------------------------------------------


def test_batch_workers_anonymous_is_sequential():
    budget.activate(identity=None)
    assert budget.current().batch_workers == 1


def test_batch_workers_authenticated_is_bounded():
    budget.activate(identity="someone")
    assert budget.current().batch_workers == 8


# --- run_batch ----------------------------------------------------------------


def test_batch_checks_run_concurrently(monkeypatch, capsys):
    """Two checks must overlap: a barrier fails if they run one at a time."""
    budget.activate(identity="someone")
    _mute_transport(monkeypatch)
    barrier = threading.Barrier(2, timeout=15)

    def fake_check_one(owner, repo, number, me, mode="rest", payload=None, thresholds=None):
        barrier.wait()  # raises BrokenBarrierError if never overlapped
        return (f"{owner}/{repo}#{number}", "GO", ["reason"], {})

    monkeypatch.setattr(cli, "check_one", fake_check_one)
    assert cli.run_batch(["o/r#1", "o/r#2"], _args()) == 0
    out = capsys.readouterr().out
    assert "o/r#1" in out
    assert "o/r#2" in out


def test_batch_output_order_preserved(monkeypatch, capsys):
    """The first target prints first even when it finishes last."""
    budget.activate(identity="someone")
    _mute_transport(monkeypatch)

    def fake_check_one(owner, repo, number, me, mode="rest", payload=None, thresholds=None):
        if number == 1:
            time.sleep(2)
        return (f"{owner}/{repo}#{number}", "GO", ["reason"], {})

    monkeypatch.setattr(cli, "check_one", fake_check_one)
    assert cli.run_batch(["o/r#1", "o/r#2"], _args()) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("GO      o/r#1")
    assert lines[1].startswith("GO      o/r#2")


def test_batch_verdict_parity_sequential_vs_parallel(monkeypatch, capsys):
    """Same targets, same output, whether 1 worker or 8 (plus a parse error)."""

    def fake_check_one(owner, repo, number, me, mode="rest", payload=None, thresholds=None):
        verdict = "TAKEN" if number == 1 else "GO"
        return (f"o/r#{number}", verdict, [f"reason {number}"], {})

    monkeypatch.setattr(cli, "check_one", fake_check_one)
    _mute_transport(monkeypatch)
    targets = ["o/r#1", "o/r#2", "bogus"]

    budget.activate(identity=None)  # anonymous: 1 worker (sequential)
    assert cli.run_batch(targets, _args()) == 3
    seq = capsys.readouterr()

    budget.reset()
    budget.activate(identity="someone")  # authenticated: 8 workers
    assert cli.run_batch(targets, _args()) == 3
    par = capsys.readouterr()

    assert seq.out == par.out
    assert seq.err == par.err
    assert "could not parse 'bogus'" in seq.err


def test_batch_uses_tier_worker_count(monkeypatch):
    """The executor is constructed with the tier's worker count."""
    import concurrent.futures

    seen = {}
    real = concurrent.futures.ThreadPoolExecutor

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", spy)

    def fake(o, r, n, me, mode="rest", payload=None, thresholds=None):
        return (f"{o}/{r}#{n}", "GO", [], {})

    monkeypatch.setattr(cli, "check_one", fake)
    _mute_transport(monkeypatch)

    budget.activate(identity=None)
    cli.run_batch(["o/r#1"], _args())
    assert seen["max_workers"] == 1

    budget.reset()
    budget.activate(identity="someone")
    cli.run_batch(["o/r#1"], _args())
    assert seen["max_workers"] == 8


def test_batch_repo_scan_checks_issues_concurrently(monkeypatch, capsys):
    """A bare owner/repo target fans its listed issues out to the pool."""
    budget.activate(identity="someone")
    _mute_transport(monkeypatch)
    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **k: [{"number": 1}, {"number": 2}])
    barrier = threading.Barrier(2, timeout=15)

    def fake_check_one(owner, repo, number, me, mode="rest", payload=None, thresholds=None):
        barrier.wait()
        return (f"{owner}/{repo}#{number}", "GO", ["reason"], {})

    monkeypatch.setattr(cli, "check_one", fake_check_one)
    assert cli.run_batch(["o/r"], _args()) == 0
    out = capsys.readouterr().out
    assert "o/r#1" in out
    assert "o/r#2" in out


def test_batch_check_error_does_not_stop_others(monkeypatch, capsys):
    """A TakenError in one check is reported; the other still completes."""
    budget.activate(identity="someone")
    _mute_transport(monkeypatch)

    def fake_check_one(owner, repo, number, me, mode="rest", payload=None, thresholds=None):
        if number == 1:
            raise checks.TakenError("simulated failure")
        return (f"{owner}/{repo}#{number}", "GO", ["reason"], {})

    monkeypatch.setattr(cli, "check_one", fake_check_one)
    assert cli.run_batch(["o/r#1", "o/r#2"], _args()) == 3
    out = capsys.readouterr()
    assert "error: o/r#1: simulated failure" in out.err
    assert "GO      o/r#2" in out.out


# --- scan_repo (MCP) ------------------------------------------------------------


def _payload(number, verdict="GO"):
    return {
        "target": f"o/r#{number}",
        "verdict": verdict,
        "reasons": ["reason"],
        "findings": {},
        "friendly_labels": [],
        "welcoming": [],
    }


def test_scan_repo_checks_run_concurrently(monkeypatch):
    budget.activate(identity="someone")
    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **k: [{"number": 1}, {"number": 2}])
    monkeypatch.setattr(graphql, "fetch_mode", lambda *a, **k: "rest")
    barrier = threading.Barrier(2, timeout=15)

    def fake_check_one(owner, repo, number, me=None, mode=None, payload=None, thresholds=None):
        barrier.wait()
        return _payload(number)

    monkeypatch.setattr(mcp_server, "_check_one", fake_check_one)
    out = mcp_server.scan_repo("o", "r")
    assert out["summary"] == {"GO": 2, "CAUTION": 0, "TAKEN": 0, "errors": 0}
    assert out["recommendations"] == ["o/r#1", "o/r#2"]


def test_scan_repo_result_order_matches_sequential(monkeypatch):
    """Same-verdict results keep input order even when checks finish late."""
    budget.activate(identity="someone")
    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **k: [{"number": 1}, {"number": 2}])
    monkeypatch.setattr(graphql, "fetch_mode", lambda *a, **k: "rest")

    def fake_check_one(owner, repo, number, me=None, mode=None, payload=None, thresholds=None):
        if number == 1:
            time.sleep(2)
        return _payload(number)

    monkeypatch.setattr(mcp_server, "_check_one", fake_check_one)
    out = mcp_server.scan_repo("o", "r")
    # Stable verdict-rank sort over input-ordered results: o/r#1 first.
    assert [r["target"] for r in out["results"]] == ["o/r#1", "o/r#2"]


def test_scan_repo_error_entry_keeps_its_place(monkeypatch):
    budget.activate(identity="someone")
    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **k: [{"number": 1}, {"number": 2}])
    monkeypatch.setattr(graphql, "fetch_mode", lambda *a, **k: "rest")

    def fake_check_one(owner, repo, number, me=None, mode=None, payload=None, thresholds=None):
        if number == 1:
            raise checks.TakenError("simulated failure")
        return _payload(number)

    monkeypatch.setattr(mcp_server, "_check_one", fake_check_one)
    out = mcp_server.scan_repo("o", "r")
    assert out["summary"] == {"GO": 1, "CAUTION": 0, "TAKEN": 0, "errors": 1}
    by_target = {r["target"]: r for r in out["results"]}
    assert by_target["o/r#1"]["error"] == "simulated failure"
    assert by_target["o/r#2"]["verdict"] == "GO"
