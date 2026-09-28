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
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import database as db
from instagram_api import InstagramClient, InstagramAPIError

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler()


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


# ---------------------------------------------------------------------------
# /start и справка
# ---------------------------------------------------------------------------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я публикую посты в Instagram по расписанию через официальный API.\n\n"
        "Команды:\n"
        "/connect — подключить Instagram-аккаунт\n"
        "/newpost — создать пост (сразу или по расписанию)\n"
        "/myposts — список запланированных постов\n"
        "/disconnect — отключить аккаунт\n\n"
        "⚠️ Работает только с Instagram Business/Creator аккаунтом, "
        "подключённым к странице Facebook. Личные аккаунты официальный API не поддерживает."
    )


# ---------------------------------------------------------------------------
# Подключение аккаунта
# ---------------------------------------------------------------------------
@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    await message.answer(
        "Шаг 1/2. Пришли Instagram Business Account ID.\n"
        "Как его получить — см. README.md, раздел «Получение доступа»."
    )
    await state.set_state(ConnectState.waiting_ig_user_id)


@router.message(ConnectState.waiting_ig_user_id)
async def connect_get_id(message: Message, state: FSMContext):
    await state.update_data(ig_user_id=message.text.strip())
    await message.answer("Шаг 2/2. Теперь пришли долгоживущий Page Access Token.")
    await state.set_state(ConnectState.waiting_token)


@router.message(ConnectState.waiting_token)
async def connect_get_token(message: Message, state: FSMContext):
    data = await state.get_data()
    ig_user_id = data["ig_user_id"]
    token = message.text.strip()

    client = InstagramClient(ig_user_id, token)
    try:
        info = client.verify_account()
    except InstagramAPIError as e:
        await message.answer(f"Не удалось подключиться: {e}\nПроверь ID и токен и попробуй снова /connect.")
        await state.clear()
        return

    db.save_account(message.from_user.id, ig_user_id, token, info.get("username", "?"))
    await message.answer(f"✅ Подключено: @{info.get('username', '?')}")
    await state.clear()

    # На всякий случай удаляем сообщение с токеном из чата, если возможно
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message):
    db.delete_account(message.from_user.id)
    await message.answer("Аккаунт отключён.")


# ---------------------------------------------------------------------------
# Создание поста
# ---------------------------------------------------------------------------
@router.message(Command("newpost"))
async def cmd_newpost(message: Message, state: FSMContext):
    account = db.get_account(message.from_user.id)
    if not account:
        await message.answer("Сначала подключи Instagram: /connect")
        return
    await state.update_data(media_urls=[], media_type=None)
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
    urls.append(url)
    await state.update_data(media_urls=urls, media_type="photo" if len(urls) == 1 else "carousel")
    await message.answer(f"Добавлено фото ({len(urls)}). Ещё фото или /done.")


@router.message(NewPostState.waiting_media, F.video)
async def newpost_add_video(message: Message, state: FSMContext):
    url = await _file_public_url(message.video.file_id)
    await state.update_data(media_urls=[url], media_type="video")
    await message.answer("Видео добавлено. Напиши /done чтобы продолжить.")


@router.message(NewPostState.waiting_media, Command("done"))
async def newpost_media_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("media_urls"):
        await message.answer("Ты не прислал ни одного медиафайла.")
        return
    await message.answer("Теперь пришли подпись (caption) к посту, или /skip чтобы оставить пустой.")
    await state.set_state(NewPostState.waiting_caption)


@router.message(NewPostState.waiting_caption, Command("skip"))
async def newpost_skip_caption(message: Message, state: FSMContext):
    await state.update_data(caption="")
    await _ask_schedule_choice(message, state)


@router.message(NewPostState.waiting_caption)
async def newpost_caption(message: Message, state: FSMContext):
    await state.update_data(caption=message.text)
    await _ask_schedule_choice(message, state)


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
    await _do_publish(callback.from_user.id, data["media_type"], data["media_urls"], data.get("caption", ""))
    await callback.message.answer("Готово ✅")
    await state.clear()


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
    )
    await message.answer(f"📅 Запланировано на {message.text.strip()} (МСК). ID поста: {post_id}")
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
async def _do_publish(telegram_id: int, media_type: str, media_urls: list[str], caption: str) -> str:
    account = db.get_account(telegram_id)
    if not account:
        raise InstagramAPIError("Аккаунт не подключён")
    client = InstagramClient(account["ig_user_id"], account["access_token"])

    if media_type == "photo":
        return client.publish_photo(media_urls[0], caption)
    elif media_type == "video":
        return client.publish_video(media_urls[0], caption)
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
            await _do_publish(post["telegram_id"], post["media_type"], media_urls, post["caption"])
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
    scheduler.add_job(check_scheduled_posts, "interval", seconds=30)
    scheduler.start()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
