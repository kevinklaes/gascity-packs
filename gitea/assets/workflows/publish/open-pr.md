
If open_pr is {{open_pr}}, create a PR only after push succeeds and sanitized
title/body from final report {{final_report}} pass policy.

Gitea forge (this rig imports the `gitea` pack): open or reuse the PR with the
pack script, located as in preflight (`$GITEA_PR`), never with `gh`, GitHub, or
a hand-written HTTP request. Write the sanitized body to a `mktemp` file
outside the worktree, and remove it after the call:

```bash
python3 "$GITEA_PR" open-pr --head "$HEAD_BRANCH" --base "$BASE_BRANCH" \
  --title '<sanitized title>' --body-file "$BODY_FILE"
```

`--base` is the rig `default_branch` from `gc rig list --json`. The script
prints JSON with `url`, `number`, `state` and `created`; `created=false` means
an existing open PR was reused. Record `gc.build.pr_url` and
`gc.build.pr_number` (the script's `url` and `number`; the keys GitHub run
roots carry and run-finished notifiers read), plus `gc.build.publish_pr_url`,
`gc.build.publish_pr_number` and `gc.build.publish_forge=gitea`, on the
workflow root, and name the PR URL and state in the close reason.

On a nonzero exit the script prints `{"error": "<code>", "message": "..."}` on
stderr. Record `gc.outcome=fail` and `gc.failure_class=<code>` (for example
`token_missing`), set `gc.build.publish_status=failed`, and leave the pushed
branch in place for a manual PR. Never fall back to `gh` or GitHub.
