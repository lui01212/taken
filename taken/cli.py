"""Command-line interface for taken."""

import argparse
import concurrent.futures
import functools
import json
import re
import sys
import time

from taken import __version__, budget, checks, discover, graphql, health
from taken.verdict import CAUTION, GO, TAKEN, decide

EXIT_CODES = {GO: 0, TAKEN: 1, CAUTION: 2}

URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
SHORT_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")
PATH_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
REPO_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)$")


def parse_target(text):
    """Parse a target into ("issue", owner, repo, number) or ("repo", owner, repo, None).

    Accepts owner/repo#123, GitHub issue URLs, or a bare owner/repo
    (scan mode: check the repo's open issues automatically).
    """
    text = text.strip()
    for pattern in (URL_RE, SHORT_RE, PATH_RE):
        match = pattern.match(text)
        if match:
            owner, repo, number = match.groups()
            return ("issue", owner, repo, int(number))
    match = REPO_RE.match(text)
    if match:
        owner, repo = match.groups()
        return ("repo", owner, repo, None)
    return None


def _parse_error(text):
    """Print the standard unparseable-target error; return exit code 3."""
    print(
        f"error: could not parse {text!r}; use owner/repo#123, an issue URL, or owner/repo to scan",
        file=sys.stderr,
    )
    return 3


def _non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _probability(value):
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return number


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit 3, like _parse_error.

    argparse exits 2 by default, and 2 is the CAUTION verdict: a script
    reading the exit code would take a mistyped flag for a verdict.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


