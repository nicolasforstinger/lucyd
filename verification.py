"""Compaction summary verification.

Structural detection + entity grounding to catch fabricated summaries.
Pure string matching — zero LLM cost.

Two tiers:
  Tier 1 (structural): Regex detects dialogue patterns (user:/assistant:/
    timestamps). High count → the model generated fake turns.
  Tier 2 (entity grounding): Extracts distinctive tokens from summary,
    checks they exist in the source transcript. Low match → hallucinated.
"""

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Tier 1 patterns — dialogue turn labels and timestamps
_TURN_PATTERNS = [
    re.compile(r"^(?:user|User|USER)\s*:", re.MULTILINE),
    re.compile(r"^(?:assistant|Assistant|ASSISTANT|A)\s*:", re.MULTILINE),
    re.compile(r"^(?:Human|human|HUMAN)\s*:", re.MULTILINE),
    re.compile(r"\[\d{1,2}:\d{2}\]"),                      # [14:32]
    re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}"),      # 2026-03-04 14:32
]

# Tier 2 — extract distinctive tokens (proper nouns, numbers, URLs, quoted)
_PROPER_NOUN = re.compile(r"\b[A-Z][a-z]{2,}\b")
_MULTI_DIGIT = re.compile(r"\b\d{3,}\b")
_URL = re.compile(r"https?://\S+")
_QUOTED = re.compile(r'"([^"]{3,})"')


@dataclass
class VerificationResult:
    """Result of compaction summary verification."""

    passed: bool
    tier_failed: str = ""
    details: str = ""
    turn_label_count: int = 0
    ungrounded_tokens: list[str] = field(default_factory=list)


def _detect_turn_labels(summary: str) -> int:
    """Tier 1: Count dialogue-pattern matches in summary."""
    count = 0
    for pat in _TURN_PATTERNS:
        count += len(pat.findall(summary))
    return count


def _extract_distinctive_tokens(text: str) -> set[str]:
    """Tier 2: Extract proper nouns, multi-digit numbers, URLs, quoted phrases."""
    tokens: set[str] = set()
    for m in _PROPER_NOUN.finditer(text):
        tokens.add(m.group().lower())
    for m in _MULTI_DIGIT.finditer(text):
        tokens.add(m.group())
    for m in _URL.finditer(text):
        tokens.add(m.group().lower())
    for m in _QUOTED.finditer(text):
        tokens.add(m.group(1).lower())
    return tokens


def _check_entity_grounding(
    summary: str, source: str,
) -> tuple[bool, list[str]]:
    """Tier 2: Check that distinctive tokens from summary exist in source.

    Returns (passed, ungrounded_tokens).
    """
    summary_tokens = _extract_distinctive_tokens(summary)
    if not summary_tokens:
        return True, []  # nothing distinctive to check

    source_lower = source.lower()
    ungrounded = [t for t in summary_tokens if t not in source_lower]
    grounded_count = len(summary_tokens) - len(ungrounded)
    ratio = grounded_count / len(summary_tokens) if summary_tokens else 1.0
    return ratio >= 0.5, ungrounded  # threshold applied by caller


def _build_deterministic_summary(message_count: int) -> str:
    """Safe fallback when verification fails."""
    return (
        f"[Compacted conversation: {message_count} messages. "
        "Summary omitted due to quality check failure. "
        "Use memory_search to find specific details from before compaction.]"
    )


def verify_compaction_summary(
    summary: str,
    conversation_text: str,
    output_tokens: int,
    *,
    max_turn_labels: int = 3,
    grounding_threshold: float = 0.5,
    token_warning_threshold: int = 1500,
) -> VerificationResult:
    """Verify a compaction summary for fabrication.

    Args:
        summary: The generated summary text.
        conversation_text: The original conversation transcript.
        output_tokens: Number of output tokens used.
        max_turn_labels: Tier 1 rejection threshold.
        grounding_threshold: Tier 2 minimum grounded ratio.
        token_warning_threshold: Log warning above this token count.

    Returns:
        VerificationResult with pass/fail and details.
    """
    if not summary.strip():
        return VerificationResult(passed=True)

    # Token count warning (signal only, not rejection)
    if output_tokens > token_warning_threshold:
        log.warning(
            "Compaction output_tokens=%d exceeds warning threshold %d",
            output_tokens, token_warning_threshold,
        )

    # Tier 1: structural — dialogue patterns
    turn_count = _detect_turn_labels(summary)
    if turn_count > max_turn_labels:
        return VerificationResult(
            passed=False,
            tier_failed="structural",
            details=f"Found {turn_count} turn labels (threshold: {max_turn_labels})",
            turn_label_count=turn_count,
        )

    # Tier 2: entity grounding
    summary_tokens = _extract_distinctive_tokens(summary)
    if summary_tokens:
        source_lower = conversation_text.lower()
        ungrounded = [t for t in summary_tokens if t not in source_lower]
        grounded_count = len(summary_tokens) - len(ungrounded)
        ratio = grounded_count / len(summary_tokens)
        if ratio < grounding_threshold:
            return VerificationResult(
                passed=False,
                tier_failed="grounding",
                details=(
                    f"Only {grounded_count}/{len(summary_tokens)} "
                    f"distinctive tokens grounded ({ratio:.0%} < "
                    f"{grounding_threshold:.0%})"
                ),
                ungrounded_tokens=ungrounded,
            )

    return VerificationResult(passed=True, turn_label_count=turn_count)
