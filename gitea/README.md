# gitea

Publish `build-basic` results as Gitea pull requests, on the rigs that ask for
it.

Import this pack on a rig whose primary forge is a Gitea server. On that rig the
build publish stage still pushes the branch over SSH with plain `git`, then
opens (or reuses) the pull request through the Gitea REST API instead of `gh`.
It records the same publish metadata the gascity publish stage records. Every
rig that does not import the pack keeps the gascity `gh` path, byte for byte.

## What it does and doesn't do

Does:

- Shadow the gascity publish-stage prompts on importing rigs:
  `assets/workflows/build-basic/publish.md`, `build-base/publish.md`, and
  `publish/{preflight,push,open-pr}.md`. The formulas and their step IDs stay
  gascity's; only the prose changes.
- Ship `assets/scripts/gitea_pr.py`, a standard-library Python script that
  parses `origin`, reads the bot token from a file, and finds or creates the PR
  (`GET`/`POST /api/v1/repos/{owner}/{repo}/pulls`).
- Ship two pack commands, `gitea open-pr` and `gitea parse-origin`, that wrap
  the script.

Doesn't:

- Change the gascity pack, its agents, or rigs that don't import this pack.
- Touch the GitHub issue-fix or PR-review workflows.
- Merge, approve, label, assign reviewers, or comment on PRs. No Gitea issues,
  webhooks, status checks, or Actions.
- Create the bot account or its token. That's an operator task, described
  below.
- Open draft or `WIP:` PRs.

## Enable it on one rig

Import gascity and this pack at rig scope. The binding names matter (see the
next section):

```toml
[[rigs]]
name = "demo-app"
default_branch = "main"

[rigs.imports.gc]
source = "https://github.com/gastownhall/gascity-packs/tree/main/gascity/roles"
version = "sha:<pin>"

[rigs.imports.gitea]          # binding must sort after "gc"
source = "https://github.com/kevinklaes/gascity-packs/tree/main/gitea"

# Settings for the rig's publisher (see "Configuration").
[[rigs.overrides]]
agent = "publisher"
env = { GC_GITEA_BASE_URL = "http://192.0.2.10:3000", GC_GITEA_TOKEN_FILE = "~/.config/gitea/gc-bot.token" }
```

If your city imports gascity at **city** scope (`[imports.gc]` in `pack.toml`)
instead, keep the `[rigs.imports.gitea]` block and set the env with a city
patch instead, because `[[rigs.overrides]]` only reaches agents that a rig
import brings in:

```toml
[[patches.agent]]
rig = "demo-app"
name = "gc.publisher"
env = { GC_GITEA_BASE_URL = "http://192.0.2.10:3000" }
```

Check the result from the rig directory. The publish step should come from this
pack:

```sh
gc formula show build-basic --rig demo-app --json
```

Its `search_paths` ends with this pack's `formulas/` dir. The
`build-basic.publish` step description either holds this pack's text, or (gc
moves large prompts out of line) names
`.../gitea/assets/workflows/build-basic/publish.md` as its resolved prompt
file.

### The binding-order rule

gc resolves a step's `description_file` through the rig's formula layers, from
lowest to highest priority: city pack imports, the city's `formulas/`, the
rig's imports **in sorted binding-name order**, and then the rig's
`formulas_dir`. The highest layer that ships the asset wins.

So when gascity and this pack are both rig imports, this pack's binding name
must sort after gascity's. `gitea` sorts after `gc` and wins. A binding such as
`aaa-gitea` sorts before `gc` and **silently loses**: the rig keeps the gascity
prompts and publishes with `gh`. `gitea/tests/test_composition_live_gc.py`
pins both outcomes against a real `gc`.

When gascity is imported at city scope, every rig import sits above it, so the
gitea binding wins whatever its name. Use `gitea` anyway, so the config stays
correct if gascity later moves to rig scope.

The pack ships an empty `formulas/` directory (`formulas/.gitkeep`) on purpose.
gc adds a formula layer only for a pack whose `formulas/` dir exists. The pack
defines no formulas.

