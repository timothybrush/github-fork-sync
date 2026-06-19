#!/usr/bin/env python3
"""Tests for sync_forks. Run with: uv run python -m unittest test_sync_forks -v"""

import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

import sync_forks as sf

NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(hours=1)
LATER = NOW + timedelta(hours=1)


def github_with(handler) -> sf.GitHub:
    """A GitHub client whose HTTP calls are served by `handler` (no network)."""
    gh = sf.GitHub("token")
    gh.client = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    return gh


class ShouldCheckTests(unittest.TestCase):
    """The gate that decides whether a fork is examined this run."""

    def test_stale_upstream_not_conflicted_is_skipped(self):
        parent = {"pushedAt": EARLIER.isoformat()}
        self.assertFalse(sf.should_check(parent, NOW, was_conflicted=False))

    def test_fresh_upstream_is_checked(self):
        parent = {"pushedAt": LATER.isoformat()}
        self.assertTrue(sf.should_check(parent, NOW, was_conflicted=False))

    def test_regression_conflicted_fork_is_rechecked_even_with_stale_upstream(self):
        # The bug: a fork left in conflict whose upstream has NOT moved since our
        # baseline used to be skipped, so the conflict silently "vanished" next run.
        parent = {"pushedAt": EARLIER.isoformat()}
        self.assertTrue(sf.should_check(parent, NOW, was_conflicted=True))

    def test_no_timestamp_forces_check(self):
        self.assertTrue(sf.should_check({}, NOW, was_conflicted=False))


class CommitsBehindTests(unittest.TestCase):
    def test_uses_upstream_branch_name_on_head_side(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"ahead_by": 4})

        gh = github_with(handler)
        behind = gh.commits_behind("me/fork", "master", "up", "main")
        self.assertEqual(behind, 4)
        self.assertEqual(seen["path"], "/repos/me/fork/compare/master...up:main")

    def test_failure_returns_none_not_zero(self):
        gh = github_with(lambda req: httpx.Response(404, json={}))
        self.assertIsNone(gh.commits_behind("me/fork", "main", "up", "main"))


class SyncForkRecheckTests(unittest.TestCase):
    def setUp(self):
        self._delay = sf.CONFLICT_RECHECK_DELAY
        sf.CONFLICT_RECHECK_DELAY = 0  # don't sleep in tests

    def tearDown(self):
        sf.CONFLICT_RECHECK_DELAY = self._delay

    def test_persistent_conflict_stays_conflict(self):
        gh = github_with(lambda req: httpx.Response(409, text="conflict"))
        result = gh.sync_fork("me/fork", "main")
        self.assertTrue(result.conflict)
        self.assertFalse(result.flaky)

    def test_transient_conflict_clears_and_is_flagged_flaky(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(409, text="conflict")
            return httpx.Response(200, json={"merge_type": "fast-forward"})

        gh = github_with(handler)
        result = gh.sync_fork("me/fork", "main")
        self.assertEqual(calls["n"], 2)
        self.assertTrue(result.success)
        self.assertTrue(result.flaky)
        self.assertFalse(result.conflict)

    def test_clean_merge_does_not_recheck(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"merge_type": "fast-forward"})

        gh = github_with(handler)
        result = gh.sync_fork("me/fork", "main")
        self.assertEqual(calls["n"], 1)
        self.assertTrue(result.success)


class ShouldAutoResolveTests(unittest.TestCase):
    """The threshold gate for auto-resolving a long-standing conflict."""

    def test_at_threshold_not_resolved(self):
        self.assertFalse(sf.should_auto_resolve(sf.AUTO_RESOLVE_AFTER_RUNS))

    def test_one_past_threshold_is_resolved(self):
        self.assertTrue(sf.should_auto_resolve(sf.AUTO_RESOLVE_AFTER_RUNS + 1))

    def test_well_under_threshold_not_resolved(self):
        self.assertFalse(sf.should_auto_resolve(1))


class UpstreamDivergenceTests(unittest.TestCase):
    def test_parses_counts_and_merge_base_from_compare(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(
                200,
                json={
                    "ahead_by": 4,  # upstream ahead of fork -> fork behind by 4
                    "behind_by": 3,  # fork ahead of upstream -> 3 divergent commits
                    "merge_base_commit": {"sha": "base123"},
                },
            )

        gh = github_with(handler)
        div = gh.upstream_divergence("me/fork", "master", "up", "main")
        self.assertEqual(seen["path"], "/repos/me/fork/compare/master...up:main")
        self.assertEqual(div.behind, 4)
        self.assertEqual(div.diverged, 3)
        self.assertEqual(div.merge_base_sha, "base123")

    def test_failure_returns_none(self):
        gh = github_with(lambda req: httpx.Response(404, json={}))
        self.assertIsNone(gh.upstream_divergence("me/fork", "main", "up", "main"))

    def test_missing_merge_base_returns_none(self):
        gh = github_with(lambda req: httpx.Response(200, json={"ahead_by": 1, "behind_by": 1}))
        self.assertIsNone(gh.upstream_divergence("me/fork", "main", "up", "main"))


class ForceResolveTests(unittest.TestCase):
    def test_reset_branch_forces_ref_update(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        gh = github_with(handler)
        gh.reset_branch("me/fork", "main", "base123")
        self.assertEqual(seen["method"], "PATCH")
        self.assertEqual(seen["path"], "/repos/me/fork/git/refs/heads/main")
        self.assertEqual(seen["body"], {"sha": "base123", "force": True})

    def test_resets_then_fast_forwards(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "PATCH":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"merge_type": "fast-forward"})

        gh = github_with(handler)
        result = gh.force_resolve("me/fork", "main", "base123")
        self.assertTrue(result.success)
        self.assertFalse(result.no_op)
        self.assertEqual(calls[0], ("PATCH", "/repos/me/fork/git/refs/heads/main"))
        self.assertEqual(calls[1], ("POST", "/repos/me/fork/merge-upstream"))

    def test_failed_reset_does_not_merge(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(422, text="reset rejected")

        gh = github_with(handler)
        result = gh.force_resolve("me/fork", "main", "base123")
        self.assertFalse(result.success)
        self.assertIn("branch reset failed", result.error)
        self.assertEqual(calls, ["PATCH"])  # never attempted the merge


class StateRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._orig = sf.STATE_FILE

    def tearDown(self):
        sf.STATE_FILE = self._orig

    def _use_tmp(self, tmpdir):
        from pathlib import Path

        sf.STATE_FILE = Path(tmpdir) / "sync_state.json"

    def test_save_then_load(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._use_tmp(tmp)
            conflicts = {"me/fork": {"first_seen": NOW.isoformat(), "runs": 2}}
            sf.save_state(NOW, conflicts)
            last_run, loaded = sf.load_state()
            self.assertEqual(last_run, NOW)
            self.assertEqual(loaded, conflicts)

    def test_legacy_state_without_conflicts_key(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            sf.STATE_FILE = Path(tmp) / "sync_state.json"
            sf.STATE_FILE.write_text(json.dumps({"last_run": NOW.isoformat()}))
            last_run, loaded = sf.load_state()
            self.assertEqual(last_run, NOW)
            self.assertEqual(loaded, {})

    def test_missing_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            sf.STATE_FILE = Path(tmp) / "nope.json"
            self.assertEqual(sf.load_state(), (None, {}))


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


if __name__ == "__main__":
    unittest.main()
