#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "rich>=13.7",
# ]
# ///
"""Sync every GitHub fork you own with its upstream and report the results to Slack."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

console = Console()
STATE_FILE = Path(__file__).resolve().parent / "sync_state.json"
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
CONFLICT_RECHECK_DELAY = 3  # seconds to wait before re-confirming a 409, to filter out flicker
AUTO_RESOLVE_AFTER_RUNS = 5  # auto-resolve a conflict once it has persisted strictly more runs than this

FORKS_QUERY = """
query($endCursor: String) {
  viewer {
    repositories(first: 25, after: $endCursor, isFork: true, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        defaultBranchRef { name }
        parent {
          owner { login }
          defaultBranchRef { name }
          pushedAt
        }
      }
    }
  }
}
"""


@dataclass
class SyncResult:
    """Outcome of a single merge-upstream attempt."""

    success: bool
    conflict: bool = False
    no_op: bool = False  # API reported the fork as behind, but the merge was a no-op
    error: str = ""
    flaky: bool = False  # first merge-upstream returned 409, but a re-confirm cleared it


@dataclass
class Divergence:
    """How a fork's default branch relates to its upstream, per the compare API."""

    behind: int  # commits the fork is behind upstream (upstream has, fork lacks)
    diverged: int  # the fork's own commits upstream lacks — discarded on auto-resolve
    merge_base_sha: str  # the common ancestor; reset target for auto-resolution


def should_auto_resolve(runs: int) -> bool:
    """Whether a conflict that has persisted `runs` runs is due for auto-resolution."""
    return runs > AUTO_RESOLVE_AFTER_RUNS