## The publish flow

On an importing rig, the `build-basic` publish step (run by gascity's
`gc.publisher` agent) does the following:

1. **Preflight.** `git remote get-url origin` works. The script is found, and
   `parse-origin` resolves the API base. When `open_pr` is on, the token file
   exists and is non-empty (`test -s`; it is never printed).
2. **Push** over SSH with plain `git push`, create-if-absent or
   `--force-with-lease`. No token is used.
3. If `open_pr` is off, it **stops** after the push. The token file is never
   read.
4. It writes the sanitized PR body to a `mktemp` file outside the worktree.
5. It runs `gitea_pr.py open-pr --head <branch> --base <rig default_branch>
   --title ... --body-file ...`, then removes the body file. The script lists
   open PRs (paginated) and reuses one that matches head, base and repo
   (`created=false`). Otherwise it creates the PR (`created=true`). A 409 or
   422 on create is resolved by listing again, so a re-run never makes a
   duplicate.
6. It records the result and closes the step, naming the PR URL and state.

When `push` and `open_pr` are both off, the step records the same `noop`
result gascity does, and never reads the token.

### Base branch

The prompt passes `--base` from the rig's `default_branch`, which it reads with
`gc rig list --json` for `$GC_RIG`. Only when that is empty does the script
fall back to `refs/remotes/origin/HEAD`. A checkout made with `git fetch`
rather than `git clone` often lacks that ref, and then the script fails with
`base_unresolved`.

### How the prompt finds the script

gc 1.5 composes a rig import's commands but does not register them as `gc`
subcommands, so `gc gitea open-pr` is not available to a rig-imported pack.
The prompt therefore finds the script through the layer that supplied it: the
highest `search_paths` entry of `gc formula show build-basic --rig "$GC_RIG"
--json` whose sibling `assets/scripts/gitea_pr.py` exists. The pack commands
still work wherever gc registers them, for example with a city-scope import,
or by hand.

### Metadata

The publish step writes `gc.outcome`, `gc.publish_outcome`, `gc.publish_mode`,
`gc.build_outcome`, `gc.final_report` and `gc.artifact_root`. The workflow root
gets `gc.build.publish_status`, `gc.build.publish_action`,
`gc.build.publish_recorded_at`, `gc.build.publish_artifact_path`,
`gc.build.publish_reason` and `gc.build.publish_remote_status`, with the same
values and meanings as on the gh path.

When a PR is opened or reused, the root also gets the PR identity, under the
same keys GitHub run roots carry, so tools that read a run's PR (such as a
run-finished notifier) need no forge-specific code:

| Key | Value |
| --- | --- |
| `gc.build.pr_url` | The PR's web URL (`html_url`) |
| `gc.build.pr_number` | The PR number |

This pack also writes three keys of its own, with the same PR identity plus
the forge name:

| Key | Value |
| --- | --- |
| `gc.build.publish_pr_url` | The PR's web URL (`html_url`) |
| `gc.build.publish_pr_number` | The PR number |
| `gc.build.publish_forge` | `gitea` |

Push-only, no-op and failed publishes write none of these.

## Configuration

Each setting resolves in this order: a flag, then an environment variable, then
a default.

| Setting | Flag | Environment | Default |
| --- | --- | --- | --- |
| Gitea web base URL | `--base-url` | `GC_GITEA_BASE_URL` | `scheme://host[:port]` of an `http(s)` origin |
| Token file | `--token-file` | `GC_GITEA_TOKEN_FILE` | `~/.config/gitea/gc-bot.token` |

Set per-rig values on the publisher's environment, as in the examples above.
Pack commands and the script inherit the agent's environment.

**SSH origins need the base URL.** From
`ssh://git@192.0.2.10:2222/acme/demo-app.git`, the script can tell the owner
and repo, but not the web port: SSH (2222) and HTTP (3000) differ. It won't
guess. Without `GC_GITEA_BASE_URL` it fails with `origin_unsupported`. The same
goes for scp-style `git@host:owner/repo.git` origins. For an `http(s)` origin,
the base comes from the origin unless you override it.

