"""Candidate discovery for taken.

Piggybacks on GitHub's issue search API, the same source the web aggregators
use, for raw candidates. What the aggregators don't do (and we do): run
taken's full verification on every candidate and rank by maintainer
responsiveness, the signal that best predicts whether volunteering will go
anywhere. No aggregator filters on that.
"""

import concurrent.futures
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import budget, checks, graphql
from .verdict import GO, decide

SEARCH_LABELS = ["good first issue", "good-first-issue", "beginner friendly", "help wanted"]
SEARCH_PER_PAGE = 50
# Baseline verify-pool size; the authenticated budget tier raises it
# (see taken/budget.py).
VERIFY_POOL = 40
DEFAULT_JOBS = 8


@dataclass
class DiscoverOptions:
    """All the knobs for discover(), in one place (issue #45).

    limit: max candidates returned; negative values clamp to 0, and 0
        skips verification entirely.
    language: only consider repositories in this language (None: no filter).
    label: issue label to search (None: good-first-issue style labels).
    min_contributors: only consider repos with at least this many
        contributors in the last 90 days.
    me: GitHub login; the caller's own comments are ignored in the
        claimant scan.
    jobs: max concurrent verifications.
    on_progress: called as on_progress(done, total) from the calling
        thread as each candidate finishes, so callers can drive a
        progress bar.
    on_searched: called as on_searched([(label, count)]) after the search
        phase, so callers can report what was searched.
    mode: verification fetch path: "rest" (default), "graphql", or
        "persistent" (see graphql.fetch_mode).
    thresholds: stale-claim decay settings (issue #83); None means the
        defaults.
    allocation: how verify-pool slots are assigned across repos: "bandit"
        (default, issue #130) spends them by Thompson sampling on each
        repo's observed GO yield, with an exploration floor so unseen
        repos keep getting tried; "recency" keeps the previous
        freshest-first order.
    explore_floor: probability (0..1) that a bandit pick ignores the
        sampled means and chooses uniformly among repos with candidates
        still queued (default 0.15). Clamped to [0, 1].
    """

    limit: int = 10
    language: str | None = None
    label: str | None = None
    min_contributors: int = 0
    me: str | None = None
    jobs: int = DEFAULT_JOBS
    on_progress: Callable | None = None
    on_searched: Callable | None = None
    mode: str = "rest"
    thresholds: dict | None = None
    allocation: str = "bandit"
    explore_floor: float = 0.15


def _verify_pool_size():
    """Candidates fully verified per run: baseline, raised when logged in."""
    return budget.effective_cap(VERIFY_POOL, "discover_pool")


# author_association values that mean the commenter can speak for the repo.
# A random "+1" from a passerby (NONE/CONTRIBUTOR/...) is not maintainer
# engagement and must not earn the +3 "maintainer replied" points.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _thread_graphql_session():
    """One persistent GraphQL session per verify-pool thread.

    graphql.get_session() shares a single keep-alive connection, which is
    not safe to use from the pool's worker threads; each thread keeps its
    own session (and its own connection) instead. The token is still read
    once per thread from `gh auth token` and held in memory only.
    """
    return graphql.thread_session()


def build_query(labels, language=None, updated_after=None):
    """Build a search/issues query matching any of the given labels.

    Accepts a single label string or a list. Multiple labels are OR'd with
    comma-separated values inside one label: qualifier, so one search call
    covers every label instead of one call per label (GitHub's secondary
    rate limits throttle burst velocity, not budget, and the old per-label
    loop died 12.5s into a cold run with zero verdicts).
    """
    if isinstance(labels, str):
        labels = [labels]
    label_part = ",".join(f'"{lab}"' for lab in labels)
    parts = ["is:open", "is:issue", "no:assignee", f"label:{label_part}"]
    if updated_after:
        parts.append(f"updated:>={updated_after}")
    if language:
        parts.append(f"language:{language}")
    return " ".join(parts)


def repo_of(search_item):
    """Extract (owner, repo) from a search result's repository_url."""
    url = (search_item.get("repository_url") or "").rstrip("/").split("/")
    if len(url) < 2:
        return None
    return url[-2], url[-1]


