"""Tests for verification.py — compaction summary verification."""

from verification import (
    _build_deterministic_summary,
    _detect_turn_labels,
    _extract_distinctive_tokens,
    verify_compaction_summary,
)


# ─── Tier 1: Structural Detection ───────────────────────────────


class TestDetectTurnLabels:
    """_detect_turn_labels counts dialogue patterns."""

    def test_no_labels_in_clean_summary(self):
        summary = "Nicolas and the agent discussed dinner plans and decided on pasta."
        assert _detect_turn_labels(summary) == 0

    def test_user_colon_detected(self):
        summary = "user: hello\nuser: how are you\nuser: goodbye"
        assert _detect_turn_labels(summary) == 3

    def test_assistant_colon_detected(self):
        summary = "assistant: I'm fine\nAssistant: Sure thing"
        assert _detect_turn_labels(summary) == 2

    def test_capital_A_colon_detected(self):
        summary = "A: some response\nA: another response"
        assert _detect_turn_labels(summary) == 2

    def test_human_colon_detected(self):
        summary = "Human: first message\nhuman: second"
        assert _detect_turn_labels(summary) == 2

    def test_timestamp_brackets_detected(self):
        summary = "At [14:32] they discussed. Then [9:05] something happened."
        assert _detect_turn_labels(summary) == 2

    def test_datetime_timestamps_detected(self):
        summary = "On 2026-03-04 14:32 this happened. Then 2026-03-05 09:00 that."
        assert _detect_turn_labels(summary) == 2

    def test_mixed_patterns(self):
        summary = "user: hello\nassistant: hi\n[14:32] discussed things"
        assert _detect_turn_labels(summary) >= 3

    def test_inline_user_not_counted(self):
        """'user:' must be at line start to match."""
        summary = "The user: experience was good."
        # This is at line start, so it matches. That's intentional —
        # "The user:" isn't typical summary prose.
        assert _detect_turn_labels(summary) == 0  # "The user:" has a prefix


class TestDetectTurnLabelsEdgeCases:
    """Edge cases for structural detection."""

    def test_empty_string(self):
        assert _detect_turn_labels("") == 0

    def test_user_in_prose_without_colon(self):
        summary = "The user mentioned wanting to visit Vienna."
        assert _detect_turn_labels(summary) == 0

    def test_colon_in_url(self):
        """URLs contain colons but shouldn't trigger turn detection."""
        summary = "They shared https://example.com and discussed it."
        assert _detect_turn_labels(summary) == 0


# ─── Tier 2: Entity Grounding ───────────────────────────────────


class TestExtractDistinctiveTokens:
    """_extract_distinctive_tokens finds proper nouns, numbers, etc."""

    def test_proper_nouns(self):
        tokens = _extract_distinctive_tokens("Nicolas went to Vienna with Maria.")
        assert "nicolas" in tokens
        assert "vienna" in tokens
        assert "maria" in tokens

    def test_multi_digit_numbers(self):
        tokens = _extract_distinctive_tokens("The invoice was 12345 euros.")
        assert "12345" in tokens

    def test_two_digit_not_extracted(self):
        tokens = _extract_distinctive_tokens("He had 42 items.")
        assert "42" not in tokens

    def test_urls(self):
        tokens = _extract_distinctive_tokens("Visit https://example.com/page for details.")
        assert "https://example.com/page" in tokens

    def test_quoted_phrases(self):
        tokens = _extract_distinctive_tokens('He said "never do that again" firmly.')
        assert "never do that again" in tokens

    def test_short_quotes_excluded(self):
        tokens = _extract_distinctive_tokens('She said "no" clearly.')
        assert "no" not in tokens

    def test_empty_string(self):
        assert _extract_distinctive_tokens("") == set()

    def test_no_distinctive_tokens(self):
        tokens = _extract_distinctive_tokens("they talked about things and stuff.")
        assert len(tokens) == 0


