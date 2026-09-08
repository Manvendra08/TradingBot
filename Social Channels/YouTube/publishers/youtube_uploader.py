"""
Social Channels/YouTube/publishers/youtube_uploader.py
Uploads video as private draft and binds custom thumbnail via YouTube Data API v3.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class YouTubePublisher:
    def __init__(self, token_path: Path):
        self.token_path = token_path
        self._youtube = None

    def _get_service(self) -> Any:
        if self._youtube is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            creds = Credentials.from_authorized_user_file(str(self.token_path))
            self._youtube = build("youtube", "v3", credentials=creds)
        return self._youtube

    def upload_private(
        self,
        video_path: Path,
        thumbnail_path: Path,
        title: str,
        description: str,
        tags: list[str],
    ) -> str:
        """Uploads video as PRIVATE (safe for unverified personal OAuth projects)."""
        from googleapiclient.http import MediaFileUpload

        youtube = self._get_service()
        body = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "tags": tags,
                "categoryId": "27",  # Education
            },
            "status": {
                "privacyStatus": "private",  # Private by design
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        log.info("Starting resumable upload to YouTube...")
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info(f"Upload progress: {int(status.progress() * 100)}%")

        video_id = response.get("id", "")
        log.info(f"Video uploaded successfully as PRIVATE. Video ID: {video_id}")

        # Set custom thumbnail if available
        if thumbnail_path and thumbnail_path.exists():
            try:
                log.info("Setting custom thumbnail...")
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
                ).execute()
                log.info("Thumbnail applied successfully.")
            except Exception as exc:
                log.warning(f"Failed to set custom thumbnail: {exc}")

        return video_id
