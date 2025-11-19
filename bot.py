import os
import re
import asyncio
import logging
from typing import List, Optional

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
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# -----------------------------------------
# Заглушка: стартовый список кортов Москвы
# (админ может потом добавлять свои записи в таблицу courts через SQL)
# -----------------------------------------

COURTS_SEED = [
    ("luzhniki", "Лужники", "Теннис в Лужниках", "Москва, ул. Лужники, 24", "Лужники / Спортивная"),
    ("multisport_luzhniki", "MultiSport Лужники", "Мультиспорт Лужники", "Москва, ул. Лужники, 24, стр. 10", "Лужники / Спортивная"),
    ("zhemchuzhina_krylatskoe", "Жемчужина", "Жемчужина (Крылатское)", "Москва, Крылатская ул., 10", "Крылатское"),
    ("proclub_lobachevskogo", "ProClub", "Теннисный клуб ProClub", "Москва, ул. Лобачевского, 120А", "ЮЗАО"),
    ("tennis_mafia", "Tennis Mafia", "Теннисный клуб Tennis Mafia", "Москва, ул. Академика Волгина, 33А", "Коньково"),
    ("soyuz_sport", "Soyuz Sport", "Теннисный центр Soyuz Sport", "Москва, ул. Академика Волгина, 33А", "ЮЗАО"),
    ("lucenter", "LuCenter", "LuCenter Tennis Club", "Москва, Старокирочный пер., 2", "Бауманская"),
    ("cooltennis_baumanskaya", "CoolTennis", "Теннисный клуб CoolTennis", "Москва, Спартаковская пл., 16/15, стр. 6", "Бауманская"),
    ("sportventure", "Sportventure", "Sportventure Moscow", "Москва, Краснопресненская наб., 14, стр. 1", "ЦАО"),
    ("cska_tennis", "ЦСКА", "Теннисный центр ЦСКА", "Москва, Ленинградский пр-т, 39, стр. 3", "Сокол / Динамо"),
    ("sokolniki_spartak", "Спартак Сокольники", "Теннисный центр «Спартак»", "Москва, Майская аллея, 7с6", "Сокольники"),
    ("itc_wegim", "ITC Wegim", "ITC by Wegim", "Москва, ул. Авиаконструктора Миля, 4А", "Некрасовка"),
    ("tennis_capital_vdnh", "Tennis Capital", "Tennis Capital ВДНХ", "Москва, пр-т Мира, 119, стр. 22", "ВДНХ"),
    ("lawn_tennis_club", "Lawn Tennis", "Lawn Tennis Club", "Москва, Котляковская ул., 3с1", "Варшавская"),
    ("sk_champion_medvedkovo", "Чемпион", "СК «Чемпион»", "Москва, Олонецкий пр., 5к1А", "Медведково"),
]

# -----------------------------------------
# FSM анкеты
# -----------------------------------------

class Onboarding(StatesGroup):
    name = State()
    gender = State()
    city = State()
    home_courts = State()
    ntrp = State()
    play_experience = State()
    matches_6m = State()
    fitness = State()
    tournaments = State()
    birth_date = State()
    about = State()
    photo = State()

# -----------------------------------------
# Клавиатуры
# -----------------------------------------

gender_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Мужчина"), KeyboardButton(text="Женщина")],
        [KeyboardButton(text="Не указывать")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

city_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Москва")],
        [KeyboardButton(text="Другой город")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Кнопки NTRP с короткими, но содержательными описаниями
ntrp_kb = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="1.0 — полностью начинающий"),
            KeyboardButton(text="1.5 — держу мяч, но нестабильно"),
        ],
        [
            KeyboardButton(text="2.0 — базовые удары, мало стабильности"),
            KeyboardButton(text="2.5 — контролирую направление, короткие розыгрыши"),
        ],
        [
            KeyboardButton(text="3.0 — стабильность в среднем темпе"),
            KeyboardButton(text="3.5 — направление и глубина на хорошем уровне"),
        ],
        [
            KeyboardButton(text="4.0 — уверенный темп, вариативность ударов"),
            KeyboardButton(text="4.5 — сила, подкрутка, меняю тактику"),
        ],
        [
            KeyboardButton(text="5.0 — уверенная глубина, сложные удары"),
            KeyboardButton(text="5.5 — мощь и стабильность в быстром темпе"),
        ],
        [
            KeyboardButton(text="6.0–7.0 — элитный/профессиональный уровень"),
        ],
        [
            KeyboardButton(text="Ввести свой уровень (например: 3.25)"),
        ],
    ],
    resize_keyboard=True,
)

