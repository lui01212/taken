"""MCP server for taken.

Exposes taken's issue checks as tools so coding agents can ask
"is this issue taken?" before volunteering for work.

Run with the ``taken-mcp`` console script (stdio transport). The server
inherits the invoker's environment, so ``gh`` must be installed and
authenticated, exactly like the ``taken`` CLI.

Needs the ``mcp`` dependency, which ships with every ``taken-gh`` install.
If it is ever missing (broken install) this module still imports cleanly,
but ``main()`` prints guidance instead of starting a server.

Never print to stdout here: it carries the JSON-RPC stream. Logs go to
stderr only.
"""

import concurrent.futures
import subprocess
import sys
from typing import Annotated

try:
    from pydantic import Field
except ImportError:  # pydantic ships with the `mcp` dependency
    # S1542 false positive: mirrors pydantic's public Field name, so
    # callers can use Field(...) with or without pydantic installed.
    def Field(**kwargs):  # type: ignore[no-redef]  # NOSONAR
        return kwargs


from taken import __version__, budget, checks, discover, graphql
from taken.verdict import decide


def _check_one(
    owner, repo, number, me=None, mode=None, payload=None, session=None, thresholds=None
):
    """Run the full check suite on one issue; return the tool payload.

    ``mode`` selects the fetch path ("rest", "graphql", "persistent");
    None resolves through ``graphql.fetch_mode()``, so a logged-in MCP
    host gets the GraphQL default and an anonymous one stays on REST,
    exactly like the CLI. GraphQL-family modes fall back to REST when
    the GraphQL transport fails; the fallback is recorded in the
    findings.

    `payload` is an optional pre-fetched issue item: on the REST path it
    skips the per-issue refetch (issue #211).

    `session` is an optional persistent GraphQL session. In persistent
    mode with no session given, each calling thread gets its own session
    via graphql.thread_session(); the process-wide singleton is never
    shared across pool worker threads (issue #317).

    `thresholds` carries the stale-claim decay settings (issue #83);
    None means the defaults from checks.default_thresholds().
    """
    if mode is None:
        mode = graphql.fetch_mode()
    if mode == "persistent" and session is None:
        # Runs on the worker thread, so thread-local storage hands this
        # thread its own session instead of the shared singleton.
        session = graphql.thread_session()
    findings = graphql.run_checks_with_fallback(
        owner,
        repo,
        number,
        me=me,
        mode=mode,
        payload=payload,
        session=session,
        thresholds=thresholds,
    )
    verdict, reasons = decide(findings)
    return {
        "target": f"{owner}/{repo}#{number}",
        "verdict": verdict,
        "reasons": reasons,
        "findings": findings,
        "friendly_labels": checks.friendly_labels(findings),
        "welcoming": checks.welcoming_signals(findings),
        "budget": checks.budget_report(),
    }


def _error_payload(exc: Exception) -> dict[str, str]:
    message = str(exc)
    if isinstance(exc, checks.RateLimitError):
        code = "rate_limited"
    elif isinstance(exc, checks.NotFoundError):
        code = "not_found"
    elif isinstance(exc, subprocess.TimeoutExpired) or "timed out after" in message.lower():
        code = "timeout"
    elif any(
        marker in message.lower()
        for marker in (
            "http 401",
            "bad credentials",
            "gh auth login",
            "`gh auth token` failed",
            "requires authentication",
        )
    ):
        code = "auth_failed"
    else:
        code = "unknown"
    return {"error": message, "error_code": code}


# Verdict ordering for scan_repo: best candidates first.
_VERDICT_RANK = {"GO": 0, "CAUTION": 1, "TAKEN": 2}


def _decay_thresholds(pr_idle_days=None, claim_silence_days=None, claim_silence_complex_days=None):
    """Stale-claim decay settings (issue #83) shared by the three MCP tools.

    Explicit values win; anything left None falls back to the checks
    defaults, exactly like discover_candidates did before this helper
    existed.
    """
    return {
        "pr_idle_days": pr_idle_days if pr_idle_days is not None else checks.DEFAULT_PR_IDLE_DAYS,
        "claim_silence_days": (
            claim_silence_days
            if claim_silence_days is not None
            else checks.DEFAULT_CLAIM_SILENCE_DAYS
        ),
        "claim_silence_complex_days": (
            claim_silence_complex_days
            if claim_silence_complex_days is not None
            else checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS
        ),
    }


