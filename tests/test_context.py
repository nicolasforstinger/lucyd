"""Tests for context.py — system prompt assembly from workspace files."""

from context import ContextBuilder


class TestFullTier:
    def test_loads_stable_and_semi_stable(self, tmp_workspace):
        """Full tier loads all stable + semi-stable files."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md", "AGENTS.md", "USER.md", "IDENTITY.md", "TOOLS.md"],
            semi_stable_files=["MEMORY.md"],
        )
        blocks = builder.build(tier="full")

        # Should have stable, semi-stable, and dynamic blocks
        assert len(blocks) >= 2

        stable = blocks[0]
        assert stable["tier"] == "stable"
        assert "I am TestAgent" in stable["text"]
        assert "Behavior rules" in stable["text"]
        assert "TestUser" in stable["text"]

        semi = blocks[1]
        assert semi["tier"] == "semi_stable"
        assert "Long-term memories" in semi["text"]


class TestOperationalTier:
    def test_loads_correct_subset(self, tmp_workspace):
        """Operational tier uses tier override files, not full set."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md", "AGENTS.md", "USER.md", "IDENTITY.md", "TOOLS.md"],
            semi_stable_files=["MEMORY.md"],
            tier_overrides={
                "operational": {
                    "stable": ["SOUL.md", "AGENTS.md", "IDENTITY.md"],
                    "semi_stable": ["HEARTBEAT.md"],
                }
            },
        )
        blocks = builder.build(tier="operational")

        stable = blocks[0]
        assert stable["tier"] == "stable"
        assert "I am TestAgent" in stable["text"]
        # USER.md should NOT be in operational tier
        assert "TestUser." not in stable["text"]

        semi = blocks[1]
        assert semi["tier"] == "semi_stable"
        assert "Automation tasks" in semi["text"]
        # MEMORY.md should NOT be in operational tier
        assert "Long-term memories" not in semi["text"]


class TestMissingFile:
    def test_missing_file_doesnt_crash(self, tmp_workspace):
        """One missing file doesn't crash — skip it, load the rest."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md", "NONEXISTENT.md", "AGENTS.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(tier="full")
        assert len(blocks) >= 1
        stable = blocks[0]
        assert "I am TestAgent" in stable["text"]
        assert "Behavior rules" in stable["text"]


class TestSourceAwareDynamic:
    def test_system_source_adds_framing(self, tmp_workspace):
        """System source adds automated infrastructure annotation."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(tier="full", source="system")
        dynamic = blocks[-1]
        assert dynamic["tier"] == "dynamic"
        assert "automated infrastructure" in dynamic["text"]

    def test_http_source_adds_framing(self, tmp_workspace):
        """HTTP source adds API integration annotation."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(tier="full", source="http")
        dynamic = blocks[-1]
        assert dynamic["tier"] == "dynamic"
        assert "HTTP API" in dynamic["text"]

    def test_telegram_source_no_framing(self, tmp_workspace):
        """Telegram source has no special annotation."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(tier="full", source="telegram")
        dynamic = blocks[-1]
        assert "automated infrastructure" not in dynamic["text"]
        assert "HTTP API" not in dynamic["text"]

    def test_empty_source_no_framing(self, tmp_workspace):
        """Empty source (default) has no special annotation."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(tier="full")
        dynamic = blocks[-1]
        assert "automated infrastructure" not in dynamic["text"]
        assert "HTTP API" not in dynamic["text"]


class TestSkillsAppended:
    def test_skills_after_workspace_files(self, tmp_workspace):
        """Skill content appears in semi-stable block after workspace files."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
        )
        blocks = builder.build(
            tier="full",
            always_on_skills=["compute-routing"],
            skill_bodies={"compute-routing": "Route compute to appropriate model."},
        )

        semi = blocks[1]
        assert semi["tier"] == "semi_stable"
        # Memory content first, then skill
        mem_pos = semi["text"].find("Long-term memories")
        skill_pos = semi["text"].find("compute-routing")
        assert mem_pos < skill_pos


# ─── Context Reload ──────────────────────────────────────────────


class TestReload:
    """TEST-11: Verify reload() is a no-op — stable/semi-stable content unchanged."""

    def test_reload_is_noop(self, tmp_workspace):
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
        )
        before = builder.build(tier="full")
        builder.reload()
        after = builder.build(tier="full")
        # Compare text content (dynamic block has timestamp so compare stable/semi only)
        assert before[0]["text"] == after[0]["text"]
        assert before[1]["text"] == after[1]["text"]

    def test_reload_picks_up_file_changes(self, tmp_workspace):
        """After reload + rebuild, changed files are reflected."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
        )
        before = builder.build(tier="full")
        original_stable = before[0]["text"]

        # Modify SOUL.md on disk
        (tmp_workspace / "SOUL.md").write_text("# Soul\nI am UpdatedAgent.")
        builder.reload()
        after = builder.build(tier="full")

        assert "UpdatedAgent" in after[0]["text"]
        assert after[0]["text"] != original_stable

    def test_reload_preserves_tier_structure(self, tmp_workspace):
        """Reload does not alter the block tier assignments."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
        )
        before = builder.build(tier="full")
        builder.reload()
        after = builder.build(tier="full")

        assert len(before) == len(after)
        for b, a in zip(before, after, strict=True):
            assert b["tier"] == a["tier"]


# ─── Voice Reply Injection ──────────────────────────────────────


class TestVoiceReplyInjection:
    """Voice reply instruction in dynamic block based on has_voice + tts tool."""

    def _builder(self, tmp_workspace):
        return ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )

    def test_voice_with_tts_injects_preference(self, tmp_workspace):
        """Voice message + tts tool → voice reply instruction injected."""
        builder = self._builder(tmp_workspace)
        blocks = builder.build(
            tier="full",
            has_voice=True,
            tool_descriptions=[("message", "Send text"), ("tts", "Text to speech")],
        )
        dynamic = blocks[-1]["text"]
        assert "voice message" in dynamic
        assert "tts tool" in dynamic

    def test_voice_without_tts_no_injection(self, tmp_workspace):
        """Voice message but no tts tool → no injection."""
        builder = self._builder(tmp_workspace)
        blocks = builder.build(
            tier="full",
            has_voice=True,
            tool_descriptions=[("message", "Send text")],
        )
        dynamic = blocks[-1]["text"]
        assert "voice message" not in dynamic

    def test_no_voice_no_injection(self, tmp_workspace):
        """No voice message → no injection regardless of tools."""
        builder = self._builder(tmp_workspace)
        blocks = builder.build(
            tier="full",
            has_voice=False,
            tool_descriptions=[("tts", "Text to speech")],
        )
        dynamic = blocks[-1]["text"]
        assert "voice message" not in dynamic

    def test_voice_with_images_both_inject(self, tmp_workspace):
        """Both voice and images → both instructions present."""
        builder = self._builder(tmp_workspace)
        blocks = builder.build(
            tier="full",
            has_voice=True,
            has_images=True,
            tool_descriptions=[("tts", "Text to speech")],
        )
        dynamic = blocks[-1]["text"]
        assert "voice message" in dynamic
        assert "Images are visible only" in dynamic
