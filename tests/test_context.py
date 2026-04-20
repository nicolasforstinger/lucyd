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


class TestTalkerAwareDynamic:
    def test_system_talker_adds_automation_framing(self, tmp_workspace):
        """talker=system produces automation framing."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(talker="system")
        dynamic = blocks[-1]
        assert dynamic["tier"] == "dynamic"
        assert "automated infrastructure" in dynamic["text"]

    def test_user_talker_adds_user_framing(self, tmp_workspace):
        """talker=user produces user/conversation framing."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(talker="user")
        dynamic = blocks[-1]
        assert "user" in dynamic["text"].lower()

    def test_operator_talker_adds_operator_framing(self, tmp_workspace):
        """talker=operator produces operator framing (no user memory)."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(talker="operator")
        dynamic = blocks[-1]
        assert "administrator" in dynamic["text"].lower() or "operator" in dynamic["text"].lower()

    def test_agent_talker_adds_agent_framing(self, tmp_workspace):
        """talker=agent produces self-action framing."""
        builder = ContextBuilder(
            workspace=tmp_workspace,
            stable_files=["SOUL.md"],
            semi_stable_files=[],
        )
        blocks = builder.build(talker="agent")
        dynamic = blocks[-1]
        assert "self-" in dynamic["text"] or "agent-to-agent" in dynamic["text"]


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




