Fix a red CI round on the pull request this build opened.

This is one iteration of build-basic's `ci-gate` loop. After you close, an exec
check (`ci-gate.sh`) waits for the PR's CI without any model and records the
verdict on the workflow root. You never wait for CI yourself: no polling, no
sleeping, no scheduled wakeups.

## Inputs

- The claimed bead's `gc.attempt` metadata is this iteration's number.
- The workflow root is the claimed bead's `gc.root_bead_id`. Read it with
  `gc bd show <root-id> --json` (unwrap a one-element list). It carries:
  - `gc.build.ci_findings_path`: the findings file the last red gate wrote
    (failing checks, their descriptions and URLs, and capped log tails);
  - `gc.build.pr_head_branch`: the PR's head branch;
  - `gc.build.pr_head_worktree`: the worktree that holds that branch;
  - `gc.build.pr_url`: the PR, for your close reason only.
- Round cap: `{{ci_fix_rounds}}` fix rounds; the gate enforces it.

## 1. Iteration 1 is a no-op

If `gc.attempt` is `1`, or the root has no `gc.build.ci_findings_path`, there
is nothing to fix yet. Record and close at once:

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=pass' \
  --set-metadata 'ci_fix.action=noop'
gc bd close "$CLAIMED_BEAD_ID" --reason 'CI gate round 1: nothing to fix yet.'
```

## 2. Get onto the PR branch

Read the findings file. Then work on the **same branch** the PR already uses:

```bash
cd "<gc.build.pr_head_worktree>"
test "$(git rev-parse --abbrev-ref HEAD)" = "<gc.build.pr_head_branch>"
git fetch origin
git merge --ff-only "origin/<gc.build.pr_head_branch>"
```

If that worktree is gone, recreate it from the remote branch and record the
new path on the workflow root, so later rounds and the merge find it:

```bash
git worktree add "<new absolute path>" "origin/<gc.build.pr_head_branch>"
cd "<new absolute path>" && git checkout -B "<gc.build.pr_head_branch>" "origin/<gc.build.pr_head_branch>"
gc bd update "<workflow-root-id>" --set-metadata 'gc.build.pr_head_worktree=<new absolute path>'
```

## 3. Fix, prove, commit, push

Make the smallest change that fixes the failures named in the findings. Run the
repository's relevant proof commands for that change. Then commit and push to
the same branch with a plain push:

```bash
git add <changed files>
git commit -m 'fix(ci): <what you fixed>'
git push origin "HEAD:refs/heads/<gc.build.pr_head_branch>"
```

If the push is rejected as non-fast-forward, run `git fetch origin`, rebase
onto `origin/<gc.build.pr_head_branch>`, rerun the proof, and push again.

Rules for every round:

- Never force-push. Never open a new pull request or a new branch.
- Never push an empty commit to retrigger CI.
- Never re-run, cancel or retrigger CI jobs; the gate judges the new head sha.
- Never commit the findings file or anything else under the artifact root.

## 4. Record and close

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=pass' \
  --set-metadata 'ci_fix.action=pushed' \
  --set-metadata 'ci_fix.pushed_sha=<full sha you pushed>'
gc bd close "$CLAIMED_BEAD_ID" --reason 'CI fix pushed to <gc.build.pr_head_branch> at <short sha>.'
```

## 5. When code cannot fix it

If the failure is not in the code (a runner or infrastructure outage, missing
secrets, a flaky external service), do not push anything. Record why and close
with a pass, so the gate ends the loop and the run reports the PR as red:

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=pass' \
  --set-metadata 'ci_fix.action=gave_up' \
  --set-metadata 'ci_fix.reason=<one line, no secrets>'
gc bd close "$CLAIMED_BEAD_ID" --reason 'CI fix gave up: <one line>.'
```
