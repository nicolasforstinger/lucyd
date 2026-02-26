"""Tests for shell tool environment filtering and process safety.

Phase 1b: Shell Secret Filtering — tools/shell.py
Tests _safe_env (per-suffix), tool_exec integration (leak, timeout, cap).
"""

import signal
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestExecOutputFormatting:
    """Output assembly: stdout, stderr, exit code, empty output."""

    @pytest.mark.asyncio
    async def test_stdout_only(self):
        result = await tool_exec("echo hello")
        assert "hello" in result
        assert "STDERR" not in result

    @pytest.mark.asyncio
    async def test_stderr_only(self):
        result = await tool_exec("echo error >&2")
        assert "STDERR:" in result
        assert "error" in result

    @pytest.mark.asyncio
    async def test_combined_stdout_stderr(self):
        result = await tool_exec("echo out && echo err >&2")
        assert "out" in result
        assert "STDERR:" in result
        assert "err" in result

    @pytest.mark.asyncio
    async def test_non_zero_exit_code_shown(self):
        result = await tool_exec("exit 42")
        assert "[exit code: 42]" in result

    @pytest.mark.asyncio
    async def test_zero_exit_code_not_shown(self):
        result = await tool_exec("echo ok")
        assert "exit code" not in result

    @pytest.mark.asyncio
    async def test_empty_output_returns_no_output(self):
        result = await tool_exec("true")
        assert result == "(no output)"


# ─── Mock-based kill chain tests ─────────────────────────────────


def _make_mock_proc(pid=12345, returncode=0, stdout=b"", stderr=b""):
    """Create a mock subprocess with configurable outputs."""
    proc = AsyncMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock()
    # kill() is synchronous on asyncio.subprocess.Process — use MagicMock
    proc.kill = MagicMock()
    return proc


class TestExecKillChain:
    """Mock-based tests for timeout kill chain and exception handling."""

    @pytest.mark.asyncio
    async def test_timeout_calls_killpg(self):
        """On timeout, os.killpg is called with process group and SIGKILL."""
        proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg") as mock_killpg:
            result = await tool_exec("test_cmd", timeout=10)
        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_killpg_fail_falls_back_to_proc_kill(self):
        """When killpg raises, falls back to proc.kill()."""
        proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg", side_effect=OSError("No such process")):
            result = await tool_exec("test_cmd", timeout=10)
        proc.kill.assert_called_once()
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_proc_kill_fail_still_returns_timeout(self):
        """When both killpg and proc.kill() fail, still returns timeout message."""
        proc = _make_mock_proc()
        proc.kill = MagicMock(side_effect=OSError("already dead"))
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg", side_effect=OSError("No such process")):
            result = await tool_exec("test_cmd", timeout=10)
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_killpg_success_does_not_call_proc_kill(self):
        """When killpg succeeds, proc.kill() is NOT called."""
        proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg"):
            await tool_exec("test_cmd", timeout=10)
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_waits_for_process_after_killpg(self):
        """After killpg, proc.wait() is called to reap the process."""
        proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg"):
            await tool_exec("test_cmd", timeout=10)
        proc.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_waits_for_process_after_proc_kill(self):
        """After proc.kill() fallback, proc.wait() is called to reap."""
        proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg", side_effect=OSError):
            await tool_exec("test_cmd", timeout=10)
        # killpg raised before its proc.wait(), so wait() called once: after proc.kill()
        proc.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_message_includes_timeout_value(self):
        """Timeout message includes the actual timeout seconds."""
        proc = _make_mock_proc()
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg"):
            result = await tool_exec("test_cmd", timeout=42)
        assert "42s" in result


class TestExecExceptionHandling:
    """Mock-based tests for non-timeout exception paths."""

    @pytest.mark.asyncio
    async def test_generic_exception_returns_error_string(self):
        """Non-timeout exception returns error with exception type name."""
        with patch("asyncio.create_subprocess_shell", side_effect=OSError("bad")):
            result = await tool_exec("test_cmd")
        assert "OSError" in result

    @pytest.mark.asyncio
    async def test_generic_exception_format(self):
        """Error format starts with 'Error: Command execution failed:'."""
        with patch("asyncio.create_subprocess_shell", side_effect=PermissionError("denied")):
            result = await tool_exec("test_cmd")
        assert result == "Error: Command execution failed: PermissionError"