def maintainer_engaged(issue, comments, me=None):
    """Heuristic: a maintainer, not the author, you, or a bot, commented.

    A maintainer reply is the strongest cheap signal that volunteering on
    the issue will get a response. Only OWNER/MEMBER/COLLABORATOR
    author_associations count; bots and the issue author never do, and
    neither does your own login (see --me).
    """
    author = issue.get("author")
    me_lower = (me or "").lower()
    for comment in comments:
        login = (comment.get("user") or {}).get("login") or ""
        if not login or login == author or login.endswith("[bot]"):
            continue
        # GitHub logins are case-insensitive: "RogueAlg0" is me even when
        # --me was passed as "roguealg0". Matches the claimant-scan
        # convention in checks.py.
        if me_lower and login.lower() == me_lower:
            continue
        if comment.get("author_association") in MAINTAINER_ASSOCIATIONS:
            return True
    return False


def _days_ago(iso_ts):
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def score_candidate(findings, updated_at, engaged):
    """Explainable score. Returns (points, [reasons])."""
    points = 0
    why = []
    if engaged:
        points += 3
        why.append("maintainer replied")
    age_days = _days_ago(updated_at)
    if age_days is not None and age_days <= 7:
        points += 2
        why.append(f"updated {age_days}d ago")
    push_days = _days_ago(findings["repo_health"].get("pushed_at") or "")
    if push_days is not None and push_days <= 7:
        points += 1
        why.append(f"repo pushed {push_days}d ago")
    if not why:
        why.append("passed verification")
    return points, why


def _score_ceiling(updated_at):
    """Max score a candidate can still reach, from its known updated_at.

    score_candidate awards at most 3 (maintainer reply) + 2 (updated within
    7 days) + 1 (repo pushed within 7 days). The +2 recency points are gone
    for candidates updated more than 7 days ago. An unparseable timestamp
    is conservatively treated as fresh (ceiling 6): never assume stale.
    """
    age_days = _days_ago(updated_at)
    return 6 if age_days is None or age_days <= 7 else 4


def _verify_candidate(
    owner,
    repo,
    number,
    item,
    min_contributors,
    me,
    mode="rest",
    thresholds=None,
    repo_memo=None,
):
    """Run the full check on one candidate.

    Kept separate so the pool can be verified concurrently; each call only
    does idempotent GETs through the (thread-safe) cache. `mode` selects
    the fetch path: "rest" (default), "graphql" (one query per issue via
    `gh api graphql`), or "persistent" (GraphQL over a per-thread
    keep-alive session).

    `thresholds` carries the stale-claim decay settings (issue #83);
    None means the defaults.

    `repo_memo` is a checks.RepoMemo shared across the run's candidates:
    repo-level stages are fetched once per repo instead of once per
    candidate (matters on a cold cache / --no-cache).

    Returns (entry, error): the ranked entry (or None when the candidate
    was filtered by a real verdict), and the TakenError when verification
    itself failed (or None). Callers use the error to tell "nothing is
    available" apart from "the tool is broken".
    """
    try:
        if mode in ("graphql", "persistent"):
            session = _thread_graphql_session() if mode == "persistent" else None
            # The wrapper falls back to REST per candidate when the GraphQL
            # transport fails, and records the fallback in the findings.
            findings = graphql.run_checks_with_fallback(
                owner, repo, number, me=me, mode=mode, session=session, thresholds=thresholds
            )
            engaged_comments = None
        else:
            # The search item already carries every field check_issue()
            # needs, so the per-issue GET is skipped (issue #153): one
            # fewer API call per candidate, up to VERIFY_POOL per run.
            # include_comments=True reuses the claimant scan's comment
            # pages for maintainer-engagement scoring instead of
            # fetching them a second time.
            findings, engaged_comments, _ = checks.run_checks(
                owner,
                repo,
                number,
                me=me,
                payload=item,
                thresholds=thresholds,
                include_comments=True,
                repo_memo=repo_memo,
            )
    except checks.TakenError as exc:
        return None, exc  # fail-closed per issue; keep scanning the rest
    verdict, reasons = decide(findings)
    if verdict != GO:
        return None, None
    if (findings["repo_health"].get("contributors") or 0) < min_contributors:
        return None, None
    try:
        # The verdict above already reflects the verified evidence; the
        # engagement check reuses the claimant scan's comment pages when
        # the REST path fetched them, and falls back to a fresh fetch when
        # it did not (early TAKEN exit, or the GraphQL path). A truncated
        # page cap here is not a verdict risk.
        if engaged_comments is not None:
            comments = engaged_comments
        else:
            comments, _ = checks.fetch_comments(owner, repo, number)
    except checks.TakenError as exc:
        return None, exc  # one bad comments fetch must not abort the run
    engaged = maintainer_engaged(findings["issue"], comments, me=me)
    points, why = score_candidate(findings, item.get("updated_at"), engaged)
    return {
        "target": f"{owner}/{repo}#{number}",
        "score": points,
        "why": why,
        "verdict": verdict,
        "reasons": reasons,
        "findings": findings,
        "updated_at": item.get("updated_at") or "",
        "friendly_labels": checks.friendly_labels(findings),
        "welcoming": checks.welcoming_signals(findings),
    }, None


