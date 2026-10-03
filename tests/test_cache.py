"""Cache tests: gh_api caches successful responses for an hour."""

import json

import pytest

from taken import checks, cli


class Proc:
    def __init__(self, stdout):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture
def cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)
    # Pre-seed the memoized identity so these tests don't pay for a lookup.
    monkeypatch.setattr(checks, "_IDENTITY", "octocat")
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", True)
    checks._MEM_CACHE.clear()


@pytest.fixture
def counting_run(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        calls.append(cmd)
        return Proc(json.dumps({"ok": True, "n_calls": len(calls)}))

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    return calls


def test_second_identical_call_uses_cache(cache_env, counting_run):
    first = checks.gh_api("repos/octo/repo")
    second = checks.gh_api("repos/octo/repo")
    assert len(counting_run) == 1
    assert first == second == {"ok": True, "n_calls": 1}


def test_params_are_part_of_cache_key(cache_env, counting_run):
    checks.gh_api("repos/octo/repo/issues/1/comments", {"per_page": "100"})
    checks.gh_api("repos/octo/repo/issues/1/comments", {"per_page": "50"})
    assert len(counting_run) == 2


def test_stale_entry_refetches(cache_env, counting_run, tmp_path):
    checks.gh_api("repos/octo/repo")
    assert len(counting_run) == 1
    v2 = tmp_path / "cache" / "v2"
    (cache_file,) = [p for p in v2.iterdir() if p.suffix == ".json"]
    entry = json.loads(cache_file.read_text())
    entry["fetched_at"] -= checks.CACHE_TTL_SECONDS + 1
    cache_file.write_text(json.dumps(entry))
    # Drop the in-memory copy so the stale file entry is actually consulted.
    checks._MEM_CACHE.clear()
    checks.gh_api("repos/octo/repo")
    assert len(counting_run) == 2


def test_cache_disabled_refetches_every_time(cache_env, counting_run, monkeypatch):
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)
    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/repo")
    assert len(counting_run) == 2


def test_corrupt_cache_file_is_ignored(cache_env, counting_run, tmp_path):
    cache_dir = tmp_path / "cache" / "v2"
    cache_dir.mkdir(parents=True)
    (cache_dir / "deadbeef.json").write_text("not json{{{")
    assert checks.gh_api("repos/octo/repo") == {"ok": True, "n_calls": 1}
    assert len(counting_run) == 1


def test_memory_cache_avoids_disk_reads(cache_env, counting_run, tmp_path):
    checks.gh_api("repos/octo/repo")
    assert len(counting_run) == 1
    v2 = tmp_path / "cache" / "v2"
    (cache_file,) = [p for p in v2.iterdir() if p.suffix == ".json"]
    cache_file.unlink()  # the file is gone; memory must still serve the key
    assert checks.gh_api("repos/octo/repo") == {"ok": True, "n_calls": 1}
    assert len(counting_run) == 1


def test_unwritable_cache_dir_does_not_break(cache_env, counting_run, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a dir")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(blocker))
    assert checks.gh_api("repos/octo/repo") == {"ok": True, "n_calls": 1}
    assert len(counting_run) == 1


def test_no_cache_flag_disables_cache(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)  # restored on teardown
    calls = []

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        calls.append(cmd)
        return Proc("not used")

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    # --no-cache with an unparseable target: must exit 3 before any API call
    from taken.cli import main

    assert main(["--no-cache", "bogus"]) == 3
    assert checks._CACHE_ENABLED is False
    assert calls == []


def test_concurrent_writes_keep_cache_valid(cache_env, counting_run):
    import threading

    errors = []

    def worker(n):
        try:
            for i in range(10):
                checks.gh_api(f"repos/octo/repo{n}", {"page": str(i)})
        except Exception as exc:  # noqa: BLE001 - any failure here is the bug
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # Every key written by every thread must be present and readable.
    for n in range(8):
        for i in range(10):
            data = checks.gh_api(f"repos/octo/repo{n}", {"page": str(i)})
            assert data["ok"] is True


def test_clear_cache_flag_removes_dir(cache_env, counting_run, tmp_path, capsys):
    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/other")
    cache_dir = tmp_path / "cache"
    assert (cache_dir / "v2").is_dir()
    assert cli.main(["--clear-cache"]) == 0
    assert not cache_dir.exists()
    out = capsys.readouterr().out
    assert f"cleared 2 cache entries ({cache_dir})" in out


def test_clear_cache_flag_empty_cache(cache_env, tmp_path, capsys):
    cache_dir = tmp_path / "cache"
    (cache_dir / "v2").mkdir(parents=True)
    assert cli.main(["--clear-cache"]) == 0
    assert not cache_dir.exists()
    assert "cleared 0 cache entries" in capsys.readouterr().out


def test_clear_cache_flag_empty_default_cache(tmp_path, capsys, monkeypatch):
    """Empty default cache directory (~/.cache/taken) is safely cleared."""
    fake_home = tmp_path / "fake_home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TAKEN_CACHE_DIR", raising=False)
    default_cache = fake_home / ".cache" / "taken"
    default_cache.mkdir(parents=True)
    assert cli.main(["--clear-cache"]) == 0
    assert not default_cache.exists()
    assert "cleared 0 cache entries" in capsys.readouterr().out


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/",
        "~",
        "/home",
        "/System/Volumes/Data/home",
        "/Users",
        "/etc",
        "/tmp/evil",
        "relative/path",
    ],
)
def test_clear_cache_rejects_unsafe_path(unsafe_path, capsys, monkeypatch):
    """--clear-cache must refuse to delete system directories."""
    monkeypatch.setenv("TAKEN_CACHE_DIR", unsafe_path)
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err


