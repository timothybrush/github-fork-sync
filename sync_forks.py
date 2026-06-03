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

    def commits_behind(self, repo: str, branch: str, upstream_owner: str) -> int:
        """Number of commits `repo` is behind its upstream on `branch` (0 if unknown)."""
        response = self._request(
            "GET", f"/repos/{repo}/compare/{branch}...{upstream_owner}:{branch}"
        )
        return response.json().get("ahead_by", 0) if response.is_success else 0

    def sync_fork(self, repo: str, branch: str) -> SyncResult:
        """Merge upstream changes into `repo`/`branch`."""
        response = self._request("POST", f"/repos/{repo}/merge-upstream", json={"branch": branch})
        if response.is_success:
            return SyncResult(success=True, no_op=response.json().get("merge_type") == "none")
        if response.status_code == 409:
            return SyncResult(success=False, conflict=True)
        return SyncResult(success=False, error=response.text.strip())


def resolve_token() -> str:
    """Use $GITHUB_TOKEN if set, otherwise fall back to the gh CLI's stored token."""
    if token := os.getenv("GITHUB_TOKEN"):
        return token
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode == 0 and (token := result.stdout.strip()):
        return token
    console.print("[red]No GitHub token found. Set GITHUB_TOKEN or run `gh auth login`.[/red]")
    sys.exit(1)


def load_last_run() -> datetime | None:
    """Read the timestamp of the last successful run, if any."""
    if not STATE_FILE.exists():
        return None
    try:
        return datetime.fromisoformat(json.loads(STATE_FILE.read_text())["last_run"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        console.print(
            f"[dim yellow]Warning: couldn't read state file ({exc}); forcing full check.[/]"
        )
        return None


def save_last_run(run_time: datetime) -> None:
    """Persist this run's start time as the baseline for the next run."""
    STATE_FILE.write_text(json.dumps({"last_run": run_time.isoformat()}))


def upstream_changed(parent: dict[str, Any], last_run: datetime | None) -> bool:
    """True if upstream was pushed since our last run (or if we can't tell)."""
    pushed_at = parent.get("pushedAt")
    if last_run and pushed_at:
        return datetime.fromisoformat(pushed_at) > last_run
    return True


def build_report(
    synced: dict[int, list[str]], conflicts: list[str], errors: list[str]
) -> list[str]:
    """Assemble the Slack report body from the run's results."""
    lines = ["*GitHub Fork Sync Report*"]
    if synced:
        lines.append("\n*✅ Synced Repositories:*")
        for count in sorted(synced, reverse=True):
            repos = ", ".join(f"`{name}`" for name in synced[count])
            lines.append(f"• {count} {'commit' if count == 1 else 'commits'}: {repos}")
    if conflicts:
        lines.append("\n*⚠️ Merge Conflicts (Manual Resolution Required):*")
        lines.append("• " + ", ".join(f"`{name}`" for name in conflicts))
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
    last_run = load_last_run()

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
    conflicts: list[str] = []
    errors: list[str] = []

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

            progress.update(
                task,
                description=(
                    f"[bold cyan]Reviewed {reviewed}/{total}[/] | "
                    f"[bold green]Synced {synced}[/] | "
                    f"[dim]Skipped {skipped}[/] | Checking {short}"
                ),
            )

            if parent and upstream_changed(parent, last_run):
                behind = github.commits_behind(repo, branch, parent["owner"]["login"])
                if behind:
                    result = github.sync_fork(repo, branch)
                    if result.conflict:
                        conflicts.append(short)
                    elif result.error:
                        errors.append(f"{short}: {result.error}")
                    elif result.no_op:
                        progress.console.print(
                            f"\n[dim yellow]Ignored {short}: API cache artifact "
                            f"(reported behind, but up to date).[/]"
                        )
                    elif result.success:
                        synced += 1
                        synced_by_count.setdefault(behind, []).append(short)
            elif parent:
                skipped += 1

            progress.advance(task)

    save_last_run(started_at)
    console.print(
        f"\n[bold green]Sync complete. {skipped} repos bypassed via state caching.[/bold green]"
    )

    if synced_by_count or conflicts or errors:
        console.print("Sending Slack notification…")
        notify_slack(webhook_url, build_report(synced_by_count, conflicts, errors))
    else:
        console.print(
            "[bold green]All active forks are up to date. No Slack notification sent.[/bold green]"
        )


if __name__ == "__main__":
    main()