play_experience_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Нет, никогда")],
        [KeyboardButton(text="Да, в этом году")],
        [KeyboardButton(text="Да, более года назад")],
        [KeyboardButton(text="Да, более пяти лет назад")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

matches_6m_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="0–10 матчей")],
        [KeyboardButton(text="10–100 матчей")],
        [KeyboardButton(text="100 и более")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

fitness_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Низкая")],
        [KeyboardButton(text="Хорошая")],
        [KeyboardButton(text="Отличная")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

tournaments_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Не участвовал")],
        [KeyboardButton(text="Tour")],
        [KeyboardButton(text="Masters")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

skip_about_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Пропустить")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

def build_home_courts_kb(courts: List[aiosqlite.Row]) -> ReplyKeyboardMarkup:
    """
    Строим клавиатуру для выбора домашних кортов из таблицы courts.
    """
    buttons: List[List[KeyboardButton]] = []
    row: List[KeyboardButton] = []

    for i, court in enumerate(courts, start=1):
        row.append(KeyboardButton(text=court["short_name"]))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append(
        [KeyboardButton(text="Готово ✅"), KeyboardButton(text="Пропустить")]
    )

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=True,
    )

# -----------------------------------------
# База данных
# -----------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # users
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                gender TEXT,
                city TEXT,
                ntrp REAL,          -- текущий рейтинг
                ntrp_self REAL,     -- самооценка
                play_experience TEXT,
                matches_6m TEXT,
                fitness TEXT,
                tournaments TEXT,
                birth_date TEXT,
                about TEXT,
                photo_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await _ensure_user_columns(db)

        # courts
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS courts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE,
                short_name TEXT NOT NULL,
                full_name TEXT,
                address TEXT,
                area TEXT,
                is_active INTEGER DEFAULT 1
            );
            """
        )

        # связь пользователь ↔ домашние корты
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_home_courts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                court_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        await seed_courts_if_empty(db)
        await db.commit()


async def _ensure_user_columns(db: aiosqlite.Connection):
    cursor = await db.execute("PRAGMA table_info(users);")
    cols = await cursor.fetchall()
    await cursor.close()
    existing = {c[1] for c in cols}

    needed = {
        "username": "TEXT",
        "name": "TEXT",
        "gender": "TEXT",
        "city": "TEXT",
        "ntrp": "REAL",
        "ntrp_self": "REAL",
        "play_experience": "TEXT",
        "matches_6m": "TEXT",
        "fitness": "TEXT",
        "tournaments": "TEXT",
        "birth_date": "TEXT",
        "about": "TEXT",
        "photo_file_id": "TEXT",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    }

    for col, coltype in needed.items():
        if col not in existing:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype};")


async def seed_courts_if_empty(db: aiosqlite.Connection):
    cursor = await db.execute("SELECT COUNT(*) FROM courts;")
    row = await cursor.fetchone()
    await cursor.close()
    count = row[0] if row is not None else 0

    if count > 0:
        return

    await db.executemany(
        """
        INSERT INTO courts (slug, short_name, full_name, address, area, is_active)
        VALUES (?, ?, ?, ?, ?, 1);
        """,
        COURTS_SEED,
    )


async def get_active_courts() -> List[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, short_name FROM courts WHERE is_active = 1 ORDER BY short_name;"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def save_user_home_courts(telegram_id: int, court_ids: List[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_home_courts WHERE telegram_id = ?;",
            (telegram_id,),
        )
        if court_ids:
            await db.executemany(
                "INSERT INTO user_home_courts (telegram_id, court_id) VALUES (?, ?);",
                [(telegram_id, cid) for cid in court_ids],
            )
        await db.commit()


async def get_user(tg_id: int):
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
    name: Optional[str],
    gender: Optional[str],
    city: Optional[str],
    ntrp: Optional[float],
    ntrp_self: Optional[float],
    play_experience: Optional[str],
    matches_6m: Optional[str],
    fitness: Optional[str],
    tournaments: Optional[str],
    birth_date: Optional[str],
    about: Optional[str],
    photo_file_id: Optional[str],
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (
                telegram_id, username, name, gender, city,
                ntrp, ntrp_self,
                play_experience, matches_6m, fitness, tournaments, birth_date,
                about, photo_file_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username        = excluded.username,
                name            = excluded.name,
                gender          = excluded.gender,
                city            = excluded.city,
                ntrp            = excluded.ntrp,
                ntrp_self       = excluded.ntrp_self,
                play_experience = excluded.play_experience,
                matches_6m      = excluded.matches_6m,
                fitness         = excluded.fitness,
                tournaments     = excluded.tournaments,
                birth_date      = excluded.birth_date,
                about           = excluded.about,
                photo_file_id   = excluded.photo_file_id;
            """,
            (
                tg_id,
                username,
                name,
                gender,
                city,
                ntrp,
                ntrp_self,
                play_experience,
                matches_6m,
                fitness,
                tournaments,
                birth_date,
                about,
                photo_file_id,
            ),
        )
        await db.commit()