class DiscoverResults(list):
    """Ranked candidates plus verification stats.

    errors: candidates that failed with TakenError instead of a verdict.
    total: candidates that entered the verify pool.
    verified: candidates whose verification was submitted to the pool
        (<= total; strictly lower when the #214 early stop fires before
        the queue drains). Counts submitted work, not consumed
        completions, so it measures the API spend the stop is meant to
        save.
    search_errors: [(label, error)] for label searches that failed during
    the per-label fallback; empty when the combined search succeeded, so
    callers can tell "partial results" apart from "everything worked".
    """

    def __init__(self, items=(), *, errors=0, total=0, verified=0, search_errors=()):
        super().__init__(items)
        self.errors = errors
        self.total = total
        self.verified = verified
        self.search_errors = list(search_errors)


def _pool_from_items(items, seen, limit):
    """Fill the verify pool from search items, freshest first.

    Items are deduplicated by (owner, repo, number) as a safety net; seen
    is shared so the per-label fallback cannot re-add an issue found under
    an earlier label. Adds at most `limit` candidates.
    """
    candidates = []
    for item in items:
        if len(candidates) >= limit:
            break
        where = repo_of(item)
        if not where:
            continue
        owner, repo = where
        key = (owner, repo, item.get("number"))
        if key in seen:
            continue
        seen.add(key)
        candidates.append((owner, repo, item.get("number"), item))
    return candidates


def _collect_candidates(labels, language, updated_after):
    """Search once with all labels OR'd and fill the verify pool.

    A single search/issues call covers every label, so a cold discover run
    no longer fires a burst of back-to-back search calls into GitHub's
    secondary rate limit. Results arrive sorted by recency (see
    checks.search_issues), so the pool fills with the freshest candidates
    first.

    When the combined search raises (rate limit, transient failure), fall
    back to one paced search per label so the run degrades to partial
    results instead of dying with zero candidates (issue #162). When every
    label fails too, the first error is re-raised: total failure stays an
    error, never a silent empty success.

    Returns (candidates, searched, search_errors): the pool capped at
    VERIFY_POOL; a [(labels, count)] list so callers can report what was
    searched (one entry for the combined query, one per label on the
    fallback path); and a [(label, error)] list for labels whose fallback
    search failed, empty on the normal path.
    """
    query = build_query(labels, language=language, updated_after=updated_after)
    try:
        items = checks.search_issues(query, per_page=SEARCH_PER_PAGE)
    except checks.TakenError:
        return _collect_candidates_per_label(labels, language, updated_after)
    candidates = _pool_from_items(items, set(), _verify_pool_size())
    searched = [(", ".join(labels), len(items))]
    return candidates, searched, []


def _collect_candidates_per_label(labels, language, updated_after):
    """Fallback: one search per label when the combined query fails.

    Keeps the candidates from the labels that succeeded and records which
    label failed and why. Each search goes through the same pacing as the
    normal path, so the fallback cannot burst into the secondary rate
    limit it is trying to recover from.
    """
    candidates = []
    searched = []
    search_errors = []
    seen = set()
    for label in labels:
        query = build_query(label, language=language, updated_after=updated_after)
        try:
            items = checks.search_issues(query, per_page=SEARCH_PER_PAGE)
        except checks.TakenError as exc:
            search_errors.append((label, str(exc)))
            continue
        searched.append((label, len(items)))
        candidates.extend(_pool_from_items(items, seen, _verify_pool_size() - len(candidates)))
        if len(candidates) >= _verify_pool_size():
            break
    if not candidates and search_errors and not searched:
        # Every label failed: re-raise instead of returning an empty
        # success that looks like "no candidates found".
        raise checks.TakenError(search_errors[0][1])
    return candidates, searched, search_errors


