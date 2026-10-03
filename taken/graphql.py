"""GraphQL fetch path for taken.

Produces the exact same findings-dict shape as ``checks.run_checks`` so
``verdict.decide()`` is untouched. Two transports:

- ``"graphql"``: one GraphQL query per issue via the ``gh api graphql``
  subprocess. Same auth model as the REST path: taken never sees tokens.
- ``"persistent"``: the same query over one persistent HTTPS keep-alive
  connection held for the process lifetime. The token comes from
  ``gh auth token`` once at startup, is held in memory only, and is never
  logged or written to disk. Explicit opt-in only.

GraphQL is the default for authenticated invokers (``gh`` logged in);
REST remains the default for anonymous use and is always available as an
escape hatch (``--rest`` / ``TAKEN_REST=1``) and as the automatic fallback
when the GraphQL transport fails for a check.
"""

import atexit
import hashlib
import http.client
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from taken import budget, checks

# Fail-closed pagination ceilings, mirroring the REST path's scan depth:
# comments/timeline up to 500 items (5 x 100), commits up to 300 (3 x 100),
# merged-PR scan up to 2 pages of 50. These are baselines: the
# authenticated budget tier may raise them (see taken/budget.py).
_MAX_COMMENT_PAGES = 5
_MAX_TIMELINE_PAGES = 5
_ISSUE_QUERY_LABEL = "issue query"
_GITHUB_API_HOST = "api.github.com"
_UTC_SUFFIX = "+00:00"
_MAX_HISTORY_PAGES = 3
_MAX_MERGE_PAGES = 2
# Labels are tiny: 3 pages x 100 covers 300 labels, far beyond any
# realistic issue, at one or two extra queries worst case.
_MAX_LABEL_PAGES = 3
_PAGE_SIZE = 100

GRAPHQL_TIMEOUT = 60

_RATE_LIMITED_RE = re.compile(r"RATE_LIMITED", re.IGNORECASE)

ISSUE_QUERY = """
query IssueVerdict(
  $owner: String!, $repo: String!, $number: Int!, $since: GitTimestamp,
  $commentsAfter: String, $timelineAfter: String, $historyAfter: String, $prsAfter: String,
  $labelsAfter: String
) {
  repository(owner: $owner, name: $repo) {
    pushedAt
    issue(number: $number) {
      state
      title
      url
      createdAt
      author { login }
      assignees(first: 20) { nodes { login } }
      labels(first: 100, after: $labelsAfter) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { name }
      }
      comments(first: 100, after: $commentsAfter) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { author { login } body createdAt url }
      }
      timelineItems(first: 100, after: $timelineAfter,
        itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source { __typename ... on PullRequest {
              number title state mergedAt updatedAt url author { login }
              repository { nameWithOwner } } }
          }
          ... on ConnectedEvent {
            source { __typename ... on PullRequest {
              number title state mergedAt updatedAt url author { login }
              repository { nameWithOwner } } }
          }
        }
      }
    }
    ai1: object(expression: "HEAD:CONTRIBUTING.md") { ... on Blob { text } }
    ai2: object(expression: "HEAD:.github/CONTRIBUTING.md") { ... on Blob { text } }
    ai3: object(expression: "HEAD:docs/CONTRIBUTING.md") { ... on Blob { text } }
    ai4: object(expression: "HEAD:CONTRIBUTING.rst") { ... on Blob { text } }
    mergedPRs: pullRequests(states: MERGED, first: 50, after: $prsAfter,
        orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { mergedAt }
    }
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $historyAfter, since: $since) {
            pageInfo { hasNextPage endCursor }
            nodes { author { user { login } email } }
          }
        }
      }
    }
  }
  rateLimit { limit cost remaining resetAt }
}
"""


def _is_authenticated():
    """True when the invoker is logged in to GitHub via `gh`.

    Reuses the memoized identity probe from checks, so this costs one
    subprocess per process at most. A failed probe means unauthenticated,
    and REST stays the safe default.
    """
    try:
        return checks._github_identity() is not None
    except Exception:
        return False


