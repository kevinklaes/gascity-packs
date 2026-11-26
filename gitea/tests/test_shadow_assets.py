"""The gitea pack's shadow prompts stay aligned with the gascity assets they shadow.

Shadowing works by relative path (REQ-002): gc picks the highest formula layer
that ships `assets/<rel>`. If gascity renames one of these files, the shadow
silently stops applying, so the paths are pinned against both packs here. The
metadata contract is pinned too (REQ-010, AC6): every `gc.*` key a gascity
asset asks for must appear in its shadow. The build-basic shadow is checked
against the union of build-basic and build-base, because build-basic's publish
step loads only that one file (plan amendment A1). The `gc.build.pr_url` and
`gc.build.pr_number` keys are pinned too, because run-finished notifiers read
them to find a run's PR.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - CI runs 3.12
    tomllib = None


PACK_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACK_DIR.parent
GASCITY_ASSETS = REPO_ROOT / "gascity" / "assets"
GITEA_ASSETS = PACK_DIR / "assets"

SHADOWED = (
    "workflows/build-basic/publish.md",
    "workflows/build-base/publish.md",
    "workflows/publish/preflight.md",
    "workflows/publish/push.md",
    "workflows/publish/open-pr.md",
)

# `gc.build.publish_status`, `gc.outcome`, ... but not `gc.publisher` agent
# names or `gc.var.*`, which are read, not written.
METADATA_KEY = re.compile(r"\bgc\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
NOT_WRITTEN = re.compile(r"^gc\.(var|publisher|run-operator)\b")

GITEA_ADDITIONS = ("gc.build.publish_pr_url", "gc.build.publish_pr_number", "gc.build.publish_forge")

# The PR keys GitHub run roots carry and run-finished notifiers read.
CANONICAL_PR_KEYS = ("gc.build.pr_url", "gc.build.pr_number")

PR_RECORDING_SHADOWS = (
    "workflows/build-basic/publish.md",
    "workflows/build-base/publish.md",
    "workflows/publish/open-pr.md",
)

NOOP_BLOCK = """\
gc bd update "$CLAIMED_BEAD_ID" \\
  --set-metadata 'gc.outcome=pass' \\
  --set-metadata 'gc.publish_outcome=noop' \\
  --set-metadata 'gc.publish_mode=disabled' \\
  --set-metadata 'gc.build_outcome=pass' \\
  --set-metadata 'gc.final_report=<final report path>' \\
  --set-metadata 'gc.artifact_root=<artifact root>'
gc bd close "$CLAIMED_BEAD_ID" --reason 'Publishing disabled; build-basic result approved.'
"""

FORBIDDEN = (
    (re.compile(r"\bgh\s+pr\b"), "gh pr"),
    (re.compile(r"\bgh\s+api\b"), "gh api"),
    (re.compile(r"\bcurl\b"), "curl"),
    (re.compile(r"^\s*(?:\$\s*)?gh\s", re.MULTILINE), "a bare gh command line"),
    (re.compile(r"\bwget\b"), "wget"),
)


def read(root: Path, rel: str) -> str:
    return root.joinpath(rel).read_text(encoding="utf-8")


def keys(text: str) -> set[str]:
    return {key for key in METADATA_KEY.findall(text) if not NOT_WRITTEN.match(key)}


def section(text: str, heading: str) -> str:
    """Return one `## ` section, from its heading up to the next `## ` heading."""
    start = text.index(f"\n## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


@pytest.mark.parametrize("rel", SHADOWED)
def test_shadowed_path_exists_in_both_packs(rel: str) -> None:
    assert GASCITY_ASSETS.joinpath(rel).is_file(), (
        f"gascity no longer ships assets/{rel}; the gitea shadow of it no longer "
        "applies. Move the shadow to the new path."
    )
    assert GITEA_ASSETS.joinpath(rel).is_file(), f"gitea pack is missing its shadow of assets/{rel}"


def test_the_shadow_set_covers_every_gascity_publish_asset() -> None:
    publish_helper = {
        f"workflows/publish/{path.name}" for path in (GASCITY_ASSETS / "workflows" / "publish").glob("*.md")
    }
    assert publish_helper <= set(SHADOWED), f"unshadowed publish helper assets: {publish_helper - set(SHADOWED)}"


@pytest.mark.parametrize("rel", SHADOWED)
def test_shadow_names_every_key_its_gascity_asset_requires(rel: str) -> None:
    required = keys(read(GASCITY_ASSETS, rel))
    if rel == "workflows/build-basic/publish.md":
        required |= keys(read(GASCITY_ASSETS, "workflows/build-base/publish.md"))
        assert required, "the key regex found nothing in gascity's publish assets"
    missing = required - keys(read(GITEA_ASSETS, rel))
    assert not missing, f"gitea assets/{rel} drops gascity metadata keys: {sorted(missing)}"


@pytest.mark.parametrize("rel", PR_RECORDING_SHADOWS)
def test_pr_identity_keys_are_recorded(rel: str) -> None:
    text = read(GITEA_ASSETS, rel)
    for key in GITEA_ADDITIONS:
        assert key in text, f"{rel} does not record {key}"
    assert "gc.build.publish_forge=gitea" in text


@pytest.mark.parametrize("rel", PR_RECORDING_SHADOWS)
def test_canonical_pr_keys_are_recorded(rel: str) -> None:
    text = read(GITEA_ASSETS, rel)
    for key in CANONICAL_PR_KEYS:
        assert key in text, f"{rel} does not record {key}; run-finished notifiers read it"


def test_no_pr_paths_write_no_pr_keys() -> None:
    text = read(GITEA_ASSETS, "workflows/build-basic/publish.md")
    no_pr = {
        "preamble": text[: text.index("\n## Inputs")],
        "push only": section(text, "3. Stop here"),
        "failure": section(text, "6. Failure handling"),
    }
    for name, body in no_pr.items():
        for key in CANONICAL_PR_KEYS + GITEA_ADDITIONS:
            assert key not in body, f"the {name} path of build-basic/publish.md writes {key}"


def test_build_basic_pr_block_sets_published_push_pr() -> None:
    step7 = section(read(GITEA_ASSETS, "workflows/build-basic/publish.md"), "7. Record the result")
    for needle in (
        "gc.build.pr_url=",
        "gc.build.pr_number=",
        "created=false",
        "gc.build.publish_status=published",
        "gc.build.publish_action=push_pr",
        "none of the PR keys",
    ):
        assert needle in step7, f"step 7 lacks {needle!r}"


def test_build_basic_shadow_keeps_the_noop_contract() -> None:
    gascity = read(GASCITY_ASSETS, "workflows/build-basic/publish.md")
    gitea = read(GITEA_ASSETS, "workflows/build-basic/publish.md")
    assert NOOP_BLOCK in gascity, "gascity's noop block changed; update NOOP_BLOCK and the shadow together"
    assert NOOP_BLOCK in gitea
    assert "Never set\n`gc.outcome=noop`" in gitea or "Never set `gc.outcome=noop`" in gitea
    assert "Close only after the push, PR creation, or no-op publish result is recorded." in gitea


def test_build_basic_shadow_is_self_sufficient() -> None:
    """A1: build-basic's publish step sees only this file."""
    text = read(GITEA_ASSETS, "workflows/build-basic/publish.md")
    for needle in (
        "test -s",  # token existence only
        "parse-origin",
        "--force-with-lease",
        "open-pr",
        "--base",
        "gc rig list --json",  # A2: rig default_branch
        "mktemp",  # A4: body outside the worktree
        'rm -f "$BODY_FILE"',
        "gc.failure_class=",
        "token_missing",
        "search_paths",  # the script locator
    ):
        assert needle in text, f"build-basic shadow lacks {needle!r}"
    # open_pr=false must stop before the token is read (REQ-011).
    stop = text.index("Stop here when open-PR is not authorized")
    assert stop < text.index("python3 \"$GITEA_PR\" open-pr")
    assert "never read" in text[stop : stop + 400]


