"""Captain assistant plugin for looking up JM-prefixed JMComic album IDs."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import jmcomic

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

PLUGIN_NAME = "astrbot_plugin_jm_assistant"
ALBUM_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])JM([0-9]+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
GROUP_ID_SEPARATOR_PATTERN = re.compile(r"[\s,，;；]+")
PDF_CLEANUP_DELAY_SECONDS = 60
PDF_CLEANUP_RETRY_COUNT = 12
PDF_CLEANUP_RETRY_INTERVAL_SECONDS = 5


def extract_album_id(message: str) -> str | None:
    """Extract the first JM-prefixed numeric album ID from a message.

    Args:
        message: Plain-text message received from the platform.

    Returns:
        The first numeric ID after a JM prefix, or None when absent.
    """
    match = ALBUM_ID_PATTERN.search(message)
    return match.group(1) if match else None


class CaptainAssistantPlugin(Star):
    """Look up JMComic albums and send each album as one PDF file."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))

        self.client_impl = str(self.config.get("client_impl", "html")).strip().lower()
        if self.client_impl not in {"api", "html"}:
            self.client_impl = "html"

        self.avs_cookie = str(self.config.get("avs_cookie", "")).strip()
        self.proxy = str(self.config.get("proxy", "")).strip()
        self.image_concurrency = self._positive_int(
            self.config.get("image_concurrency"),
            default=6,
        )
        self.photo_concurrency = self._positive_int(
            self.config.get("photo_concurrency"),
            default=2,
        )
        self.send_retry_count = self._positive_int(
            self.config.get("send_retry_count"),
            default=3,
        )

        self.group_access_mode = (
            str(self.config.get("group_access_mode", "whitelist")).strip().lower()
        )
        if self.group_access_mode not in {"whitelist", "blacklist"}:
            self.group_access_mode = "whitelist"

        self.allowed_group_ids = self._parse_group_ids(
            self.config.get("allowed_group_ids", "")
        )
        self.blocked_group_ids = self._parse_group_ids(
            self.config.get("blocked_group_ids", "")
        )

        self._download_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """Normalize a positive integer configuration value.

        Args:
            value: Raw value read from AstrBot configuration.
            default: Value to use when conversion fails or is not positive.

        Returns:
            A positive integer.
        """
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return default
        return normalized if normalized > 0 else default

    @staticmethod
    def _parse_group_ids(value: Any) -> set[str]:
        """Parse group IDs from a string or a list-like configuration value.

        Args:
            value: Group IDs separated by whitespace, commas, semicolons, or a list.

        Returns:
            A set of normalized group ID strings.
        """
        if value is None:
            return set()

        if isinstance(value, (list, tuple, set)):
            raw_values = value
        else:
            raw_values = GROUP_ID_SEPARATOR_PATTERN.split(str(value))

        return {str(item).strip() for item in raw_values if str(item).strip()}

    def _is_group_allowed(self, group_id: str) -> bool:
        """Check whether a group passes the configured access mode."""
        if self.group_access_mode == "blacklist":
            return group_id not in self.blocked_group_ids
        return group_id in self.allowed_group_ids

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.regex(ALBUM_ID_PATTERN)
    async def on_album_id(self, event: AstrMessageEvent) -> None:
        """Handle a JM-prefixed numeric ID in a QQ message."""
        if not self.enabled:
            return

        group_id = str(event.get_group_id() or "")
        if group_id and not self._is_group_allowed(group_id):
            return

        album_id = extract_album_id(event.get_message_str())
        if album_id is None:
            return

        event.stop_event()
        if self._download_lock.locked():
            await event.send(
                event.plain_result("⏳ 已有一个本子正在处理中，请稍后再试。")
            )
            return

        async with self._download_lock:
            await event.send(
                event.plain_result(f"🔎 正在查询并生成 JM{album_id} PDF，请稍候……")
            )
            await self._download_and_send(event, album_id)

    async def _download_and_send(self, event: AstrMessageEvent, album_id: str) -> None:
        """Download one album, export it as PDF, send it, and clean up temporary files.

        Args:
            event: QQ message event that triggered the lookup.
            album_id: Numeric JMComic album ID.
        """
        temp_parent = Path(get_astrbot_temp_path())
        temp_parent.mkdir(parents=True, exist_ok=True)

        jmcomic_module = self._load_jmcomic()

        temp_dir = Path(
            tempfile.mkdtemp(
                dir=temp_parent,
                prefix=f"{PLUGIN_NAME}_{album_id}_",
            )
        )
        try:
            try:
                pdf_dir = temp_dir / "pdf"
                pdf_dir.mkdir(parents=True, exist_ok=True)

                option = self._build_jm_option(jmcomic_module, temp_dir)
                pdf_feature = jmcomic_module.Feature.export_pdf(
                    pdf_dir=str(pdf_dir),
                    filename_rule=f"JM{album_id}",
                    delete_original_file=False,
                )
                album, downloader = await jmcomic_module.download_album_async(
                    album_id,
                    option=option,
                    check_exception=False,
                    extra=pdf_feature,
                )

                pdf_path = self._find_pdf(pdf_dir, album_id)
                if pdf_path is None:
                    await event.send(
                        event.plain_result(
                            f"❌ JM{album_id} 未生成 PDF，可能是车号不存在、需要登录、网络不可用或 PDF 依赖未安装。"
                        )
                    )
                    return

                failed_image_count = len(
                    getattr(downloader, "download_failed_image", [])
                )
                failed_photo_count = len(
                    getattr(downloader, "download_failed_photo", [])
                )
                if failed_image_count or failed_photo_count:
                    logger.warning(
                        "JMComic album JM%s PDF is partial: failed_images=%d, failed_photos=%d",
                        album_id,
                        failed_image_count,
                        failed_photo_count,
                    )

                try:
                    await self._send_with_retries(
                        event,
                        event.chain_result(
                            [File(name=pdf_path.name, file=str(pdf_path))]
                        ),
                    )
                except Exception:
                    logger.exception("PDF file send failed for JM%s", album_id)
                    await event.send(
                        event.plain_result(
                            f"❌ JM{album_id} PDF 已生成，但 QQ 文件发送失败，请检查协议端文件发送配置。"
                        )
                    )
                    return

                logger.info(
                    "JMComic album JM%s PDF sent: file=%s size=%d failed_images=%d failed_photos=%d",
                    album_id,
                    pdf_path,
                    pdf_path.stat().st_size,
                    failed_image_count,
                    failed_photo_count,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JMComic album JM%s PDF processing failed", album_id)
                await event.send(
                    event.plain_result(
                        f"❌ JM{album_id} 查询或生成 PDF 失败，请检查网络、JMComic 配置或 PDF 依赖。"
                    )
                )
        finally:
            self._schedule_cleanup(temp_dir)

    @staticmethod
    def _load_jmcomic() -> Any:
        """Return the JMComic module installed by AstrBot's plugin loader."""
        return jmcomic

    def _build_jm_option(self, jmcomic_module: Any, temp_dir: Path) -> Any:
        """Build an isolated JMComic option for one temporary download.

        Args:
            jmcomic_module: Imported JMComic module.
            temp_dir: Temporary directory used as the download root.

        Returns:
            A configured JMComic option object.
        """
        client_meta: dict[str, Any] = {}
        if self.proxy:
            client_meta["proxies"] = self.proxy
        if self.avs_cookie:
            client_meta["cookies"] = {"AVS": self.avs_cookie}

        client_config: dict[str, Any] = {
            "impl": self.client_impl,
            "async_impl": "async_api",
        }
        if client_meta:
            client_config["postman"] = {"meta_data": client_meta}

        return jmcomic_module.JmOption.construct(
            {
                "client": client_config,
                "download": {
                    "cache": False,
                    "image": {
                        "decode": True,
                        "suffix": ".jpg",
                    },
                    "threading": {
                        "image": self.image_concurrency,
                        "photo": self.photo_concurrency,
                    },
                },
                "dir_rule": {
                    "base_dir": str(temp_dir),
                    "rule": "Bd / Pindex",
                },
            }
        )

    @staticmethod
    def _find_pdf(pdf_dir: Path, album_id: str) -> Path | None:
        """Find the PDF generated by JMComic's export feature.

        Args:
            pdf_dir: Directory passed to the JMComic PDF feature.
            album_id: Album ID used as the preferred filename.

        Returns:
            The generated PDF path, or None if no PDF exists.
        """
        candidates = sorted(
            path.resolve()
            for path in pdf_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
        if not candidates:
            return None

        preferred = (pdf_dir / f"JM{album_id}.pdf").resolve()
        return preferred if preferred in candidates else candidates[0]

    def _schedule_cleanup(self, temp_dir: Path) -> None:
        """Schedule delayed cleanup so QQ can finish reading the PDF file."""
        task = asyncio.create_task(self._cleanup_temp_dir_later(temp_dir))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _cleanup_temp_dir_later(self, temp_dir: Path) -> None:
        """Remove a temporary task directory after the file upload has settled."""
        await asyncio.sleep(PDF_CLEANUP_DELAY_SECONDS)

        for attempt in range(PDF_CLEANUP_RETRY_COUNT):
            try:
                shutil.rmtree(temp_dir)
                logger.info("Cleaned JMComic temporary directory: %s", temp_dir)
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                if attempt + 1 >= PDF_CLEANUP_RETRY_COUNT:
                    logger.warning(
                        "Could not clean JMComic temporary directory after retries: %s (%s)",
                        temp_dir,
                        exc,
                    )
                    return
                await asyncio.sleep(PDF_CLEANUP_RETRY_INTERVAL_SECONDS)

    async def _send_with_retries(self, event: AstrMessageEvent, result: Any) -> None:
        """Send one message result with a small retry budget.

        Args:
            event: Event used to send the message.
            result: AstrBot message result to send.

        Raises:
            Exception: The last platform error when every attempt fails.
        """
        last_error: Exception | None = None
        for attempt in range(self.send_retry_count):
            try:
                await event.send(result)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.send_retry_count:
                    await asyncio.sleep(attempt + 1)
        if last_error is not None:
            raise last_error
