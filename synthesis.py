"""Memory synthesis layer for Lucyd.

Transforms raw recall blocks into style-appropriate context
before injection into the response prompt. Optional — defaults
to passthrough ("structured") for zero regression.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

VALID_STYLES = {"structured", "narrative", "factual"}


class SynthesisResult:
    """Synthesis output with optional usage for cost tracking."""
    __slots__ = ("text", "usage")

    def __init__(self, text: str, usage=None):
        self.text = text
        self.usage = usage  # providers.Usage or None


async def synthesize_recall(
    recall_text: str,
    style: str,
    provider,
    prompt_override: str = "",
) -> SynthesisResult:
    """Transform raw recall blocks into synthesized context.

    Args:
        recall_text: Raw formatted recall output from inject_recall().
        style: Synthesis style from config ("structured", "narrative", "factual").
        provider: LLM provider instance with format_system/format_messages/complete.

    Returns:
        SynthesisResult with .text (synthesized or original) and .usage (if LLM called).
    """
    if style == "structured" or not recall_text or not recall_text.strip():
        return SynthesisResult(recall_text)

    if not prompt_override:
        log.warning("No synthesis prompt provided for style '%s', falling back to structured", style)
        return SynthesisResult(recall_text)

    prompt = prompt_override.format(recall_text=recall_text)

    try:
        fmt_system = provider.format_system([])
        fmt_messages = provider.format_messages(
            [{"role": "user", "content": prompt}]
        )
        response = await provider.complete(fmt_system, fmt_messages, [])
        synthesized = response.text or ""

        if not synthesized.strip():
            log.warning("Synthesis returned empty, falling back to raw recall")
            return SynthesisResult(recall_text, response.usage)

        # Preserve token usage footer from inject_recall()
        footer_lines = []
        for line in recall_text.splitlines():
            if line.startswith("[Memory loaded:") or line.startswith("[Dropped"):
                footer_lines.append(line)

        result = synthesized.strip()
        if footer_lines:
            result = f"{result}\n{chr(10).join(footer_lines)}"

        log.debug(
            "Synthesis (%s): %d chars -> %d chars",
            style, len(recall_text), len(result),
        )
        return SynthesisResult(result, response.usage)

    except Exception:
        log.exception("Synthesis failed (%s), falling back to raw recall", style)
        return SynthesisResult(recall_text)
