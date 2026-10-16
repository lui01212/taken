# taken?

[![PyPI](https://img.shields.io/pypi/v/taken-gh?cacheSeconds=3600)](https://pypi.org/project/taken-gh/)
[![npm](https://img.shields.io/npm/v/taken-gh?cacheSeconds=3600)](https://www.npmjs.com/package/taken-gh)
[![CI](https://github.com/RogueAlg0/taken/actions/workflows/ci.yml/badge.svg)](https://github.com/RogueAlg0/taken/actions)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/RogueAlg0/taken/badge)](https://scorecard.dev/viewer/?uri=github.com/RogueAlg0/taken)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/14960/badge)](https://www.bestpractices.dev/projects/14960)
[![SonarCloud Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=RogueAlg0_taken&metric=alert_status)](https://sonarcloud.io/project/overview?id=RogueAlg0_taken)
[![License: MIT](https://img.shields.io/github/license/RogueAlg0/taken)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://pypi.org/project/taken-gh/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.RogueAlg0%2Ftaken-blue)](https://registry.modelcontextprotocol.io)

<!-- mcp-name: io.github.RogueAlg0/taken -->

`taken?` answers one question before you volunteer for a GitHub issue: is it
already taken?

In plain terms, it checks an issue before you start work: is it still open to
contributions, is anyone already on it, and is the repo responsive enough that
your PR will get reviewed.

Try it without installing anything: the [live console](https://roguealg0.github.io/taken/console/)
runs the real checks in your browser.

The catch it was built for: GitHub shows "linked a pull request" events in the
issue timeline, but never in the comments. You can read every comment on an
issue and still miss that someone already opened a PR for it. `taken?` checks
the timeline, scans comments for people claiming the work, looks at assignees,
reads CONTRIBUTING.md for AI contribution policies, and checks whether the
repo is still active. Then it gives a verdict, GO, TAKEN, or CAUTION, with the
evidence cited.

## Requirements

- Python 3.10 or newer
- The [GitHub CLI](https://cli.github.com/) (`gh`), authenticated

`taken?` only makes read-only API calls through your own `gh` login. It never
sees or stores tokens, and it never writes anything to GitHub.

## Install

    uv tool install taken-gh

Or with pipx:

    pipx install taken-gh

Then:

    taken owner/repo#123

A full issue URL works too:

    taken https://github.com/owner/repo/issues/123

## The verdict

TAKEN wins over CAUTION, which wins over GO.

- TAKEN: the issue is closed, an open PR links to it, or someone is assigned.
- CAUTION: a comment says someone wants it (soft claim, they may have moved
  on), a linked PR was merged but the issue is still open, the repo has an AI
  policy to respect, the repo looks inactive, or a beginner-friendly label
  sits alongside a long discussion thread or a design-level label (worth
  skimming before you start).
- GO: none of the above.

The claimant scan is a heuristic over comment text, not proof. The verdict
always prints its evidence so you can judge for yourself.

Exit codes: 0 means GO, 1 means TAKEN, 2 means CAUTION, 3 means something
broke (bad target or flag, no `gh`, API error).

## Examples

An issue with a PR already fixing it:

    $ taken Exodus-Privacy/exodus#300
    taken? Exodus-Privacy/exodus#300
    verdict: TAKEN
      ...
      why:
        - open PR #700 already covers this: https://github.com/Exodus-Privacy/exodus/pull/700

An issue someone is already assigned to:

    $ taken chahe-dridi/vscode-agent-bell#200
    taken? chahe-dridi/vscode-agent-bell#200
    verdict: TAKEN
      ...
      why:
        - assigned to: dk5488

A clean issue, with your own volunteering comment filtered out:

    $ taken snowflakedb/snowflake-cli#3145 --me RogueAlg0
    taken? snowflakedb/snowflake-cli#3145
    verdict: GO
      ...
      why:
        - no linked PRs, no assignees, no claimants, repo is active

## Useful flags

- `--json`: print the full findings as JSON instead of the human summary
- `--me LOGIN`: ignore your own comments when scanning for claimants
- `--no-cache`: bypass the one-hour API response cache
- `--rest`: force the REST fetch path (the default is GraphQL for logged-in users)
- `--health`: maintainer report for one repo instead of a contributor verdict

`taken --help` lists everything.

## Batch mode and repo scans

Give `taken` several targets and it prints one verdict line per target:

    taken owner/repo#123 owner/repo#124 --me mylogin
    TAKEN   owner/repo#123  assigned to: dk5488
    GO      owner/repo#124  no linked PRs, no assignees, no claimants, repo is active

A bare `owner/repo` scans the repo automatically: its open issues
(most recently updated first, PRs excluded) are each checked:

    taken django/django --label "good first issue" --limit 10

API responses are cached for one hour in `~/.cache/taken`
(override with `TAKEN_CACHE_DIR`), so repeated scans stay cheap.

The opt-in REST transport (`TAKEN_TRANSPORT=httpx`) preserves both JSON
objects and arrays, including empty listings. Response ETags are cached
alongside either shape: after expiry, a `304 Not Modified` reuses the cached
body and refreshes its TTL. The default subprocess transport is unchanged.

## Discover mode

`taken --discover` finds contribution candidates across GitHub: it takes raw
candidates from GitHub's issue search, runs taken's full verification on each
one, and ranks the survivors. Only GO verdicts make the list, scored on the
signal no aggregator filters on: maintainer responsiveness.

    taken --discover --language python --min-contributors 3 --limit 10
      6  octocat/hello-world#42  maintainer replied; updated 1d ago; repo pushed 0d ago
      4  octocat/other-repo#7    updated 3d ago; repo pushed 2d ago

Each line explains its own score. Candidates are verified in parallel
(8 workers by default; `--jobs N` tunes it).

## MCP server

`taken` also ships as an MCP server, so coding agents can check issues with a
tool call instead of shelling out to the CLI. Same setup: `gh` installed and
authenticated, read-only API calls through your own login.

    uvx --from "taken-gh[mcp]" taken-mcp

Three tools: `check_issue`, `scan_repo`, `discover_candidates`. Published in
the official [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.RogueAlg0/taken`.

## Development

    uv sync          # install dev tools (ruff, pytest)
    uv run pytest
    uv run ruff check
    uv run ruff format --check

## AI assistance

This project is built with AI assistance, and says so openly. Every
contribution is reviewed and understood by its author before it lands.

## Security

See [SECURITY.md](SECURITY.md) for the supported versions and how to report
a vulnerability. Please use private vulnerability reporting, not a public
issue.
