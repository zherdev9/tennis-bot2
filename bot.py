import os
import logging
import asyncio
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

# -------------------------------------------------
# Настройки
# -------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

DB_PATH = "tennis.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# -------------------------------------------------
# FSM для онбординга
# -------------------------------------------------

class Onboarding(StatesGroup):
    name = State()
    gender = State()
    ntrp = State()
    about = State()


# -------------------------------------------------
# Тексты для NTRP
# -------------------------------------------------

NTRP_DESCRIPTION = (
    "Шкала NTRP (1.0–7.0):\n\n"
    "1.0–1.5 — только начинаю, учусь стабильно попадать по мячу.\n"
    "2.0–2.5 — умею держать розыгрыш с партнёром, иногда задаю направление.\n"
    "3.0–3.5 — могу контролировать длину и направление, есть базовая тактика.\n"
    "4.0–4.5 — уверенные удары справа/слева, умею менять темп и глубину.\n"
    "5.0+ — сильный игрок, стабильная техника, опыт турниров.\n"
)


# -------------------------------------------------
# Работа с базой (SQLite + aiosqlite)
# -------------------------------------------------

async def init_db():
    """Создаём таблицу, если её ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username    TEXT,
                name        TEXT,
                gender      TEXT,
                ntrp        REAL,
                about       TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.commit()


async def get_user(tg_id: int) -> Optional[aiosqlite.Row]:
    """Вернуть пользователя по telegram_id или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def upsert_user(
    tg_id: int,
    username: Optional[str],
    name: str,
    gender: Optional[str],
    ntrp: float,
    about: Optional[str],
) -> None:
    """Создать или обновить пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, username, name, gender, ntrp, about)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                name     = excluded.name,
                gender   = excluded.gender,
                ntrp     = excluded.ntrp,
                about    = excluded.about;
            """,
            (tg_id, username, name, gender, ntrp, about),
        )
        await db.commit()


# -------------------------------------------------
# Хендлеры
# -------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    user = await get_user(tg_id)

    # Пользователь уже есть — не гоняем по анкете второй раз
    if user:
        await state.clear()
        await message.answer(
            "Привет 👋\n"
            "Я тебя уже знаю. Можешь посмотреть свой профиль командой /me 🎾"
        )
        return

    # Новый пользователь — запускаем онбординг
    await message.answer(
        "Привет 👋\n"
        "Я теннис-бот. Помогаю находить соперников и вести рейтинг NTRP.\n\n""Давай познакомимся — это займёт минуту.\n\n"
        "Как тебя подписывать? (имя или ник)"
    )
    await state.set_state(Onboarding.name)


@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Нужно что-то написать 🙂 Попробуй ещё раз.")
        return

    await state.update_data(name=name)

    await message.answer(
        "Принято 👍\n\n"
        "Какой у тебя пол?\n"
        "Напиши: <b>мужской</b>, <b>женский</b> или <b>не указывать</b>.",
        parse_mode="HTML",
    )
    await state.set_state(Onboarding.gender)


@dp.message(Onboarding.gender)
async def onboarding_gender(message: Message, state: FSMContext):
    raw = message.text.strip().lower()

    if raw in ("мужской", "м", "male"):
        gender = "male"
    elif raw in ("женский", "ж", "female"):
        gender = "female"
    elif raw in ("не указывать", "не скажу", "нет"):
        gender = None
    else:
        # Если ввели что-то странное — не ругаемся, просто ставим other
        gender = "other"

    await state.update_data(gender=gender)

    await message.answer(
        "Теперь про уровень игры 🎾\n\n"
        + NTRP_DESCRIPTION
        + "\n\nВведи своё число по шкале NTRP (например: 2.5 или 4.0)."
    )
    await state.set_state(Onboarding.ntrp)


@dp.message(Onboarding.ntrp)
async def onboarding_ntrp(message: Message, state: FSMContext):
    raw = message.text.strip().replace(",", ".")
    try:
        ntrp = float(raw)
    except ValueError:
        await message.answer("Нужно число, например 2.5 или 4.0 🙂")
        return

    # Лёгкая валидация диапазона
    if not (1.0 <= ntrp <= 7.0):
        await message.answer("Шкала NTRP от 1.0 до 7.0. Попробуй ещё раз 🙂")
        return

    await state.update_data(ntrp=ntrp)

    await message.answer(
        "Супер 🙌\n"
        "Последний вопрос — расскажи чуть-чуть о себе.\n\n"
        "Например: как давно играешь, какие корты удобны, когда обычно можешь.\n"
        "Если не хочешь писать, просто отправь «пропустить»."
    )
    await state.set_state(Onboarding.about)


@dp.message(Onboarding.about)
async def onboarding_about(message: Message, state: FSMContext):
    about_raw = message.text.strip()
    if about_raw.lower() in ("пропустить", "skip"):
        about = None
    else:
        about = about_raw

    data = await state.get_data()
    await state.clear()

    tg_id = message.from_user.id
    username = message.from_user.username

    await upsert_user(
        tg_id=tg_id,
        username=username,
        name=data["name"],
        gender=data["gender"],
        ntrp=data["ntrp"],
        about=about,
    )

    await message.answer(
        "Готово, анкета сохранена 🎾\n\n"
        "Посмотреть свой профиль можно командой /me.\n"
        "Позже здесь появится поиск соперников и матчи."
    )


@dp.message(Command("me"))
async def cmd_me(message: Message):
    user = await get_user(message.from_user.id)

    if not user:
        await message.answer(
            "Похоже, ты ещё не проходил анкету.\n"
            "Напиши /start — познакомимся 🙂"
        )
        return

    gender_map = {
        None: "не указан",
        "male": "мужской",
        "female": "женский",
        "other": "другое",
    }
    gender_text = gender_map.get(user["gender"], "не указан")

    about = user["about"] or "—"

    text = (
        "📋 <b>Твой профиль</b>\n\n"
        f"Имя: {user['name']}\n"
        f"Пол: {gender_text}\n"
        f"NTRP: {user['ntrp']}\n"
        f"О себе: {about}"
    )

    await message.answer(text, parse_mode="HTML")


# -------------------------------------------------
# Точка входа
# -------------------------------------------------

async def main():
    await init_db()
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
