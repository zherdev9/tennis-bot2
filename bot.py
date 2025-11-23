import os
import re
import asyncio
import logging
from datetime import date, datetime, timedelta
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

# Кол-во матчей на страницу в /games
GAMES_PAGE_SIZE = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

MOSCOW_UTC_OFFSET = 3  # Москва: UTC+3 без перехода на летнее время


def get_moscow_now() -> datetime:
    """
    Текущее время в Москве (UTC+3), даже если сервер работает в UTC.
    """
    return datetime.utcnow() + timedelta(hours=MOSCOW_UTC_OFFSET)


def get_moscow_today() -> date:
    """
    Текущая дата в Москве.
    """
    return get_moscow_now().date()


# -----------------------------------------
# FSM анкеты, редактирования, поддержки, матчей
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
    creator_mode = State()
    court = State()
    date_choice = State()
    date_manual = State()
    time = State()
    end_time = State()
    payment_type = State()
    game_type = State()
    rating_limit_choice = State()
    rating_min = State()
    rating_max = State()
    players_count = State()
    court_booking = State()
    privacy = State()
    comment = State()


class ViewGames(StatesGroup):
    date_choice = State()
    date_manual = State()
    time_choice = State()
    time_manual = State()
    home_courts_filter = State()
    browsing = State()


class MyGames(StatesGroup):
    main = State()
    created_menu = State()
    waiting_score = State()

# -----------------------------------------
# Хелперы
# -----------------------------------------

def calculate_age_from_str(birth_date_str: str) -> Optional[int]:
    """
    birth_date_str: 'ДД.ММ.ГГГГ'
    Возвращает возраст в полных годах или None, если дата некорректна.
    """
    if not birth_date_str:
        return None
    try:
        day, month, year = map(int, birth_date_str.split("."))
        dob = date(year, month, day)
    except ValueError:
        return None

    today = get_moscow_today()
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
    """Клавиатура выбора домашних кортов с кнопкой «Готово» вверху."""
    buttons: List[List[KeyboardButton]] = []
    row: List[KeyboardButton] = []

    # Сначала строка с «Готово» / «Пропустить»
    buttons.append(
        [KeyboardButton(text=HOME_DONE), KeyboardButton(text=HOME_SKIP)]
    )

    # Затем сами корты по 2 в строке
    for i, court in enumerate(courts, start=1):
        row.append(KeyboardButton(text=court["short_name"]))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

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

# Кнопки выбора даты матча
date_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сегодня")],
        [KeyboardButton(text="Завтра")],
        [KeyboardButton(text="Ввести дату")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

def generate_time_keyboard(match_date_obj: date) -> InlineKeyboardMarkup:
    """Клавиатура времени с шагом 30 минут.
    Для сегодняшней даты скрываются уже прошедшие слоты.
    Если слотов нет (например, уже глубокая ночь) — вернём пустую клавиатуру,
    а логика выше покажет сообщение, что на эту дату матч создать нельзя.
    """
    now = get_moscow_now()
    base = datetime(
        year=match_date_obj.year,
        month=match_date_obj.month,
        day=match_date_obj.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    buttons: list[InlineKeyboardButton] = []

    # Собираем список всех слотов
    for i in range(48):  # 24 часа * 2 слота по 30 минут
        slot_dt = base + timedelta(minutes=30 * i)
        # если это сегодня — не показываем прошедшие слоты
        if match_date_obj == now.date() and slot_dt <= now:
            continue
        label = slot_dt.strftime("%H:%M")
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"newgame_time:{label}",
            )
        )

    # Раскладываем кнопки по рядам по 4 в строке
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for btn in buttons:
        row.append(btn)
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows)

# Клавиатура выбора продолжительности матча
duration_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="30 мин", callback_data="duration:30")],
        [InlineKeyboardButton(text="1 ч", callback_data="duration:60")],
        [InlineKeyboardButton(text="1 ч 30 мин", callback_data="duration:90")],
        [InlineKeyboardButton(text="2 ч", callback_data="duration:120")],
        [InlineKeyboardButton(text="2 ч 30 мин", callback_data="duration:150")],
        [InlineKeyboardButton(text="3 ч", callback_data="duration:180")],
    ],
)


