"""Tests for the repo-health pulls scan page cutoff (issue #218).

Page 2+ is fetched only while the previous page's oldest `updated_at`
is still inside the window: pages are `sort=updated desc`, and a merged
PR always satisfies `updated_at >= merged_at`, so a page whose oldest
entry predates the cutoff cannot hide an in-window merge on any later
page. The scan therefore skips a provably redundant call.
"""

from datetime import datetime, timedelta, timezone

from taken import checks

WINDOW_DAYS = 30


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cutoff():
    return datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)


def _pr(merged_days_ago, updated_days_ago):
    """One pulls payload entry; updated_days_ago=None omits updated_at."""
    pr = {"merged_at": _ts(datetime.now(timezone.utc) - timedelta(days=merged_days_ago))}
    if updated_days_ago is not None:
        pr["updated_at"] = _ts(datetime.now(timezone.utc) - timedelta(days=updated_days_ago))
    return pr


def _make_fake(pages):
    """Fake gh_api dispatching on the page param; records pages fetched."""
    calls = []

    def fake(endpoint, params=None):
        assert endpoint == "repos/o/r/pulls"
        page = int(params["page"])
        calls.append(page)
        return pages.get(page, [])

    fake.calls = calls
    return fake


def _run(monkeypatch, pages, pulls_pages=2):
    fake = _make_fake(pages)
    monkeypatch.setattr(checks, "gh_api", fake)
    count = checks._repo_recent_merges("o", "r", _cutoff(), pulls_pages)
    return count, fake.calls


def test_stale_first_page_skips_page_two(monkeypatch):
    """A full page whose oldest update predates the cutoff: page 2 is skipped."""
    pages = {1: [_pr(40, 40) for _ in range(50)], 2: [_pr(1, 1) for _ in range(50)]}
    count, calls = _run(monkeypatch, pages)
    assert calls == [1]
    assert count == 0


def test_fresh_first_page_still_fetches_page_two(monkeypatch):
    """No behavior change on active repos: a fresh full page keeps paging."""
    pages = {
        1: [_pr(2, 1) for _ in range(3)] + [_pr(40, 1) for _ in range(47)],
        2: [_pr(40, 40) for _ in range(5)],
    }
    count, calls = _run(monkeypatch, pages)
    assert calls == [1, 2]
    assert count == 3


def test_oldest_exactly_at_cutoff_keeps_paging(monkeypatch):
    """Boundary is strict: updated_at == cutoff may still hide an in-window merge."""
    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    prs = [_pr(40, 40) for _ in range(49)]
    prs.append({"merged_at": _ts(cutoff), "updated_at": _ts(cutoff)})
    fake = _make_fake({1: prs, 2: []})
    monkeypatch.setattr(checks, "gh_api", fake)
    count = checks._repo_recent_merges("o", "r", cutoff, 2)
    assert fake.calls == [1, 2]
    assert count == 1


def test_empty_page_breaks(monkeypatch):
    count, calls = _run(monkeypatch, {1: []})
    assert calls == [1]
    assert count == 0


def test_short_page_breaks(monkeypatch):
    """Short page still short-circuits before the timestamp check."""
    pages = {1: [_pr(2, 1), _pr(2, 1), _pr(40, 1)], 2: [_pr(1, 1)]}
    count, calls = _run(monkeypatch, pages)
    assert calls == [1]
    assert count == 2


def test_missing_updated_at_fails_closed(monkeypatch):
    """No updated_at on the oldest entry: keep paging as before."""
    pages = {1: [_pr(40, None) for _ in range(50)], 2: []}
    count, calls = _run(monkeypatch, pages)
    assert calls == [1, 2]
    assert count == 0


def test_malformed_updated_at_fails_closed(monkeypatch):
    """Unparseable updated_at: keep paging as before."""
    page1 = [_pr(40, 40) for _ in range(49)]
    page1.append(
        {
            "merged_at": _ts(datetime.now(timezone.utc) - timedelta(days=40)),
            "updated_at": "not-a-timestamp",
        }
    )
    pages = {1: page1, 2: []}
    count, calls = _run(monkeypatch, pages)
    assert calls == [1, 2]
    assert count == 0


def test_in_window_merges_on_stale_page_still_count(monkeypatch):
    """The cutoff break only skips the fetch; page-1 merges are counted."""
    pages = {1: [_pr(2, 40) for _ in range(10)] + [_pr(40, 40) for _ in range(40)]}
    count, calls = _run(monkeypatch, pages)
    assert calls == [1]
    assert count == 10
