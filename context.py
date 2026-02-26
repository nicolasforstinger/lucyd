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
        silent_tokens: list[str] | None = None,
        max_turns: int = 0,
        max_cost: float = 0.0,
        compaction_threshold: int = 0,
        has_images: bool = False,
        has_voice: bool = False,
        sender: str = "",
    ) -> list[dict]:
        """Build system prompt blocks for the given tier.

        Returns list of {"text": str, "tier": "stable"|"semi_stable"|"dynamic"}
        """
        blocks = []

        # Determine file lists for this tier
        stable, semi_stable = self._files_for_tier(tier)
        log.debug("Context tier=%s: %d stable, %d semi-stable files",
                  tier, len(stable), len(semi_stable))

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
                    semi_text += f"\n\n## Skill: {skill_name} [active — loaded automatically]\n\n{body}"

        # Skills index (for on-demand loading)
        if skill_index:
            semi_text += (
                "\n\n## Skills Available for Loading\n\n"
                "These skills are NOT loaded yet. Use the `load_skill` tool "
                "with the skill name to load one.\n\n"
                f"{skill_index}"
            )

        if semi_text.strip():
            blocks.append({"text": semi_text, "tier": "semi_stable"})

        # Dynamic block: runtime metadata
        voice_reply = (
            has_voice
            and bool(tool_descriptions)
            and any(n == "tts" for n, _ in tool_descriptions)
        )
        dynamic = self._build_dynamic(
            tier=tier, source=source, extra=extra_dynamic,
            silent_tokens=silent_tokens, max_turns=max_turns,
            max_cost=max_cost, compaction_threshold=compaction_threshold,
            has_images=has_images, voice_reply=voice_reply, sender=sender,
        )
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
        """Read and concatenate workspace files with boundary markers."""
        parts = []
        for name in file_names:
            path = self.workspace / name
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    parts.append(f"--- {name} ---\n{content}")
                except Exception as e:
                    log.warning("Failed to read %s: %s", path, e)
            else:
                log.debug("Context file not found: %s", path)
        return "\n\n".join(parts)

    def _build_dynamic(
        self,
        tier: str = "full",
        source: str = "",
        extra: str = "",
        silent_tokens: list[str] | None = None,
        max_turns: int = 0,
        max_cost: float = 0.0,
        compaction_threshold: int = 0,
        has_images: bool = False,
        voice_reply: bool = False,
        sender: str = "",
    ) -> str:
        """Build dynamic context block (changes every turn)."""
        now = time.strftime("%a, %d. %b %Y - %H:%M %Z")
        parts = [f"Current date/time: {now}"]

        # Tier announcement — tell agent what context it has
        if tier == "full":
            parts.append(
                "Context tier: full. All workspace files, memory, and skills are loaded."
            )
        elif tier == "operational":
            parts.append(
                "Context tier: operational. Only essential workspace files are loaded. "
                "Memory files and some personality files are not available in this session."
            )
        elif tier == "minimal":
            parts.append(
                "Context tier: minimal. No workspace files are loaded. You have tools only."
            )

        # Source framing — tell agent where messages come from
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
        elif source == "telegram":
            parts.append(
                "Session type: primary channel (Telegram). "
                "Messages come from the user via Telegram."
            )
        elif source == "cli":
            parts.append(
                "Session type: CLI. "
                "Messages come from a local command-line interface."
            )

        # Session contact
        if sender:
            parts.append(f"Session contact: {sender}")

        # Framework conventions
        parts.append(
            "Messages prefixed with [system: ...] are framework notifications, not from the user."
        )

        # Consolidation pipeline awareness
        parts.append(
            "Background pipeline: facts, episodes, and commitments are automatically "
            "extracted from your sessions and stored in structured memory. You do not "
            "need to manually summarize conversations — the pipeline handles this."
        )

        # Silent tokens
        if silent_tokens:
            tokens_str = ", ".join(silent_tokens)
            parts.append(
                f"Silent response tokens: {tokens_str}. "
                f"If your response starts or ends with one of these, "
                f"it is NOT delivered to the user."
            )

        # Limits
        if max_turns:
            parts.append(f"Tool-use turn limit: {max_turns} per message.")
        if max_cost > 0:
            parts.append(f"Cost limit: ${max_cost:.2f} per message. Loop stops if exceeded.")
        if compaction_threshold:
            parts.append(
                f"Compaction threshold: {compaction_threshold:,} tokens. "
                f"Older messages are summarized when exceeded."
            )

        # Image ephemerality (only when images present)
        if has_images:
            parts.append(
                "Note: Images are visible only on the turn they are received. "
                "Previous-turn images are NOT in your conversation history. "
                "Describe or summarize image content in text if you need to reference it later."
            )

        # Voice reply preference (only when voice message + tts available)
        if voice_reply:
            parts.append(
                "The user sent a voice message. "
                "Prefer replying via the tts tool with send_to set to the contact name."
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
