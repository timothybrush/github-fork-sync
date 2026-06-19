# Daily Slack Rollup for Synced Repositories — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the noisy per-run "Synced Repositories" list into a once-a-day rollup posted on the first run of the next day, while every run posts a brief one-line synced summary (heartbeat) and conflicts/errors keep full per-run detail.

**Architecture:** Accumulate per-repo day-total commits in a new `daily` block inside `sync_state.json`, keyed by the local calendar date. A set of small pure helpers (`accrue_synced`, `daily_rollover`, `build_daily_summary`, `brief_synced_line`, `build_run_report`) handle accumulation, rollover detection, and rendering — each unit-testable without HTTP. `main()` wires them in: detect rollover at start, accrue at end, always post.

**Tech Stack:** Python 3.13, `httpx`, `rich`; tests via `unittest` (`uv run python -m unittest test_sync_forks`).

**Spec:** `docs/superpowers/specs/2026-06-19-daily-slack-rollup-design.md`

**Reference — current code:** `sync_forks.py` (`build_report` at lines 303-354, `load_state`/`save_state` at 244-268, `main` at 365-521). Tests in `test_sync_forks.py` (`BuildReportTests` 241-290, `StateRoundTripTests` 198-238).

**Conventions:**
- Repo names in `synced_by_count` / `daily.synced` are **short** names (e.g. `repo-a`), matching `main()`'s `short` variable.
- Unknown commit count uses the sentinel `-1` (existing convention).
- Run all tests with: `uv run python -m unittest test_sync_forks -v`
- Commit after each task.

---

### Task 1: Shared grouped renderer + daily summary

**Files:**
- Modify: `sync_forks.py` (add `_synced_bullets` and `build_daily_summary` above `build_report`, after `_format_ts` at line 300)
- Test: `test_sync_forks.py` (add `DailySummaryTests` class)

- [ ] **Step 1: Write the failing test**

Add to `test_sync_forks.py` (after `BuildReportTests` or at end before `if __name__`):

```python
class DailySummaryTests(unittest.TestCase):
    def test_groups_by_total_descending(self):
        text = "\n".join(
            sf.build_daily_summary({"repo-a": 14, "repo-b": 3, "repo-c": 3}, "2026-06-18")
        )
        self.assertIn("*📅 Daily Sync Summary — 2026-06-18*", text)
        self.assertIn("• 14 commits: `repo-a`", text)
        self.assertIn("• 3 commits: `repo-b`, `repo-c`", text)
        self.assertLess(text.index("14 commits"), text.index("3 commits"))

    def test_empty_returns_no_lines(self):
        self.assertEqual(sf.build_daily_summary({}, "2026-06-18"), [])

    def test_unknown_count_bucket(self):
        text = "\n".join(sf.build_daily_summary({"repo-x": -1}, "2026-06-18"))
        self.assertIn("unknown commit count: `repo-x`", text)

    def test_singular_commit(self):
        text = "\n".join(sf.build_daily_summary({"repo-a": 1}, "2026-06-18"))
        self.assertIn("• 1 commit: `repo-a`", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest test_sync_forks.DailySummaryTests -v`
Expected: FAIL with `AttributeError: module 'sync_forks' has no attribute 'build_daily_summary'`

- [ ] **Step 3: Write minimal implementation**