# Режим создания игры
creator_mode_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Создаю матч для себя")],
        [KeyboardButton(text="Создаю матч для других")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Тип оплаты за корт
payment_type_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Делим поровну между всеми игроками")],
        [KeyboardButton(text="Плачу я (организатор)")],
        [KeyboardButton(text="Обсудим в чате")],
        [KeyboardButton(text="Отмена")],
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

# ----- Клавиатуры для /games -----

games_date_filter_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
        [KeyboardButton(text="Все даты")],
        [KeyboardButton(text="Ввести дату")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

games_time_choice_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Без фильтра по времени")],
        [KeyboardButton(text="Утро"), KeyboardButton(text="День")],
        [KeyboardButton(text="Вечер"), KeyboardButton(text="Ночь")],
        [KeyboardButton(text="Назад"), KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

games_home_filter_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Все корты")],
        [KeyboardButton(text="Только домашние корты")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

games_browse_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Показать ещё 10 матчей")],
        [KeyboardButton(text="Закрыть список матчей")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# ----- Клавиатуры для /mygames -----

my_games_main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Предстоящие матчи")],
        [KeyboardButton(text="Завершённые матчи")],
        [KeyboardButton(text="Отменённые матчи")],
        [KeyboardButton(text="Все мои матчи")],
        [KeyboardButton(text="Назад")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


my_games_created_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Предстоящие матчи")],
        [KeyboardButton(text="Завершённые матчи")],
        [KeyboardButton(text="Назад")],
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
                match_end_time TEXT,
                duration_minutes INTEGER,
                game_type TEXT NOT NULL,
                rating_min REAL,
                rating_max REAL,
                players_count INTEGER NOT NULL,
                comment TEXT,
                is_court_booked INTEGER DEFAULT 0,
                visibility TEXT DEFAULT 'public',
                creator_mode TEXT DEFAULT 'self',
                payment_type TEXT,
                is_active INTEGER DEFAULT 1,
                status TEXT DEFAULT 'scheduled',
                score TEXT,
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

        # заявки на матчи
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                applicant_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
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
        "creator_mode": "TEXT DEFAULT 'self'",
        "is_active": "INTEGER DEFAULT 1",
        "status": "TEXT DEFAULT 'scheduled'",
        "score": "TEXT",
        "match_end_time": "TEXT",
        "duration_minutes": "INTEGER",
        "payment_type": "TEXT",
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




async def update_username_only(tg_id: int, username: Optional[str]):
    """Обновляет username в базе для пользователя, если он есть."""
    if username is None:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET username = ? WHERE telegram_id = ?;",
            (username, tg_id),
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
    match_end_time: Optional[str],
    duration_minutes: Optional[int],
    game_type: str,
    rating_min: Optional[float],
    rating_max: Optional[float],
    players_count: int,
    comment: Optional[str],
    is_court_booked: bool,
    visibility: str,
    creator_mode: str = "self",
    payment_type: Optional[str] = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO games (
                creator_id, court_id, match_date, match_time, match_end_time, duration_minutes,
                game_type, rating_min, rating_max,
                players_count, comment,
                is_court_booked, visibility, creator_mode, payment_type, is_active, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'scheduled');
            """,
            (
                creator_id,
                court_id,
                match_date,
                match_time,
                match_end_time,
                duration_minutes,
                game_type,
                rating_min,
                rating_max,
                players_count,
                comment,
                1 if is_court_booked else 0,
                visibility,
                creator_mode,
                payment_type,
            ),
        )
        cursor = await db.execute("SELECT last_insert_rowid();")
        row = await cursor.fetchone()
        await cursor.close()
        await db.commit()
        return row[0]


async def get_game_by_id(game_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT g.*, c.short_name AS court_short_name, c.address AS court_address
            FROM games g
            JOIN courts c ON c.id = g.court_id
            WHERE g.id = ?;
            """,
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def get_game_occupancy(game_id: int) -> tuple[int, int]:
    """
    Возвращает кортеж (занятых мест, всего мест) для матча.
    Организатор матча учитывается как занявший одно место, если creator_mode = 'self'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Общая информация по матчу
        cursor = await db.execute(
            "SELECT players_count, creator_mode FROM games WHERE id = ?;",
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if not row:
            return 0, 0

        players_count = row["players_count"]
        creator_mode = row["creator_mode"]

        # Сколько заявок уже принято
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM game_applications
            WHERE game_id = ?
              AND status = 'accepted';
            """,
            (game_id,),
        )
        accepted_row = await cursor.fetchone()
        await cursor.close()

        accepted_count = accepted_row[0] if accepted_row else 0

        base = 1 if creator_mode == "self" else 0
        occupied = base + accepted_count

        return occupied, players_count


async def get_game_participant_ids(game_id: int, include_creator: bool = True) -> List[int]:
    """
    Возвращает список Telegram ID участников матча.
    Участники = все принятые заявки + организатор (если он играет сам).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT creator_id, creator_mode FROM games WHERE id = ?;",
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if not row:
            return []

        creator_id = row["creator_id"]
        creator_mode = row["creator_mode"]

        participant_ids = set()

        # Организатор считается участником, только если он создавал матч «для себя»
        if include_creator and creator_mode == "self":
            participant_ids.add(creator_id)

        # Все принятые заявки
        cursor = await db.execute(
            """
            SELECT applicant_id
            FROM game_applications
            WHERE game_id = ? AND status = 'accepted';
            """,
            (game_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        for r in rows:
            participant_ids.add(r["applicant_id"])

        return list(participant_ids)


async def get_games_for_listing(
    user_id: int,
    filter_date: Optional[str],
    filter_time_from: Optional[str],
    only_home: bool,
    limit: int,
    offset: int,
) -> List[aiosqlite.Row]:
    """
    Список публичных активных предстоящих матчей с учётом фильтров.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        params: List = []
        sql = """
            SELECT g.*,
                   c.short_name AS court_short_name,
                   c.address AS court_address,
                   u.name AS creator_name,
                   u.ntrp AS creator_ntrp
            FROM games g
            JOIN courts c ON c.id = g.court_id
            LEFT JOIN users u ON u.telegram_id = g.creator_id
            WHERE g.is_active = 1
              AND g.visibility = 'public'
              AND g.status = 'scheduled'
        """

        if filter_date:
            sql += " AND g.match_date = ?"
            params.append(filter_date)

        if filter_time_from:
            if filter_time_from == "morning":
                sql += " AND g.match_time >= ? AND g.match_time <= ?"
                params.extend(["04:00", "10:00"])
            elif filter_time_from == "day":
                sql += " AND g.match_time >= ? AND g.match_time <= ?"
                params.extend(["10:30", "16:00"])
            elif filter_time_from == "evening":
                sql += " AND g.match_time >= ? AND g.match_time <= ?"
                params.extend(["16:30", "23:00"])
            elif filter_time_from == "night":
                sql += " AND (g.match_time >= ? OR g.match_time <= ?)"
                params.extend(["23:30", "03:30"])
            else:
                sql += " AND g.match_time >= ?"
                params.append(filter_time_from)

        if only_home:
            sql += """
              AND g.court_id IN (
                  SELECT court_id
                  FROM user_home_courts
                  WHERE telegram_id = ?
              )
            """
            params.append(user_id)

        sql += """
            ORDER BY g.match_date, g.match_time
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def get_games_created_by_user(
    creator_id: int,
    status: Optional[str] = None,
) -> List[aiosqlite.Row]:
    """
    Матчи, созданные пользователем.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        params: List = [creator_id]
        sql = """
            SELECT g.*,
                   c.short_name AS court_short_name,
                   c.address AS court_address
            FROM games g
            JOIN courts c ON c.id = g.court_id
            WHERE g.creator_id = ?
        """
        if status:
            sql += " AND g.status = ?"
            params.append(status)

        sql += " ORDER BY g.match_date DESC, g.match_time DESC;"
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def get_games_with_user_participation(user_id: int) -> List[aiosqlite.Row]:
    """
    Матчи, где пользователь участвует:
    • есть принятая заявка на матч
    • или он сам создал матч в режиме "Создаю матч для себя" (creator_mode = 'self')
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT g.*,
                   c.short_name AS court_short_name,
                   c.address AS court_address,
                   ga.status AS application_status,
                   u.name AS creator_name,
                   u.ntrp AS creator_ntrp
            FROM games g
            JOIN courts c ON c.id = g.court_id
            LEFT JOIN game_applications ga
              ON ga.game_id = g.id
             AND ga.applicant_id = ?
             AND ga.status = 'accepted'
            LEFT JOIN users u ON u.telegram_id = g.creator_id
            WHERE (ga.id IS NOT NULL)
               OR (g.creator_id = ? AND g.creator_mode = 'self');
            """,
            (user_id, user_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


# -----------------------------------------
# Хэндлеры: старт, профиль, reset, edit, help, newgame, games, mygames
# -----------------------------------------

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await update_username_only(message.from_user.id, message.from_user.username)
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
            "/newgame — создать новый матч\n"
            "/games — посмотреть доступные матчи\n"
            "/mygames — мои матчи\n"
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
    await update_username_only(message.from_user.id, message.from_user.username)
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
    await update_username_only(message.from_user.id, message.from_user.username)
    await state.clear()
    await delete_user(message.from_user.id)
    await message.answer(
        "Я сбросил твою анкету и данные профиля.\n\n"
        "Теперь можно пройти всё заново — жми /start 🙂",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.text == "/edit")
async def edit_cmd(message: Message, state: FSMContext):
    await update_username_only(message.from_user.id, message.from_user.username)
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
    await update_username_only(message.from_user.id, message.from_user.username)
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

        await message.answer(
            summary,
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            "Теперь давай оценим твой уровень по шкале NTRP.",
            reply_markup=ntrp_kb,
        )
        await state.set_state(Onboarding.ntrp)
        return

    # Обычный корт
    courts = await get_active_courts()
    name_to_id = {c["short_name"]: c["id"] for c in courts}
    name_to_addr = {c["short_name"]: c["address"] for c in courts}

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

    base_ntrp_raw = data.get("ntrp_self")
    try:
        base_ntrp = float(base_ntrp_raw) if base_ntrp_raw is not None else 3.0
    except (TypeError, ValueError):
        base_ntrp = 3.0

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
# Создание матча: /newgame
# -----------------------------------------

@dp.message(F.text == "/newgame")
async def newgame_cmd(message: Message, state: FSMContext):
    await update_username_only(message.from_user.id, message.from_user.username)
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Сначала нужно заполнить профиль.\n"
            "Пройди онбординг через /start 🙂"
        )
        return

    courts = await get_active_courts()
    if not courts:
        await message.answer(
            "В базе пока нет ни одного корта. Обратись к админу.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await state.clear()
    await state.update_data(creator_mode="self")
    await state.set_state(NewGame.court)
    await message.answer(
        "Создаём новый матч 🎾\n\n"
        "Выбери корт, на котором планируешь играть:",
        reply_markup=build_courts_single_kb(courts),
    )


@dp.message(NewGame.creator_mode)
async def newgame_creator_mode(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "Отмена":
        await state.clear()
        await message.answer("Создание игры отменено.", reply_markup=ReplyKeyboardRemove())
        return

    if text == "Создаю матч для себя":
        mode = "self"
    elif text == "Создаю матч для других":
        mode = "others"
    else:
        await message.answer(
            "Пожалуйста, выбери один из вариантов на клавиатуре 🙂",
            reply_markup=creator_mode_kb,
        )
        return

    await state.update_data(creator_mode=mode)
    await state.set_state(NewGame.court)

    courts = await get_active_courts()
    if not courts:
        await message.answer(
            "В базе пока нет ни одного корта. Обратись к админу.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    await message.answer(
        "Выбери корт, на котором планируешь играть:",
        reply_markup=build_courts_single_kb(courts),
    )


@dp.message(NewGame.court)
async def newgame_court(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "Отмена":
        await state.clear()
        await message.answer("Создание игры отменено.", reply_markup=ReplyKeyboardRemove())
        return

    courts = await get_active_courts()
    name_to_id = {c["short_name"]: c["id"] for c in courts}

    if text not in name_to_id:
        await message.answer(
            "Пожалуйста, выбери корт из списка или нажми «Отмена».",
            reply_markup=build_courts_single_kb(courts),
        )
        return

    cid = name_to_id[text]
    await state.update_data(court_id=cid, court_name=text)

    await state.set_state(NewGame.date_choice)
    await message.answer(
        "Выбери дату матча:",
        reply_markup=date_choice_kb,
    )


@dp.message(NewGame.date_choice)
async def newgame_date_choice(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    today = get_moscow_today()

    if text == "Сегодня":
        match_date_obj = today
    elif text == "Завтра":
        match_date_obj = today + timedelta(days=1)
    elif text == "Ввести дату":
        await state.set_state(NewGame.date_manual)
        await message.answer(
            "Укажи дату матча в формате ДД.ММ.ГГГГ\n"
            "Например: 25.11.2024",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    else:
        await message.answer(
            "Пожалуйста, выбери один из вариантов: Сегодня, Завтра или Ввести дату 🙂",
            reply_markup=date_choice_kb,
        )
        return

    max_date = today + timedelta(days=MAX_MATCH_DAYS_AHEAD)
    if match_date_obj < today:
        await message.answer(
            "Нельзя создать матч в прошлом.\n"
            "Выбери дату не раньше сегодняшнего дня.",
            reply_markup=date_choice_kb,
        )
        return
    if match_date_obj > max_date:
        await message.answer(
            "Нельзя создавать игры более чем на 3 месяца вперёд.\n"
            "Выбери дату ближе по времени.",
            reply_markup=date_choice_kb,
        )
        return

    match_date_str = match_date_obj.strftime("%d.%m.%Y")
    await state.update_data(match_date=match_date_str)

    # Генерируем клавиатуру времени
    time_kb = generate_time_keyboard(match_date_obj)

    # Если слотов нет (например, уже 23:42 и всё на сегодня прошло)
    if not time_kb.inline_keyboard:
        if match_date_obj == today:
            await message.answer(
                "На сегодня уже нельзя создать матч — все временные слоты прошли.\n\n"
                "Выбери другую дату.",
                reply_markup=date_choice_kb,
            )
        else:
            await message.answer(
                "На выбранную дату уже нельзя создать матч — все временные слоты прошли.\n\n"
                "Выбери другую дату.",
                reply_markup=date_choice_kb,
            )
        return

    await state.set_state(NewGame.time)
    await message.answer(
        f"Дата матча: {match_date_str}\n\n"
        "Выбери время начала матча ⏰",
        reply_markup=time_kb,
    )


@dp.message(NewGame.date_manual)
async def newgame_date_manual(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        await message.answer(
            "Не похоже на дату 😅\n"
            "Нужен формат ДД.ММ.ГГГГ, например: 25.11.2024",
        )
        return

    try:
        day, month, year = map(int, text.split("."))
        match_date_obj = date(year, month, day)
    except ValueError:
        await message.answer(
            "Не получилось разобрать дату.\n"
            "Проверь формат и попробуй ещё раз.",
        )
        return

    today = get_moscow_today()
    max_date = today + timedelta(days=MAX_MATCH_DAYS_AHEAD)

    if match_date_obj < today:
        await message.answer(
            "Нельзя создать матч в прошлом.\n"
            "Выбери дату не раньше сегодняшнего дня.",
        )
        return

    if match_date_obj > max_date:
        await message.answer(
            "Нельзя создавать игры более чем на 3 месяца вперёд.\n"
            "Выбери дату ближе по времени.",
        )
        return

    match_date_str = match_date_obj.strftime("%d.%m.%Y")
    await state.update_data(match_date=match_date_str)

    # Генерируем клавиатуру времени
    time_kb = generate_time_keyboard(match_date_obj)

    # Если на эту дату не осталось свободных слотов
    if not time_kb.inline_keyboard:
        if match_date_obj == today:
            text_msg = (
                "На сегодня уже нельзя создать матч — все временные слоты прошли.\n\n"
                "Выбери другую дату."
            )
        else:
            text_msg = (
                "На выбранную дату уже нельзя создать матч — все временные слоты прошли.\n\n"
                "Выбери другую дату."
            )

        await message.answer(text_msg, reply_markup=date_choice_kb)
        return

    await state.set_state(NewGame.time)
    await message.answer(
        f"Дата матча: {match_date_str}\n\n"
        "Выбери время начала матча ⏰",
        reply_markup=time_kb,
    )



@dp.callback_query(F.data.startswith("newgame_time:"))
async def newgame_time_choice(callback: CallbackQuery, state: FSMContext):
    """Выбор времени начала матча кнопками с шагом 30 минут."""
    time_str = callback.data.split("newgame_time:", 1)[1]

    await state.update_data(match_time=time_str)
    await state.set_state(NewGame.end_time)

    await callback.message.answer(f"Время начала матча: {time_str}")
    await callback.message.answer(
        "Теперь выбери продолжительность матча ⏱",
        reply_markup=duration_kb,
    )
    await callback.answer()

@dp.message(NewGame.time)
async def newgame_time(message: Message, state: FSMContext):
    time_str = parse_time(message.text or "")
    if not time_str:
        await message.answer(
            "Не похоже на время 😅\n"
            "Нужен формат ЧЧ:ММ, например: 19:30",
        )
        return

    await state.update_data(match_time=time_str)

    await state.set_state(NewGame.end_time)
    await message.answer(f"Время начала матча: {time_str}")
    await message.answer(
        "Теперь укажи время окончания матча в формате ЧЧ:ММ.\n"
        "Например: 20:30",
    )




@dp.callback_query(F.data.startswith("duration:"))
async def newgame_duration_choice(callback: CallbackQuery, state: FSMContext):
    """Выбор продолжительности матча после выбора времени начала."""
    data = callback.data or ""
    try:
        _, minutes_str = data.split("duration:", 1)
        duration_minutes = int(minutes_str)
    except Exception:
        await callback.answer("Не удалось распознать продолжительность.", show_alert=True)
        return

    fsm = await state.get_data()
    start_time_str = fsm.get("match_time")
    if not start_time_str:
        await callback.answer("Сначала выбери время начала матча.", show_alert=True)
        return

    try:
        sh, sm = map(int, start_time_str.split(":"))
    except Exception:
        await callback.answer("Время начала указано в неверном формате.", show_alert=True)
        return

    start_total = sh * 60 + sm
    end_total = start_total + duration_minutes
    end_h = (end_total // 60) % 24
    end_m = end_total % 60
    end_time_str = f"{end_h:02d}:{end_m:02d}"

    # Человеческое представление длительности
    hours = duration_minutes // 60
    mins = duration_minutes % 60
    if hours and mins:
        duration_text = f"{hours} ч {mins} мин"
    elif hours:
        duration_text = f"{hours} ч"
    else:
        duration_text = f"{mins} мин"

    await state.update_data(match_end_time=end_time_str, duration_minutes=duration_minutes)
    await state.set_state(NewGame.payment_type)

    await callback.message.answer(
        f"Время матча: {start_time_str}–{end_time_str}\n"
        f"Длительность: {duration_text}",
    )
    await callback.message.answer(
        "💰 Как планируешь делить оплату за корт?\n"
        "Выбери вариант, чтобы игроки сразу всё понимали.",
        reply_markup=payment_type_kb,
    )
    await callback.answer()


@dp.message(NewGame.end_time)
async def newgame_end_time(message: Message, state: FSMContext):
    """Ручной ввод времени окончания матча (запасной вариант)."""
    end_time_str = parse_time(message.text or "")
    if not end_time_str:
        await message.answer(
            "Не похоже на время 😅\n"
            "Нужен формат ЧЧ:ММ, например: 21:30",
        )
        return

    data = await state.get_data()
    start_time_str = data.get("match_time")

    duration_minutes = None
    if start_time_str:
        try:
            sh, sm = map(int, start_time_str.split(":"))
            eh, em = map(int, end_time_str.split(":"))
            start_minutes = sh * 60 + sm
            end_minutes = eh * 60 + em
            if end_minutes <= start_minutes:
                await message.answer(
                    "Время окончания матча должно быть позже времени начала.\n"
                    "Попробуй ещё раз, например: 21:30",
                )
                return
            duration_minutes = end_minutes - start_minutes
        except Exception:
            # На всякий случай, если что-то пошло не так — примем время без проверки
            pass

    # Человеческое представление длительности, если можем посчитать
    if duration_minutes is not None:
        hours = duration_minutes // 60
        mins = duration_minutes % 60
        if hours and mins:
            duration_text = f"{hours} ч {mins} мин"
        elif hours:
            duration_text = f"{hours} ч"
        else:
            duration_text = f"{mins} мин"
    else:
        duration_text = "не указана"

    await state.update_data(match_end_time=end_time_str, duration_minutes=duration_minutes)

    await state.set_state(NewGame.payment_type)
    await message.answer(
        f"Время матча: {start_time_str}–{end_time_str}\n"
        f"Длительность: {duration_text}",
    )
    await message.answer(
        "💰 Как планируешь делить оплату за корт?\n"
        "Выбери вариант, чтобы игроки сразу всё понимали.",
        reply_markup=payment_type_kb,
    )

@dp.message(NewGame.payment_type)
async def newgame_payment_type(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Отмена":
        await state.clear()
        await message.answer("Создание игры отменено.", reply_markup=ReplyKeyboardRemove())
        return

    if text == "Делим поровну между всеми игроками":
        payment_type = "split"
    elif text == "Плачу я (организатор)":
        payment_type = "creator"
    elif text == "Обсудим в чате":
        payment_type = "discuss"
    else:
        await message.answer(
            "Пожалуйста, выбери один из вариантов на клавиатуре 🙂",
            reply_markup=payment_type_kb,
        )
        return

    await state.update_data(payment_type=payment_type)

    await state.set_state(NewGame.game_type)
    await message.answer(
        "Выбери тип матча:",
        reply_markup=game_type_kb,
    )


@dp.message(NewGame.game_type)
async def newgame_game_type(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text not in ["Тренировка", "Матч на рейтинг"]:
        await message.answer(
            "Пожалуйста, выбери один из вариантов: Тренировка или Матч на рейтинг 🙂",
            reply_markup=game_type_kb,
        )
        return

    await state.update_data(game_type=text)

    await state.set_state(NewGame.rating_limit_choice)
    await message.answer(
        "Нужно ли ограничение по рейтингу?\n\n"
        "Если да — дальше выберешь диапазон.\n"
        "Если нет — матч будет доступен для любого уровня.",
        reply_markup=rating_limit_choice_kb,
    )


@dp.message(NewGame.rating_limit_choice)
async def newgame_rating_limit_choice(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Без ограничений":
        await state.update_data(rating_min=None, rating_max=None)
        await state.set_state(NewGame.players_count)
        await message.answer(
            "Сколько игроков планируется?",
            reply_markup=players_count_kb,
        )
        return

    if text == "Да":
        await state.set_state(NewGame.rating_min)
        await message.answer(
            "Выбери минимальный рейтинг игрока:",
            reply_markup=build_rating_kb(),
        )
        return

    await message.answer(
        "Пожалуйста, выбери «Да» или «Без ограничений».",
        reply_markup=rating_limit_choice_kb,
    )


@dp.message(NewGame.rating_min)
async def newgame_rating_min(message: Message, state: FSMContext):
    val = parse_rating_value(message.text or "")
    if val is None:
        await message.answer(
            "Не удалось распознать рейтинг.\n"
            "Выбери значение по кнопке (от 1.0 до 7.0).",
            reply_markup=build_rating_kb(),
        )
        return

    await state.update_data(rating_min=val)

    await state.set_state(NewGame.rating_max)
    await message.answer(
        f"Минимальный рейтинг: {val:.1f}\n"
        "Теперь выбери максимальный рейтинг (не ниже минимального):",
        reply_markup=build_rating_kb(),
    )


@dp.message(NewGame.rating_max)
async def newgame_rating_max(message: Message, state: FSMContext):
    data = await state.get_data()
    rating_min_val = data.get("rating_min")

    val = parse_rating_value(message.text or "")
    if val is None:
        await message.answer(
            "Не удалось распознать рейтинг.\n"
            "Выбери значение по кнопке (от 1.0 до 7.0).",
            reply_markup=build_rating_kb(),
        )
        return

    if rating_min_val is not None and val < rating_min_val:
        await message.answer(
            f"Максимальный рейтинг не может быть меньше минимального ({rating_min_val:.1f}).\n"
            "Попробуй ещё раз.",
                    reply_markup=build_rating_kb(),
        )
        return

    await state.update_data(rating_max=val)

    await state.set_state(NewGame.players_count)
    await message.answer(
        "Сколько игроков планируется?",
        reply_markup=players_count_kb,
    )


@dp.message(NewGame.players_count)
async def newgame_players_count(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "2 игрока":
        cnt = 2
    elif text == "4 игрока":
        cnt = 4
    else:
        await message.answer(
            "Пожалуйста, выбери 2 игрока или 4 игрока 🙂",
            reply_markup=players_count_kb,
        )
        return

    await state.update_data(players_count=cnt)

    await state.set_state(NewGame.court_booking)
    await message.answer(
        "Корт на это время уже забронирован?",
        reply_markup=court_booking_kb,
    )


@dp.message(NewGame.court_booking)
async def newgame_court_booking(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Корт уже забронирован":
        booked = True
    elif text == "Корт пока не забронирован":
        booked = False
    else:
        await message.answer(
            "Пожалуйста, выбери один из вариантов:\n"
            "«Корт уже забронирован» или «Корт пока не забронирован».",
            reply_markup=court_booking_kb,
        )
        return

    await state.update_data(is_court_booked=booked)

    await state.set_state(NewGame.privacy)
    await message.answer(
        "Укажи приватность матча:\n\n"
        "• Публичный матч — будет виден в общем списке игр.\n"
        "• Приватный матч — для приглашённых, не показывается в списке.",
        reply_markup=privacy_kb,
    )


@dp.message(NewGame.privacy)
async def newgame_privacy(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Публичный матч":
        visibility = "public"
    elif text == "Приватный матч":
        visibility = "private"
    else:
        await message.answer(
            "Пожалуйста, выбери «Публичный матч» или «Приватный матч».",
            reply_markup=privacy_kb,
        )
        return

    await state.update_data(visibility=visibility)

    await state.set_state(NewGame.comment)
    await message.answer(
        "Добавь комментарий к игре (например, сумму к оплате с каждого игрока или другие детали).\n"
        "Если ничего не хочешь добавлять — отправь «Пропустить».",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Пропустить")]],
            resize_keyboard=True,
        ),
    )


@dp.message(NewGame.comment)
async def newgame_comment(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower().startswith("пропус"):
        comment = None
    else:
        comment = text

    data = await state.get_data()
    await state.clear()

    court_id = data.get("court_id")
    court_name = data.get("court_name")
    match_date = data.get("match_date")
    match_time = data.get("match_time")
    match_end_time = data.get("match_end_time")
    duration_minutes = data.get("duration_minutes")
    game_type = data.get("game_type")
    rating_min = data.get("rating_min")
    rating_max = data.get("rating_max")
    players_count = data.get("players_count")
    is_court_booked = data.get("is_court_booked", False)
    visibility = data.get("visibility", "public")
    creator_mode = data.get("creator_mode", "self")
    payment_type = data.get("payment_type")

    game_id = await create_game(
        creator_id=message.from_user.id,
        court_id=court_id,
        match_date=match_date,
        match_time=match_time,
        duration_minutes=duration_minutes,
        match_end_time=match_end_time,
        game_type=game_type,
        rating_min=rating_min,
        rating_max=rating_max,
        players_count=players_count,
        comment=comment,
        is_court_booked=is_court_booked,
        visibility=visibility,
        creator_mode=creator_mode,
        payment_type=payment_type,
    )

    court_row = await get_court_by_id(court_id)
    if court_row:
        addr = court_row["address"] or "Адрес не указан"
    else:
        addr = "Адрес не указан"

    if rating_min is not None and rating_max is not None:
        rating_text = f"{rating_min:.2f}-{rating_max:.2f}"
    else:
        rating_text = "Без ограничений"

    booking_text = "забронирован" if is_court_booked else "не забронирован"
    privacy_text = "приватный матч" if visibility == "private" else "публичный матч"

    if payment_type == "split":
        payment_text = "делим поровну между всеми игроками"
    elif payment_type == "creator":
        payment_text = "организатор оплачивает корт"
    elif payment_type == "discuss":
        payment_text = "обсудим оплату в чате"
    else:
        payment_text = "не указано"

    comment_text = comment if comment else "—"
    occupied, total = await get_game_occupancy(game_id)

    time_line = (
        f"Время: {match_time}–{match_end_time}\n"
        if match_end_time
        else f"Время: {match_time}\n"
    )

    txt = (
        "Матч создан ✅\n\n"
        f"ID игры: {game_id}\n"
        f"Тип: {game_type}\n"
        f"Дата: {match_date}\n"
        f"{time_line}"
        f"Корт: {court_name} — <i>📍 {addr}</i>\n"
        f"Игроки: {occupied} из {total}\n"
        f"Ограничение по рейтингу: {rating_text}\n"
        f"Бронь корта: {booking_text}\n"
        f"Оплата: {payment_text}\n"
        f"Приватность: {privacy_text}\n"
        f"Комментарий: {comment_text}"
    )

    await message.answer(txt, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

# -----------------------------------------
# Просмотр матчей: /games
# -----------------------------------------

@dp.message(F.text == "/games")
async def games_cmd(message: Message, state: FSMContext):
    await update_username_only(message.from_user.id, message.from_user.username)
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Сначала нужно заполнить профиль.\n"
            "Пройди онбординг через /start 🙂"
        )
        return

    await state.clear()
    await state.set_state(ViewGames.date_choice)
    await message.answer(
        "Фильтр по дате:\n"
        "• Сегодня / Завтра\n"
        "• Ввести конкретную дату\n"
        "• Или выбрать «Все даты»",
        reply_markup=games_date_filter_kb,
    )


@dp.message(ViewGames.date_choice)
async def games_date_choice(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    today = get_moscow_today()

    if text == "Отмена":
        await state.clear()
        await message.answer(
            "Просмотр матчей отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == "Сегодня":
        d = today.strftime("%d.%m.%Y")
        await state.update_data(filter_date=d)
    elif text == "Завтра":
        d = (today + timedelta(days=1)).strftime("%d.%m.%Y")
        await state.update_data(filter_date=d)
    elif text == "Все даты":
        await state.update_data(filter_date=None)
    elif text == "Ввести дату":
        await state.set_state(ViewGames.date_manual)
        await message.answer(
            "Введи дату в формате ДД.ММ.ГГГГ\nНапример: 25.11.2024",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    else:
        await message.answer(
            "Пожалуйста, выбери вариант на клавиатуре.",
            reply_markup=games_date_filter_kb,
        )
        return

    # Переход к фильтру по времени
    await state.set_state(ViewGames.time_choice)
    await message.answer(
        "Фильтр по времени:\n"
        "• Без фильтра\n"
        "• Утро (04:00–10:00)\n"
        "• День (10:30–16:00)\n"
        "• Вечер (16:30–23:00)\n"
        "• Ночь (23:30–03:30)",
        reply_markup=games_time_choice_kb,
    )


@dp.message(ViewGames.date_manual)
async def games_date_manual(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        await message.answer(
            "Не похоже на дату 😅\nНужен формат ДД.ММ.ГГГГ, например: 25.11.2024",
        )
        return

    try:
        day, month, year = map(int, text.split("."))
        date(year, month, day)
    except ValueError:
        await message.answer(
            "Не получилось разобрать дату.\nПроверь формат и попробуй ещё раз.",
        )
        return

    await state.update_data(filter_date=text)

    await state.set_state(ViewGames.time_choice)
    await message.answer(
        "Фильтр по времени:\n"
        "• Без фильтра\n"
        "• Утро (04:00–10:00)\n"
        "• День (10:30–16:00)\n"
        "• Вечер (16:30–23:00)\n"
        "• Ночь (23:30–03:30)",
        reply_markup=games_time_choice_kb,
    )


@dp.message(ViewGames.time_choice)
async def games_time_choice(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Отмена":
        await state.clear()
        await message.answer(
            "Просмотр матчей отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == "Назад":
        await state.set_state(ViewGames.date_choice)
        await message.answer(
            "Фильтр по дате:\n"
            "• Сегодня / Завтра\n"
            "• Ввести конкретную дату\n"
            "• Или выбрать «Все даты»",
            reply_markup=games_date_filter_kb,
        )
        return

    if text == "Без фильтра по времени":
        await state.update_data(filter_time_from=None)
    elif text == "Утро":
        await state.update_data(filter_time_from="morning")
    elif text == "День":
        await state.update_data(filter_time_from="day")
    elif text == "Вечер":
        await state.update_data(filter_time_from="evening")
    elif text == "Ночь":
        await state.update_data(filter_time_from="night")
    else:
        await message.answer(
            "Пожалуйста, выбери вариант на клавиатуре.",
            reply_markup=games_time_choice_kb,
        )
        return

    await state.set_state(ViewGames.home_courts_filter)
    await message.answer(
        "Фильтр по корту:\n"
        "• Все корты\n"
        "• Только твои домашние корты",
        reply_markup=games_home_filter_kb,
    )


@dp.message(ViewGames.time_manual)
async def games_time_manual(message: Message, state: FSMContext):
    time_str = parse_time(message.text or "")
    if not time_str:
        await message.answer(
            "Не похоже на время 😅\nНужен формат ЧЧ:ММ, например: 18:00",
        )
        return

    await state.update_data(filter_time_from=time_str)

    await state.set_state(ViewGames.home_courts_filter)
    await message.answer(
        "Фильтр по корту:\n"
        "• Все корты\n"
        "• Только твои домашние корты",
        reply_markup=games_home_filter_kb,
    )


async def _send_games_page(message: Message, state: FSMContext, initial: bool = False):
    data = await state.get_data()
    filter_date = data.get("filter_date")
    filter_time_from = data.get("filter_time_from")
    only_home = data.get("only_home", False)
    offset = data.get("offset", 0)

    games = await get_games_for_listing(
        user_id=message.from_user.id,
        filter_date=filter_date,
        filter_time_from=filter_time_from,
        only_home=only_home,
        limit=GAMES_PAGE_SIZE,
        offset=offset,
    )

    if initial and not games:
        await message.answer(
            "По выбранным фильтрам пока нет доступных матчей 😔",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    if not games:
        await message.answer(
            "Больше матчей по этим фильтрам нет.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    for g in games:
        if g["rating_min"] is not None and g["rating_max"] is not None:
            rating_text = f"{g['rating_min']:.2f}-{g['rating_max']:.2f}"
        else:
            rating_text = "Без ограничений"

        booking_text = "забронирован" if g["is_court_booked"] else "не забронирован"
        comment_text = g["comment"] if g["comment"] else "—"

        payment_type = g["payment_type"]
        if payment_type == "split":
            payment_text = "делим поровну между всеми игроками"
        elif payment_type == "creator":
            payment_text = "организатор оплачивает корт"
        elif payment_type == "discuss":
            payment_text = "обсудим оплату в чате"
        else:
            payment_text = "не указано"

        creator_name = g["creator_name"] or "Игрок"
        creator_ntrp = g["creator_ntrp"]
        if creator_ntrp is not None:
            creator_line = f"{creator_name} (NTRP {creator_ntrp:.2f})"
        else:
            creator_line = creator_name

        addr = g["court_address"] or "Адрес не указан"
        occupied, total = await get_game_occupancy(g["id"])

        duration_minutes = g['duration_minutes']
        if duration_minutes:
            hours = duration_minutes // 60
            mins = duration_minutes % 60
            if hours and mins:
                duration_text = f"{hours} ч {mins} мин"
            elif hours:
                duration_text = f"{hours} ч"
            else:
                duration_text = f"{mins} мин"
            time_line = (
                f"Время: {g['match_time']}–{g['match_end_time']} ({duration_text})\n"
                if g['match_end_time']
                else f"Время: {g['match_time']} ({duration_text})\n"
            )
        else:
            time_line = (
                f"Время: {g['match_time']}–{g['match_end_time']}\n"
                if g['match_end_time']
                else f"Время: {g['match_time']}\n"
            )

        txt = (
            f"🎾 <b>Матч #{g['id']}</b>\n\n"
            f"Организатор: {creator_line}\n"
            f"Тип: {g['game_type']}\n"
            f"Дата: {g['match_date']}\n"
            f"{time_line}"
            f"Корт: {g['court_short_name']} — <i>📍 {addr}</i>\n"
            f"Игроки: {occupied} из {total}\n"
            f"Ограничение по рейтингу: {rating_text}\n"
            f"Бронь корта: {booking_text}\n"
            f"Оплата: {payment_text}\n"
            f"Комментарий: {comment_text}"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подать заявку на матч",
                        callback_data=f"apply_game:{g['id']}",
                    )
                ]
            ]
        )

        await message.answer(txt, parse_mode="HTML", reply_markup=kb)

    # Если выдано ровно PAGE_SIZE — предложим показать ещё
    if len(games) == GAMES_PAGE_SIZE:
        await state.update_data(offset=offset + GAMES_PAGE_SIZE)
        await message.answer(
            "Показать ещё матчи?",
            reply_markup=games_browse_kb,
        )
        await state.set_state(ViewGames.browsing)
    else:
        await message.answer(
            "Это все матчи по выбранным фильтрам.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()


@dp.message(ViewGames.home_courts_filter)
async def games_home_filter(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Отмена":
        await state.clear()
        await message.answer(
            "Просмотр матчей отменён.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    only_home = False
    if text == "Все корты":
        only_home = False
    elif text == "Только домашние корты":
        home_courts = await get_user_home_courts(message.from_user.id)
        if not home_courts:
            await message.answer(
                "У тебя пока не настроены домашние корты.\n"
                "Я покажу матчи на всех кортах.\n\n"
                "Домашние корты можно указать через /edit → Домашние корты.",
            )
            only_home = False
        else:
            only_home = True
    else:
        await message.answer(
            "Пожалуйста, выбери вариант на клавиатуре.",
            reply_markup=games_home_filter_kb,
        )
        return

    await state.update_data(only_home=only_home, offset=0)

    # Показываем первую страницу
    await _send_games_page(message, state, initial=True)


@dp.message(ViewGames.browsing)
async def games_browsing(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Показать ещё 10 матчей":
        await _send_games_page(message, state, initial=False)
        return

    if text == "Закрыть список матчей":
        await state.clear()
        await message.answer(
            "Просмотр матчей завершён.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Любой другой ввод — тоже выходим
    await state.clear()
    await message.answer(
        "Просмотр матчей остановлен.",
        reply_markup=ReplyKeyboardRemove(),
    )

# -----------------------------------------
# Мои матчи: /mygames
# -----------------------------------------

async def _send_created_games_list(message: Message, user_id: int, status: Optional[str]):
    """
    Список матчей пользователя по статусу.
    status может быть: "scheduled", "finished", "cancelled" или None (все матчи).
    """
    games = await get_games_created_by_user(user_id, status=status)
    if not games:
        if status == "scheduled":
            await message.answer("У тебя пока нет предстоящих матчей.")
        elif status == "finished":
            await message.answer("У тебя пока нет завершённых матчей.")
        elif status == "cancelled":
            await message.answer("У тебя пока нет отменённых матчей.")
        else:
            await message.answer("У тебя пока нет матчей.")
        return


    for g in games:
        if g["rating_min"] is not None and g["rating_max"] is not None:
            rating_text = f"{g['rating_min']:.2f}-{g['rating_max']:.2f}"
        else:
            rating_text = "Без ограничений"

        booking_text = "забронирован" if g["is_court_booked"] else "не забронирован"
        comment_text = g["comment"] if g["comment"] else "—"
        addr = g["court_address"] or "Адрес не указан"
        occupied, total = await get_game_occupancy(g["id"])
        score_text = g["score"] or "—"

        payment_type = g["payment_type"]
        if payment_type == "split":
            payment_text = "делим поровну между всеми игроками"
        elif payment_type == "creator":
            payment_text = "организатор оплачивает корт"
        elif payment_type == "discuss":
            payment_text = "обсудим оплату в чате"
        else:
            payment_text = "не указано"


        duration_minutes = g['duration_minutes']
        if duration_minutes:
            hours = duration_minutes // 60
            mins = duration_minutes % 60
            if hours and mins:
                duration_text = f"{hours} ч {mins} мин"
            elif hours:
                duration_text = f"{hours} ч"
            else:
                duration_text = f"{mins} мин"
        else:
            duration_text = None

        if g['match_end_time']:
            if duration_text:
                time_line = f"Время: {g['match_time']}–{g['match_end_time']} ({duration_text})\n"
            else:
                time_line = f"Время: {g['match_time']}–{g['match_end_time']}\n"
        else:
            if duration_text:
                time_line = f"Время: {g['match_time']} ({duration_text})\n"
            else:
                time_line = f"Время: {g['match_time']}\n"

        txt = (
            f"🎾 <b>Матч #{g['id']}</b>\n\n"
            f"Статус: {'запланирован' if g['status']=='scheduled' else 'завершён' if g['status']=='finished' else 'отменён'}\n"
            f"Дата: {g['match_date']}\n"
            f"{time_line}"
            f"Корт: {g['court_short_name']} — <i>📍 {addr}</i>\n"
            f"Игроки: {occupied} из {total}\n"
            f"Ограничение по рейтингу: {rating_text}\n"
            f"Бронь корта: {booking_text}\n"
            f"Оплата: {payment_text}\n"
            f"Комментарий: {comment_text}\n"
            f"Счёт: {score_text}"
        )

        if status == "scheduled":
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="👀 Откликнувшиеся",
                            callback_data=f"view_apps:{g['id']}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Отменить матч",
                            callback_data=f"cancel_game:{g['id']}",
                        )
                    ],
                ]
            )
            await message.answer(txt, parse_mode="HTML", reply_markup=kb)
        else:  # finished
            if not g["score"]:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Внести счёт",
                                callback_data=f"set_score:{g['id']}",
                            )
                        ]
                    ]
                )
                await message.answer(txt, parse_mode="HTML", reply_markup=kb)
            else:
                await message.answer(txt, parse_mode="HTML")


async def _send_my_participating_games(message: Message, user_id: int):
    """
    Раздел «Матчи с моим участием».

    Показываем:
    • матчи, куда у пользователя есть принятая заявка;
    • а также матчи, которые он создал «для себя» (creator_mode = 'self').
    """
    games = await get_games_with_user_participation(user_id)
    if not games:
        await message.answer("У тебя пока нет матчей с принятыми заявками или созданных тобой матчей.")
        return

    for g in games:
        # Ограничение по рейтингу
        if g["rating_min"] is not None and g["rating_max"] is not None:
            rating_text = f"{g['rating_min']:.2f}-{g['rating_max']:.2f}"
        else:
            rating_text = "Без ограничений"

        booking_text = "забронирован" if g["is_court_booked"] else "не забронирован"
        comment_text = g["comment"] if g["comment"] else "—"

        payment_type = g["payment_type"]
        if payment_type == "split":
            payment_text = "делим поровну между всеми игроками"
        elif payment_type == "creator":
            payment_text = "организатор оплачивает корт"
        elif payment_type == "discuss":
            payment_text = "обсудим оплату в чате"
        else:
            payment_text = "не указано"

        addr = g["court_address"] or "Адрес не указан"
        occupied, total = await get_game_occupancy(g["id"])
        score_text = g["score"] or "—"

        creator_name = g["creator_name"] or "Игрок"
        creator_ntrp = g["creator_ntrp"]
        if creator_ntrp is not None:
            creator_line = f"{creator_name} (NTRP {creator_ntrp:.2f})"
        else:
            creator_line = creator_name

        is_creator = g["creator_id"] == user_id
        if is_creator:
            participation_line = "Ты организатор этого матча"
        else:
            participation_line = "Твоё участие: заявка принята ✅"

        duration_minutes = g['duration_minutes']
        if duration_minutes:
            hours = duration_minutes // 60
            mins = duration_minutes % 60
            if hours and mins:
                duration_text = f"{hours} ч {mins} мин"
            elif hours:
                duration_text = f"{hours} ч"
            else:
                duration_text = f"{mins} мин"
        else:
            duration_text = None

        if g['match_end_time']:
            if duration_text:
                time_line = f"Время: {g['match_time']}–{g['match_end_time']} ({duration_text})\n"
            else:
                time_line = f"Время: {g['match_time']}–{g['match_end_time']}\n"
        else:
            if duration_text:
                time_line = f"Время: {g['match_time']} ({duration_text})\n"
            else:
                time_line = f"Время: {g['match_time']}\n"


        txt = (
            f"🎾 <b>Матч #{g['id']}</b>\n\n"
            f"{participation_line}\n"
            f"Организатор: {creator_line}\n"
            f"Статус матча: {g['status']}\n"
            f"Дата: {g['match_date']}\n"
            f"{time_line}"
            f"Корт: {g['court_short_name']} — <i>📍 {addr}</i>\n"
            f"Игроки: {occupied} из {total}\n"
            f"Ограничение по рейтингу: {rating_text}\n"
            f"Бронь корта: {booking_text}\n"
            f"Оплата: {payment_text}\n"
            f"Комментарий: {comment_text}\n"
            f"Счёт: {score_text}"
        )

        await message.answer(txt, parse_mode="HTML")


@dp.message(F.text == "/mygames")
async def mygames_cmd(message: Message, state: FSMContext):
    await update_username_only(message.from_user.id, message.from_user.username)
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Сначала нужно заполнить профиль.\n"
            "Пройди онбординг через /start 🙂"
        )
        return

    await state.clear()
    await state.set_state(MyGames.main)
    await message.answer(
        "Раздел «Мои матчи».\n"
        "Выбери, что показать:",
        reply_markup=my_games_main_kb,
    )


@dp.message(MyGames.main)
async def mygames_main_handler(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Предстоящие матчи":
        await _send_created_games_list(message, message.from_user.id, status="scheduled")
        await message.answer(
            "Раздел «Мои матчи».\nВыбери, что показать:",
            reply_markup=my_games_main_kb,
        )
    elif text == "Завершённые матчи":
        await _send_created_games_list(message, message.from_user.id, status="finished")
        await message.answer(
            "Раздел «Мои матчи».\nВыбери, что показать:",
            reply_markup=my_games_main_kb,
        )
    elif text == "Отменённые матчи":
        await _send_created_games_list(message, message.from_user.id, status="cancelled")
        await message.answer(
            "Раздел «Мои матчи».\nВыбери, что показать:",
            reply_markup=my_games_main_kb,
        )
    elif text == "Все мои матчи":
        # Без фильтра по статусу — покажем все матчи пользователя
        await _send_created_games_list(message, message.from_user.id, status=None)
        await message.answer(
            "Раздел «Мои матчи».\nВыбери, что показать:",
            reply_markup=my_games_main_kb,
        )
    elif text == "Назад":
        await state.clear()
        await message.answer(
            "Выход из раздела «Мои матчи».",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "Пожалуйста, выбери вариант на клавиатуре.",
            reply_markup=my_games_main_kb,
        )

# ---------- Заявка на матч: helper для карточки ----------

async def send_application_card_to_creator(
    creator_chat_id: int,
    application_id: int,
    game_id: int,
    applicant_user: Optional[aiosqlite.Row],
):
    """
    Показываем карточку игрока при новой заявке или при просмотре откликнувшихся.
    """
    if not applicant_user:
        # fallback – просто текст
        txt = (
            f"📨 Новая заявка на матч #{game_id}\n"
            f"ID заявки: {application_id}\n"
            f"Информация об игроке недоступна."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Принять",
                        callback_data=f"app_decision:{application_id}:accept",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"app_decision:{application_id}:reject",
                    ),
                ]
            ]
        )
        await bot.send_message(creator_chat_id, txt, reply_markup=kb)
        return

    name = applicant_user["name"] or "—"
    gender = applicant_user["gender"] or "—"
    city = applicant_user["city"] or "—"
    ntrp = applicant_user["ntrp"]
    ntrp_text = f"{ntrp:.2f}" if ntrp is not None else "—"
    about = applicant_user["about"] or "—"
    birth_date_str = applicant_user["birth_date"]
    age = calculate_age_from_str(birth_date_str)
    age_text = f"{age} лет" if age is not None else "—"
    photo_file_id = applicant_user["photo_file_id"]

    txt = (
        f"📇 <b>Заявка на матч #{game_id}</b>\n\n"
        f"Имя: {name}\n"
        f"Пол: {gender}\n"
        f"Город: {city}\n"
        f"Рейтинг: {ntrp_text}\n"
        f"Возраст: {age_text}\n"
        f"О себе: {about}\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять",
                    callback_data=f"app_decision:{application_id}:accept",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"app_decision:{application_id}:reject",
                ),
            ]
        ]
    )

    if photo_file_id:
        await bot.send_photo(
            creator_chat_id,
            photo=photo_file_id,
            caption=txt,
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        await bot.send_message(
            creator_chat_id,
            txt,
            reply_markup=kb,
            parse_mode="HTML",
        )

# ---------- Заявка на матч: callback-кнопка ----------

@dp.callback_query(F.data.startswith("apply_game:"))
async def apply_game_callback(callback: CallbackQuery):
    await update_username_only(callback.from_user.id, callback.from_user.username)
    data = callback.data or ""
    try:
        _, game_id_str = data.split(":", 1)
        game_id = int(game_id_str)
    except Exception:
        await callback.answer("Что-то пошло не так 😔", show_alert=False)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Проверим, что матч существует и публичный
        cursor = await db.execute(
            "SELECT id, creator_id, visibility, is_active FROM games WHERE id = ?;",
            (game_id,),
        )
        game = await cursor.fetchone()
        await cursor.close()

        if not game or game["is_active"] != 1 or game["visibility"] != "public":
            await callback.answer("Этот матч недоступен для заявок.", show_alert=True)
            return

        if game["creator_id"] == callback.from_user.id:
            await callback.answer("Это твой матч 🙂", show_alert=True)
            return

        # Проверим, не подавал ли уже заявку
        cursor = await db.execute(
            """
            SELECT id FROM game_applications
            WHERE game_id = ? AND applicant_id = ?;
            """,
            (game_id, callback.from_user.id),
        )
        exists = await cursor.fetchone()
        await cursor.close()

        if exists:
            await callback.answer(
                "Ты уже подавал заявку на этот матч.",
                show_alert=True,
            )
            return

        # Создаём заявку
        await db.execute(
            """
            INSERT INTO game_applications (game_id, applicant_id)
            VALUES (?, ?);
            """,
            (game_id, callback.from_user.id),
        )
        cursor = await db.execute("SELECT last_insert_rowid();")
        row = await cursor.fetchone()
        await cursor.close()
        application_id = row[0]
        await db.commit()

    # Пытаемся получить профиль игрока
    applicant_user = await get_user(callback.from_user.id)

    # Показываем карточку игрока создателю матча
    try:
        await send_application_card_to_creator(
            creator_chat_id=game["creator_id"],
            application_id=application_id,
            game_id=game_id,
            applicant_user=applicant_user,
        )
    except Exception as e:
        logger.exception("Failed to notify game creator with card: %s", e)

    await callback.answer("Заявка отправлена создателю матча ✅", show_alert=True)

# ---------- Обработка решений по заявке (принять/отклонить) ----------

@dp.callback_query(F.data.startswith("app_decision:"))
async def app_decision_callback(callback: CallbackQuery):
    await update_username_only(callback.from_user.id, callback.from_user.username)
    data = callback.data or ""
    try:
        _, app_id_str, action = data.split(":", 2)
        application_id = int(app_id_str)
        assert action in ("accept", "reject")
    except Exception:
        await callback.answer("Некорректные данные заявки.", show_alert=False)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT ga.*, g.creator_id, g.id AS game_id
            FROM game_applications ga
            JOIN games g ON g.id = ga.game_id
            WHERE ga.id = ?;
            """,
            (application_id,),
        )
        app_row = await cursor.fetchone()
        await cursor.close()

        if not app_row:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        creator_id = app_row["creator_id"]
        game_id = app_row["game_id"]
        applicant_id = app_row["applicant_id"]
        status = app_row["status"]

        if callback.from_user.id != creator_id:
            await callback.answer("Вы не организатор этого матча.", show_alert=True)
            return

        if status != "pending":
            await callback.answer(
                f"Заявка уже обработана (статус: {status}).",
                show_alert=True,
            )
            return

        new_status = "accepted" if action == "accept" else "rejected"
        await db.execute(
            "UPDATE game_applications SET status = ? WHERE id = ?;",
            (new_status, application_id),
        )
        await db.commit()

    # Если заявка отклонена — просто уведомляем игрока и организатора
    if new_status == "rejected":
        try:
            await bot.send_message(
                applicant_id,
                f"❌ Увы, ваше участие в матче #{game_id} отклонено организатором.",
            )
            await callback.message.reply(
                f"Заявка отклонена ❌ (матч #{game_id}).",
            )
        except Exception as e:
            logger.exception("Failed to notify about application decision: %s", e)

        await callback.answer("Решение по заявке сохранено.", show_alert=False)
        return

    # Дальше — логика для принятой заявки
    # Собираем список всех участников матча после принятия заявки
    participant_ids = await get_game_participant_ids(game_id, include_creator=True)

    # Словарь профилей участников
    users_by_id = {}
    for pid in participant_ids:
        u = await get_user(pid)
        if u:
            users_by_id[pid] = u

    def format_contact(u) -> str:
        if not u:
            return "Игрок (профиль недоступен)"
        username = u["username"]
        name = u["name"] or "Игрок"
        if username:
            return f"@{username}"
        return name

    def build_contacts_for(recipient_id: int) -> str:
        contacts = []
        for pid in participant_ids:
            if pid == recipient_id:
                continue
            u = users_by_id.get(pid)
            if not u:
                continue
            contacts.append(format_contact(u))
        if not contacts:
            return "Пока нет других участников с указанным Telegram-ником."
        return "\n".join(f"• {c}" for c in contacts)

    # Текущая заполняемость матча
    occupied, total = await get_game_occupancy(game_id)

    # 1) Сообщение организатору
    try:
        new_player_user = users_by_id.get(applicant_id)
        new_player_contact = format_contact(new_player_user)

        text_creator_lines = [
            f"Ура! Вы приняли нового участника матча #{game_id} ✅",
            f"Теперь вы можете написать ему {new_player_contact} и обсудить детали матча.",
        ]
        if occupied >= total:
            text_creator_lines.append(
                f"Теперь ваш матч полностью укомплектован: {occupied} из {total} участников."
            )
        else:
            text_creator_lines.append(
                f"Сейчас в матче {occupied} из {total} участников."
            )

        await callback.message.reply("\n".join(text_creator_lines))
    except Exception as e:
        logger.exception("Failed to notify organizer about accepted application: %s", e)

    # 2) Сообщение принятому участнику
    try:
        contacts_for_applicant = build_contacts_for(applicant_id)
        await bot.send_message(
            applicant_id,
            f"Ура! Ваше участие в матче #{game_id} подтверждено организатором ✅\n\n"
            f"Вот контакты других участников матча:\n{contacts_for_applicant}",
        )
    except Exception as e:
        logger.exception("Failed to notify applicant about accepted application: %s", e)

    # 3) Сообщения остальным участникам матча
    try:
        new_player_user = users_by_id.get(applicant_id)
        new_player_contact = format_contact(new_player_user)

        for pid in participant_ids:
            if pid == applicant_id or pid == creator_id:
                # Этим двоим уже отправили отдельные сообщения
                continue
            contacts_for_other = build_contacts_for(pid)
            await bot.send_message(
                pid,
                f"К вашему матчу #{game_id} присоединился новый участник {new_player_contact} ✅\n\n"
                f"Актуальный список участников (которым вы можете написать в Telegram):\n{contacts_for_other}",
            )
    except Exception as e:
        logger.exception("Failed to notify existing participants about new one: %s", e)

    await callback.answer("Решение по заявке сохранено.", show_alert=False)


@dp.callback_query(F.data.startswith("cancel_game:"))
async def cancel_game_callback(callback: CallbackQuery):
    await update_username_only(callback.from_user.id, callback.from_user.username)
    data = callback.data or ""
    try:
        _, game_id_str = data.split(":", 1)
        game_id = int(game_id_str)
    except Exception:
        await callback.answer("Некорректный ID матча.", show_alert=False)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, что создатель — текущий пользователь
        cursor = await db.execute(
            "SELECT creator_id, status FROM games WHERE id = ?;",
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if not row:
            await callback.answer("Матч не найден.", show_alert=True)
            return

        if row["creator_id"] != callback.from_user.id:
            await callback.answer("Ты не организатор этого матча.", show_alert=True)
            return

        if row["status"] == "cancelled":
            await callback.answer("Матч уже отменён.", show_alert=True)
            return

        await db.execute(
            "UPDATE games SET status = 'cancelled', is_active = 0 WHERE id = ?;",
            (game_id,),
        )
        # Обновим статусы заявок
        await db.execute(
            """
            UPDATE game_applications
            SET status = 'cancelled'
            WHERE game_id = ? AND status = 'pending';
            """,
            (game_id,),
        )
        await db.commit()

    await callback.answer("Матч отменён.", show_alert=False)
    await callback.message.reply(f"Матч #{game_id} отменён ❌")

# ---------- Просмотр откликнувшихся ----------

@dp.callback_query(F.data.startswith("view_apps:"))
async def view_apps_callback(callback: CallbackQuery):
    await update_username_only(callback.from_user.id, callback.from_user.username)
    data = callback.data or ""
    try:
        _, game_id_str = data.split(":", 1)
        game_id = int(game_id_str)
    except Exception:
        await callback.answer("Некорректный ID матча.", show_alert=False)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Проверяем, что юзер — создатель матча
        cursor = await db.execute(
            "SELECT creator_id FROM games WHERE id = ?;",
            (game_id,),
        )
        game_row = await cursor.fetchone()
        await cursor.close()

        if not game_row:
            await callback.answer("Матч не найден.", show_alert=True)
            return

        if game_row["creator_id"] != callback.from_user.id:
            await callback.answer("Ты не организатор этого матча.", show_alert=True)
            return

        cursor = await db.execute(
            """
            SELECT ga.*, u.*
            FROM game_applications ga
            LEFT JOIN users u ON u.telegram_id = ga.applicant_id
            WHERE ga.game_id = ?
            ORDER BY ga.created_at ASC;
            """,
            (game_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    if not rows:
        await callback.message.reply("На этот матч пока нет откликнувшихся.")
        await callback.answer()
        return

    # Показываем карточки для всех, у кого статус pending
    pending_shown = False
    for r in rows:
        app_status = r["status"]
        if app_status == "pending":
            pending_shown = True
            application_id = r["id"]
            # user-поля начинаются после столбцов ga; проще получить user отдельно
            # но мы уже джоинили, поэтому сделаем маленький хак:
            # В таблице users у нас точно есть telegram_id, возьмём его и ещё раз запросим
            applicant_id = r["applicant_id"]
            applicant_user = await get_user(applicant_id)
            await send_application_card_to_creator(
                creator_chat_id=callback.from_user.id,
                application_id=application_id,
                game_id=game_id,
                applicant_user=applicant_user,
            )

    if not pending_shown:
        await callback.message.reply(
            "Все заявки на этот матч уже обработаны (приняты или отклонены)."
        )

    await callback.answer()

# ---------- Ввод счёта для завершённого матча ----------

@dp.callback_query(F.data.startswith("set_score:"))
async def set_score_callback(callback: CallbackQuery, state: FSMContext):
    await update_username_only(callback.from_user.id, callback.from_user.username)
    data = callback.data or ""
    try:
        _, game_id_str = data.split(":", 1)
        game_id = int(game_id_str)
    except Exception:
        await callback.answer("Некорректный ID матча.", show_alert=False)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT creator_id, status FROM games WHERE id = ?;",
            (game_id,),
        )
        game_row = await cursor.fetchone()
        await cursor.close()

    if not game_row:
        await callback.answer("Матч не найден.", show_alert=True)
        return

    if game_row["creator_id"] != callback.from_user.id:
        await callback.answer("Ты не организатор этого матча.", show_alert=True)
        return

    # По ТЗ — ввод счёта для завершённых матчей
    # Но не будем жёстко проверять статус; если хочешь – можно ужесточить.
    await state.set_state(MyGames.waiting_score)
    await state.update_data(score_game_id=game_id)

    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        f"Введи счёт матча #{game_id} в свободной форме (например: 6-4 3-6 10-7):",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(MyGames.waiting_score)
async def mygames_waiting_score_handler(message: Message, state: FSMContext):
    score_text = (message.text or "").strip()
    data = await state.get_data()
    game_id = data.get("score_game_id")

    if not game_id:
        await state.clear()
        await message.answer(
            "Не нашёл ID матча для сохранения счёта. Попробуй ещё раз из меню.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE games SET score = ?, status = 'finished' WHERE id = ? AND creator_id = ?;",
            (score_text, game_id, message.from_user.id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"Счёт матча #{game_id} сохранён ✅\n\n"
        f"Счёт: {score_text}",
        reply_markup=ReplyKeyboardRemove(),
    )

# -----------------------------------------
# HTTP-сервер для Render (healthcheck)
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