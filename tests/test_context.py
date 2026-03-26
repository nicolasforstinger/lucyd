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
        blocks = builder.build()

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


class TestMissingFile:
    def test_missing_file_doesnt_crash(self, tmp_workspace):
        """One missing file doesn't crash — skip it, load the rest."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md", "NONEXISTENT.md", "AGENTS.md"],
            semi_stable_files=[],
        )
        blocks = builder.build()
        assert len(blocks) >= 1
        stable = blocks[0]
        assert "I am TestAgent" in stable["text"]
        assert "Behavior rules" in stable["text"]


class TestSourceAwareDynamic:
    def test_system_no_deliver_adds_automation_framing(self, tmp_workspace):
        """System task_type with deliver=False adds automated infrastructure annotation."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(task_type="system", deliver=False)
        dynamic = blocks[-1]
        assert dynamic["tier"] == "dynamic"
        assert "automated infrastructure" in dynamic["text"]

    def test_system_deliver_adds_notification_framing(self, tmp_workspace):
        """System task_type with deliver=True adds notification framing."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(task_type="system", deliver=True)
        dynamic = blocks[-1]
        assert "notification routed to operator" in dynamic["text"]

    def test_http_source_adds_framing(self, tmp_workspace):
        """Conversational task_type adds conversation framing."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(task_type="conversational")
        dynamic = blocks[-1]
        assert dynamic["tier"] == "dynamic"
        assert "conversation" in dynamic["text"].lower()

    def test_telegram_source_no_framing(self, tmp_workspace):
        """Task task_type adds ephemeral framing."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(task_type="task")
        dynamic = blocks[-1]
        assert "ephemeral task" in dynamic["text"]
        assert "automated infrastructure" not in dynamic["text"]

    def test_empty_source_no_framing(self, tmp_workspace):
        """Default task_type (conversational) has no system annotation."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build()
        dynamic = blocks[-1]
        assert "automated infrastructure" not in dynamic["text"]
        assert "ephemeral task" not in dynamic["text"]


class TestSkillsAppended:
    def test_skills_after_workspace_files(self, tmp_workspace):
        """Skill content appears in semi-stable block after workspace files."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=["MEMORY.md"],
        )
        blocks = builder.build(

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




