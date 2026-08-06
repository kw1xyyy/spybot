import asyncio
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted, FSInputFile
from aiogram.enums import ParseMode, ChatType
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN

# ==================== НАСТРОЙКИ ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

DB_PATH = "messages.db"
HISTORY_LIMIT = 100          # сколько сообщений показывать в /history

# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                is_enabled INTEGER DEFAULT 1,
                connected_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER,
                message_id INTEGER,
                connection_id TEXT,
                from_user_id INTEGER,
                from_user_name TEXT,
                text TEXT,
                media_type TEXT,
                file_id TEXT,
                date INTEGER,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        await db.commit()

async def save_connection(connection: BusinessConnection):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO connections
            (connection_id, user_id, user_name, is_enabled, connected_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            connection.id,
            connection.user.id,
            connection.user.full_name,
            1 if connection.is_enabled else 0,
            int(connection.date.timestamp()) if connection.date else int(datetime.now().timestamp())
        ))
        await db.commit()

async def get_owner_id(connection_id: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM connections WHERE connection_id = ? AND is_enabled = 1",
            (connection_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def save_message(message: Message, connection_id: str | None = None):
    text = message.text or message.caption or ""
    media_type = None
    file_id = None

    if message.photo:
        media_type, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        media_type, file_id = "video", message.video.file_id
    elif message.voice:
        media_type, file_id = "voice", message.voice.file_id
    elif message.video_note:
        media_type, file_id = "video_note", message.video_note.file_id
    elif message.document:
        media_type, file_id = "document", message.document.file_id
    elif message.animation:
        media_type, file_id = "animation", message.animation.file_id
    elif message.sticker:
        media_type, file_id = "sticker", message.sticker.file_id
    elif message.audio:
        media_type, file_id = "audio", message.audio.file_id

    from_user = message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO messages
            (chat_id, message_id, connection_id, from_user_id, from_user_name,
             text, media_type, file_id, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message.chat.id,
            message.message_id,
            connection_id or getattr(message, "business_connection_id", None),
            from_user.id if from_user else 0,
            from_user.full_name if from_user else "Неизвестно",
            text,
            media_type,
            file_id,
            int(message.date.timestamp()) if message.date else 0
        ))
        await db.commit()

async def get_message(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT from_user_name, text, media_type, file_id FROM messages "
            "WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        ) as cursor:
            return await cursor.fetchone()

def get_chat_title(chat) -> str:
    if chat.type == ChatType.PRIVATE:
        return chat.full_name or f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "Неизвестный"
    return chat.title or "Группа"

# ==================== АРХИВ ====================

async def get_user_chats(user_id: int) -> list[tuple]:
    """
    Возвращает список чатов пользователя:
    [(chat_id, last_name, messages_count, last_date), ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Сначала находим connection_id пользователя
        async with db.execute(
            "SELECT connection_id FROM connections WHERE user_id = ? AND is_enabled = 1",
            (user_id,)
        ) as cursor:
            connections = [row[0] for row in await cursor.fetchall()]

        if not connections:
            return []

        placeholders = ",".join("?" * len(connections))
        query = f"""
            SELECT 
                chat_id,
                MAX(from_user_name) as name,
                COUNT(*) as cnt,
                MAX(date) as last_date
            FROM messages
            WHERE connection_id IN ({placeholders})
            GROUP BY chat_id
            ORDER BY last_date DESC
        """
        async with db.execute(query, connections) as cursor:
            return await cursor.fetchall()

async def get_chat_history(chat_id: int, connection_ids: list[str], limit: int = 20) -> list[tuple]:
    placeholders = ",".join("?" * len(connection_ids))
    query = f"""
        SELECT from_user_name, text, media_type, date
        FROM messages
        WHERE chat_id = ? AND connection_id IN ({placeholders})
        ORDER BY date DESC
        LIMIT ?
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, [chat_id] + connection_ids + [limit]) as cursor:
            rows = await cursor.fetchall()
            return list(reversed(rows))  # от старых к новым

# ==================== КОМАНДЫ ====================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👋 <b>Привет!</b>\n\n"
        "Я сохраняю удалённые и отредактированные сообщения "
        "в твоих личных переписках через <b>Telegram Business</b>.\n\n"
        "<b>Как подключить:</b>\n"
        "1. Настройки → <b>Telegram Business</b>\n"
        "2. Раздел <b>Чат-боты</b>\n"
        "3. Добавь этого бота\n\n"
        "<b>Команды:</b>\n"
        "/chats — список сохранённых чатов\n"
        "/history — история переписки\n"
        "/status — статус подключения\n"
        "/myid — твой ID"
    )
    await message.answer(text)

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой ID: <code>{message.from_user.id}</code>")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT connection_id, is_enabled, connected_at FROM connections WHERE user_id = ?",
            (message.from_user.id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer(
            "❌ Бот пока не подключён.\n\n"
            "Подключи его через:\n"
            "<b>Настройки → Telegram Business → Чат-боты</b>"
        )
        return

    lines = ["<b>Твои подключения:</b>\n"]
    for conn_id, is_enabled, connected_at in rows:
        status = "✅ Активно" if is_enabled else "❌ Отключено"
        date = datetime.fromtimestamp(connected_at).strftime("%d.%m.%Y %H:%M") if connected_at else "—"
        lines.append(f"{status}\n<code>{conn_id}</code>\n{date}\n")
    await message.answer("\n".join(lines))

@dp.message(Command("chats"))
async def cmd_chats(message: Message):
    chats = await get_user_chats(message.from_user.id)

    if not chats:
        await message.answer(
            "Пока нет сохранённых чатов.\n\n"
            "Как только в подключённых переписках появятся сообщения — они появятся здесь."
        )
        return

    text = "<b>📂 Сохранённые чаты</b>\n\n"
    for i, (chat_id, name, count, last_date) in enumerate(chats, 1):
        date_str = datetime.fromtimestamp(last_date).strftime("%d.%m %H:%M") if last_date else "—"
        text += f"<b>{i}.</b> {name}\n"
        text += f"    сообщений: {count} • последнее: {date_str}\n"
        text += f"    <code>/history {i}</code>\n\n"

    text += "Напиши <code>/history номер</code>, чтобы посмотреть переписку."
    await message.answer(text)

@dp.message(Command("history"))
async def cmd_history(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "<code>/history номер</code>\n\n"
            "Сначала посмотри список чатов командой /chats"
        )
        return

    raw = args[1].strip()

    # Получаем список чатов пользователя
    chats = await get_user_chats(message.from_user.id)
    if not chats:
        await message.answer("Нет сохранённых чатов.")
        return

    # Определяем chat_id
    chat_id = None
    chat_name = "Чат"

    if raw.isdigit():
        num = int(raw)
        if 1 <= num <= len(chats):
            chat_id = chats[num - 1][0]
            chat_name = chats[num - 1][1]
        else:
            # Возможно, пользователь ввёл сам chat_id
            chat_id = num
            for c in chats:
                if c[0] == num:
                    chat_name = c[1]
                    break
    else:
        await message.answer("Нужно указать номер чата из списка /chats")
        return

    # Получаем connection_id пользователя
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT connection_id FROM connections WHERE user_id = ? AND is_enabled = 1",
            (message.from_user.id,)
        ) as cursor:
            connection_ids = [row[0] for row in await cursor.fetchall()]

    if not connection_ids:
        await message.answer("Нет активных подключений.")
        return

    history = await get_chat_history(chat_id, connection_ids, HISTORY_LIMIT)

    if not history:
        await message.answer(f"В чате с <b>{chat_name}</b> пока нет сохранённых сообщений.")
        return

    lines = [f"<b>💬 История: {chat_name}</b>\n"]
    for name, text, media_type, date in history:
        time_str = datetime.fromtimestamp(date).strftime("%d.%m %H:%M") if date else ""
        content = text if text else f"[{media_type or 'медиа'}]"
        # Обрезаем длинный текст
        if len(content) > 180:
            content = content[:180] + "…"
        lines.append(f"<b>{name}</b> <i>{time_str}</i>\n{content}\n")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:4000] + "\n\n… (сообщение обрезано)"

    await message.answer(result)

