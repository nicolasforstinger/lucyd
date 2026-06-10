"""Config schema coverage — closes the mypy blind spot on Config.__getattr__.

Config resolves all _SCHEMA keys via __getattr__, so `config.<key>` is `Any`
to mypy --strict: a renamed/removed/typo'd key sails past type checking and
only raises AttributeError at runtime (this is how the recall_max_dynamic_tokens
default divergence slipped in). These tests are the type checker mypy can't be
for this surface:

- test_no_dead_schema_keys: every _SCHEMA key is read somewhere in production
  source (a key nothing reads is dead config).
- test_no_phantom_config_access: every attribute access on a value known to be
  a Config names a real schema key, @property, or method (catches typos/renames
  in callers). Conservative by design — it only flags accesses it can prove are
  Config (annotated params, self.config / self._config, and aliases assigned
  directly from those), so a LoopConfig `cfg` is never misread as Config.
"""

from __future__ import annotations

import ast
from pathlib import Path

_LUCYD_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PY = _LUCYD_DIR / "config.py"

# Production source: repo-root modules + tools/ providers/ channels/ plugins.d/,
# excluding tests and the build/ copy.
_PROD_DIRS = ["tools", "providers", "channels", "plugins.d"]

# Keys read only through dynamic access (no literal `.key` token to find), so the
# static reader scan can't see them. Each is genuinely live — keep the comment
# current if this set changes.
#   *_model: lucyd.get_provider() reads `getattr(config, f"{role}_model")`.
_DYNAMIC_ACCESS = frozenset({
    "compaction_model", "consolidation_model", "subagent_model",
})


def _prod_files() -> list[Path]:
    files = [p for p in _LUCYD_DIR.glob("*.py")]
    for d in _PROD_DIRS:
        files.extend((_LUCYD_DIR / d).glob("*.py"))
    return [f for f in files if "build/" not in str(f)]


def _config_tree() -> ast.Module:
    return ast.parse(_CONFIG_PY.read_text(encoding="utf-8"))


def _schema_keys() -> set[str]:
    """Extract the _SCHEMA dict literal keys from config.py via AST."""
    for node in ast.walk(_config_tree()):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "_SCHEMA" and isinstance(node.value, ast.Dict):
            return {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError("could not locate the _SCHEMA dict literal in config.py")


def _config_member_names() -> set[str]:
    """Every attribute a Config legitimately exposes: schema keys + class members."""
    members: set[str] = set(_schema_keys())
    for node in ast.walk(_config_tree()):
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.add(item.name)
    # Instance attributes set in __init__ / _validate (not callables, not schema).
    members.update({"data_dir", "_data", "_values", "_config_dir",
                    "_explicit_keys", "_data_dir"})
    return members


def _is_config_annotation(ann: ast.expr | None) -> bool:
    """True if a parameter annotation resolves to Config (never LoopConfig etc.)."""
    if ann is None:
        return False
    # `Config`, `Config | None`, `"Config"`, `Optional[Config]`
    names: list[str] = []
    for n in ast.walk(ann):
        if isinstance(n, ast.Name):
            names.append(n.id)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            names.append(n.value)
    return "Config" in names and not any(
        x in names for x in ("LoopConfig", "ModelConfig")
    )


def _config_accesses(tree: ast.Module) -> list[tuple[str, int]]:
    """Find (attr, lineno) for every access provably on a Config value.

    Config-bound names within a function: parameters annotated Config, plus
    locals assigned directly from `self.config` / `self._config` / an already-
    bound config name. `self.config` and `self._config` always count.
    """
    accesses: list[tuple[str, int]] = []

    def _rhs_is_config(value: ast.expr, bound: set[str]) -> bool:
        if isinstance(value, ast.Attribute) and value.attr in ("config", "_config") \
                and isinstance(value.value, ast.Name) and value.value.id == "self":
            return True
        return isinstance(value, ast.Name) and value.id in bound

    def _is_config_value(value: ast.expr, bound: set[str]) -> bool:
        if isinstance(value, ast.Name) and value.id in bound:
            return True
        return (isinstance(value, ast.Attribute) and value.attr in ("config", "_config")
                and isinstance(value.value, ast.Name) and value.value.id == "self")

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound: set[str] = {
            a.arg for a in [*fn.args.args, *fn.args.kwonlyargs]
            if _is_config_annotation(a.annotation)
        }
        for node in ast.walk(fn):
            # Track `x = self.config` / `x = <config-name>` aliases.
            if isinstance(node, ast.Assign) and _rhs_is_config(node.value, bound):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        bound.add(tgt.id)
            if isinstance(node, ast.Attribute) and _is_config_value(node.value, bound):
                if not node.attr.startswith("__"):
                    accesses.append((node.attr, node.lineno))
    return accesses


def test_no_dead_schema_keys() -> None:
    """Every _SCHEMA key must be read somewhere in production source.

    config.py is included in the search: a @property that reads `self.<key>`
    (e.g. resolved_agent_id → self.agent_id) is a legitimate reader. Dynamically
    accessed keys are exempted via _DYNAMIC_ACCESS.
    """
    keys = _schema_keys() - _DYNAMIC_ACCESS
    sources = {f: f.read_text(encoding="utf-8") for f in _prod_files()}
    dead = []
    for key in keys:
        token = f".{key}"
        if not any(token in src for src in sources.values()):
            dead.append(key)
    assert not dead, (
        f"_SCHEMA keys with no production reader (dead config — wire or remove): {sorted(dead)}"
    )


def test_no_phantom_config_access() -> None:
    """Every provable Config attribute access names a real member."""
    valid = _config_member_names()
    phantom: list[str] = []
    for f in _prod_files():
        if f == _CONFIG_PY:
            continue
        for attr, lineno in _config_accesses(ast.parse(f.read_text(encoding="utf-8"))):
            if attr not in valid:
                phantom.append(f"{f.name}:{lineno} config.{attr}")
    assert not phantom, (
        "config attribute access naming no _SCHEMA key / property / method "
        f"(typo or stale rename): {phantom}"
    )
