import os
import logging
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from aiohttp import web


# --- Конфиг ---

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise SystemExit("BOT_TOKEN is not set")


# --- Логирование ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


# --- Aiogram ---

bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋\n"
        "Я теннис-бот. Пока мы в разработке, но я уже живой 🎾\n\n"
        "Скоро здесь появится поиск соперников и рейтинг NTRP."
    )


@dp.message(Command("me"))
async def cmd_me(message: Message):
    await message.answer(
        "Профиль игрока мы подключим в следующем спринте.\n"
        "Сейчас главное — что бот запустился ✅"
    )


@dp.message()
async def fallback(message: Message):
    await message.answer("Я пока только тренируюсь. Напиши /start 🙂")


async def run_bot():
    logger.info("Starting polling...")
    await dp.start_polling(bot)


# --- Простой HTTP-сервер для Render ---

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="OK: tennis bot is running")


async def run_web():
    app = web.Application()
    app.router.add_get("/", handle_root)

    port = int(os.getenv("PORT", "8000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"HTTP server started on port {port}")

    while True:
        await asyncio.sleep(3600)


async def main():
    await asyncio.gather(
        run_bot(),
        run_web(),
    )


if name == "__main__":
    asyncio.run(main())
