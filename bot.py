import os
import asyncio
import logging

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)


# -----------------------------------------
# Настройки
# -----------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

DB_PATH = "tennis.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# -----------------------------------------
# FSM анкеты
# -----------------------------------------

class Onboarding(StatesGroup):
    name = State()
    gender = State()
    city = State()
    ntrp = State()
    about = State()


# -----------------------------------------
# Клавиатуры
# -----------------------------------------

gender_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Мужской"), KeyboardButton(text="Женский")],
        [KeyboardButton(text="Не указывать")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

city_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Москва")],
        [KeyboardButton(text="Другой город"), KeyboardButton(text="Пропустить")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

ntrp_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="1.0"), KeyboardButton(text="1.5"), KeyboardButton(text="2.0")],
        [KeyboardButton(text="2.5"), KeyboardButton(text="3.0"), KeyboardButton(text="3.5")],
        [KeyboardButton(text="4.0"), KeyboardButton(text="4.5"), KeyboardButton(text="5.0")],
        [KeyboardButton(text="5.5"), KeyboardButton(text="6.0"), KeyboardButton(text="6.5")],
        [KeyboardButton(text="7.0"), KeyboardButton(text="Другое значение")],
    ],
    resize_keyboard=True
)

skip_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Пропустить")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# -----------------------------------------
# База данных
# -----------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                gender TEXT,
                city TEXT,
                ntrp REAL,
                about TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()


async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (tg_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def upsert_user(tg_id, username, name, gender, city, ntrp, about):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (telegram_id, username, name, gender, city, ntrp, about)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name,
                gender=excluded.gender,
                city=excluded.city,
                ntrp=excluded.ntrp,
                about=excluded.about
        """, (tg_id, username, name, gender, city, ntrp, about))
        await db.commit()


# -----------------------------------------
# Анкета
# -----------------------------------------

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if user:
        await state.clear()
        await message.answer(
            "Привет 👋\n"
            "Ты уже прошёл анкету.\n""Посмотреть профиль → /me"
        )
        return

    await message.answer(
        "Привет 👋\nМеня зовут TennisBot.\n"
        "Сейчас я за минуту помогу настроить твой профиль.\n\n"
        "Как тебя подписывать?",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Onboarding.name)


@dp.message(Onboarding.name)
async def get_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(name=name)

    await message.answer("Выбери пол:", reply_markup=gender_kb)
    await state.set_state(Onboarding.gender)


@dp.message(Onboarding.gender)
async def get_gender(message: Message, state: FSMContext):
    gender_raw = message.text.lower()
    if gender_raw.startswith("муж"):
        gender = "male"
    elif gender_raw.startswith("жен"):
        gender = "female"
    elif gender_raw.startswith("не"):
        gender = None
    else:
        gender = "other"

    await state.update_data(gender=gender)

    await message.answer(
        "В каком городе ты играешь?\nПока только Москва 😊",
        reply_markup=city_kb
    )
    await state.set_state(Onboarding.city)


@dp.message(Onboarding.city)
async def get_city(message: Message, state: FSMContext):
    raw = message.text.lower()

    if raw.startswith("моск"):
        city = "Москва"
    elif raw.startswith("друг"):
        city = "Другой город"
    elif raw.startswith("пропус"):
        city = None
    else:
        city = message.text

    await state.update_data(city=city)

    await message.answer(
        "Оцени свой уровень по шкале NTRP:",
        reply_markup=ntrp_kb
    )
    await state.set_state(Onboarding.ntrp)


@dp.message(Onboarding.ntrp)
async def get_ntrp(message: Message, state: FSMContext):
    raw = message.text.replace(",", ".").strip()

    if raw.lower().startswith("другое"):
        await message.answer("Введи число, например: 3.0 или 4.5")
        return

    try:
        ntrp = float(raw)
    except ValueError:
        await message.answer("Это не похоже на число 🤔 Попробуй ещё раз.")
        return

    await state.update_data(ntrp=ntrp)

    await message.answer(
        "Напиши немного о себе или нажми «Пропустить»",
        reply_markup=skip_kb
    )
    await state.set_state(Onboarding.about)


@dp.message(Onboarding.about)
async def get_about(message: Message, state: FSMContext):
    about = message.text
    if about.lower().startswith("пропус"):
        about = None

    data = await state.get_data()
    await state.clear()

    await upsert_user(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        name=data["name"],
        gender=data["gender"],
        city=data["city"],
        ntrp=data["ntrp"],
        about=about,
    )

    await message.answer("Профиль сохранён! 🎾\nПосмотреть → /me")


# -----------------------------------------
# Профиль
# -----------------------------------------

@dp.message(F.text == "/me")
async def profile_cmd(message: Message):
    user = await get_user(message.from_user.id)

    if not user:
        await message.answer("Ты ещё не проходил анкету. Жми /start")
        return

    txt = (
        "📋 <b>Твой профиль</b>\n\n"
        f"Имя: {user['name']}\n"
        f"Пол: {user['gender'] or 'не указан'}\n"
        f"Город: {user['city'] or 'не указан'}\n"
        f"NTRP: {user['ntrp']}\n"
        f"О себе: {user['about'] or '—'}"
    )

    await message.answer(txt, parse_mode="HTML")


# -----------------------------------------
# HTTP сервер, чтобы Render не ругался
# -----------------------------------------

async def handle_root(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_root)
    port = int(os.getenv("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # держим сервер
    while True:
        await asyncio.sleep(3600)


# -----------------------------------------
# MAIN
# -----------------------------------------

async def main():
    await init_db()
    await asyncio.gather(
        dp.start_polling(bot),
        start_web()
    )

if __name__ == "__main__":
    asyncio.run(main())