The script talks to the forge directly. It ignores `*_proxy` environment
variables, so the token is never handed to a proxy. It refuses HTTP redirects,
so the `Authorization` header is never forwarded to another host or downgraded
from `https` to `http`.

## The token file

```sh
mkdir -p ~/.config/gitea
umask 077
printf '%s\n' '<token>' > ~/.config/gitea/gc-bot.token
chmod 600 ~/.config/gitea/gc-bot.token
```

- The token is read only from this file. There is no `--token` flag, and the
  script ignores `GITEA_TOKEN`-style environment variables. It is stripped of
  surrounding whitespace and sent only in the `Authorization: token ...` header.
- Every message the script prints passes through a redaction filter, and no
  traceback is printed (unless `GC_GITEA_DEBUG=1`, which is redacted too).
- If the file is readable by group or other, the script prints a warning and
  carries on. Run `chmod 600` on it.
- A missing, empty or unreadable file fails closed (see below). The branch
  stays pushed.

### The `gc-bot` account

Create a dedicated, restricted Gitea user for the token, for example `gc-bot`:

- Not a site admin, and not an organization owner.
- A collaborator on only the repos it publishes to, with **write** access: what
  is needed to push a branch and open a PR. Give no admin access to the repo.
- Not on any branch-protection allowlist. It must not be able to push to, or
  merge into, the protected default branch, nor bypass required reviews. Merges
  stay with people.
- Scope its access token to repositories only (`write:repository`), with no
  admin, organization or user scopes.

Pushes go over SSH with the publisher's own key. The token is used only for the
pull-request API.

## Errors and the manual fallback

The script exits 2 and prints one JSON line on stderr,
`{"error": "<code>", "message": "<text>"}`, that never contains the token. The
publish step records it as `gc.outcome=fail`, with
`gc.failure_class=<code>` and `gc.build.publish_status=failed`. It never falls
back to `gh` or GitHub.

| Code | Meaning |
| --- | --- |
| `token_missing` | The token file doesn't exist (the message names the path). |
| `token_empty` | The token file is empty after stripping whitespace. |
| `token_unreadable` | The token file can't be read (permissions, a directory, not UTF-8). |
| `origin_missing` | `git remote get-url origin` failed. |
| `origin_unsupported` | The origin isn't `http(s)`, `ssh` or scp-style `<owner>/<repo>`, or it is SSH and no base URL is set. |
| `base_url_invalid` | The base URL override isn't a plain `http(s)` URL. |
| `base_unresolved` | No `--base`, and `origin/HEAD` is unset. |
| `head_unresolved` / `head_is_base` | Detached HEAD with no `--head`, or the head is the base. |
| `title_empty` / `body_unreadable` | Bad title or body input. |
| `http_<status>` | Gitea answered with an error (`http_401` is a bad token, `http_404` a wrong repo or no access, `http_302` a refused redirect). |
| `api_error` | The server couldn't be reached, or the reply wasn't JSON. |

To finish by hand after a failure, the pushed branch is still on the remote.
Open the repo in the Gitea web UI, choose **New Pull Request**, select the rig's
default branch as the base and the pushed branch as the head, and create it.
Then fix the cause (usually the token file or `GC_GITEA_BASE_URL`) before the
next run. A re-run finds the PR you opened and reuses it.

Check the configuration without touching the token:

```sh
python3 <pack>/assets/scripts/gitea_pr.py parse-origin
```

## Tests

```sh
python3 -m pytest gitea/tests -q
GC_TEST_BIN="$(command -v gc)" python3 -m pytest gitea/tests/test_composition_live_gc.py -q
```

The unit tests run the script against a local fake Gitea (`http.server` on
127.0.0.1). They cover create, reuse, the 409/422 race, pagination, HTTP
errors, refused redirects, and a sentinel token that must never appear outside
the `Authorization` header. The live test stands up scratch cities with a real
`gc` and checks which pack supplies the publish prompts.
