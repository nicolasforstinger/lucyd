"""Tests for shell tool environment filtering and process safety.

Phase 1b: Shell Secret Filtering — tools/shell.py
Tests _safe_env (per-suffix), tool_exec integration (leak, timeout, cap).
"""

import pytest

from tools.shell import _SECRET_PREFIXES, _SECRET_SUFFIXES, _safe_env, tool_exec

# ─── _safe_env — per-suffix unit tests ───────────────────────────


class TestSafeEnvPrefix:
    """Each prefix type individually tested.
    IMPORTANT: var names must NOT match any suffix pattern,
    so we isolate the prefix check."""

    def test_filters_lucyd_prefix(self, monkeypatch):
        """LUCYD_CUSTOM_SETTING filtered by LUCYD_ prefix alone."""
        monkeypatch.setenv("LUCYD_CUSTOM_SETTING", "sk-secret")
        env = _safe_env()
        assert "LUCYD_CUSTOM_SETTING" not in env

    def test_filters_second_lucyd_var(self, monkeypatch):
        """LUCYD_BRAVE_ENDPOINT filtered by LUCYD_ prefix alone."""
        monkeypatch.setenv("LUCYD_BRAVE_ENDPOINT", "https://api.brave.com")
        env = _safe_env()
        assert "LUCYD_BRAVE_ENDPOINT" not in env


class TestSafeEnvSuffix:
    """Each suffix type individually tested — one test per suffix."""

    def test_filters_key_suffix(self, monkeypatch):
        """AWS_ACCESS_KEY_ID ends with _ID."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        env = _safe_env()
        assert "AWS_ACCESS_KEY_ID" not in env

    def test_filters_token_suffix(self, monkeypatch):
        """GITHUB_TOKEN ends with _TOKEN."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc123")
        env = _safe_env()
        assert "GITHUB_TOKEN" not in env

    def test_filters_secret_suffix(self, monkeypatch):
        """DATABASE_SECRET ends with _SECRET."""
        monkeypatch.setenv("DATABASE_SECRET", "s3cr3t")
        env = _safe_env()
        assert "DATABASE_SECRET" not in env

    def test_filters_password_suffix(self, monkeypatch):
        """SMTP_PASSWORD ends with _PASSWORD."""
        monkeypatch.setenv("SMTP_PASSWORD", "p@ssw0rd")
        env = _safe_env()
        assert "SMTP_PASSWORD" not in env

    def test_filters_credentials_suffix(self, monkeypatch):
        """GCP_CREDENTIALS ends with _CREDENTIALS."""
        monkeypatch.setenv("GCP_CREDENTIALS", '{"type":"service_account"}')
        env = _safe_env()
        assert "GCP_CREDENTIALS" not in env

    def test_filters_code_suffix(self, monkeypatch):
        """GOOGLE_OAUTH_CODE ends with _CODE."""
        monkeypatch.setenv("GOOGLE_OAUTH_CODE", "4/0AfJohXn...")
        env = _safe_env()
        assert "GOOGLE_OAUTH_CODE" not in env

    def test_filters_pass_suffix(self, monkeypatch):
        """DB_PASS ends with _PASS."""
        monkeypatch.setenv("DB_PASS", "p@ss")
        env = _safe_env()
        assert "DB_PASS" not in env

    def test_filters_id_suffix(self, monkeypatch):
        """CLIENT_ID ends with _ID."""
        monkeypatch.setenv("CLIENT_ID", "abc123")
        env = _safe_env()
        assert "CLIENT_ID" not in env


