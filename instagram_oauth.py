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
        self.app_id = os.getenv("META_APP_ID")
        self.app_secret = os.getenv("META_APP_SECRET")
        self.redirect_uri = os.getenv("META_REDIRECT_URI", "https://telegram-story-bot-production-4554.up.railway.app/oauth/instagram/callback")
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
            "scope": "pages_show_list,instagram_basic,instagram_content_publish,pages_read_engagement",
            "state": state,
        }
        version = os.getenv("META_GRAPH_API_VERSION", "v25.0")
        return "https://www.facebook.com/" + version + "/dialog/oauth?" + urlencode(params)


    async def start(self):
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_get("/oauth/instagram/callback", self.callback)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.port).start()

    async def health(self, request):
        return web.json_response({"ok": True, "oauth_configured": self.enabled})

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
        version = os.getenv("META_GRAPH_API_VERSION", "v25.0")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://graph.facebook.com/{version}/oauth/access_token", params={
                    "client_id": self.app_id, "client_secret": self.app_secret,
                    "redirect_uri": self.redirect_uri, "code": code,
                }, timeout=30) as r:
                    short = await r.json()
                    if r.status >= 400 or "access_token" not in short:
                        raise RuntimeError(short)

                async with session.get(f"https://graph.facebook.com/{version}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token",
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "fb_exchange_token": short["access_token"],
                }, timeout=30) as r:
                    long = await r.json()
                    if r.status >= 400 or "access_token" not in long:
                        raise RuntimeError(long)

                user_token = long["access_token"]
                expires_in = int(long.get("expires_in", 60 * 24 * 3600))

                async with session.get(f"https://graph.facebook.com/{version}/me/accounts", params={
                    "fields": "id,name,access_token,instagram_business_account",
                    "access_token": user_token,
                }, timeout=30) as r:
                    pages = await r.json()
                    if r.status >= 400:
                        raise RuntimeError(pages)

                page = next((p for p in pages.get("data", []) if p.get("instagram_business_account", {}).get("id")), None)
                if not page:
                    raise RuntimeError("Не найдена Facebook Page с подключённым Instagram Business/Creator.")

                ig_user_id = page["instagram_business_account"]["id"]
                page_token = page["access_token"]

                async with session.get(f"https://graph.facebook.com/{version}/{ig_user_id}", params={
                    "fields": "id,username,name",
                    "access_token": page_token,
                }, timeout=30) as r:
                    info = await r.json()
                    if r.status >= 400 or "id" not in info:
                        raise RuntimeError(info)

            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
            db.save_account(telegram_id, info["id"], page_token, info.get("username", "?"), expires_at, "facebook")
            await self.bot.send_message(
                telegram_id,
                f"✅ Instagram подключён: @{info.get('username', '?')}\n"
                "Подключение через Facebook Login. Теперь доступны Reels и поиск музыки Instagram.\n"
                "Можно использовать /newpost."
            )
            return web.Response(content_type="text/html",
                text="<h2>Instagram подключён ✅</h2><p>Можно закрыть окно и вернуться в Telegram.</p>")
        except Exception as exc:
            logger = os.getenv("LOG_LEVEL", "INFO")
            await self.bot.send_message(
                telegram_id,
                "❌ Не удалось подключить Instagram через Facebook.\n"
                f"Причина: {exc}\n\n"
                "Проверь, что Instagram — Business/Creator и привязан к Facebook Page."
            )
            return web.Response(status=400, text="Не удалось подключить Instagram.");
