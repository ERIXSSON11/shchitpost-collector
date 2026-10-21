import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
POLL_HOURS = int(os.getenv("POLL_HOURS", "6"))
DB_PATH = os.getenv("DB_PATH", "videos.db")

HASHTAGS = {
    "щитпост": "https://www.tiktok.com/tag/%D1%89%D0%B8%D1%82%D0%BF%D0%BE%D1%81%D1%82",
    "щп": "https://www.tiktok.com/tag/%D1%89%D0%BF",
}
VIDEO_RE = re.compile(r'https?://(?:www\.)?tiktok\.com/@[^/\s"<>]+/video/\d+')

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shchitpost-bot")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in your hosting environment.")
if OWNER_ID <= 0:
    raise RuntimeError("Set OWNER_ID to your numeric Telegram user ID.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scan_lock = asyncio.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            url TEXT PRIMARY KEY,
            hashtag TEXT NOT NULL,
            found_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)


async def scan_hashtag(session: aiohttp.ClientSession, tag: str, page_url: str):
    """Best-effort HTML scan. TikTok may block or change this page at any time."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(page_url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status != 200:
                log.warning("TikTok returned HTTP %s for #%s", resp.status, tag)
                return []
            html = await resp.text(errors="ignore")
    except Exception as exc:
        log.warning("Could not fetch #%s: %s", tag, exc)
        return []

    # Normalize escaped slashes commonly found in embedded page data.
    html = html.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    found = list(dict.fromkeys(VIDEO_RE.findall(html)))
    return [(url.rstrip(".,)"), tag) for url in found]


async def scan_and_send(chat_id: int):
    async with scan_lock:
        await bot.send_message(
            chat_id,
            "🔎 Проверяю доступные страницы хештегов. TikTok может ограничивать автоматический просмотр."
        )
        timeout = aiohttp.ClientTimeout(total=35)
        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            discovered = []
            for tag, page in HASHTAGS.items():
                discovered.extend(await scan_hashtag(session, tag, page))

        conn = db()
        new_items = []
        now = datetime.now(timezone.utc).isoformat()
        for url, tag in discovered:
            try:
                conn.execute(
                    "INSERT INTO videos(url, hashtag, found_at) VALUES (?, ?, ?)",
                    (url, tag, now),
                )
                new_items.append((url, tag))
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        conn.close()

        if not discovered:
            await bot.send_message(
                chat_id,
                "Не удалось получить ссылки с хештег-страниц в этот раз. Это не обязательно значит, что роликов нет: TikTok часто блокирует автоматический просмотр. Попробуй позже или пришли ссылки вручную командой /add."
            )
            return

        if not new_items:
            await bot.send_message(
                chat_id,
                f"Проверка завершена: найдено ссылок на странице — {len(discovered)}, новых — 0. Повторов нет."
            )
            return

        await bot.send_message(chat_id, f"Нашёл новых ссылок: {len(new_items)}")
        for i in range(0, len(new_items), 8):
            chunk = new_items[i:i + 8]
            text = "\n\n".join(f"#{tag}\n{url}" for url, tag in chunk)
            await bot.send_message(chat_id, text, disable_web_page_preview=True)


@dp.message(Command("start"))
async def start(message: Message):
    if not is_owner(message):
        await message.answer("Этот бот приватный.")
        return
    await message.answer(
        "Привет! Я собираю доступные ссылки TikTok по #щитпост и #щп.\n\n"
        "/scan — проверить хештеги сейчас\n"
        "/add ССЫЛКА — сохранить ссылку вручную\n"
        "/latest — показать последние сохранённые ссылки\n"
        "/count — количество сохранённых ссылок\n"
        "/help — помощь\n\n"
        "Автосканирование выполняется периодически, пока сервис запущен. "
        "Сбор зависит от доступности страниц TikTok."
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not is_owner(message):
        return
    await message.answer(
        "/scan — сканировать страницы хештегов\n"
        "/add https://www.tiktok.com/@user/video/123 — сохранить ссылку\n"
        "/latest — последние 10 ссылок\n"
        "/count — сколько ссылок сохранено\n\n"
        "Бот собирает ссылки, а не гарантирует скачивание всех видео без водяного знака. "
        "Скачивай только ролики, на которые у тебя есть разрешение."
    )


@dp.message(Command("scan"))
async def scan_cmd(message: Message):
    if not is_owner(message):
        return
    await scan_and_send(message.chat.id)


@dp.message(Command("add"))
async def add_cmd(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /add https://www.tiktok.com/@user/video/123")
        return
    url = parts[1].strip()
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"} or "/video/" not in parsed.path:
        await message.answer("Не похоже на ссылку TikTok-видео. Пришли полную ссылку на ролик.")
        return
    conn = db()
    try:
        conn.execute(
            "INSERT INTO videos(url, hashtag, found_at) VALUES (?, ?, ?)",
            (url, "manual", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        await message.answer("Ссылка сохранена 👍")
    except sqlite3.IntegrityError:
        await message.answer("Эта ссылка уже есть в базе.")
    finally:
        conn.close()


@dp.message(Command("latest"))
async def latest_cmd(message: Message):
    if not is_owner(message):
        return
    conn = db()
    rows = conn.execute(
        "SELECT url, hashtag, found_at FROM videos ORDER BY found_at DESC LIMIT 10"
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("Пока ссылок нет. Запусти /scan или добавь ссылку через /add.")
        return
    lines = [f"#{tag}\n{url}" for url, tag, _ in rows]
    await message.answer("\n\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("count"))
async def count_cmd(message: Message):
    if not is_owner(message):
        return
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    conn.close()
    await message.answer(f"В базе сохранено ссылок: {total}")


async def scheduler():
    # First scan after startup, then repeat every POLL_HOURS.
    await asyncio.sleep(15)
    while True:
        try:
            await scan_and_send(OWNER_ID)
        except Exception:
            log.exception("Scheduled scan failed")
        await asyncio.sleep(max(POLL_HOURS, 1) * 3600)


async def main():
    db().close()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
