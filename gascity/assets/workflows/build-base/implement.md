This is the `build-base` implement stage. Treat it as a virtual contract that concrete formulas may override.

Implement the decomposed work using the selected drain policy and implementation target {{implementation_target}}. Keep implementation work isolated from the launcher checkout, run focused tests as work progresses, and record the implementation summary path.

Run the tests that cover the code you changed. In a repository that holds
several packs or projects, that is the changed one's own suite plus any
shared tests the change touches, not the repository's whole CI test command.

Close this step only after all assigned implementation work has either passed or has an explicit failure artifact.