def test_clear_cache_rejects_empty_arbitrary_custom_dir(tmp_path, capsys, monkeypatch):
    """An arbitrary empty custom directory not inside default cache must be rejected."""
    empty_dir = tmp_path / "arbitrary_empty"
    empty_dir.mkdir()
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(empty_dir))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err
    assert empty_dir.exists()


def test_clear_cache_rejects_symlink_to_system_root(tmp_path, capsys, monkeypatch):
    """A symlink pointing to a protected system directory must be rejected."""
    link = tmp_path / "link_to_system_root"
    import os

    target = "/home" if os.path.exists("/home") else "/etc"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("cannot create symlink")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(link))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err


def test_clear_cache_rejects_symlink_to_user_home(tmp_path, capsys, monkeypatch):
    """A symlink pointing to user home directory must be rejected."""
    import os

    link = tmp_path / "link_to_user_home"
    try:
        link.symlink_to(os.path.expanduser("~"))
    except OSError:
        pytest.skip("cannot create symlink")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(link))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err


def test_clear_cache_accepts_custom_dir_with_only_cache_files(
    cache_env, counting_run, tmp_path, capsys
):
    """A custom TAKEN_CACHE_DIR holding only taken cache files may be cleared."""
    checks.gh_api("repos/octo/repo")
    cache_dir = tmp_path / "cache"
    assert (cache_dir / "v2").is_dir()
    assert cli.main(["--clear-cache"]) == 0
    assert not cache_dir.exists()
    assert "cleared 1 cache entry" in capsys.readouterr().out


def test_clear_cache_refuses_custom_dir_with_foreign_files(
    cache_env, counting_run, tmp_path, capsys
):
    """A custom TAKEN_CACHE_DIR containing non-cache files must not be deleted."""
    checks.gh_api("repos/octo/repo")
    cache_dir = tmp_path / "cache"
    precious = cache_dir / "precious.json"
    precious.write_text('{"do": "not delete"}')
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err
    assert precious.exists()
    assert (cache_dir / "v2").is_dir()


def test_clear_cache_rejects_unrelated_subdirectory_alongside_v2(tmp_path, capsys, monkeypatch):
    """An empty unrelated subdirectory alongside v2/ must be rejected."""
    cache_dir = tmp_path / "custom_cache"
    (cache_dir / "v2").mkdir(parents=True)
    unrelated = cache_dir / "unrelated"
    unrelated.mkdir()
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err
    assert cache_dir.exists()
    assert (cache_dir / "v2").is_dir()
    assert unrelated.is_dir()


@pytest.mark.parametrize(
    "subpath",
    ["nested/api_cache.json", "v2/api_cache.json"],
)
def test_clear_cache_rejects_nested_api_cache_json(tmp_path, capsys, monkeypatch, subpath):
    """api_cache.json is only permitted at the cache root, never in subdirectories."""
    cache_dir = tmp_path / "custom_cache"
    nested_file = cache_dir / subpath
    nested_file.parent.mkdir(parents=True, exist_ok=True)
    nested_file.write_text("{}")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err
    assert nested_file.exists()


@pytest.mark.parametrize(
    "subpath",
    ["v2/v2", "nested/v2"],
)
def test_clear_cache_rejects_nested_v2_directory(tmp_path, capsys, monkeypatch, subpath):
    """v2 directory is permitted only directly at cache root; nested v2 is rejected."""
    cache_dir = tmp_path / "custom_cache"
    nested_v2 = cache_dir / subpath
    nested_v2.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err
    assert nested_v2.exists()


