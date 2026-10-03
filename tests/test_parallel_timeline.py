"""Tests for parallel linked-PR fetches in check_timeline (issue #216).

Linked PRs are independent fetches, so the authenticated tier fetches a
timeline page's PRs concurrently; the anonymous tier keeps the exact
sequential behavior. Futures are consumed in submission order, so
linked-PR ordering, the decisive-PR early exit, and first-error
semantics are identical either way.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from taken import budget, checks


@pytest.fixture(autouse=True)
def _reset_budget():
    budget.reset()
    yield
    budget.reset()


def _recent_timestamp():
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cross_ref(n):
    return {
        "event": "cross-referenced",
        "source": {"issue": {"html_url": f"https://github.com/o/r/pull/{n}"}},
    }


def _pr_payload(n, state="closed"):
    return {
        "number": n,
        "title": f"PR {n}",
        "state": state,
        "merged_at": "2026-09-01T00:00:00Z" if state == "closed" else None,
        "user": {"login": "bob"},
        "html_url": f"https://github.com/o/r/pull/{n}",
        "updated_at": _recent_timestamp(),
    }


def make_timeline_fake(pr_numbers, states=None, fail_on=None):
    """Fake gh_api serving one timeline page plus PR detail fetches."""
    calls = []
    lock = threading.Lock()
    states = states or {}

    def fake(endpoint, params=None):
        with lock:
            calls.append(endpoint)
        if endpoint == "repos/o/r/issues/1/timeline":
            return [_cross_ref(n) for n in pr_numbers]
        if endpoint.startswith("repos/o/r/pulls/"):
            number = endpoint.rsplit("/", 1)[1]
            if fail_on is not None and number == fail_on:
                raise checks.TakenError("boom")
            return _pr_payload(int(number), states.get(number, "closed"))
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    fake.calls = calls
    return fake


def test_parallel_matches_sequential(monkeypatch):
    fake = make_timeline_fake([3, 1, 2])
    monkeypatch.setattr(checks, "gh_api", fake)
    budget.activate(identity=None)  # anonymous: sequential
    sequential = checks.check_timeline("o", "r", 1)
    seq_calls = sorted(fake.calls)
    fake.calls.clear()
    budget.reset()
    budget.activate(identity="someone")  # authenticated: parallel
    parallel = checks.check_timeline("o", "r", 1)
    assert parallel == sequential
    # Same calls, none added or lost by the concurrent fetch.
    assert sorted(fake.calls) == seq_calls
    # Ordering follows the timeline event order, not completion order.
    assert [p["number"] for p in parallel[0]] == [3, 1, 2]
    assert parallel[1] is False


def test_linked_pr_fetches_overlap(monkeypatch):
    started = []
    lock = threading.Lock()
    go = threading.Event()

    def fake(endpoint, params=None):
        if endpoint.startswith("repos/o/r/pulls/"):
            with lock:
                started.append(endpoint)
                if len(started) == 3:
                    go.set()
            # If the fetches ran sequentially this times out and fails:
            # proof of overlap without any timing assertions.
            assert go.wait(timeout=10), f"linked PR fetches did not overlap: {started}"
        if endpoint == "repos/o/r/issues/1/timeline":
            return [_cross_ref(n) for n in (3, 1, 2)]
        return _pr_payload(int(endpoint.rsplit("/", 1)[1]))

    monkeypatch.setattr(checks, "gh_api", fake)
    budget.activate(identity="someone")
    linked, truncated = checks.check_timeline("o", "r", 1)
    assert [p["number"] for p in linked] == [3, 1, 2]
    assert truncated is False
    assert len(started) == 3


def test_decisive_early_exit_matches_sequential(monkeypatch):
    # PR 2 is open and fresh: TAKEN-decisive. Both tiers return the same
    # prefix and truncation flag. The parallel tier may speculatively
    # fetch PR 3 (its fetch is already submitted when PR 2's ordered
    # result proves decisive); the sequential tier never does.
    for identity, may_fetch_3 in ((None, False), ("someone", True)):
        fake = make_timeline_fake([1, 2, 3], states={"2": "open"})
        monkeypatch.setattr(checks, "gh_api", fake)
        budget.reset()
        budget.activate(identity=identity)
        linked, truncated = checks.check_timeline("o", "r", 1)
        assert [p["number"] for p in linked] == [1, 2]
        assert truncated is True
        assert ("repos/o/r/pulls/3" in fake.calls) == may_fetch_3


def test_first_error_surfaces_in_order(monkeypatch):
    # The failing PR's error propagates in both tiers; the error is the
    # first failure in timeline order either way.
    for identity, fail_on in ((None, "1"), ("someone", "1"), ("someone", "2")):
        fake = make_timeline_fake([1, 2, 3], fail_on=fail_on)
        monkeypatch.setattr(checks, "gh_api", fake)
        budget.reset()
        budget.activate(identity=identity)
        with pytest.raises(checks.TakenError, match="boom"):
            checks.check_timeline("o", "r", 1)
