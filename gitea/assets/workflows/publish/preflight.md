
Validate auth, token scope, remote, branch names, PR base, collision policy,
protected/default targets, sanitized metadata, final report {{final_report}},
push authorization {{push}}, and open_pr authorization {{open_pr}}.

Gitea forge (this rig imports the `gitea` pack). Also check, without reading
or printing the token:

- The pack script resolves through the formula layer that supplied this
  prompt: the highest `search_paths` entry of
  `gc formula show publish --rig "$GC_RIG" --json` whose sibling
  `assets/scripts/gitea_pr.py` exists. Call it `$GITEA_PR`.
- `python3 "$GITEA_PR" parse-origin` exits 0. An SSH origin needs
  `GC_GITEA_BASE_URL` (or `--base-url`); otherwise it fails with
  `origin_unsupported`.
- The PR base is the rig `default_branch` from `gc rig list --json`, else
  `refs/remotes/origin/HEAD`.
- Only if open_pr is {{open_pr}} and true: the token file exists and is
  non-empty, checked with
  `test -s "${GC_GITEA_TOKEN_FILE:-$HOME/.config/gitea/gc-bot.token}"`. Never
  `cat` it. A missing file fails preflight with `token_missing`. With open_pr
  false, skip this check so the token file is never touched.
