import os
import logging
import asyncio
import aiosqlite

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiohttp import web


BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# -------------------------------------
# FSM (онбординг)
# -------------------------------------

class Onboarding(StatesGroup):
    name = State()
    gender = State()
    ntrp = State()
    about = State()


# -------------------------------------
# База данных (SQLite)
# -------------------------------------

DB_PATH = "tennis.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                gender TEXT,
                ntrp REAL,
                about TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()


async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute_fetchone(
            "SELECT * FROM users WHERE telegram_id = ?",
            (tg_id,)
        )
        return row


async def save_user(tg_id: int, username: str, name: str, gender: str, ntrp: float, about: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (telegram_id, username, name, gender, ntrp, about)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                name = excluded.name,
                gender = excluded.gender,
                ntrp = excluded.ntrp,
                about = excluded.about;
        """, (tg_id, username, name, gender, ntrp, about))
        await db.commit()


# -------------------------------------
# Хендлеры
# -------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    user = await get_user(tg_id)

    if user:
        await message.answer(
            "Привет! 👋\n"
            "Я тебя помню. Хочешь посмотреть свой профиль? Напиши /me 🎾"
        )
        await state.clear()
        return

    await message.answer(
        "Привет! 👋\n"
        "Я помогу тебе находить соперников по теннису.\n"
        "Для начала давай заполним мини-анкету.\n\n"
        "Как тебя подписать?"
    )
    await state.set_state(Onboarding.name)


@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(name=name)

    await message.answer(
        "Окей 👍\nКакой у тебя пол?\n\n"
        "Напиши: мужской / женский / не указывать"
    )
    await state.set_state(Onboarding.gender)


@dp.message(Onboarding.gender)
async def onboarding_gender(message: Message, state: FSMContext):
    gender = message.text.strip().lower()

    if gender in ("мужской", "м"):
        gender = "male"
    elif gender in ("женский", "ж"):
        gender = "female"
    elif gender in ("не указывать", "не скажу", "нет"):
        gender = None
    else:
        gender = "other"

    await state.update_data(gender=gender)

    await message.answer(
        "Отлично!\n\n"
        "Теперь оцени свой уровень по шкале NTRP (1.0–7.0).\n"
        "Например: 2.5 или 4.0"
    )
    await state.set_state(Onboarding.ntrp)


@dp.message(Onboarding.ntrp)
async def onboarding_ntrp(message: Message, state: FSMContext):
    raw = message.text.strip().replace(",", ".")
    try:
        ntrp = float(raw)
    except:await message.answer("Нужно число, например 2.5 или 4.0 🙂")
        return

    await state.update_data(ntrp=ntrp)

    await message.answer(
        "Последний шаг — расскажи немного о себе.\n"
        "Например: как давно играешь, что ищешь.\n"
        "Или напиши «пропустить»."
    )
    await state.set_state(Onboarding.about)


@dp.message(Onboarding.about)
async def onboarding_about(message: Message, state: FSMContext):
    about = message.text.strip()
    if about.lower() in ("пропустить", "skip"):
        about = None

    data = await state.get_data()
    await state.clear()

    tg_id = message.from_user.id
    username = message.from_user.username

    await save_user(
        tg_id=tg_id,
        username=username,
        name=data["name"],
        gender=data["gender"],
        ntrp=data["ntrp"],
        about=about
    )

    await message.answer(
        "Готово! 🎾\n"
        "Твой профиль сохранён.\n\n"
        "Посмотреть профиль: /me"
    )


@dp.message(Command("me"))
async def cmd_me(message: Message):
    user = await get_user(message.from_user.id)

    if not user:
        await message.answer("Ты ещё не проходил анкету. Напиши /start.")
        return

    text = (
        f"Твой профиль:\n\n"
        f"Имя: {user['name']}\n"
        f"Пол: {user['gender'] or 'не указан'}\n"
        f"NTRP: {user['ntrp']}\n"
        f"О себе: {user['about'] or '—'}"
    )

    await message.answer(text)


# -------------------------------------
# Web-сервер для Render
# -------------------------------------

async def handle_root(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", handle_root)
    port = int(os.getenv("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    while True:
        await asyncio.sleep(3600)


async def main():
    await init_db()
    await asyncio.gather(
        dp.start_polling(bot),
        run_web()
    )


if name == "__main__":
    asyncio.run(main())