# -----------------------------------------
# Логика рейтинга
# -----------------------------------------

def parse_ntrp_from_button(text: str) -> Optional[float]:
    """
    Парсим NTRP из текста кнопки.
    Ожидаем форматы типа:
      '3.0 — ...'
      '6.0–7.0 — ...' -> берём 6.0
    """
    if not text:
        return None

    head = text.split("—", 1)[0].strip()  # до длинного тире
    head = head.replace(" ", "")

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


def normalize_custom_ntrp(value: float) -> float:
    """
    Ограничиваем [1.0; 7.0] и округляем до двух знаков.
    """
    if value < 1.0:
        value = 1.0
    if value > 7.0:
        value = 7.0
    return round(value, 2)


def compute_final_ntrp(
    base_ntrp: float,
    play_experience: Optional[str],
    matches_6m: Optional[str],
    fitness: Optional[str],
    tournaments: Optional[str],
) -> float:
    """
    Новая модель с меньшими модификаторами:
    - максимум реальный прирост ≈ +0.75
    - без жёсткого обрезания сверху, всё зашито в коэффициенты.
    """
    mod = 0.0
    pe = (play_experience or "").lower()
    m6 = (matches_6m or "").lower()
    fit = (fitness or "").lower()
    tour = (tournaments or "").lower()

    # 1. Как давно играл в большой теннис?
    if "никогда" in pe:
        mod -= 0.25
    elif "в этом году" in pe:
        mod += 0.10
    elif "более года" in pe:
        mod -= 0.05
    elif "более пяти" in pe:
        mod -= 0.15

    # 2. Матчи за 6 месяцев
    if "0–10" in m6 or "0-10" in m6:
        mod += 0.0
    elif "10–100" in m6 or "10-100" in m6:
        mod += 0.15
    elif "100" in m6:
        mod += 0.25

    # 3. Физподготовка
    if "низкая" in fit:
        mod -= 0.15
    elif "хорошая" in fit:
        mod += 0.0
    elif "отличная" in fit:
        mod += 0.10

    # 4. Турниры
    if "tour" in tour:
        mod += 0.15
    elif "masters" in tour:
        mod += 0.30

    final = base_ntrp + mod
    if final < 1.0:
        final = 1.0
    if final > 7.0:
        final = 7.0

    return round(final, 2)

# -----------------------------------------
# Хэндлеры
# -----------------------------------------

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if user:
        await state.clear()
        await message.answer(
            "Привет 👋\n"
            "Ты уже проходил анкету.\n\n"
            "Посмотреть профиль → /me",
        )
        return

    await message.answer(
        "Привет 👋\nМеня зовут TennisBot.\n"
        "Сейчас за пару минут настроим твой теннисный профиль.\n\n"
        "Как тебя подписывать?",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Onboarding.name)


@dp.message(F.text == "/me")
async def profile_cmd(message: Message):
    user = await get_user(message.from_user.id)

    if not user:
        await message.answer("Ты ещё не проходил анкету. Жми /start")
        return

    txt = (
        "📋 <b>Твой профиль</b>\n\n"
        f"Имя: {user['name']}\n"
        f"Пол: {user['gender'] or 'Не указывать'}\n"
        f"Город: {user['city'] or 'не указан'}\n"
        f"Рейтинг NTRP: {user['ntrp'] or '—'}\n"
        f"Опыт игры: {user['play_experience'] or '—'}\n"
        f"Матчей за 6 мес: {user['matches_6m'] or '—'}\n"
        f"Физподготовка: {user['fitness'] or '—'}\n"
        f"Турниры: {user['tournaments'] or '—'}\n"
        f"Дата рождения: {user['birth_date'] or '—'}\n"
        f"О себе: {user['about'] or '—'}"
    )

    if user["photo_file_id"]:
        await message.answer_photo(
            photo=user["photo_file_id"],
            caption=txt,
            parse_mode="HTML",
        )
    else:
        await message.answer(txt, parse_mode="HTML")


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
    gender_raw = (message.text or "").strip().lower()

    if gender_raw.startswith("муж"):
        gender = "Мужчина"
    elif gender_raw.startswith("жен"):
        gender = "Женщина"
    elif gender_raw.startswith("не"):
        gender = "Не указывать"
    else:
        await message.answer("Пожалуйста, выбери один из вариантов на клавиатуре 🙂")
        return

    await state.update_data(gender=gender)

    await message.answer(
        "В каком городе ты обычно играешь?\n"
        "Сейчас фокус на Москве, но можно указать и другой город.",
        reply_markup=city_kb,
    )
    await state.set_state(Onboarding.city)