def check_issue(
    owner: str,
    repo: str,
    issue_number: int,
    me: str | None = None,
    graphql: Annotated[
        bool,
        Field(description="Force the GraphQL path. Default: automatic from auth state."),
    ] = False,
    persistent_session: Annotated[
        bool,
        Field(
            description="Force the persistent-session GraphQL path; token from "
            "`gh auth token` held in memory only. Default: automatic from auth state."
        ),
    ] = False,
    pr_idle_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days of linked-PR inactivity before "
            "TAKEN weakens to CAUTION. Default: 90."
        ),
    ] = None,
    claim_silence_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days one claim blocks as CAUTION on a "
            "simple issue; the clock resets on claimant activity. Default: 7."
        ),
    ] = None,
    claim_silence_complex_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days one claim blocks as CAUTION on a "
            "complex issue. Default: 14."
        ),
    ] = None,
) -> dict:
    """Check whether a GitHub issue is already taken.

    Verdicts: GO (free to volunteer), TAKEN (spoken for: closed, open PR,
    or assignee), CAUTION (soft signal: someone expressed interest, the repo
    bans AI contributions, or the repo looks stale).

    The payload also carries `friendly_labels` (first-time-contributor
    labels on the issue) and `welcoming` (repo-level signs contributions
    are welcome), the same markers `scan_repo` returns.

    Error payloads carry `error` plus `error_code`: RateLimitError ->
    `rate_limited`, NotFoundError -> `not_found`, HTTP 401/CLI authentication
    failures -> `auth_failed`, subprocess timeouts -> `timeout`, other
    TakenError failures -> `unknown`.

    Args:
        owner: repository owner login
        repo: repository name
        issue_number: issue number to check
        me: your GitHub login; your own comments are ignored in the claimant scan
        graphql: force the GraphQL fetch path instead of the automatic choice
        persistent_session: force the persistent-session GraphQL path instead of the automatic one
        pr_idle_days: stale-claim decay threshold for idle linked PRs (default 90)
        claim_silence_days: stale-claim decay threshold for simple issues (default 7)
        claim_silence_complex_days: stale-claim decay threshold for complex issues
            (default 14)
    """
    # Explicit flags win; otherwise the transport is automatic from auth
    # state (GraphQL when logged in, REST when anonymous), like the CLI.
    if persistent_session:
        mode = "persistent"
    elif graphql:
        mode = "graphql"
    else:
        mode = None
    thresholds = _decay_thresholds(pr_idle_days, claim_silence_days, claim_silence_complex_days)
    try:
        return _check_one(owner, repo, issue_number, me=me, mode=mode, thresholds=thresholds)
    except (checks.TakenError, subprocess.TimeoutExpired) as exc:
        return {"target": f"{owner}/{repo}#{issue_number}", **_error_payload(exc)}