def build_parser():
    """Build and return the argument parser for the taken CLI.

    Takes no arguments. Configures command-line flags and options for
    positional issue targets, file inputs (--file), repository scanning
    (--limit, --label), candidate discovery (--discover), maintainer health
    reports (--health), cache management, output formatting (--json, --verbose,
    --debug), and stale-claim decay thresholds.
    """
    parser = _ArgumentParser(
        prog="taken",
        description="Check whether a GitHub issue is already taken before you volunteer for it.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="owner/repo#123 or GitHub issue URLs (one or more)",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help="read targets from a file, one per line (blank lines and # comments ignored)",
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        default=20,
        metavar="N",
        help=(
            "max open issues to check per scan target, or max candidates to return "
            "in --discover mode (default: 20)"
        ),
    )
    parser.add_argument(
        "--label",
        metavar="LABEL",
        default=None,
        help="scan mode: only consider open issues carrying this label",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="discover top candidates via GitHub issue search, verified and ranked",
    )
    parser.add_argument(
        "--language",
        metavar="LANG",
        default=None,
        help="discover: only consider repos in this language",
    )
    parser.add_argument(
        "--min-contributors",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="discover: only consider repos with at least N contributors in the last 90 days",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=discover.DEFAULT_JOBS,
        metavar="N",
        help="discover: verify candidates with N parallel workers "
        f"(default: {discover.DEFAULT_JOBS})",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="discover: hide the progress bar",
    )
    parser.add_argument(
        "--allocation",
        choices=["bandit", "recency"],
        default="bandit",
        help="discover: how verify budget is split across repos: 'bandit' "
        "spends it by Thompson sampling on each repo's observed GO yield "
        "(default), 'recency' verifies freshest first",
    )
    parser.add_argument(
        "--explore-floor",
        type=_probability,
        default=0.15,
        metavar="P",
        help="discover: probability a bandit pick explores uniformly "
        "instead of following sampled yields (default: 0.15)",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="report maintainer-facing repo health for one owner/repo target "
        "(claims waiting, stale PRs, stale beginner labels, untriaged issues); "
        "read-only, no verdicts",
    )
    parser.add_argument(
        "--claim-wait-days",
        type=_non_negative_int,
        default=health.DEFAULT_CLAIM_WAIT_DAYS,
        metavar="N",
        help="health: a claim with no maintainer reply counts as waiting after N days; "
        "a claim gone quiet after a maintainer reply counts after N days "
        f"(default: {health.DEFAULT_CLAIM_WAIT_DAYS})",
    )
    parser.add_argument(
        "--pr-stale-days",
        type=_non_negative_int,
        default=health.DEFAULT_PR_STALE_DAYS,
        metavar="N",
        help="health: an open PR counts as stale after N days without activity "
        f"(default: {health.DEFAULT_PR_STALE_DAYS})",
    )
    parser.add_argument(
        "--gfi-stale-days",
        type=_non_negative_int,
        default=health.DEFAULT_GFI_STALE_DAYS,
        metavar="N",
        help="health: a good first issue or hacktoberfest label counts as stale "
        f"after N days untouched (default: {health.DEFAULT_GFI_STALE_DAYS})",
    )
    parser.add_argument("--json", action="store_true", help="print the full findings as JSON")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print an API usage summary (calls per endpoint, cache hits/misses) "
        "to stderr at the end of the run",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print a machine-readable JSON debug report (wall-clock timings "
        "per phase, rate-limit state before/after, retries, backoff time, "
        "API usage) to stderr at the end of the run; implies --verbose. "
        "Only counts, timings, and sizes: never tokens or response bodies.",
    )
    parser.add_argument(
        "--me",
        metavar="LOGIN",
        default=None,
        help="your GitHub login; your own comments are ignored in the claimant scan",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the API response cache (~/.cache/taken, 1h TTL)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the API response cache (~/.cache/taken) and exit",
    )
    parser.add_argument(
        "--graphql",
        action="store_true",
        help="force the GraphQL fetch path even when not logged in to GitHub "
        "(falls back to REST on failure); the default for logged-in users",
    )
    parser.add_argument(
        "--rest",
        action="store_true",
        help="force the REST fetch path even when logged in to GitHub; "
        "escape hatch for the GraphQL default (or TAKEN_REST=1)",
    )
    parser.add_argument(
        "--persistent-session",
        action="store_true",
        help="GraphQL over one persistent HTTPS connection for the process; "
        "token from `gh auth token` is held in memory only. Opt-in.",
    )
    parser.add_argument(
        "--pr-idle-days",
        type=_non_negative_int,
        default=checks.DEFAULT_PR_IDLE_DAYS,
        metavar="N",
        help="stale-claim decay: an open linked PR with no activity for longer than N days "
        f"weakens from TAKEN to CAUTION (default: {checks.DEFAULT_PR_IDLE_DAYS})",
    )
    parser.add_argument(
        "--claim-silence-days",
        type=_non_negative_int,
        default=checks.DEFAULT_CLAIM_SILENCE_DAYS,
        metavar="N",
        help="stale-claim decay: days one claim blocks as CAUTION on a simple issue; "
        "the clock resets on any claimant activity "
        f"(default: {checks.DEFAULT_CLAIM_SILENCE_DAYS})",
    )
    parser.add_argument(
        "--claim-silence-complex-days",
        type=_non_negative_int,
        default=checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS,
        metavar="N",
        help="stale-claim decay: days one claim blocks as CAUTION on a complex issue "
        f"(default: {checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS})",
    )
    parser.add_argument("--version", action="version", version=f"taken {__version__}")
    return parser


def _pr_status(pr):
    """One-word status for a linked PR dict."""
    if pr["state"] == "open":
        return "open"
    if pr["merged"]:
        return "merged"
    return "closed"


def _human_linked_prs(findings, skipped, not_checked):
    lines = []
    if "timeline" in skipped:
        lines.append(f"  linked PRs: {not_checked}")
    elif findings["linked_prs"]:
        for pr in findings["linked_prs"]:
            lines.append(f'  linked PR: #{pr["number"]} "{pr["title"]}" ({_pr_status(pr)})')
            lines.append(f"             {pr['url']}")
    else:
        lines.append("  linked PRs: none found in timeline")
    return lines