class TestEntityGrounding:
    """verify_compaction_summary tier 2 — entity grounding checks."""

    def test_all_grounded_passes(self):
        source = "Nicolas went to Vienna to meet Maria about the 12345 invoice."
        summary = "Nicolas visited Vienna and discussed the 12345 invoice with Maria."
        result = verify_compaction_summary(summary, source, 100)
        assert result.passed

    def test_ungrounded_entities_fail(self):
        source = "They discussed dinner plans."
        summary = "Bartholomew traveled to Constantinople and met Anastasia about invoice 98765."
        result = verify_compaction_summary(
            summary, source, 100, grounding_threshold=0.5,
        )
        assert not result.passed
        assert result.tier_failed == "grounding"
        assert len(result.ungrounded_tokens) > 0

    def test_partial_grounding_at_threshold(self):
        source = "Nicolas mentioned the 12345 number."
        # 2 grounded (nicolas, 12345), 1 ungrounded (vienna) = 66%
        summary = "Nicolas referenced 12345 from Vienna."
        result = verify_compaction_summary(
            summary, source, 100, grounding_threshold=0.5,
        )
        assert result.passed

    def test_low_grounding_ratio_rejects(self):
        """1 of 4 distinctive tokens grounded = 25% < 50% threshold."""
        source = "Nicolas mentioned something."
        # 4 tokens: Nicolas (grounded), Vienna, Constantinople, Bartholomew (ungrounded)
        summary = "Nicolas traveled to Vienna, Constantinople, and met Bartholomew."
        result = verify_compaction_summary(
            summary, source, 100, grounding_threshold=0.5,
        )
        assert not result.passed
        assert result.tier_failed == "grounding"

    def test_exactly_at_grounding_threshold_passes(self):
        """1 of 2 distinctive tokens grounded = 50% = threshold → passes."""
        source = "Nicolas had a discussion."
        # 2 tokens: Nicolas (grounded), Vienna (ungrounded)
        summary = "Nicolas was in Vienna."
        result = verify_compaction_summary(
            summary, source, 100, grounding_threshold=0.5,
        )
        assert result.passed

    def test_no_distinctive_tokens_passes(self):
        source = "they talked about things."
        summary = "a conversation about general topics happened."
        result = verify_compaction_summary(summary, source, 100)
        assert result.passed


# ─── Full Verification Pipeline ─────────────────────────────────


class TestVerifyCompactionSummary:
    """End-to-end verify_compaction_summary tests."""

    def test_clean_summary_passes(self):
        source = "Nicolas asked about the weather. The agent said it was sunny."
        summary = "Nicolas inquired about weather conditions. It was sunny."
        result = verify_compaction_summary(summary, source, 80)
        assert result.passed
        assert result.tier_failed == ""

    def test_turn_labels_rejected(self):
        source = "user: hi\nassistant: hello\nuser: bye\nassistant: see you"
        summary = "user: hi\nassistant: hello\nuser: bye\nassistant: see you"
        result = verify_compaction_summary(summary, source, 100, max_turn_labels=3)
        assert not result.passed
        assert result.tier_failed == "structural"
        assert result.turn_label_count > 3

    def test_exactly_at_threshold_passes(self):
        source = "Human said hi there. The assistant replied. Then continued."
        # 3 turn labels = threshold default, should pass tier 1
        summary = "user: said hi\nassistant: replied\nHuman: continued"
        result = verify_compaction_summary(summary, source, 50, max_turn_labels=3)
        assert result.passed

    def test_structural_checked_before_grounding(self):
        """Tier 1 rejects before tier 2 even runs."""
        source = "They talked."
        summary = (
            "user: hello\nassistant: hi\nuser: bye\nassistant: see you\n"
            "Bartholomew went to Constantinople."
        )
        result = verify_compaction_summary(summary, source, 100)
        assert not result.passed
        assert result.tier_failed == "structural"  # not grounding

    def test_empty_summary_passes(self):
        result = verify_compaction_summary("", "some conversation", 0)
        assert result.passed

    def test_whitespace_only_passes(self):
        result = verify_compaction_summary("   \n  ", "some conversation", 0)
        assert result.passed

    def test_custom_thresholds(self):
        source = "They discussed something."
        summary = "user: first\nassistant: second"
        # With max_turn_labels=5, this should pass tier 1
        result = verify_compaction_summary(
            summary, source, 50, max_turn_labels=5,
        )
        assert result.passed

    def test_token_warning_logged(self, caplog):
        """High output tokens trigger a warning log."""
        import logging
        with caplog.at_level(logging.WARNING, logger="verification"):
            verify_compaction_summary(
                "Some summary.", "Some conversation.", 2000,
                token_warning_threshold=1500,
            )
        assert "output_tokens=2000" in caplog.text

    def test_token_warning_not_logged_under_threshold(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="verification"):
            verify_compaction_summary(
                "Some summary.", "Some conversation.", 500,
                token_warning_threshold=1500,
            )
        assert "output_tokens" not in caplog.text


# ─── Deterministic Fallback ─────────────────────────────────────


class TestBuildDeterministicSummary:
    """_build_deterministic_summary produces a safe fallback."""

    def test_includes_message_count(self):
        result = _build_deterministic_summary(42)
        assert "42 messages" in result

    def test_includes_quality_check_note(self):
        result = _build_deterministic_summary(10)
        assert "quality check failure" in result

    def test_suggests_memory_search(self):
        result = _build_deterministic_summary(10)
        assert "memory_search" in result

    def test_is_bracketed(self):
        result = _build_deterministic_summary(10)
        assert result.startswith("[")
        assert result.endswith("]")
