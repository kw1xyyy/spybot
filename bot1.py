"""Telegram Business-бот: хранит переписки, сообщает об удалённом/изменённом
и показывает историю чатов в Mini App (aiogram 3 + aiohttp + aiosqlite)."""
import asyncio
import html
import io
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, BusinessConnection, BusinessMessagesDeleted, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import config

BOT_TOKEN = os.getenv("BOT_TOKEN") or config.BOT_TOKEN
# Bothost сам выдаёт DOMAIN и PORT; локально можно задать WEBAPP_URL в config.py
WEBAPP_URL = (os.getenv("DOMAIN") or getattr(config, "WEBAPP_URL", "")).rstrip("/")
if WEBAPP_URL and not WEBAPP_URL.startswith("http"):
    WEBAPP_URL = "https://" + WEBAPP_URL
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.getenv("PORT") or getattr(config, "WEB_PORT", 3000))
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "messages.db")
INDEX_HTML = Path(__file__).parent / "webapp" / "index.html"
TG_JS_URL = "https://telegram.org/js/telegram-web-app.js"
TG_JS_LOCAL = Path(__file__).parent / "telegram-web-app.js"  # можно положить вручную
TG_JS_CACHE = DATA_DIR / "telegram-web-app.js"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db: aiosqlite.Connection  # одно соединение на весь процесс

esc = html.escape
MEDIA_KINDS = ("video", "voice", "document", "video_note", "sticker", "animation", "audio")
NO_CAPTION = {"video_note", "sticker"}
EXT = {"photo": ".jpg", "video": ".mp4", "animation": ".mp4", "video_note": ".mp4",
       "voice": ".ogg", "sticker": ".webp", "audio": ".mp3"}
MIME = {"photo": "image/jpeg", "sticker": "image/webp", "video": "video/mp4",
        "animation": "video/mp4", "video_note": "video/mp4", "voice": "audio/ogg",
        "audio": "audio/mpeg"}
OWNED = "connection_id IN (SELECT connection_id FROM connections WHERE user_id = ?)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, user_name TEXT,
    is_enabled INTEGER DEFAULT 1, connected_at INTEGER);
CREATE TABLE IF NOT EXISTS chats (
    connection_id TEXT, chat_id INTEGER, title TEXT,
    PRIMARY KEY (connection_id, chat_id));
