"""Range validation for numeric CLI flags and MCP parameters (issue #339)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from taken import checks, discover
from taken.cli import build_parser
from taken.mcp_server import mcp

COUNT_FLAGS = ("--limit", "--min-contributors")

DECAY_PARAMETERS = ("pr_idle_days", "claim_silence_days", "claim_silence_complex_days")
MCP_NUMERIC_PARAMETERS = [
    ("scan_repo", "limit"),
    ("discover_candidates", "limit"),
    ("discover_candidates", "min_contributors"),
] + [
    (tool, parameter)
    for tool in ("check_issue", "scan_repo", "discover_candidates")
    for parameter in DECAY_PARAMETERS
]
REQUIRED_ARGUMENTS = {
    "check_issue": {"owner": "octo", "repo": "repo", "issue_number": 1},
    "scan_repo": {"owner": "octo", "repo": "repo"},
    "discover_candidates": {},
}


@pytest.mark.parametrize("flag", COUNT_FLAGS)
def test_count_flags_reject_negative_values(flag, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([flag, "-5"])

    assert exc_info.value.code == 3
    assert f"argument {flag}: must be non-negative" in capsys.readouterr().err


@pytest.mark.parametrize("flag", COUNT_FLAGS)
def test_count_flags_accept_zero(flag):
    args = build_parser().parse_args([flag, "0"])

    assert getattr(args, flag[2:].replace("-", "_")) == 0


@pytest.mark.parametrize("value", ["-0.1", "1.5", "nan"])
def test_explore_floor_rejects_values_outside_zero_to_one(value, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--explore-floor", value])

    assert exc_info.value.code == 3
    assert "argument --explore-floor: must be between 0 and 1" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "0.15", "1"])
def test_explore_floor_accepts_zero_to_one(value):
    assert build_parser().parse_args(["--explore-floor", value]).explore_floor == float(value)


def _tool_schemas():
    return {tool.name: tool.input_schema for tool in asyncio.run(mcp.list_tools())}


def _minimum(property_schema):
    # Optional parameters are wrapped in anyOf with a null branch.
    for branch in property_schema.get("anyOf", [property_schema]):
        if "minimum" in branch:
            return branch["minimum"]
    return None


@pytest.mark.parametrize(("tool", "parameter"), MCP_NUMERIC_PARAMETERS)
def test_mcp_numeric_parameters_advertise_a_minimum_of_zero(tool, parameter):
    assert _minimum(_tool_schemas()[tool]["properties"][parameter]) == 0


@pytest.mark.parametrize(("tool", "parameter"), MCP_NUMERIC_PARAMETERS)
def test_mcp_numeric_parameters_reject_negative_values(monkeypatch, tool, parameter):
    def no_api_calls(*args, **kwargs):
        raise AssertionError("a negative value must be rejected before any API call")

    monkeypatch.setattr(checks, "gh_api", no_api_calls)
    monkeypatch.setattr(discover, "discover", no_api_calls)

    # Build the coroutine outside the raises block so the block holds a
    # single throwing invocation (python:S5778). Creating the coroutine
    # runs no code; the ToolError surfaces when asyncio runs it.
    coro = mcp.call_tool(tool, {**REQUIRED_ARGUMENTS[tool], parameter: -5})
    with pytest.raises(ToolError, match="greater than or equal to 0"):
        asyncio.run(coro)
