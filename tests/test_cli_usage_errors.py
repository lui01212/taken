"""Usage errors exit 3, never argparse's 2, which is the CAUTION verdict (#338)."""

import pytest

from taken.cli import main


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "need at least one target, --file, or --discover"),
        (["--discover", "octo/repo"], "--discover takes no targets"),
        (["--health"], "--health takes exactly one owner/repo target"),
        (["--health", "octo/a", "octo/b"], "--health takes exactly one owner/repo target"),
        (["--no-such-flag", "octo/repo#1"], "unrecognized arguments: --no-such-flag"),
        (["--limit", "many", "octo/repo"], "argument --limit: invalid"),
    ],
)
def test_usage_error_exits_3(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert err.startswith("usage: taken")
    assert f"taken: error: {message}" in err


def test_health_with_file_exits_3(tmp_path, capsys):
    targets = tmp_path / "targets.txt"
    targets.write_text("octo/repo\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--health", "--file", str(targets)])
    assert exc.value.code == 3
    assert "--health takes a single owner/repo target, not --file" in capsys.readouterr().err


def test_help_still_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "usage: taken" in capsys.readouterr().out
