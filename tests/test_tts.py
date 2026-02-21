"""Tests for tools/tts.py — TTS output file permissions and path validation."""

from unittest.mock import MagicMock, patch

import pytest

import tools.filesystem as fs_mod
from tools.tts import configure, tool_tts


@pytest.fixture(autouse=True)
def reset_tts_state():
    """Reset module state between tests."""
    import tools.tts as mod
    original = (mod._api_key, mod._provider, mod._output_dir, mod._channel,
                mod._default_voice_id, mod._default_model_id)
    yield
    mod._api_key, mod._provider, mod._output_dir, mod._channel, \
        mod._default_voice_id, mod._default_model_id = original


class TestTTSPermissions:
    """SEC-11: TTS output file permissions."""

    @pytest.mark.asyncio
    async def test_tts_output_not_world_readable(self, tmp_path):
        """Generated TTS files should have 0o600 permissions."""
        configure(
            api_key="test-key",
            provider="elevenlabs",
            output_dir=str(tmp_path),
            default_voice_id="test-voice",
        )

        # Mock the HTTP call to return fake audio data
        mock_response = MagicMock()
        mock_response.read.return_value = b"fake audio data"

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = await tool_tts("Hello world")

        assert "Audio saved to" in result
        # Find the generated file
        files = list(tmp_path.glob("lucyd-tts-*"))
        assert len(files) == 1
        mode = oct(files[0].stat().st_mode & 0o777)
        assert mode == "0o600"


class TestTTSOutputPathValidation:
    """TTS output_file must be validated against filesystem allowlist."""

    @pytest.fixture(autouse=True)
    def setup_filesystem(self, tmp_path):
        """Configure filesystem allowlist for tests."""
        self.allowed_dir = tmp_path / "allowed"
        self.allowed_dir.mkdir()
        fs_mod.configure(allowed_paths=[str(self.allowed_dir)])
        yield
        fs_mod.configure(allowed_paths=[])

    @pytest.mark.asyncio
    async def test_rejects_output_path_outside_allowlist(self):
        """output_file outside filesystem allowlist must be rejected."""
        configure(api_key="test-key", provider="elevenlabs",
                  default_voice_id="test-voice")
        result = await tool_tts("Hello", output_file="/etc/evil.mp3")
        assert "Error" in result
        assert "not allowed" in result.lower()

    @pytest.mark.asyncio
    async def test_rejects_traversal_in_output_path(self):
        """Path traversal in output_file must be blocked."""
        configure(api_key="test-key", provider="elevenlabs",
                  default_voice_id="test-voice")
        evil_path = str(self.allowed_dir / "../../etc/evil.mp3")
        result = await tool_tts("Hello", output_file=evil_path)
        assert "Error" in result
        assert "not allowed" in result.lower()

    @pytest.mark.asyncio
    async def test_allows_output_path_within_allowlist(self):
        """output_file within filesystem allowlist should proceed to API call."""
        configure(api_key="test-key", provider="elevenlabs",
                  default_voice_id="test-voice")
        allowed_path = str(self.allowed_dir / "output.mp3")

        mock_response = MagicMock()
        mock_response.read.return_value = b"fake audio"

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = await tool_tts("Hello", output_file=allowed_path)

        assert "Audio saved to" in result

    @pytest.mark.asyncio
    async def test_default_temp_file_not_checked(self, tmp_path):
        """Default (empty output_file) uses mkstemp, bypasses _check_path."""
        configure(api_key="test-key", provider="elevenlabs",
                  output_dir=str(tmp_path), default_voice_id="test-voice")

        mock_response = MagicMock()
        mock_response.read.return_value = b"fake audio"

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = await tool_tts("Hello")

        # Should succeed even though /tmp may not be in allowlist
        assert "Audio saved to" in result
