Use the built-in Gas City publish flow, retargeted at Gitea by the `gitea` pack.

This rig imports the `gitea` pack, so its forge is a Gitea server, not GitHub.
The push is a plain `git push` to `origin` over SSH. The pull request is opened
through the Gitea REST API by the pack's `gitea_pr.py` script. Never fall back
to the GitHub CLI (`gh`), GitHub, or a hand-written HTTP request, even when a
step below fails.

This prompt is self-sufficient: this step does not expand the `publish` helper
formula. Run every part below yourself.

If publishing is enabled, publish the finalized build-basic result. If
publishing is disabled, record a no-op publish outcome with the final artifact
paths.

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

For that disabled case, also record the workflow root metadata from step 7
with `gc.build.publish_status=noop`, `gc.build.publish_action=noop` and a reason
such as `push=false_open_pr=false`. Do not read the token file and do not call
the Gitea API.

## Inputs

- Push authorization: `{{push}}`. Open-PR authorization: `{{open_pr}}`. If a
  value is not rendered here, read `gc.var.push` and `gc.var.open_pr` from the
  workflow root bead (`gc.root_bead_id` on this step).
- Artifact root: `{{artifact_root}}`. The final report is the finalize stage's
  report under that root.
- Head branch: the branch holding the finalized result's worktree. If that
  worktree is on a detached HEAD, create the local branch
  `build/<workflow-root-id>` at its HEAD and use that.
- Base branch: the rig's `default_branch`, read from gc's rig config:

  ```bash
  BASE_BRANCH=$(gc rig list --json | python3 -c 'import json,os,sys
  rig = os.environ["GC_RIG"]
  print(next((r.get("default_branch", "") for r in json.load(sys.stdin)["rigs"] if r["name"] == rig), ""))')
  ```

  If it comes back empty, omit `--base` below; the script then falls back to
  `refs/remotes/origin/HEAD` and fails with `base_unresolved` if that is unset.

## 1. Preflight

Run these from the head branch's worktree before pushing.

- `git remote get-url origin` succeeds and the head branch is not the base
  branch.
- Locate the pack's script through the formula layer that supplied this
  prompt. Rig-imported pack commands are not registered as `gc` subcommands,
  so find the script from `gc formula show` instead:

  ```bash
  GITEA_PR=$(gc formula show build-basic --rig "$GC_RIG" --json | python3 -c 'import json,os,sys
  for layer in reversed(json.load(sys.stdin)["search_paths"]):
      script = os.path.join(os.path.dirname(layer), "assets", "scripts", "gitea_pr.py")
      if os.path.isfile(script):
          print(script)
          break')
  test -n "$GITEA_PR"
  ```

- `python3 "$GITEA_PR" parse-origin` exits 0. It reads no token and prints the
  API base, owner and repo. An SSH origin needs `GC_GITEA_BASE_URL` in this
  agent's environment (or `--base-url`); without it the script fails with
  `origin_unsupported`.
- Only when open-PR is authorized: the token file exists and is non-empty.
  Check existence only, never print or `cat` it:

  ```bash
  TOKEN_FILE="${GC_GITEA_TOKEN_FILE:-$HOME/.config/gitea/gc-bot.token}"
  test -s "$TOKEN_FILE"
  ```

A preflight failure is handled by step 6. A missing token is
`gc.failure_class=token_missing`.

## 2. Push (if push is authorized)

Push over SSH with plain git. No token is used for the push. Use
create-if-absent or expected-object-id update only:

- New remote branch: `git push origin "refs/heads/$HEAD_BRANCH:refs/heads/$HEAD_BRANCH"`.
- Existing remote branch: `git push --force-with-lease="$HEAD_BRANCH:<expected remote sha>" origin "$HEAD_BRANCH"`.

Fail closed if the remote cannot enforce atomic or lease-checked semantics.
If push is not authorized, skip to step 7 with `noop` or the disabled reason.

## 3. Stop here when open-PR is not authorized

If `open_pr` is false, stop after the push. Record
`gc.build.publish_action=push`, `gc.build.publish_status=published`,
`gc.publish_outcome=pushed` and `gc.publish_mode=push`. Do not run the script's
`open-pr` subcommand, so the token is never read. Write no PR keys on the
workflow root.

## 4. Prepare the title and body

Create a PR only after the push succeeds and the sanitized title and body pass
policy. Derive both from the final report: one plain-text title line, and a
body with no secrets, no absolute home or city paths, and no bead dumps. Write
the body to a temp file outside every worktree and remove it afterwards:

```bash
BODY_FILE=$(mktemp "${TMPDIR:-/tmp}/gitea-pr-body.XXXXXX")
# write the sanitized body into "$BODY_FILE"
```

## 5. Open or reuse the pull request

```bash
python3 "$GITEA_PR" open-pr \
  --head "$HEAD_BRANCH" \
  --base "$BASE_BRANCH" \
  --title '<sanitized title>' \
  --body-file "$BODY_FILE"
STATUS=$?
rm -f "$BODY_FILE"
```

On success it prints one JSON object: `url`, `number`, `state`, `created`,
`head`, `base`, `api_base`, `owner`, `repo`, `forge`. `created=false` means an
open PR for this head and base already existed and was reused; that is a
success. The token is read from the token file only. Never pass it on the
command line, never export it, and never echo it.

## 6. Failure handling

If any step fails (preflight, push, or a nonzero exit from the script), do not
retry through another tool. The script prints
`{"error": "<code>", "message": "<text>"}` on stderr. Leave any pushed branch
in place so an operator can open the PR by hand, and record:

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=fail' \
  --set-metadata 'gc.failure_class=<error code, e.g. token_missing>' \
  --set-metadata 'gc.failure_reason=<token-free message>' \
  --set-metadata 'gc.publish_outcome=failed' \
  --set-metadata 'gc.publish_mode=<push|push_pr>' \
  --set-metadata 'gc.build_outcome=pass' \
  --set-metadata 'gc.final_report=<final report path>' \
  --set-metadata 'gc.artifact_root=<artifact root>'
```

and on the workflow root `gc.build.publish_status=failed`,
`gc.build.publish_action=failed`, and the error code plus token-free message
as `gc.build.publish_reason`. Never fall back to `gh` or GitHub.

## 7. Record the result

Write a publish result artifact under the artifact root (for example
`<artifact root>/publish-result.md`) holding the PR URL, number, state,
`created`, head, base, and the publish action. Then record on this step:

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=pass' \
  --set-metadata 'gc.publish_outcome=<published|pushed|noop>' \
  --set-metadata 'gc.publish_mode=<push_pr|push|disabled>' \
  --set-metadata 'gc.build_outcome=pass' \
  --set-metadata 'gc.final_report=<final report path>' \
  --set-metadata 'gc.artifact_root=<artifact root>'
```

and on the workflow root bead:

- `gc.build.publish_status=published|noop|failed`
- `gc.build.publish_action=push|pr|push_pr|noop|failed`
- `gc.build.publish_recorded_at=<UTC timestamp>`
- `gc.build.publish_artifact_path=<publish result artifact path>`
- `gc.build.publish_reason=<short machine-readable reason>`
- `gc.build.publish_remote_status=<whether origin was present and reachable>`

When the script's `open-pr` succeeded, whether it opened a PR
(`created=true`) or reused an open one (`created=false`), the root also gets
these, with `gc.build.publish_status=published` and
`gc.build.publish_action=push_pr`:

- `gc.build.pr_url=<url from the script>` (the key GitHub run roots carry and
  run-finished notifiers read)
- `gc.build.pr_number=<number from the script>` (the same key as on GitHub run
  roots; the bare integer, e.g. `4`)
- `gc.build.publish_pr_url=<url from the script>` (gitea pack addition)
- `gc.build.publish_pr_number=<number from the script>` (gitea pack addition)
- `gc.build.publish_forge=gitea` (gitea pack addition)

For example, with the root id from this step's `gc.root_bead_id`:

```bash
gc bd update "<workflow-root-id>" \
  --set-metadata 'gc.build.pr_url=<url>' \
  --set-metadata 'gc.build.pr_number=<number>' \
  --set-metadata 'gc.build.publish_pr_url=<url>' \
  --set-metadata 'gc.build.publish_pr_number=<number>' \
  --set-metadata 'gc.build.publish_forge=gitea' \
  --set-metadata 'gc.build.publish_status=published' \
  --set-metadata 'gc.build.publish_action=push_pr'
```

Push-only, no-op and failed publishes write none of the PR keys.

The final report's publish fields use the same names as the gascity flow. When
a PR was opened or reused, close with its URL and state:

```bash
gc bd close "$CLAIMED_BEAD_ID" --reason 'Published to Gitea: <url> (<state>, created=<true|false>).'
```

Close only after the push, PR creation, or no-op publish result is recorded.
