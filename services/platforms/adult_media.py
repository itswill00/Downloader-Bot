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
    DownloadProgress,
    ResilientDownloader,
    log_download_metrics,
)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

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
    proxy = os.getenv("YTDLP_ADULT_PROXY") or os.getenv("YTDLP_PROXY")
    if proxy and proxy.strip():
        options["proxy"] = proxy.strip()
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

    @staticmethod
    def _build_progress_bridge(loop: asyncio.AbstractEventLoop, callback):
        if callback is None:
            return None
        pending_tasks: set[asyncio.Task] = set()

        async def _emit(progress: DownloadProgress) -> None:
            try:
                maybe = callback(progress)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:
                logging.debug("Adult yt-dlp progress callback failed: error=%s", exc)

        def _schedule(progress: DownloadProgress) -> None:
            def _runner() -> None:
                task = asyncio.create_task(_emit(progress))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            loop.call_soon_threadsafe(_runner)

        return _schedule

    def _notify_progress(
        self,
        progress_callback,
        *,
        downloaded_bytes: int,
        total_bytes: int,
        started_at: float,
        speed_bps: float,
        eta_seconds: Optional[float],
        done: bool,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            DownloadProgress(
                downloaded_bytes=max(0, int(downloaded_bytes)),
                total_bytes=max(0, int(total_bytes)),
                elapsed=max(0.001, time.monotonic() - started_at),
                speed_bps=max(0.0, float(speed_bps or 0.0)),
                eta_seconds=eta_seconds,
                done=done,
            )
        )

    def _build_progress_hook(self, progress_callback, started_at: float):
        def _hook(status: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            state = status.get("status")
            downloaded = _safe_int(status.get("downloaded_bytes"))
            total = _safe_int(status.get("total_bytes")) or _safe_int(
                status.get("total_bytes_estimate")
            )
            speed = float(status.get("speed") or 0.0)
            eta = status.get("eta")
            eta_seconds = float(eta) if isinstance(eta, (int, float)) else None
            if state == "finished":
                total = total or downloaded
                self._notify_progress(
                    progress_callback,
                    downloaded_bytes=total,
                    total_bytes=total,
                    started_at=started_at,
                    speed_bps=speed,
                    eta_seconds=0.0,
                    done=True,
                )
                return
            if state == "downloading":
                self._notify_progress(
                    progress_callback,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    started_at=started_at,
                    speed_bps=speed,
                    eta_seconds=eta_seconds,
                    done=False,
                )

        return _hook

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
        on_progress=None,
    ) -> Optional[DownloadMetrics]:
        out_path = self._downloader._resolve_target_path(filename)
        os.makedirs(os.path.dirname(out_path) or self._output_dir, exist_ok=True)
        start = time.monotonic()
        ydl_opts = build_adult_ytdlp_options(
            format=format_selector,
            outtmpl=out_path,
        )
        if merge_output_format:
            ydl_opts["merge_output_format"] = merge_output_format
        if max_filesize is not None:
            ydl_opts["max_filesize"] = int(max_filesize)
        if on_progress is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                bridge = self._build_progress_bridge(loop, on_progress)
                ydl_opts["progress_hooks"] = [self._build_progress_hook(bridge, start)]
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