def fetch_mode(args=None):
    """Resolve which fetch path to use: "rest" | "graphql" | "persistent".

    Selection order (first match wins):

    1. explicit persistent (``--persistent-session`` /
       ``TAKEN_PERSISTENT_SESSION=1``)
    2. explicit rest (``--rest`` / ``TAKEN_REST=1``): the escape hatch, and
       it beats ``--graphql`` so there is always a way to force REST
    3. explicit graphql (``--graphql`` / ``TAKEN_GRAPHQL=1``)
    4. authenticated invoker -> ``"graphql"`` (the default for logged-in users)
    5. otherwise ``"rest"`` (the anonymous / console tier stays on REST)

    This is the single place that maps "what the caller asked for" to a
    transport, so a future budget tier can pick the pipe here.
    """
    flag_graphql = bool(args and getattr(args, "graphql", False))
    flag_persistent = bool(args and getattr(args, "persistent_session", False))
    flag_rest = bool(args and getattr(args, "rest", False))
    if flag_persistent or os.environ.get("TAKEN_PERSISTENT_SESSION") == "1":
        return "persistent"
    if flag_rest or os.environ.get("TAKEN_REST") == "1":
        return "rest"
    if flag_graphql or os.environ.get("TAKEN_GRAPHQL") == "1":
        return "graphql"
    if _is_authenticated():
        return "graphql"
    return "rest"


def _raise_for_errors(payload, what):
    """Fail closed on a GraphQL `errors` array.

    RATE_LIMITED becomes RateLimitError so the retry policy applies;
    anything else becomes TakenError naming the error type. Partial
    `data` alongside errors is treated as failure, never silently used.
    """
    errors = payload.get("errors") or []
    if not errors:
        return
    kinds = []
    for err in errors:
        kind = (err.get("type") or err.get("code") or "UNKNOWN").upper()
        kinds.append(kind)
        if _RATE_LIMITED_RE.search(kind) or _RATE_LIMITED_RE.search(err.get("message") or ""):
            raise checks.RateLimitError(
                f"GitHub GraphQL rate limit hit for {what}. "
                "Check `gh api rate_limit` for the reset time. No verdict was recorded."
            )
    raise checks.TakenError(f"GitHub GraphQL query failed for {what}: {', '.join(kinds)}")


