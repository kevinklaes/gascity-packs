
If push is {{push}}, use create-if-absent or expected-object-id update only.
Fail closed if the remote cannot enforce atomic or lease-checked semantics.

Gitea forge (this rig imports the `gitea` pack): push with plain `git push` to
`origin` over SSH, using create-if-absent or `--force-with-lease=<branch>:<expected>`
for updates. No token is used for the push, and never `gh` or GitHub.
