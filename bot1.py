import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, BusinessConnection, BusinessMessagesDeleted, FSInputFile,
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

DB_PATH = "messages.db"

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

        try:
            await db.execute("DELETE FROM messages")
            logger.info("✅ База сообщений очищена при запуске")
        except Exception as e:
            logger.warning(f"Не удалось очистить сообщения: {e}")

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

async def get_user_by_connection(connection_id: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM connections WHERE connection_id = ? AND is_enabled = 1",
            (connection_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def get_user_connections(user_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT connection_id FROM connections WHERE user_id = ? AND is_enabled = 1",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def save_message(message: Message, connection_id: str | None = None):
    text = message.text or message.caption or ""
    media_type = None
    file_id = None

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    elif message.video_note:
        media_type = "video_note"
        file_id = message.video_note.file_id
    elif message.sticker:
        media_type = "sticker"
        file_id = message.sticker.file_id
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id

    from_user = message.from_user
    name = from_user.full_name if from_user else "Неизвестно"
    user_id = from_user.id if from_user else 0
    conn_id = connection_id or getattr(message, "business_connection_id", None)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO messages
            (chat_id, message_id, connection_id, from_user_id, from_user_name, text, media_type, file_id, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message.chat.id,
            message.message_id,
            conn_id,
            user_id,
            name,
            text,
            media_type,
            file_id,
            int(message.date.timestamp()) if message.date else 0
        ))
        await db.commit()

async def get_message(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT from_user_name, text, media_type, file_id, connection_id FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        ) as cursor:
            return await cursor.fetchone()

# ==================== ИСТОРИЯ ====================

async def get_chats_for_user(user_id: int) -> list[tuple]:
    connections = await get_user_connections(user_id)
    if not connections:
        return []

    placeholders = ",".join("?" * len(connections))

    async with aiosqlite.connect(DB_PATH) as db:
        query = f"""
            SELECT 
                chat_id,
                from_user_name,
                MAX(date) as last_date,
                COUNT(*) as msg_count
            FROM messages
            WHERE connection_id IN ({placeholders})
              AND from_user_id != ?
            GROUP BY chat_id
            ORDER BY last_date DESC
        """
        async with db.execute(query, (*connections, user_id)) as cursor:
            return await cursor.fetchall()

async def get_chat_history(chat_id: int, connection_ids: list[str]) -> list[tuple]:
    if not connection_ids:
        return []

    placeholders = ",".join("?" * len(connection_ids))

    async with aiosqlite.connect(DB_PATH) as db:
        query = f"""
            SELECT from_user_name, text, media_type, file_id, date
            FROM messages
            WHERE chat_id = ? AND connection_id IN ({placeholders})
            ORDER BY date ASC
        """
        async with db.execute(query, (chat_id, *connection_ids)) as cursor:
            return await cursor.fetchall()
# ==================== КОМАНДЫ ====================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👋 <b>Привет!</b>\n\n"
        "Это бот для отслеживания удалённых и отредактированных сообщений.\n\n"
        "<b>Команды:</b>\n"
        "/chats — список чатов\n"
        "/chats all — показать ВСЕ сообщения (по частям, если много)\n"
        "/status — подключение\n"
        "/myid — твой ID\n"
        "/clear — ОЧИСТИТЬ базу"
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
            "❌ Бот ещё не подключён.\n\n"
            "Подключи его через:\n"
            "Настройки → Telegram Business → Чат-боты"
        )
        return

    text = "<b>Твои подключения:</b>\n\n"
    for conn_id, is_enabled, connected_at in rows:
        status = "✅ Активно" if is_enabled else "❌ Отключено"
        date = datetime.fromtimestamp(connected_at).strftime("%d.%m.%Y %H:%M") if connected_at else "—"
        text += f"• {status}\n  ID: <code>{conn_id}</code>\n  Дата: {date}\n\n"

    await message.answer(text)

@dp.message(Command("chats"))
async def cmd_chats(message: Message):
    user_id = message.from_user.id
    chats = await get_chats_for_user(user_id)

    if not chats:
        await message.answer("У тебя пока нет сохранённых переписок.")
        return

    buttons = []
    for chat_id, name, last_date, msg_count in chats:
        date_str = datetime.fromtimestamp(last_date).strftime("%d.%m %H:%M") if last_date else "—"
        btn_text = f"{name} ({msg_count}) • {date_str}"
        buttons.append([
            InlineKeyboardButton(
                text=btn_text[:64],
                callback_data=f"history:{chat_id}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        f"<b>Твои чаты</b> ({len(chats)}):\nВыбери чат, чтобы посмотреть историю:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("history:"))
async def show_history(callback: CallbackQuery):
    await callback.answer()

    try:
        chat_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.message.answer("Ошибка данных.")
        return

    user_id = callback.from_user.id
    connections = await get_user_connections(user_id)
    history = await get_chat_history(chat_id, connections)

    if not history:
        await callback.message.answer("История этого чата пуста.")
        return

    total = len(history)
    await callback.message.answer(f"📜 <b>История чата</b> (всего {total} сообщений)\n"
                                   f"Выгружаю по частям...")

    current_page = 0
    while current_page < total:
        chunk = history[current_page * 50 : (current_page + 1) * 50]

        for name, text, media_type, file_id, date in chunk:
            time_str = datetime.fromtimestamp(date).strftime("%d.%m %H:%M") if date else ""
            header = f"<b>{name}</b> <i>{time_str}</i>"

            try:
                if text:
                    await callback.message.answer(f"{header}\n{text}")
                elif media_type and file_id:
                    caption = f"{header}\n[{media_type}]"
                    if media_type == "photo":
                        await callback.message.answer_photo(file_id, caption=caption)
                    elif media_type == "video":
                        await callback.message.answer_video(file_id, caption=caption)
                    elif media_type == "voice":
                        await callback.message.answer_voice(file_id, caption=caption)
                    elif media_type == "document":
                        await callback.message.answer_document(file_id, caption=caption)
                    elif media_type == "video_note":
                        await callback.message.answer_video_note(file_id)
                        await callback.message.answer(header)
                    elif media_type == "sticker":
                        await callback.message.answer_sticker(file_id)
                        await callback.message.answer(header)
                    elif media_type == "animation":
                        await callback.message.answer_animation(file_id, caption=caption)
                    else:
                        await callback.message.answer(f"{header}\n[{media_type}]")
                else:
                    await callback.message.answer(f"{header}\n[пустое сообщение]")
            except Exception:
                await callback.message.answer(f"{header}\n[медиа недоступно]")

        current_page += 1
        if current_page * 50 < total:
            await callback.message.answer(f"Продолжаю... (страница {current_page + 1})")

# ==================== КОМАНДА ВЫГРУЗКИ ВСЕХ СООБЩЕНИЙ ====================

@dp.message(Command("chats"))
async def cmd_chats(message: Message):
    if message.text.strip() == "/chats all":
        await show_all_history(message)
        return

    user_id = message.from_user.id
    chats = await get_chats_for_user(user_id)

    if not chats:
        await message.answer("У тебя пока нет сохранённых переписок.")
        return

    buttons = []
    for chat_id, name, last_date, msg_count in chats:
        date_str = datetime.fromtimestamp(last_date).strftime("%d.%m %H:%M") if last_date else "—"
        btn_text = f"{name} ({msg_count}) • {date_str}"
        buttons.append([
            InlineKeyboardButton(
                text=btn_text[:64],
                callback_data=f"history:{chat_id}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        f"<b>Твои чаты</b> ({len(chats)}):\nВыбери чат, чтобы посмотреть историю:",
        reply_markup=keyboard
    )

async def show_all_history(message: Message):
    user_id = message.from_user.id
    connections = await get_user_connections(user_id)

    if not connections:
        await message.answer("У тебя пока нет сохранённых чатов.")
        return

    placeholders = ",".join("?" * len(connections))

    async with aiosqlite.connect(DB_PATH) as db:
        query = f"""
            SELECT from_user_name, text, media_type, file_id, date, chat_id
            FROM messages
            WHERE connection_id IN ({placeholders})
            ORDER BY date ASC
        """
        async with db.execute(query, (*connections,)) as cursor:
            all_messages = await cursor.fetchall()

    if not all_messages:
        await message.answer("История пуста.")
        return

    total = len(all_messages)
    await message.answer(f"📜 <b>Полная история</b> ({total} сообщений)\n"
                         f"Выгружаю по частям...")

    current_page = 0
    while current_page < total:
        chunk = all_messages[current_page * 50 : (current_page + 1) * 50]

        for name, text, media_type, file_id, date, _ in chunk:
            time_str = datetime.fromtimestamp(date).strftime("%d.%m %H:%M") if date else ""
            header = f"<b>{name}</b> <i>{time_str}</i>"

            try:
                if text:
                    await message.answer(f"{header}\n{text}")
                elif media_type and file_id:
                    caption = f"{header}\n[{media_type}]"
                    if media_type == "photo":
                        await message.answer_photo(file_id, caption=caption)
                    elif media_type == "video":
                        await message.answer_video(file_id, caption=caption)
                    elif media_type == "voice":
                        await message.answer_voice(file_id, caption=caption)
                    elif media_type == "document":
                        await message.answer_document(file_id, caption=caption)
                    elif media_type == "video_note":
                        await message.answer_video_note(file_id)
                        await message.answer(header)
                    elif media_type == "sticker":
                        await message.answer_sticker(file_id)
                        await message.answer(header)
                    elif media_type == "animation":
                        await message.answer_animation(file_id, caption=caption)
                    else:
                        await message.answer(f"{header}\n[{media_type}]")
                else:
                    await message.answer(f"{header}\n[пустое сообщение]")
            except Exception:
                await message.answer(f"{header}\n[медиа недоступно]")

        current_page += 1
        if current_page * 50 < total:
            await message.answer(f"Продолжаю... (страница {current_page + 1})")

# ==================== ОЧИСТКА БАЗЫ ====================

async def create_backup() -> str | None:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_filename = f"messages_backup_{timestamp}.sql"
    backup_path = Path(".").joinpath(backup_filename)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async for line in db.iterdump():
                backup_path.write_text(line, encoding="utf-8", mode="a")
        logger.info(f"✅ Сохранён дамп базы: {backup_filename}")
        return str(backup_path)
    except Exception as e:
        logger.error(f"❌ Ошибка создания дампа: {e}")
        return None


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    await message.answer("⚠️ <b>Внимание!</b>\n\n"
                         "Это действие <b>необратимо</b>!\n"
                         "Все сообщения будут удалены навсегда.\n"
                         "Сначала будет создан дамп базы.\n\n"
                         "Напиши <b>ДА</b>, чтобы подтвердить.")

    @dp.message(F.text == "ДА")
    async def confirm_clear(m: Message):
        try:
            backup_path = await create_backup()
            if backup_path:
                await m.answer_document(
                    FSInputFile(backup_path),
                    caption="✅ <b>Сохранён дамп базы</b>\n"
                            "Теперь бот очистит историю."
                )
            else:
                await m.answer("❌ Не удалось создать дамп.")

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM messages")
                await db.commit()

            await m.answer("✅ <b>База сообщений успешно очищена</b>")
        except Exception as e:
            await m.answer(f"❌ Ошибка: {e}")

# ==================== BUSINESS ОБРАБОТЧИКИ ====================

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    await save_connection(connection)

    status = "подключён ✅" if connection.is_enabled else "отключён ❌"
    text = (
        f"<b>Business-соединение {status}</b>\n"
        f"Пользователь: {connection.user.full_name}\n"
        f"ID: <code>{connection.user.id}</code>\n"
        f"Connection ID: <code>{connection.id}</code>"
    )

    try:
        await bot.send_message(connection.user.id, text)
    except Exception:
        pass

    logger.info(f"Business connection {status}: user={connection.user.id}")

# (все остальные обработчики on_business_message, on_edited_business_message, on_deleted_business_messages остаются без изменений)

# ==================== ЗАПУСК ====================

async def main():
    await init_db()
    logger.info("Мультипользовательский бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())