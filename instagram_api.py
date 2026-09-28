"""
Обёртка над официальным Instagram Graph API (для Business/Creator аккаунтов,
подключённых к странице Facebook).

Документация: https://developers.facebook.com/docs/instagram-api/guides/content-publishing
"""

import os
import time
import requests

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v25.0")
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class InstagramAPIError(Exception):
    pass


class InstagramClient:
    def __init__(self, ig_user_id: str, access_token: str):
        """
        ig_user_id — Instagram Business Account ID (не username!)
        access_token — долгоживущий Page Access Token с правами:
            instagram_basic, instagram_content_publish, pages_read_engagement
        """
        self.ig_user_id = ig_user_id
        self.access_token = access_token

    def _get(self, path, params=None):
        params = params or {}
        params["access_token"] = self.access_token
        r = requests.get(f"{GRAPH_URL}/{path}", params=params, timeout=30)
        return self._handle(r)

    def _post(self, path, data=None):
        data = data or {}
        data["access_token"] = self.access_token
        r = requests.post(f"{GRAPH_URL}/{path}", data=data, timeout=30)
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

    def verify_account(self) -> dict:
        """Проверяет, что ig_user_id и токен рабочие, возвращает username."""
        return self._get(self.ig_user_id, {"fields": "username,name"})

    # ---------- Публикация одиночного фото ----------
    def publish_photo(self, image_url: str, caption: str = "") -> str:
        container = self._post(f"{self.ig_user_id}/media", {
            "image_url": image_url,
            "caption": caption,
        })
        creation_id = container["id"]
        return self._publish_container(creation_id)

    # ---------- Публикация одиночного видео / Reels ----------
    def publish_video(self, video_url: str, caption: str = "", is_reel: bool = True) -> str:
        media_type = "REELS" if is_reel else "VIDEO"
        container = self._post(f"{self.ig_user_id}/media", {
            "video_url": video_url,
            "caption": caption,
            "media_type": media_type,
        })
        creation_id = container["id"]
        self._wait_until_ready(creation_id)
        return self._publish_container(creation_id)

    # ---------- Карусель (альбом из нескольких фото/видео) ----------
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
        creation_id = container["id"]
        return self._publish_container(creation_id)

    def _wait_until_ready(self, creation_id: str, timeout: int = 120):
        """Видео обрабатывается асинхронно — ждём статус FINISHED."""
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

    # ---------- Базовая статистика поста (без накрутки — реальные официальные метрики) ----------
    def get_media_insights(self, media_id: str) -> dict:
        return self._get(f"{media_id}/insights", {
            "metric": "impressions,reach,likes,comments,saved"
        })