class GitHub:
    """Thin httpx wrapper over the GitHub REST + GraphQL APIs with retry/backoff."""

    def __init__(self, token: str) -> None:
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue a request, transparently retrying transient errors and rate limits."""
        for attempt in range(MAX_RETRIES):
            response = self.client.request(method, path, **kwargs)
            rate_limited = (
                response.status_code in (403, 429)
                and "secondary rate limit" in response.text.lower()
            )
            if response.status_code not in TRANSIENT_STATUSES and not rate_limited:
                return response
            backoff = 2**attempt * (10 if rate_limited else 1)
            delay = int(response.headers.get("retry-after", backoff))
            console.print(
                f"\n[yellow]Transient GitHub error ({response.status_code}); "
                f"retrying in {delay}s…[/]"
            )
            time.sleep(delay)
        return response

    def forks(self) -> list[dict[str, Any]]:
        """Page through every fork owned by the viewer, 25 at a time."""
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), console=console
        ) as progress:
            task = progress.add_task("[cyan]Fetching forks…", total=None)
            while True:
                response = self._request(
                    "POST",
                    "/graphql",
                    json={"query": FORKS_QUERY, "variables": {"endCursor": cursor}},
                )
                response.raise_for_status()
                payload = response.json()
                if errors := payload.get("errors"):
                    console.print(f"[red]GraphQL error: {errors}[/red]")
                    sys.exit(1)
                repos = payload["data"]["viewer"]["repositories"]
                results.extend(repos["nodes"])
                progress.update(task, description=f"[cyan]Fetched {len(results)} forks…")
                if not repos["pageInfo"]["hasNextPage"]:
                    return results
                cursor = repos["pageInfo"]["endCursor"]
                time.sleep(0.5)  # gentle pacing to avoid secondary rate limits

    def commits_behind(
        self, repo: str, branch: str, upstream_owner: str, upstream_branch: str
    ) -> int | None:
        """Commits `repo` is behind upstream; None if GitHub wouldn't tell us.

        Compares the fork's branch against the upstream's *own* default branch — the
        two may be named differently (e.g. `master` vs `main`). A non-success response
        returns None (unknown) rather than 0, so callers don't mistake an API failure
        for "up to date" and silently skip a fork that is actually behind.
        """
        response = self._request(
            "GET", f"/repos/{repo}/compare/{branch}...{upstream_owner}:{upstream_branch}"
        )
        return response.json().get("ahead_by", 0) if response.is_success else None

    def _merge_upstream(self, repo: str, branch: str) -> SyncResult:
        """Single POST to the merge-upstream endpoint."""
        response = self._request("POST", f"/repos/{repo}/merge-upstream", json={"branch": branch})
        if response.is_success:
            return SyncResult(success=True, no_op=response.json().get("merge_type") == "none")
        if response.status_code == 409:
            return SyncResult(success=False, conflict=True)
        return SyncResult(success=False, error=response.text.strip())

    def sync_fork(self, repo: str, branch: str) -> SyncResult:
        """Merge upstream into `repo`/`branch`, re-confirming any reported conflict.

        GitHub computes mergeability against internally cached git data that can be
        transiently stale right after an upstream push, occasionally yielding a bogus
        409. We re-confirm a conflict once after a short delay; if the second attempt
        succeeds, the conflict was a flicker and we mark the result `flaky` instead of
        crying wolf.
        """
        result = self._merge_upstream(repo, branch)
        if not result.conflict:
            return result
        time.sleep(CONFLICT_RECHECK_DELAY)
        confirmed = self._merge_upstream(repo, branch)
        if not confirmed.conflict and not confirmed.error:
            confirmed.flaky = True
        return confirmed

    def upstream_divergence(
        self, repo: str, branch: str, upstream_owner: str, upstream_branch: str
    ) -> Divergence | None:
        """How `repo`/`branch` diverges from upstream; None if GitHub wouldn't tell us.

        A single compare with the fork's branch as base and upstream as head: the
        compare's `ahead_by` is how far upstream is ahead of the fork (fork behind),
        `behind_by` is how many commits the fork has that upstream lacks (the
        divergent commits we'd discard), and `merge_base_commit.sha` is their common
        ancestor — the ref we reset to so the branch can fast-forward again.
        """
        response = self._request(
            "GET", f"/repos/{repo}/compare/{branch}...{upstream_owner}:{upstream_branch}"
        )
        if not response.is_success:
            return None
        data = response.json()
        merge_base = (data.get("merge_base_commit") or {}).get("sha")
        if not merge_base:
            return None
        return Divergence(
            behind=data.get("ahead_by", 0),
            diverged=data.get("behind_by", 0),
            merge_base_sha=merge_base,
        )

    def reset_branch(self, repo: str, branch: str, sha: str) -> httpx.Response:
        """Force-move `repo`/`branch` to point at `sha` (a non-fast-forward update)."""
        return self._request(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{branch}",
            json={"sha": sha, "force": True},
        )

    def force_resolve(self, repo: str, branch: str, merge_base_sha: str) -> SyncResult:
        """Discard the fork's divergent commits, then fast-forward to upstream.

        Resets the branch back to its merge-base with upstream (dropping the fork's
        own commits that made it unmergeable), then runs merge-upstream to fast-
        forward up to the latest upstream. A failure at either step is surfaced so
        the caller keeps tracking the conflict rather than declaring a false win.
        """
        reset = self.reset_branch(repo, branch, merge_base_sha)
        if not reset.is_success:
            return SyncResult(success=False, error=f"branch reset failed: {reset.text.strip()}")
        return self._merge_upstream(repo, branch)


def resolve_token() -> str:
    """Use $GITHUB_TOKEN if set, otherwise fall back to the gh CLI's stored token."""
    if token := os.getenv("GITHUB_TOKEN"):
        return token
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode == 0 and (token := result.stdout.strip()):
        return token
    console.print("[red]No GitHub token found. Set GITHUB_TOKEN or run `gh auth login`.[/red]")
    sys.exit(1)


def load_state() -> tuple[datetime | None, dict[str, dict[str, Any]], dict[str, Any]]:
    """Read the last run timestamp, the forks left in conflict, and the daily block.

    Tolerant of older state formats: a missing `conflicts` or `daily` key simply
    yields an empty mapping.
    """
    if not STATE_FILE.exists():
        return None, {}, {}
    try:
        data = json.loads(STATE_FILE.read_text())
        raw_last_run = data.get("last_run")
        last_run = datetime.fromisoformat(raw_last_run) if raw_last_run else None
        return last_run, data.get("conflicts", {}), data.get("daily", {})
    except (ValueError, json.JSONDecodeError) as exc:
        console.print(
            f"[dim yellow]Warning: couldn't read state file ({exc}); forcing full check.[/]"
        )
        return None, {}, {}


def save_state(
    run_time: datetime, conflicts: dict[str, dict[str, Any]], daily: dict[str, Any]
) -> None:
    """Persist this run's start time, still-conflicting forks, and the daily block."""
    STATE_FILE.write_text(
        json.dumps(
            {"last_run": run_time.isoformat(), "conflicts": conflicts, "daily": daily},
            indent=2,
        )
    )


def local_today() -> str:
    """Local calendar date (YYYY-MM-DD) used as the daily-rollup boundary."""
    return datetime.now().astimezone().date().isoformat()


def upstream_changed(parent: dict[str, Any], last_run: datetime | None) -> bool:
    """True if upstream was pushed since our last run (or if we can't tell)."""
    pushed_at = parent.get("pushedAt")
    if last_run and pushed_at:
        return datetime.fromisoformat(pushed_at) > last_run
    return True


def should_check(
    parent: dict[str, Any], last_run: datetime | None, was_conflicted: bool
) -> bool:
    """Whether a fork warrants a sync attempt this run.

    A fork is checked when upstream has moved since our last run *or* when it was
    left in a merge conflict last run. Re-checking conflicts every run — regardless
    of upstream activity — is what stops an unresolved (or since-resolved) conflict
    from being silently hidden by the upstream-activity cache: once a conflict is
    reported, upstream's `pushedAt` no longer advances past our baseline, so the
    plain timestamp gate would skip the fork forever and the conflict would appear
    to "vanish" on the next run.
    """
    return was_conflicted or upstream_changed(parent, last_run)


def _format_ts(iso: str) -> str:
    """Render an ISO timestamp as a compact UTC date for Slack."""
    try:
        return f"{datetime.fromisoformat(iso):%Y-%m-%d %H:%M} UTC"
    except ValueError:
        return iso or "unknown"


def _synced_bullets(synced_by_count: dict[int, list[str]]) -> list[str]:
    """Render `{commit_count: [repos]}` as grouped bullets, highest count first.

    Repos within a bucket are sorted by name for deterministic output. A negative
    count is the `unknown commit count` sentinel.
    """
    lines: list[str] = []
    for count in sorted(synced_by_count, reverse=True):
        repos = ", ".join(f"`{name}`" for name in sorted(synced_by_count[count]))
        if count < 0:
            lines.append(f"• unknown commit count: {repos}")
        else:
            lines.append(f"• {count} {'commit' if count == 1 else 'commits'}: {repos}")
    return lines


def build_daily_summary(daily_synced: dict[str, int], date: str) -> list[str]:
    """The dated, grouped synced report for a completed day; empty if no syncs.

    `daily_synced` maps each repo's short name to its day-total commits. It is
    inverted into the `{count: [repos]}` shape the shared bullet renderer consumes.
    """
    if not daily_synced:
        return []
    by_count: dict[int, list[str]] = {}
    for repo, total in daily_synced.items():
        by_count.setdefault(total, []).append(repo)
    return [
        f"*📅 Daily Sync Summary — {date}*",
        "*Synced Repositories:*",
        *_synced_bullets(by_count),
    ]


def brief_synced_line(synced_by_count: dict[int, list[str]]) -> str:
    """One-line summary of this run: total commits across total repos.

    Unknown-count syncs (the `-1` bucket) contribute to the repo count but not the
    commit sum, since their true count is unknown.
    """
    repos = sum(len(names) for names in synced_by_count.values())
    commits = sum(count * len(names) for count, names in synced_by_count.items() if count >= 0)
    return (
        f"*Synced Repositories:* {commits} {'commit' if commits == 1 else 'commits'} "
        f"across {repos} {'repo' if repos == 1 else 'repos'}"
    )


def accrue_synced(
    daily_synced: dict[str, int], run_synced_by_count: dict[int, list[str]]
) -> dict[str, int]:
    """Fold a run's `{count: [repos]}` syncs into the day's `{repo: total}` totals.

    Returns a new dict (does not mutate the input). A repo's total is summed across
    syncs; once a repo records an unknown count (`-1`) for the day it stays unknown,
    since a true total can no longer be computed.
    """
    updated = dict(daily_synced)
    for count, repos in run_synced_by_count.items():
        for repo in repos:
            if count < 0 or updated.get(repo, 0) < 0:
                updated[repo] = -1
            else:
                updated[repo] = updated.get(repo, 0) + count
    return updated


def daily_rollover(daily: dict[str, Any], today: str) -> tuple[list[str], dict[str, int]]:
    """Decide whether to emit the previous day's summary and reset the accumulator.

    `daily` is the persisted block (`{"date": ..., "synced": {...}}`) or empty.
    Returns `(prev_day_lines, accumulator_for_this_run)`:
    - Same local date as `today` → no report, carry the existing accumulator forward.
    - A different (earlier) date → report it under its own date, start fresh.
      If whole days were skipped, accruals report under their own `date`, not "yesterday".
    - No prior date (first ever run) → no report, fresh accumulator.
    """
    prev_date = daily.get("date")
    prev_synced = daily.get("synced", {})
    if prev_date == today:
        return [], prev_synced
    report = build_daily_summary(prev_synced, prev_date) if prev_date else []
    return report, {}


def build_run_report(
    synced: dict[int, list[str]],
    conflicts: list[tuple[str, int, str]],
    errors: list[str],
    resolved: list[tuple[str, dict[str, Any]]],
    flaky: list[str],
    force_resolved: list[tuple[str, int, int]] | None = None,
    prev_day_lines: list[str] | None = None,
) -> list[str]:
    """Assemble the per-run Slack message.

    Order: header, the previous day's full summary (only on the first run of a new
    day), this run's brief synced line, then the unchanged full-detail sections for
    conflicts, cleared conflicts, transient conflicts, auto-resolved conflicts, and
    errors. Posted every run (heartbeat).
    """
    force_resolved = force_resolved or []
    prev_day_lines = prev_day_lines or []
    lines = ["*GitHub Fork Sync Report*"]
    if prev_day_lines:
        lines.append("")
        lines.extend(prev_day_lines)
    lines.append("")
    lines.append(brief_synced_line(synced))
    if force_resolved:
        lines.append("\n*🛠️ Auto-Resolved Conflicts (divergent commits discarded to fast-forward):*")
        for name, runs, diverged in force_resolved:
            commits = f"{diverged} {'commit' if diverged == 1 else 'commits'}"
            lines.append(
                f"• `{name}` — discarded {commits} and fast-forwarded "
                f"after {runs} {'run' if runs == 1 else 'runs'} in conflict"
            )
    if conflicts:
        lines.append("\n*⚠️ Merge Conflicts (Manual Resolution Required):*")
        for name, runs, first_seen in conflicts:
            if runs <= 1:
                lines.append(f"• `{name}` — new this run")
            else:
                lines.append(
                    f"• `{name}` — unresolved across {runs} runs "
                    f"(first seen {_format_ts(first_seen)})"
                )
    if resolved:
        lines.append("\n*🔄 Conflicts Cleared Since Last Run:*")
        for name, prior in resolved:
            runs = prior.get("runs", 1)
            lines.append(
                f"• `{name}` — cleared without intervention after being flagged {runs} "
                f"{'run' if runs == 1 else 'runs'} (first seen {_format_ts(prior.get('first_seen', ''))})"
            )
    if flaky:
        lines.append("\n*♻️ Transient Conflicts (cleared on in-run re-check):*")
        lines.append("• " + ", ".join(f"`{name}`" for name in flaky))
    if errors:
        lines.append("\n*❌ Errors Encountered:*")
        lines.extend(f"• {err}" for err in errors)
    return lines


def notify_slack(webhook_url: str, lines: list[str]) -> None:
    """Post the report to Slack."""
    try:
        httpx.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10.0).raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"\n[red]Failed to send Slack notification: {exc}[/red]")


