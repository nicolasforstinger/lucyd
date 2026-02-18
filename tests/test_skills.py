"""Tests for skills.py — _parse_frontmatter() and SkillLoader."""


from skills import SkillLoader, _parse_frontmatter

# ─── Frontmatter Parser ─────────────────────────────────────────

class TestParseFrontmatter:
    def test_simple_key_value(self):
        text = "---\nname: hello\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "hello"
        assert body == "body"

    def test_double_quoted_value(self):
        text = '---\nname: "hello world"\n---\n'
        meta, _ = _parse_frontmatter(text)
        assert meta["name"] == "hello world"

    def test_single_quoted_value(self):
        text = "---\nname: 'hello world'\n---\n"
        meta, _ = _parse_frontmatter(text)
        assert meta["name"] == "hello world"

    def test_folded_block_scalar(self):
        text = "---\ndesc: >\n  line one\n  line two\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert "line one" in meta["desc"]
        assert "line two" in meta["desc"]
        assert body == "body"

    def test_literal_block_scalar(self):
        text = "---\ndesc: |\n  line one\n  line two\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert "line one" in meta["desc"]
        assert "\n" in meta["desc"]
        assert body == "body"

    def test_no_frontmatter_returns_empty_dict(self):
        text = "Just some text without frontmatter."
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_body_after_closing_fence(self):
        text = "---\nkey: val\n---\nFirst line\nSecond line"
        meta, body = _parse_frontmatter(text)
        assert meta["key"] == "val"
        assert "First line" in body
        assert "Second line" in body

    def test_empty_frontmatter(self):
        text = "---\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == "body"

    def test_block_followed_by_new_key_ends_block(self):
        text = "---\ndesc: >\n  block text\nnext_key: val\n---\n"
        meta, _ = _parse_frontmatter(text)
        assert "block text" in meta["desc"]
        assert meta["next_key"] == "val"


# ─── SkillLoader ─────────────────────────────────────────────────

class TestSkillLoader:
    def test_scan_finds_skills(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        loader.scan()
        names = loader.list_skill_names()
        assert "compute-routing" in names
        assert "bare-skill" in names

    def test_missing_dir_no_crash(self, tmp_path):
        loader = SkillLoader(tmp_path / "nonexistent")
        loader.scan()  # Should not raise
        assert loader.list_skill_names() == []

    def test_skips_non_dirs(self, skill_workspace):
        # Create a regular file in the skills directory
        (skill_workspace / "skills" / "not-a-dir.txt").write_text("nope")
        loader = SkillLoader(skill_workspace)
        loader.scan()
        assert "not-a-dir.txt" not in loader.list_skill_names()

    def test_get_skill_returns_dict(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        skill = loader.get_skill("compute-routing")
        assert skill is not None
        assert skill["name"] == "compute-routing"
        assert "description" in skill
        assert "body" in skill

    def test_get_skill_missing_returns_none(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        assert loader.get_skill("nonexistent") is None

    def test_list_skill_names(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        names = loader.list_skill_names()
        assert isinstance(names, list)
        assert len(names) == 2

    def test_build_index_formatting(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        index = loader.build_index()
        assert "**compute-routing**" in index
        assert "Route compute tasks" in index

    def test_build_index_no_description_skill(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        index = loader.build_index()
        # bare-skill has no description — should just show the name
        assert "**bare-skill**" in index

    def test_get_bodies_filters_by_name(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        bodies = loader.get_bodies(["compute-routing"])
        assert "compute-routing" in bodies
        assert "bare-skill" not in bodies
        assert "Haiku" in bodies["compute-routing"]

    def test_lazy_scan_on_first_access(self, skill_workspace):
        loader = SkillLoader(skill_workspace)
        assert not loader._loaded
        # Accessing skills triggers scan
        names = loader.list_skill_names()
        assert loader._loaded
        assert len(names) == 2
