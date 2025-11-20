import os
import re
import asyncio
import logging
from datetime import date, timedelta
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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# -----------------------------------------
# Настройки
# -----------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

DB_PATH = "tennis.db"

# ID админа, куда будут прилетать обращения по /help
ADMIN_CHAT_ID = 199804073

MIN_AGE = 18
# Верхнюю границу явно не показываем как ограничение сервиса,
# но отсеиваем совсем нереалистичные даты > 100 лет
MAX_REALISTIC_AGE = 100

# Ограничение на создание матчей: не в прошлом и не дальше чем на 3 месяца вперёд
MAX_MATCH_DAYS_AHEAD = 90

# Сколько матчей показывать за раз в списке /games
GAMES_PAGE_SIZE = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# -----------------------------------------
# FSM анкеты, редактирования, поддержки, матчей, просмотра матчей
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


class EditProfile(StatesGroup):
    choose_field = State()
    name = State()
    gender = State()
    city = State()
    birth_date = State()
    home_courts = State()
    about = State()
    photo = State()


class HelpState(StatesGroup):
    waiting_text = State()


class NewGame(StatesGroup):
    court = State()
    date_choice = State()
    date_manual = State()
    time = State()
    game_type = State()
    rating_limit_choice = State()
    rating_min = State()
    rating_max = State()
    players_count = State()
    court_booking = State()
    privacy = State()
    comment = State()


class GamesFilter(StatesGroup):
    date_choice = State()
    date_manual = State()
    time = State()
    home_only = State()

# -----------------------------------------
# Хелперы
# -----------------------------------------

def calculate_age_from_str(birth_date_str: str) -> Optional[int]:
    """
    birth_date_str: 'ДД.ММ.ГГГГ'
    Возвращает возраст в полных годах или None, если дата некорректна.
    """
    try:
        day, month, year = map(int, birth_date_str.split("."))
        dob = date(year, month, day)
    except ValueError:
        return None

    today = date.today()
    age = (
        today.year
        - dob.year
        - ((today.month, today.day) < (dob.month, dob.day))
    )
    return age


def parse_time(text: str) -> Optional[str]:
    """
    Ожидаем формат ЧЧ:ММ (24 часа). Возвращаем нормализованную строку 'HH:MM' или None.
    """
    text = text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", text):
        return None
    try:
        hh, mm = map(int, text.split(":"))
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def parse_ntrp_from_button(text: str) -> Optional[float]:
    if not text:
        return None
    head = text.split("—", 1)[0].strip()
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


def parse_rating_value(text: str) -> Optional[float]:
    """
    Парсим значение рейтинга из кнопок вида '1.0', '1.5', '2.0', ... '7.0'
    """
    if not text:
        return None
    t = text.replace(",", ".").strip()
    try:
        val = float(t)
    except ValueError:
        return None
    if val < 1.0 or val > 7.0:
        return None
    return round(val, 2)


def normalize_custom_ntrp(value: float) -> float:
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
    mod = 0.0
    pe = (play_experience or "").lower()
    m6 = (matches_6m or "").lower()
    fit = (fitness or "").lower()
    tour = (tournaments or "").lower()

    # Как давно играл
    if "никогда" in pe:
        mod -= 0.25
    elif "в этом году" in pe:
        mod += 0.10
    elif "более года" in pe:
        mod -= 0.05
    elif "более пяти" in pe:
        mod -= 0.15

    # Матчи за 6 месяцев
    if "0–10" in m6 or "0-10" in m6:
        mod += 0.0
    elif "10–100" in m6 or "10-100" in m6:
        mod += 0.15
    elif "100" in m6:
        mod += 0.25

    # Физподготовка
    if "низкая" in fit:
        mod -= 0.15
    elif "хорошая" in fit:
        mod += 0.0
    elif "отличная" in fit:
        mod += 0.10

    # Турниры
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
# Клавиатуры
# -----------------------------------------

gender_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Мужчина"), KeyboardButton(text="Женщина")],
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