def main() -> None:
    started_at = datetime.now(timezone.utc)
    last_run, prior_conflicts = load_state()

    if not (webhook_url := os.getenv("SLACK_WEBHOOK_URL")):
        console.print("[red]Error: SLACK_WEBHOOK_URL environment variable is not set.[/red]")
        sys.exit(1)

    github = GitHub(resolve_token())

    console.print("[bold green]Starting GitHub Fork Sync[/bold green]")
    if last_run:
        console.print(f"[dim]Last successful run: {last_run:%Y-%m-%d %H:%M:%S} UTC[/dim]")

    try:
        forks = github.forks()
    except httpx.HTTPError as exc:
        console.print(f"[red]Failed to fetch forks: {exc}[/red]")
        sys.exit(1)

    total = len(forks)
    reviewed = synced = skipped = 0
    synced_by_count: dict[int, list[str]] = {}
    conflicts: list[tuple[str, int, str]] = []
    errors: list[str] = []
    resolved: list[tuple[str, dict[str, Any]]] = []
    flaky: list[str] = []
    force_resolved: list[tuple[str, int, int]] = []
    current_conflicts: dict[str, dict[str, Any]] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("", total=total)

        for fork in forks:
            reviewed += 1
            repo = fork["nameWithOwner"]
            short = repo.rsplit("/", 1)[-1]
            branch = (fork.get("defaultBranchRef") or {}).get("name", "main")
            parent = fork.get("parent")
            was_conflicted = repo in prior_conflicts

            progress.update(
                task,
                description=(
                    f"[bold cyan]Reviewed {reviewed}/{total}[/] | "
                    f"[bold green]Synced {synced}[/] | "
                    f"[dim]Skipped {skipped}[/] | Checking {short}"
                ),
            )

            if parent and should_check(parent, last_run, was_conflicted):
                upstream_branch = (parent.get("defaultBranchRef") or {}).get("name") or branch
                behind = github.commits_behind(
                    repo, branch, parent["owner"]["login"], upstream_branch
                )
                if behind == 0:
                    # Fully up to date; if it was flagged before, the conflict cleared itself.
                    if was_conflicted:
                        resolved.append((short, prior_conflicts[repo]))
                else:
                    # behind > 0, or None (compare unreadable) — let merge-upstream decide.
                    result = github.sync_fork(repo, branch)
                    if result.conflict:
                        prior = prior_conflicts.get(repo, {})
                        runs = prior.get("runs", 0) + 1
                        first_seen = prior.get("first_seen", started_at.isoformat())
                        if should_auto_resolve(runs):
                            # Persisted past the threshold: discard the fork's divergent
                            # commits (reset to the merge-base) and fast-forward upstream.
                            div = github.upstream_divergence(
                                repo, branch, parent["owner"]["login"], upstream_branch
                            )
                            outcome = (
                                github.force_resolve(repo, branch, div.merge_base_sha)
                                if div is not None
                                else SyncResult(
                                    success=False,
                                    error="compare unreadable; cannot locate merge-base",
                                )
                            )
                            if outcome.success:
                                force_resolved.append((short, runs, div.diverged))
                                progress.console.print(
                                    f"\n[yellow]Auto-resolved {short}: discarded "
                                    f"{div.diverged} divergent commit(s) and fast-forwarded "
                                    f"after {runs} runs in conflict.[/]"
                                )
                                # Conflict cleared — intentionally not carried forward.
                            else:
                                current_conflicts[repo] = {"first_seen": first_seen, "runs": runs}
                                conflicts.append((short, runs, first_seen))
                                errors.append(f"{short}: auto-resolve failed: {outcome.error}")
                        else:
                            current_conflicts[repo] = {"first_seen": first_seen, "runs": runs}
                            conflicts.append((short, runs, first_seen))
                    elif result.error:
                        errors.append(f"{short}: {result.error}")
                        if was_conflicted:  # keep tracking until it truly clears
                            current_conflicts[repo] = prior_conflicts[repo]
                    elif result.no_op:
                        progress.console.print(
                            f"\n[dim yellow]Ignored {short}: API cache artifact "
                            f"(reported behind, but up to date).[/]"
                        )
                        if was_conflicted:
                            resolved.append((short, prior_conflicts[repo]))
                    elif result.success:
                        synced += 1
                        synced_by_count.setdefault(behind if behind is not None else -1, []).append(
                            short
                        )
                        if result.flaky:
                            flaky.append(short)
                        if was_conflicted:
                            resolved.append((short, prior_conflicts[repo]))
            elif parent:
                skipped += 1

            progress.advance(task)

    save_state(started_at, current_conflicts)
    console.print(
        f"\n[bold green]Sync complete. {skipped} repos bypassed via state caching.[/bold green]"
    )
    if resolved:
        console.print(
            f"[dim]{len(resolved)} previously-conflicting repo(s) cleared since last run.[/dim]"
        )
    if force_resolved:
        console.print(
            f"[dim]{len(force_resolved)} stale conflict(s) auto-resolved by discarding "
            f"divergent commits and fast-forwarding.[/dim]"
        )
    if current_conflicts:
        console.print(
            f"[dim]{len(current_conflicts)} repo(s) still in conflict; "
            f"will be re-checked next run regardless of upstream activity.[/dim]"
        )

    if synced_by_count or conflicts or errors or resolved or flaky or force_resolved:
        console.print("Sending Slack notification…")
        notify_slack(
            webhook_url,
            build_report(
                synced_by_count, conflicts, errors, resolved, flaky, force_resolved
            ),
        )
    else:
        console.print(
            "[bold green]All active forks are up to date. No Slack notification sent.[/bold green]"
        )


if __name__ == "__main__":
    main()
