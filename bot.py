"""
Telegram-бот для автопостинга в Instagram (Business/Creator аккаунты).

Работает через официальный Instagram Graph API — публикует фото, видео/Reels
и карусели по расписанию или сразу. Никакой накрутки — это инструмент
автоматизации контента, аналог Later/Buffer.

Запуск:
    pip install -r requirements.txt
    cp .env.example .env   # и заполнить переменные
    python bot.py
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from instagram_oauth import OAuthServer

import database as db
from instagram_api import InstagramClient, InstagramAPIError

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")

SUBSCRIPTION_STARS = 250
SUBSCRIPTION_DAYS = 30
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_TELEGRAM_ID
PAYMENT_PAYLOAD = "postpilot_monthly_250_stars"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler()
oauth_server = OAuthServer(bot)


# ---------------------------------------------------------------------------
# FSM состояния
# ---------------------------------------------------------------------------
class ConnectState(StatesGroup):
    waiting_ig_user_id = State()
    waiting_token = State()


class NewPostState(StatesGroup):
    waiting_media = State()
    waiting_caption = State()
    waiting_schedule_choice = State()
    waiting_datetime = State()
    waiting_music_choice = State()
    waiting_music_query = State()


# ---------------------------------------------------------------------------
# /start и справка
# ---------------------------------------------------------------------------
@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@router.message(Command("start"))
async def cmd_start(message: Message):
    if is_admin(message.from_user.id):
        subscription_text = "👑 Администратор — доступ бесплатный и без ограничений"
    else:
        access = db.get_access(message.from_user.id)
        subscription_text = "Подписка активна" if access["active"] else (
        "1-й пост бесплатно" if not access["trial_used"] else "Нужна подписка: 250 ⭐ / 30 дней"
    )
    await message.answer(
        "Привет! 👋 Я PostPilot — автопостинг в Instagram.\n\n"
        "Как пользоваться:\n"
        "1. /connect — подключи Instagram Business/Creator.\n"
        "2. /newpost — пришли фото/видео, добавь текст и выбери «сейчас» или дату.\n"
        "3. Первый пост — бесплатно. Далее подписка 250 ⭐ на 30 дней.\n\n"
        "Команды:\n"
        "/newpost — создать публикацию\n"
        "/myposts — мои запланированные публикации\n"
        "/subscribe — оплатить 250 ⭐ / 30 дней\n"
        "/status — проверить доступ\n"
        "/disconnect — отключить Instagram\n"
        "/terms — условия\n"
        "/paysupport — помощь по оплате\n\n"
        f"Статус: {subscription_text}\n\n"
        "⚠️ Нужен Instagram Business/Creator, подключённый к Facebook Page."
    )


# ---------------------------------------------------------------------------
# Подключение аккаунта
# ---------------------------------------------------------------------------
@router.message(Command("connect"))
async def cmd_connect(message: Message):
    if not oauth_server.enabled:
        await message.answer(
            "Подключение через Meta пока не настроено на сервере.\n"
            "Администратору нужно добавить Meta App ID и Secret в Railway."
        )
        return
    url = oauth_server.authorization_url(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Подключить Instagram через Meta", url=url)]
    ])
    await message.answer(
        "Нажми кнопку ниже и войди в свой Instagram Business/Creator аккаунт.\n\n"
        "После разрешения доступ сохранится автоматически — ID и токен вручную вводить не нужно.",
        reply_markup=kb,
    )


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message):
    db.delete_account(message.from_user.id)
    await message.answer("Аккаунт отключён.")


# ---------------------------------------------------------------------------
# Оплата Telegram Stars
# ---------------------------------------------------------------------------
async def _send_subscription_invoice(message: Message):
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="PostPilot — подписка",
        description="Автопостинг в Instagram на 30 дней. 250 Telegram Stars.",
        payload=PAYMENT_PAYLOAD,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="30 дней PostPilot", amount=SUBSCRIPTION_STARS)],
    )


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    if is_admin(message.from_user.id):
        await message.answer("👑 Ты администратор. Доступ к PostPilot бесплатный и без ограничений.")
        return
    access = db.get_access(message.from_user.id)
    if access["active"]:
        until = datetime.fromisoformat(access["subscription_until"])
        await message.answer(
            f"Подписка уже активна до {until.strftime('%d.%m.%Y %H:%M')} UTC.\n"
            "Продлить можно здесь же:"
        )
    await _send_subscription_invoice(message)


@router.message(Command("status"))
async def cmd_status(message: Message):
    access = db.get_access(message.from_user.id)
    if access["active"]:
        until = datetime.fromisoformat(access["subscription_until"])
        await message.answer(
            f"✅ Подписка активна до {until.strftime('%d.%m.%Y %H:%M')} UTC.\n"
            "Можно публиковать без ограничений."
        )
    elif not access["trial_used"]:
        await message.answer(
            "🎁 Твой первый пост бесплатный.\n"
            "После него — 250 ⭐ за 30 дней."
        )
    else:
        await message.answer(
            "Подписка не активна.\n"
            "Оплата: 250 ⭐ за 30 дней."
        )


@router.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "Условия PostPilot\n\n"
        "• Первый опубликованный/запланированный пост — бесплатно.\n"
        "• Далее доступ к автопостингу — 250 Telegram Stars за 30 дней.\n"
        "• Подписка оплачивается внутри Telegram Stars.\n"
        "• Instagram должен поддерживать публикацию через официальный API Meta.\n"
        "• Сервис не гарантирует публикацию при ошибках Meta, Instagram, "
        "Facebook или недоступности аккаунта.\n"
        "• По вопросам оплаты используй /paysupport."
    )


@router.message(Command("paysupport"))
async def cmd_pay_support(message: Message):
    await message.answer(
        "Помощь по оплате\n\n"
        "Если Stars списались, но подписка не включилась, напиши сюда "
        "сообщение с датой оплаты и скрином чека.\n"
        "Не присылай токены Meta, пароли или коды входа."
    )


@router.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    if query.invoice_payload != PAYMENT_PAYLOAD:
        await query.answer(ok=False, error_message="Неизвестный платёж.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    if payment.invoice_payload != PAYMENT_PAYLOAD:
        return
    if payment.currency != "XTR" or payment.total_amount != SUBSCRIPTION_STARS:
        await message.answer("Платёж получен, но параметры не совпали. Напиши /paysupport.")
        return

    recorded = db.record_payment(
        payment.telegram_payment_charge_id,
        message.from_user.id,
        payment.total_amount,
        payment.currency,
    )
    if not recorded:
        await message.answer("Этот платёж уже был обработан. Проверь /status.")
        return

    until = db.activate_subscription(message.from_user.id, SUBSCRIPTION_DAYS)
    await message.answer(
        "🎉 Оплата получена!\n\n"
        f"Подписка PostPilot активна до {until.strftime('%d.%m.%Y %H:%M')} UTC.\n"
        "Теперь можно публиковать посты без ограничений.\n\n"
        "➡️ /newpost"
    )


# ---------------------------------------------------------------------------
# Создание поста
# ---------------------------------------------------------------------------
@router.message(Command("newpost"))
async def cmd_newpost(message: Message, state: FSMContext):
    account = db.get_account(message.from_user.id)
    if not account:
        await message.answer("Сначала подключи Instagram: /connect")
        return

    access = db.get_access(message.from_user.id)
    if not is_admin(message.from_user.id) and not access["active"] and access["trial_used"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Оплатить 250 Stars / 30 дней", callback_data="buy_subscription")]
        ])
        await message.answer(
            "Твой бесплатный пост уже использован.\n\n"
            "Для дальнейших публикаций нужна подписка: **250 ⭐ / 30 дней**.",
            reply_markup=kb,
        )
        return

    await state.update_data(media_urls=[], media_file_ids=[], media_type=None, audio_id=None, audio_title=None)
    await message.answer(
        "Пришли фото или видео для поста.\n"
        "Можно прислать несколько фото подряд — соберу карусель.\n"
        "Когда закончишь — напиши /done."
    )
    await state.set_state(NewPostState.waiting_media)


async def _file_public_url(file_id: str) -> str:
    file = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"


@router.message(NewPostState.waiting_media, F.photo)
async def newpost_add_photo(message: Message, state: FSMContext):
    url = await _file_public_url(message.photo[-1].file_id)
    data = await state.get_data()
    urls = data.get("media_urls", [])
    file_ids = data.get("media_file_ids", [])
    urls.append(url)
    file_ids.append(message.photo[-1].file_id)
    media_type = "photo" if len(urls) == 1 else "carousel"
    await state.update_data(media_urls=urls, media_file_ids=file_ids, media_type=media_type)
    await message.answer(f"Добавлено фото ({len(urls)}). Ещё фото или /done.")


@router.message(NewPostState.waiting_media, F.video)
async def newpost_add_video(message: Message, state: FSMContext):
    url = await _file_public_url(message.video.file_id)
    await state.update_data(media_urls=[url], media_file_ids=[message.video.file_id], media_type="video")
    await message.answer("Видео добавлено. Напиши /done чтобы продолжить.")


@router.message(NewPostState.waiting_media, Command("done"))
async def newpost_media_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("media_urls"):
        await message.answer("Ты не прислал ни одного медиафайла.")
        return
    if len(data.get("media_urls", [])) > 1:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📸 Пост / карусель", callback_data="type_post")],
            [InlineKeyboardButton(text="🎬 Reels", callback_data="type_reel")],
        ])
        await message.answer("Что публикуем?", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📸 Пост", callback_data="type_post")],
            [InlineKeyboardButton(text="🎬 Reels", callback_data="type_reel")],
            [InlineKeyboardButton(text="⭕ История", callback_data="type_story")],
        ])
        await message.answer("Что публикуем?", reply_markup=kb)
    await state.set_state(NewPostState.waiting_caption)


@router.callback_query(NewPostState.waiting_caption, F.data.startswith("type_"))
async def choose_media_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    selected = callback.data.replace("type_", "")
    await state.update_data(publish_type=selected)
    await callback.message.edit_text("Тип публикации выбран: " + {
        "post": "📸 Пост",
        "reel": "🎬 Reels",
        "story": "⭕ История",
    }.get(selected, selected) + "\n\nТеперь пришли подпись к публикации или нажми /skip.")
    await state.set_state(NewPostState.waiting_caption)


@router.message(NewPostState.waiting_caption, Command("skip"))
async def newpost_skip_caption(message: Message, state: FSMContext):
    await state.update_data(caption="")
    await _ask_music_or_schedule(message, state)


@router.message(NewPostState.waiting_caption)
async def newpost_caption(message: Message, state: FSMContext):
    await state.update_data(caption=message.text)
    await _ask_music_or_schedule(message, state)


async def _ask_music_or_schedule(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("publish_type") == "reel":
        await _ask_music_choice(message, state)
    else:
        await _ask_schedule_choice(message, state)


async def _ask_music_choice(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Добавить музыку Instagram", callback_data="music_instagram")],
        [InlineKeyboardButton(text="🔇 Без музыки", callback_data="music_skip")],
    ])
    await message.answer("Добавить музыку?", reply_markup=kb)
    await state.set_state(NewPostState.waiting_music_choice)


@router.callback_query(NewPostState.waiting_music_choice, F.data == "music_skip")
async def music_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _ask_schedule_choice(callback.message, state)


@router.callback_query(NewPostState.waiting_music_choice, F.data == "music_instagram")
async def music_instagram_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "🎵 Напиши название песни или исполнителя.\n"
        "Если оставить пустой запрос, покажу доступные/популярные треки."
    )
    await state.set_state(NewPostState.waiting_music_query)


@router.message(NewPostState.waiting_music_query)
async def music_search(message: Message, state: FSMContext):
    account = db.get_account(message.from_user.id)
    if not account:
        await message.answer("Сначала подключи Instagram: /connect")
        return
    try:
        client = InstagramClient(account["ig_user_id"], account["access_token"])
        tracks = await asyncio.to_thread(client.search_audio, (message.text or "").strip(), "music")
    except Exception as e:
        await message.answer(
            f"❌ Не удалось получить музыку Instagram.\n{e}\n\n"
            "Для музыки аккаунт должен быть подключён через Facebook Login."
        )
        return
    if not tracks:
        await message.answer("Ничего не нашла. Напиши другое название или исполнителя.")
        return
    tracks = tracks[:8]
    await state.update_data(music_tracks=tracks)
    buttons = []
    for i, track in enumerate(tracks):
        title = track.get("title") or track.get("name") or track.get("audio_name") or f"Трек {i+1}"
        artist = track.get("artist") or track.get("artist_name") or track.get("username")
        label = f"🎵 {title}"[:55]
        if artist:
            label = f"{label} — {artist}"[:60]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"music_pick_{i}")])
    buttons.append([InlineKeyboardButton(text="🔇 Без музыки", callback_data="music_skip")])
    await message.answer("Выбери трек:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(NewPostState.waiting_music_choice)


@router.callback_query(NewPostState.waiting_music_choice, F.data.startswith("music_pick_"))
async def music_pick(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    try:
        idx = int(callback.data.rsplit("_", 1)[1])
        track = data["music_tracks"][idx]
    except Exception:
        await callback.message.answer("Трек устарел. Попробуй выбрать музыку ещё раз.")
        return
    audio_id = track.get("audio_id") or track.get("id")
    title = track.get("title") or track.get("name") or track.get("audio_name") or "Instagram music"
    if not audio_id:
        await callback.message.answer("У этого трека Meta не вернула ID. Выбери другой.")
        return
    await state.update_data(audio_id=str(audio_id), audio_title=title)
    await callback.message.edit_text(f"🎵 Выбрано: {title}\n\nКогда публикуем?")
    await state.set_state(NewPostState.waiting_schedule_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="post_now")],
        [InlineKeyboardButton(text="🕒 Запланировать", callback_data="post_schedule")],
    ])
    await callback.message.edit_reply_markup(reply_markup=kb)


async def _ask_schedule_choice(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="post_now")],
        [InlineKeyboardButton(text="🕒 Запланировать", callback_data="post_schedule")],
    ])
    await message.answer("Когда публикуем?", reply_markup=kb)
    await state.set_state(NewPostState.waiting_schedule_choice)


@router.callback_query(NewPostState.waiting_schedule_choice, F.data == "post_now")
async def newpost_now(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("Публикую...")
    try:
        publish_type = data.get("publish_type", "reel" if data["media_type"] == "video" else "post")
        await _do_publish(callback.from_user.id, data["media_type"], data["media_urls"], data.get("caption", ""), data.get("audio_id"), publish_type)
    except Exception as e:
        await callback.message.answer(f"❌ Не удалось опубликовать: {e}")
        await state.clear()
        return

    access = db.get_access(callback.from_user.id)
    if not is_admin(callback.from_user.id) and not access["trial_used"] and not access["active"]:
        db.consume_trial(callback.from_user.id)
        await callback.message.answer(
            "Готово ✅ Первый пост бесплатный!\n\n"
            "Для следующих публикаций — подписка 250 ⭐ / 30 дней.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Оплатить подписку", callback_data="buy_subscription")]
            ]),
        )
    else:
        await callback.message.answer("Готово ✅")
    await state.clear()


@router.callback_query(F.data == "buy_subscription")
async def buy_subscription_callback(callback: CallbackQuery):
    await callback.answer()
    await _send_subscription_invoice(callback.message)


@router.callback_query(NewPostState.waiting_schedule_choice, F.data == "post_schedule")
async def newpost_schedule(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Пришли дату и время публикации в формате:\n"
        "ДД.ММ.ГГГГ ЧЧ:ММ\n"
        "Например: 25.09.2026 18:30\n"
        "(время указывай по МСК)"
    )
    await state.set_state(NewPostState.waiting_datetime)


@router.message(NewPostState.waiting_datetime)
async def newpost_datetime(message: Message, state: FSMContext):
    try:
        dt_msk = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer("Неверный формат. Пример: 25.09.2026 18:30")
        return

    dt_utc = dt_msk - timedelta(hours=3)  # МСК = UTC+3
    if dt_utc <= datetime.utcnow():
        await message.answer("Это время уже в прошлом. Укажи будущую дату.")
        return

    data = await state.get_data()
    post_id = db.add_scheduled_post(
        telegram_id=message.from_user.id,
        media_type=data["media_type"],
        media_urls_json=json.dumps(data["media_urls"]),
        caption=data.get("caption", ""),
        publish_at_iso=dt_utc.isoformat(),
        media_file_ids_json=json.dumps(data.get("media_file_ids", [])),
        audio_id=data.get("audio_id"),
        audio_title=data.get("audio_title"),
        publish_type=data.get("publish_type", "post"),
    )

    access = db.get_access(message.from_user.id)
    trial_message = ""
    if not is_admin(message.from_user.id) and not access["trial_used"] and not access["active"]:
        db.consume_trial(message.from_user.id)
        trial_message = "\n\n🎁 Это твой бесплатный первый пост."

    await message.answer(
        f"📅 Запланировано на {message.text.strip()} (МСК). ID поста: {post_id}"
        f"{trial_message}"
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Список / отмена запланированных постов
# ---------------------------------------------------------------------------
@router.message(Command("myposts"))
async def cmd_myposts(message: Message):
    posts = db.get_user_posts(message.from_user.id)
    if not posts:
        await message.answer("Запланированных постов нет.")
        return
    lines = []
    for p in posts:
        dt_utc = datetime.fromisoformat(p["publish_at"])
        dt_msk = dt_utc + timedelta(hours=3)
        lines.append(
            f"#{p['id']} — {p['status']} — {dt_msk.strftime('%d.%m.%Y %H:%M')} МСК — "
            f"{p['media_type']}"
        )
    lines.append("\nЧтобы отменить: /cancel <id>")
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй: /cancel <id_поста>")
        return
    ok = db.cancel_post(int(parts[1]), message.from_user.id)
    await message.answer("Отменено ✅" if ok else "Пост не найден или уже опубликован.")


# ---------------------------------------------------------------------------
# Публикация
# ---------------------------------------------------------------------------
async def _do_publish(telegram_id: int, media_type: str, media_urls: list[str], caption: str, audio_id: str | None = None, publish_type: str = "post") -> str:
    account = db.get_account(telegram_id)
    if not account:
        raise InstagramAPIError("Аккаунт не подключён")
    client = InstagramClient(account["ig_user_id"], account["access_token"], account.get("auth_type", "instagram"))

    if account.get("token_expires_at"):
        try:
            expires_at = datetime.fromisoformat(account["token_expires_at"])
            if account.get("auth_type", "instagram") == "instagram" and expires_at - datetime.utcnow() < timedelta(days=30):
                refreshed = client.refresh_long_lived_token()
                new_token = refreshed.get("access_token")
                if new_token:
                    expires_in = int(refreshed.get("expires_in", 60 * 24 * 3600))
                    db.update_account_token(
                        telegram_id,
                        new_token,
                        (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat(),
                    )
                    client = InstagramClient(account["ig_user_id"], new_token, account.get("auth_type", "instagram"))
        except Exception:
            logger.warning("Instagram token refresh failed; using current token", exc_info=True)

    if publish_type == "story":
        if len(media_urls) != 1:
            raise InstagramAPIError("История поддерживает только одно фото или видео")
        return client.publish_story(media_urls[0], media_type == "video")
    if publish_type == "reel":
        if media_type != "video":
            raise InstagramAPIError("Reels сейчас доступны для видео")
        return client.publish_video(media_urls[0], caption, is_reel=True, audio_id=audio_id)
    if media_type == "photo":
        return client.publish_photo(media_urls[0], caption)
    elif media_type == "video":
        return client.publish_video(media_urls[0], caption, is_reel=True, audio_id=audio_id)
    elif media_type == "carousel":
        return client.publish_carousel(media_urls, caption)
    else:
        raise InstagramAPIError(f"Неизвестный тип медиа: {media_type}")


async def check_scheduled_posts():
    """Фоновая задача: раз в 30 секунд проверяет, что пора публиковать."""
    now_iso = datetime.utcnow().isoformat()
    due = db.get_due_posts(now_iso)
    for post in due:
        try:
            media_urls = json.loads(post["media_urls"])
            if post.get("media_file_ids"):
                file_ids = json.loads(post["media_file_ids"])
                refreshed_urls = [await _file_public_url(file_id) for file_id in file_ids]
                if refreshed_urls:
                    media_urls = refreshed_urls
            await _do_publish(post["telegram_id"], post["media_type"], media_urls, post["caption"], post.get("audio_id"), post.get("publish_type", "post"))
            db.mark_post_done(post["id"])
            await bot.send_message(post["telegram_id"], f"✅ Пост #{post['id']} опубликован в Instagram.")
        except Exception as e:
            logger.exception("Publish failed for post %s", post["id"])
            db.mark_post_error(post["id"], str(e))
            await bot.send_message(
                post["telegram_id"],
                f"❌ Не удалось опубликовать пост #{post['id']}: {e}"
            )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
async def main():
    db.init_db()
    await oauth_server.start()
    scheduler.add_job(check_scheduled_posts, "interval", seconds=30)
    scheduler.start()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
