Show the Gitea API base, owner and repo that `gc gitea open-pr` would use.

Reads no token and makes no request, so it is safe as a preflight check.

Example:
  gc gitea parse-origin
  gc gitea parse-origin --base-url http://gitea.example:3000

Flags:
  --base-url <url>   Gitea web base (default: $GC_GITEA_BASE_URL, else
                     derived from an http(s) origin; required for SSH origins)
  --origin <url>     origin URL (default: git remote get-url origin)
  --cwd <dir>        git checkout to inspect (default: current directory)

Output: one JSON object on stdout with api_base, owner, repo, host, port,
origin_kind and forge="gitea". On failure: exit 2 and
{"error": "<code>", "message": "..."} on stderr.