def scan_repo(
    owner: str,
    repo: str,
    limit: Annotated[int, Field(description="Max open issues to check. Default: 20.")] = 20,
    label: Annotated[
        str | None,
        Field(
            description="Only consider open issues carrying this label. Default: no label filter."
        ),
    ] = None,
    me: Annotated[
        str | None,
        Field(description="Your GitHub login; your own comments are ignored. Default: none."),
    ] = None,
    pr_idle_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days of linked-PR inactivity before "
            "TAKEN weakens to CAUTION. Default: 90."
        ),
    ] = None,
    claim_silence_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days one claim blocks as CAUTION on a "
            "simple issue; the clock resets on claimant activity. Default: 7."
        ),
    ] = None,
    claim_silence_complex_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days one claim blocks as CAUTION on a "
            "complex issue. Default: 14."
        ),
    ] = None,
) -> dict:
    """Scan a repository's open issues and recommend the GO ones.

    Results are ordered GO first, then CAUTION, then TAKEN, so the best
    candidates to volunteer for come first. `recommendations` lists just
    the GO targets; `summary` counts each verdict. Each result carries
    `friendly_labels` (first-time-contributor labels on the issue) and
    `welcoming` (repo-level signs contributions are welcome), so an agent
    can prefer the safest issues to adopt.

    Error payloads carry `error` plus `error_code`: RateLimitError ->
    `rate_limited`, NotFoundError -> `not_found`, HTTP 401/CLI authentication
    failures -> `auth_failed`, subprocess timeouts -> `timeout`, other
    TakenError failures -> `unknown`.

    Args:
        owner: repository owner login
        repo: repository name
        limit: max open issues to check (default 20)
        label: only consider open issues carrying this label
        me: your GitHub login; your own comments are ignored in the claimant scan
        pr_idle_days: stale-claim decay threshold for idle linked PRs (default 90)
        claim_silence_days: stale-claim decay threshold for simple issues (default 7)
        claim_silence_complex_days: stale-claim decay threshold for complex issues
            (default 14)
    """
    thresholds = _decay_thresholds(pr_idle_days, claim_silence_days, claim_silence_complex_days)
    effective_parameters = {"limit": limit, "label": label, "me": me, "thresholds": thresholds}
    try:
        issues = checks.list_open_issues(owner, repo, limit=limit, label=label)
    except (checks.TakenError, subprocess.TimeoutExpired) as exc:
        return {
            "target": f"{owner}/{repo}",
            "effective_parameters": effective_parameters,
            **_error_payload(exc),
        }
    # The per-issue checks run through a worker pool sized by the budget
    # tier, reusing discover's ThreadPoolExecutor pattern. Futures are
    # consumed in input order, so the stable verdict-rank sort below
    # yields exactly the sequential output. The fetch mode is resolved
    # once up front so the identity probe never fires concurrently.
    mode = graphql.fetch_mode()
    workers = budget.current().batch_workers
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _check_one,
                owner,
                repo,
                item["number"],
                me=me,
                mode=mode,
                payload=item,
                thresholds=thresholds,
            )
            for item in issues
        ]
        for item, future in zip(issues, futures, strict=True):
            number = item["number"]
            try:
                # The listing already fetched this issue: pass it as payload
                # so the per-issue refetch is skipped (issue #211).
                payload = future.result()
            except (checks.TakenError, subprocess.TimeoutExpired) as exc:
                results.append({"target": f"{owner}/{repo}#{number}", **_error_payload(exc)})
                continue
            # The _check_one payload already carries the markers; reuse
            # them instead of recomputing (issue #46).
            results.append(
                {
                    "target": payload["target"],
                    "verdict": payload["verdict"],
                    "reasons": payload["reasons"],
                    "friendly_labels": payload["friendly_labels"],
                    "welcoming": payload["welcoming"],
                }
            )
    results.sort(key=lambda r: _VERDICT_RANK.get(str(r.get("verdict") or ""), 3))
    summary = {"GO": 0, "CAUTION": 0, "TAKEN": 0, "errors": 0}
    for item in results:
        verdict = item.get("verdict")
        if verdict in summary:
            summary[verdict] += 1
        else:
            summary["errors"] += 1
    return {
        "target": f"{owner}/{repo}",
        "effective_parameters": effective_parameters,
        "results": results,
        "recommendations": [r["target"] for r in results if r.get("verdict") == "GO"],
        "summary": summary,
        "budget": checks.budget_report(),
    }


