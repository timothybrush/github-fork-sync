# Sync Github Forks

A vibe coded single-file Python script that keeps every GitHub fork you own in sync with its upstream parent, then reports the results to Slack.

If you maintain dozens — or hundreds — of forks, manually clicking "Sync fork" in the GitHub UI does not scale. `sync_forks.py` walks your forks via the GitHub GraphQL API, merges upstream changes into each default branch via the REST API, and posts a tidy summary to a Slack channel.

## What it does

1. Lists every repository you own that is a fork (paginated via GraphQL, 25 at a time).
2. Skips forks whose upstream has not been pushed to since the last successful run (timestamp persisted in `sync_state.json`) — **except** forks left in a merge conflict last run, which are always re-checked so an unresolved conflict can't be silently hidden by the timestamp cache.
3. For each remaining fork, asks GitHub how many commits behind its upstream's default branch it is (the fork and upstream may use different default branch names).
4. Calls the [`/merge-upstream`](https://docs.github.com/en/rest/branches/branches#sync-a-fork-branch-with-the-upstream-repository) endpoint to fast-forward the fork's default branch. A reported conflict (HTTP 409) is re-confirmed once after a short delay, since GitHub's cached merge state can be transiently stale right after an upstream push.
5. Classifies the outcome as synced, merge conflict, transient/flaky conflict (cleared on re-check), no-op (API cache artifact), or error.
6. Tracks conflicts across runs: reports how long each has been unresolved, and flags conflicts that cleared on their own since the last run.
7. **Auto-resolves stale conflicts.** Once a fork has been in conflict for more than 5 runs, the script discards the fork's own divergent commits — resetting its default branch to the merge-base with upstream — and fast-forwards it to the latest upstream. This is automatic and **destructive**: the fork's commits on its default branch are permanently dropped. It only triggers after a conflict has gone unresolved across 6+ runs, and applies only to your own forks.
8. Posts a Slack report every run. Each run posts a brief one-line synced summary plus full detail for any conflicts/errors. The noisy per-repo synced list is accumulated per local-calendar day and posted once, on the first run of the next day, as a "Daily Sync Summary".

Transient `429` / `5xx` responses and GitHub's secondary rate limits are retried with exponential backoff, honoring `Retry-After` when present.

## Requirements

- Python 3.13+
- A GitHub token with `repo` scope (classic) or equivalent fine-grained permissions on the forks you want to sync. The script will use `$GITHUB_TOKEN` if set, otherwise it falls back to `gh auth token`.
- A Slack incoming webhook URL.

Dependencies (`httpx`, `rich`) are declared both in `pyproject.toml` and inline via [PEP 723](https://peps.python.org/pep-0723/) script metadata, so the script can be run directly with `uv` without a virtual environment.

## Setup

Clone and install with [uv](https://docs.astral.sh/uv/):

```sh
git clone <this-repo>
cd github-fork-sync
uv sync
```

Or run the script directly — `uv` will resolve the inline dependencies on the fly:

```sh
uv run sync_forks.py
```

## Configuration

Set these environment variables (a `.envrc` file works well with [direnv](https://direnv.net/)):

| Variable | Required | Purpose |
| --- | --- | --- |
| `SLACK_WEBHOOK_URL` | yes | Incoming webhook for the report channel. |
| `GITHUB_TOKEN` | no | Personal access token. Falls back to `gh auth token` if unset. |

## Usage

```sh
uv run sync_forks.py
```

The script prints a live progress bar showing forks reviewed, synced, and skipped. On completion it persists the run's start time to `sync_state.json` so the next invocation can skip forks whose upstream has not moved since.

To force a full re-check, delete the state file:

```sh
rm sync_state.json
```

## Slack output

A report is posted every run (heartbeat). Each run shows a brief synced line and the full per-run detail for any conflicts, cleared conflicts, transient conflicts, auto-resolved conflicts, or errors. The verbose per-repo synced list is rolled up per local-calendar day and posted once, on the first run of the next day:

```
*GitHub Fork Sync Report*

*📅 Daily Sync Summary — 2026-06-18*
*Synced Repositories:*
• 14 commits: `some-repo`
• 3 commits: `another-repo`, `third-repo`
• 1 commit: `tiny-repo`

*Synced Repositories:* 5 commits across 2 repos

*⚠️ Merge Conflicts (Manual Resolution Required):*
• `divergent-repo` — unresolved across 4 runs (first seen 2026-06-08 09:12 UTC)
```

The dated **Daily Sync Summary** appears only on the first run of a new day and covers the previous day's syncs (each repo's commits summed across that day, grouped descending). The brief **Synced Repositories** line summarizes the current run. Conflicts and errors are never deferred — they keep full detail on the run they occur.

The **Conflicts Cleared Since Last Run** and **Transient Conflicts** sections are the telemetry that answers "was it really a conflict?": a repo that clears on the in-run re-check was GitHub merge-state flicker, while one that clears across runs without intervention was either resolved upstream or eventual consistency. A conflict that keeps incrementing its run count is genuine and needs manual resolution.

## Automating it

The script is designed to be run on a schedule (cron, launchd, GitHub Actions, etc.). Because state is persisted in `sync_state.json`, each run only does meaningful work for upstreams that have moved since the last successful run, keeping API usage low even with hundreds of forks.

## Files

- `sync_forks.py` — the script.
- `sync_state.json` — last-run timestamp, tracked conflicts, and the current day's accumulated synced counts; written by the script (gitignored).
- `pyproject.toml` / `uv.lock` — project metadata and pinned dependencies for `uv sync` workflows.
