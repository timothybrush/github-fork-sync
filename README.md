# Sync Github Forks

A vibe coded single-file Python script that keeps every GitHub fork you own in sync with its upstream parent, then reports the results to Slack.

If you maintain dozens — or hundreds — of forks, manually clicking "Sync fork" in the GitHub UI does not scale. `sync_forks.py` walks your forks via the GitHub GraphQL API, merges upstream changes into each default branch via the REST API, and posts a tidy summary to a Slack channel.

## What it does

1. Lists every repository you own that is a fork (paginated via GraphQL, 25 at a time).
2. Skips forks whose upstream has not been pushed to since the last successful run (timestamp persisted in `sync_state.json`).
3. For each remaining fork, asks GitHub how many commits behind upstream it is.
4. Calls the [`/merge-upstream`](https://docs.github.com/en/rest/branches/branches#sync-a-fork-branch-with-the-upstream-repository) endpoint to fast-forward the fork's default branch.
5. Classifies the outcome as synced, merge conflict, no-op (API cache artifact), or error.
6. Posts a grouped Slack report — only when something actually happened.

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
cd update
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

*Merge Conflicts (Manual Resolution Required):*
• `divergent-repo`

*Errors Encountered:*
• broken-repo: <error message from GitHub>
```

If every fork is already up to date, no Slack notification is sent.

## Automating it

The script is designed to be run on a schedule (cron, launchd, GitHub Actions, etc.). Because state is persisted in `sync_state.json`, each run only does meaningful work for upstreams that have moved since the last successful run, keeping API usage low even with hundreds of forks.

## Files

- `sync_forks.py` — the script.
- `sync_state.json` — last-run timestamp, written by the script (gitignored).
- `pyproject.toml` / `uv.lock` — project metadata and pinned dependencies for `uv sync` workflows.