def _human_assignees(issue):
    if issue["assignees"]:
        return [f"  assignees: {', '.join(issue['assignees'])}"]
    return ["  assignees: none"]


def _human_claimants(findings, skipped, not_checked):
    lines = []
    if "claimants" in skipped:
        lines.append(f"  claimants: {not_checked}")
    elif findings["claimants"]:
        for hit in findings["claimants"]:
            lines.append(
                f'  claimant: {hit["author"]} on {hit["date"]} (matched "{hit["pattern"]}")'
            )
            lines.append(f'            "{hit["snippet"]}"')
    else:
        lines.append("  claimants: none found in comments")
    return lines


def _human_ai_policy(policy, skipped, not_checked):
    if "ai_policy" in skipped:
        return [f"  AI policy: {not_checked}"]
    if policy["source"]:
        lines = [f"  AI policy: {policy['verdict']} ({policy['source']})"]
        if policy["snippet"]:
            lines.append(f'             "{policy["snippet"]}"')
        return lines
    return ["  AI policy: none found (no CONTRIBUTING file)"]


def _human_repo_health(health, skipped, not_checked):
    if "repo_health" in skipped:
        return [f"  repo health: {not_checked}"]
    return [
        f"  repo health: pushed {health['pushed_at'] or 'unknown'}, "
        f"{health['recent_merges']} PRs merged in last 30 days, "
        f"{health['contributors']} contributors in last 90 days",
    ]


def format_human(findings, verdict, reasons):
    """Format single-issue check results into a human-readable terminal report.

    Accepts `findings` (the dictionary of issue metadata, linked PRs, claimants,
    AI policy, repo health, and any skipped stages or transport fallback notes),
    the final `verdict` string (GO, TAKEN, or CAUTION), and a list of `reasons`
    explaining the decision, returning a multiline string for terminal output.
    """
    issue = findings["issue"]
    health = findings["repo_health"]
    policy = findings["ai_policy"]
    # Stages skipped by the cheapest-decisive-first early stop (#125) are
    # reported as not checked, never as observed facts.
    skipped = set(findings.get("stages_skipped") or [])
    not_checked = "not checked (verdict already decided)"
    lines = [
        f"taken? {findings['target']}",
        f"verdict: {verdict}",
        "",
    ]
    # A GraphQL->REST fallback is honest evidence about the check itself:
    # say so up front so the verdict is never read as more confident than
    # the transport that produced it.
    if findings.get("transport_fallback"):
        lines.append(f"  note: {findings['transport_fallback']}")
        lines.append("")
    lines += [
        f'  issue: {issue["state"]}, "{issue["title"]}"',
        f"         {issue['url']} ({issue['comment_count']} comments)",
    ]
    lines += _human_linked_prs(findings, skipped, not_checked)
    lines += _human_assignees(issue)
    lines += _human_claimants(findings, skipped, not_checked)
    lines += _human_ai_policy(policy, skipped, not_checked)
    lines += _human_repo_health(health, skipped, not_checked)
    friendly = checks.friendly_labels(findings)
    if friendly:
        lines.append(f"  first-time friendly: {', '.join(friendly)}")
    welcoming = checks.welcoming_signals(findings)
    if welcoming:
        lines.append(f"  welcoming: {', '.join(welcoming)}")
    lines.append("")
    lines.append("why:")
    for reason in reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)


def _read_targets_file(args, targets):
    """Append --file targets to targets.

    Returns an exit code when the file cannot be read, else None.
    """
    if not args.file:
        return None
    try:
        # S8707 false positive: --file is the invoker's own explicit path;
        # the caller and the file owner are the same party, so no traversal
        # boundary is crossed.
        with open(args.file, encoding="utf-8") as fh:  # NOSONAR
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append(line)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 3
    return None


def _run_discover_command(parser, args):
    if args.targets or args.file:
        parser.error("--discover takes no targets")
    return _run_with_stats(run_discover, args, verbose=args.verbose, debug=args.debug)