class TestExecOutputEdgeCases:
    """Mock-based tests for output assembly edge cases."""

    @pytest.mark.asyncio
    async def test_stdout_none_treated_as_empty(self):
        """When stdout is None, no crash and no 'None' in output."""
        proc = _make_mock_proc(stdout=None, stderr=b"err")
        proc.communicate = AsyncMock(return_value=(None, b"err"))
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", return_value=(None, b"err")):
            result = await tool_exec("test_cmd")
        assert "None" not in result

    @pytest.mark.asyncio
    async def test_stderr_none_treated_as_empty(self):
        """When stderr is None, no crash."""
        proc = _make_mock_proc(stdout=b"out", stderr=None)
        proc.communicate = AsyncMock(return_value=(b"out", None))
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", return_value=(b"out", None)):
            result = await tool_exec("test_cmd")
        assert "out" in result
        assert "STDERR" not in result

    @pytest.mark.asyncio
    async def test_negative_exit_code_shown(self):
        """Negative exit code (e.g., signal kill) shown in output."""
        proc = _make_mock_proc(returncode=-9, stdout=b"")
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", return_value=(b"", b"")):
            result = await tool_exec("test_cmd")
        assert "[exit code: -9]" in result

    @pytest.mark.asyncio
    async def test_utf8_replace_on_invalid_bytes(self):
        """Invalid UTF-8 bytes are replaced, not raised."""
        proc = _make_mock_proc(stdout=b"hello\xffworld")
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", return_value=(b"hello\xffworld", b"")):
            result = await tool_exec("test_cmd")
        assert "hello" in result
        assert "world" in result

    @pytest.mark.asyncio
    async def test_stdout_with_stderr_separator(self):
        """When both stdout and stderr present, separated by newline + STDERR:."""
        proc = _make_mock_proc(stdout=b"out", stderr=b"err")
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", return_value=(b"out", b"err")):
            result = await tool_exec("test_cmd")
        assert "out\nSTDERR:\nerr" in result


class TestExecTimeoutCapping:
    """Mock-based tests for timeout parameter handling."""

    @pytest.mark.asyncio
    async def test_default_timeout_used_when_none(self, monkeypatch):
        """When timeout is None, _DEFAULT_TIMEOUT is passed to wait_for."""
        import tools.shell as shell_mod
        monkeypatch.setattr(shell_mod, "_DEFAULT_TIMEOUT", 99)
        monkeypatch.setattr(shell_mod, "_MAX_TIMEOUT", 999)
        proc = _make_mock_proc(stdout=b"ok")
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", return_value=(b"ok", b"")) as mock_wait:
            await tool_exec("test_cmd")
        mock_wait.assert_called_once()
        assert mock_wait.call_args[1].get("timeout", mock_wait.call_args[0][1] if len(mock_wait.call_args[0]) > 1 else None) == 99

    @pytest.mark.asyncio
    async def test_explicit_timeout_capped_at_max(self, monkeypatch):
        """Explicit timeout > _MAX_TIMEOUT is capped."""
        import tools.shell as shell_mod
        monkeypatch.setattr(shell_mod, "_MAX_TIMEOUT", 100)
        proc = _make_mock_proc(stdout=b"ok")
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg"):
            result = await tool_exec("test_cmd", timeout=9999)
        assert "100s" in result  # Capped to _MAX_TIMEOUT

    @pytest.mark.asyncio
    async def test_explicit_timeout_below_max_used(self, monkeypatch):
        """Explicit timeout < _MAX_TIMEOUT is used as-is."""
        import tools.shell as shell_mod
        monkeypatch.setattr(shell_mod, "_MAX_TIMEOUT", 999)
        proc = _make_mock_proc(stdout=b"ok")
        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=TimeoutError), \
             patch("os.killpg"):
            result = await tool_exec("test_cmd", timeout=5)
        assert "5s" in result
