"""submit_next ordering (issue #335): a candidate is never silently lost.

The queue index must only leave its queue once pool.submit has actually
accepted the work. If submit raises, the index goes back to the front of
the queue it came from and the error propagates.
"""

import pytest

from taken.discover import DiscoverOptions, _RollingVerifier


class ExplodingPool:
    """pool.submit always raises, like a closed executor at shutdown."""

    def __init__(self):
        self.calls = 0

    def submit(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("cannot schedule new futures after shutdown")


class FakeFuture:
    pass


class WorkingPool:
    """pool.submit succeeds and hands back a dummy future."""

    def __init__(self):
        self.calls = 0
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        future = FakeFuture()
        self.submitted.append((fn, args, future))
        return future


def make_verifier(n=3, allocation="recency"):
    candidates = [("octo", "repo", i, {"number": i}) for i in range(n)]
    options = DiscoverOptions(allocation=allocation, jobs=2)
    return _RollingVerifier(candidates, options, limit=10)


def test_recency_submit_raise_keeps_candidate_queued():
    verifier = make_verifier(allocation="recency")
    pool = ExplodingPool()
    with pytest.raises(RuntimeError, match="shutdown"):
        verifier.submit_next(pool)
    assert pool.calls == 1
    # The index was restored: still work, still the same head, nothing
    # recorded as submitted.
    assert verifier.has_work()
    assert list(verifier.queue) == [0, 1, 2]
    assert verifier.in_flight == {}
    assert verifier.verified == 0


def test_recency_failed_submit_retries_same_candidate():
    verifier = make_verifier(allocation="recency")
    with pytest.raises(RuntimeError):
        verifier.submit_next(ExplodingPool())
    pool = WorkingPool()
    verifier.submit_next(pool)
    # The restored index is submitted first, preserving queue order.
    assert verifier.in_flight[pool.submitted[0][2]] == 0
    assert list(verifier.queue) == [1, 2]
    assert verifier.verified == 1


def test_recency_successful_submit_pops_as_before():
    verifier = make_verifier(allocation="recency")
    pool = WorkingPool()
    verifier.submit_next(pool)
    verifier.submit_next(pool)
    assert list(verifier.queue) == [2]
    assert sorted(verifier.in_flight.values()) == [0, 1]
    assert verifier.verified == 2


def test_bandit_submit_raise_restores_repo_queue():
    verifier = make_verifier(n=2, allocation="bandit")
    repo_key = ("octo", "repo")
    assert list(verifier.repo_queues[repo_key]) == [0, 1]
    with pytest.raises(RuntimeError, match="shutdown"):
        verifier.submit_next(ExplodingPool())
    assert list(verifier.repo_queues[repo_key]) == [0, 1]
    assert verifier.has_work()
    assert verifier.in_flight == {}
    assert verifier.verified == 0


def test_bandit_failed_submit_retries_same_candidate():
    verifier = make_verifier(n=2, allocation="bandit")
    with pytest.raises(RuntimeError):
        verifier.submit_next(ExplodingPool())
    pool = WorkingPool()
    verifier.submit_next(pool)
    assert verifier.in_flight[pool.submitted[0][2]] == 0
    assert verifier.verified == 1


def test_bandit_successful_submit_pops_as_before():
    verifier = make_verifier(n=2, allocation="bandit")
    pool = WorkingPool()
    verifier.submit_next(pool)
    verifier.submit_next(pool)
    repo_key = ("octo", "repo")
    assert list(verifier.repo_queues[repo_key]) == []
    assert not verifier.has_work()
    assert verifier.verified == 2