# ==================== BUSINESS СОБЫТИЯ ====================

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    await save_connection(connection)

    if connection.is_enabled:
        text = (
            "✅ <b>Бот успешно подключён!</b>\n\n"
            "Теперь я сохраняю сообщения в твоих личных чатах.\n"
            "Если что-то удалят или отредактируют — я сразу сообщу.\n\n"
            "Полезные команды:\n"
            "/chats — архив переписок\n"
            "/status — статус"
        )
    else:
        text = (
            "❌ <b>Бот отключён</b>\n\n"
            "Я больше не получаю сообщения из твоих чатов."
        )

    try:
        await bot.send_message(connection.user.id, text)
    except Exception:
        pass

    logger.info(f"Business connection {'enabled' if connection.is_enabled else 'disabled'} | user={connection.user.id}")

@dp.business_message()
async def on_business_message(message: Message):
    connection_id = message.business_connection_id
    await save_message(message, connection_id)

    # Сохранение одноразовых медиа
    if not (message.reply_to_message and message.from_user and connection_id):
        return

    owner_id = await get_owner_id(connection_id)
    if not owner_id or message.from_user.id != owner_id:
        return

    replied = message.reply_to_message
    media_type = file_id = None

    if replied.photo:
        media_type, file_id = "photo", replied.photo[-1].file_id
    elif replied.video:
        media_type, file_id = "video", replied.video.file_id
    elif replied.video_note:
        media_type, file_id = "video_note", replied.video_note.file_id
    elif replied.voice:
        media_type, file_id = "voice", replied.voice.file_id
    elif replied.document:
        media_type, file_id = "document", replied.document.file_id
    elif replied.animation:
        media_type, file_id = "animation", replied.animation.file_id

    if not file_id:
        return

    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return
                content = await resp.read()

        ext = {
            "photo": ".jpg", "video": ".mp4", "video_note": ".mp4",
            "animation": ".mp4", "voice": ".ogg"
        }.get(media_type, ".bin")

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        input_file = FSInputFile(tmp_path)
        caption = f"💾 <b>Сохранено одноразовое медиа</b>\nОт: {replied.from_user.full_name if replied.from_user else 'неизвестно'}"

        try:
            if media_type == "photo":
                await bot.send_photo(owner_id, input_file, caption=caption)
            elif media_type == "video":
                await bot.send_video(owner_id, input_file, caption=caption)
            elif media_type == "voice":
                await bot.send_voice(owner_id, input_file, caption=caption)
            elif media_type == "document":
                await bot.send_document(owner_id, input_file, caption=caption)
            elif media_type == "animation":
                await bot.send_animation(owner_id, input_file, caption=caption)
            elif media_type == "video_note":
                await bot.send_video_note(owner_id, input_file)
                await bot.send_message(owner_id, caption)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        logger.error(f"Ошибка сохранения одноразового медиа: {e}")