@pytest.mark.parametrize(
    "file_path",
    ["unexpected.txt", "v2/unexpected.txt"],
)
def test_clear_cache_rejects_unexpected_non_json_files(tmp_path, capsys, monkeypatch, file_path):
    """Unexpected non-JSON files at root or inside v2/ must be rejected."""
    cache_dir = tmp_path / "custom_cache"
    (cache_dir / "v2").mkdir(parents=True, exist_ok=True)
    target = cache_dir / file_path
    target.write_text("not json content")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 1
    assert "not a safe path" in capsys.readouterr().err
    assert target.exists()


def test_clear_cache_accepts_valid_root_api_cache_json(tmp_path, capsys, monkeypatch):
    """A custom cache directory containing a valid root-level api_cache.json can be cleared."""
    cache_dir = tmp_path / "custom_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "api_cache.json").write_text("{}")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 0
    assert not cache_dir.exists()
    assert "cleared 1 cache entry" in capsys.readouterr().out


def test_clear_cache_accepts_valid_root_v2_digest_json(tmp_path, capsys, monkeypatch):
    """A custom cache directory with valid root-level v2/<digest>.json can be cleared."""
    cache_dir = tmp_path / "custom_cache"
    v2_dir = cache_dir / "v2"
    v2_dir.mkdir(parents=True)
    digest_filename = "abcd1234ef567890" * 4 + ".json"
    (v2_dir / digest_filename).write_text("{}")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 0
    assert not cache_dir.exists()
    assert "cleared 1 cache entry" in capsys.readouterr().out


def test_clear_cache_accepts_valid_custom_cache_with_temp_files(tmp_path, capsys, monkeypatch):
    """A valid custom cache directory with entry files and atomic write
    temp files is safely cleared.
    """
    cache_dir = tmp_path / "custom_cache"
    v2_dir = cache_dir / "v2"
    v2_dir.mkdir(parents=True)
    (v2_dir / "abcd1234ef567890.json").write_text("{}")
    (v2_dir / ".cache-tmp12345").write_text("{}")
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(cache_dir))
    assert cli.main(["--clear-cache"]) == 0
    assert not cache_dir.exists()
    assert "cleared 1 cache entry" in capsys.readouterr().out


def test_cache_misses_after_identity_switch(monkeypatch, tmp_path):
    """Switching gh identity must not serve the previous identity's entries."""
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", False)
    monkeypatch.setattr(checks, "_IDENTITY", None)
    checks._MEM_CACHE.clear()

    logins = ["alice"]
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            return Proc(logins[0] + "\n")
        return Proc(json.dumps({"who": logins[0], "n": len(calls)}))

    monkeypatch.setattr(checks.subprocess, "run", fake_run)

    first = checks.gh_api("repos/octo/repo")
    assert first == {"who": "alice", "n": 2}  # identity lookup + real call
    assert checks.gh_api("repos/octo/repo") == first  # cache hit, same identity

    # Simulate a fresh process after `gh auth switch`, sharing the disk cache.
    logins[0] = "bob"
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", False)
    monkeypatch.setattr(checks, "_IDENTITY", None)
    checks._MEM_CACHE.clear()

    third = checks.gh_api("repos/octo/repo")
    assert third["who"] == "bob"  # miss: refetched, never alice's data
    assert third != first


def test_cache_skipped_when_identity_unknown(monkeypatch, tmp_path):
    """Fail closed: no caching at all when the gh identity can't be determined."""
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", False)
    monkeypatch.setattr(checks, "_IDENTITY", None)
    checks._MEM_CACHE.clear()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            proc = Proc("")
            proc.returncode = 1
            proc.stderr = "gh: not authenticated"
            return proc
        return Proc(json.dumps({"ok": True}))

    monkeypatch.setattr(checks.subprocess, "run", fake_run)

    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/repo")
    real_calls = [c for c in calls if c[:3] != ["gh", "api", "user"]]
    assert len(real_calls) == 2
    assert len(checks._MEM_CACHE) == 0


def test_identity_lookup_memoized(monkeypatch, tmp_path):
    """`gh api user` runs once per process, not once per API call."""
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", False)
    monkeypatch.setattr(checks, "_IDENTITY", None)
    checks._MEM_CACHE.clear()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            return Proc("OctoCat\n")
        return Proc(json.dumps({"ok": True}))

    monkeypatch.setattr(checks.subprocess, "run", fake_run)

    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/repo")
    identity_calls = [c for c in calls if c[:3] == ["gh", "api", "user"]]
    assert len(identity_calls) == 1
