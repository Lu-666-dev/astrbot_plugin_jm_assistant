from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace


PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _install_astrbot_test_stubs() -> None:
    """Install the small AstrBot surface needed by these unit tests."""

    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.AstrBotConfig = dict
    astrbot_api.logger = SimpleNamespace(
        exception=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )

    astrbot_event = types.ModuleType("astrbot.api.event")

    class Filter:
        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        @staticmethod
        def regex(pattern):
            return lambda function: function

        @staticmethod
        def platform_adapter_type(adapter_type):
            return lambda function: function

    astrbot_event.AstrMessageEvent = object
    astrbot_event.filter = Filter

    astrbot_components = types.ModuleType("astrbot.api.message_components")
    astrbot_components.File = type("File", (), {})

    astrbot_star = types.ModuleType("astrbot.api.star")
    astrbot_star.Context = object
    astrbot_star.Star = type("Star", (), {"__init__": lambda self, context: None})

    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_temp_path = lambda: str(PLUGIN_DIR)

    jmcomic = types.ModuleType("jmcomic")
    astrbot = types.ModuleType("astrbot")
    astrbot_core = types.ModuleType("astrbot.core")
    astrbot_utils = types.ModuleType("astrbot.core.utils")
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": astrbot_api,
            "astrbot.api.event": astrbot_event,
            "astrbot.api.message_components": astrbot_components,
            "astrbot.api.star": astrbot_star,
            "astrbot.core": astrbot_core,
            "astrbot.core.utils": astrbot_utils,
            "astrbot.core.utils.astrbot_path": astrbot_path,
            "jmcomic": jmcomic,
        }
    )


try:
    import main as plugin_main  # noqa: E402
    from main import CaptainAssistantPlugin, extract_album_id  # noqa: E402
except ModuleNotFoundError:
    # The repository's full runtime dependencies are not required for these pure helpers.
    _install_astrbot_test_stubs()
    sys.modules.pop("main", None)
    import main as plugin_main  # noqa: E402
    from main import CaptainAssistantPlugin, extract_album_id  # noqa: E402


class FakeEvent:
    def __init__(self, message: str, group_id: str = "") -> None:
        self.message = message
        self.group_id = group_id
        self.stopped = False
        self.sent = []

    def get_group_id(self) -> str:
        return self.group_id

    def get_message_str(self) -> str:
        return self.message

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result: str) -> None:
        self.sent.append(result)


def test_extract_first_standalone_six_digit_id() -> None:
    assert extract_album_id("请查一下 JM350234，谢谢") == "350234"
    assert extract_album_id("jm350234 和 JM123456") == "350234"


def test_requires_jm_prefix_and_does_not_match_longer_tokens() -> None:
    assert extract_album_id("350234") is None
    assert extract_album_id("1234567") is None
    assert extract_album_id("JM1234567") is None
    assert extract_album_id("编号 12345") is None


def test_does_not_match_digits_embedded_in_alphanumeric_text() -> None:
    assert extract_album_id("notJM350234") is None
    assert extract_album_id("JM350234abc") is None
    assert extract_album_id("https://example.test/JM350234abcdef") is None


def test_find_pdf_prefers_album_filename() -> None:
    with tempfile.TemporaryDirectory(dir=PLUGIN_DIR) as raw_tmp_path:
        tmp_path = Path(raw_tmp_path)
        other = tmp_path / "other.pdf"
        preferred = tmp_path / "JM350234.pdf"
        for path in (other, preferred):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"pdf")

        assert CaptainAssistantPlugin._find_pdf(tmp_path, "350234") == preferred.resolve()


def test_delayed_cleanup_removes_temporary_directory(monkeypatch) -> None:
    monkeypatch.setattr(plugin_main, "PDF_CLEANUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(plugin_main, "PDF_CLEANUP_RETRY_COUNT", 1)

    temp_dir = Path(tempfile.mkdtemp(dir=PLUGIN_DIR))
    (temp_dir / "JM350234.pdf").write_bytes(b"pdf")
    plugin = CaptainAssistantPlugin(None, {})

    asyncio.run(plugin._cleanup_temp_dir_later(temp_dir))

    assert not temp_dir.exists()


def test_private_message_triggers_without_group_filter() -> None:
    plugin = CaptainAssistantPlugin(None, {})
    calls = []

    async def fake_download(event, album_id):
        calls.append(album_id)

    plugin._download_and_send = fake_download
    event = FakeEvent("请查 JM350234")

    asyncio.run(plugin.on_album_id(event))

    assert calls == ["350234"]
    assert event.stopped


def test_group_message_requires_configured_group_id() -> None:
    plugin = CaptainAssistantPlugin(None, {})
    calls = []

    async def fake_download(event, album_id):
        calls.append(album_id)

    plugin._download_and_send = fake_download
    event = FakeEvent("JM350234", group_id="123456")

    asyncio.run(plugin.on_album_id(event))

    assert calls == []
    assert not event.stopped


def test_whitelisted_group_message_triggers() -> None:
    plugin = CaptainAssistantPlugin(
        None,
        {"allowed_group_ids": "123456, 789012\n345678"},
    )
    calls = []

    async def fake_download(event, album_id):
        calls.append(album_id)

    plugin._download_and_send = fake_download
    event = FakeEvent("JM350234", group_id="123456")

    asyncio.run(plugin.on_album_id(event))

    assert calls == ["350234"]
    assert event.stopped


def test_blacklisted_group_message_does_not_trigger() -> None:
    plugin = CaptainAssistantPlugin(
        None,
        {
            "group_access_mode": "blacklist",
            "blocked_group_ids": "123456; 789012",
        },
    )
    calls = []

    async def fake_download(event, album_id):
        calls.append(album_id)

    plugin._download_and_send = fake_download
    event = FakeEvent("JM350234", group_id="789012")

    asyncio.run(plugin.on_album_id(event))

    assert calls == []
    assert not event.stopped


def test_non_blacklisted_group_message_triggers() -> None:
    plugin = CaptainAssistantPlugin(
        None,
        {
            "group_access_mode": "blacklist",
            "blocked_group_ids": "123456, 789012",
        },
    )
    calls = []

    async def fake_download(event, album_id):
        calls.append(album_id)

    plugin._download_and_send = fake_download
    event = FakeEvent("JM350234", group_id="999999")

    asyncio.run(plugin.on_album_id(event))

    assert calls == ["350234"]
    assert event.stopped