edit_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Имя"), KeyboardButton(text="Пол")],
        [KeyboardButton(text="Город"), KeyboardButton(text="Дата рождения")],
        [KeyboardButton(text="Домашние корты")],
        [KeyboardButton(text="О себе"), KeyboardButton(text="Фото")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

HOME_DONE = "Готово ✅"
HOME_SKIP = "Пропустить"

def build_home_courts_kb(courts: List[aiosqlite.Row]) -> ReplyKeyboardMarkup:
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
        [KeyboardButton(text=HOME_DONE), KeyboardButton(text=HOME_SKIP)]
    )

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def build_courts_single_kb(courts: List[aiosqlite.Row]) -> ReplyKeyboardMarkup:
    """
    Клавиатура для выбора одного корта (создание матча).
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

    buttons.append([KeyboardButton(text="Отмена")])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=True,
    )

# Кнопки выбора даты матча (создание игры)
date_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сегодня")],
        [KeyboardButton(text="Завтра")],
        [KeyboardButton(text="Ввести дату")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Кнопки выбора даты для фильтра /games
games_date_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
        [KeyboardButton(text="Ввести дату"), KeyboardButton(text="Все даты")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

games_home_only_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Да")],
        [KeyboardButton(text="Нет")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Тип игры
game_type_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Тренировка")],
        [KeyboardButton(text="Матч на рейтинг")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Ограничение по рейтингу
rating_limit_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Да"), KeyboardButton(text="Без ограничений")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Значения рейтинга для диапазона (1.0, 1.5, ..., 7.0)
rating_values = [f"{x / 2:.1f}" for x in range(2, 15)]  # 1.0..7.0

def build_rating_kb() -> ReplyKeyboardMarkup:
    row = []
    rows = []
    for i, val in enumerate(rating_values, start=1):
        row.append(KeyboardButton(text=val))
        if i % 4 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
    )

# Кол-во игроков
players_count_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="2 игрока")],
        [KeyboardButton(text="4 игрока")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Бронь корта
court_booking_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Корт уже забронирован")],
        [KeyboardButton(text="Корт пока не забронирован")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Приватность
privacy_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Публичный матч")],
        [KeyboardButton(text="Приватный матч")],
    ],
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
                ntrp REAL,
                ntrp_self REAL,
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
        # старые записи могли иметь NULL — считаем их активными
        await db.execute("UPDATE courts SET is_active = 1 WHERE is_active IS NULL;")
        await seed_courts_if_empty(db)

        # games
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                court_id INTEGER NOT NULL,
                match_date TEXT NOT NULL,
                match_time TEXT NOT NULL,
                game_type TEXT NOT NULL,
                rating_min REAL,
                rating_max REAL,
                players_count INTEGER NOT NULL,
                comment TEXT,
                is_court_booked INTEGER DEFAULT 0,
                visibility TEXT DEFAULT 'public',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await _ensure_games_columns(db)

        # user_home_courts
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

        await db.commit()


async def seed_courts_if_empty(db: aiosqlite.Connection):
    """
    Если таблица courts пустая – читаем courts_seed_big.sql и заполняем её.
    """
    cursor = await db.execute("SELECT COUNT(*) FROM courts;")
    row = await cursor.fetchone()
    await cursor.close()
    count = row[0] if row is not None else 0

    if count > 0:
        return

    sql_path = os.path.join(os.path.dirname(__file__), "courts_seed_big.sql")
    try:
        with open(sql_path, "r", encoding="utf-8") as f:
            sql_script = f.read()
        await db.executescript(sql_script)
        logging.info("Courts seeded from courts_seed_big.sql")
    except FileNotFoundError:
        logging.warning(
            "courts_seed_big.sql not found, courts table will stay empty."
        )
    except Exception as e:
        logging.exception("Failed to seed courts from courts_seed_big.sql: %s", e)


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


async def _ensure_games_columns(db: aiosqlite.Connection):
    cursor = await db.execute("PRAGMA table_info(games);")
    cols = await cursor.fetchall()
    await cursor.close()
    existing = {c[1] for c in cols}

    needed = {
        "is_court_booked": "INTEGER DEFAULT 0",
        "visibility": "TEXT DEFAULT 'public'",
        "is_active": "INTEGER DEFAULT 1",
    }

    for col, coltype in needed.items():
        if col not in existing:
            await db.execute(f"ALTER TABLE games ADD COLUMN {col} {coltype};")


async def get_active_courts() -> List[aiosqlite.Row]:
    """
    Возвращаем все «активные» корты.
    Важно: старые записи могли иметь is_active = NULL,
    поэтому считаем COALESCE(is_active, 1) = 1.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, short_name, address
            FROM courts
            WHERE COALESCE(is_active, 1) = 1
            ORDER BY short_name;
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def get_court_by_id(court_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM courts WHERE id = ?;",
            (court_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


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


async def get_user_home_courts(tg_id: int) -> List[aiosqlite.Row]:
    """
    Возвращает список домашних кортов пользователя:
    rows с полями short_name, address
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT c.short_name, c.address
            FROM user_home_courts uh
            JOIN courts c ON c.id = uh.court_id
            WHERE uh.telegram_id = ?
            ORDER BY c.short_name;
            """,
            (tg_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def get_user_home_court_ids(tg_id: int) -> List[int]:
    """
    Возвращает список ID домашних кортов пользователя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT court_id FROM user_home_courts WHERE telegram_id = ?;",
            (tg_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [r[0] for r in rows] if rows else []


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


async def delete_user(tg_id: int):
    """
    Удаляет пользователя и его домашние корты.
    Нужен для /reset, чтобы можно было пройти онбординг заново.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_home_courts WHERE telegram_id = ?;",
            (tg_id,),
        )
        await db.execute(
            "DELETE FROM users WHERE telegram_id = ?;",
            (tg_id,),
        )
        await db.commit()


async def create_game(
    creator_id: int,
    court_id: int,
    match_date: str,
    match_time: str,
    game_type: str,
    rating_min: Optional[float],
    rating_max: Optional[float],
    players_count: int,
    comment: Optional[str],
    is_court_booked: bool,
    visibility: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO games (
                creator_id, court_id, match_date, match_time,
                game_type, rating_min, rating_max,
                players_count, comment,
                is_court_booked, visibility, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1);
            """,
            (
                creator_id,
                court_id,
                match_date,
                match_time,
                game_type,
                rating_min,
                rating_max,
                players_count,
                comment,
                1 if is_court_booked else 0,
                visibility,
            ),
        )
        cursor = await db.execute("SELECT last_insert_rowid();")
        row = await cursor.fetchone()
        await cursor.close()
        await db.commit()
        return row[0]


async def get_game_with_court(game_id: int) -> Optional[aiosqlite.Row]:
    """
    Возвращает матч вместе с информацией о корте.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT g.*, c.short_name AS court_name, c.address
            FROM games g
            JOIN courts c ON c.id = g.court_id
            WHERE g.id = ?;
            """,
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def load_games_page(
    viewer_id: int,
    date_filter: Optional[str],
    time_from: Optional[str],
    home_only: bool,
    offset: int,
    limit: int = GAMES_PAGE_SIZE,
):
    """
    Загружает страницу матчей с фильтрами.
    Возвращает (rows, has_more, next_offset)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        conditions = ["g.is_active = 1", "g.visibility = 'public'"]
        params: List = []

        if date_filter:
            conditions.append("g.match_date = ?")
            params.append(date_filter)

        if time_from:
            conditions.append("g.match_time >= ?")
            params.append(time_from)

        if home_only:
            conditions.append(
                "g.court_id IN (SELECT court_id FROM user_home_courts WHERE telegram_id = ?)"
            )
            params.append(viewer_id)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
            SELECT g.*, c.short_name AS court_name, c.address
            FROM games g
            JOIN courts c ON c.id = g.court_id
            {where_clause}
            ORDER BY g.match_date, g.match_time, g.id
            LIMIT ? OFFSET ?;
        """
        params.extend([limit + 1, offset])

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_offset = offset + limit

        return rows, has_more, next_offset


def build_game_card_text(row: aiosqlite.Row) -> str:
    """
    Формирует текст карточки матча.
    На карточку выводим всё, что указывали при создании,
    КРОМЕ приватности.
    """
    game_id = row["id"]
    match_date = row["match_date"]
    match_time = row["match_time"]
    game_type = row["game_type"]
    court_name = row["court_name"]
    address = row["address"] or "Адрес не указан"
    players_count = row["players_count"]
    comment = row["comment"] or "—"
    rating_min = row["rating_min"]
    rating_max = row["rating_max"]
    is_court_booked = bool(row["is_court_booked"])

    if rating_min is not None and rating_max is not None:
        rating_text = f"{rating_min:.2f}-{rating_max:.2f}"
    else:
        rating_text = "Без ограничений"

    booking_text = "корт забронирован" if is_court_booked else "корт не забронирован"

    txt = (
        f"🎾 <b>Матч #{game_id}</b>\n\n"
        f"Тип: {game_type}\n"
        f"Дата: {match_date}\n"
        f"Время: {match_time}\n"
        f"Корт: {court_name} — <i>📍 {address}</i>\n"
        f"Игроков: {players_count}\n"
        f"Ограничение по рейтингу: {rating_text}\n"
        f"Бронь корта: {booking_text}\n"
        f"Комментарий: {comment}"
    )
    return txt

# -----------------------------------------
# Хэндлеры: старт, профиль, reset, edit, help, onboarding, newgame, games
# -----------------------------------------

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if user:
        await state.clear()
        await message.answer(
            "Привет 👋\n"
            "Ты уже проходил анкету.\n\n"
            "Команды:\n"
            "/start — начать онбординг / показать меню\n"
            "/me — показать мой профиль\n"
            "/edit — изменить профиль\n"
            "/reset — сбросить анкету и пройти заново\n"
            "/newgame — создать новую игру\n"
            "/games — посмотреть доступные матчи\n"
            "/help — написать в поддержку",
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

    home_courts = await get_user_home_courts(message.from_user.id)

    lines = [
        "📋 <b>Твой профиль</b>\n",
        f"Имя: {user['name']}",
        f"Пол: {user['gender'] or 'не указан'}",
        f"Город: {user['city'] or 'не указан'}",
        f"Рейтинг NTRP: {user['ntrp'] or '—'}",
        f"Дата рождения: {user['birth_date'] or '—'}",
        f"О себе: {user['about'] or '—'}",
    ]

    if home_courts:
        lines.append("")
        lines.append("Домашние корты:")
        for row in home_courts:
            addr = row["address"] or "Адрес не указан"
            lines.append(f"• {row['short_name']} — <i>📍 {addr}</i>")
    else:
        lines.append("")
        lines.append("Домашние корты: не выбраны")

    txt = "\n".join(lines)

    if user["photo_file_id"]:
        await message.answer_photo(
            photo=user["photo_file_id"],
            caption=txt,
            parse_mode="HTML",
        )
    else:
        await message.answer(txt, parse_mode="HTML")


@dp.message(F.text == "/reset")
async def reset_cmd(message: Message, state: FSMContext):
    await state.clear()
    await delete_user(message.from_user.id)
    await message.answer(
        "Я сбросил твою анкету и данные профиля.\n\n"
        "Теперь можно пройти всё заново — жми /start 🙂",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.text == "/edit")
async def edit_cmd(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Пока у тебя нет профиля.\nСначала пройди анкету через /start 🙂"
        )
        return

    await state.clear()
    await state.set_state(EditProfile.choose_field)
    await message.answer(
        "Что хочешь изменить?",
        reply_markup=edit_menu_kb,
    )

# ---------- Редактор профиля ----------

@dp.message(EditProfile.choose_field)
async def edit_choose_field(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Имя":
        await state.set_state(EditProfile.name)
        await message.answer(
            "Введи новое имя:",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif text == "Пол":
        await state.set_state(EditProfile.gender)
        await message.answer(
            "Выбери пол:",
            reply_markup=gender_kb,
        )

    elif text == "Город":
        await state.set_state(EditProfile.city)
        await message.answer(
            "Напиши новый город, в котором ты обычно играешь:",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif text == "Дата рождения":
        await state.set_state(EditProfile.birth_date)
        await message.answer(
            "Введи новую дату рождения в формате ДД.ММ.ГГГГ\n"
            "Например: 31.12.1990",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif text == "Домашние корты":
        courts = await get_active_courts()
        if not courts:
            await message.answer(
                "Пока нет доступных кортов для выбора. Обратись к админу.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await state.clear()
            return

        await state.update_data(home_courts=[])
        await state.set_state(EditProfile.home_courts)
        await message.answer(
            "Выбери один или несколько домашних кортов.\n"
            "Нажимай по кнопкам, чтобы добавить/убрать корт.\n"
            "Когда закончишь, нажми «Готово ✅». Если не хочешь менять — «Пропустить».",
            reply_markup=build_home_courts_kb(courts),
        )

    elif text == "О себе":
        await state.set_state(EditProfile.about)
        await message.answer(
            "Напиши новый текст «о себе».\n"
            "Если передумаешь — отправь слово «Пропустить».",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif text == "Фото":
        await state.set_state(EditProfile.photo)
        await message.answer(
            "Отправь новое фото для профиля 📷\n"
            "Или отправь «Пропустить», если не хочешь менять.",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif text == "Отмена":
        await state.clear()
        await message.answer(
            "Окей, ничего не меняем 🙂",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:
        await message.answer(
            "Пожалуйста, выбери один из вариантов на клавиатуре 🙂",
            reply_markup=edit_menu_kb,
        )


@dp.message(EditProfile.name)
async def edit_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Попробуй ещё раз 🙂")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET name = ? WHERE telegram_id = ?;",
            (name, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"Имя обновлено: {name}\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(EditProfile.gender)
async def edit_gender(message: Message, state: FSMContext):
    gender_raw = (message.text or "").strip().lower()

    if gender_raw.startswith("муж"):
        gender = "Мужчина"
    elif gender_raw.startswith("жен"):
        gender = "Женщина"
    else:
        await message.answer("Пожалуйста, выбери один из вариантов на клавиатуре 🙂")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET gender = ? WHERE telegram_id = ?;",
            (gender, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"Пол обновлён: {gender}\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(EditProfile.city)
async def edit_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not city:
        await message.answer("Нужно указать город текстом. Попробуй ещё раз 🙂")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET city = ? WHERE telegram_id = ?;",
            (city, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"Город обновлён: {city}\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(EditProfile.birth_date)
async def edit_birth_date(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        await message.answer(
            "Не похоже на дату 😅\n"
            "Нужен формат ДД.ММ.ГГГГ, например: 31.12.1990",
        )
        return

    age = calculate_age_from_str(text)
    if age is None:
        await message.answer(
            "Не получилось обработать дату рождения.\n"
            "Проверь формат и попробуй ещё раз."
        )
        return

    if age < MIN_AGE:
        await message.answer(
            "Наш сервис доступен только для лиц, достигших 18-летнего возраста.\n"
            "Проверь дату рождения и введи её ещё раз."
        )
        return

    if age > MAX_REALISTIC_AGE:
        await message.answer(
            "Выглядит так, что дата не настоящая.\n"
            "Проверь дату рождения и введи её ещё раз."
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET birth_date = ? WHERE telegram_id = ?;",
            (text, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        "Дата рождения обновлена ✅\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(EditProfile.home_courts)
async def edit_home_courts(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    selected_ids: List[int] = data.get("home_courts", []) or []

    courts = await get_active_courts()
    name_to_id = {c["short_name"]: c["id"] for c in courts}
    name_to_addr = {c["short_name"]: c["address"] for c in courts}

    if text == HOME_SKIP:
        # Ничего не меняем
        await state.clear()
        await message.answer(
            "Домашние корты оставлены без изменений.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == HOME_DONE:
        await save_user_home_courts(message.from_user.id, selected_ids)
        await state.clear()
        if selected_ids:
            id_to_name = {c["id"]: c["short_name"] for c in courts}
            chosen_names = [id_to_name.get(cid, str(cid)) for cid in selected_ids]
            summary = "Твои домашние корты обновлены: " + ", ".join(chosen_names)
        else:
            summary = "Ты не выбрал ни одного домашнего корта."
        await message.answer(
            summary + "\n\nПосмотреть профиль → /me",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text not in name_to_id:
        await message.answer(
            "Пожалуйста, выбери корт из списка или нажми «Готово ✅» / «Пропустить».",
            reply_markup=build_home_courts_kb(courts),
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

    id_to_name = {c["id"]: c["short_name"] for c in courts}
    if selected_ids:
        chosen_names = [id_to_name.get(x, str(x)) for x in selected_ids]
        selected_str = "Сейчас выбрано: " + ", ".join(chosen_names)
    else:
        selected_str = "Сейчас ничего не выбрано."

    address = name_to_addr.get(text) or "Адрес не указан"

    await message.answer(
        f"Я {action} «{text}» в список домашних кортов.\n"
        f"<i>Адрес: 📍 {address}</i>\n\n"
        f"{selected_str}\n\n"
        f"Можешь выбрать ещё или нажать «{HOME_DONE}», когда закончишь.",
        reply_markup=build_home_courts_kb(courts),
        parse_mode="HTML",
    )


@dp.message(EditProfile.about)
async def edit_about(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text.lower().startswith("пропус"):
        about = None
    else:
        about = text

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET about = ? WHERE telegram_id = ?;",
            (about, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        "Текст «о себе» обновлён ✅\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(EditProfile.photo)
async def edit_photo(message: Message, state: FSMContext):
    if message.text and message.text.strip().lower().startswith("пропус"):
        photo_file_id = None
    elif message.photo:
        photo_file_id = message.photo[-1].file_id
    else:
        await message.answer("Пожалуйста, отправь фото или «Пропустить» 🙂")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET photo_file_id = ? WHERE telegram_id = ?;",
            (photo_file_id, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        "Фото профиля обновлено ✅\n\n"
        "Посмотреть профиль → /me",
        reply_markup=ReplyKeyboardRemove(),
    )

# ---------- Поддержка: /help ----------

@dp.message(F.text == "/help")
async def help_cmd(message: Message, state: FSMContext):
    if not ADMIN_CHAT_ID:
        await message.answer(
            "Пока поддержка не настроена 🛠\n"
            "Админ ещё не указал свой ID."
        )
        return

    await state.clear()
    await state.set_state(HelpState.waiting_text)
    await message.answer(
        "Напиши в одном сообщении, что случилось или какой вопрос.\n"
        "Я передам это админу 🙂",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(HelpState.waiting_text)
async def help_text_handler(message: Message, state: FSMContext):
    if not ADMIN_CHAT_ID:
        await state.clear()
        await message.answer(
            "Пока поддержка не настроена 🛠\n"
            "Админ ещё не указал свой ID."
        )
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужно написать текст обращения 🙂 Попробуй ещё раз.")
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "—"
    full_name = message.from_user.full_name or "—"
    user_id = message.from_user.id

    admin_text = (
        "🆘 Новое обращение в поддержку\n\n"
        f"От: {full_name}\n"
        f"Username: {username}\n"
        f"Telegram ID: {user_id}\n\n"
        f"Текст обращения:\n{text}"
    )

    try:
        await bot.send_message(int(ADMIN_CHAT_ID), admin_text)
    except Exception as e:
        logger.exception("Failed to send help message to admin: %s", e)
        await message.answer(
            "Не получилось отправить сообщение админу 😔\n"
            "Попробуй позже или напиши ему напрямую, если знаешь контакт."
        )
        await state.clear()
        return

    await state.clear()
    await message.answer(
        "Спасибо! Я передал твоё сообщение админу 💬\n"
        "Если нужно, он свяжется с тобой в Телеграме.",
    )

# -----------------------------------------
# Онбординг
# -----------------------------------------

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
        city = text
        manual = data.get("city_manual", False)

    await state.update_data(city=city, city_manual=manual)

    courts = await get_active_courts()
    if not courts:
        await message.answer(
            "Пока я не знаю теннисные корты в этом городе, пропускаем этот шаг.\n"
            "Позже админ добавит список кортов.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.update_data(home_courts=[])
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

    # Пропустить
    if text == HOME_SKIP:
        await state.update_data(home_courts=[])
        await message.answer(
            "Окей, пока без домашних кортов.\n\n"
            "Теперь давай оценим твой уровень по шкале NTRP.",
            reply_markup=ntrp_kb,
        )
        await state.set_state(Onboarding.ntrp)
        return

    # Готово
    if text == HOME_DONE:
        courts = await get_active_courts()
        id_to_name = {c["id"]: c["short_name"] for c in courts}
        if selected_ids:
            chosen_names = [id_to_name.get(cid, str(cid)) for cid in selected_ids]
            summary = "Твои домашние корты: " + ", ".join(chosen_names)
        else:
            summary = "Ты не выбрал ни одного домашнего корта."

    ...