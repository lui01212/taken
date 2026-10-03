"""Tests for scripts/hotspots.py, especially the read-only --check mode (#368).

The script computes hotspot metrics from the repo's git history via radon,
so these tests run against the checked-out repo itself (BADGE_PATH is
redirected to a tmp dir so the real badge file is never touched).
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load_hotspots(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hotspots", SCRIPTS_DIR / "hotspots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BADGE_PATH", tmp_path / "hotspot.json")
    return mod


@pytest.fixture
def hotspots(tmp_path, monkeypatch):
    # radon is not a test dependency (only the hotspots workflow installs
    # it via `uv run --with radon`); skip instead of failing where it is
    # absent. The script itself stays fail-closed: without radon, main()
    # exits 2 because it cannot verify anything.
    pytest.importorskip("radon")
    return load_hotspots(tmp_path, monkeypatch)


def test_default_mode_writes_badge(hotspots, capsys):
    rc = hotspots.main([])
    assert rc == 0
    badge = json.loads(hotspots.BADGE_PATH.read_text())
    assert badge["schemaVersion"] == 1
    assert badge["label"] == "hotspots"
    out = capsys.readouterr().out
    assert "wrote" in out
    assert "file" in out  # the ranked table was printed


def test_check_mode_passes_when_badge_is_fresh(hotspots, capsys):
    assert hotspots.main([]) == 0
    capsys.readouterr()  # discard the write-mode output
    before = hotspots.BADGE_PATH.read_bytes()
    rc = hotspots.main(["--check"])
    assert rc == 0
    assert hotspots.BADGE_PATH.read_bytes() == before  # nothing rewritten
    out = capsys.readouterr().out
    assert "up to date" in out
    assert "wrote" not in out


def test_check_mode_fails_without_writing_when_stale(hotspots, capsys):
    assert hotspots.main([]) == 0
    capsys.readouterr()  # discard the write-mode output
    hotspots.BADGE_PATH.write_text('{"stale": true}\n')  # simulate drift
    rc = hotspots.main(["--check"])
    assert rc == 1
    assert hotspots.BADGE_PATH.read_text() == '{"stale": true}\n'  # not rewritten
    out = capsys.readouterr().out
    assert "would change" in out
    assert "wrote" not in out


def test_check_mode_fails_when_badge_missing(hotspots, capsys):
    assert not hotspots.BADGE_PATH.exists()
    rc = hotspots.main(["--check"])
    assert rc == 1
    assert not hotspots.BADGE_PATH.exists()  # still not written
    out = capsys.readouterr().out
    assert "would change" in out


def test_parse_args():
    # sanity: --check is accepted and defaults to off
    spec = importlib.util.spec_from_file_location("hotspots_args", SCRIPTS_DIR / "hotspots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.parse_args(["--check"]).check is True
    assert mod.parse_args([]).check is False
