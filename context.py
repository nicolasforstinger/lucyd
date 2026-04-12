"""Context builder — assembles system prompt from workspace files.

Organizes files into stable/semi-stable/dynamic blocks with cache hints.
Generates tool usage section and skills index automatically.
Supports max_system_tokens cap and context budget logging.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import tiktoken

log = logging.getLogger(__name__)

_tiktoken_enc = tiktoken.get_encoding("cl100k_base")


def _estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken cl100k_base.

    Standard BPE tokenizer used by GPT-4 and a reasonable cross-model
    approximation for Anthropic/local models.
    Margin of error: ±5% for English, ±15% for code, ±20% for CJK text.
    """
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    return len(_tiktoken_enc.encode(text))


class ContextBuilder:
    """Builds system prompt blocks with cache-hint metadata."""

    def __init__(
        self,
        workspace: Path,
        stable_files: list[str],
        semi_stable_files: list[str],
        max_system_tokens: int = 0,
    ):
        self.workspace = workspace
        self.stable_files = stable_files
        self.semi_stable_files = semi_stable_files
        self.max_system_tokens = max_system_tokens

    def build(
        self,
        task_type: str = "conversational",
        deliver: bool = True,
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
        sender: str = "",
    ) -> list[dict[str, str]]:
        """Build system prompt blocks.

        Returns list of {"text": str, "tier": "stable"|"semi_stable"|"dynamic"}
        """
        blocks = []

        stable = self.stable_files
        semi_stable = self.semi_stable_files
        log.debug("Context: %d stable, %d semi-stable files",
                  len(stable), len(semi_stable))

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
        dynamic = self._build_dynamic(
            task_type=task_type, deliver=deliver, extra=extra_dynamic,
            silent_tokens=silent_tokens, max_turns=max_turns,
            max_cost=max_cost, compaction_threshold=compaction_threshold,
            has_images=has_images, sender=sender,
        )
        if dynamic.strip():
            blocks.append({"text": dynamic, "tier": "dynamic"})

        # Enforce max_system_tokens cap
        if self.max_system_tokens > 0:
            blocks = self._enforce_token_cap(blocks)

        # Log context budget diagnostic
        self._log_budget(blocks)

        return blocks

    def _enforce_token_cap(self, blocks: list[dict[str, str]]) -> list[dict[str, str]]:
        """Enforce max_system_tokens cap by trimming lower-priority tiers.

        Priority: stable (never trimmed) > semi_stable > dynamic.
        If stable alone exceeds the cap, logs an error — persona is inviolable.
        """
        total = sum(_estimate_tokens(b["text"]) for b in blocks)
        cap = self.max_system_tokens
        if total <= cap:
            return blocks

        # Trim dynamic first, then semi-stable
        for tier in ("dynamic", "semi_stable"):
            if total <= cap:
                break
            for i in range(len(blocks) - 1, -1, -1):
                if blocks[i]["tier"] == tier:
                    removed_tokens = _estimate_tokens(blocks[i]["text"])
                    log.warning(
                        "Context cap: trimming %s block (%d tokens) to fit %d limit",
                        tier, removed_tokens, cap,
                    )
                    blocks.pop(i)
                    total -= removed_tokens
                    if total <= cap:
                        break

        if total > cap:
            log.error(
                "System prompt exceeds max_system_tokens (%d > %d) even after "
                "trimming all semi-stable and dynamic content. Stable persona "
                "is too large — reduce workspace files or increase the cap.",
                total, cap,
            )
        return blocks

    def _log_budget(self, blocks: list[dict[str, str]]) -> None:
        """Log per-tier token breakdown for context budget visibility."""
        by_tier: dict[str, int] = {}
        for b in blocks:
            tier = b.get("tier", "unknown")
            by_tier[tier] = by_tier.get(tier, 0) + _estimate_tokens(b["text"])
        total = sum(by_tier.values())
        parts = [f"{tier}={tokens}" for tier, tokens in sorted(by_tier.items())]
        log.debug("Context budget: %s, total=%d tokens", ", ".join(parts), total)

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
                    log.warning("Failed to read %s: %s", path, e, exc_info=True)
            else:
                log.debug("Context file not found: %s", path)
        return "\n\n".join(parts)

    def _build_dynamic(
        self,
        task_type: str = "conversational",
        deliver: bool = True,
        extra: str = "",
        silent_tokens: list[str] | None = None,
        max_turns: int = 0,
        max_cost: float = 0.0,
        compaction_threshold: int = 0,
        has_images: bool = False,
        sender: str = "",
    ) -> str:
        """Build dynamic context block (changes every turn)."""
        now = time.strftime("%a, %d. %b %Y - %H:%M %Z")
        parts = [f"Current date/time: {now}"]

        # Session framing — tell agent the session lifecycle and intent
        if task_type == "system" and not deliver:
            parts.append(
                "Session type: automated infrastructure. "
                "Messages in this session are system automation, "
                "not from the user. Execute tasks as instructed. "
                "Replies are internal only — not delivered to any channel.",
            )
        elif task_type == "system" and deliver:
            parts.append(
                "Session type: notification routed to operator. "
                "This is an automated notification delivered to the operator's session. "
                "Your reply will be sent to the operator.",
            )
        elif task_type == "task":
            parts.append(
                "Session type: ephemeral task. "
                "Process the request and return a response. "
                "This session closes after your reply.",
            )
        else:
            parts.append(
                "Session type: conversation. "
                "Messages come from the user. Conversation history is preserved.",
            )

        # Session contact
        if sender:
            parts.append(f"Session contact: {sender}")

        # Framework conventions
        parts.append(
            "Messages prefixed with [system: ...] are framework notifications, not from the user.",
        )

        # Consolidation pipeline awareness
        parts.append(
            "Background pipeline: facts, episodes, and commitments are automatically "
            "extracted from your sessions and stored in structured memory. You do not "
            "need to manually summarize conversations — the pipeline handles this.",
        )

        # Silent tokens
        if silent_tokens:
            tokens_str = ", ".join(silent_tokens)
            parts.append(
                f"Silent response tokens: {tokens_str}. "
                f"If your response starts or ends with one of these, "
                f"it is NOT delivered to the user.",
            )

        # Limits
        if max_turns:
            parts.append(f"Tool-use turn limit: {max_turns} per message.")
        if max_cost > 0:
            parts.append(f"Cost limit: ${max_cost:.2f} per message. Loop stops if exceeded.")
        if compaction_threshold:
            parts.append(
                f"Compaction threshold: {compaction_threshold:,} tokens. "
                f"Older messages are summarized when exceeded.",
            )

        # Image ephemerality (only when images present)
        if has_images:
            parts.append(
                "Note: Images are visible only on the turn they are received. "
                "Previous-turn images are NOT in your conversation history. "
                "Describe or summarize image content in text if you need to reference it later.",
            )

        if extra:
            parts.append(extra)
        return "\n".join(parts)

    def build_stable(self) -> list[dict[str, str]]:
        """Return only the stable context blocks (persona/identity).

        Used by consolidation for persona-aware extraction.
        Read-only — doesn't modify state, doesn't depend on session.
        """
        stable_text = self._read_files(self.stable_files)
        if stable_text.strip():
            return [{"text": stable_text, "tier": "stable"}]
        return []

