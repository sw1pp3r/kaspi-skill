from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
SCRIPT = ROOT / "scripts" / "kaspi.py"


class AgentSkillContractTests(unittest.TestCase):
    def test_portable_frontmatter_and_agent_triggers(self) -> None:
        content = SKILL.read_text(encoding="utf-8")
        match = re.match(r"^---\n(?P<frontmatter>.*?)\n---\n", content, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md needs YAML frontmatter")
        frontmatter = match.group("frontmatter")
        keys = [line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line]
        self.assertEqual(keys, ["name", "description"])
        self.assertIn("name: kaspi", frontmatter)
        self.assertIn("Claude Code", frontmatter)
        self.assertIn("Codex", frontmatter)
        self.assertIn("/kaspi", frontmatter)
        self.assertIn("$kaspi", frontmatter)
        self.assertNotRegex(content, r"/Users/|/home/[^<]")

    def test_openai_metadata_matches_skill(self) -> None:
        metadata = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Kaspi"', metadata)
        self.assertIn("$kaspi", metadata)

    def test_readme_has_one_command_for_both_agents(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        command = "npx skills add sw1pp3r/kaspi-skill -g -a claude-code codex -y"
        self.assertIn(command, readme)
        self.assertIn("`/kaspi`", readme)
        self.assertIn("`$kaspi`", readme)

    def test_help_does_not_require_a_writable_temp_directory(self) -> None:
        probe = textwrap.dedent(
            f"""
            import runpy
            import sys
            from unittest.mock import patch

            script = {str(SCRIPT)!r}
            sys.argv = [script, "--help"]
            with patch("tempfile.gettempdir", side_effect=FileNotFoundError("read-only sandbox")):
                try:
                    runpy.run_path(script, run_name="__main__")
                except SystemExit as exc:
                    raise SystemExit(exc.code or 0)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shortlist", result.stdout)


if __name__ == "__main__":
    unittest.main()
