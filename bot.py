import os
import logging
import asyncio

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from aiohttp import web

# --------------------
# Конфиг
# --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set")


# --------------------
# Логирование
# --------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


# --------------------
# Aiogram
# --------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Пул подключений к БД будет лежать в bot["db_pool"]
DB_POOL_KEY = "db_pool"


class Onboarding(StatesGroup):
    name = State()
    gender = State()
    ntrp = State()
    about_me = State()


# --------------------
# Работа с БД
# --------------------

async def init_db_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:
        # Создаём таблицу пользователей, если её ещё нет
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id           SERIAL PRIMARY KEY,
                telegram_id  BIGINT UNIQUE NOT NULL,
                username     TEXT,
                name         TEXT NOT NULL,
                gender       TEXT,
                ntrp         NUMERIC(3,2),
                about_me     TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

    logger.info("DB initialized")
    return pool


async def get_user_by_telegram_id(pool: asyncpg.Pool, telegram_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            telegram_id,
        )


async def create_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    name: str,
    gender: str | None,
    ntrp: float | None,
    about_me: str | None,
):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, name, gender, ntrp, about_me)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username,
                name     = EXCLUDED.name,
                gender   = EXCLUDED.gender,
                ntrp     = EXCLUDED.ntrp,
                about_me = EXCLUDED.about_me;
            """,
            telegram_id,
            username,
            name,
            gender,
            ntrp,
            about_me,
        )


# --------------------
# Хендлеры
# --------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    pool: asyncpg.Pool = message.bot[DB_POOL_KEY]
    tg_id = message.from_user.id

    user = await get_user_by_telegram_id(pool, tg_id)

    if user:
        # Уже есть профиль
        text = (
            "Привет ещё раз! 👋\n"
            "У тебя уже есть профиль в теннис-боте 🎾\n\n"
            "Посмотреть его можно командой /me.\n"
            "Скоро я научусь подбирать тебе соперников по NTRP."
        )
        await message.answer(text)
        await state.clear()
        return

    # Новый пользователь — запускаем онбординг
    await message.answer(
        "Привет! 👋\n"
        "Я помогу тебе находить соперников по теннису в Москве 🎾\n\n"
        "Для начала давай заполним мини-анкету.\n\n"
        "Как тебя подписать? Напиши имя или ник."
    )
    await state.set_state(Onboarding.name)


@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.
answer("Нужно что-то написать 🙂 Попробуй ещё раз.")
        return

    await state.update_data(name=name)

    text = (
        "Окей, запомнил 👍\n\n"
        "К какому полу тебе комфортнее себя отнести?\n"
        "Напиши одним словом: <b>мужской</b>, <b>женский</b> или <b>другое</b>.\n"
        "Если не хочешь указывать — просто напиши «не указывать»."
    )
    await message.answer(text)
    await state.set_state(Onboarding.gender)


@dp.message(Onboarding.gender)
async def onboarding_gender(message: Message, state: FSMContext):
    raw = (message.text or "").strip().lower()
    if raw in ("мужской", "м", "male"):
        gender = "male"
    elif raw in ("женский", "ж", "female"):
        gender = "female"
    elif raw in ("не указывать", "не скажу", "неважно", "никак"):
        gender = None
    else:
        gender = "other"

    await state.update_data(gender=gender)

    text = (
        "Теперь важное 🤓\n\n"
        "Мы используем шкалу NTRP (1.0–7.0).\n"
        "Напиши число, которое лучше всего описывает твой уровень.\n\n"
        "Примеры:\n"
        "• 2.0 – только начинаю, мяч держится 2–3 удара\n"
        "• 3.0 – стабильный любитель, могу держать розыгрыш\n"
        "• 4.0 – хорошо контролирую направление и глубину\n"
        "• 5.0+ – турнирный уровень\n\n"
        "Если сомневаешься — лучше немного занизить уровень, "
        "бот потом скорректирует по результатам матчей 😉"
    )
    await message.answer(text)
    await state.set_state(Onboarding.ntrp)


@dp.message(Onboarding.ntrp)
async def onboarding_ntrp(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        ntrp = float(raw)
    except ValueError:
        await message.answer("Хочется видеть число вроде 2.5 или 4.0 🙂 Попробуй ещё раз.")
        return

    if not (1.0 <= ntrp <= 7.0):
        await message.answer("Шкала NTRP от 1.0 до 7.0. Введи значение в этом диапазоне.")
        return

    await state.update_data(ntrp=ntrp)

    await message.answer(
        "Круто, записал твой стартовый NTRP 🎾\n\n"
        "Напиши пару слов о себе: как давно играешь, что ищешь (спарринг, турниры, тренировки).\n"
        "Если не хочешь писать — просто отправь «пропустить»."
    )
    await state.set_state(Onboarding.about_me)


@dp.message(Onboarding.about_me)
async def onboarding_about_me(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    about_me = None
    if text.lower() not in ("пропустить", "skip", "нет", "ничего"):
        about_me = text

    data = await state.get_data()
    await state.clear()

    pool: asyncpg.Pool = message.bot[DB_POOL_KEY]
    tg_id = message.from_user.id
    username = message.from_user.username

    await create_user(
        pool=pool,
        telegram_id=tg_id,
        username=username,
        name=data["name"],
        gender=data.get("gender"),
        ntrp=data.get("ntrp"),
        about_me=about_me,
    )

    await message.answer(
        "Готово! ✅ Профиль создан.\n\n"
        "Позже здесь появятся команды:\n"
        "• /me — показать профиль\n"
        "• /find — поиск соперников\n"
        "• /new — создать матч\n\n"
        "Пока можешь просто написать /start ещё раз и убедиться, что я помню тебя 😉"
    )


@dp.message(Command("me"))
async def cmd_me(message: Message):
    pool: asyncpg.Pool = message.bot[DB_POOL_KEY]
    tg_id = message.from_user.id
    user = await get_user_by_telegram_id(pool, tg_id)
    if not user:
        await message.answer
