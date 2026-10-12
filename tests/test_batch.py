"""Batch mode tests: many targets in one run, one verdict line each."""

import json
from datetime import datetime, timezone

import pytest

from taken import checks
from taken.cli import main


def make_fake(state):
    """state maps issue number to 'taken', 'go', or 'boom'."""

    def fake(endpoint, params=None):
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            kind = state.get(number, "go")
            if kind == "boom":
                raise checks.TakenError("simulated `gh` failure")
            if endpoint.endswith("/timeline"):
                return []
            if endpoint.endswith("/comments"):
                return []
            return {
                "state": "open",
                "title": f"issue {number}",
                "labels": [],
                "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
                "comments": 0,
                "user": {"login": "someone"},
                "html_url": f"https://github.com/octo/repo/issues/{number}",
                "created_at": "2026-01-01T00:00:00Z",
            }
        if "/contents/" in endpoint:
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
def two_targets(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "taken", 2: "go"}))


def test_batch_prints_one_line_per_target(two_targets, capsys):
    assert main(["octo/repo#1", "octo/repo#2"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("TAKEN   octo/repo#1")
    assert "assigned to: dk5488" in lines[0]
    assert lines[1].startswith("GO      octo/repo#2")


def test_batch_parse_error_does_not_stop_others(two_targets, capsys):
    assert main(["bogus-target", "octo/repo#2"]) == 3
    out = capsys.readouterr()
    assert "could not parse 'bogus-target'" in out.err
    assert "GO      octo/repo#2" in out.out


def test_batch_api_error_does_not_stop_others(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "boom", 2: "go"}))
    assert main(["octo/repo#1", "octo/repo#2"]) == 3
    out = capsys.readouterr()
    assert "octo/repo#1" in out.err
    assert "simulated `gh` failure" in out.err
    assert "GO      octo/repo#2" in out.out


def test_batch_json(two_targets, capsys):
    assert main(["--json", "octo/repo#1", "octo/repo#2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["verdict"] for r in data] == ["TAKEN", "GO"]
    assert [r["target"] for r in data] == ["octo/repo#1", "octo/repo#2"]
    assert all("findings" in r and "reasons" in r for r in data)


def test_file_input(two_targets, tmp_path, capsys):
    path = tmp_path / "targets.txt"
    path.write_text("# candidates\nocto/repo#1\n\n  \nocto/repo#2\n")
    assert main(["--file", str(path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("TAKEN")
    assert lines[1].startswith("GO")


def test_file_and_positional_combine(two_targets, tmp_path, capsys):
    path = tmp_path / "targets.txt"
    path.write_text("octo/repo#2\n")
    assert main(["octo/repo#1", "--file", str(path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2


def test_missing_file_is_exit_3(capsys):
    assert main(["--file", "/nonexistent-targets.txt"]) == 3
    assert "cannot read" in capsys.readouterr().err


def test_no_targets_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 3
