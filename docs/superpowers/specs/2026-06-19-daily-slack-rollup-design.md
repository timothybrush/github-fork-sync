# Daily Slack Rollup for Synced Repositories — Design

**Date:** 2026-06-19
**Component:** `sync_forks.py`

## Problem

The script runs several times a day, and most runs sync a long list of
repositories. Today every run posts the full grouped "Synced Repositories" list
to Slack, so the channel fills with large, repetitive messages. The signal
(conflicts, errors) is buried under the noise of routine syncs.

## Goal

Collapse the noisy per-run synced list into a single **once-a-day** rollup,
while keeping actionable results (conflicts, errors) immediate.

- Accumulate each repo's synced commits over the course of a (local) calendar
  day in `sync_state.json`.
- On the **first run of a new day**, post the **previous day's** full grouped
  synced report, then reset the accumulator and run that day's first sync.
- Every run still posts a **brief** line: `Synced Repositories: N commits across
  M repos` for that run. On a rollover run, this brief line appears **after** the
  previous day's full report, in the same Slack message.
- Conflicts, cleared conflicts, flaky conflicts, auto-resolved conflicts, and
  errors keep their **full per-run detail** — they do not roll up. Deferring them
  by up to a day would delay response to real problems.

## Decisions (from brainstorming)

- **Rollup scope:** only the synced list rolls up. All other categories stay
  per-run with full detail.
- **Day boundary:** the **local** calendar date of the machine running the
  script (not UTC). Existing `last_run` timestamps remain UTC; the day boundary
  is a separate, local-date comparison.
- **Brief cadence:** the brief line posts on **every** run (heartbeat), even when
  nothing synced (`0 commits across 0 repos`).
- **Daily total per repo:** a repo synced multiple times in a day is summed into
  a single day-total and shown once (e.g. morning 3 + afternoon 5 → `8 commits`),
  not as a per-sync breakdown.

## State model

`sync_state.json` gains a `daily` block:

```json
{
  "last_run": "2026-06-19T12:13:34.122989+00:00",
  "conflicts": { },
  "daily": {
    "date": "2026-06-18",
    "synced": { "repo-a": 14, "repo-b": 3, "repo-c": -1 }
  }
}
```

- `daily.date` — the **local** calendar date (`YYYY-MM-DD`) the accruals belong
  to.
- `daily.synced` — maps each repo's short name to its **day-total** commits.
  An unknown-count sync uses the existing `-1` sentinel; once a repo is marked
  `-1` for the day it stays `-1` (mixing a known count with an unknown one keeps
  it unknown, since the true total can't be computed).
- Load is tolerant: a missing `daily` key yields an empty accumulator, exactly
  like the legacy-`conflicts` handling already in `load_state`.

## Run flow

1. Load state; compute `today` = local calendar date.
2. **Rollover check:** if `daily.date` is present, differs from `today`, and has
   accruals → build the previous-day full report block and mark accruals for
   reset. If the script skipped one or more whole days, the accruals are reported
   under their own `daily.date` (the last day that actually accrued), not as
   "yesterday".
3. Run the sync (unchanged core logic).
4. Merge this run's synced repos into `daily.synced`: start from the existing
   accumulator if same-day, or from empty if this run rolled over. Persist the
   updated `daily` block (with `date = today`) alongside `last_run` and
   `conflicts`.

## Slack message per run

A single post, assembled in this order:

1. *(rollover runs only)* the previous-day full synced report, headed
   `*📅 Daily Sync Summary — 2026-06-18*` followed by the grouped list.
2. The brief line: `*Synced Repositories:* N commits across M repos` — this run's
   totals (`N` = sum of commits synced this run, `M` = count of repos synced this
   run; unknown-count syncs contribute to `M` but not to `N`).
3. The per-run conflict / cleared / flaky / auto-resolved / error sections —
   **unchanged** from today.

The message posts every run (heartbeat). The previous-day block fires on the
first run of a new day even if that run itself synced nothing.

## Daily summary rendering

The previous-day block reuses the existing grouped-list logic. `daily.synced`
(`{repo: day_total}`) is inverted into the `{count: [repos]}` structure that the
current synced renderer already consumes, so descending-count sort, singular /
plural `commit`, and the `-1` unknown bucket all stay identical and covered by
existing tests.

Example:

```
*📅 Daily Sync Summary — 2026-06-18*
*Synced Repositories:*
• 14 commits: `repo-a`
• 3 commits: `repo-b`, `repo-c`
```

## Code shape

Refactor the monolithic `build_report` into focused, independently testable
pieces:

- `build_synced_section(synced_by_count) -> list[str]` — the grouped list,
  extracted from today's `build_report`. Reused by both the daily block and (if
  desired) elsewhere.
