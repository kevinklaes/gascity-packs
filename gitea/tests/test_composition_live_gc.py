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
CI_GATE_SCRIPT = REPO_ROOT / "gascity" / "assets" / "scripts" / "ci_gate.py"
CI_GATE_CHECK = REPO_ROOT / "gascity" / "assets" / "scripts" / "checks" / "ci-gate.sh"

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


def locate_script(doc: dict, name: str = "gitea_pr.py") -> Path | None:
    """The locator the prompts and ci_gate.py run: highest layer shipping the script."""
    for layer in reversed(doc["search_paths"]):
        candidate = Path(layer).parent / "assets" / "scripts" / name
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


# --------------------------------------------------------------------------
# build-basic's CI gate and merge (ci-gate-merge REQ-014, REQ-016, AC-1)


def formula_with(gc_bin: Path, workspace, *extra: str) -> dict:
    result = run_gc(gc_bin, workspace, "formula", "show", "build-basic", "--rig", "demo", *extra, "--json")
    assert result.returncode == 0, f"gc formula show build-basic failed:\n{result.stdout}{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.parametrize("city", ["imports", "plain"])
def test_ci_steps_compile_in_only_with_open_pr(gc_test_bin, cities, city) -> None:  # noqa: F811
    doc = formula_with(gc_test_bin, cities[city], "--var", "open_pr=true")
    steps = {step["id"]: step for step in doc["steps"]}
    ids = list(steps)
    for step_id in ("build-basic.ci-gate", "build-basic.ci-gate.iteration.1", "build-basic.ci-merge"):
        assert step_id in steps, f"{city}: {step_id} missing from {ids}"
    assert ids.index("build-basic.publish") < ids.index("build-basic.ci-gate") < ids.index("build-basic.ci-merge")
    assert ids[-1] == "build-basic.workflow-finalize"

    gate = steps["build-basic.ci-gate"]["metadata"]
    assert gate["gc.kind"] == "ralph" and gate["gc.check_mode"] == "exec"
    assert gate["gc.max_attempts"] == "11"
    # The check always comes from gascity: sibling packs may not shadow it.
    assert Path(gate["gc.check_path"]).resolve() == CI_GATE_CHECK.resolve()
    assert steps["build-basic.ci-merge"]["metadata"]["gc.run_target"] == "gc.publisher"
    assert "explicit authorization to merge" in steps["build-basic.ci-merge"]["description"]

    plain = formula_with(gc_test_bin, cities[city])
    assert not [step["id"] for step in plain["steps"] if ".ci-" in step["id"]], "open_pr=false must compile the CI steps out"


def sandbox_env(workspace) -> dict:
    """gc's check sandbox: HOME=<city root>, a narrow PATH, GC_CITY_PATH and nothing else (RA-3)."""
    return {
        "PATH": ":".join(dict.fromkeys([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"])),
        "HOME": str(workspace.city_dir),
        "GC_HOME": workspace.env["GC_HOME"],
        "GC_CITY_PATH": str(workspace.city_dir),
    }


@pytest.mark.parametrize(("city", "gitea_found"), [("imports", True), ("plain", False)])
def test_ci_scripts_are_locatable_under_the_check_sandbox_env(gc_test_bin, cities, city, gitea_found) -> None:  # noqa: F811
    """ci_gate.py finds gitea_pr.py (and the merge prompt finds ci_gate.py) with gc's scrubbed check env."""
    workspace = cities[city]
    env = sandbox_env(workspace)
    env["PATH"] = f"{Path(gc_test_bin).parent}:{env['PATH']}"
    result = subprocess.run(
        [str(gc_test_bin), "formula", "show", "build-basic", "--rig", "demo", "--json"],
        cwd=workspace.city_dir, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    doc = json.loads(result.stdout)
    assert locate_script(doc, "ci_gate.py").resolve() == CI_GATE_SCRIPT.resolve()
    located = locate_script(doc, "gitea_pr.py")
    if gitea_found:
        assert located is not None and located.resolve() == SCRIPT.resolve()
    else:
        assert located is None
