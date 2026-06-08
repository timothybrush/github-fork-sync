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


class BuildReportTests(unittest.TestCase):
    def test_conflict_shows_persistence_duration(self):
        report = "\n".join(
            sf.build_report(
                synced={},
                conflicts=[("fork", 3, EARLIER.isoformat())],
                errors=[],
                resolved=[],
                flaky=[],
            )
        )
        self.assertIn("unresolved across 3 runs", report)

    def test_resolved_section_reports_self_clearing(self):
        report = "\n".join(
            sf.build_report(
                synced={},
                conflicts=[],
                errors=[],
                resolved=[("fork", {"first_seen": EARLIER.isoformat(), "runs": 2})],
                flaky=[],
            )
        )
        self.assertIn("Cleared Since Last Run", report)
        self.assertIn("cleared without intervention after being flagged 2 runs", report)

    def test_flaky_section(self):
        report = "\n".join(
            sf.build_report({}, [], [], [], flaky=["fork"])
        )
        self.assertIn("Transient Conflicts", report)
        self.assertIn("`fork`", report)

    def test_unknown_commit_count_bucket(self):
        report = "\n".join(sf.build_report({-1: ["fork"]}, [], [], [], []))
        self.assertIn("unknown commit count", report)


if __name__ == "__main__":
    unittest.main()