@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    connection_id = message.business_connection_id
    if not connection_id:
        return

    owner_id = await get_owner_id(connection_id)
    if not owner_id:
        return

    if message.from_user and message.from_user.id == owner_id:
        await save_message(message, connection_id)
        return

    old = await get_message(message.chat.id, message.message_id)
    old_text = old[1] if old else "не сохранено"
    new_text = message.text or message.caption or "[медиа]"
    from_name = message.from_user.full_name if message.from_user else "Неизвестно"
    chat_title = get_chat_title(message.chat)

    text = (
        f"✏️ <b>Сообщение отредактировано</b>\n\n"
        f"<b>Чат с:</b> {chat_title}\n"
        f"<b>От:</b> {from_name}\n\n"
        f"<b>Было:</b>\n<code>{old_text}</code>\n\n"
        f"<b>Стало:</b>\n<code>{new_text}</code>"
    )
    await bot.send_message(owner_id, text)
    await save_message(message, connection_id)

@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    connection_id = event.business_connection_id
    if not connection_id:
        return

    owner_id = await get_owner_id(connection_id)
    if not owner_id:
        return

    chat_title = get_chat_title(event.chat)

    for msg_id in event.message_ids:
        saved = await get_message(event.chat.id, msg_id)

        if not saved:
            await bot.send_message(
                owner_id,
                f"🗑 <b>Сообщение удалено</b>\n\n"
                f"<b>Чат с:</b> {chat_title}\n"
                f"Оригинал не сохранён"
            )
            continue

        name, text, media_type, file_id = saved

        notify = (
            f"🗑 <b>Сообщение удалено</b>\n\n"
            f"<b>Чат с:</b> {chat_title}\n"
            f"<b>От:</b> {name}\n\n"
            f"<b>Текст:</b>\n<code>{text or '—'}</code>"
        )
        await bot.send_message(owner_id, notify)

        if file_id and media_type:
            caption = f"Восстановлено\nЧат с: {chat_title}"
            try:
                if media_type == "photo":
                    await bot.send_photo(owner_id, file_id, caption=caption)
                elif media_type == "video":
                    await bot.send_video(owner_id, file_id, caption=caption)
                elif media_type == "voice":
                    await bot.send_voice(owner_id, file_id, caption=caption)
                elif media_type == "video_note":
                    await bot.send_video_note(owner_id, file_id)
                    await bot.send_message(owner_id, caption)
                elif media_type == "document":
                    await bot.send_document(owner_id, file_id, caption=caption)
                elif media_type == "animation":
                    await bot.send_animation(owner_id, file_id, caption=caption)
                elif media_type == "sticker":
                    await bot.send_sticker(owner_id, file_id)
                elif media_type == "audio":
                    await bot.send_audio(owner_id, file_id, caption=caption)
            except Exception as e:
                logger.warning(f"Не удалось отправить медиа: {e}")

# ==================== ЗАПУСК ====================

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())