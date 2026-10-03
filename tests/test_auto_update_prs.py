"""Tests for .github/workflows/auto_update_prs.py (issue #350).

The workflow script is the only workflow-side code that writes to the
GitHub API, so it gets unit tests with mocked urllib responses: the PR
listing must paginate past the first 100, behind PRs must get an
update-branch PUT, and no failure mode may pass silently.
"""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "auto_update_prs.py"
API = "https://api.github.com"
LIST = f"{API}/repos/o/r/pulls?state=open&per_page=100"
DETAIL = f"{API}/repos/o/r/pulls"
UPDATE = f"{API}/repos/o/r/pulls"


def load_script(monkeypatch, name="auto_update_prs"):
    """Import the workflow script fresh, with workflow env vars set."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pr(number, draft=False):
    return {"number": number, "draft": draft, "head": {"sha": f"sha{number}"}}


def detail(number, state):
    return {"number": number, "mergeable_state": state, "head": {"sha": f"sha{number}"}}


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHTTP:
    """Mock for urllib.request.urlopen, keyed on (method, url)."""

    def __init__(self, monkeypatch, module):
        self.routes = {}
        self.calls = []
        self.fallback = None
        monkeypatch.setattr(module.urllib.request, "urlopen", self._urlopen)

    def get(self, url, status, payload):
        self.routes[("GET", url)] = (status, payload)

    def put(self, url, status, payload):
        self.routes[("PUT", url)] = (status, payload)

    def fail(self, method, url, code, message):
        self.routes[(method, url)] = ("error", code, {"message": message})

    def _urlopen(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        data = json.loads(req.data.decode()) if req.data else None
        self.calls.append((method, url, data))
        route = self.routes.get((method, url))
        if route is None:
            if self.fallback is not None:
                return self.fallback(method, url, data)
            raise AssertionError(f"unexpected request: {method} {url}")
        if route[0] == "error":
            _, code, payload = route
            raise urllib.error.HTTPError(
                url, code, "err", {}, io.BytesIO(json.dumps(payload).encode())
            )
        status, payload = route
        return _FakeResponse(status, payload)

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]


def test_import_needs_no_env_vars(monkeypatch):
    """The script must import cleanly without workflow env vars set."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("auto_update_prs_noenv", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not raise


def test_pagination_walks_past_first_page(monkeypatch):
    """A full first page must not hide PRs on later pages."""
    mod = load_script(monkeypatch)
    http = FakeHTTP(monkeypatch, mod)
    page1 = [pr(n) for n in range(1, 101)]
    http.get(LIST + "&page=1", 200, page1)
    http.get(LIST + "&page=2", 200, [pr(101), pr(102)])
    # Page 2 is short, so no page 3 is requested.
    # Page-1 PRs are all clean; only #101 is behind.
    http.fallback = lambda m, u, d: _FakeResponse(200, detail(1, "clean"))
    http.get(f"{DETAIL}/101", 200, detail(101, "behind"))
    http.get(f"{DETAIL}/102", 200, detail(102, "clean"))
    http.put(f"{UPDATE}/101/update-branch", 202, {})

    mod.main()

    puts = http.puts()
    assert len(puts) == 1
    assert puts[0][1] == f"{UPDATE}/101/update-branch"
    assert puts[0][2] == {"expected_head_sha": "sha101"}
    listed_pages = [c[1] for c in http.calls if c[1].startswith(LIST)]
    assert listed_pages == [LIST + "&page=1", LIST + "&page=2"]


def test_behind_pr_gets_update_branch_put(monkeypatch, capsys):
    mod = load_script(monkeypatch)
    http = FakeHTTP(monkeypatch, mod)
    http.get(LIST + "&page=1", 200, [pr(7)])
    http.get(f"{DETAIL}/7", 200, detail(7, "behind"))
    http.put(f"{UPDATE}/7/update-branch", 200, {})

    mod.main()

    out = capsys.readouterr().out
    assert "PR #7: branch updated to main" in out
    assert "done: 1 PR branch(es) updated" in out
    assert "skipped" not in out


