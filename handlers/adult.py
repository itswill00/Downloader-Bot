import asyncio
import re
from typing import Any, Optional

from aiogram import types, Router, F
from yt_dlp import YoutubeDL, DownloadError

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, MAX_FILE_SIZE
from handlers.request_dedupe import claim_message_request
from handlers.user import update_info
from handlers.utils import (
    get_bot_url,
    get_message_text,
    handle_download_error,
    handle_video_too_large,
    load_user_settings,
    make_backpressure_handler,
    make_retry_status_notifier,
    make_status_text_progress_updater,
    maybe_delete_user_message,
    react_to_message,
    remove_file,
    safe_delete_message,
    safe_edit_text,
    send_chat_action_if_needed,
    should_skip_duplicate_business_message,
    retry_async_operation,
    with_message_logging,
)
from services.logger import logger as logging, summarize_url_for_log
from app_context import bot, db, send_analytics
from utils.download_manager import (
    DownloadTooLargeError,
    DownloadMetrics,
)
from services.media.orchestration import run_single_media_flow
from services.media.delivery import make_video_or_document_senders
from services.platforms import adult_media as adult_platform

logging = logging.bind(service="adult")

ADULT_URL_REGEX = (
    r"(https?://(?:www\.|m\.)?(?:pornhub|xvideos|xnxx|xhamster|spankbang|redgifs|eporner)\.(?:com)/\S+)"
)

router = Router()

YTDLP_FORMAT_ADULT = adult_platform.YTDLP_FORMAT_ADULT


def _extract_adult_url(text: str) -> Optional[str]:
    match = re.search(ADULT_URL_REGEX, text or "", re.IGNORECASE)
    if not match:
        return None
    url = match.group(0).strip()
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


class AdultMediaService(adult_platform.AdultMediaService):
    def __init__(self, output_dir: str) -> None:
        super().__init__(
            output_dir,
            retry_async_operation_func=lambda *args, **kwargs: retry_async_operation(
                *args, **kwargs
            ),
            youtube_dl_factory=lambda options: YoutubeDL(options),
        )


adult_media_service = AdultMediaService(OUTPUT_DIR)


def get_adult_video(url):
    return adult_media_service.get_video_info(url)


async def _get_adult_video_with_timeout(url: str):
    return await asyncio.wait_for(
        asyncio.to_thread(get_adult_video, url),
        timeout=adult_platform.ADULT_INFO_TIMEOUT_SECONDS,
    )


async def download_with_ytdlp_metrics(
    url: str,
    filename: str,
    format_selector: str,
    source: str,
    *,
    max_filesize: Optional[int] = None,
    merge_output_format: Optional[str] = "mp4",
) -> Optional[DownloadMetrics]:
    return await adult_media_service.download_with_ytdlp_metrics(
        url,
        filename,
        format_selector,
        source,
        max_filesize=max_filesize,
        merge_output_format=merge_output_format,
    )


