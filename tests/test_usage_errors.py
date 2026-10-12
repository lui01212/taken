"""Every usage error exits 3, never argparse's 2 (issue #338).

Exit 2 means CAUTION, so a script reading the exit code must not mistake a
misused flag for a verdict.
"""

import pytest

from taken import cli

USAGE_ERRORS = [
    (["--discover", "octo/repo#1"], "--discover takes no targets"),
    (["--discover", "--file", "TARGETS"], "--discover takes no targets"),
    (["--health", "--file", "TARGETS"], "--health takes a single owner/repo target"),
    (["--health"], "--health takes exactly one owner/repo target"),
    (["--health", "octo/a", "octo/b"], "--health takes exactly one owner/repo target"),
    ([], "need at least one target, --file, or --discover"),
    (["--no-such-flag"], "unrecognized arguments: --no-such-flag"),
    (["--limit", "-1", "octo/repo"], "argument --limit: must be non-negative"),
]


@pytest.fixture
def no_run(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("a usage error must stop before any check runs")

    monkeypatch.setattr(cli, "_run_with_stats", fail)


@pytest.mark.parametrize(("argv", "message"), USAGE_ERRORS)
def test_usage_error_exits_3(argv, message, no_run, tmp_path, capsys):
    targets = tmp_path / "targets.txt"
    targets.write_text("octo/repo#1\n", encoding="utf-8")
    argv = [str(targets) if arg == "TARGETS" else arg for arg in argv]
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)

    assert exc_info.value.code == 3
    err = capsys.readouterr().err
    assert err.startswith("usage: taken")
    assert f"taken: error: {message}" in err


def test_help_still_exits_0(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: taken")
