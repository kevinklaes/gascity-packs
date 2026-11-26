"""Prove with a real `gc` that the gitea pack shadows only the rigs that import it.

REQ-002/REQ-013: a scratch city whose rig imports the pack must resolve the
publish-stage description files from `gitea/assets`, and a rig without the
import must resolve them from `gascity/assets`, unchanged. REQ-003: rig imports
are layered in sorted binding order, so a binding that sorts before `gc` loses.

Where gascity is imported matters for that last case. City-scope imports sit
below every rig import, so with `gc` at city scope *any* rig binding wins, even
`aaa-gitea`. The binding-order rule bites when both are rig imports, which is
the README's shape, so the order cases import both at rig scope.

Skipped unless `GC_TEST_BIN` names a gc binary (the shared `gc_live_city`
harness). Run: GC_TEST_BIN="$(command -v gc)" python3 -m pytest
gitea/tests/test_composition_live_gc.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

# The shared live-gc harness lives in the repo's tests/ dir, which is not a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from gc_live_city import (  # noqa: E402
    REPO_ROOT,
    gc_output,
    gc_test_bin,  # noqa: F401 -- pytest fixture, used by name
    reap_live_cities,  # noqa: F401 -- autouse fixture: stops what gc left per test
    reap_live_cities_at_session_end,  # noqa: F401 -- autouse fixture: and at session end
    run_gc,
    write_city,
)


GITEA = REPO_ROOT / "gitea"
ROLES = REPO_ROOT / "gascity" / "roles"
SCRIPT = GITEA / "assets" / "scripts" / "gitea_pr.py"

BUILD_BASIC_PUBLISH = "build-basic.publish"
HELPER_STEPS = {
    "publish.preflight": "workflows/publish/preflight.md",
    "publish.push": "workflows/publish/push.md",
    "publish.open-pr": "workflows/publish/open-pr.md",
}


# gc inlines a small description file; a large one becomes a stub that names
# the file it resolved, which is an even more direct answer to "which layer won".
RESOLVED_PROMPT = re.compile(r"^- Resolved prompt file: `([^`]+)`$", re.MULTILINE)


def asset(pack: str, rel: str) -> str:
    return REPO_ROOT.joinpath(pack, "assets", rel).read_text(encoding="utf-8")


def assert_from(description: str, pack: str, rel: str, context: str) -> None:
    expected = REPO_ROOT / pack / "assets" / rel
    stub = RESOLVED_PROMPT.search(description)
    if stub is not None:
        resolved = Path(stub.group(1)).resolve()
        assert resolved == expected.resolve(), f"{context}: resolved {resolved}, want {pack}'s asset"
    else:
        assert description == expected.read_text(encoding="utf-8"), f"{context}: text is not {pack}'s asset"


def formula(gc_bin: Path, workspace, name: str) -> dict:
    result = run_gc(gc_bin, workspace, "formula", "show", name, "--rig", "demo", "--json")
    assert result.returncode == 0, f"gc formula show {name} failed:\n{result.stdout}{result.stderr}"
    return json.loads(result.stdout)


def step_descriptions(doc: dict) -> dict[str, str]:
    return {step["id"]: step.get("description", "") for step in doc["steps"]}


def locate_script(doc: dict) -> Path | None:
    """The locator the shadow prompt runs: highest layer shipping gitea_pr.py."""
    for layer in reversed(doc["search_paths"]):
        candidate = Path(layer).parent / "assets" / "scripts" / "gitea_pr.py"
        if candidate.is_file():
            return candidate
    return None


def warnings(gc_bin: Path, workspace) -> set[str]:
    lines: set[str] = set()
    for args in (("config", "show", "--validate"), ("formula", "show", "build-basic", "--rig", "demo", "--json")):
        result = run_gc(gc_bin, workspace, *args)
        assert result.returncode == 0, f"gc {' '.join(args)} failed:\n{result.stdout}{result.stderr}"
        lines |= {line.strip() for line in result.stderr.splitlines() if "warn" in line.lower()}
    return lines


@pytest.fixture(scope="module")
def cities(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("gitea-composition")
    return {
        # E1: the importing rig, gascity at city scope as most cities have it.
        "imports": write_city(root / "imports", {"gc": ROLES}, {"gitea": GITEA}),
        # E2: no import, the control.
        "plain": write_city(root / "plain", {"gc": ROLES}),
        # README shape: both bindings at rig scope, gitea sorting after gc.
        "rig-ordered": write_city(root / "rig-ordered", {}, {"gc": ROLES, "gitea": GITEA}),
        # E3: a binding that sorts before gc silently loses.
        "rig-misordered": write_city(root / "rig-misordered", {}, {"gc": ROLES, "aaa-gitea": GITEA}),
    }


@pytest.mark.parametrize(
    ("city", "winner"),
    [("imports", "gitea"), ("plain", "gascity"), ("rig-ordered", "gitea"), ("rig-misordered", "gascity")],
)
def test_build_basic_publish_description_source(gc_test_bin, cities, city, winner) -> None:  # noqa: F811
    doc = formula(gc_test_bin, cities[city], "build-basic")
    description = step_descriptions(doc)[BUILD_BASIC_PUBLISH]
    assert_from(description, winner, "workflows/build-basic/publish.md", f"{city} build-basic publish")


@pytest.mark.parametrize(("city", "winner"), [("imports", "gitea"), ("plain", "gascity"), ("rig-misordered", "gascity")])
def test_publish_helper_descriptions_source(gc_test_bin, cities, city, winner) -> None:  # noqa: F811
    descriptions = step_descriptions(formula(gc_test_bin, cities[city], "publish"))
    for step, rel in HELPER_STEPS.items():
        assert_from(descriptions[step], winner, rel, f"{city} {step}")


def test_plain_rig_is_byte_identical_to_gascity(gc_test_bin, cities) -> None:  # noqa: F811
    """E2: the gascity asset text, exactly, with nothing from the gitea pack."""
    doc = formula(gc_test_bin, cities["plain"], "build-basic")
    gitea_layer = (GITEA / "formulas").resolve()
    assert all(Path(layer).resolve() != gitea_layer for layer in doc["search_paths"])
    assert locate_script(doc) is None
    description = step_descriptions(doc)[BUILD_BASIC_PUBLISH]
    assert description == asset("gascity", "workflows/build-basic/publish.md")


def test_shadow_prompt_locator_finds_the_pack_script(gc_test_bin, cities) -> None:  # noqa: F811
    """The prompt's script locator resolves from the rig, and the script runs."""
    doc = formula(gc_test_bin, cities["imports"], "build-basic")
    located = locate_script(doc)
    assert located is not None and located.resolve() == SCRIPT.resolve()
    result = subprocess.run(
        [sys.executable, str(located), "parse-origin", "--origin", "https://gitea.test:3000/acme/demo-app.git"],
        capture_output=True, text=True, cwd=cities["imports"].rig_dir,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["api_base"] == "https://gitea.test:3000"


def test_pack_commands_when_registered_resolve_to_the_pack(gc_test_bin, cities) -> None:  # noqa: F811
    """`gc gitea open-pr` is not registered for rig imports by gc 1.5.x.

    gc composes a rig import's commands but does not add them to the CLI, so
    the prompts locate the script through `gc formula show` instead (above).
    If a later gc registers them, they must resolve to this pack's wrapper.
    """
    result = run_gc(gc_test_bin, cities["imports"], "gitea", "open-pr", "--help")
    output = result.stdout + result.stderr
    if "--token-file" not in output:
        pytest.skip("this gc does not register rig-imported pack commands")
    assert result.returncode == 0


def test_importing_the_pack_adds_no_warnings(gc_test_bin, cities) -> None:  # noqa: F811
    """REQ-001: no composition error or new warning, including from formulas/.gitkeep."""
    added = warnings(gc_test_bin, cities["imports"]) - warnings(gc_test_bin, cities["plain"])
    assert not added, f"importing gitea added warnings: {sorted(added)}"
    output = gc_output(gc_test_bin, cities["imports"], "config", "show", "--validate")
    assert "Config valid." in output
