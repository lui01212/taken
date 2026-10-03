"""Root pytest configuration and global state isolation fixtures."""

import pytest

from taken import budget, checks


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """Isolate mutable global state across test runs.

    Resets budget tier, memoized identity, in-memory cache, and API stats
    to their known default baselines both before and after every test,
    ensuring test order and ambient environment credentials cannot leak
    between tests. Note: this resets state to default baselines rather
    than restoring prior ambient state.
    """
    budget.reset()
    checks._IDENTITY = None
    checks._IDENTITY_FETCHED = False
    checks._MEM_CACHE.clear()
    checks.reset_api_stats()
    yield
    budget.reset()
    checks._IDENTITY = None
    checks._IDENTITY_FETCHED = False
    checks._MEM_CACHE.clear()
    checks.reset_api_stats()
