This is the `build-from-convoy-base` same-session implementation drain.

Implement the work in the implementation convoy recorded as `gc.input_convoy_id`.
Use drain policy `same-session`, route work to {{implementation_target}}, and
process exactly one item at a time in the shared lane so context is preserved
without losing item traceability.

Do not modify requirements, plan, plan-review, or decomposition artifacts from
this step. Keep implementation work isolated from the launcher checkout, run
focused tests as work progresses, and record item evidence so the inherited
review suffix can map changes back to the approved artifacts.

Run the tests that cover the code you changed. In a repository that holds
several packs or projects, that is the changed one's own suite plus any
shared tests the change touches, not the repository's whole CI test command.

If one item fails, skip remaining shared-lane work and record the failure
reason. Close this step only after the lane is complete, skipped because of a
prior item failure, or blocked with explicit evidence.