def _cache_key_for(query, variables):
    digest = hashlib.sha256(
        json.dumps({"q": query, "v": variables}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return checks._cache_key("graphql", {"body": digest})


def _cached_or_fetch(query, variables, fetcher):
    key = _cache_key_for(query, variables)
    if checks._CACHE_ENABLED:
        cache_start = time.perf_counter()
        cached = checks._cache_read(key)
        checks.record_phase("cache", time.perf_counter() - cache_start)
        if cached is not None:
            checks.record_cache_result(True)
            return cached
        checks.record_cache_result(False)
    checks.record_api_call("graphql")
    payload = fetcher()
    _raise_for_errors(payload, _ISSUE_QUERY_LABEL)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise checks.TakenError("GitHub GraphQL query returned an unexpected response")
    if checks._CACHE_ENABLED:
        checks._cache_write(key, payload)
    return payload


def _build_gql_cmd(query, variables):
    """Build the `gh api graphql` argv, skipping null variables."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in (variables or {}).items():
        if value is None:
            continue
        cmd.extend(["-F", f"{name}={value}"])
    return cmd


def _run_gql_attempt(cmd):
    """Run one `gh api graphql` subprocess; return the payload dict.

    Raises TakenError on transport or parse failures, RateLimitError when
    stderr signals a rate limit.
    """
    try:
        gql_start = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GRAPHQL_TIMEOUT)
        checks.record_phase("graphql", time.perf_counter() - gql_start)
    except FileNotFoundError:
        raise checks.TakenError("the `gh` CLI is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise checks.TakenError(f"`gh api graphql` timed out after {GRAPHQL_TIMEOUT}s") from None
    checks.record_bytes(len((proc.stdout or "").encode("utf-8")))
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload.get("errors"):
        _raise_for_errors(payload, _ISSUE_QUERY_LABEL)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if checks._is_rate_limited(err):
            raise checks.RateLimitError(checks._rate_limit_message("graphql", err))
        detail = err or (proc.stdout or "").strip() or f"exit status {proc.returncode}"
        raise checks.TakenError(f"`gh api graphql` failed: {detail[:300]}")
    return payload


def _fetch_with_retries(query, variables, cmd):
    """Fetch through the cache with bounded rate-limit retries and backoff."""
    attempt = 0
    while True:
        try:
            return _cached_or_fetch(query, variables, lambda: _run_gql_attempt(cmd))
        except checks.RateLimitError:
            attempt += 1
            if attempt >= checks.RETRY_ATTEMPTS:
                raise
            delay = checks.RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            checks.record_retry(delay)
            time.sleep(delay)


def graphql_via_gh(query, variables):
    """POST one GraphQL query via the `gh api graphql` subprocess (path B).

    Mirrors checks.gh_api's retry discipline: RATE_LIMITED responses get
    bounded retries with backoff and jitter.
    """
    return _fetch_with_retries(query, variables, _build_gql_cmd(query, variables))


def _gh_auth_token():
    """Read a token from `gh auth token`. Called once per session.

    Raises TakenError (never a raw traceback) when `gh` is missing,
    hangs, or cannot run.
    """
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise checks.TakenError(
            "the `gh` CLI is not installed or not on PATH; "
            "the persistent session needs `gh auth token`"
        ) from None
    except subprocess.TimeoutExpired:
        raise checks.TakenError(
            "`gh auth token` timed out after 30s; "
            "the persistent session needs a responsive authenticated `gh`"
        ) from None
    except OSError as exc:
        raise checks.TakenError(f"`gh auth token` could not run: {exc}") from None
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        raise checks.TakenError(
            "`gh auth token` failed; the persistent session needs an "
            "authenticated `gh`. Fall back to `--graphql` (subprocess) or REST."
        )
    return token


class _ConnectionLost(Exception):
    """Internal: the keep-alive connection died; safe to reconnect and retry."""


class PersistentGraphQLSession:
    """GraphQL over one persistent HTTPS keep-alive connection (path C).

    The token is obtained from ``token_provider`` (default: ``gh auth
    token``) exactly once, held in memory only, and never logged or
    written to disk. Only the response bodies go through the cache.
    """

    def __init__(self, token_provider: Callable[[], str] | None = None):
        self._token_provider = token_provider or _gh_auth_token
        self._token: str | None = None
        self._conn: http.client.HTTPSConnection | None = None
        self.calls = 0  # introspection hook for tests/smoke runs

    def _ensure(self):
        if self._token is None:
            self._token = self._token_provider()
        if self._conn is None:
            self._conn = self._connect()

    @staticmethod
    def _connect():
        """HTTPS connection to api.github.com, honoring proxy env vars.

        Standard CONNECT tunneling: TLS is negotiated end-to-end through
        the proxy, so keep-alive and per-request auth keep working.
        """
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy_url and not urllib.request.proxy_bypass(_GITHUB_API_HOST):
            parsed = urllib.parse.urlparse(proxy_url)
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=GRAPHQL_TIMEOUT
            )
            conn.set_tunnel(_GITHUB_API_HOST, 443)
            return conn
        return http.client.HTTPSConnection(_GITHUB_API_HOST, timeout=GRAPHQL_TIMEOUT)

    def post(self, query, variables):
        """POST one GraphQL query, reconnecting once if the tunnel died.

        Retrying is safe: the queries issued here are read-only.
        """
        try:
            return self._post_once(query, variables)
        except _ConnectionLost:
            self._drop()
            self._ensure()
            try:
                return self._post_once(query, variables)
            except _ConnectionLost as exc:
                self._drop()
                raise checks.TakenError(f"persistent GraphQL connection failed: {exc}") from exc

    def _drop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def _post_once(self, query, variables):
        self._ensure()
        assert self._conn is not None and self._token is not None
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            # The token lives only in this header value, in memory.
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "taken-graphql-persistent",
        }
        self.calls += 1
        gql_start = time.perf_counter()
        try:
            self._conn.request("POST", "/graphql", body=body, headers=headers)
            resp = self._conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            # Proxies and servers close idle keep-alive connections; the
            # caller reconnects and retries once.
            raise _ConnectionLost(f"persistent GraphQL connection dropped: {exc}") from exc
        finally:
            checks.record_phase("graphql", time.perf_counter() - gql_start)
        checks.record_bytes(len(raw or b""))
        if resp.status == 429:
            raise checks.RateLimitError(
                "GitHub GraphQL rate limit hit (HTTP 429 on persistent session). "
                "Check `gh api rate_limit` for the reset time. No verdict was recorded."
            )
        if resp.status >= 400:
            raise checks.TakenError(f"persistent GraphQL request failed: HTTP {resp.status}")
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise checks.TakenError("persistent GraphQL response was not JSON") from None

    def query(self, query, variables):
        """POST with the same retry discipline as the subprocess path."""
        attempt = 0
        while True:
            try:
                return _cached_or_fetch(
                    query, variables, lambda: self._checked_post(query, variables)
                )
            except checks.RateLimitError:
                attempt += 1
                if attempt >= checks.RETRY_ATTEMPTS:
                    raise
                delay = checks.RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                checks.record_retry(delay)
                time.sleep(delay)

    def _checked_post(self, query, variables):
        payload = self.post(query, variables)
        _raise_for_errors(payload, _ISSUE_QUERY_LABEL)
        if not isinstance(payload.get("data"), dict):
            raise checks.TakenError("persistent GraphQL query returned an unexpected response")
        return payload

    def close(self):
        self._drop()


_SESSION: PersistentGraphQLSession | None = None
_SESSION_LOCK = threading.Lock()


def get_session():
    """Process-wide persistent session, created once on first use.

    Double-checked locking, the same pattern as budget.activate: two
    threads racing first use get one session instead of two (the loser
    would leak its keep-alive connection).
    """
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = PersistentGraphQLSession()
    return _SESSION


def _close_persistent_sessions():
    """Close the process-wide session at interpreter exit (atexit hook).

    Keep-alive connections must not outlive the process and wait on GC.
    Shutdown failures are swallowed: there is nothing useful to do with
    them, and a traceback at exit would mask the real result.
    """
    global _SESSION
    session, _SESSION = _SESSION, None
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


atexit.register(_close_persistent_sessions)


_thread_state = threading.local()


def thread_session():
    """One persistent GraphQL session per calling thread.

    ``get_session()`` shares a single keep-alive connection, which is not
    safe to use from pool worker threads; each thread keeps its own
    session (and its own connection) instead. The token is still read once
    per thread from ``gh auth token`` and held in memory only.
    """
    session = getattr(_thread_state, "graphql_session", None)
    if session is None:
        session = PersistentGraphQLSession()
        _thread_state.graphql_session = session
    return session


# ---------------------------------------------------------------------------
# Findings mapping: GraphQL response -> checks.run_checks findings shape.
# ---------------------------------------------------------------------------


def _map_issue(issue_node, number):
    if issue_node is None:
        raise checks.NotFoundError("issue not found")
    author = issue_node.get("author") or {}
    comments = issue_node.get("comments") or {}
    labels = issue_node.get("labels") or {}
    return {
        "number": number,
        "state": (issue_node.get("state") or "").lower(),
        "title": issue_node.get("title"),
        "labels": [n.get("name") for n in labels.get("nodes", [])],
        "assignees": [n.get("login") for n in (issue_node.get("assignees") or {}).get("nodes", [])],
        "comment_count": comments.get("totalCount", 0),
        "author": author.get("login"),
        "url": issue_node.get("url"),
        "created_at": issue_node.get("createdAt"),
        "_comment_nodes": comments.get("nodes", []),
        "_comment_page": comments.get("pageInfo") or {},
        "_label_nodes": labels.get("nodes", []),
        "_label_page": labels.get("pageInfo") or {},
    }


def _map_linked_prs(timeline_nodes):
    linked = []
    seen = set()
    for event in timeline_nodes or []:
        src = event.get("source") or {}
        if src.get("__typename") != "PullRequest":
            continue
        repo_name = (src.get("repository") or {}).get("nameWithOwner") or ""
        key = (repo_name, src.get("number"))
        if key in seen:
            continue
        seen.add(key)
        author = src.get("author") or {}
        # REST reports merged PRs as state "closed" + merged flag; GraphQL
        # has a distinct MERGED enum value. Normalize for shape parity.
        state = (src.get("state") or "").lower()
        if state == "merged":
            state = "closed"
        linked.append(
            {
                "number": src.get("number"),
                "title": src.get("title"),
                "state": state,
                "merged": bool(src.get("mergedAt")),
                "author": author.get("login"),
                "url": src.get("url"),
                # Shape parity with the REST path (issue #83): every linked
                # PR carries its age so decide() can weaken idle ones.
                "updated_at": src.get("updatedAt"),
                "idle_days": checks.days_since(src.get("updatedAt")),
                "age_label": "",
            }
        )
        linked[-1]["age_label"] = checks.pr_age_label(linked[-1])
    return linked


def _map_comments_to_rest_shape(nodes):
    """Shape GraphQL comment nodes like REST comment payloads for find_claimant_hits."""
    shaped = []
    for node in nodes or []:
        author = node.get("author") or {}
        shaped.append(
            {
                "user": {"login": author.get("login") or ""},
                "body": node.get("body") or "",
                "created_at": node.get("createdAt") or "",
                "html_url": node.get("url"),
            }
        )
    return shaped


_AI_PATHS = [
    ("ai1", "CONTRIBUTING.md"),
    ("ai2", ".github/CONTRIBUTING.md"),
    ("ai3", "docs/CONTRIBUTING.md"),
    ("ai4", "CONTRIBUTING.rst"),
]


def _map_ai_policy(repository):
    for alias, path in _AI_PATHS:
        blob = repository.get(alias)
        if not isinstance(blob, dict):
            continue
        verdict, snippet = checks.classify_policy(blob.get("text") or "")
        return {"verdict": verdict, "snippet": snippet, "source": path}
    return {"verdict": "none-found", "snippet": "", "source": None}


def _pushed_recency(repository, window_days):
    """Return (pushed_at, pushed_recently) for the repository payload."""
    pushed_at = repository.get("pushedAt") or ""
    if not pushed_at:
        return pushed_at, False
    pushed_dt = checks._parse_ts(pushed_at)
    if pushed_dt is None:
        return pushed_at, False
    recent = datetime.now(timezone.utc) - pushed_dt <= timedelta(days=window_days)
    return pushed_at, recent


def _count_recent_merges(repository, cutoff):
    """Count merged PRs merged at or after the cutoff datetime."""
    recent_merges = 0
    for pr in (repository.get("mergedPRs") or {}).get("nodes", []):
        merged_at = pr.get("mergedAt")
        if not merged_at:
            continue
        merged_dt = checks._parse_ts(merged_at)
        if merged_dt is not None and merged_dt >= cutoff:
            recent_merges += 1
    return recent_merges


def _commit_author_key(commit):
    """Dedup key for a commit author: login, else email, else None.

    Bot logins return None so automation never counts as a contributor.
    """
    author = commit.get("author") or {}
    user = author.get("user") or {}
    login = user.get("login") or ""
    if login:
        if login.endswith("[bot]"):
            return None
        return login.lower()
    email = author.get("email") or ""
    return email.lower() if email else None


def _collect_contributors(repository):
    """Logins/emails of human commit authors on the default branch."""
    authors = set()
    history = ((repository.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}
    for commit in history.get("nodes", []):
        key = _commit_author_key(commit)
        if key is not None:
            authors.add(key)
    return authors


def _map_repo_health(repository, window_days=checks.HEALTH_WINDOW_DAYS):
    pushed_at, pushed_recently = _pushed_recency(repository, window_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    authors = _collect_contributors(repository)
    return {
        "pushed_at": pushed_at[:10],
        "pushed_recently": pushed_recently,
        "recent_merges": _count_recent_merges(repository, cutoff),
        "contributors": len(authors),
        "contributors_window_days": checks.CONTRIBUTORS_WINDOW_DAYS,
    }


def _paginate(connection, fetch_next, variables, after_key, max_pages):
    """Follow cursor pagination for one connection, up to max_pages total.

    Returns (nodes, truncated). truncated is True when the connection still
    reports hasNextPage after the page cap, meaning nodes beyond the cap
    were never fetched. `fetch_next(new_variables)` must return the next
    page's connection dict.
    """
    nodes = list(connection.get("nodes", []))
    page_info = connection.get("pageInfo") or {}
    pages = 1
    while page_info.get("hasNextPage") and pages < max_pages:
        variables = {**variables, after_key: page_info.get("endCursor")}
        connection = fetch_next(variables)
        nodes.extend(connection.get("nodes", []))
        page_info = connection.get("pageInfo") or {}
        pages += 1
    return nodes, bool(page_info.get("hasNextPage"))


def _select_fetch(mode, session):
    """Return the (query, variables) -> payload callable for the mode."""
    if mode == "persistent":
        sess = session or get_session()
        return lambda q, v: sess.query(q, v)
    return lambda q, v: graphql_via_gh(q, v)


def _fetch_repository(fetch, variables, owner, repo):
    """Fetch the repository payload; raise NotFoundError when absent."""
    payload = fetch(ISSUE_QUERY, variables)
    repository = payload["data"].get("repository")
    if repository is None:
        raise checks.NotFoundError(f"not found: repos/{owner}/{repo}")
    return repository


def _refetch(fetch, new_variables):
    """Re-run the issue query; return the repository payload."""
    return fetch(ISSUE_QUERY, new_variables)["data"]["repository"]


def _paginate_comments(issue, fetch, variables):
    """Paginate issue comments; return (nodes, truncated)."""
    comment_nodes = list(issue.pop("_comment_nodes"))
    comment_page = issue.pop("_comment_page")
    if not comment_page.get("hasNextPage"):
        return comment_nodes, False
    return _paginate(
        {"nodes": comment_nodes, "pageInfo": comment_page},
        lambda v: _refetch(fetch, v)["issue"]["comments"],
        variables,
        "commentsAfter",
        budget.effective_cap(_MAX_COMMENT_PAGES, "gql_comment_pages"),
    )


def _paginate_labels(issue, fetch, variables):
    """Paginate issue labels in place; return labels_truncated."""
    label_nodes = list(issue.pop("_label_nodes"))
    label_page = issue.pop("_label_page")
    if not label_page.get("hasNextPage"):
        return False
    label_nodes, labels_truncated = _paginate(
        {"nodes": label_nodes, "pageInfo": label_page},
        lambda v: _refetch(fetch, v)["issue"]["labels"],
        variables,
        "labelsAfter",
        budget.effective_cap(_MAX_LABEL_PAGES, "gql_label_pages"),
    )
    issue["labels"] = [n.get("name") for n in label_nodes]
    return labels_truncated


def _paginate_timeline(issue_node, fetch, variables):
    """Paginate timeline items; return (nodes, truncated)."""
    timeline_conn = issue_node.get("timelineItems") or {}
    timeline_nodes = list(timeline_conn.get("nodes", []))
    if not (timeline_conn.get("pageInfo") or {}).get("hasNextPage"):
        return timeline_nodes, False
    return _paginate(
        timeline_conn,
        lambda v: _refetch(fetch, v)["issue"]["timelineItems"],
        variables,
        "timelineAfter",
        budget.effective_cap(_MAX_TIMELINE_PAGES, "gql_timeline_pages"),
    )


def _paginate_history(repository, fetch, variables):
    """Paginate default-branch history; return repository with full history."""
    branch_target = (repository.get("defaultBranchRef") or {}).get("target") or {}
    history = branch_target.get("history") or {}
    if not (history.get("pageInfo") or {}).get("hasNextPage"):
        return repository
    history_nodes, _ = _paginate(
        history,
        lambda v: (
            ((_refetch(fetch, v).get("defaultBranchRef") or {}).get("target") or {}).get("history")
            or {}
        ),
        variables,
        "historyAfter",
        budget.effective_cap(_MAX_HISTORY_PAGES, "gql_history_pages"),
    )
    history = {**history, "nodes": history_nodes}
    return {
        **repository,
        "defaultBranchRef": {
            **(repository.get("defaultBranchRef") or {}),
            "target": {**branch_target, "history": history},
        },
    }


def _paginate_merged_prs(repository, fetch, variables):
    """Paginate merged PRs until the health-window cutoff; return repository."""
    merged = repository.get("mergedPRs") or {}
    merged_page = merged.get("pageInfo") or {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=checks.HEALTH_WINDOW_DAYS)
    pages = 1
    while (
        merged_page.get("hasNextPage")
        and pages < budget.effective_cap(_MAX_MERGE_PAGES, "gql_merge_pages")
        and _oldest_merged_at(merged) >= cutoff
    ):
        variables = {**variables, "prsAfter": merged_page.get("endCursor")}
        merged = _refetch(fetch, variables)["mergedPRs"]
        repository = {
            **repository,
            "mergedPRs": {
                "nodes": (repository.get("mergedPRs") or {}).get("nodes", [])
                + merged.get("nodes", []),
                "pageInfo": merged.get("pageInfo", {}),
            },
        }
        merged = repository["mergedPRs"]
        merged_page = merged.get("pageInfo") or {}
        pages += 1
    return repository


def run_checks_graphql(owner, repo, number, me=None, mode="graphql", session=None, thresholds=None):
    """Run the check suite via GraphQL; return findings like checks.run_checks.

    `thresholds` carries the stale-claim decay settings (issue #83); None
    means the defaults from checks.default_thresholds()."""
    fetch = _select_fetch(mode, session)
    since = (datetime.now(timezone.utc) - timedelta(days=checks.CONTRIBUTORS_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    variables: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "number": number,
        "since": since,
    }
    repository = _fetch_repository(fetch, variables, owner, repo)
    issue = _map_issue(repository.get("issue"), number)

    # Paginate connections that can exceed the first page (rare).
    comment_nodes, comments_truncated = _paginate_comments(issue, fetch, variables)

    # Labels paginate like comments/timeline: an issue can carry more
    # labels than one page holds, and a design-level label past the
    # first page is CAUTION context we must not silently drop.
    labels_truncated = _paginate_labels(issue, fetch, variables)

    issue_node = repository.get("issue") or {}
    timeline_nodes, timeline_truncated = _paginate_timeline(issue_node, fetch, variables)

    repository = _paginate_history(repository, fetch, variables)
    repository = _paginate_merged_prs(repository, fetch, variables)

    return {
        "target": f"{owner}/{repo}#{number}",
        "issue": issue,
        "linked_prs": _map_linked_prs(timeline_nodes),
        "claimants": checks.find_claimant_hits(_map_comments_to_rest_shape(comment_nodes), me=me),
        # Parity with checks.run_checks: stale-claim decay settings (issue #83).
        "thresholds": thresholds or checks.default_thresholds(),
        "ai_policy": _map_ai_policy(repository),
        "repo_health": _map_repo_health(repository),
        # Parity with checks.run_checks: which evidence scans stopped early.
        "scan_truncated": {
            "timeline": timeline_truncated,
            "comments": comments_truncated,
            "labels": labels_truncated,
        },
        # The GraphQL path always runs every stage (no cheapest-first early
        # stop), so the honest value is the empty list.
        "stages_skipped": [],
    }


def run_checks_with_fallback(
    owner, repo, number, me=None, mode="graphql", session=None, payload=None, thresholds=None
):
    """Run the check suite, falling back from GraphQL to REST on failure.

    GraphQL is the default transport for authenticated invokers, but it
    must never fail louder than REST can recover: any transport-level
    failure (auth, rate limit, schema error, timeout) retries the check
    over REST for that issue. The fallback is recorded in the findings so
    it is never silent, and a fallback verdict carries no more confidence
    than the REST evidence behind it.

    ``NotFoundError`` is not a transport failure (the issue is absent on
    both paths) and is re-raised without a fallback attempt.

    `payload` is a pre-fetched REST issue item (e.g. from
    list_open_issues): on the REST path it skips check_issue()'s redundant
    GET (issue #211). The GraphQL path issues one combined query per
    issue, so a REST item cannot substitute for it and the payload is
    ignored there.
    """
    if mode not in ("graphql", "persistent"):
        findings = checks.run_checks(
            owner, repo, number, me=me, payload=payload, thresholds=thresholds
        )
        findings["transport"] = "rest"
        return findings
    try:
        findings = run_checks_graphql(
            owner, repo, number, me=me, mode=mode, session=session, thresholds=thresholds
        )
    except checks.NotFoundError:
        raise
    except checks.TakenError as exc:
        findings = checks.run_checks(
            owner, repo, number, me=me, payload=payload, thresholds=thresholds
        )
        findings["transport"] = "rest"
        findings["transport_fallback"] = f"{mode} transport failed ({exc}); fell back to REST"
        return findings
    findings["transport"] = mode
    return findings


def _oldest_merged_at(merged):
    nodes = merged.get("nodes", [])
    if not nodes:
        return datetime.min.replace(tzinfo=timezone.utc)
    last = nodes[-1].get("mergedAt") or ""
    dt = checks._parse_ts(last)
    if dt is not None:
        return dt
    return datetime.min.replace(tzinfo=timezone.utc)
