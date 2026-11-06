from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers import adult
from services.links.detection import detect_supported_service, extract_supported_link
from utils.download_manager import DownloadMetrics


def test_extract_adult_url():
    assert adult._extract_adult_url("watch https://www.pornhub.com/view_video.php?viewkey=abc123") == "https://www.pornhub.com/view_video.php?viewkey=abc123"
    assert adult._extract_adult_url("https://www.xvideos.com/video123/x") is not None
    assert adult._extract_adult_url("no link here") is None


def test_adult_detection_preserves_viewkey():
    assert detect_supported_service("https://www.pornhub.com/view_video.php?viewkey=abc") == "adult"
    url = "https://www.pornhub.com/view_video.php?viewkey=abc123"
    assert extract_supported_link(url) == ("adult", url)


@pytest.mark.asyncio
async def test_download_adult_video_sends_video(monkeypatch, tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    metrics = DownloadMetrics(
        url="https://www.pornhub.com/view_video.php?viewkey=abc123",
        path=str(video_path),
        size=video_path.stat().st_size,
        elapsed=0.1,
        used_multipart=False,
        resumed=False,
    )
    status_message = SimpleNamespace(delete=AsyncMock())
    sent_message = SimpleNamespace(video=SimpleNamespace(file_id="cached-video-id"))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        message_id=1,
        answer=AsyncMock(return_value=status_message),
        reply_video=AsyncMock(return_value=sent_message),
        reply_document=AsyncMock(return_value=sent_message),
        reply=AsyncMock(return_value=status_message),
    )
    info = {
        "id": "abc123",
        "title": "Test Adult Video",
        "webpage_url": "https://www.pornhub.com/view_video.php?viewkey=abc123",
        "formats": [{"format_id": "x", "url": "https://example.com/v.mp4"}],
    }

    monkeypatch.setattr(adult, "get_message_text", lambda _m: "https://www.pornhub.com/view_video.php?viewkey=abc123")
    monkeypatch.setattr(adult, "react_to_message", AsyncMock())
    monkeypatch.setattr(adult, "load_user_settings", AsyncMock(return_value={"captions": "on", "delete_message": "off", "info_buttons": "on", "url_button": "on", "audio_button": "on", "as_document": "off"}))
    monkeypatch.setattr(adult, "get_bot_url", AsyncMock(return_value="https://t.me/angkutmedia_bot"))
    monkeypatch.setattr(adult, "get_adult_video", Mock(return_value=info))
    monkeypatch.setattr(adult, "download_with_ytdlp_metrics", AsyncMock(return_value=metrics))
    monkeypatch.setattr(adult.db, "get_file_id", AsyncMock(return_value=None))
    monkeypatch.setattr(adult.db, "add_file", AsyncMock())
    monkeypatch.setattr(adult, "send_chat_action_if_needed", AsyncMock())
    monkeypatch.setattr(adult, "safe_edit_text", AsyncMock(return_value=True))
    monkeypatch.setattr(adult, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(adult, "remove_file", AsyncMock())
    monkeypatch.setattr(adult, "update_info", AsyncMock())

    await adult.download_adult_video(message)

    adult.download_with_ytdlp_metrics.assert_awaited_once()
    message.reply_video.assert_awaited_once()
    adult.db.add_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_adult_video_returns_none_path(monkeypatch):
    status_message = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        message_id=1,
        answer=AsyncMock(return_value=status_message),
        reply=AsyncMock(return_value=status_message),
    )
    monkeypatch.setattr(adult, "get_message_text", lambda _m: "https://www.xvideos.com/video123/x")
    monkeypatch.setattr(adult, "react_to_message", AsyncMock())
    monkeypatch.setattr(adult, "load_user_settings", AsyncMock(return_value={"captions": "on", "delete_message": "off", "info_buttons": "on", "url_button": "on", "audio_button": "on", "as_document": "off"}))
    monkeypatch.setattr(adult, "get_bot_url", AsyncMock(return_value="https://t.me/angkutmedia_bot"))
    monkeypatch.setattr(adult, "get_adult_video", Mock(return_value=None))
    monkeypatch.setattr(adult, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(adult, "update_info", AsyncMock())

    await adult.download_adult_video(message)

    message.reply.assert_awaited_once_with(adult.bm.nothing_found())


@pytest.mark.asyncio
async def test_download_adult_video_age_wall_when_no_formats(monkeypatch):
    status_message = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username="tester", full_name="Tester"),
        business_connection_id=None,
        chat=SimpleNamespace(id=99, type="private"),
        message_id=1,
        answer=AsyncMock(return_value=status_message),
        reply=AsyncMock(return_value=status_message),
    )
    info = {
        "id": "xhRytaC",
        "title": "Age walled",
        "webpage_url": "https://xhamster.com/videos/x-xhRytaC",
        "formats": [],
    }
    monkeypatch.setattr(adult, "get_message_text", lambda _m: "https://xhamster.com/videos/x-xhRytaC")
    monkeypatch.setattr(adult, "react_to_message", AsyncMock())
    monkeypatch.setattr(adult, "load_user_settings", AsyncMock(return_value={"captions": "on", "delete_message": "off", "info_buttons": "on", "url_button": "on", "audio_button": "on", "as_document": "off"}))
    monkeypatch.setattr(adult, "get_bot_url", AsyncMock(return_value="https://t.me/angkutmedia_bot"))
    monkeypatch.setattr(adult, "get_adult_video", Mock(return_value=info))
    monkeypatch.setattr(adult, "safe_delete_message", AsyncMock())
    monkeypatch.setattr(adult, "update_info", AsyncMock())

    await adult.download_adult_video(message)

    message.reply.assert_awaited_once_with(adult.bm.adult_age_verification_required())


def test_adult_progress_hook_reports_downloaded_bytes():
    from services.platforms.adult_media import AdultMediaService
    from unittest.mock import AsyncMock as _AM
    svc = AdultMediaService.__new__(AdultMediaService)
    seen = []
    hook = svc._build_progress_hook(seen.append, started_at=__import__("time").monotonic())
    hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000, "speed": 123.0, "eta": 4})
    assert len(seen) == 1
    assert seen[0].downloaded_bytes == 500
    assert seen[0].total_bytes == 1000
    assert seen[0].done is False
    hook({"status": "finished", "downloaded_bytes": 1000, "total_bytes": 1000})
    assert seen[1].done is True