def _run_health_command(parser, args, targets):
    if args.file:
        parser.error("--health takes a single owner/repo target, not --file")
    if len(targets) != 1:
        parser.error("--health takes exactly one owner/repo target")
    parsed = parse_target(targets[0])
    if not parsed or parsed[0] != "repo":
        print(
            f"error: --health needs an owner/repo target, got {targets[0]!r}",
            file=sys.stderr,
        )
        return 3
    _, owner, repo, _number = parsed
    return _run_with_stats(run_health, owner, repo, args, verbose=args.verbose, debug=args.debug)


def _run_target_command(parser, args, targets):
    if not targets:
        parser.error("need at least one target, --file, or --discover")
    if len(targets) == 1:
        if not parse_target(targets[0]):
            # Fail before _run_with_stats: a usage error must exit 3
            # without touching the network, so the budget identity probe
            # (and any other subprocess call) must not fire.
            return _parse_error(targets[0])
        return _run_with_stats(run_single, targets[0], args, verbose=args.verbose, debug=args.debug)
    return _run_with_stats(run_batch, targets, args, verbose=args.verbose, debug=args.debug)


def main(argv=None):
    """Main CLI entry point orchestrating argument parsing, dispatch, and exit codes.

    Accepts `argv` (argument strings, defaulting to sys.argv[1:]), parses flags with
    `build_parser`, and routes execution: `--clear-cache` to `run_clear_cache`,
    `--discover` to `run_discover`, `--health` to `run_health`, a single issue/repo
    target to `run_single`, and multiple targets to `run_batch`. Follows the exit-code
    contract: 0 on GO or command success, 1 on TAKEN (or cache clear error), 2 on
    CAUTION, and 3 on unparseable targets, missing arguments, or API/network errors.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.clear_cache:
        return run_clear_cache()
    if args.no_cache:
        checks._CACHE_ENABLED = False
    targets = list(args.targets)
    file_error = _read_targets_file(args, targets)
    if file_error is not None:
        return file_error
    if args.discover:
        return _run_discover_command(parser, args)
    if args.health:
        return _run_health_command(parser, args, targets)
    return _run_target_command(parser, args, targets)


def _run_with_stats(func, *fargs, verbose=False, debug=False):
    """Run a CLI command, printing stats to stderr at the end of the run.

    --verbose prints the human API usage summary; --debug implies it and
    adds a machine-readable JSON report (timings per phase, rate-limit
    state before/after, retries, backoff time). Both go to stderr so
    --json stdout stays clean for piping, and both print even when the
    run fails (exit 3), which is exactly when the numbers matter most.

    The budget line (tier + requests used) prints on every run, verbose
    or not: per-run accounting is local only, never telemetry.
    """
    budget.activate()
    checks.reset_api_stats()
    rate_start = checks.rate_limit_snapshot() if debug else None
    start = time.perf_counter()
    try:
        return func(*fargs)
    finally:
        total = time.perf_counter() - start
        rate_end = checks.rate_limit_snapshot() if debug else None
        print(checks.budget_line(), file=sys.stderr)
        if verbose or debug:
            print(checks.api_stats_summary(), file=sys.stderr)
        if debug:
            print(checks.debug_report(total, rate_start, rate_end), file=sys.stderr)


def format_discover_line(result):
    """Format a single candidate discovery result into a one-line summary.

    Takes `result`, a candidate dictionary containing the computed `score`,
    `target` identifier (owner/repo#number), `why` reason list, and any
    `friendly_labels` or `welcoming` signals, and returns a formatted single-line string.
    """
    why = "; ".join(result["why"])
    markers = "".join(f" [{m}]" for m in result["friendly_labels"] + result["welcoming"])
    return f"{result['score']:3}  {result['target']}  {why}{markers}"


def _make_progress_bar(args):
    """A tqdm progress bar for candidate verification, or None."""
    if args.no_progress or not sys.stderr.isatty():
        return None
    from tqdm import tqdm

    return tqdm(
        total=0,
        desc="verifying candidates",
        unit="issue",
        file=sys.stderr,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}",
    )


def _update_progress_bar(bar, done, total):
    bar.total = total
    bar.n = done
    bar.refresh()


def _log_searched(searched):
    detail = ", ".join(f"{lab} ({count})" for lab, count in searched)
    print(f"searched: {detail}", file=sys.stderr)


def _discover_options(args, bar):
    return discover.DiscoverOptions(
        limit=args.limit,
        language=args.language,
        label=args.label,
        min_contributors=args.min_contributors,
        me=args.me,
        jobs=args.jobs,
        on_progress=functools.partial(_update_progress_bar, bar) if bar is not None else None,
        on_searched=_log_searched,
        mode=graphql.fetch_mode(args),
        thresholds=_thresholds_from_args(args),
        allocation=args.allocation,
        explore_floor=args.explore_floor,
    )


def _warn_search_errors(results):
    for label, error in results.search_errors:
        print(
            f'warning: partial results: search failed for "{label}": {error}',
            file=sys.stderr,
        )


def _report_no_candidates(results):
    """Explain an empty candidate list; exit 3 when everything errored."""
    errors = getattr(results, "errors", 0)
    total = getattr(results, "total", 0)
    if errors and errors == total:
        # Every candidate errored: the tool is broken, not the data.
        # Exit 3 (hard failure) so scripts and agents do not mistake
        # this for a healthy empty result.
        print(
            f"no candidates passed verification: all {total} errored "
            "(check `gh auth status` and your network connection)",
            file=sys.stderr,
        )
        return 3
    if errors:
        print(
            f"no candidates passed verification "
            f"({errors} of {total} candidates failed with errors)",
            file=sys.stderr,
        )
    else:
        print("no candidates passed verification", file=sys.stderr)
    return 0


def _print_discover_results(results, args):
    if args.json:
        budget = checks.budget_report()
        print(
            json.dumps(
                [
                    {
                        "target": r["target"],
                        "score": r["score"],
                        "why": r["why"],
                        "verdict": r["verdict"],
                        "reasons": r["reasons"],
                        "findings": r["findings"],
                        "friendly_labels": r["friendly_labels"],
                        "welcoming": r["welcoming"],
                        "budget": budget,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        for r in results:
            print(format_discover_line(r))


def run_discover(args):
    """Search, verify, and rank the top candidates."""
    if args.jobs < 1:
        print("error: --jobs must be at least 1", file=sys.stderr)
        return 3
    bar = _make_progress_bar(args)
    try:
        results = discover.discover(_discover_options(args, bar))
    except checks.TakenError as exc:
        if bar is not None:
            bar.close()
        print(f"error: {exc}", file=sys.stderr)
        return 3
    _warn_search_errors(results)
    if bar is not None:
        bar.close()
    if not results:
        return _report_no_candidates(results)
    _print_discover_results(results, args)
    return 0


def _thresholds_from_args(args):
    """Stale-claim decay settings (issue #83) from the parsed CLI flags.

    getattr with defaults keeps check_one() callable with hand-built
    namespaces that predate these flags (as in tests).
    """
    return {
        "pr_idle_days": getattr(args, "pr_idle_days", checks.DEFAULT_PR_IDLE_DAYS),
        "claim_silence_days": getattr(
            args, "claim_silence_days", checks.DEFAULT_CLAIM_SILENCE_DAYS
        ),
        "claim_silence_complex_days": getattr(
            args, "claim_silence_complex_days", checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS
        ),
    }


def check_one(owner, repo, number, me, mode="rest", payload=None, thresholds=None, session=None):
    """Run the full check on one issue. Returns (target, verdict, reasons, findings).

    GraphQL-family modes fall back to REST per issue when the GraphQL
    transport fails; the fallback is recorded in the findings.

    `payload` is an optional pre-fetched issue item (e.g. from
    list_open_issues): on the REST path it skips the per-issue refetch
    (issue #211). The GraphQL path issues one combined query per issue
    and cannot reuse a REST item, so the payload is ignored there.

    `thresholds` carries the stale-claim decay settings (issue #83);
    None means the defaults.

    `session` is an optional persistent GraphQL session. In persistent
    mode with no session given, each calling thread gets its own session
    via graphql.thread_session(); the process-wide singleton is never
    shared across pool worker threads (issue #317).
    """
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
        thresholds=thresholds,
        session=session,
    )
    verdict, reasons = decide(findings)
    return f"{owner}/{repo}#{number}", verdict, reasons, findings


def run_clear_cache():
    """Delete the API response cache and report what was removed."""
    removed = checks.clear_cache()
    if removed == -1:
        cache_dir = checks.cache_dir()
        print(
            f"error: refusing to clear cache: {cache_dir!r} is not a safe path. "
            "Check your TAKEN_CACHE_DIR environment variable.",
            file=sys.stderr,
        )
        return 1
    noun = "entry" if removed == 1 else "entries"
    print(f"cleared {removed} cache {noun} ({checks.cache_dir()})")
    return 0


def run_single(text, args):
    """Check a single target issue or dispatch a bare repo target to run_batch.

    Parses the target string `text` (such as owner/repo#123 or an issue URL), runs
    issue checks via `check_one` with options from `args` (including `--json`, `--me`,
    and decay thresholds), prints either human-readable or JSON output, and returns
    the exit code (0 for GO, 1 for TAKEN, 2 for CAUTION, or 3 on parse/API errors).
    """
    parsed = parse_target(text)
    if not parsed:
        return _parse_error(text)
    if parsed[0] == "repo":
        return run_batch([text], args)
    _, owner, repo, number = parsed
    try:
        target, verdict, reasons, findings = check_one(
            owner,
            repo,
            number,
            args.me,
            mode=graphql.fetch_mode(args),
            thresholds=_thresholds_from_args(args),
        )
    except checks.TakenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(
            json.dumps(
                {
                    "target": target,
                    "verdict": verdict,
                    "reasons": reasons,
                    "findings": findings,
                    "budget": checks.budget_report(),
                },
                indent=2,
            )
        )
    else:
        print(format_human(findings, verdict, reasons))
    return EXIT_CODES[verdict]


def run_health(owner, repo, args):
    """Print the maintainer health report for one repo. Read-only; exit 0 on success, 3 on error."""
    options = health.HealthOptions(
        claim_wait_days=args.claim_wait_days,
        pr_stale_days=args.pr_stale_days,
        gfi_stale_days=args.gfi_stale_days,
    )
    try:
        report = health.repo_health(owner, repo, options=options, me=args.me)
    except checks.TakenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(
            json.dumps(
                {
                    "target": f"{owner}/{repo}",
                    "health": health.health_to_dict(report),
                    "budget": checks.budget_report(),
                },
                indent=2,
            )
        )
    else:
        print(health.format_health_human(report))
    return 0


def format_batch_line(target, verdict, reasons):
    """Format a target's check result into a single line for batch output.

    Takes the `target` identifier string, the `verdict` string (GO, TAKEN, or CAUTION),
    and a sequence of `reasons`, returning a compact line displaying the padded
    verdict, target name, and primary reason.
    """
    first = reasons[0] if reasons else ""
    return f"{verdict:7} {target}  {first}"


def _expand_repo_target(text, owner, repo, args, jobs):
    """List a repo's open issues into jobs. Returns True when it failed."""
    try:
        issues = checks.list_open_issues(owner, repo, limit=args.limit, label=args.label)
    except checks.TakenError as exc:
        print(f"error: {text}: {exc}", file=sys.stderr)
        return True
    if not issues:
        print(f"note: {text}: no open issues found", file=sys.stderr)
    for item in issues:
        number = item["number"]
        # The listing already fetched this issue: pass it as
        # payload so check_issue() skips the redundant GET.
        jobs.append((f"{owner}/{repo}#{number}", owner, repo, number, item))
    return False


def _collect_batch_jobs(targets, args):
    """Parse targets into check jobs.

    Returns (jobs, failed, scanned_repo). Target parsing and repo issue
    listings stay sequential (cheap, and their error messages keep input
    order); jobs is a list of (error label, owner, repo, number, payload)
    in input order.
    """
    jobs = []
    failed = False
    scanned_repo = False
    for text in targets:
        parsed = parse_target(text)
        if not parsed:
            _parse_error(text)
            failed = True
            continue
        if parsed[0] == "repo":
            scanned_repo = True
            _, owner, repo, _number = parsed
            if _expand_repo_target(text, owner, repo, args, jobs):
                failed = True
        else:
            _, owner, repo, number = parsed
            jobs.append((text, owner, repo, number, None))
    return jobs, failed, scanned_repo


def _run_batch_jobs(jobs, args, mode):
    """Run check jobs through a worker pool. Returns (results, failed).

    Results are collected in input order, so output is identical to the
    sequential run.
    """
    results = []
    failed = False
    workers = budget.current().batch_workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                check_one,
                owner,
                repo,
                number,
                args.me,
                mode,
                payload,
                _thresholds_from_args(args),
            )
            for _, owner, repo, number, payload in jobs
        ]
        for (label, _, _, _, _), future in zip(jobs, futures, strict=True):
            try:
                results.append(future.result())
            except checks.TakenError as exc:
                print(f"error: {label}: {exc}", file=sys.stderr)
                failed = True
    return results, failed


def _print_go_summary(results):
    gos = [
        (target, checks.friendly_labels(findings))
        for target, verdict, _r, findings in results
        if verdict == GO
    ]
    # First-time-friendly issues first: the safest ones to adopt.
    gos.sort(key=lambda item: (not item[1], item[0]))
    print()
    if gos:
        noun = "candidate" if len(gos) == 1 else "candidates"
        parts = [f"{target} ({', '.join(labels)})" if labels else target for target, labels in gos]
        print(f"{len(gos)} GO {noun}: " + ", ".join(parts))
    else:
        print("no GO candidates in this scan.")


def _print_batch_results(results, args, scanned_repo):
    if args.json:
        budget_info = checks.budget_report()
        print(
            json.dumps(
                [
                    {
                        "target": target,
                        "verdict": verdict,
                        "reasons": reasons,
                        "findings": findings,
                        "budget": budget_info,
                    }
                    for target, verdict, reasons, findings in results
                ],
                indent=2,
            )
        )
    else:
        for target, verdict, reasons, _findings in results:
            print(format_batch_line(target, verdict, reasons))
        if scanned_repo and results:
            _print_go_summary(results)


def run_batch(targets, args):
    """Check many targets; print one verdict line each.

    A bare owner/repo target is scanned automatically: its open issues
    (up to --limit, optionally filtered by --label) are each checked.
    When any repo was scanned, a GO-candidate recommendation summary is
    printed at the end.
    Returns 0 when every target produced a verdict, 3 when any target
    failed to parse or its checks errored.

    Target parsing and repo issue listings stay sequential (cheap, and
    their error messages keep input order); the per-issue checks run
    through a worker pool sized by the budget tier, reusing discover's
    ThreadPoolExecutor pattern. Results are collected in input order,
    so output is identical to the sequential run.
    """
    jobs, failed, scanned_repo = _collect_batch_jobs(targets, args)
    results = []
    if jobs:
        batch_results, batch_failed = _run_batch_jobs(jobs, args, graphql.fetch_mode(args))
        results.extend(batch_results)
        failed = failed or batch_failed
    _print_batch_results(results, args, scanned_repo)
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