def test_failures_are_surfaced_not_silent(monkeypatch, capsys):
    """Unreadable PRs, unknown mergeability, and failed PUTs all land in
    the final summary instead of vanishing into per-line logs."""
    mod = load_script(monkeypatch)
    monkeypatch.setattr(mod, "MERGEABILITY_RETRIES", 2)
    monkeypatch.setattr(mod, "MERGEABILITY_SLEEP", 0)
    http = FakeHTTP(monkeypatch, mod)
    http.get(LIST + "&page=1", 200, [pr(7), pr(8), pr(9)])
    http.get(f"{DETAIL}/7", 200, detail(7, "behind"))
    http.put(f"{UPDATE}/7/update-branch", 409, {})
    http.fail("GET", f"{DETAIL}/8", 403, "forbidden")
    http.get(f"{DETAIL}/9", 200, detail(9, "unknown"))

    mod.main()

    out = capsys.readouterr().out
    assert "done: 0 PR branch(es) updated" in out
    assert "#7 (409: " in out
    assert "#8 (unreadable (403))" in out
    assert "#9 (mergeability still unknown)" in out
    # No update-branch PUT for the PRs that could not be examined.
    assert [c[1] for c in http.puts()] == [f"{UPDATE}/7/update-branch"]


def test_draft_prs_are_reported_as_skipped(monkeypatch, capsys):
    """Drafts are not updated, but they get a disposition line and land
    in the skip summary instead of vanishing silently (issue #367)."""
    mod = load_script(monkeypatch)
    http = FakeHTTP(monkeypatch, mod)
    http.get(LIST + "&page=1", 200, [pr(7, draft=True)])

    behind, skipped = mod.behind_prs()

    assert behind == []
    assert skipped == [(7, "draft")]
    out = capsys.readouterr().out
    assert "PR #7: skipped (draft)" in out
    # No detail request is ever made for a draft.
    assert http.calls == [("GET", LIST + "&page=1", None)]


def test_non_behind_states_are_reported_as_skipped(monkeypatch, capsys):
    """clean/dirty/blocked PRs get one disposition line each with their
    mergeable_state, and all land in the final summary (issue #367)."""
    mod = load_script(monkeypatch)
    http = FakeHTTP(monkeypatch, mod)
    http.get(LIST + "&page=1", 200, [pr(7), pr(8), pr(9)])
    http.get(f"{DETAIL}/7", 200, detail(7, "clean"))
    http.get(f"{DETAIL}/8", 200, detail(8, "dirty"))
    http.get(f"{DETAIL}/9", 200, detail(9, "blocked"))

    mod.main()

    out = capsys.readouterr().out
    assert "PR #7: skipped (mergeable_state=clean)" in out
    assert "PR #8: skipped (mergeable_state=dirty)" in out
    assert "PR #9: skipped (mergeable_state=blocked)" in out
    assert "done: 0 PR branch(es) updated" in out
    assert "#7 (mergeable_state=clean)" in out
    assert "#8 (mergeable_state=dirty)" in out
    assert "#9 (mergeable_state=blocked)" in out


def test_pr_list_failure_exits_loudly(monkeypatch):
    """A failed listing is a hard error, not an empty silent run."""
    mod = load_script(monkeypatch)
    http = FakeHTTP(monkeypatch, mod)
    http.get(LIST + "&page=1", 500, {"message": "boom"})

    with pytest.raises(SystemExit, match="could not list PRs"):
        mod.main()


def test_summary_written_to_step_summary(monkeypatch, tmp_path):
    mod = load_script(monkeypatch)
    step_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_file))
    http = FakeHTTP(monkeypatch, mod)
    http.get(LIST + "&page=1", 200, [pr(7)])
    http.get(f"{DETAIL}/7", 200, detail(7, "clean"))

    mod.main()

    text = step_file.read_text()
    assert "Auto-update PR branches" in text
    assert "done: 0 PR branch(es) updated" in text
