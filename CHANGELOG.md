# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.8.0] - 2026-10-02

### Security
- Cache-directory validation hardened: protected system roots (such as `/`, `/home`, and `/tmp`) can never be selected for cache deletion, symlinks are rejected, and only the exact taken cache structure qualifies for cleanup (#356).

### Fixed
- GraphQL subprocess failures with no stderr now report stdout or the exit status instead of an empty diagnostic (#343).

### Changed
- CONTRIBUTING.md pre-push checklist now includes the mypy step (#351).

## [Unreleased]

## [0.7.5] - 2026-10-01

### Added
- npm installer (`taken-gh`): `npm install taken-gh` now pulls the matching
  Python CLI through pip in a postinstall step, with `taken` and `taken-mcp`
  shims on PATH. The postinstall skips when the installed version already
  matches, reinstalls on version mismatch, and retries once with
  `--break-system-packages` on PEP 668 systems (Debian 12+, Ubuntu 23.04+).
  It never fails the npm install. README and the npm page carry an npm
  version badge.
- `taken --health owner/repo`: maintainer-facing repo health overview.
  Read-only report listing claims waiting on a maintainer reply (oldest
  first), claims where the claimant went quiet, open PRs grouped by idle
  age, stale `good first issue` / `hacktoberfest` labels, and untriaged
  issues. Summary counts on top, `--json` output, tunable thresholds
  (`--claim-wait-days`, `--pr-stale-days`, `--gfi-stale-days`).
- `--discover` now allocates the verify budget across repositories with
  Thompson sampling: each repository is a bandit arm, a clean GO is a
  success, a clean non-GO is a failure, and transport errors do not update
  the arm. `--allocation {bandit,recency}` and `--explore-floor` tune it;
  recency ordering is preserved within each repository.
- `DiscoverOptions` dataclass replaces the long parameter list on
  `discover()`; the stale-claim decay thresholds are threaded through
  `--discover` and the MCP `discover_candidates` tool.
- MCP tools now return machine-readable error codes instead of plain
  strings.

### Fixed
- Homebrew tap bump workflow requirements path.

### Changed
- CI no longer carries the `merge_group` trigger: merge queues require an
  organization-owned repository, so the trigger could never fire here.

### Security
- The GitHub API endpoint allowlist is now re-validated at the subprocess
  boundary, so no caller can reach `subprocess.run` with an unvalidated
  path (defense in depth; the command already ran as a list without a
  shell).

## [0.7.4] - 2026-09-30

### Fixed
- Reject negative stale-claim decay thresholds with a clear CLI error (#239).

### Changed
- Stale-claim decay, validated half (issue #83): every claimant hit and
  every linked PR now carries an age label in the findings ("expressed
  interest 214 days ago", "open PR #12, last activity 96 days ago") on both
  the REST and GraphQL paths. An open linked PR with no activity past the
  `--pr-idle-days` threshold (default 90) weakens from TAKEN to CAUTION
  instead of blocking as taken; a claim blocks as CAUTION only while its
  claimant was recently active (`--claim-silence-days`, default 7, or
  `--claim-silence-complex-days`, default 14 on complex issues), with the
  clock resetting on any claimant activity. Claim age alone never changes a
  verdict, and a claim never closes anything.
- Authenticated `run_checks` tail stages (claimants, AI policy, repo health)
  now run concurrently, and the repo-health sub-fetches run concurrently
  too; anonymous callers keep the sequential path. Identical verdicts,
  lower wall-clock time.

### Added
- GraphQL is now the default fetch path for logged-in users (`gh`
  authenticated): one query per issue instead of ~10 REST calls, with
  identical verdicts. REST remains the default for anonymous use, the
  automatic per-issue fallback when the GraphQL transport fails (recorded
  in the findings, never silent), and an escape hatch via `--rest` /
  `TAKEN_REST=1`. Transport selection lives in `graphql.fetch_mode`, one
  place for a future budget tier to pick the pipe.
- GraphQL findings now include `"stages_skipped": []` for shape parity with
  the REST path (the GraphQL path always runs every stage).
- Budget-aware engine: the engine now picks a budget tier at startup from
  the authenticated `gh` identity (the same memoized probe `fetch_mode`
  uses, so this costs no extra subprocess). Anonymous callers keep today's
  exact lean behavior (60/hr console budget); authenticated callers get
  deeper comment/timeline scans, more repo-health pages, and a larger
  discover candidate pool (5,000/hr budget). Every run prints a one-line
  budget accounting to stderr, and `--json` / MCP payloads carry a `budget`
  object. All accounting is local; no telemetry.
- README now states what taken is for (in plain terms) before how it works,
  and links the live in-browser console.
- CI now fails if the `taken --version` string in `docs/py/webshim.py`
  drifts from the package version in `pyproject.toml`.
- Contributor onboarding: expanded CONTRIBUTING.md with setup, workflow,
  and local checks; new bug-report and feature-request issue templates;
  PR template gains a short "Verification" section.
- Difficulty-fit heads-up: an issue carrying a beginner-friendly label now
  yields CAUTION (instead of a bare GO) when it also shows heavier signals,
  a long discussion thread (30+ comments) or a design-level label such as
  `needs design` or `rfc`. The reasons are phrased as neutral context for
  the contributor, not as a judgment on the labels.
- New ARCHITECTURE.md: module map, the fetch/decide/present pipeline, and
  the invariants contributors must not break.

### Fixed
- Scan mode no longer refetches issues it already holds: `list_open_issues`
  now returns the full issue items and the CLI, MCP `scan_repo`, and web
  console pass them as `payload=` into the check, skipping the redundant
  per-issue GET on the REST path (the #153 mechanism). Verdicts are
  unchanged; the GraphQL path still issues its single combined query.
- Web console discover no longer aborts on the first failed label search: it
  now mirrors the engine's #162 partial-results behavior, keeping candidates
  from the labels that succeeded, reporting which label searches failed in
  the output, and erroring only when every label search fails.
- REST `gh api` calls now pin `--method GET`: stock `gh` switches to POST
  whenever `-f` parameters are added, which broke every parameterized read
  for PyPI/Homebrew users on a real `gh` CLI. The GraphQL invocation keeps
  its auto-POST (that endpoint only accepts POST).
- Search pacing now actually caps in-flight searches at one: the lock is
  held through the pace wait, the search subprocess, and retries, instead
  of being released before the subprocess ran.
- Discover no longer aborts with zero candidates when the label search
  fails: it falls back to one paced search per label, keeps the candidates
  from the labels that succeeded, and reports which labels failed (CLI
  warning, `DiscoverResults.search_errors`, MCP `search_errors`). When every
  label fails the run still errors instead of returning an empty success.
- Discover reuses each search result's issue fields instead of re-fetching
  the issue over REST: one fewer API call per candidate, up to 40 saved per
  run. The plain `taken owner/repo#123` path is unchanged.
- GraphQL fetch no longer silently drops labels beyond the first 30: the
  labels connection now requests `pageInfo` and paginates (up to 300 labels,
  one or two extra queries worst case). If the cap is ever hit, the run
  reports it as a CAUTION truncation reason through the existing honesty
  machinery instead of silently losing label context.

### Changed
- Scan and batch modes now check issues concurrently: `run_batch` (CLI) and
  `scan_repo` (MCP) verify targets through a worker pool reusing discover's
  ThreadPoolExecutor pattern instead of one at a time. The anonymous tier
  stays sequential (1 worker, today's exact behavior); the authenticated
  tier uses 8 workers. Output order and verdicts are unchanged.

## [0.7.3] - 2026-09-27

### Added
- Opt-in GraphQL fetch paths for the single-issue pipeline: `--graphql`
  (`TAKEN_GRAPHQL=1`) runs one GraphQL query per issue via `gh api
  graphql` instead of ~10 REST calls, and `--persistent-session`
  (`TAKEN_PERSISTENT_SESSION=1`) runs that query over one persistent
  HTTPS keep-alive connection (token from `gh auth token`, held in memory
  only). REST stays the default; both paths produce the same findings
  shape and verdicts. MCP `check_issue` gains matching `graphql` and
  `persistent_session` parameters. See GRAPHQL_NOTES.md for measurements
  and the security tradeoff.
- `--verbose` flag: prints an API usage summary (calls per endpoint,
  cache hits/misses) to stderr at the end of the run.
- `--debug` flag: prints a machine-readable JSON debug report (wall-clock
  timings per phase, rate-limit state before/after, retries, backoff time,
  API usage) to stderr at the end of the run; implies `--verbose`. Only
  counts, timings, and sizes: never tokens or response bodies.
- `taken-mcp` now works out of the box on every install channel: the MCP
  SDK is a required dependency instead of an optional extra, so a plain
  `pip install taken-gh` ships a working server. The `.deb` now vendors
  the full dependency closure for offline installs, and the README and
  MCP registry entries were updated to match. `taken-gh[mcp]` still
  installs fine as a harmless no-op.

### Fixed
- Timeline and comment scans now fail closed when they hit the page cap
  instead of silently truncating results.
- `--discover` exits 3 when every candidate errors, instead of reporting
  a misleading success.
- `--discover` clamps a negative `--limit` to 0 instead of slicing winners
  off the ranked list.
- Error messages that merely contain the digits "404" are no longer
  misclassified as not-found.
- `--me` matching in maintainer-engagement checks is now case-insensitive.
- The API cache is now namespaced by GitHub identity, so switching
  accounts no longer serves stale cross-user results.
- Skipped fetch stages are reported as "not checked" in human output
  instead of being silently omitted.

### Changed
- Single-issue fetches are now ordered cheapest-decisive-first, stopping
  early on a decisive TAKEN to save API calls.
- Ruff rule sets UP (pyupgrade) and B (bugbear) are now enabled.
- CI now guards that the vendored docs pipeline copies stay byte-identical
  with `taken/`.
- Hotspot metric collection hardened against silent drift.

## [0.7.2] - 2026-09-26

### Added
- `.deb` packaging: the publish workflow builds `taken_<ver>_all.deb`
  (offline install via a post-install virtualenv) and attaches it to the
  tag's GitHub release.
- Homebrew tap auto-bump: a workflow opens a formula-bump PR in the tap
  repo after each release tag once the sdist is on PyPI.
- SLSA provenance for release artifacts and REUSE compliance across the
  repo; SonarQube scan now uses least-privilege tokens.

### Fixed
- `--clear-cache` fails closed for `TAKEN_CACHE_DIR` paths outside the
  cache tree instead of deleting them.
- `--discover` no longer aborts the entire run when a single candidate's
  comments fetch fails; the failure is reported per candidate.
- Rate-limit responses now get bounded retries with backoff and jitter,
  honoring `Retry-After` (capped at 120s), instead of failing the run.

### Changed
- The publish-registry job only runs on version tags.
- Test pipeline: mypy in CI, parallel test runs, and an 85% coverage gate.

## [0.7.1] - 2026-09-26

### Added
- MCP `scan_repo` and `discover_candidates` responses now include the
  effective optional parameters used for the query, and their input schemas
  document every default.
- `mcp` is now an optional dependency: plain `taken-gh` installs the CLI
  without the MCP SDK; `pip install taken-gh[mcp]` (or
  `uvx --from "taken-gh[mcp]" taken-mcp`) enables the MCP server.
  Running `taken-mcp` without the extra prints guidance instead of a
  traceback.
- Hotspots badge in the README: per-file cyclomatic complexity x commit churn.
  `scripts/hotspots.py` regenerates `docs/badges/hotspot.json`; a workflow
  opens a refresh PR on every push to main when the numbers change.
- New `--clear-cache` flag: deletes the API response cache
  (`~/.cache/taken`, overridable via `TAKEN_CACHE_DIR`) and reports how
  many entries were cleared. Works as a standalone action:
  `taken --clear-cache` clears and exits.

### Changed
- Repo health now tracks contributors instead of stars: `repo_health` reports
  `contributors` (distinct commit authors in the last 90 days, bots excluded)
  and `contributors_window_days` instead of `stars`. `--discover`'s
  `--min-stars` flag is now `--min-contributors`, and the MCP
  `discover_candidates` parameter `min_stars` is now `min_contributors`.
  Star counts accumulate forever; contributor breadth shows who is actually
  landing changes right now.

### Fixed
- The web console's `taken --version` string in docs/py/webshim.py is
  bumped to 0.7.0 (it was hardcoded to 0.6.0).
- The PyPI badges in README.md and docs/index.html are back to the dynamic
  `img.shields.io/pypi/v/taken-gh` badge. A pinned static badge was tried
  first because shields kept serving a stale cached version (v0.5.0 while
  PyPI was at 0.7.0), then reverted per maintainer preference: the dynamic
  badge lags releases due to shields caching but is self-maintaining and
  needs no per-release bump.
- Web terminal: every http(s) URL printed in the console is now a clickable
  link, not just `owner/repo#123` refs. The link provider scans each line
  for URLs, trims trailing punctuation, and opens the URL in a new tab
  (`noopener`).

## [0.7.0] - 2026-09-26

### Fixed
- `--discover` ranking: candidates with equal scores are now ordered
  most-recently-updated first, matching the documented "score desc, then
  recency desc" order (the old double sort left ties oldest-first).
- Single-issue output (`taken owner/repo#123`) now prints the
  first-time-friendly markers (`first-time friendly:`, `welcoming:`) when
  present, matching what `--discover` lines and the MCP tools already show.
- MCP `check_issue` now returns top-level `friendly_labels` and `welcoming`,
  consistent with `scan_repo` and `discover_candidates`.
- Discover's maintainer-engagement signal now uses the comment's
  `author_association`: only OWNER/MEMBER/COLLABORATOR comments count, so a
  random "+1" no longer earns the +3 "maintainer replied" points. Your own
  `--me` login is also excluded from counting as maintainer engagement.
- `--discover` now searches every requested label: candidates are drawn
  round-robin from each label's results instead of stopping once the first
  label fills the verify pool. The labels searched and their raw candidate
  counts are printed to stderr.
- Web console refreshed: the vendored `docs/py/checks.py` / `verdict.py`
  are byte-copies of main again (they were stale, predating the
  first-time-friendly markers). Web `--discover` lines, repo-scan GO
  recommendations, and single-issue output now show the same
  first-time-friendly / welcoming markers as the CLI, and the web
  maintainer-engagement heuristic uses comment `author_association`
  (OWNER/MEMBER/COLLABORATOR only, `--me` excluded) like the CLI.
- `gh` transport is honest about failures: rate-limit output (HTTP 429 /
  "rate limit exceeded") raises a dedicated `RateLimitError` with the reset
  time when `gh` reports one, and is never retried. Transient 5xx errors get
  at most 2 retries with backoff and jitter (so parallel workers don't
  stampede); everything else fails fast.
- `--discover` tells error-drops apart from verdict-drops: when nothing
  passes verification it reports how many candidates errored, and when all
  of them did it says so explicitly with a hint to check `gh auth status`
  and the network.
- Web console: `owner/repo#123` references in the terminal are now
  clickable links that open the GitHub issue page (hover underlines, plain
  click opens).
- Docs site no longer scrolls sideways on narrow screens: the stylesheet
  now uses `border-box` sizing everywhere, guards with `overflow-x: clip`
  on `html, body`, and keeps badge images within the viewport width.
- Docs site is now a fixed full dark theme (GitHub-dark palette matching
  the terminal demo) instead of following the system light/dark setting.

### Added
- First-time-friendly recommendations: `taken --discover` lines and MCP
  results now carry `friendly_labels` (the issue's own
  first-time-contributor labels, e.g. `good first issue`) and `welcoming`
  (repo-level signs contributions are welcome: a CONTRIBUTING guide,
  recently merged PRs). These come from data the check suite already
  fetches, so they cost no extra API calls. CLI repo scans annotate the GO
  recommendations with friendly labels and list the friendliest first.

## [0.6.0] - 2026-09-26

### Fixed
- Timeline and comment scans now page through up to 5 pages (500 items)
  instead of trusting the first 100 results: a linked PR or a claimant
  comment hiding on a later page of a busy issue could previously flip a
  verdict to GO silently.
- 404 errors now say `not found: <endpoint>` instead of printing the bare
  API endpoint.

### Added
- Smarter repo scans: `taken owner/repo` now ends with a GO-candidate
  recommendation summary ("2 GO candidates: a/b#1, a/b#2", or
  "no GO candidates in this scan").
- Smarter MCP `scan_repo`: results are ordered GO first, then CAUTION,
  then TAKEN, and the payload adds `recommendations` (just the GO
  targets) plus a verdict `summary`, so agents can pick a candidate
  without parsing every verdict.
- MCP Registry publishing is automated: `publish.yml` gained a
  `publish-registry` job that logs in with GitHub OIDC (no stored secrets, no
  device flow) and publishes `server.json` to the official MCP Registry on
  every release tag. `workflow_dispatch` backfills older releases.
- `taken` is now listed in the official MCP Registry as
  `io.github.RogueAlg0/taken`.

## [0.5.0] - 2026-09-26

### Added
- MCP server: `taken-mcp` exposes taken as tools for coding agents
  (`check_issue`, `scan_repo`, `discover_candidates`) over stdio. Run it with
  `uvx --from taken-gh taken-mcp`; like the CLI it uses your own `gh` login
  and only makes read-only API calls.
- `server.json` registry metadata for the official MCP registry
  (`io.github.RogueAlg0/taken`).

## [0.4.1] - 2026-09-26

### Fixed
- Discover-mode cache redesigned: one file per cache key plus an in-memory
  layer instead of a single 28MB JSON file rewritten under a lock. Parallel
  verification no longer serializes on cache writes; a full 40-candidate run
  dropped from ~6.5 minutes sequential to about a minute with 8 workers.

## [0.4.0] - 2026-09-26

### Added
- Discover mode is now parallel: candidates are verified with 8 workers by
  default (`--jobs N` to tune), cutting a full 40-candidate run from minutes
  to under a minute. The API cache is thread-safe (locked, atomic writes).
- Discover mode shows a progress bar on stderr while verifying, so long runs
  give live feedback. `--no-progress` hides it; `--json` output on stdout is
  unaffected.

## [0.3.0] - 2026-09-26

### Added
- Discover mode: `taken --discover` piggybacks on GitHub's issue search API
  (the same source the web aggregators use) for raw candidates, then runs
  taken's full verification on each and ranks the survivors. No aggregator
  filters on the ranking signal that matters most: maintainer responsiveness.
  Only GO verdicts are ranked, scored on maintainer replies (+3), recent
  updates (+2), and repo push activity (+1), with an explainable breakdown
  per candidate. `--language` and `--min-stars` narrow the search;
  `--limit` caps the output (default 10); `--label` restricts to one label
  instead of the default set.

## [0.2.0] - 2026-09-26

### Added
- Batch mode: pass several targets (or `--file`) and get one verdict line
  per target. Exit code is 0 when every target produced a verdict, 3 when
  any target failed.
- Repo scan: a bare `owner/repo` target automatically discovers the repo's
  open issues (PRs excluded, most recently updated first) and checks each
  one. `--limit N` caps the scan (default 20); `--label` filters by label.
- API response cache: successful `gh api` responses are cached for one hour
  in `~/.cache/taken` (`TAKEN_CACHE_DIR` overrides), so repeated scans stay
  cheap. `--no-cache` bypasses it. The cache never breaks the tool: any
  cache error is ignored and the request goes out normally.

## [0.1.2] - 2026-09-26

### Fixed
- `taken --version` now reports the installed distribution version (read
  from package metadata) instead of a stale hardcoded string.

## [0.1.1] - 2026-09-26

### Added
- Published to PyPI as `taken-gh` via trusted publishing (GitHub Actions
  OIDC). Install with `uv tool install taken-gh` or `pipx install taken-gh`;
  README and the project page now point at PyPI instead of a git URL.

## [Unreleased]

### Fixed
- Fail closed: every GitHub API check now validates the shape of the
  response and raises a hard error (exit code 3) on anything unexpected,
  including unreadable CONTRIBUTING files. A failed check can no longer
  degrade quietly into a GO verdict.

### Added
- Refusal-path tests: AI-policy ban detection on a sample CONTRIBUTING file,
  malformed targets (exit code 3 with a clean error, no traceback), and a
  positive test that the --me filter turns an own-comment-only thread into GO.
- CI smoke job: builds the wheel, installs it into a fresh venv, and
  exercises the installed `taken` entry point (--version, --help, and a
  malformed target expecting exit code 3).
- README documents the --json output schema (field names and verdict values).
- README with usage, three real examples, and the verdict rules;
  CONTRIBUTING guide; pull request template.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`): runs `ruff check`,
  `ruff format --check`, and `pytest` on push and pull requests.
- Pytest suite (`tests/`): 31 tests covering the verdict logic (GO, TAKEN,
  CAUTION, and precedence) and the claimant-pattern matching.
- Command-line interface (`taken/cli.py`): `taken owner/repo#123` (full
  issue URLs also accepted), with `--json`, `--me`, `--version`, and
  `--help`. Exit codes 0 (GO), 1 (TAKEN), 2 (CAUTION), 3 (error).
- Verdict logic (`taken/verdict.py`): GO, TAKEN, and CAUTION, with TAKEN
  winning over CAUTION winning over GO. A claimant comment is a soft signal
  (CAUTION); a closed issue, an open linked PR, or an assignee is a hard
  signal (TAKEN).
- GitHub API checks (`taken/checks.py`): issue basics, timeline
  cross-reference scan, claimant language scan, AI policy detection, and repo
  health. All read-only via the `gh` CLI, using the invoker's own auth.
