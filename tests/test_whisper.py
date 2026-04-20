"""Tests for whisper.py — Whisper STT plugin."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins import (
    PluginEmptyOutput,
    PluginError,
    PluginNotConfigured,
    PluginTransient,
    PluginUpstream,
)

# ─── Import the plugin as a module ──────────────────────────────

_root = Path(__file__).parent.parent
_plugin_path = _root / "plugins.d" / "whisper.py"
if "whisper_plugin" not in sys.modules and _plugin_path.exists():
    _spec = importlib.util.spec_from_file_location("whisper_plugin", _plugin_path)
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["whisper_plugin"] = _mod
    _spec.loader.exec_module(_mod)

import whisper_plugin  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_globals() -> None:  # type: ignore[misc]
    """Reset plugin globals between tests."""
    whisper_plugin._backend = ""
    whisper_plugin._client = None
    whisper_plugin._api_key = ""
    whisper_plugin._api_url = ""
    whisper_plugin._model = "whisper-1"
    whisper_plugin._timeout = 60
    whisper_plugin._retries = 2
    whisper_plugin._local_endpoint = ""
    whisper_plugin._local_language = "auto"
    whisper_plugin._local_ffmpeg_timeout = 30
    whisper_plugin._local_request_timeout = 60
    whisper_plugin._cost_per_minute = 0.0
    whisper_plugin._cost_currency = "USD"
    whisper_plugin._metering = None
    whisper_plugin._converter = None
    yield


@pytest.fixture
def audio_file(tmp_path: Path) -> str:
    """Create a minimal audio file for testing."""
    audio = tmp_path / "test_audio.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 100)
    return str(audio)


@pytest.fixture
def mock_metering() -> MagicMock:
    m = MagicMock()
    m.record = AsyncMock(return_value=0.001)
    return m


@pytest.fixture
def toml_file(tmp_path: Path) -> Path:
    """Write a minimal whisper.toml and return its path."""
    toml = tmp_path / "whisper.toml"
    toml.write_text("""\
backend = "openai"

[openai]
api_key_env = "TEST_WHISPER_KEY"
api_url = "https://api.openai.com/v1/audio/transcriptions"
model = "whisper-1"
timeout = 30
retries = 1

[local]
endpoint = "http://whisper:8082/inference"
language = "de"
ffmpeg_timeout = 15
request_timeout = 45

