Find the open Gitea pull request for a pushed branch, or create it.

The token is read only from a file, never from a flag or an environment
variable, and is sent only in the Authorization header. Redirects are refused.

Example:
  gc gitea open-pr \
    --head feature/x \
    --base main \
    --title "feat: add widget" \
    --body-file "$BODY_FILE"

Flags:
  --head <branch>       head branch (default: the current branch)
  --base <branch>       base branch; pass the rig default_branch
                        (fallback: refs/remotes/origin/HEAD)
  --title <text>        PR title; whitespace collapsed, capped at 255 chars
  --body-file <path>    file holding the PR body (keep it outside the worktree)
  --base-url <url>      Gitea web base (default: $GC_GITEA_BASE_URL, else
                        derived from an http(s) origin; required for SSH origins)
  --token-file <path>   token file (default: $GC_GITEA_TOKEN_FILE, else
                        ~/.config/gitea/gc-bot.token)
  --origin <url>        origin URL (default: git remote get-url origin)
  --cwd <dir>           git checkout to inspect (default: current directory)

Output: one JSON object on stdout with url, number, state, created, head,
base, api_base, owner, repo and forge="gitea". On failure: exit 2 and
{"error": "<code>", "message": "..."} on stderr. Codes: token_missing,
token_empty, token_unreadable, origin_missing, origin_unsupported,
base_url_invalid, base_unresolved, head_unresolved, head_is_base,
title_empty, body_unreadable, api_error, http_<status>, internal_error.