@router.message(
    F.text.regexp(ADULT_URL_REGEX, mode="search")
    | F.caption.regexp(ADULT_URL_REGEX, mode="search")
)
@router.business_message(
    F.text.regexp(ADULT_URL_REGEX, mode="search")
    | F.caption.regexp(ADULT_URL_REGEX, mode="search")
)
@with_message_logging("adult", "message")
async def download_adult_video(message: types.Message, direct_url: Optional[str] = None):
    url = direct_url or _extract_adult_url(get_message_text(message))
    if not url:
        return
    business_id = message.business_connection_id
    if await should_skip_duplicate_business_message(
        message, bot, service_name="Adult video", logger=logging
    ):
        await update_info(message)
        return

    logging.debug(
        "Downloading adult video: user_id=%s username=%s url=%s",
        message.from_user.id,
        message.from_user.username,
        summarize_url_for_log(url),
    )
    await send_analytics(
        user_id=message.from_user.id,
        chat_type=message.chat.type,
        action_name="adult_video",
    )
    status_message: Optional[types.Message] = None
    request_lease = None
    show_service_status = business_id is None
    try:
        request_lease = await claim_message_request(message, service="adult", url=url)
        if request_lease is None:
            return
        await react_to_message(message, "👾", business_id=business_id)
        if show_service_status:
            status_message = await message.answer(bm.downloading_video_status())

        user_settings = await load_user_settings(db, message)
        user_captions = user_settings["captions"]
        bot_url = await get_bot_url(bot)

        info = await _get_adult_video_with_timeout(url)
        if not info:
            await safe_delete_message(status_message)
            await message.reply(bm.nothing_found())
            return
        if not (info.get("formats") or []):
            # e.g. XHamster serves an age-verification page with no formats
            # from age-law regions; fail fast with an actionable message.
            logging.error(
                "Adult info has no formats (age verification wall?): url=%s",
                summarize_url_for_log(url),
            )
            await safe_delete_message(status_message)
            await message.reply(bm.adult_age_verification_required())
            return

        video_id = str(info.get("id") or "adult")
        webpage_url = info.get("webpage_url") or url
        title = info.get("title") or "Adult video"
        name = f"{video_id}_adult_video.mp4"

        async def _edit_status(text: str) -> None:
            await safe_edit_text(status_message, text)

        on_progress = make_status_text_progress_updater("Adult video", _edit_status)
        on_retry_download = make_retry_status_notifier(_edit_status)

        def _reply_markup():
            return kb.return_video_info_keyboard(
                views=None,
                likes=None,
                comments=None,
                shares=None,
                music_play_url=None,
                video_url=webpage_url,
                user_settings=user_settings,
                audio_callback_data=None,
            )

        async def _download_media():
            metrics = await download_with_ytdlp_metrics(
                webpage_url,
                name,
                YTDLP_FORMAT_ADULT,
                "adult_video",
                max_filesize=MAX_FILE_SIZE,
            )
            return metrics

        as_document = user_settings.get("as_document") == "on"

        _send_cached, _send_downloaded, _extract_file_id = (
            make_video_or_document_senders(
                message,
                caption=bm.captions(user_captions, title, bot_url),
                reply_markup_fn=_reply_markup,
                as_document=as_document,
                cached_log_label="Adult video",
                cached_log_url=webpage_url,
            )
        )

        async def _after_send():
            await maybe_delete_user_message(
                message, user_settings.get("delete_message")
            )

        async def _inspect_metrics(metrics):
            if metrics.size >= MAX_FILE_SIZE:
                await handle_video_too_large(message, business_id=business_id)
                return False
            return True

        _handle_backpressure = make_backpressure_handler(message, business_id)

        cache_key = f"{webpage_url}#document" if as_document else webpage_url
        cache_file_type = "document" if as_document else "video"

        sent_message = await run_single_media_flow(
            cache_key=cache_key,
            cache_file_type=cache_file_type,
            db_service=db,
            upload_status_text=bm.uploading_status(),
            upload_action="upload_video",
            update_status=_edit_status,
            send_chat_action=lambda action: send_chat_action_if_needed(
                bot, message.chat.id, action, business_id
            ),
            send_cached=_send_cached,
            download_media=_download_media,
            send_downloaded=_send_downloaded,
            extract_file_id=_extract_file_id,
            cleanup_path=remove_file,
            delete_status_message=lambda: safe_delete_message(status_message),
            on_missing_media=lambda: handle_download_error(
                message, business_id=business_id
            ),
            on_after_send=_after_send,
            inspect_metrics=_inspect_metrics,
            on_rate_limit=_handle_backpressure,
            on_queue_busy=_handle_backpressure,
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            chat_type=getattr(message.chat, "type", None),
            service="adult",
            url=direct_url or webpage_url,
            title=title,
        )
        if sent_message and getattr(sent_message, "video", None):
            request_lease.mark_success()
    except DownloadTooLargeError:
        await handle_video_too_large(message, business_id=business_id)
    except (asyncio.TimeoutError, DownloadError):
        if show_service_status:
            await safe_edit_text(status_message, bm.timeout_error())
            await handle_download_error(
                message, business_id=business_id, text=bm.timeout_error()
            )
        else:
            await handle_download_error(message, business_id=business_id)
    except Exception as e:
        logging.error("Adult video download error: %s", e)
        await handle_download_error(message, business_id=business_id)
    finally:
        if request_lease is not None:
            request_lease.finish()
        await safe_delete_message(status_message)
    await update_info(message)


async def process_adult_url(message: types.Message, url: Optional[str] = None) -> None:
    await download_adult_video(message, direct_url=url)
