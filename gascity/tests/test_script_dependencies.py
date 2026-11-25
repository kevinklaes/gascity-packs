"""The pack's YAML-importing scripts declare PyYAML inline (PEP 723).

Check gates run with a narrow PATH whose python3 usually lacks PyYAML, so
build-artifact-valid.sh runs the validator through `uv run --script`, which
installs what the script's inline metadata declares. A YAML-importing script
without that block would fail there with `PyYAML is required ...`.
"""

from __future__ import annotations

import pathlib
import re
import tomllib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "assets" / "scripts"
YAML_IMPORT = re.compile(r"^\s*import yaml\b", re.M)
# The reference regex from the inline script metadata specification.
PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
EXPECTED = {
    "create_beads_from_tasks.py",
    "github_reports.py",
    "validate_build_artifact.py",
    "validate_context_bundle.py",
    "validate_verdict_report.py",
}
NO_YAML = {"artifacts.py", "github_api.py"}


def yaml_importing_scripts() -> dict[str, str]:
    found = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if YAML_IMPORT.search(text):
            found[path.name] = text
    return found


def requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement.strip(), maxsplit=1)[0].lower()


class ScriptDependencyTests(unittest.TestCase):
    def test_yaml_importing_scripts_are_the_expected_set(self) -> None:
        # Guards against the scan silently matching nothing.
        self.assertEqual(set(yaml_importing_scripts()), EXPECTED)

    def test_yaml_importing_scripts_declare_pyyaml_inline(self) -> None:
        for name, text in yaml_importing_scripts().items():
            with self.subTest(script=name):
                lines = text.splitlines()
                self.assertGreaterEqual(len(lines), 2, name)
                self.assertTrue(lines[0].startswith("#!"), f"{name}: line 1 must be the shebang")
                self.assertEqual(
                    lines[1], "# /// script", f"{name}: the PEP 723 block must start on line 2"
                )

                blocks = [m for m in PEP723_BLOCK.finditer(text) if m.group("type") == "script"]
                self.assertEqual(len(blocks), 1, f"{name}: expected exactly one script block")
                block = blocks[0]
                self.assertEqual(block.start(), len(lines[0]) + 1, name)

                content = "".join(
                    line[2:] if line.startswith("# ") else line[1:]
                    for line in block.group("content").splitlines(keepends=True)
                )
                metadata = tomllib.loads(content)
                dependencies = metadata.get("dependencies", [])
                self.assertIn(
                    "pyyaml",
                    {requirement_name(dep) for dep in dependencies},
                    f"{name}: dependencies must include pyyaml",
                )

    def test_scripts_without_yaml_have_no_block(self) -> None:
        for name in sorted(NO_YAML):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIsNone(YAML_IMPORT.search(text), name)
                self.assertNotIn("# /// script", text.splitlines(), name)

    def test_docs_show_uv_run_script_forms(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for script in ("validate_context_bundle.py", "validate_verdict_report.py"):
            self.assertIn(f"uv run --script <pack-root>/assets/scripts/{script}", readme)
        self.assertIn("$HOME/.cache/uv", readme)
        self.assertIn("gitignore `.cache/`", readme)

        skill = (ROOT / "skills" / "mayor" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(
            "uv run --script <pack-root>/assets/scripts/create_beads_from_tasks.py", skill
        )


if __name__ == "__main__":
    unittest.main()
