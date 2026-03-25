"""Tests for stt.py — speech-to-text boundary module."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def audio_file(tmp_path):
    """Create a minimal audio file for testing."""
    audio = tmp_path / "test_audio.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 100)
    return str(audio)


# ─── Backend dispatch ─────────────────────────────────────────────


class TestBackendDispatch:
    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self, audio_file):
        import stt
        with pytest.raises(RuntimeError, match="Unknown STT backend"):
            await stt.transcribe({"backend": "nonexistent"}, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_empty_backend_raises(self, audio_file):
        import stt
        with pytest.raises(RuntimeError, match="Unknown STT backend"):
            await stt.transcribe({}, audio_file, "audio/ogg")


# ─── OpenAI backend ───────────────────────────────────────────────


class TestOpenAIBackend:
    def _make_config(self, api_key_env="STT_KEY", **openai_overrides):
        cfg = {"backend": "openai", "api_key_env": api_key_env}
        if openai_overrides:
            cfg["openai"] = openai_overrides
        return cfg

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, audio_file):
        import stt
        cfg = self._make_config(api_key_env="NONEXISTENT_STT_KEY_12345")
        with pytest.raises(RuntimeError, match="Required STT API key not configured"):
            await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_no_api_key_env_raises(self, audio_file):
        import stt
        cfg = {"backend": "openai"}
        with pytest.raises(RuntimeError, match="Required STT API key not configured"):
            await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_successful_transcription(self, audio_file, monkeypatch):
        import stt
        monkeypatch.setenv("TEST_STT_KEY", "sk-test-key-12345")
        cfg = self._make_config(api_key_env="TEST_STT_KEY")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Hello, how are you?"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await stt.transcribe(cfg, audio_file, "audio/ogg")

        assert result == "Hello, how are you?"
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://api.openai.com/v1/audio/transcriptions"
        assert call_args[1]["headers"]["Authorization"] == "Bearer sk-test-key-12345"

    @pytest.mark.asyncio
    async def test_custom_config(self, audio_file, monkeypatch):
        import stt
        monkeypatch.setenv("TEST_STT_KEY", "sk-custom-key")
        cfg = self._make_config(
            api_key_env="TEST_STT_KEY",
            api_url="https://custom.api/v1/transcribe",
            model="whisper-large-v3",
            timeout=120,
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Custom transcription"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            result = await stt.transcribe(cfg, audio_file, "audio/ogg")

        assert result == "Custom transcription"
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://custom.api/v1/transcribe"
        assert call_args[1]["data"]["model"] == "whisper-large-v3"
        mock_cls.assert_called_once_with(timeout=120)

    @pytest.mark.asyncio
    async def test_api_error_raises(self, audio_file, monkeypatch):
        import httpx
        import stt
        monkeypatch.setenv("TEST_STT_KEY", "sk-test")
        cfg = self._make_config(api_key_env="TEST_STT_KEY")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             pytest.raises(httpx.HTTPStatusError):
            await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_empty_text_raises(self, audio_file, monkeypatch):
        import stt
        monkeypatch.setenv("TEST_STT_KEY", "sk-test")
        cfg = self._make_config(api_key_env="TEST_STT_KEY")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="empty transcription"):
                await stt.transcribe(cfg, audio_file, "audio/ogg")


# ─── Local backend ────────────────────────────────────────────────


class TestLocalBackend:
    def _make_config(self, **overrides):
        local = {
            "endpoint": "http://whisper:8082/inference",
            "language": "de",
            "ffmpeg_timeout": 30,
            "request_timeout": 60,
        }
        local.update(overrides)
        return {"backend": "local", "local": local}

    @pytest.mark.asyncio
    async def test_successful_transcription(self, audio_file):
        import stt
        cfg = self._make_config()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Guten Morgen"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run") as mock_ffmpeg, \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await stt.transcribe(cfg, audio_file, "audio/ogg")

        assert result == "Guten Morgen"
        mock_ffmpeg.assert_called_once()
        ffmpeg_args = mock_ffmpeg.call_args[0][0]
        assert ffmpeg_args[0] == "ffmpeg"
        assert "-ar" in ffmpeg_args
        assert "16000" in ffmpeg_args
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://whisper:8082/inference"
        assert call_args[1]["data"]["language"] == "de"

    @pytest.mark.asyncio
    async def test_ffmpeg_failure_raises(self, audio_file):
        import subprocess
        import stt
        cfg = self._make_config()

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with pytest.raises(subprocess.CalledProcessError):
                await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_ffmpeg_timeout_raises(self, audio_file):
        import subprocess
        import stt
        cfg = self._make_config()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with pytest.raises(subprocess.TimeoutExpired):
                await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_whisper_error_raises(self, audio_file):
        import httpx
        import stt
        cfg = self._make_config()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             pytest.raises(httpx.HTTPStatusError):
            await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_empty_text_raises(self, audio_file):
        import stt
        cfg = self._make_config()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": ""})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="empty transcription"):
                await stt.transcribe(cfg, audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_wav_cleanup_on_success(self, audio_file):
        import stt
        cfg = self._make_config()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "test"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        wav_paths = []
        original_mkstemp = __import__("tempfile").mkstemp

        def track_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            wav_paths.append(path)
            return fd, path

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("tempfile.mkstemp", side_effect=track_mkstemp):
            await stt.transcribe(cfg, audio_file, "audio/ogg")

        assert len(wav_paths) == 1
        assert not Path(wav_paths[0]).exists()

    @pytest.mark.asyncio
    async def test_wav_cleanup_on_error(self, audio_file):
        import httpx
        import stt
        cfg = self._make_config()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Error", request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        wav_paths = []
        original_mkstemp = __import__("tempfile").mkstemp

        def track_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            wav_paths.append(path)
            return fd, path

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("tempfile.mkstemp", side_effect=track_mkstemp), \
             pytest.raises(httpx.HTTPStatusError):
            await stt.transcribe(cfg, audio_file, "audio/ogg")

        assert len(wav_paths) == 1
        assert not Path(wav_paths[0]).exists()
