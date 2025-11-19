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
    photo = State()   # новый шаг – фото


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

# Кнопки NTRP с описанием навыков
ntrp_kb = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="1.0–1.5: только учусь попадать по мячу"),
            KeyboardButton(text="2.0: держу мяч недолго, розыгрыши короткие"),
        ],
        [
            KeyboardButton(text="2.5: могу держать розыгрыш и задавать направление"),
            KeyboardButton(text="3.0–3.5: контролирую направление и глубину мяча"),
        ],
        [
            KeyboardButton(text="4.0–4.5: уверенно играю, меняю темп и глубину"),
            KeyboardButton(text="5.0–5.5: сильный любитель, опыт матчей/турниров"),
        ],
        [
            KeyboardButton(text="6.0–7.0: очень сильный, почти профи/профи"),
        ],
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
        # Базовое создание таблицы (на случай первого запуска)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                gender TEXT,
                city TEXT,
                ntrp REAL,
                about TEXT,
                photo_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Лёгкая миграция для уже существующей таблицы без photo_file_id
        await _ensure_user_columns(db)
        await db.commit()


async def _ensure_user_columns(db: aiosqlite.Connection):
    cursor = await db.execute("PRAGMA table_info(users);")
    cols = await cursor.fetchall()
    await cursor.close()
    existing = {c[1] for c in cols}  # имя колонки в позиции 1

    if "photo_file_id" not in existing:
        await db.execute("ALTER TABLE users ADD COLUMN photo_file_id TEXT;")
    if "created_at" not in existing:
        await db.execute(
            "ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"
        )


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
        async def upsert_user(tg_id, username, name, gender, city, ntrp, about, photo_file_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (telegram_id, username, name, gender, city, ntrp, about, photo_file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username      = excluded.username,
                name          = excluded.name,
                gender        = excluded.gender,
                city          = excluded.city,
                ntrp          = excluded.ntrp,
                about         = excluded.about,
                photo_file_id = excluded.photo_file_id
        """, (tg_id, username, name, gender, city, ntrp, about, photo_file_id))
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
            "Ты уже прошёл анкету.\n"
            "Посмотреть профиль → /me"
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
    name = (message.text or "").strip()
    if not name:
        await message.answer("Нужно что-то написать 🙂 Попробуй ещё раз.")
        return

    await state.update_data(name=name)

    await message.answer("Выбери пол:", reply_markup=gender_kb)
    await state.set_state(Onboarding.gender)


@dp.message(Onboarding.gender)
async def get_gender(message: Message, state: FSMContext):
    gender_raw = (message.text or "").lower()
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
        "В каком городе ты играешь?\nПока основной фокус — Москва 😊",
        reply_markup=city_kb
    )
    await state.set_state(Onboarding.city)


@dp.message(Onboarding.city)
async def get_city(message: Message, state: FSMContext):
    raw = (message.text or "").lower()

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
        "Теперь выбери свой уровень по шкале NTRP.\n\n"
        "Кнопки ниже — с описанием навыков:\n"
        "умеешь ли держать розыгрыш, задавать направление и глубину мяча и т.д.",
        reply_markup=ntrp_kb
    )
    await state.set_state(Onboarding.ntrp)


def _parse_ntrp_from_button(text: str) -> float | None:
    """
    Парсим NTRP из текста кнопки.
    Ожидаем форматы вроде:
    - '2.5: ...'
    - '3.0–3.5: ...'
    """
    if not text:
        return None

    head = text.split(":", 1)[0].strip()  # '2.5' или '3.0–3.5'
    head = head.replace(" ", "")

    # Диапазон: берём нижнюю границу
    if "–" in head:
        part = head.split("–", 1)[0]
    elif "-" in head:
        part = head.split("-", 1)[0]
    else:
        part = head

    part = part.replace(",", ".")
    try:
        return float(part)
    except ValueError:
        return None


@dp.message(Onboarding.ntrp)
async def get_ntrp(message: Message, state: FSMContext):
    raw = (message.text or "").strip()

    # Сначала пытаемся парсить из текста кнопки
    ntrp = _parse_ntrp_from_button(raw)

    # Если пользователь вдруг ввёл просто число руками
    if ntrp is None:
        try:
            ntrp = float(raw.replace(",", "."))
        except ValueError:
            await message.answer("Это не похоже на уровень NTRP 🤔 Попробуй выбрать кнопку.")
            return

    # Лёгкая валидация
    if not (1.0 <= ntrp <= 7.0):
        await message.answer("Шкала NTRP от 1.0 до 7.0. Выбери из кнопок или введи число в этом диапазоне 🙂")
        return

    await state.update_data(ntrp=ntrp)

    await message.answer(
        "Напиши немного о себе (как играешь, что ищешь) или нажми «Пропустить»",
        reply_markup=skip_kb
    )
    await state.set_state(Onboarding.about)


@dp.message(Onboarding.about)
async def get_about(message: Message, state: FSMContext):
    about_raw = (message.text or "").strip().lower()
    if about_raw.startswith("пропус"):
        about = None
    else:
        about = message.text

    await state.update_data(about=about)

    await message.answer(
        "И финальный штрих — добавь фото для профиля 📷\n\n"
        "Отправь фото или нажми «Пропустить».",
        reply_markup=skip_kb
    )
    await state.set_state(Onboarding.photo)


@dp.message(Onboarding.photo)
async def get_photo(message: Message, state: FSMContext):
    photo_file_id = None

    if message.photo:
        # Берём самое большое по размеру фото (последний элемент)
        photo_file_id = message.photo[-1].file_id
    else:
        text = (message.text or "").lower()
        if not text.startswith("пропус"):
            await message.answer("Отправь фото или нажми «Пропустить» 🙂")
            return

    data = await state.get_data()
    await state.clear()

    await upsert_user(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        name=data["name"],
        gender=data["gender"],
        city=data["city"],
        ntrp=data["ntrp"],
        about=data["about"],
        photo_file_id=photo_file_id,
    )

    await message.answer("Профиль сохранён! 🎾\nПосмотреть → /me", reply_markup=ReplyKeyboardRemove())


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

    if user["photo_file_id"]:
        await message.answer_photo(
            photo=user["photo_file_id"],
            caption=txt,
            parse_mode="HTML"
        )
    else:
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