class _RepoBandit:
    """Thompson-sampling allocator that spends verify budget across repos.

    Issue #130. Each repo is an arm; the reward is 1 when a verified
    candidate banks GO and 0 when it verifies cleanly to a non-GO
    verdict. Transport failures (TakenError) carry no signal about the
    repo, so they leave the arm untouched.

    Arms start at the uniform Beta(1, 1) prior, so unseen repos are
    genuinely competitive from the first draw. The exploration floor is
    an extra guarantee: with probability `explore_floor` the pick is
    uniform over every repo that still has candidates queued, so a
    low-mean arm can never starve the unseen ones out entirely.

    `rng` is injectable for deterministic tests; defaults to a fresh
    random.Random.
    """

    def __init__(self, explore_floor=0.15, rng=None):
        self.explore_floor = max(0.0, min(1.0, explore_floor))
        self.rng = rng if rng is not None else random.Random()
        self.arms = {}

    def pick(self, repos):
        """Choose the next repo to verify a candidate from.

        `repos` is the non-empty list of repos with candidates still
        queued. Returns one of them.
        """
        if self.rng.random() < self.explore_floor:
            return self.rng.choice(repos)
        best, best_sample = repos[0], -1.0
        for repo in repos:
            alpha, beta = self.arms.get(repo, (1.0, 1.0))
            sample = self.rng.betavariate(alpha, beta)
            if sample > best_sample:
                best, best_sample = repo, sample
        return best

    def update(self, repo, success):
        """Record one verified candidate: True banked GO, False did not."""
        alpha, beta = self.arms.get(repo, (1.0, 1.0))
        if success:
            alpha += 1
        else:
            beta += 1
        self.arms[repo] = (alpha, beta)


