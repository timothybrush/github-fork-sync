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
8. Posts a grouped Slack report — only when something actually happened.

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

The report is sent only when at least one fork was synced, hit a merge conflict, or errored. It groups synced repos by how many commits they were behind, so the most active upstreams float to the top:

```
*GitHub Fork Sync Report*

*Synced Repositories:*
• 14 commits: `some-repo`
• 3 commits: `another-repo`, `third-repo`
• 1 commit: `tiny-repo`

*Auto-Resolved Conflicts (divergent commits discarded to fast-forward):*
• `stale-repo` — discarded 2 commits and fast-forwarded after 6 runs in conflict

*Merge Conflicts (Manual Resolution Required):*
• `divergent-repo` — unresolved across 4 runs (first seen 2026-06-08 09:12 UTC)
• `new-conflict-repo` — new this run

*Conflicts Cleared Since Last Run:*
• `flapping-repo` — cleared without intervention after being flagged 2 runs (first seen 2026-06-08 08:30 UTC)

*Transient Conflicts (cleared on in-run re-check):*
• `racey-repo`

*Errors Encountered:*
• broken-repo: <error message from GitHub>
```

The **Conflicts Cleared Since Last Run** and **Transient Conflicts** sections are the telemetry that answers "was it really a conflict?": a repo that clears on the in-run re-check was GitHub merge-state flicker, while one that clears across runs without intervention was either resolved upstream or eventual consistency. A conflict that keeps incrementing its run count is genuine and needs manual resolution.

If every fork is already up to date, no Slack notification is sent.

## Automating it

The script is designed to be run on a schedule (cron, launchd, GitHub Actions, etc.). Because state is persisted in `sync_state.json`, each run only does meaningful work for upstreams that have moved since the last successful run, keeping API usage low even with hundreds of forks.

## Files

- `sync_forks.py` — the script.
- `sync_state.json` — last-run timestamp, written by the script (gitignored).
- `pyproject.toml` / `uv.lock` — project metadata and pinned dependencies for `uv sync` workflows.
