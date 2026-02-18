"""Skill loader — reads skill definitions from workspace.

Skills are markdown files with YAML frontmatter (name, description) + body.
The frontmatter parser handles simple key: value and > / | block scalars
without PyYAML dependency.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter from markdown.

    Handles:
    - Simple key: value
    - Folded block scalar (key: >)
    - Literal block scalar (key: |)

    Returns (metadata_dict, body_text).
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    meta: dict[str, str] = {}
    body_start = len(lines)
    current_key = ""
    current_value = ""
    in_block = False
    block_type = ""  # ">" or "|"

    for i, line in enumerate(lines[1:], start=1):
        stripped = line.strip()

        if stripped == "---":
            # End of frontmatter
            if in_block and current_key:
                meta[current_key] = current_value.strip()
            body_start = i + 1
            break

        if in_block:
            # Inside a block scalar
            if line and not line[0].isspace() and ":" in line:
                # New key — end current block
                meta[current_key] = current_value.strip()
                in_block = False
                current_key = ""
                current_value = ""
                # Fall through to process this line as a key
            else:
                # Continuation of block
                text_line = line.strip() if block_type == ">" else line
                if block_type == ">":
                    if current_value:
                        current_value += " " + text_line
                    else:
                        current_value = text_line
                else:  # "|"
                    if current_value:
                        current_value += "\n" + text_line
                    else:
                        current_value = text_line
                continue

        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            if value in (">", "|"):
                # Block scalar indicator
                current_key = key
                current_value = ""
                in_block = True
                block_type = value
            else:
                # Simple key: value
                # Strip surrounding quotes if present
                if value and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                meta[key] = value

    body = "\n".join(lines[body_start:])
    return meta, body


class SkillLoader:
    """Loads skills from workspace directory."""

    def __init__(self, workspace: Path, skills_dir: str = "skills"):
        self.workspace = workspace
        self.skills_dir = skills_dir
        self._skills: dict[str, dict] = {}
        self._loaded = False

    def scan(self) -> None:
        """Scan skills directory for SKILL.md files."""
        skills_path = self.workspace / self.skills_dir
        if not skills_path.exists():
            log.debug("Skills directory not found: %s", skills_path)
            return

        self._skills = {}
        for skill_dir in sorted(skills_path.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                text = skill_file.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(text)

                name = meta.get("name", skill_dir.name)
                description = meta.get("description", "")

                self._skills[name] = {
                    "name": name,
                    "description": description,
                    "body": body.strip(),
                    "path": str(skill_file),
                }
                log.debug("Loaded skill: %s", name)
            except Exception as e:
                log.warning("Failed to load skill from %s: %s", skill_dir, e)

        self._loaded = True
        log.info("Loaded %d skills from %s", len(self._skills), skills_path)

    def get_skill(self, name: str) -> dict | None:
        """Get a skill by name."""
        if not self._loaded:
            self.scan()
        return self._skills.get(name)

    def list_skill_names(self) -> list[str]:
        """List all available skill names."""
        if not self._loaded:
            self.scan()
        return list(self._skills.keys())

    def build_index(self) -> str:
        """Build a brief skill index for the system prompt."""
        if not self._loaded:
            self.scan()
        if not self._skills:
            return ""

        lines = []
        for name, skill in sorted(self._skills.items()):
            desc = skill["description"]
            if desc:
                lines.append(f"- **{name}**: {desc}")
            else:
                lines.append(f"- **{name}**")
        return "\n".join(lines)

    def get_bodies(self, names: list[str]) -> dict[str, str]:
        """Get skill bodies for a list of names (for always-on injection)."""
        if not self._loaded:
            self.scan()
        return {
            name: self._skills[name]["body"]
            for name in names
            if name in self._skills
        }
