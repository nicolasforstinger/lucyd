"""Tests for elevenlabs.py — ElevenLabs TTS plugin."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Import the plugin as a module ──────────────────────────────

_root = Path(__file__).parent.parent
_plugin_path = _root / "plugins.d" / "elevenlabs.py"
if "elevenlabs_plugin" not in sys.modules and _plugin_path.exists():
    _spec = importlib.util.spec_from_file_location("elevenlabs_plugin", _plugin_path)
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["elevenlabs_plugin"] = _mod
    _spec.loader.exec_module(_mod)

import elevenlabs_plugin  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset plugin globals between tests."""
    elevenlabs_plugin._client = None
    elevenlabs_plugin._api_url = ""
    elevenlabs_plugin._default_voice_id = ""
    elevenlabs_plugin._default_model_id = ""
    elevenlabs_plugin._timeout = 60
    elevenlabs_plugin._voice_speed = 1.0
    elevenlabs_plugin._voice_stability = 0.5
    elevenlabs_plugin._voice_similarity_boost = 0.75
    elevenlabs_plugin._output_dir = ""
    elevenlabs_plugin._cost_per_1k_chars = 0.0
    elevenlabs_plugin._cost_currency = "USD"
    elevenlabs_plugin._metering = None
    elevenlabs_plugin._converter = None
    yield