class _RollingVerifier:
    """Rolling verification loop for discover().

    At most `workers` candidates are ever in flight, and the stop proof
    is evaluated after every completion and BEFORE replacement work is
    submitted, so no candidate is ever submitted once the top-`limit`
    ranking is decided (issue #214). Eagerly submitting the whole pool up
    front would let fast workers start every verification before the
    proof can fire, doing all the API work the stop exists to save.

    Ceilings cover every candidate that has not banked a score yet:
    queued and in-flight alike. .verified counts submitted verification
    work, not consumed completions: one submission is one
    _verify_candidate run, and nothing is ever cancelled, so every
    submission runs.
    """

    def __init__(self, candidates, options, limit):
        self.candidates = candidates
        self.options = options
        self.limit = limit
        self.workers = max(1, options.jobs)
        # One repo-level memo per run: repo_health and ai_policy are
        # per-repo data, so candidates from the same repo share a single
        # fetch even on a cold cache / --no-cache.
        self.repo_memo = checks.RepoMemo()
        self.ranked = []
        self.errors = 0
        self.verified = 0
        self.banked_scores = []
        self.ceilings = {
            idx: _score_ceiling(item.get("updated_at"))
            for idx, (_, _, _, item) in enumerate(candidates)
        }
        self.in_flight = {}
        self.bandit = None
        self.repo_queues = None
        self.queue = deque(range(len(candidates)))
        if options.allocation == "bandit":
            # Group the pool by repo, keeping recency order inside each
            # repo: the bandit chooses the repo, recency still chooses the
            # candidate within it.
            self.bandit = _RepoBandit(explore_floor=options.explore_floor)
            self.repo_queues = {}
            for idx, (owner, repo, _, _) in enumerate(candidates):
                self.repo_queues.setdefault((owner, repo), deque()).append(idx)
            self.queue = None

    def has_work(self):
        if self.bandit is not None:
            return any(self.repo_queues.values())
        return bool(self.queue)

    def submit_next(self, pool):
        if self.bandit is not None:
            live = [r for r, q in self.repo_queues.items() if q]
            repo = self.bandit.pick(live)
            queue = self.repo_queues[repo]
        else:
            queue = self.queue
        idx = queue.popleft()
        owner, repo, number, item = self.candidates[idx]
        try:
            future = pool.submit(
                _verify_candidate,
                owner,
                repo,
                number,
                item,
                self.options.min_contributors,
                self.options.me,
                self.options.mode,
                thresholds=self.options.thresholds,
                repo_memo=self.repo_memo,
            )
        except Exception:
            # pool.submit can raise (e.g. RuntimeError from a closed
            # executor during interpreter shutdown). The index is already
            # popped, so restore it to the front of its queue: a candidate
            # must never be silently lost (issue #335).
            queue.appendleft(idx)
            raise
        self.in_flight[future] = idx
        self.verified += 1

    def ranking_decided(self):
        remaining_ceiling = max(self.ceilings.values(), default=-1)
        return sum(1 for s in self.banked_scores if s > remaining_ceiling) >= self.limit

    def _prime(self, pool):
        while self.has_work() and len(self.in_flight) < self.workers:
            self.submit_next(pool)

    def _consume(self, future, done):
        idx = self.in_flight.pop(future)
        del self.ceilings[idx]
        entry, error = future.result()
        done += 1
        if self.options.on_progress is not None:
            self.options.on_progress(done, len(self.candidates))
        if self.bandit is not None and error is None:
            # A clean verification is one Bernoulli trial for the repo:
            # GO banked or not. A transport error says nothing about the
            # repo's yield, so it is skipped.
            owner, repo, _, _ = self.candidates[idx]
            self.bandit.update((owner, repo), entry is not None)
        if error is not None:
            self.errors += 1
        elif entry is not None:
            self.ranked.append(entry)
            self.banked_scores.append(entry["score"])
        return done

    def run(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            self._prime(pool)
            done = 0
            stopped = False
            while self.in_flight and not stopped:
                finished, _ = concurrent.futures.wait(
                    self.in_flight, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in finished:
                    done = self._consume(future, done)
                    if self.ranking_decided():
                        # The top-`limit` ranking is decided: no remaining
                        # candidate can displace the banked top-`limit`, so
                        # the rest of the queue is never submitted. In-flight
                        # futures finish during executor shutdown; their
                        # results are discarded (a bounded overrun of at most
                        # `workers - 1` extra verifications).
                        stopped = True
                        break
                if not stopped:
                    self._prime(pool)
        return self.ranked, self.errors, self.verified


def discover(options=None):
    """Search, verify, and rank contribution candidates.

    Takes a single DiscoverOptions object (issue #45) instead of a
    sprawling keyword list; None means all defaults. Candidates are
    verified concurrently (options.jobs threads). Returns a
    DiscoverResults (a list of dicts sorted by score (desc), then recency
    (desc)) with .errors / .total stats, so callers can tell "no GO
    candidates" apart from "verification kept failing", plus
    .search_errors [(label, error)] when the search phase fell back to
    per-label queries and some of them failed: target, score, why, verdict,
    reasons, findings, updated_at, friendly_labels (first-time-contributor
    labels on the issue), welcoming (repo-level signs contributions are
    welcome).

    Verification stops early (issue #214) once the top-`limit` ranking is
    provably decided: when `limit` banked GO candidates all score strictly
    above the highest score any not-yet-banked candidate can still reach
    (from its known updated_at), the remaining pool cannot change the
    output. Submission is rolling and bounded: at most `jobs` candidates
    are ever in flight, and the stop proof is evaluated after every
    completion before replacement work is submitted, so no candidate is
    submitted once the ranking is decided. .verified reports how many
    candidates were actually submitted, so callers can distinguish a pool
    of 80 verified in full from one cut short at 23.
    """
    if options is None:
        options = DiscoverOptions()
    # A negative limit is meaningless; clamp to 0 (empty result) instead of
    # letting ranked[:limit] silently drop the top candidates. This also
    # covers the MCP discover_candidates path, which bypasses argparse.
    limit = max(0, options.limit)
    if options.allocation not in ("bandit", "recency"):
        raise ValueError(
            f"unknown allocation: {options.allocation!r} (expected 'bandit' or 'recency')"
        )
    updated_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    labels = [options.label] if options.label else SEARCH_LABELS
    candidates, searched, search_errors = _collect_candidates(
        labels, options.language, updated_after
    )
    if options.on_searched is not None:
        options.on_searched(searched)

    total = len(candidates)
    if options.on_progress is not None:
        options.on_progress(0, total)
    if limit == 0:
        # Nothing can make the cut; skip verification entirely.
        return DiscoverResults([], errors=0, total=total, verified=0, search_errors=search_errors)
    verifier = _RollingVerifier(candidates, options, limit)
    ranked, errors, verified = verifier.run()
    # Score desc, then recency desc: the freshest candidate wins ties.
    # (A single sort; the old double-sort accidentally left equal scores
    # oldest-first because the second stable sort preserved the first.)
    ranked.sort(key=lambda r: (r["score"], r["updated_at"]), reverse=True)
    return DiscoverResults(
        ranked[:limit],
        errors=errors,
        total=total,
        verified=verified,
        search_errors=search_errors,
    )
