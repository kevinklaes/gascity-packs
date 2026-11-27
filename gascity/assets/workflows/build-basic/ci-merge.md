Merge this build's pull request when its CI verdict allows.

This step is this bead's explicit authorization to merge the pull request,
scoped by `merge={{merge}}`: merge only through the command below, which merges
only when the recorded CI verdict is green or no CI and `merge` is true. No
other step of this run merges, and you never merge any other way.

The CI gate before this step already waited for CI and recorded the verdict on
the workflow root (`gc.build.ci_status`, `gc.build.ci_rounds`,
`gc.build.ci_head_sha`). Do not wait for CI or read the forge yourself.

## 1. Locate the merge script

Find `ci_gate.py` through the formula layers that supplied this prompt:

```bash
CI_GATE=$(gc formula show build-basic --rig "$GC_RIG" --json | python3 -c 'import json,os,sys
for layer in reversed(json.load(sys.stdin)["search_paths"]):
    script = os.path.join(os.path.dirname(layer), "assets", "scripts", "ci_gate.py")
    if os.path.isfile(script):
        print(script)
        break')
test -n "$CI_GATE"
```

## 2. Run it once

The workflow root id is this step's `gc.root_bead_id`.

```bash
python3 "$CI_GATE" merge --root "<workflow-root-id>"
```

It prints one JSON line with `merge_status`, `outcome`, `failure_class`,
`failure_reason`, `pr_url`, `ci_status` and `ci_rounds`, and has already
written `gc.build.merge_status` on the workflow root. It squash-merges pinned
to the head sha CI judged and deletes the branch. It uses this session's own
forge credentials; never print, export or pass a token yourself.

Run it once. Do not retry a failed merge, and do not merge by any other means.

## 3. Record and close

Copy the result onto this step:

```bash
gc bd update "$CLAIMED_BEAD_ID" \
  --set-metadata 'gc.outcome=<outcome>' \
  --set-metadata 'gc.build.merge_status=<merge_status>'
```

When `outcome` is `fail`, also set
`--set-metadata 'gc.failure_class=<failure_class>'` and
`--set-metadata 'gc.failure_reason=<failure_reason>'`. The PR stays open.

If the script cannot be located or exits nonzero without a JSON line, record
`gc.outcome=fail`, `gc.failure_class=merge_failed` and a token-free
`gc.failure_reason`, and leave the PR open.

Close with the PR URL and the merge status:

```bash
gc bd close "$CLAIMED_BEAD_ID" --reason 'CI merge: <pr_url> <merge_status> (ci <ci_status>, <ci_rounds> fix rounds).'
```
