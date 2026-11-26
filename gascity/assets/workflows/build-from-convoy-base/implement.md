This is the `build-from-convoy-base` separate-session implementation drain.

Implement the work in the implementation convoy recorded as `gc.input_convoy_id`.
Use drain policy `separate`, route each item to {{implementation_target}}, and
preserve per-item traceability back to `{{requirements_path}}`,
`{{plan_path}}`, and `{{decomposition_path}}`.

Do not modify requirements, plan, plan-review, or decomposition artifacts from
this step. Keep implementation work isolated from the launcher checkout, run
focused tests as work progresses, and record item evidence so the inherited
review suffix can map changes back to the approved artifacts.

Run the tests that cover the code you changed. In a repository that holds
several packs or projects, that is the changed one's own suite plus any
shared tests the change touches, not the repository's whole CI test command.

Close this step only after every drained implementation item has either passed
with evidence or has an explicit failure artifact.
