"""Context builder — assembles system prompt from workspace files.

Organizes files into cache tiers for provider-level optimization.
Generates tool usage section and skills index automatically.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class ContextBuilder:
    """Builds system prompt blocks with cache tier metadata."""

    def __init__(
        self,
        workspace: Path,
        stable_files: list[str],
        semi_stable_files: list[str],
        tier_overrides: dict | None = None,
    ):
        self.workspace = workspace
        self.stable_files = stable_files
        self.semi_stable_files = semi_stable_files
        self.tier_overrides = tier_overrides or {}

    def build(
        self,
        tier: str = "full",
        source: str = "",
        tool_descriptions: list[tuple[str, str]] | None = None,
        skill_index: str = "",
        always_on_skills: list[str] | None = None,
        skill_bodies: dict[str, str] | None = None,
        extra_dynamic: str = "",
    ) -> list[dict]:
        """Build system prompt blocks for the given tier.

        Returns list of {"text": str, "tier": "stable"|"semi_stable"|"dynamic"}
        """
        blocks = []

        # Determine file lists for this tier
        stable, semi_stable = self._files_for_tier(tier)

        # Stable block: persona files + tool instructions
        stable_text = self._read_files(stable)
        if tool_descriptions:
            stable_text += "\n\n## Available Tools\n\n"
            for name, desc in tool_descriptions:
                stable_text += f"- **{name}**: {desc}\n"

        if stable_text.strip():
            blocks.append({"text": stable_text, "tier": "stable"})

        # Semi-stable block: memory files + always-on skills
        semi_text = self._read_files(semi_stable)

        # Always-on skill bodies
        if always_on_skills and skill_bodies:
            for skill_name in always_on_skills:
                body = skill_bodies.get(skill_name, "")
                if body:
                    semi_text += f"\n\n## Skill: {skill_name}\n\n{body}"

        # Skills index (for on-demand loading)
        if skill_index:
            semi_text += f"\n\n## Available Skills\n\n{skill_index}\n\nUse the `load_skill` tool to load a skill's full instructions."

        if semi_text.strip():
            blocks.append({"text": semi_text, "tier": "semi_stable"})

        # Dynamic block: runtime metadata
        dynamic = self._build_dynamic(source=source, extra=extra_dynamic)
        if dynamic.strip():
            blocks.append({"text": dynamic, "tier": "dynamic"})

        return blocks

    def _files_for_tier(self, tier: str) -> tuple[list[str], list[str]]:
        """Get file lists for a tier, with override support."""
        if tier == "full":
            return self.stable_files, self.semi_stable_files

        override = self.tier_overrides.get(tier, {})
        if isinstance(override, dict):
            stable = override.get("stable", [])
            semi = override.get("semi_stable", [])
            return stable, semi

        # Fallback: minimal
        return [], []

    def _read_files(self, file_names: list[str]) -> str:
        """Read and concatenate workspace files."""
        parts = []
        for name in file_names:
            path = self.workspace / name
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    parts.append(content)
                except Exception as e:
                    log.warning("Failed to read %s: %s", path, e)
            else:
                log.debug("Context file not found: %s", path)
        return "\n\n".join(parts)

    def _build_dynamic(self, source: str = "", extra: str = "") -> str:
        """Build dynamic context block (changes every turn)."""
        now = time.strftime("%a, %d. %b %Y - %H:%M %Z")
        parts = [f"Current date/time: {now}"]
        if source == "system":
            parts.append(
                "Session type: automated infrastructure. "
                "Messages in this session are cron-triggered system automation, "
                "not from the user. Execute tasks as instructed. "
                "Replies are internal only — not delivered to any channel."
            )
        elif source == "http":
            parts.append(
                "Session type: HTTP API integration. "
                "Messages in this session come from an external automation pipeline "
                "(automation pipelines, scripts, webhooks), not from the user via the primary channel. "
                "Process requests and return useful responses. "
                "Use the message tool to notify the user on the primary channel if the results warrant it."
            )
        if extra:
            parts.append(extra)
        return "\n".join(parts)

    def build_stable(self) -> list[dict]:
        """Return only the stable context blocks (persona/identity).

        Used by consolidation for persona-aware extraction.
        Read-only — doesn't modify state, doesn't depend on session.
        """
        stable_text = self._read_files(self.stable_files)
        if stable_text.strip():
            return [{"text": stable_text, "tier": "stable"}]
        return []

    def reload(self) -> None:
        """Reload workspace files on next build (called on SIGUSR1).

        Since files are read fresh on each build(), this is a no-op.
        Exists for explicit signaling intent.
        """
        log.info("Context reload triggered (files will be re-read on next build)")
