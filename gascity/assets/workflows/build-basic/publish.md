Use the built-in Gas City publish flow.

If publishing is enabled, publish the finalized build-basic result with the existing publish helper. If publishing is disabled, record a no-op publish outcome with the final artifact paths.

For build-basic, a finalized result can be an approved source anchor/worktree.
Do not mark publish failed or downgrade the workflow merely because the launcher
rig root was not mutated. When publishing is disabled, record a `noop` publish
result while preserving the approved build outcome.

`gc.outcome` is the workflow step outcome, not the publish mode. Never set
`gc.outcome=noop`. A disabled/no-op publish is a successful publish step:

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=pass' \
  --set-metadata 'gc.publish_outcome=noop' \
  --set-metadata 'gc.publish_mode=disabled' \
  --set-metadata 'gc.build_outcome=pass' \
  --set-metadata 'gc.final_report=<final report path>' \
  --set-metadata 'gc.artifact_root=<artifact root>'
gc bd close "$CLAIMED_BEAD_ID" --reason 'Publishing disabled; build-basic result approved.'
```

## Record the PR on the workflow root

build-basic's `ci-gate` and `ci-merge` steps read the PR from the workflow root
(this step's `gc.root_bead_id`). When the publish helper opened a pull request
or reused an open one, record:

```bash
gc bd update "<workflow-root-id>" \
  --set-metadata 'gc.build.pr_url=<PR URL>' \
  --set-metadata 'gc.build.pr_number=<PR number, the bare integer>' \
  --set-metadata 'gc.build.publish_forge=github' \
  --set-metadata 'gc.build.pr_head_branch=<PR head branch>' \
  --set-metadata 'gc.build.pr_head_worktree=<absolute path of the worktree on that branch>'
```

When this step ends without a pull request (publishing disabled, push only, or
a failed publish), record none of those keys and record instead:

```bash
gc bd update "<workflow-root-id>" --set-metadata 'gc.build.merge_status=skipped:no_pr'
```

Close only after the push, PR creation, or no-op publish result is recorded.
