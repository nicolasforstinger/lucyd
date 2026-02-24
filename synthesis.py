"""Memory synthesis layer for Lucyd.

Transforms raw recall blocks into style-appropriate context
before injection into the response prompt. Optional — defaults
to passthrough ("structured") for zero regression.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# --- Synthesis Prompts ------------------------------------------------

PROMPTS: dict[str, str] = {
    "narrative": (
        "TASK: Rewrite the memory blocks below into a short narrative paragraph.\n\n"
        "OUTPUT RULES (follow exactly):\n"
        "1. Write 2-4 sentences of prose. No more.\n"
        "2. Use temporal framing: 'over the past week', 'since Monday', 'in the last few days'.\n"
        "3. Show trajectory: 'went from X to Y', 'started with X, now at Y'.\n"
        "4. DO NOT list, enumerate, or use bullet points. No dashes, no numbering.\n"
        "5. DO NOT invent facts. Only use information from the blocks below.\n"
        "6. If there are open commitments with deadlines, copy them exactly at the end "
        "on a line starting with 'Open commitments:'.\n"
        "7. Return ONLY the paragraph (and commitments line if any). "
        "No preamble, no explanation, no labels, no 'Here is...'.\n\n"
        "EXAMPLE INPUT:\n"
        "[Known facts]\n"
        "  user — project: launched beta\n"
        "  user — mood: stressed\n"
        "[Recent conversations]\n"
        "  [2026-02-20] Debugged auth system (tone: frustrated)\n"
        "  [2026-02-22] Shipped beta to first client (tone: relieved)\n"
        "[Open commitments]\n"
        "  #3 - user: send invoice (by 2026-02-25)\n\n"
        "EXAMPLE OUTPUT:\n"
        "After a frustrating stretch debugging the auth system, the user shipped "
        "the beta to their first client by the end of the week — stressed but relieved "
        "to have it out the door.\n"
        "Open commitments: #3 - user: send invoice (by 2026-02-25)\n\n"
        "MEMORY BLOCKS:\n{recall_text}\n\n"
        "OUTPUT:"
    ),
    "factual": (
        "TASK: Rewrite the memory blocks below into a short factual summary.\n\n"
        "OUTPUT RULES (follow exactly):\n"
        "1. Write 3-5 sentences of prose. No more.\n"
        "2. Lead with the most recent or important facts.\n"
        "3. Group related facts in the same sentence where natural.\n"
        "4. DO NOT list, enumerate, or use bullet points. No dashes, no numbering.\n"
        "5. DO NOT invent facts. Only use information from the blocks below.\n"
        "6. Neutral tone. No emotional framing, no editorializing.\n"
        "7. If there are open commitments with deadlines, copy them exactly at the end "
        "on a line starting with 'Open commitments:'.\n"
        "8. Return ONLY the summary (and commitments line if any). "
        "No preamble, no explanation, no labels, no 'Here is...'.\n\n"
        "EXAMPLE INPUT:\n"
        "[Known facts]\n"
        "  user — location: Vienna\n"
        "  user — project: CRM migration\n"
        "[Recent conversations]\n"
        "  [2026-02-21] Reviewed database schema (tone: neutral)\n"
        "[Open commitments]\n"
        "  #5 - user: deploy staging (by 2026-02-24)\n\n"
        "EXAMPLE OUTPUT:\n"
        "The user is based in Vienna and currently working on a CRM migration. "
        "The database schema was reviewed on Feb 21.\n"
        "Open commitments: #5 - user: deploy staging (by 2026-02-24)\n\n"
        "MEMORY BLOCKS:\n{recall_text}\n\n"
        "OUTPUT:"
    ),
}

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

    prompt_template = PROMPTS.get(style)
    if prompt_template is None:
        log.warning("Unknown synthesis_style '%s', falling back to structured", style)
        return SynthesisResult(recall_text)

    prompt = prompt_template.format(recall_text=recall_text)

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