def discover_candidates(
    limit: Annotated[int, Field(description="Max candidates to return. Default: 10.")] = 10,
    language: Annotated[
        str | None,
        Field(
            description="Only consider repositories in this language. Default: no language filter."
        ),
    ] = None,
    label: Annotated[
        str | None,
        Field(
            description="Issue label to search. Default: good first issue, good-first-issue, "
            "beginner friendly, and help wanted."
        ),
    ] = None,
    min_contributors: Annotated[
        int,
        Field(
            description="Only consider repositories with at least this many "
            "contributors in the last 90 days. Default: 0."
        ),
    ] = 0,
    me: Annotated[
        str | None,
        Field(description="Your GitHub login; your own comments are ignored. Default: none."),
    ] = None,
    pr_idle_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days of linked-PR inactivity before "
            "TAKEN weakens to CAUTION. Default: 90."
        ),
    ] = None,
    claim_silence_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days one claim blocks as CAUTION on a "
            "simple issue; the clock resets on claimant activity. Default: 7."
        ),
    ] = None,
    claim_silence_complex_days: Annotated[
        int | None,
        Field(
            description="Stale-claim decay: days one claim blocks as CAUTION on a "
            "complex issue. Default: 14."
        ),
    ] = None,
) -> dict:
    """Discover top open-source contribution candidates.

    Searches GitHub for good-first-issue style issues, runs taken's full
    verification on each, and returns the ranked candidates. Each result
    carries `friendly_labels` (first-time-contributor labels on the issue)
    and `welcoming` (repo-level signs contributions are welcome).

    Error payloads carry `error` plus `error_code`: RateLimitError ->
    `rate_limited`, NotFoundError -> `not_found`, HTTP 401/CLI authentication
    failures -> `auth_failed`, subprocess timeouts -> `timeout`, other
    TakenError failures -> `unknown`.

    Args:
        limit: max candidates to return (default 10)
        language: only consider repos in this language
        label: issue label to search (defaults to good-first-issue style labels)
        min_contributors: only consider repos with at least this many contributors
            in the last 90 days
        me: your GitHub login; your own comments are ignored in the claimant scan
        pr_idle_days: stale-claim decay threshold for idle linked PRs (default 90)
        claim_silence_days: stale-claim decay threshold for simple issues (default 7)
        claim_silence_complex_days: stale-claim decay threshold for complex issues
            (default 14)
    """
    thresholds = _decay_thresholds(pr_idle_days, claim_silence_days, claim_silence_complex_days)
    effective_parameters = {
        "limit": limit,
        "language": language,
        "labels": [label] if label else list(discover.SEARCH_LABELS),
        "min_contributors": min_contributors,
        "me": me,
        "thresholds": thresholds,
    }
    try:
        options = discover.DiscoverOptions(
            limit=limit,
            language=language,
            label=label,
            min_contributors=min_contributors,
            me=me,
            jobs=discover.DEFAULT_JOBS,
            on_progress=None,
            # Automatic from auth state (GraphQL when logged in), like the CLI.
            mode=graphql.fetch_mode(),
            thresholds=thresholds,
        )
        results = discover.discover(options)
    except (checks.TakenError, subprocess.TimeoutExpired) as exc:
        return {"effective_parameters": effective_parameters, **_error_payload(exc)}
    return {
        "effective_parameters": effective_parameters,
        "search_errors": [
            {"label": label, **_error_payload(checks.TakenError(error))}
            for label, error in getattr(results, "search_errors", [])
        ],
        "results": [
            {
                "target": item["target"],
                "score": item["score"],
                "why": item["why"],
                "verdict": item["verdict"],
                "reasons": item["reasons"],
                "friendly_labels": item["friendly_labels"],
                "welcoming": item["welcoming"],
            }
            for item in results
        ],
        "budget": checks.budget_report(),
    }


def _create_server():
    """Build the MCP server and register taken's tools.

    Called at import time; an ImportError is caught by the caller so a
    broken ``mcp`` install degrades to a guidance message.
    """
    from mcp.server import MCPServer

    server = MCPServer(
        "taken",
        title="taken",
        description="Check whether a GitHub issue is already taken before volunteering for it.",
        version=__version__,
    )
    server.tool()(check_issue)
    server.tool()(scan_repo)
    server.tool()(discover_candidates)
    return server


try:
    mcp = _create_server()
except ImportError:  # `mcp` is required; this only triggers on a broken install
    mcp = None


def main():
    """Entry point for the ``taken-mcp`` console script."""
    budget.activate()
    if mcp is None:
        print(
            "taken-mcp needs the MCP SDK, which ships with taken-gh: "
            'try pip install --force-reinstall "taken-gh[mcp]"',
            file=sys.stderr,
        )
        return 2
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    print("taken-mcp speaks JSON-RPC on stdio; launch it from an MCP client.", file=sys.stderr)
    sys.exit(2)
