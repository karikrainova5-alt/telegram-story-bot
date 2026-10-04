import os
from datetime import datetime, timedelta
from urllib.parse import urlencode
import secrets
import aiohttp
from aiohttp import web
import database as db


class OAuthServer:
    def __init__(self, bot):
        self.bot = bot
        self.app_id = os.getenv("META_FACEBOOK_APP_ID") or os.getenv("META_APP_ID")
        self.app_secret = os.getenv("META_FACEBOOK_APP_SECRET") or os.getenv("META_APP_SECRET")
        self.login_config_id = os.getenv("META_FACEBOOK_LOGIN_CONFIG_ID", "").strip()
        self.graph_version = os.getenv("META_GRAPH_API_VERSION", "v25.0")
        self.redirect_uri = os.getenv(
            "META_REDIRECT_URI",
            "https://telegram-story-bot-production-4554.up.railway.app/oauth/instagram/callback",
        )
        self.host = os.getenv("OAUTH_HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", "8080"))
        self.enabled = bool(self.app_id and self.app_secret and self.redirect_uri)

    def authorization_url(self, telegram_id: int) -> str:
        state = secrets.token_urlsafe(32)
        db.create_oauth_state(state, telegram_id, 10)
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self.login_config_id:
            params["config_id"] = self.login_config_id
        else:
            params["scope"] = ",".join([
                "instagram_basic",
                "instagram_content_publish",
                "pages_show_list",
                "pages_read_engagement",
            ])
        return "https://www.facebook.com/" + self.graph_version + "/dialog/oauth?" + urlencode(params)

    async def start(self):
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_get("/oauth/instagram/callback", self.callback)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.port).start()

    async def health(self, request):
        return web.json_response({
            "ok": True,
            "oauth_configured": self.enabled,
            "facebook_login_configured": bool(self.login_config_id),
        })

    async def callback(self, request):
        code, state = request.query.get("code"), request.query.get("state")
        if request.query.get("error"):
            return web.Response(text="Подключение отменено. Вернись в Telegram и попробуй снова.")
        if not code or not state:
            return web.Response(status=400, text="Не хватает code/state.")

        row = db.consume_oauth_state(state)
        if not row:
            return web.Response(status=400, text="Ссылка устарела или уже использована.")
        telegram_id = row["telegram_id"]

        graph = f"https://graph.facebook.com/{self.graph_version}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{graph}/oauth/access_token", params={
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "redirect_uri": self.redirect_uri,
                    "code": code,
                }, timeout=30) as r:
                    short = await r.json()
                    if r.status >= 400 or "access_token" not in short:
                        raise RuntimeError(f"Ошибка обмена code: {short}")

                async with session.get(f"{graph}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token",
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "fb_exchange_token": short["access_token"],
                }, timeout=30) as r:
                    long = await r.json()
                    if r.status >= 400 or "access_token" not in long:
                        raise RuntimeError(f"Ошибка получения long-lived token: {long}")

                user_token = long["access_token"]
                expires_in = int(long.get("expires_in", 60 * 24 * 3600))

                # First get Pages with their Page access tokens.
                # Do not rely on a nested instagram_business_account field:
                # Meta can omit that field even when the Page is connected
                # to Instagram in Business Settings.
                async with session.get(f"{graph}/me/accounts", params={
                    "fields": "id,name,access_token",
                    "access_token": user_token,
                }, timeout=30) as r:
                    pages = await r.json()
                    if r.status >= 400 or "data" not in pages:
                        raise RuntimeError(f"Ошибка получения Facebook Pages: {pages}")

                page_candidates = pages.get("data", [])
                if not page_candidates:
                    async with session.get(f"{graph}/me", params={
                        "fields": "id,name",
                        "access_token": user_token,
                    }, timeout=30) as r:
                        me = await r.json()
                    raise RuntimeError(
                        "Meta вернула 0 Facebook Pages для этого пользователя. "
                        f"Пользователь Meta: {me}. "
                        "Проверь Full control над Page и разрешение pages_show_list."
                    )

                selected = None
                checked_pages = []

                # Query each Page directly using its Page access token.
                for page in page_candidates:
                    page_id = page.get("id")
                    page_token = page.get("access_token")
                    if not page_id or not page_token:
                        continue

                    async with session.get(f"{graph}/{page_id}", params={
                        "fields": "id,name,instagram_business_account{id,username}",
                        "access_token": page_token,
                    }, timeout=30) as r:
                        page_info = await r.json()

                    checked_pages.append({
                        "id": page_id,
                        "name": page.get("name"),
                        "instagram_business_account": page_info.get("instagram_business_account"),
                    })

                    ig = page_info.get("instagram_business_account")
                    if ig and ig.get("id"):
                        selected = (page, ig)
                        break

                if not selected:
                    raise RuntimeError(
                        "Meta вернула Facebook Pages, но ни одна не содержит instagram_business_account. "
                        f"Проверенные Pages: {checked_pages}"
                    )

                page, ig = selected
                page_token = page.get("access_token")
                if not page_token:
                    raise RuntimeError("Meta не вернула Page Access Token.")

            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
            db.save_account(
                telegram_id,
                ig["id"],
                page_token,
                ig.get("username", "?"),
                expires_at,
                auth_type="facebook",
            )
            await self.bot.send_message(
                telegram_id,
                f"✅ Instagram подключён через Facebook: @{ig.get('username', '?')}\n"
                "Теперь доступен поиск музыки Instagram.\n"
                "Данные подключения сохранены. Теперь /newpost."
            )
            return web.Response(
                content_type="text/html",
                text="<h2>Instagram подключён через Facebook ✅</h2><p>Можно закрыть окно и вернуться в Telegram.</p>",
            )
        except Exception as e:
            await self.bot.send_message(
                telegram_id,
                f"❌ Meta не завершила подключение.\n{e}\n\n"
                "Теперь бот показывает точную причину и данные Pages, которые вернула Meta."
            )
            return web.Response(status=400, text="Не удалось подключить Instagram через Facebook.")