@dp.message(Onboarding.city)
async def get_city(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()

    # если до этого просили "Другой город" — берём любой текст
    if text == "Москва":
        city = "Москва"
        manual = False
    elif text == "Другой город" and not data.get("city_manual"):
        await state.update_data(city_manual=True)
        await message.answer(
            "Ок, напиши, пожалуйста, свой город текстом.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    else:
        # если уже ожидали ручной город или просто приняли текст
        city = text
        manual = data.get("city_manual", False)

    await state.update_data(city=city, city_manual=manual)

    # Переходим к выбору домашних кортов
    courts = await get_active_courts()
    if not courts:
        await message.answer(
            "Пока я не знаю теннисные корты в этом городе, пропускаем этот шаг.\n"
            "Позже админ добавит список кортов.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.update_data(home_courts=[])
        # Теперь NTRP
        await message.answer(
            "Теперь давай оценим твой уровень по шкале NTRP.",
            reply_markup=ntrp_kb,
        )
        await state.set_state(Onboarding.ntrp)
        return

    await state.update_data(home_courts=[])
    await message.answer(
        "Выбери один или несколько домашних кортов.\n"
        "Нажимай по кнопкам, чтобы добавить/убрать корт.\n"
        "Когда закончишь, нажми «Готово ✅». Если пока неважно – «Пропустить».",
        reply_markup=build_home_courts_kb(courts),
    )
    await state.set_state(Onboarding.home_courts)


@dp.message(Onboarding.home_courts)
async def home_courts_handler(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    selected_ids: List[int] = data.get("home_courts", []) or []

    if text == "Пропустить":
        await state.update_data(home_courts=[])
        await message.answer(
            "Окей, пока без домашних кортов.\n\n"
            "Теперь давай оценим твой уровень по шкале NTRP.",
            reply_markup=ntrp_kb,
        )
        await state.set_state(Onboarding.ntrp)
        return

    if text == "Готово ✅":
        await message.answer(
            "Отлично, двигаемся дальше.\n\n"
            "Теперь давай оценим твой уровень по шкале NTRP.",
            reply_markup=ntrp_kb,
        )
        await state.set_state(Onboarding.ntrp)
        return

    courts = await get_active_courts()
    name_to_id = {c["short_name"]: c["id"] for c in courts}

    if text not in name_to_id:
        await message.answer(
            "Пожалуйста, выбери корт из списка или нажми «Готово ✅» / «Пропустить»."
        )
        return

    cid = name_to_id[text]
    if cid in selected_ids:
        selected_ids.remove(cid)
        action = "убрал"
    else:
        selected_ids.append(cid)
        action = "добавил"

    await state.update_data(home_courts=selected_ids)
    await message.answer(
        f"Я {action} «{text}» в список домашних кортов.\n"
        "Можешь выбрать ещё или нажать «Готово ✅», когда закончишь.",
    )


@dp.message(Onboarding.ntrp)
async def get_ntrp(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    data = await state.get_data()
    waiting_custom = data.get("waiting_custom_ntrp", False)

    if text.startswith("Ввести свой уровень"):
        await state.update_data(waiting_custom_ntrp=True)
        await message.answer(
            "Введи свой уровень NTRP числом от 1.00 до 7.00.\n"
            "Например: 3.25",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if waiting_custom:
        raw = text.replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            await message.answer(
                "Не удалось распознать число 🤔\n"
                "Пример: 3.25",
            )
            return

        value = normalize_custom_ntrp(value)
        await state.update_data(ntrp_self=value, waiting_custom_ntrp=False)
    else:
        base_ntrp = parse_ntrp_from_button(text)
        if base_ntrp is None:
            await message.answer(
                "Выбери уровень по кнопке или нажми «Ввести свой уровень (например: 3.25)».",
                reply_markup=ntrp_kb,
            )
            return
        await state.update_data(ntrp_self=base_ntrp)

    await message.answer(
        "Как давно ты играл в большой теннис?",
        reply_markup=play_experience_kb,
    )
    await state.set_state(Onboarding.play_experience)


@dp.message(Onboarding.play_experience)
async def get_play_experience(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text not in [
        "Нет, никогда",
        "Да, в этом году",
        "Да, более года назад",
        "Да, более пяти лет назад",
    ]:
        await message.answer("Пожалуйста, выбери один из вариантов на клавиатуре 🙂")
        return

    await state.update_data(play_experience=text)

    await message.answer(
        "Сколько матчей ты сыграл за последние 6 месяцев?",
        reply_markup=matches_6m_kb,
    )
    await state.set_state(Onboarding.matches_6m)


@dp.message(Onboarding.matches_6m)
async def get_matches_6m(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text not in [
        "0–10 матчей",
        "10–100 матчей",
        "100 и более",
    ]:
        await message.answer("Пожалуйста, выбери один из вариантов на клавиатуре 🙂")
        return

    await state.update_data(matches_6m=text)

    await message.answer(
        "Оцени свою общую физическую подготовку:",
        reply_markup=fitness_kb,
    )
    await state.set_state(Onboarding.fitness)


@dp.message(Onboarding.fitness)
async def get_fitness(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text not in [
        "Низкая",
        "Хорошая",
        "Отличная",
    ]:
        await message.answer("Пожалуйста, выбери один из вариантов на клавиатуре 🙂")
        return

    await state.update_data(fitness=text)

    await message.answer(
        "Участвовал ли ты в турнирах?\n\n"
        "• Tour — любительские турниры\n"
        "• Masters — более высокий уровень",
        reply_markup=tournaments_kb,
    )
    await state.set_state(Onboarding.tournaments)


@dp.message(Onboarding.tournaments)
async def get_tournaments(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text not in [
        "Не участвовал",
        "Tour",
        "Masters",
    ]:
        await message.answer("Пожалуйста, выбери один из вариантов на клавиатуре 🙂")
        return

    await state.update_data(tournaments=text)

    await message.answer(
        "Укажи дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 31.12.1990",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Onboarding.birth_date)


@dp.message(Onboarding.birth_date)
async def get_birth_date(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        await message.answer(
            "Не похоже на дату 😅\n"
            "Нужен формат ДД.ММ.ГГГГ, например: 31.12.1990",
        )
        return

    await state.update_data(birth_date=text)

    await message.answer(
        "Напиши немного о себе: как играешь и что ищешь.\n"
        "Или нажми «Пропустить».",
        reply_markup=skip_about_kb,
    )
    await state.set_state(Onboarding.about)


@dp.message(Onboarding.about)
async def get_about(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text.lower().startswith("пропус"):
        about = None
    else:
        about = text

    await state.update_data(about=about)

    await message.answer(
        "Финальный штрих — добавь фото для профиля 📷\n\n"
        "Просто отправь фото или нажми «Пропустить».",
        reply_markup=skip_about_kb,
    )
    await state.set_state(Onboarding.photo)


@dp.message(Onboarding.photo)
async def get_photo(message: Message, state: FSMContext):
    if message.text and message.text.strip().lower().startswith("пропус"):
        photo_file_id = None
    elif message.photo:
        photo_file_id = message.photo[-1].file_id
    else:
        await message.answer("Пожалуйста, отправь фото или нажми «Пропустить» 🙂")
        return

    data = await state.get_data()
    await state.clear()

    base_ntrp = data.get("ntrp_self")
    play_experience = data.get("play_experience")
    matches_6m = data.get("matches_6m")
    fitness = data.get("fitness")
    tournaments = data.get("tournaments")

    final_ntrp = compute_final_ntrp(
        base_ntrp=base_ntrp,
        play_experience=play_experience,
        matches_6m=matches_6m,
        fitness=fitness,
        tournaments=tournaments,
    )

    await upsert_user(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        name=data.get("name"),
        gender=data.get("gender"),
        city=data.get("city"),
        ntrp=final_ntrp,
        ntrp_self=base_ntrp,
        play_experience=play_experience,
        matches_6m=matches_6m,
        fitness=fitness,
        tournaments=tournaments,
        birth_date=data.get("birth_date"),
        about=data.get("about"),
        photo_file_id=photo_file_id,
    )

    # сохраняем домашние корты
    home_courts_ids: List[int] = data.get("home_courts", []) or []
    await save_user_home_courts(message.from_user.id, home_courts_ids)

    await message.answer(
        "Профиль сохранён! 🎾\n\n"
        f"Твой текущий рейтинг NTRP: {final_ntrp:.2f}\n\n"
        "Он будет меняться после сыгранных матчей.\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )

# -----------------------------------------
# HTTP-сервер для Render
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

    while True:
        await asyncio.sleep(3600)

# -----------------------------------------
# MAIN
# -----------------------------------------

async def main():
    await init_db()
    await asyncio.gather(
        dp.start_polling(bot),
        start_web(),
    )


if __name__ == "__main__":
    asyncio.run(main())