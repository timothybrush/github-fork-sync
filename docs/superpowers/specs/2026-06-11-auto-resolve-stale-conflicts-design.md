# Auto-Resolve Stale Fork Conflicts — Design

**Date:** 2026-06-11
**Component:** `sync_forks.py`

## Problem

Some forks sit in an unresolvable merge-conflict state run after run. The script
tracks how long each conflict has persisted (`runs` counter in `sync_state.json`)
but never acts on it — a genuinely diverged fork just keeps getting reported as a
manual-resolution item forever.

## Goal

After a conflict has persisted for **more than 5 runs**, automatically resolve it
by discarding the fork's divergent commits so its default branch can fast-forward
to upstream.

## Key Insight

For a diverged fork (it has commits upstream lacks *and* is behind upstream),
fast-forwarding to upstream requires the fork's branch tip to be an ancestor of
upstream. Any retained divergent commit breaks that. So "discard the necessary
number of commits" is deterministic: discard **all** of the fork's divergent
commits (`behind_by` from the compare API), which means resetting the branch ref
to the **merge-base** with upstream. A subsequent `merge-upstream` then
fast-forwards cleanly.

## Behavior

- Constant `AUTO_RESOLVE_AFTER_RUNS = 5`. A conflict is eligible for
  auto-resolution when the run count being recorded this run is `> 5` (the 6th
  persistent run onward).
- **Fully automatic** — no env flag, no dry-run. When a re-confirmed conflict is
  eligible, the script force-resets the fork and fast-forwards it.
- This is destructive: it permanently discards the fork's own commits on its
  default branch. These are the user's own forks and the action only triggers
  after 5+ runs of an unresolvable conflict.

## Mechanism

When a conflict is re-confirmed in the main loop and `should_auto_resolve(runs)`:

1. `upstream_divergence(repo, branch, up_owner, up_branch)` — one `/compare`
   call returning `(behind, diverged, merge_base_sha)`, or `None` if the compare
   is unreadable. `diverged` is the count of commits that will be discarded.
2. `force_resolve(repo, branch, merge_base_sha)`:
   - `reset_branch(repo, branch, merge_base_sha)` → `PATCH
     /repos/{repo}/git/refs/heads/{branch}` with `{"sha": ..., "force": true}`.
   - `_merge_upstream(repo, branch)` → fast-forward up to upstream.
   - Returns a `SyncResult`.
3. On success → record in a new `force_resolved` list; the fork **drops out** of
   `current_conflicts` (cleared). On failure or unreadable compare → the fork
   stays a tracked conflict and an error is noted.

## New / changed code

- `GitHub.upstream_divergence` — new.
- `GitHub.reset_branch` — new.
- `GitHub.force_resolve` — new.
- `should_auto_resolve(runs) -> bool` — module-level helper, `runs >
  AUTO_RESOLVE_AFTER_RUNS`.
- `build_report(...)` — new keyword arg `force_resolved` (default `[]`) rendering
  a "🛠️ Auto-Resolved Conflicts" section. Added as a trailing keyword arg so
  existing call sites and tests are unaffected.
- Main loop `result.conflict` branch — branch on `should_auto_resolve(runs)`.

## Reporting

New Slack section:

```
*🛠️ Auto-Resolved Conflicts (divergent commits discarded to fast-forward):*
• `oh-my-pi` — discarded 3 divergent commits and fast-forwarded after 11 runs
```

## Tests

- `upstream_divergence` parses `merge_base_commit.sha` + counts and hits the
  correct compare path; failure → `None`.
- `reset_branch` issues PATCH with `force: true` to the refs endpoint.
- `force_resolve` resets then fast-forwards and returns success; a still-failing
  merge surfaces as non-success.
- `should_auto_resolve` threshold matrix (5 → False, 6 → True).
- `build_report` renders the new section.
