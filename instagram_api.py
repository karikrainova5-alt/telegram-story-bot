"""
Обёртка над официальным Instagram Graph API.
"""

import os
import time
import requests

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v25.0")


class InstagramAPIError(Exception):
    pass


class InstagramClient:
    def __init__(self, ig_user_id: str, access_token: str, auth_type: str = "instagram"):
        self.ig_user_id = ig_user_id
        self.access_token = access_token
        self.auth_type = auth_type or "instagram"
        self.graph_url = (
            f"https://graph.facebook.com/{GRAPH_API_VERSION}"
            if self.auth_type == "facebook"
            else f"https://graph.instagram.com/{GRAPH_API_VERSION}"
        )

    def _get(self, path, params=None):
        params = params or {}
        params["access_token"] = self.access_token
        r = requests.get(f"{self.graph_url}/{path}", params=params, timeout=30)
        return self._handle(r)

    def _post(self, path, data=None):
        data = data or {}
        data["access_token"] = self.access_token
        r = requests.post(f"{self.graph_url}/{path}", data=data, timeout=30)
        return self._handle(r)

    @staticmethod
    def _handle(r: requests.Response):
        try:
            payload = r.json()
        except ValueError:
            raise InstagramAPIError(f"Некорректный ответ API: {r.text[:300]}")
        if r.status_code >= 400 or "error" in payload:
            err = payload.get("error", {})
            msg = err.get("message", "неизвестная ошибка")
            raise InstagramAPIError(f"Instagram API error: {msg}")
        return payload

    def refresh_long_lived_token(self) -> dict:
        if self.auth_type == "facebook":
            raise InstagramAPIError("Для Facebook Login токен обновляется через повторное подключение Meta.")
        r = requests.get("https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token",
            "access_token": self.access_token,
        }, timeout=30)
        return self._handle(r)

    def verify_account(self) -> dict:
        return self._get(self.ig_user_id, {"fields": "username,name"})

    def publish_photo(self, image_url: str, caption: str = "") -> str:
        container = self._post(f"{self.ig_user_id}/media", {
            "image_url": image_url,
            "caption": caption,
        })
        return self._publish_container(container["id"])

    def publish_video(self, video_url: str, caption: str = "", is_reel: bool = True, audio_id: str | None = None) -> str:
        media_type = "REELS" if is_reel else "VIDEO"
        data = {"video_url": video_url, "caption": caption, "media_type": media_type}
        if audio_id and is_reel:
            data["audio_id"] = audio_id
        container = self._post(f"{self.ig_user_id}/media", data)
        creation_id = container["id"]
        self._wait_until_ready(creation_id)
        return self._publish_container(creation_id)

    def publish_story(self, media_url: str, is_video: bool = False) -> str:
        data = {"media_type": "STORIES"}
        if is_video:
            data["video_url"] = media_url
        else:
            data["image_url"] = media_url
        container = self._post(f"{self.ig_user_id}/media", data)
        creation_id = container["id"]
        if is_video:
            self._wait_until_ready(creation_id)
        return self._publish_container(creation_id)

    def search_audio(self, query: str = "", audio_type: str = "music") -> list[dict]:
        # Instagram Audio API requires the connected IG user ID explicitly.
        params = {
            "ig_user_id": self.ig_user_id,
            "audio_type": audio_type,
        }
        if query:
            params["search_query"] = query
        result = self._get("ig_audio", params)
        return result.get("data", result.get("audio", []))

    def publish_carousel(self, media_urls: list[str], caption: str = "") -> str:
        item_ids = []
        for url in media_urls:
            is_video = url.lower().endswith((".mp4", ".mov"))
            payload = {"is_carousel_item": "true"}
            if is_video:
                payload["video_url"] = url
                payload["media_type"] = "VIDEO"
            else:
                payload["image_url"] = url
            item = self._post(f"{self.ig_user_id}/media", payload)
            item_ids.append(item["id"])

        container = self._post(f"{self.ig_user_id}/media", {
            "media_type": "CAROUSEL",
            "caption": caption,
            "children": ",".join(item_ids),
        })
        return self._publish_container(container["id"])

    def _wait_until_ready(self, creation_id: str, timeout: int = 120):
        start = time.time()
        while time.time() - start < timeout:
            status = self._get(creation_id, {"fields": "status_code"})
            code = status.get("status_code")
            if code == "FINISHED":
                return
            if code == "ERROR":
                raise InstagramAPIError("Обработка видео завершилась ошибкой")
            time.sleep(3)
        raise InstagramAPIError("Таймаут ожидания обработки видео")

    def _publish_container(self, creation_id: str) -> str:
        result = self._post(f"{self.ig_user_id}/media_publish", {
            "creation_id": creation_id,
        })
        return result["id"]

    def get_media_insights(self, media_id: str) -> dict:
        return self._get(f"{media_id}/insights", {
            "metric": "impressions,reach,likes,comments,saved"
        })
