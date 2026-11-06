import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Optional

from services.logger import logger as logging
from services.platforms.ytdlp_helpers import (
    resolve_downloaded_path,
    run_ytdlp_download,
)
from services.platforms.youtube_media import (
    YTDLP_FORMAT_720,
    YTDLP_SPEED_OPTS,
)
from utils.download_manager import (
    DownloadConfig,
    DownloadMetrics,
    ResilientDownloader,
    log_download_metrics,
)

logging = logging.bind(service="adult_media")

ADULT_INFO_TIMEOUT_SECONDS = 45.0
DEFAULT_ADULT_COOKIES_FILE = os.path.join("cookies", "adult.txt")

# Generic best-effort format for adult sites; yt-dlp extractors for these
# sites expose different format sets than YouTube, so fall back to best.
YTDLP_FORMAT_ADULT = (
    "best[height<=720][ext=mp4][acodec!=none][vcodec!=none]/"
    "best[height<=720][acodec!=none][vcodec!=none]/"
    "best[height<=720]/best"
)


def build_adult_ytdlp_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {**YTDLP_SPEED_OPTS}
    cookies_file = os.getenv("YTDLP_ADULT_COOKIES_FILE")
    if cookies_file and cookies_file.strip():
        options["cookiefile"] = cookies_file.strip()
    elif os.path.isfile(DEFAULT_ADULT_COOKIES_FILE):
        options["cookiefile"] = DEFAULT_ADULT_COOKIES_FILE
    options["js_runtimes"] = {"deno": {}, "node": {}}
    options.update({key: value for key, value in overrides.items() if value is not None})
    return options


class AdultMediaService:
    def __init__(
        self,
        output_dir: str,
        *,
        retry_async_operation_func: Callable[..., Awaitable[Any]],
        youtube_dl_factory: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._output_dir = output_dir
        self._retry_async_operation = retry_async_operation_func
        self._youtube_dl_factory = youtube_dl_factory
        self._downloader = ResilientDownloader(
            output_dir,
            config=DownloadConfig(
                chunk_size=2 * 1024 * 1024,
                multipart_threshold=8 * 1024 * 1024,
                max_workers=10,
                retry_backoff=0.6,
            ),
            source="adult",
        )

    def _run_ytdlp_download(self, url: str, ydl_opts: dict[str, Any]) -> None:
        run_ytdlp_download(self._youtube_dl_factory, url, ydl_opts)

    _resolve_downloaded_path = staticmethod(resolve_downloaded_path)

    def get_video_info(self, url: str) -> Optional[dict[str, Any]]:
        try:
            ydl_opts = build_adult_ytdlp_options(
                skip_download=True,
                ignore_no_formats_error=True,
            )
            with self._youtube_dl_factory(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            logging.error("Error fetching adult info: %s", exc)
            return None

    async def download_with_ytdlp_metrics(
        self,
        url: str,
        filename: str,
        format_selector: str,
        source: str,
        *,
        max_filesize: Optional[int] = None,
        merge_output_format: Optional[str] = "mp4",
    ) -> Optional[DownloadMetrics]:
        out_path = self._downloader._resolve_target_path(filename)
        os.makedirs(os.path.dirname(out_path) or self._output_dir, exist_ok=True)
        ydl_opts = build_adult_ytdlp_options(
            format=format_selector,
            outtmpl=out_path,
        )
        if merge_output_format:
            ydl_opts["merge_output_format"] = merge_output_format
        if max_filesize is not None:
            ydl_opts["max_filesize"] = int(max_filesize)
        start = time.monotonic()
        try:
            await asyncio.to_thread(self._run_ytdlp_download, url, ydl_opts)
            resolved_path = self._resolve_downloaded_path(out_path)
            elapsed = time.monotonic() - start
            metrics = DownloadMetrics(
                url=url,
                path=resolved_path,
                size=os.path.getsize(resolved_path),
                elapsed=elapsed,
                used_multipart=False,
                resumed=False,
            )
            log_download_metrics(source, metrics)
            return metrics
        except Exception as exc:
            logging.error("yt-dlp adult download failed: url=%s error=%s", url, exc)
            return None
