---
name: example-skill
description: Demonstrates the skill file format
---
# Example Skill

This is the skill body. It's injected into the system prompt when the agent
loads this skill via the `load_skill` tool, or when it's listed in `always_on`.

## Usage

Skills are markdown files with YAML frontmatter in `workspace/skills/<name>/SKILL.md`.

**Frontmatter fields:**
- `name` — Skill name (defaults to directory name if omitted)
- `description` — One-line summary shown in the skill index

**Body** — Full instructions, examples, or rules. Loaded on demand or always-on.