@pytest.mark.parametrize("rel", ["workflows/publish/preflight.md", "workflows/publish/push.md", "workflows/publish/open-pr.md"])
def test_publish_helper_shadows_keep_gascity_text_and_templating(rel: str) -> None:
    gascity = read(GASCITY_ASSETS, rel)
    gitea = read(GITEA_ASSETS, rel)
    assert gitea.startswith(gascity), f"{rel} must start with gascity's text verbatim"
    for var in re.findall(r"\{\{\s*\w+\s*\}\}", gascity):
        assert var in gitea


def test_build_base_shadow_keeps_gascity_text() -> None:
    rel = "workflows/build-base/publish.md"
    assert read(GITEA_ASSETS, rel).startswith(read(GASCITY_ASSETS, rel))


@pytest.mark.parametrize("rel", SHADOWED)
def test_shadow_never_reaches_for_github_or_raw_http(rel: str) -> None:
    text = read(GITEA_ASSETS, rel)
    for pattern, label in FORBIDDEN:
        match = pattern.search(text)
        assert match is None, f"{rel} contains {label}: {match.group(0)!r}"


@pytest.mark.parametrize("rel", SHADOWED)
def test_shadow_never_prints_the_token(rel: str) -> None:
    text = read(GITEA_ASSETS, rel)
    assert not re.search(r"\bcat\s+[^\n`]*token", text), f"{rel} cats the token file"
    assert "--token " not in text
    assert not re.search(r"(?<![A-Z_])GITEA_TOKEN(?![A-Z_])", text), f"{rel} names a token env var"


def test_formulas_layer_exists_with_no_formula() -> None:
    formulas = PACK_DIR / "formulas"
    assert formulas.is_dir(), "gc adds a formula layer only for a pack with a formulas/ dir"
    assert list(formulas.glob("*.formula.toml")) == []
    assert formulas.joinpath(".gitkeep").is_file()


@pytest.mark.skipif(tomllib is None, reason="tomllib needs Python 3.11")
def test_pack_manifest_is_schema_2_with_no_agents_or_services() -> None:
    manifest = tomllib.loads(PACK_DIR.joinpath("pack.toml").read_text(encoding="utf-8"))
    assert manifest["pack"]["name"] == "gitea"
    assert manifest["pack"]["schema"] == 2
    assert set(manifest) == {"pack"}, f"pack.toml declares more than [pack]: {sorted(manifest)}"
    assert not (PACK_DIR / "agents").exists(), "agents would collide with gascity's gc.* agents"


@pytest.mark.skipif(tomllib is None, reason="tomllib needs Python 3.11")
def test_every_command_points_at_an_executable_wrapper() -> None:
    commands = sorted((PACK_DIR / "commands").glob("*/command.toml"))
    assert {path.parent.name for path in commands} == {"open-pr", "parse-origin"}
    for command in commands:
        spec = tomllib.loads(command.read_text(encoding="utf-8"))
        assert spec.get("description")
        wrapper = (command.parent / spec["run"]).resolve()
        assert wrapper.is_file(), f"{command} runs missing {spec['run']}"
        assert os.access(wrapper, os.X_OK), f"{wrapper} is not executable"
        assert command.parent.joinpath("help.md").is_file()
        text = wrapper.read_text(encoding="utf-8")
        assert 'GC_PACK_DIR' in text and "assets/scripts/gitea_pr.py" in text
        assert f"gitea_pr.py\" {command.parent.name}" in text