[cost]
per_minute = 0.006
currency = "USD"
""")
    return toml


# ─── Configure tests ────────────────────────────────────────────


class TestWhisperConfigure:
    def test_configure_loads_toml(
        self, toml_file: Path, mock_metering: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_WHISPER_KEY", "sk-test-123")
        with patch.object(whisper_plugin, "__file__", str(toml_file.parent / "whisper.py")), \
             patch.object(whisper_plugin, "openai_sdk") as mock_sdk:
            mock_sdk.AsyncOpenAI = MagicMock(return_value=MagicMock())
            whisper_plugin.configure(metering=mock_metering)

        assert whisper_plugin._backend == "openai"
        assert whisper_plugin._model == "whisper-1"
        assert whisper_plugin._timeout == 30
        assert whisper_plugin._retries == 1
        assert whisper_plugin._local_endpoint == "http://whisper:8082/inference"
        assert whisper_plugin._local_language == "de"
        assert whisper_plugin._cost_per_minute == 0.006
        assert whisper_plugin._metering is mock_metering

    def test_configure_missing_toml_is_inert(self, tmp_path: Path) -> None:
        with patch.object(whisper_plugin, "__file__", str(tmp_path / "whisper.py")):
            whisper_plugin.configure()
        assert whisper_plugin._backend == ""
        assert whisper_plugin._client is None

    def test_configure_creates_async_openai_client(
        self, toml_file: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_WHISPER_KEY", "sk-test-123")
        mock_cls = MagicMock(return_value=MagicMock(name="async_client"))
        with patch.object(whisper_plugin, "__file__", str(toml_file.parent / "whisper.py")), \
             patch.object(whisper_plugin, "openai_sdk") as mock_sdk:
            mock_sdk.AsyncOpenAI = mock_cls
            whisper_plugin.configure()
        mock_cls.assert_called_once()
        assert whisper_plugin._client is not None

    def test_configure_local_validates_ffmpeg(
        self, tmp_path: Path,
    ) -> None:
        toml = tmp_path / "whisper.toml"
        toml.write_text('backend = "local"\n')
        with patch.object(whisper_plugin, "__file__", str(tmp_path / "whisper.py")), \
             patch.object(whisper_plugin, "_validate_ffmpeg") as mock_validate:
            whisper_plugin.configure()
        mock_validate.assert_called_once()

    def test_configure_sdk_missing_logs_warning(
        self, toml_file: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_WHISPER_KEY", "sk-test-123")
        with patch.object(whisper_plugin, "__file__", str(toml_file.parent / "whisper.py")), \
             patch.object(whisper_plugin, "openai_sdk", None):
            whisper_plugin.configure()
        assert whisper_plugin._client is None


# ─── Backend dispatch ────────────────────────────────────────────


class TestBackendDispatch:
    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self, audio_file: str) -> None:
        whisper_plugin._backend = "nonexistent"
        with pytest.raises(PluginNotConfigured, match="Unknown backend"):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_empty_backend_raises(self, audio_file: str) -> None:
        whisper_plugin._backend = ""
        with pytest.raises(PluginNotConfigured, match="Unknown backend"):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")


# ─── OpenAI backend ─────────────────────────────────────────────


class TestOpenAIBackend:
    @pytest.mark.asyncio
    async def test_successful_transcription(self, audio_file: str) -> None:
        whisper_plugin._backend = "openai"
        mock_response = MagicMock()
        mock_response.text = "Hello, how are you?"
        mock_response.duration = 3.5

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            return_value=mock_response,
        )
        whisper_plugin._client = mock_client

        result = await whisper_plugin.transcribe(audio_file, "audio/ogg")

        assert result == "Hello, how are you?"
        mock_client.audio.transcriptions.create.assert_called_once()
        call_kwargs = mock_client.audio.transcriptions.create.call_args[1]
        assert call_kwargs["model"] == "whisper-1"
        assert call_kwargs["response_format"] == "verbose_json"

    @pytest.mark.asyncio
    async def test_sdk_not_installed_raises(self, audio_file: str) -> None:
        whisper_plugin._backend = "openai"
        whisper_plugin._client = None
        with patch.object(whisper_plugin, "openai_sdk", None):
            with pytest.raises(PluginNotConfigured, match="not initialized"):
                await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_client_not_configured_raises(self, audio_file: str) -> None:
        whisper_plugin._backend = "openai"
        whisper_plugin._client = None
        with pytest.raises(PluginNotConfigured, match="not initialized"):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_empty_text_raises(self, audio_file: str) -> None:
        whisper_plugin._backend = "openai"
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.duration = 1.0

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            return_value=mock_response,
        )
        whisper_plugin._client = mock_client

        with pytest.raises(PluginEmptyOutput, match="empty transcription"):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_unexpected_error_propagates(self, audio_file: str) -> None:
        """Non-SDK RuntimeError propagates — framework wrapper only catches PluginError."""
        whisper_plugin._backend = "openai"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            side_effect=RuntimeError("unexpected internal error"),
        )
        whisper_plugin._client = mock_client

        with pytest.raises(RuntimeError, match="unexpected internal error"):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")


# ─── Local backend ──────────────────────────────────────────────


class TestLocalBackend:
    @pytest.mark.asyncio
    async def test_successful_transcription(self, audio_file: str) -> None:
        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"
        whisper_plugin._local_language = "de"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "Guten Morgen"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run") as mock_ffmpeg, \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await whisper_plugin.transcribe(audio_file, "audio/ogg")

        assert result == "Guten Morgen"
        mock_ffmpeg.assert_called_once()
        ffmpeg_args = mock_ffmpeg.call_args[0][0]
        assert ffmpeg_args[0] == "ffmpeg"
        assert "-ar" in ffmpeg_args
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://whisper:8082/inference"
        assert call_args[1]["data"]["language"] == "de"

    @pytest.mark.asyncio
    async def test_ffmpeg_failure_raises(self, audio_file: str) -> None:
        import subprocess

        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"

        with patch("subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with pytest.raises(PluginUpstream, match="ffmpeg failed"):
                await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_ffmpeg_timeout_raises(self, audio_file: str) -> None:
        import subprocess

        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"

        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with pytest.raises(PluginTransient, match="ffmpeg timed out"):
                await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_whisper_http_error_raises_upstream(self, audio_file: str) -> None:
        """5xx response from local whisper is translated to PluginUpstream."""
        import httpx

        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"

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
             patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PluginUpstream, match="HTTP 500"):
                await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_empty_text_raises(self, audio_file: str) -> None:
        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"text": ""})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(PluginEmptyOutput, match="empty transcription"):
                await whisper_plugin.transcribe(audio_file, "audio/ogg")

    @pytest.mark.asyncio
    async def test_wav_cleanup_on_success(self, audio_file: str) -> None:
        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "test"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        wav_paths: list[str] = []
        original_mkstemp = __import__("tempfile").mkstemp

        def track_mkstemp(**kwargs: object) -> tuple[int, str]:
            fd, path = original_mkstemp(**kwargs)
            wav_paths.append(path)
            return fd, path

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("tempfile.mkstemp", side_effect=track_mkstemp):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

        assert len(wav_paths) == 1
        assert not Path(wav_paths[0]).exists()

    @pytest.mark.asyncio
    async def test_wav_cleanup_on_error(self, audio_file: str) -> None:
        import httpx

        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"

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

        wav_paths: list[str] = []
        original_mkstemp = __import__("tempfile").mkstemp

        def track_mkstemp(**kwargs: object) -> tuple[int, str]:
            fd, path = original_mkstemp(**kwargs)
            wav_paths.append(path)
            return fd, path

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("tempfile.mkstemp", side_effect=track_mkstemp), \
             patch("plugins.asyncio.sleep", new=AsyncMock()), \
             pytest.raises(PluginUpstream):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

        # Framework may retry retryable errors; every attempt must clean up.
        assert wav_paths, "expected at least one temp wav"
        for path in wav_paths:
            assert not Path(path).exists(), f"{path} was not cleaned up"
        return

        assert len(wav_paths) == 1
        assert not Path(wav_paths[0]).exists()


# ─── Cost tracking ───────────────────────────��──────────────────


class TestWhisperCostTracking:
    @pytest.mark.asyncio
    async def test_cost_recorded_on_openai_success(
        self, audio_file: str, mock_metering: MagicMock,
    ) -> None:
        whisper_plugin._backend = "openai"
        whisper_plugin._metering = mock_metering
        whisper_plugin._cost_per_minute = 0.006

        mock_response = MagicMock()
        mock_response.text = "Hello"
        mock_response.duration = 30.0  # 30 seconds = 0.5 min → $0.003

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            return_value=mock_response,
        )
        whisper_plugin._client = mock_client

        await whisper_plugin.transcribe(audio_file, "audio/ogg")

        mock_metering.record.assert_called_once()
        call_kwargs = mock_metering.record.call_args[1]
        assert call_kwargs["provider"] == "openai"
        assert call_kwargs["call_type"] == "transcription"
        assert abs(call_kwargs["cost_override"] - 0.003) < 0.0001

    @pytest.mark.asyncio
    async def test_no_cost_for_local_backend(
        self, audio_file: str, mock_metering: MagicMock,
    ) -> None:
        whisper_plugin._backend = "local"
        whisper_plugin._local_endpoint = "http://whisper:8082/inference"
        whisper_plugin._metering = mock_metering

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"text": "test"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("subprocess.run"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

        mock_metering.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cost_on_failure(
        self, audio_file: str, mock_metering: MagicMock,
    ) -> None:
        whisper_plugin._backend = "openai"
        whisper_plugin._metering = mock_metering
        whisper_plugin._cost_per_minute = 0.006

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            side_effect=RuntimeError("API error"),
        )
        whisper_plugin._client = mock_client

        with pytest.raises(RuntimeError):
            await whisper_plugin.transcribe(audio_file, "audio/ogg")

        mock_metering.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_crash_without_metering(self, audio_file: str) -> None:
        whisper_plugin._backend = "openai"
        whisper_plugin._metering = None
        whisper_plugin._cost_per_minute = 0.006

        mock_response = MagicMock()
        mock_response.text = "Hello"
        mock_response.duration = 5.0

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            return_value=mock_response,
        )
        whisper_plugin._client = mock_client

        result = await whisper_plugin.transcribe(audio_file, "audio/ogg")
        assert result == "Hello"


# ─── Preprocessor ───────────────────────────────────────────────


class TestPreprocessAudio:
    @pytest.mark.asyncio
    async def test_audio_attachment_transcribed(self) -> None:
        whisper_plugin._backend = "openai"

        att = MagicMock()
        att.content_type = "audio/ogg"
        att.is_voice = True
        att.local_path = "/tmp/test.ogg"

        with patch.object(whisper_plugin, "transcribe",
                          new=AsyncMock(return_value="Hello world")):
            text, remaining = await whisper_plugin.preprocess_audio(
                "", [att], MagicMock(),
            )

        assert "Hello world" in text
        assert "voice message" in text
        assert remaining == []

    @pytest.mark.asyncio
    async def test_non_audio_passes_through(self) -> None:
        whisper_plugin._backend = "openai"

        att = MagicMock()
        att.content_type = "image/png"

        text, remaining = await whisper_plugin.preprocess_audio(
            "original", [att], MagicMock(),
        )

        assert text == "original"
        assert remaining == [att]

    @pytest.mark.asyncio
    async def test_inactive_backend_passes_through(self) -> None:
        whisper_plugin._backend = ""

        att = MagicMock()
        att.content_type = "audio/ogg"

        text, remaining = await whisper_plugin.preprocess_audio(
            "original", [att], MagicMock(),
        )

        assert text == "original"
        assert remaining == [att]

    @pytest.mark.asyncio
    async def test_transcription_failure_propagates(self) -> None:
        """Preprocessor propagates PluginError; the dispatch layer owns fallback_text."""
        whisper_plugin._backend = "openai"

        att = MagicMock()
        att.content_type = "audio/ogg"
        att.is_voice = False
        att.local_path = "/tmp/test.ogg"

        with patch.object(whisper_plugin, "transcribe",
                          new=AsyncMock(side_effect=PluginUpstream("boom"))):
            with pytest.raises(PluginUpstream, match="boom"):
                await whisper_plugin.preprocess_audio(
                    "", [att], MagicMock(),
                )
