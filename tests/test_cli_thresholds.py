"""CLI validation for stale-claim threshold flags."""

import pytest

from taken.cli import build_parser

THRESHOLD_FLAGS = (
    "--pr-idle-days",
    "--claim-silence-days",
    "--claim-silence-complex-days",
)


@pytest.mark.parametrize("flag", THRESHOLD_FLAGS)
def test_stale_claim_thresholds_reject_negative_values(flag, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([flag, "-1"])

    assert exc_info.value.code == 3
    assert f"argument {flag}: must be non-negative" in capsys.readouterr().err


@pytest.mark.parametrize("flag", THRESHOLD_FLAGS)
def test_stale_claim_thresholds_accept_zero(flag):
    args = build_parser().parse_args([flag, "0"])

    assert getattr(args, flag[2:].replace("-", "_")) == 0