@pytest.fixture
def mock_config(tmp_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.http_download_dir = str(tmp_path)
    return cfg


@pytest.fixture
def mock_metering() -> MagicMock:
    m = MagicMock()
    m.record = AsyncMock(return_value=0.01)
    return m


@pytest.fixture
def toml_file(tmp_path: Path) -> Path:
    """Write a minimal elevenlabs.toml and return its path."""
    toml = tmp_path / "elevenlabs.toml"
    toml.write_text("""\
api_key_env = "TEST_ELEVENLABS_KEY"
api_url = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
default_voice_id = "voice-abc"
default_model_id = "eleven_v3"
timeout = 30

[voice]
speed = 1.2
stability = 0.8
similarity_boost = 0.9

[cost]
per_1k_chars = 0.20
currency = "USD"
""")
    return toml


# ─── Configure tests ────────────────────────────────────────────


class TestElevenlabsConfigure:
    def test_configure_loads_toml(
        self, toml_file: Path, mock_config: MagicMock,
        mock_metering: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "sk-test-123")
        with patch.object(elevenlabs_plugin, "__file__", str(toml_file.parent / "elevenlabs.py")):
            with patch.object(elevenlabs_plugin, "AsyncElevenLabs") as mock_sdk:
                mock_sdk.return_value = MagicMock()
                elevenlabs_plugin.configure(
                    config=mock_config, metering=mock_metering,
                )
        assert elevenlabs_plugin._default_voice_id == "voice-abc"
        assert elevenlabs_plugin._default_model_id == "eleven_v3"
        assert elevenlabs_plugin._timeout == 30
        assert elevenlabs_plugin._voice_speed == 1.2
        assert elevenlabs_plugin._voice_stability == 0.8
        assert elevenlabs_plugin._voice_similarity_boost == 0.9
        assert elevenlabs_plugin._cost_per_1k_chars == 0.20
        assert elevenlabs_plugin._cost_currency == "USD"
        assert elevenlabs_plugin._metering is mock_metering

    def test_configure_missing_toml_is_inert(
        self, tmp_path: Path, mock_config: MagicMock,
    ) -> None:
        # Point __file__ to a dir without elevenlabs.toml
        with patch.object(elevenlabs_plugin, "__file__", str(tmp_path / "elevenlabs.py")):
            elevenlabs_plugin.configure(config=mock_config)
        assert elevenlabs_plugin._client is None

    def test_configure_creates_sdk_client(
        self, toml_file: Path, mock_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "sk-test-123")
        mock_sdk_cls = MagicMock()
        mock_sdk_cls.return_value = MagicMock(name="sdk_client")
        with patch.object(elevenlabs_plugin, "__file__", str(toml_file.parent / "elevenlabs.py")), \
             patch.object(elevenlabs_plugin, "AsyncElevenLabs", mock_sdk_cls):
            elevenlabs_plugin.configure(config=mock_config)
        mock_sdk_cls.assert_called_once()
        assert elevenlabs_plugin._client is not None

    def test_configure_sdk_missing_logs_warning(
        self, toml_file: Path, mock_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "sk-test-123")
        with patch.object(elevenlabs_plugin, "__file__", str(toml_file.parent / "elevenlabs.py")), \
             patch.object(elevenlabs_plugin, "AsyncElevenLabs", None):
            elevenlabs_plugin.configure(config=mock_config)
        assert elevenlabs_plugin._client is None

    def test_configure_stores_metering_and_converter(
        self, toml_file: Path, mock_config: MagicMock,
        mock_metering: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "sk-test-123")
        converter = MagicMock()
        with patch.object(elevenlabs_plugin, "__file__", str(toml_file.parent / "elevenlabs.py")), \
             patch.object(elevenlabs_plugin, "AsyncElevenLabs", MagicMock()):
            elevenlabs_plugin.configure(
                config=mock_config, metering=mock_metering, converter=converter,
            )
        assert elevenlabs_plugin._metering is mock_metering
        assert elevenlabs_plugin._converter is converter


# ─── Tool function tests ────────────────────────────────────────


class TestToolTts:
    @pytest.mark.asyncio
    async def test_successful_generation(self, tmp_path: Path) -> None:
        audio_data = b"fake-mp3-audio-bytes"
        mock_client = MagicMock()

        async def _fake_convert(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            yield audio_data

        mock_client.text_to_speech.convert = _fake_convert
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "voice-1"
        elevenlabs_plugin._default_model_id = "eleven_v3"
        elevenlabs_plugin._output_dir = str(tmp_path)
        # Ensure VoiceSettings is available
        elevenlabs_plugin.VoiceSettings = MagicMock()

        result = await elevenlabs_plugin.tool_tts(text="Hello world")

        assert "attachments" in result
        assert len(result["attachments"]) == 1
        output_path = Path(result["attachments"][0])
        assert output_path.exists()
        assert output_path.read_bytes() == audio_data
        assert "20 bytes" in result["text"]

    @pytest.mark.asyncio
    async def test_sdk_not_installed_returns_error(self) -> None:
        elevenlabs_plugin._client = None
        with patch.object(elevenlabs_plugin, "AsyncElevenLabs", None):
            result = await elevenlabs_plugin.tool_tts(text="Hello")
        assert "Error" in result["text"]
        assert "SDK not installed" in result["text"]
        assert result["attachments"] == []

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self) -> None:
        elevenlabs_plugin._client = None
        with patch.object(elevenlabs_plugin, "AsyncElevenLabs", MagicMock()):
            result = await elevenlabs_plugin.tool_tts(text="Hello")
        assert "Error" in result["text"]
        assert "not configured" in result["text"]

    @pytest.mark.asyncio
    async def test_missing_voice_id_returns_error(self) -> None:
        elevenlabs_plugin._client = MagicMock()
        elevenlabs_plugin._default_voice_id = ""
        result = await elevenlabs_plugin.tool_tts(text="Hello")
        assert "Error" in result["text"]
        assert "voice_id" in result["text"]

    @pytest.mark.asyncio
    async def test_api_error_returns_error_dict(self, tmp_path: Path) -> None:
        async def _fail(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            raise RuntimeError("API connection failed")
            yield b""  # type: ignore[misc]  # make this an async generator

        mock_client = MagicMock()
        mock_client.text_to_speech.convert = _fail
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "voice-1"
        elevenlabs_plugin._output_dir = str(tmp_path)
        elevenlabs_plugin.VoiceSettings = MagicMock()

        result = await elevenlabs_plugin.tool_tts(text="Hello")
        assert "Error" in result["text"]
        assert "RuntimeError" in result["text"]
        assert result["attachments"] == []

    @pytest.mark.asyncio
    async def test_custom_voice_overrides_default(self, tmp_path: Path) -> None:
        captured_kwargs: dict[str, object] = {}

        async def _capture(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            captured_kwargs.update(kwargs)
            yield b"audio"

        mock_client = MagicMock()
        mock_client.text_to_speech.convert = _capture
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "default-voice"
        elevenlabs_plugin._output_dir = str(tmp_path)
        elevenlabs_plugin.VoiceSettings = MagicMock()

        await elevenlabs_plugin.tool_tts(text="Hi", voice_id="custom-voice")
        assert captured_kwargs["voice_id"] == "custom-voice"


# ─── Cost tracking tests ────────────────────────────────────────


class TestElevenlabsCostTracking:
    @pytest.mark.asyncio
    async def test_cost_recorded_on_success(
        self, tmp_path: Path, mock_metering: MagicMock,
    ) -> None:
        async def _gen(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            yield b"audio"

        mock_client = MagicMock()
        mock_client.text_to_speech.convert = _gen
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "v1"
        elevenlabs_plugin._output_dir = str(tmp_path)
        elevenlabs_plugin._metering = mock_metering
        elevenlabs_plugin._cost_per_1k_chars = 0.20
        elevenlabs_plugin._cost_currency = "USD"
        elevenlabs_plugin.VoiceSettings = MagicMock()

        await elevenlabs_plugin.tool_tts(text="Hello world")
        mock_metering.record.assert_called_once()
        call_kwargs = mock_metering.record.call_args[1]
        assert call_kwargs["provider"] == "elevenlabs"
        assert call_kwargs["call_type"] == "tts"
        # "Hello world" = 11 chars → 11/1000 * 0.20 = 0.0022
        assert abs(call_kwargs["cost_override"] - 0.0022) < 0.0001

    @pytest.mark.asyncio
    async def test_cost_per_character_computation(
        self, tmp_path: Path, mock_metering: MagicMock,
    ) -> None:
        async def _gen(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            yield b"audio"

        mock_client = MagicMock()
        mock_client.text_to_speech.convert = _gen
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "v1"
        elevenlabs_plugin._output_dir = str(tmp_path)
        elevenlabs_plugin._metering = mock_metering
        elevenlabs_plugin._cost_per_1k_chars = 0.20
        elevenlabs_plugin.VoiceSettings = MagicMock()

        text = "A" * 5000  # 5000 chars → 5.0 * 0.20 = $1.00
        await elevenlabs_plugin.tool_tts(text=text)
        cost = mock_metering.record.call_args[1]["cost_override"]
        assert abs(cost - 1.0) < 0.001

    @pytest.mark.asyncio
    async def test_no_cost_on_failure(
        self, tmp_path: Path, mock_metering: MagicMock,
    ) -> None:
        async def _fail(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            raise RuntimeError("boom")
            yield b""  # type: ignore[misc]

        mock_client = MagicMock()
        mock_client.text_to_speech.convert = _fail
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "v1"
        elevenlabs_plugin._output_dir = str(tmp_path)
        elevenlabs_plugin._metering = mock_metering
        elevenlabs_plugin._cost_per_1k_chars = 0.20
        elevenlabs_plugin.VoiceSettings = MagicMock()

        await elevenlabs_plugin.tool_tts(text="Hello")
        mock_metering.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_crash_without_metering(self, tmp_path: Path) -> None:
        async def _gen(**kwargs: object) -> types.AsyncGeneratorType:  # type: ignore[type-arg]
            yield b"audio"

        mock_client = MagicMock()
        mock_client.text_to_speech.convert = _gen
        elevenlabs_plugin._client = mock_client
        elevenlabs_plugin._default_voice_id = "v1"
        elevenlabs_plugin._output_dir = str(tmp_path)
        elevenlabs_plugin._metering = None
        elevenlabs_plugin._cost_per_1k_chars = 0.20
        elevenlabs_plugin.VoiceSettings = MagicMock()

        result = await elevenlabs_plugin.tool_tts(text="Hello")
        assert "Error" not in result["text"]