- `build_daily_summary(daily_synced, date) -> list[str]` — inverts
  `{repo: total}` → `{count: [repos]}`, prepends the dated header, calls
  `build_synced_section`.
- `build_run_report(...)` — the per-run message: brief synced line + the existing
  conflict / cleared / flaky / auto-resolved / error sections.
- `daily_rollover(daily, today) -> tuple[list[str], dict]` — pure helper deciding
  report-vs-reset: returns the previous-day report lines (empty if no rollover)
  and the accumulator to carry into this run.
- `accrue_synced(daily_synced, run_synced_by_count) -> dict` — folds a run's
  results into the day accumulator (summing, `-1` stickiness).
- `load_state` / `save_state` — extended to read/write the `daily` block.
- `local_today() -> date` — small helper for the local calendar date, isolated so
  tests can pin it.

Keeping these as small pure functions (no I/O, no network) means the rollover,
accrual, and rendering logic are all unit-testable without HTTP mocking, matching
the existing test style.

## Tests

- **Rollover:** fires when `daily.date != today` with accruals present; produces
  the dated summary; resets the accumulator.
- **No rollover:** same-day run leaves accruals intact and emits no daily block.
- **Accrual:** repo synced twice in a day sums; a second sync of a known-count
  repo with an unknown count makes it `-1`; `-1` stays `-1`.
- **Multi-day gap:** accruals from a skipped-over day report under their own
  `daily.date`, not under "yesterday".
- **Brief line math:** `N`/`M` computed correctly, including unknown-count syncs
  counting toward `M` but not `N`; `0 commits across 0 repos` on an empty run.
- **Heartbeat:** the brief line posts even when nothing synced and there are no
  conflicts/errors.
- **State round-trip:** new `daily` key persists and reloads; legacy state
  without a `daily` key still loads (empty accumulator).
- **Daily summary rendering:** inversion to `{count: [repos]}`, descending sort,
  singular/plural, `-1` unknown bucket, dated header.

## Out of scope / next-version Slack reporting

Forward-looking ideas, deliberately **not** in this change, captured because the
user asked how reporting could improve next:

1. **Slack Block Kit** instead of a single `text` blob — real sections, fields,
   and dividers; collapsible/structured layout; severity-colored attachments for
   conflicts and errors.
2. **Threaded daily digest.** Switch from an incoming webhook to the Slack Web
   API (`chat.postMessage`, needs a bot token) so the day's first message is a
   parent and each subsequent run's brief posts as a **thread reply** — channel
   stays clean, detail is one click away.
3. **In-place daily message** via `chat.update` — maintain one "today" message
   that updates live with running totals instead of posting per run.
4. **Severity routing / @mentions** — only ping the channel (or a person) for
   conflicts and errors that need manual action; keep the heartbeat quiet.
5. **Append-only event log** (the deferred "Approach C") — record every run's
   results to a log file, enabling weekly rollups, per-repo history, and trend
   reporting beyond the single-day window.
6. **Clickable repo links** — render repo names as links to the GitHub
   repo / compare view.

These would mostly be additive layers over the per-run/daily split this change
introduces; (2) and (5) are the highest-leverage next steps.