Add to `sync_forks.py` immediately after `_format_ts` (line 300), before `build_report`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest test_sync_forks.DailySummaryTests -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py test_sync_forks.py
git commit -m "Add shared grouped renderer and daily summary builder"
```

---

### Task 2: Brief per-run synced line

**Files:**
- Modify: `sync_forks.py` (add `brief_synced_line` after `build_daily_summary`)
- Test: `test_sync_forks.py` (add `BriefSyncedLineTests`)

- [ ] **Step 1: Write the failing test**

```python
class BriefSyncedLineTests(unittest.TestCase):
    def test_sums_commits_and_counts_repos(self):
        self.assertEqual(
            sf.brief_synced_line({14: ["a"], 3: ["b", "c"]}),
            "*Synced Repositories:* 20 commits across 3 repos",
        )

    def test_empty_run(self):
        self.assertEqual(
            sf.brief_synced_line({}),
            "*Synced Repositories:* 0 commits across 0 repos",
        )

    def test_unknown_count_counts_repo_not_commits(self):
        self.assertEqual(
            sf.brief_synced_line({-1: ["a"], 5: ["b"]}),
            "*Synced Repositories:* 5 commits across 2 repos",
        )

    def test_singular(self):
        self.assertEqual(
            sf.brief_synced_line({1: ["a"]}),
            "*Synced Repositories:* 1 commit across 1 repo",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest test_sync_forks.BriefSyncedLineTests -v`
Expected: FAIL with `AttributeError: ... has no attribute 'brief_synced_line'`

- [ ] **Step 3: Write minimal implementation**

Add to `sync_forks.py` after `build_daily_summary`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest test_sync_forks.BriefSyncedLineTests -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py test_sync_forks.py
git commit -m "Add brief per-run synced summary line"
```

---

### Task 3: Accrue a run's syncs into the day accumulator

**Files:**
- Modify: `sync_forks.py` (add `accrue_synced` after `brief_synced_line`)
- Test: `test_sync_forks.py` (add `AccrueSyncedTests`)

- [ ] **Step 1: Write the failing test**

```python
class AccrueSyncedTests(unittest.TestCase):
    def test_adds_new_repos(self):
        self.assertEqual(sf.accrue_synced({}, {3: ["a"], 5: ["b"]}), {"a": 3, "b": 5})

    def test_sums_repeat_repo_across_runs(self):
        self.assertEqual(sf.accrue_synced({"a": 3}, {5: ["a"]}), {"a": 8})

    def test_unknown_makes_repo_unknown(self):
        self.assertEqual(sf.accrue_synced({"a": 3}, {-1: ["a"]}), {"a": -1})

    def test_unknown_is_sticky(self):
        self.assertEqual(sf.accrue_synced({"a": -1}, {5: ["a"]}), {"a": -1})

    def test_does_not_mutate_input(self):
        original = {"a": 3}
        sf.accrue_synced(original, {5: ["a"]})
        self.assertEqual(original, {"a": 3})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest test_sync_forks.AccrueSyncedTests -v`
Expected: FAIL with `AttributeError: ... has no attribute 'accrue_synced'`

- [ ] **Step 3: Write minimal implementation**

Add to `sync_forks.py` after `brief_synced_line`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest test_sync_forks.AccrueSyncedTests -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py test_sync_forks.py
git commit -m "Add per-day synced accumulator fold"
```

---

### Task 4: Daily rollover decision

**Files:**
- Modify: `sync_forks.py` (add `daily_rollover` after `accrue_synced`)
- Test: `test_sync_forks.py` (add `DailyRolloverTests`)

- [ ] **Step 1: Write the failing test**

```python
class DailyRolloverTests(unittest.TestCase):
    def test_same_day_carries_accumulator(self):
        report, acc = sf.daily_rollover({"date": "2026-06-19", "synced": {"a": 3}}, "2026-06-19")
        self.assertEqual(report, [])
        self.assertEqual(acc, {"a": 3})

    def test_new_day_reports_and_resets(self):
        report, acc = sf.daily_rollover({"date": "2026-06-18", "synced": {"a": 3}}, "2026-06-19")
        self.assertIn("Daily Sync Summary — 2026-06-18", "\n".join(report))
        self.assertEqual(acc, {})

    def test_new_day_with_no_accruals_no_report(self):
        report, acc = sf.daily_rollover({"date": "2026-06-18", "synced": {}}, "2026-06-19")
        self.assertEqual(report, [])
        self.assertEqual(acc, {})

    def test_first_ever_run_no_report(self):
        report, acc = sf.daily_rollover({}, "2026-06-19")
        self.assertEqual(report, [])
        self.assertEqual(acc, {})

    def test_multi_day_gap_reports_under_accrual_date(self):
        report, acc = sf.daily_rollover({"date": "2026-06-15", "synced": {"a": 2}}, "2026-06-19")
        self.assertIn("2026-06-15", "\n".join(report))
        self.assertEqual(acc, {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest test_sync_forks.DailyRolloverTests -v`
Expected: FAIL with `AttributeError: ... has no attribute 'daily_rollover'`

- [ ] **Step 3: Write minimal implementation**

Add to `sync_forks.py` after `accrue_synced`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest test_sync_forks.DailyRolloverTests -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py test_sync_forks.py
git commit -m "Add daily rollover decision helper"
```

---

### Task 5: Replace `build_report` with `build_run_report`

Replaces the monolithic report builder. The synced grouped list is gone from the per-run message (replaced by the brief line); an optional previous-day block is prepended; all other sections are unchanged.

**Files:**
- Modify: `sync_forks.py` (replace `build_report`, lines 303-354)
- Test: `test_sync_forks.py` (replace `BuildReportTests`, lines 241-290, with `BuildRunReportTests`)

- [ ] **Step 1: Write the failing test**

Delete the entire `BuildReportTests` class (lines 241-290) and replace with:

```python
class BuildRunReportTests(unittest.TestCase):
    def test_includes_brief_synced_line(self):
        report = "\n".join(sf.build_run_report({3: ["a"]}, [], [], [], []))
        self.assertIn("*Synced Repositories:* 3 commits across 1 repo", report)

    def test_conflict_shows_persistence_duration(self):
        report = "\n".join(
            sf.build_run_report({}, [("fork", 3, EARLIER.isoformat())], [], [], [])
        )
        self.assertIn("unresolved across 3 runs", report)

    def test_resolved_section_reports_self_clearing(self):
        report = "\n".join(
            sf.build_run_report(
                {}, [], [], [("fork", {"first_seen": EARLIER.isoformat(), "runs": 2})], []
            )
        )
        self.assertIn("Cleared Since Last Run", report)
        self.assertIn("cleared without intervention after being flagged 2 runs", report)

    def test_flaky_section(self):
        report = "\n".join(sf.build_run_report({}, [], [], [], ["fork"]))
        self.assertIn("Transient Conflicts", report)
        self.assertIn("`fork`", report)

    def test_force_resolved_section(self):
        report = "\n".join(
            sf.build_run_report({}, [], [], [], [], force_resolved=[("fork", 11, 3)])
        )
        self.assertIn("Auto-Resolved Conflicts", report)
        self.assertIn("discarded 3 commits", report)
        self.assertIn("after 11 runs", report)

    def test_force_resolved_singular_commit(self):
        report = "\n".join(
            sf.build_run_report({}, [], [], [], [], force_resolved=[("fork", 6, 1)])
        )
        self.assertIn("discarded 1 commit and", report)

    def test_prev_day_block_precedes_brief_line(self):
        prev = sf.build_daily_summary({"a": 5}, "2026-06-18")
        report = "\n".join(
            sf.build_run_report({3: ["b"]}, [], [], [], [], prev_day_lines=prev)
        )
        self.assertLess(report.index("Daily Sync Summary"), report.index("3 commits across 1 repo"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest test_sync_forks.BuildRunReportTests -v`
Expected: FAIL with `AttributeError: ... has no attribute 'build_run_report'`

- [ ] **Step 3: Write minimal implementation**

In `sync_forks.py`, replace the whole `build_report` function (lines 303-354) with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest test_sync_forks.BuildRunReportTests -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py test_sync_forks.py
git commit -m "Replace build_report with per-run report (brief line + prev-day block)"
```

---

### Task 6: `local_today` + extend state persistence with the `daily` block

**Files:**
- Modify: `sync_forks.py` (`load_state` 244-261, `save_state` 264-268; add `local_today` near them)
- Test: `test_sync_forks.py` (update `StateRoundTripTests` 198-238; add `LocalTodayTests`)

- [ ] **Step 1: Write the failing test**

Replace the three methods inside `StateRoundTripTests` (`test_save_then_load`, `test_legacy_state_without_conflicts_key`, `test_missing_file`) with these, and add a new `LocalTodayTests` class:

```python
    def test_save_then_load(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._use_tmp(tmp)
            conflicts = {"me/fork": {"first_seen": NOW.isoformat(), "runs": 2}}
            daily = {"date": "2026-06-08", "synced": {"a": 3}}
            sf.save_state(NOW, conflicts, daily)
            last_run, loaded, loaded_daily = sf.load_state()
            self.assertEqual(last_run, NOW)
            self.assertEqual(loaded, conflicts)
            self.assertEqual(loaded_daily, daily)

    def test_legacy_state_without_conflicts_key(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            sf.STATE_FILE = Path(tmp) / "sync_state.json"
            sf.STATE_FILE.write_text(json.dumps({"last_run": NOW.isoformat()}))
            last_run, loaded, daily = sf.load_state()
            self.assertEqual(last_run, NOW)
            self.assertEqual(loaded, {})
            self.assertEqual(daily, {})

    def test_missing_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            sf.STATE_FILE = Path(tmp) / "nope.json"
            self.assertEqual(sf.load_state(), (None, {}, {}))


class LocalTodayTests(unittest.TestCase):
    def test_returns_iso_date_string(self):
        self.assertRegex(sf.local_today(), r"^\d{4}-\d{2}-\d{2}$")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest test_sync_forks.StateRoundTripTests test_sync_forks.LocalTodayTests -v`
Expected: FAIL — `save_state()` takes 2 positional args / `local_today` missing / `load_state` returns 2-tuple.

- [ ] **Step 3: Write minimal implementation**

In `sync_forks.py`, replace `load_state` (244-261) and `save_state` (264-268) with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest test_sync_forks.StateRoundTripTests test_sync_forks.LocalTodayTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py test_sync_forks.py
git commit -m "Extend state with daily block; add local_today helper"
```

---

### Task 7: Wire `main()` — rollover at start, accrue at end, always post

**Files:**
- Modify: `sync_forks.py` (`main`, lines 365-521)

No new unit tests (the helpers are covered; `main` is the I/O-bound orchestrator). Verification is the full suite plus a syntax/import check.

- [ ] **Step 1: Update the state load and add rollover at the top of `main`**

Replace lines 366-367:

```python
    started_at = datetime.now(timezone.utc)
    last_run, prior_conflicts = load_state()
```

with:

```python
    started_at = datetime.now(timezone.utc)
    today = local_today()
    last_run, prior_conflicts, daily_state = load_state()
    prev_day_lines, day_synced = daily_rollover(daily_state, today)
```

- [ ] **Step 2: Update the save + accrual after the loop**

Replace line 491:

```python
    save_state(started_at, current_conflicts)
```

with:

```python
    day_synced = accrue_synced(day_synced, synced_by_count)
    save_state(started_at, current_conflicts, {"date": today, "synced": day_synced})
```

- [ ] **Step 3: Replace the conditional Slack block with an unconditional heartbeat post**

Replace lines 510-521 (the `if synced_by_count or conflicts ... else ...` block):

```python
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
```

with:

```python
    console.print("Sending Slack notification…")
    notify_slack(
        webhook_url,
        build_run_report(
            synced_by_count, conflicts, errors, resolved, flaky, force_resolved, prev_day_lines
        ),
    )
```

- [ ] **Step 4: Verify import + full suite pass**

Run: `uv run python -c "import sync_forks"`
Expected: no output (imports cleanly — no lingering `build_report` references).

Run: `uv run python -m unittest test_sync_forks -v`
Expected: PASS (all tests; original 27 minus removed `BuildReportTests` (6) plus new tests).

Run: `grep -n "build_report" sync_forks.py test_sync_forks.py`
Expected: no matches (the old function name is fully gone).

- [ ] **Step 5: Commit**

```bash
git add sync_forks.py
git commit -m "Wire daily rollover, per-day accrual, and heartbeat posting into main"
```

---

### Task 8: Update README

**Files:**
- Modify: `README.md` (the "What it does" item 8, the "Slack output" section, and the "Files" section)

- [ ] **Step 1: Update "What it does" item 8**

Replace the line:

```markdown
8. Posts a grouped Slack report — only when something actually happened.
```

with:

```markdown
8. Posts a Slack report every run. Each run posts a brief one-line synced summary plus full detail for any conflicts/errors. The noisy per-repo synced list is accumulated per local-calendar day and posted once, on the first run of the next day, as a "Daily Sync Summary".
```

- [ ] **Step 2: Update the "Slack output" section**

Replace the intro sentence and example block under `## Slack output` (from "The report is sent only when..." through the closing ``` of the example) with:

````markdown
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
````

- [ ] **Step 3: Update the "Files" section**

Replace:

```markdown
- `sync_state.json` — last-run timestamp, written by the script (gitignored).
```

with:

```markdown
- `sync_state.json` — last-run timestamp, tracked conflicts, and the current day's accumulated synced counts; written by the script (gitignored).
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document daily Slack rollup and heartbeat reporting"
```

---

## Self-Review

**Spec coverage:**
- State model (`daily` block, per-repo totals, sticky `-1`) → Tasks 3, 6.
- Local day boundary → Task 6 (`local_today`), Task 4 (`daily_rollover` by date string).
- Rollover reports previous day then resets → Task 4, wired in Task 7.
- Multi-day gap reports under accrual date → Task 4 `test_multi_day_gap_reports_under_accrual_date`.
- Per-run message order (prev-day block → brief line → sections) → Task 5.
- Brief math (unknown counts toward repos not commits; `0 across 0`) → Task 2.
- Heartbeat every run → Task 7 Step 3.
- Daily summary reuses grouped renderer (descending, plural, `-1`) → Task 1.
- Legacy state without `daily` still loads → Task 6 `test_legacy_state_without_conflicts_key`.
- README → Task 8.

**Placeholder scan:** none — every code/test step contains full content.

**Type consistency:** `build_daily_summary(daily_synced, date)`, `accrue_synced(daily_synced, run_synced_by_count)`, `daily_rollover(daily, today) -> (list, dict)`, `brief_synced_line(synced_by_count)`, `build_run_report(synced, conflicts, errors, resolved, flaky, force_resolved=None, prev_day_lines=None)`, `load_state() -> (datetime|None, dict, dict)`, `save_state(run_time, conflicts, daily)`, `local_today() -> str` — names and signatures are consistent across tasks and call sites in Task 7.
