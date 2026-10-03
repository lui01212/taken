# Contributing to taken

Thanks for stopping by. taken is a small tool with a big job: tell a
contributor whether a GitHub issue is actually up for grabs before they invest
an evening in it. Contributions that keep it honest, fast, and easy to audit
are welcome.

## Ways to contribute

- **Code**: bug fixes and small features. One concern per PR keeps reviews fast.
- **Docs**: the README, this file, and the docs site. If behavior changes, the
  docs change in the same PR.
- **Bug reports and ideas**: open an issue with the template. A good report
  says what you ran, what you expected, and what happened instead.

## Getting started

1. Fork the repo and clone your fork.
2. `uv sync` to set up the environment (Python 3.10+).
3. Install the [GitHub CLI](https://cli.github.com/) and run `gh auth login`.
   taken shells out to `gh api` with your own credentials, so the tool cannot
   do anything you could not do yourself.
4. `uv run taken --help` to see it working.
5. Read [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and the
   invariants to keep intact.

## Workflow

- Create a branch from `main` with a short descriptive name
  (`fix/123-short-desc`).
- Make the change, add or update tests in `tests/`, and run the checks below.
- Open a PR against `main` from your fork. Fill in the PR template: what
  changed, why, and the checklist.
- CI runs the test suite on Python 3.10 through 3.13, plus ruff, mypy, and a
  smoke test. Everything goes green before merge.

## Before you push

Run these three commands. CI runs the same ones:

- `uv run ruff check`
- `uv run ruff format --check`
- `uv run pytest`
- `uv run --frozen mypy taken/`

## Two gotchas

- **Vendored docs copies.** `docs/console/py/checks.py` and `docs/console/py/verdict.py` are
  byte-copies of `taken/checks.py` and `taken/verdict.py` for the in-browser
  docs console. If you touch the pipeline files, re-copy them into `docs/console/py/`.
  CI checks the copies are in sync and fails the PR otherwise.
- **The tool is read-only by design.** It must never write anything to the
  GitHub API: no comments, no labels, no state changes, only GET requests.
  Keep it that way.

## Style

- Keep runtime dependencies minimal. `tqdm` (progress bar) and `mcp` (the MCP
  server SDK) are the only two; anything new must justify its weight. Dev
  tools (ruff, pytest) live in the uv dev group.
- Small functions, plain names, no cleverness. The verdict logic in
  `taken/verdict.py` should stay easy to audit.
- If you change verdict behavior, update the tests in `tests/` and the "How
  the verdict works" section of the README.