CREATE TABLE IF NOT EXISTS messages (
    connection_id TEXT, chat_id INTEGER, message_id INTEGER,
    from_user_id INTEGER, from_user_name TEXT, text TEXT,
    media_type TEXT, file_id TEXT, date INTEGER,
    edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
    PRIMARY KEY (connection_id, chat_id, message_id));
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, message_id);
"""

# ==================== БАЗА ====================

async def fetchall(sql: str, args: tuple = ()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchall()

async def fetchone(sql: str, args: tuple = ()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()

async def get_owner(connection_id: str) -> int | None:
    row = await fetchone(
        "SELECT user_id FROM connections WHERE connection_id = ? AND is_enabled = 1",
        (connection_id,))
    return row[0] if row else None

async def save_connection(c: BusinessConnection):
    await db.execute(
        "INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?, ?)",
        (c.id, c.user.id, c.user.full_name, int(c.is_enabled),
         int((c.date or datetime.now()).timestamp())))
    await db.commit()

def extract_media(m: Message) -> tuple[str | None, str | None]:
    if m.photo:
        return "photo", m.photo[-1].file_id
    for kind in MEDIA_KINDS:
        obj = getattr(m, kind)
        if obj:
            return kind, obj.file_id
    return None, None

async def save_message(m: Message, conn_id: str):
    kind, file_id = extract_media(m)
    u = m.from_user
    await db.execute("""
        INSERT INTO messages (connection_id, chat_id, message_id, from_user_id, from_user_name,
                              text, media_type, file_id, date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (connection_id, chat_id, message_id) DO UPDATE SET
            text = excluded.text, media_type = excluded.media_type,
            file_id = excluded.file_id, edited = 1""",
        (conn_id, m.chat.id, m.message_id, u.id if u else 0,
         u.full_name if u else "Неизвестно", m.text or m.caption or "",
         kind, file_id, int(m.date.timestamp())))
    await db.execute(
        "INSERT INTO chats VALUES (?, ?, ?) ON CONFLICT DO UPDATE SET title = excluded.title",
        (conn_id, m.chat.id, m.chat.full_name))
    await db.commit()

# ==================== ОТПРАВКА МЕДИА ====================

async def send_media(chat_id: int, kind: str, file, caption: str | None = None):
    send = getattr(bot, f"send_{kind}")
    if kind in NO_CAPTION:
        await send(chat_id, file)
        if caption:
            await bot.send_message(chat_id, caption)
    else:
        await send(chat_id, file, caption=caption)

def clip(text: str, n: int = 3500) -> str:
    return text if len(text) <= n else text[:n] + "…"

# ==================== КОМАНДЫ ====================

def app_keyboard() -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Открыть историю", web_app=WebAppInfo(url=WEBAPP_URL))]])

@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(
        "👋 <b>Привет!</b>\n\nЯ слежу за удалёнными и изменёнными сообщениями "
        "в твоих личных чатах (Telegram Business).\n\n"
        "/chats — история чатов в мини-приложении\n"
        "/export — выгрузка всей истории файлом\n"
        "/status — подключение\n"
        "/myid — твой ID\n"
        "/clear — удалить свою историю",
        reply_markup=app_keyboard())

@dp.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>")

@dp.message(Command("status"))
async def cmd_status(m: Message):
    rows = await fetchall(
        "SELECT connection_id, is_enabled, connected_at FROM connections WHERE user_id = ?",
        (m.from_user.id,))
    if not rows:
        await m.answer("❌ Бот ещё не подключён.\n\n"
                       "Настройки → Telegram Business → Чат-боты")
        return
    lines = ["<b>Твои подключения:</b>\n"]
    for conn_id, enabled, at in rows:
        when = datetime.fromtimestamp(at).strftime("%d.%m.%Y %H:%M") if at else "—"
        lines.append(f"• {'✅ Активно' if enabled else '❌ Отключено'}\n"
                     f"  ID: <code>{conn_id}</code>\n  Дата: {when}\n")
    await m.answer("\n".join(lines))

@dp.message(Command("chats"))
async def cmd_chats(m: Message):
    kb = app_keyboard()
    if not kb:
        await m.answer("Mini App не настроен: добавь <code>WEBAPP_URL</code> в config.py. "
                       "Пока доступен /export.")
        return
    await m.answer("История чатов — в мини-приложении 👇", reply_markup=kb)

async def build_export(uid: int) -> bytes:
    rows = await fetchall(f"""
        SELECT COALESCE(c.title, m.from_user_name), m.date, m.from_user_name,
               COALESCE(NULLIF(m.text, ''), '[' || m.media_type || ']', ''), m.deleted
        FROM messages m LEFT JOIN chats c USING (connection_id, chat_id)
        WHERE m.{OWNED} ORDER BY 1, m.date""", (uid,))
    out, current = [], None
    for title, date, who, text, deleted in rows:
        if title != current:
            current = title
            out.append(f"\n=== {title} ===")
        stamp = datetime.fromtimestamp(date).strftime("%d.%m.%Y %H:%M")
        out.append(f"[{stamp}] {who}{' (удалено)' if deleted else ''}: {text}")
    return "\n".join(out).strip().encode()

@dp.message(Command("export"))
async def cmd_export(m: Message):
    data = await build_export(m.from_user.id)
    if not data:
        await m.answer("История пуста.")
        return
    await m.answer_document(BufferedInputFile(data, "history.txt"), caption="📜 Твоя история")

@dp.message(Command("clear"))
async def cmd_clear(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data="clear:yes"),
        InlineKeyboardButton(text="Отмена", callback_data="clear:no")]])
    await m.answer("⚠️ <b>Удалить всю твою историю?</b>\n\nДействие необратимо. "
                   "Перед удалением я пришлю файл с копией.", reply_markup=kb)

@dp.callback_query(F.data.startswith("clear:"))
async def on_clear(cb: CallbackQuery):
    await cb.answer()
    if cb.data != "clear:yes":
        await cb.message.edit_text("Отменено.")
        return
    uid = cb.from_user.id
    data = await build_export(uid)
    if data:
        await cb.message.answer_document(BufferedInputFile(data, "history_backup.txt"),
                                         caption="💾 Копия перед удалением")
    await db.execute(f"DELETE FROM messages WHERE {OWNED}", (uid,))
    await db.execute(f"DELETE FROM chats WHERE {OWNED}", (uid,))
    await db.commit()
    await cb.message.edit_text("✅ История удалена.")

# ==================== BUSINESS ====================

@dp.business_connection()
async def on_business_connection(c: BusinessConnection):
    await save_connection(c)
    status = "подключён ✅" if c.is_enabled else "отключён ❌"
    try:
        await bot.send_message(
            c.user.id,
            f"<b>Business-соединение {status}</b>\n"
            f"Пользователь: {esc(c.user.full_name)}\nID: <code>{c.user.id}</code>")
    except TelegramAPIError:
        pass
    log.info("Business connection %s: user=%s", status, c.user.id)

@dp.business_message()
async def on_business_message(m: Message):
    conn = m.business_connection_id
    await save_message(m, conn)

    # Владелец отвечает на чужое медиа — бот присылает его копию в личку
    r = m.reply_to_message
    owner = await get_owner(conn)
    if not (r and owner and m.from_user and m.from_user.id == owner):
        return
    if r.from_user and r.from_user.id == owner:
        return
    kind, file_id = extract_media(r)
    if not file_id:
        return
    try:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        name = (r.document.file_name if kind == "document" and r.document.file_name
                else f"{r.message_id}{EXT.get(kind, '.bin')}")
        who = esc(r.from_user.full_name) if r.from_user else "неизвестно"
        await send_media(owner, kind, BufferedInputFile(buf.getvalue(), name),
                         f"💾 <b>Сохранено</b>\nОт: {who}")
    except TelegramAPIError as e:
        log.error("Save media error: %s", e)
        await bot.send_message(owner, f"❌ Не удалось сохранить файл: {esc(str(e))}")

@dp.edited_business_message()
async def on_edited_business_message(m: Message):
    conn = m.business_connection_id
    owner = await get_owner(conn) if conn else None
    if not owner:
        return
    if not (m.from_user and m.from_user.id != owner):
        await save_message(m, conn)
        return
    old = await fetchone(
        "SELECT text FROM messages WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
        (conn, m.chat.id, m.message_id))
    old_text = old[0] if old else "не сохранено"
    new_text = m.text or m.caption or "[медиа]"
    await bot.send_message(
        owner,
        f"✏️ <b>Сообщение отредактировано</b>\nОт: {esc(m.from_user.full_name)}\n\n"
        f"<b>Было:</b>\n<code>{esc(clip(old_text))}</code>\n\n"
        f"<b>Стало:</b>\n<code>{esc(clip(new_text))}</code>")
    await save_message(m, conn)

@dp.deleted_business_messages()
async def on_deleted_business_messages(e: BusinessMessagesDeleted):
    conn = e.business_connection_id
    owner = await get_owner(conn) if conn else None
    if not owner:
        return
    for msg_id in e.message_ids:
        row = await fetchone(
            "SELECT from_user_name, text, media_type, file_id FROM messages "
            "WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
            (conn, e.chat.id, msg_id))
        if not row:
            await bot.send_message(
                owner, f"🗑 Удалено сообщение (id <code>{msg_id}</code>), оригинал не был сохранён.")
            continue
        name, text, kind, file_id = row
        await bot.send_message(
            owner, f"🗑 <b>Сообщение удалено</b>\nОт: {esc(name)}\n\n"
                   f"<b>Текст:</b>\n<code>{esc(clip(text or '[без текста]'))}</code>")
        if kind and file_id:
            try:
                await send_media(owner, kind, file_id, "♻️ Восстановленное медиа")
            except TelegramAPIError as err:
                log.warning("Не удалось отправить медиа: %s", err)
    await db.executemany(
        "UPDATE messages SET deleted = 1 WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
        [(conn, e.chat.id, i) for i in e.message_ids])
    await db.commit()

# ==================== API ДЛЯ MINI APP ====================

def auth(request: web.Request) -> int:
    """Проверяет подпись initData от Telegram и возвращает id пользователя."""
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, request.headers.get("X-Init-Data", ""))
    except ValueError:
        raise web.HTTPUnauthorized()
    if not data.user or time.time() - data.auth_date.timestamp() > 86400:
        raise web.HTTPUnauthorized()
    return data.user.id

def qint(request: web.Request, key: str, default: int | None = None) -> int:
    try:
        return int(request.query[key])
    except (KeyError, ValueError):
        if default is None:
            raise web.HTTPBadRequest()
        return default

async def find_message(uid: int, chat: int, msg_id: int):
    row = await fetchone(
        f"SELECT media_type, file_id FROM messages "
        f"WHERE chat_id = ? AND message_id = ? AND {OWNED} AND file_id IS NOT NULL",
        (chat, msg_id, uid))
    if not row:
        raise web.HTTPNotFound()
    return row

async def api_index(_):
    return web.FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

async def api_tgjs(_):
    """Отдаёт скрипт Telegram с нашего сервера: у части пользователей telegram.org не открывается."""
    path = TG_JS_LOCAL if TG_JS_LOCAL.exists() else TG_JS_CACHE
    if not path.exists():
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as s, s.get(TG_JS_URL) as r:
                r.raise_for_status()
                TG_JS_CACHE.write_bytes(await r.read())
        except Exception as e:
            log.error("Не удалось скачать telegram-web-app.js: %s", e)
            raise web.HTTPBadGateway()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

async def api_chats(request: web.Request):
    uid = auth(request)
    rows = await fetchall(f"""
        WITH mine AS (SELECT * FROM messages WHERE {OWNED}),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY date DESC, message_id DESC) AS rn,
                   COUNT(*) OVER (PARTITION BY chat_id) AS n,
                   SUM(deleted) OVER (PARTITION BY chat_id) AS gone
            FROM mine)
        SELECT r.chat_id, COALESCE(c.title, r.from_user_name), r.n, r.gone, r.date,
               COALESCE(NULLIF(r.text, ''), '[' || r.media_type || ']', '')
        FROM ranked r LEFT JOIN chats c
             ON c.chat_id = r.chat_id AND c.connection_id = r.connection_id
        WHERE r.rn = 1 ORDER BY r.date DESC""", (uid,))
    return web.json_response([
        {"id": i, "title": t, "count": n, "deleted": g, "date": d, "preview": p[:90]}
        for i, t, n, g, d, p in rows])

async def api_history(request: web.Request):
    uid = auth(request)
    chat, limit = qint(request, "chat"), 50
    cols = "SELECT message_id, from_user_id, text, media_type, date, edited, deleted FROM messages"
    has_newer = False
    if "around" in request.query or "after" in request.query:
        # around=<id>: окно вокруг сообщения; after=<id>: следующая порция более новых
        around = "around" in request.query
        mid = qint(request, "around" if around else "after")
        size = limit // 2 if around else limit
        newer = await fetchall(
            f"{cols} WHERE chat_id = ? AND {OWNED} AND message_id > ? ORDER BY message_id LIMIT ?",
            (chat, uid, mid, size + 1))
        has_newer, newer = len(newer) > size, newer[:size]
        older = await fetchall(
            f"{cols} WHERE chat_id = ? AND {OWNED} AND message_id <= ? "
            f"ORDER BY message_id DESC LIMIT ?", (chat, uid, mid, size)) if around else []
        rows, has_more = [*reversed(older), *newer], bool(around and len(older) == size)
    else:
        before = qint(request, "before", 2**62)
        extra, args = "", [chat, before, uid]
        q = request.query.get("q", "").strip().lower()
        if q:
            extra = " AND pylower(text) LIKE ? ESCAPE '\\'"
            args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        recent = await fetchall(
            f"{cols} WHERE chat_id = ? AND message_id < ? AND {OWNED}{extra} "
            f"ORDER BY message_id DESC LIMIT ?", (*args, limit))
        rows, has_more = list(reversed(recent)), len(recent) == limit
    items = [{"id": i, "from": f, "text": t, "media": k, "date": d, "edited": e, "deleted": x}
             for i, f, t, k, d, e, x in rows]
    return web.json_response({"owner": uid, "items": items, "has_more": has_more,
                              "has_newer": has_newer})

async def api_media(request: web.Request):
    uid = auth(request)
    kind, file_id = await find_message(uid, qint(request, "chat"), qint(request, "id"))
    buf = io.BytesIO()
    try:
        await bot.download(file_id, destination=buf)
    except TelegramAPIError:  # например, файл больше 20 МБ
        raise web.HTTPNotFound()
    return web.Response(body=buf.getvalue(), content_type=MIME.get(kind, "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=86400"})

async def api_resend(request: web.Request):
    uid = auth(request)
    kind, file_id = await find_message(uid, qint(request, "chat"), qint(request, "id"))
    try:
        await send_media(uid, kind, file_id)
    except TelegramAPIError:
        raise web.HTTPBadGateway()
    return web.json_response({"ok": True})

AVATARS: dict[int, bytes | None] = {}

async def api_avatar(request: web.Request):
    uid = auth(request)
    chat = qint(request, "chat")
    if not await fetchone(f"SELECT 1 FROM messages WHERE chat_id = ? AND {OWNED} LIMIT 1", (chat, uid)):
        raise web.HTTPNotFound()
    if chat not in AVATARS:
        data = None
        try:  # для Business-чатов фото часто недоступно — тогда в приложении будут инициалы
            info = await bot.get_chat(chat)
            if info.photo:
                buf = io.BytesIO()
                await bot.download(info.photo.small_file_id, destination=buf)
                data = buf.getvalue()
        except TelegramAPIError:
            pass
        AVATARS[chat] = data
    if not AVATARS[chat]:
        raise web.HTTPNotFound()
    return web.Response(body=AVATARS[chat], content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

def make_web_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", api_index), web.get("/tg.js", api_tgjs), web.get("/api/chats", api_chats),
        web.get("/api/history", api_history), web.get("/api/media", api_media),
        web.get("/api/avatar", api_avatar), web.post("/api/resend", api_resend)])
    return app

# ==================== ПЕРЕНОС СТАРОЙ БАЗЫ ====================

async def migrate_old_db():
    """Переносит сообщения из old_messages*.db (можно запускать повторно, дублей не будет)."""
    folders = (Path(__file__).parent, DATA_DIR)
    for src in sorted({p.resolve() for d in folders for p in d.glob("old_messages*.db")}):
        await db.commit()
        await db.execute("ATTACH DATABASE ? AS old", (str(src),))
        try:
            await db.execute("INSERT OR IGNORE INTO connections SELECT connection_id, user_id, "
                             "user_name, is_enabled, connected_at FROM old.connections")
            cur = await db.execute("""
                INSERT OR IGNORE INTO messages (connection_id, chat_id, message_id, from_user_id,
                    from_user_name, text, media_type, file_id, date)
                SELECT connection_id, chat_id, message_id, from_user_id, from_user_name,
                       text, media_type, file_id, date
                FROM old.messages WHERE connection_id IS NOT NULL""")
            await db.execute("""
                INSERT OR IGNORE INTO chats (connection_id, chat_id, title)
                SELECT m.connection_id, m.chat_id, MAX(m.from_user_name)
                FROM old.messages m JOIN old.connections c USING (connection_id)
                WHERE m.from_user_id != c.user_id GROUP BY m.connection_id, m.chat_id""")
            await db.commit()
            log.info("Перенесено сообщений из %s: %s", src, cur.rowcount)
        except Exception as e:
            log.error("Перенос из %s не удался: %s", src, e)
            await db.rollback()
        finally:
            await db.execute("DETACH DATABASE old")

# ==================== ЗАПУСК ====================

async def main():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA synchronous = NORMAL")
    await db.executescript(SCHEMA)
    await db.create_function("pylower", 1, lambda s: s.lower() if s else "", deterministic=True)
    await migrate_old_db()

    runner = web.AppRunner(make_web_app())
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    log.info("Web: http://%s:%s", WEB_HOST, WEB_PORT)

    if WEBAPP_URL:  # кнопка меню слева от поля ввода
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="История", web_app=WebAppInfo(url=WEBAPP_URL)))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())