class TestSafeEnvPreserves:
    """Positive tests: safe vars pass through."""

    def test_preserves_path(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = _safe_env()
        assert "PATH" in env
        assert env["PATH"] == "/usr/bin:/bin"

    def test_preserves_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        env = _safe_env()
        assert "HOME" in env
        assert env["HOME"] == "/home/testuser"

    def test_preserves_editor(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "vim")
        env = _safe_env()
        assert env.get("EDITOR") == "vim"

    def test_preserves_lang(self, monkeypatch):
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        env = _safe_env()
        assert env.get("LANG") == "en_US.UTF-8"


class TestSafeEnvCombined:
    """Combined prefix+suffix filtering."""

    def test_filters_combined_both_paths(self, monkeypatch):
        """LUCYD_BRAVE_KEY matches both LUCYD_ prefix and _KEY suffix.
        Also test a prefix-only var to ensure both paths work."""
        monkeypatch.setenv("LUCYD_BRAVE_KEY", "secret")
        monkeypatch.setenv("LUCYD_EXTRA_SETTING", "also-secret")
        env = _safe_env()
        assert "LUCYD_BRAVE_KEY" not in env
        assert "LUCYD_EXTRA_SETTING" not in env


class TestSafeEnvIterationOrder:
    """Verify _safe_env skips (not breaks) on secret matches.
    Variables added via setenv append in insertion order (CPython 3.7+),
    so a safe var added AFTER a secret var detects continue→break mutations."""

    def test_safe_var_after_prefix_match(self, monkeypatch):
        """Safe var added after a prefix-matched secret must be preserved."""
        monkeypatch.setenv("LUCYD_HIDDEN", "secret")
        monkeypatch.setenv("SAFE_UNIQUE_MUTCHECK_PFX", "visible")
        env = _safe_env()
        assert "LUCYD_HIDDEN" not in env
        assert "SAFE_UNIQUE_MUTCHECK_PFX" in env

    def test_safe_var_after_suffix_match(self, monkeypatch):
        """Safe var added after a suffix-matched secret must be preserved."""
        monkeypatch.setenv("MY_API_TOKEN", "secret")
        monkeypatch.setenv("SAFE_UNIQUE_MUTCHECK_SFX", "visible")
        env = _safe_env()
        assert "MY_API_TOKEN" not in env
        assert "SAFE_UNIQUE_MUTCHECK_SFX" in env


class TestSecretPatterns:
    """Verify the pattern lists are correct."""

    def test_prefixes(self):
        assert "LUCYD_" in _SECRET_PREFIXES

    def test_suffixes(self):
        assert "_KEY" in _SECRET_SUFFIXES
        assert "_TOKEN" in _SECRET_SUFFIXES
        assert "_SECRET" in _SECRET_SUFFIXES
        assert "_PASSWORD" in _SECRET_SUFFIXES
        assert "_CREDENTIALS" in _SECRET_SUFFIXES
        assert "_ID" in _SECRET_SUFFIXES
        assert "_CODE" in _SECRET_SUFFIXES
        assert "_PASS" in _SECRET_SUFFIXES


# ─── tool_exec — integration tests ──────────────────────────────


class TestExecSecretLeaking:
    """Integration: tool_exec uses _safe_env() for subprocess environment."""

    @pytest.mark.asyncio
    async def test_exec_does_not_leak_lucyd_prefix(self, monkeypatch):
        """Set LUCYD_CUSTOM_SETTING (no suffix match), run `env`, assert filtered by prefix."""
        monkeypatch.setenv("LUCYD_CUSTOM_SETTING", "super-secret-value-12345")
        result = await tool_exec("env")
        assert "super-secret-value-12345" not in result
        assert "LUCYD_CUSTOM_SETTING" not in result

    @pytest.mark.asyncio
    async def test_exec_does_not_leak_token_suffix(self, monkeypatch):
        """Set MY_API_TOKEN, run `env`, assert not leaked."""
        monkeypatch.setenv("MY_API_TOKEN", "tok-secret-67890")
        result = await tool_exec("env")
        assert "tok-secret-67890" not in result
        assert "MY_API_TOKEN" not in result

    @pytest.mark.asyncio
    async def test_exec_preserves_path_in_env(self, monkeypatch):
        """PATH should be available in subprocess."""
        result = await tool_exec("echo $PATH")
        # PATH should exist and be non-empty
        assert result.strip() != ""


class TestExecTimeout:
    """Timeout enforcement in tool_exec."""

    @pytest.mark.asyncio
    async def test_exec_timeout_kills_command(self, monkeypatch):
        """Monkeypatch _MAX_TIMEOUT to 2, run sleep 30, assert timeout."""
        import tools.shell as shell_mod
        monkeypatch.setattr(shell_mod, "_MAX_TIMEOUT", 2)
        monkeypatch.setattr(shell_mod, "_DEFAULT_TIMEOUT", 2)
        result = await tool_exec("sleep 30")
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_exec_timeout_cap_applied(self, monkeypatch):
        """Request timeout=3600, but _MAX_TIMEOUT=2 caps it. Command still times out."""
        import tools.shell as shell_mod
        monkeypatch.setattr(shell_mod, "_MAX_TIMEOUT", 2)
        result = await tool_exec("sleep 30", timeout=3600)
        assert "timed out" in result.lower()
