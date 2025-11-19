import os
import re
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

HOME_DONE = "Готово ✅"
HOME_SKIP = "Пропустить"

# Глобальное хранилище фильтров списка матчей (по юзеру)
user_game_filters: Dict[int, Dict[str, Any]] = {}

# -----------------------------------------
# FSM
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


class GameCreation(StatesGroup):
    mode = State()
    date = State()
    time = State()
    court = State()
    players_count = State()
    ranked_choice = State()
    rating_range = State()
    comment = State()


class GamesFilter(StatesGroup):
    date = State()
    level = State()
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


def parse_date_ddmmyyyy(text: str) -> Optional[str]:
    """
    Принимает 'ДД.ММ.ГГГГ', 'сегодня', 'завтра'.
    Возвращает 'ГГГГ-ММ-ДД' или None.
    """
    text = text.strip().lower()
    today = date.today()

    if text in ("сегодня", "today"):
        d = today
    elif text in ("завтра", "tomorrow"):
        d = today + timedelta(days=1)
    else:
        if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
            return None
        try:
            day, month, year = map(int, text.split("."))
            d = date(year, month, day)
        except ValueError:
            return None

    return d.strftime("%Y-%m-%d")


def parse_time_hhmm(text: str) -> Optional[str]:
    """
    Ожидает 'ЧЧ:ММ' (24 часа). Возвращает ту же строку или None.
    """
    text = text.strip()
    if not re.match(r"^\d{2}:\d{2}$", text):
        return None
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError:
        return None
    return text


def parse_rating_range(text: str) -> Optional[tuple]:
    """
    Парсит диапазон рейтинга формата '3.0-3.75' / '3,0–3,75'.
    Возвращает (min, max) или None.
    """
    text = text.strip().lower().replace(",", ".")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*[-–]\s*([0-9]+(?:\.[0-9]+)?)$", text)
    if not m:
        return None
    r1, r2 = float(m.group(1)), float(m.group(2))
    if r1 > r2:
        r1, r2 = r2, r1
    if r1 < 1.0 or r2 > 7.0:
        return None
    return (round(r1, 2), round(r2, 2))


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

newgame_type_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Тренировка")],
        [KeyboardButton(text="Матч на рейтинг")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

players_count_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="2 игрока")],
        [KeyboardButton(text="4 игрока")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

yes_no_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Да"), KeyboardButton(text="Нет")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


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


def build_single_court_kb(courts: List[aiosqlite.Row]) -> ReplyKeyboardMarkup:
    """
    Клавиатура для выбора ОДНОГО корта (при создании игры).
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
                username    TEXT,
                name        TEXT,
                gender      TEXT,
                city        TEXT,
                ntrp        REAL,
                ntrp_self   REAL,
                play_experience TEXT,
                matches_6m  TEXT,
                fitness     TEXT,
                tournaments TEXT,
                birth_date  TEXT,
                about       TEXT,
                photo_file_id TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # courts – ты уже наполнил её своим каталогом
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS courts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                slug       TEXT UNIQUE,
                short_name TEXT NOT NULL,
                full_name  TEXT,
                address    TEXT,
                area       TEXT,
                is_active  INTEGER DEFAULT 1
            );
            """
        )

        # user_home_courts
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_home_courts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                court_id    INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # games – историчность матчей
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id    INTEGER NOT NULL,
                court_id      INTEGER NOT NULL,
                game_date     TEXT NOT NULL,   -- YYYY-MM-DD
                game_time     TEXT NOT NULL,   -- HH:MM
                game_type     TEXT NOT NULL,   -- 'Тренировка' / 'Матч на рейтинг'
                is_ranked     INTEGER NOT NULL DEFAULT 0,
                players_count INTEGER NOT NULL,
                rating_min    REAL,
                rating_max    REAL,
                comment       TEXT,
                status        TEXT NOT NULL DEFAULT 'open',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # game_applications – заявки на игры (тоже историчные)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_applications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id    INTEGER NOT NULL,
                player_id  INTEGER NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        await db.commit()


async def get_active_courts() -> List[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, short_name, address FROM courts WHERE is_active = 1 ORDER BY short_name;"
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
    Удаляет пользователя и его домашние корты. Нужен для /reset.
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
    game_date: str,
    game_time: str,
    game_type: str,
    is_ranked: bool,
    players_count: int,
    rating_min: Optional[float],
    rating_max: Optional[float],
    comment: Optional[str],
) -> int:
    """
    Создаёт игру, возвращает id.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO games (
                creator_id, court_id, game_date, game_time,
                game_type, is_ranked, players_count,
                rating_min, rating_max, comment, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open');
            """,
            (
                creator_id,
                court_id,
                game_date,
                game_time,
                game_type,
                1 if is_ranked else 0,
                players_count,
                rating_min,
                rating_max,
                comment,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_game(game_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT g.*, c.short_name AS court_name, c.address AS court_address,
                   u.name AS creator_name, u.ntrp AS creator_ntrp
            FROM games g
            JOIN courts c ON c.id = g.court_id
            JOIN users u ON u.telegram_id = g.creator_id
            WHERE g.id = ?;
            """,
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def list_games_for_user(
    tg_id: int,
    offset: int = 0,
    limit: int = 10,
) -> List[aiosqlite.Row]:
    """
    Список игр с учётом фильтра user_game_filters[tg_id].
    Только открытые игры, начиная с сегодняшней даты.
    """
    filters = user_game_filters.get(tg_id, {})
    date_filter = filters.get("date")
    rating_min = filters.get("rating_min")
    rating_max = filters.get("rating_max")
    home_only = filters.get("home_only", False)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        where = ["g.status = 'open'"]
        params: List[Any] = []

        today_str = date.today().strftime("%Y-%m-%d")
        where.append("g.game_date >= ?")
        params.append(today_str)

        if date_filter:
            where.append("g.game_date = ?")
            params.append(date_filter)

        if rating_min is not None:
            # Игра либо без ограничений, либо нижняя граница не выше желаемого минимума
            where.append("(g.rating_min IS NULL OR g.rating_min <= ?)")
            params.append(rating_min)

        if rating_max is not None:
            # Игра либо без ограничений, либо верхняя граница не ниже желаемого максимума
            where.append("(g.rating_max IS NULL OR g.rating_max >= ?)")
            params.append(rating_max)

        join_home = ""
        if home_only:
            join_home = """
                JOIN user_home_courts uh
                  ON uh.court_id = g.court_id AND uh.telegram_id = ?
            """
            params.insert(0, tg_id)

        where_sql = " AND ".join(where)

        sql = f"""
            SELECT g.*, c.short_name AS court_name, c.address AS court_address,
                   u.name AS creator_name, u.ntrp AS creator_ntrp
            FROM games g
            JOIN courts c ON c.id = g.court_id
            JOIN users u ON u.telegram_id = g.creator_id
            {join_home}
            WHERE {where_sql}
            ORDER BY g.game_date, g.game_time, g.id
            LIMIT ? OFFSET ?;
        """
        params.extend([limit, offset])

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def add_game_application(game_id: int, player_id: int) -> bool:
    """
    Создаёт заявку на игру, если её ещё не было. Возвращает True/False.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM game_applications
            WHERE game_id = ? AND player_id = ?;
            """,
            (game_id, player_id),
        )
        row = await cursor.fetchone()
        already = row[0] > 0
        await cursor.close()

        if already:
            return False

        await db.execute(
            """
            INSERT INTO game_applications (game_id, player_id, status)
            VALUES (?, ?, 'pending');
            """,
            (game_id, player_id),
        )
        await db.commit()
        return True


# -----------------------------------------
# Логика рейтинга
# -----------------------------------------

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
# Команды: старт, профиль, reset, edit
# -----------------------------------------

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if user:
        await state.clear()
        await message.answer(
            "Привет 👋\n"
            "Ты уже проходил анкету.\n\n"
            "Доступные команды:\n"
            "/start — начать онбординг / показать это меню\n"
            "/me — показать мой профиль\n"
            "/edit — изменить профиль\n"
            "/reset — сбросить анкету и пройти заново\n"
            "/newgame — создать новую игру\n"
            "/games — список доступных игр\n"
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
# Онбординг (остальные шаги)
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
async def onboarding_city(message: Message, state: FSMContext):
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
async def onboarding_home_courts(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    selected_ids: List[int] = data.get("home_courts", []) or []

    if text == HOME_SKIP:
        await state.update_data(home_courts=[])
        await message.answer(
            "Окей, пока без домашних кортов.\n\n"
            "Теперь давай оценим твой уровень по шкале NTRP.",
            reply_markup=ntrp_kb,
        )
        await state.set_state(Onboarding.ntrp)
        return

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
async def onboarding_birth_date(message: Message, state: FSMContext):
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
async def onboarding_about(message: Message, state: FSMContext):
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
async def onboarding_photo(message: Message, state: FSMContext):
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
# Создание игры: /newgame
# -----------------------------------------

@dp.message(F.text == "/newgame")
async def newgame_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала нужно заполнить профиль через /start 🙂")
        return
    if not user["ntrp"]:
        await message.answer(
            "У тебя ещё нет рейтинга NTRP. Пройди онбординг заново через /reset, "
            "если что-то пошло не так."
        )
        return

    await state.clear()
    await state.set_state(GameCreation.mode)
    await message.answer(
        "Что создаём?",
        reply_markup=newgame_type_kb,
    )


@dp.message(GameCreation.mode)
async def newgame_mode(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == "Отмена":
        await state.clear()
        await message.answer("Окей, ничего не создаём 🙂", reply_markup=ReplyKeyboardRemove())
        return

    if text not in ["Тренировка", "Матч на рейтинг"]:
        await message.answer(
            "Пожалуйста, выбери один из вариантов:",
            reply_markup=newgame_type_kb,
        )
        return

    await state.update_data(game_type=text)
    await state.set_state(GameCreation.date)
    await message.answer(
        "На какую дату игра?\n"
        "Формат: ДД.ММ.ГГГГ, либо напиши «Сегодня» или «Завтра».",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(GameCreation.date)
async def newgame_date(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    date_str = parse_date_ddmmyyyy(text)
    if not date_str:
        await message.answer(
            "Не получилось распознать дату.\n"
            "Используй формат ДД.ММ.ГГГГ или напиши «Сегодня» / «Завтра».",
        )
        return

    await state.update_data(game_date=date_str)
    await state.set_state(GameCreation.time)
    await message.answer(
        "Во сколько начинаем?\nФормат: ЧЧ:ММ (24 часа), например 19:30.",
    )


@dp.message(GameCreation.time)
async def newgame_time(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    time_str = parse_time_hhmm(text)
    if not time_str:
        await message.answer(
            "Не получилось распознать время.\n"
            "Используй формат ЧЧ:ММ, например 19:30.",
        )
        return

    courts = await get_active_courts()
    if not courts:
        await message.answer(
            "В базе пока нет ни одного корта. Обратись к админу.",
        )
        await state.clear()
        return

    await state.update_data(game_time=time_str)
    await state.set_state(GameCreation.court)
    await message.answer(
        "Выбери корт для игры:",
        reply_markup=build_single_court_kb(courts),
    )


@dp.message(GameCreation.court)
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
            reply_markup=build_single_court_kb(courts),
        )
        return

    await state.update_data(court_id=name_to_id[text])
    await state.set_state(GameCreation.players_count)
    await message.answer(
        "Сколько игроков планируется?",
        reply_markup=players_count_kb,
    )


@dp.message(GameCreation.players_count)
async def newgame_players(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "2 игрока":
        players = 2
    elif text == "4 игрока":
        players = 4
    else:
        await message.answer(
            "Пожалуйста, выбери один из вариантов:",
            reply_markup=players_count_kb,
        )
        return

    await state.update_data(players_count=players)

    data = await state.get_data()
    if data.get("game_type") == "Тренировка":
        # Тренировка – ограничение по рейтингу не обязательно,
        # но по твоей логике это не матч "на рейтинг" → is_ranked=False
        await state.update_data(is_ranked=False, rating_min=None, rating_max=None)
        await state.set_state(GameCreation.comment)
        await message.answer(
            "Добавь комментарий к игре (формат, уровень, пожелания)\n"
            "Или напиши «Пропустить».",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await state.set_state(GameCreation.ranked_choice)
        await message.answer(
            "Нужно ли ограничение по рейтингу?\n\n"
            "Если да — выбери «Да», и далее введёшь диапазон.\n"
            "Если нет — выбери «Нет».",
            reply_markup=yes_no_kb,
        )


@dp.message(GameCreation.ranked_choice)
async def newgame_ranked_choice(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    # В любом случае это «матч на рейтинг», просто с/без фильтра
    if text.startswith("нет"):
        await state.update_data(is_ranked=True, rating_min=None, rating_max=None)
        await state.set_state(GameCreation.comment)
        await message.answer(
            "Добавь комментарий к игре (формат, уровень, пожелания)\n"
            "Или напиши «Пропустить».",
            reply_markup=ReplyKeyboardRemove(),
        )
    elif text.startswith("да"):
        await state.update_data(is_ranked=True)
        await state.set_state(GameCreation.rating_range)
        await message.answer(
            "Введи диапазон рейтинга в формате, например: 3.0-3.75",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "Пожалуйста, выбери «Да» или «Нет».",
            reply_markup=yes_no_kb,
        )


@dp.message(GameCreation.rating_range)
async def newgame_rating_range(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    rng = parse_rating_range(text)
    if not rng:
        await message.answer(
            "Не удалось распознать диапазон.\n"
            "Используй формат, например: 3.0-3.75 (от 1.0 до 7.0).",
        )
        return

    rmin, rmax = rng
    await state.update_data(rating_min=rmin, rating_max=rmax)
    await state.set_state(GameCreation.comment)
    await message.answer(
        "Добавь комментарий к игре (формат, уровень, пожелания)\n"
        "Или напиши «Пропустить».",
    )


@dp.message(GameCreation.comment)
async def newgame_comment(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    comment = None if text.lower().startswith("пропус") else text

    data = await state.get_data()
    await state.clear()

    game_type = data.get("game_type")
    game_date = data.get("game_date")
    game_time = data.get("game_time")
    court_id = data.get("court_id")
    players_count = data.get("players_count")
    is_ranked = bool(data.get("is_ranked", False))
    rating_min = data.get("rating_min")
    rating_max = data.get("rating_max")

    game_id = await create_game(
        creator_id=message.from_user.id,
        court_id=court_id,
        game_date=game_date,
        game_time=game_time,
        game_type=game_type,
        is_ranked=is_ranked,
        players_count=players_count,
        rating_min=rating_min,
        rating_max=rating_max,
        comment=comment,
    )

    game = await get_game(game_id)
    rating_part = "Без ограничений по рейтингу"
    if rating_min is not None and rating_max is not None:
        rating_part = f"Рейтинг: {rating_min:.2f}-{rating_max:.2f}"

    comment_part = f"Комментарий: {comment}" if comment else "Комментарий: —"

    txt = (
        "Игра создана ✅\n\n"
        f"ID: {game_id}\n"
        f"Тип: {game_type}\n"
        f"Дата: {game_date}\n"
        f"Время: {game_time}\n"
        f"Корт: {game['court_name']}\n"
        f"📍 <i>{game['court_address'] or 'Адрес не указан'}</i>\n"
        f"Игроков: {players_count}\n"
        f"{rating_part}\n"
        f"{comment_part}"
    )

    await message.answer(txt, parse_mode="HTML")

# -----------------------------------------
# Список игр и фильтры: /games
# -----------------------------------------

async def send_games_list(message: Message, user_id: int, offset: int = 0):
    games = await list_games_for_user(user_id, offset=offset, limit=10)

    if not games and offset == 0:
        await message.answer(
            "Пока нет доступных игр по твоим фильтрам.\n"
            "Можешь создать свою игру через /newgame.",
        )
        return
    if not games:
        await message.answer("Больше игр не найдено 🙂")
        return

    for g in games:
        rating_part = "Без ограничений по рейтингу"
        if g["rating_min"] is not None and g["rating_max"] is not None:
            rating_part = f"Рейтинг: {g['rating_min']:.2f}-{g['rating_max']:.2f}"

        comment_part = f"Комментарий: {g['comment']}" if g["comment"] else "Комментарий: —"

        text = (
            f"🎾 Игра #{g['id']}\n"
            f"{g['game_type']}\n"
            f"Дата: {g['game_date']}\n"
            f"Время: {g['game_time']}\n"
            f"Корт: {g['court_name']}\n"
            f"📍 <i>{g['court_address'] or 'Адрес не указан'}</i>\n"
            f"Игроков: {g['players_count']}\n"
            f"{rating_part}\n"
            f"{comment_part}\n"
            f"Создатель: {g['creator_name'] or '—'} (рейтинг {g['creator_ntrp'] or '—'})"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подать заявку",
                        callback_data=f"apply:{g['id']}",
                    )
                ]
            ]
        )
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    # Кнопки "Фильтр" и "Ещё"
    more_kb_buttons = [
        [
            InlineKeyboardButton(
                text="Установить фильтр",
                callback_data="setfilter",
            )
        ],
        [
            InlineKeyboardButton(
                text="Показать ещё",
                callback_data=f"more:{offset + 10}",
            )
        ],
    ]
    more_kb = InlineKeyboardMarkup(inline_keyboard=more_kb_buttons)
    await message.answer("Ещё опции:", reply_markup=more_kb)


@dp.message(F.text == "/games")
async def games_cmd(message: Message, state: FSMContext):
    await state.clear()
    await send_games_list(message, message.from_user.id, offset=0)


@dp.callback_query(F.data.startswith("more:"))
async def games_more_callback(query: CallbackQuery):
    try:
        offset = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Ошибка параметров.")
        return
    await query.answer()
    await send_games_list(query.message, query.from_user.id, offset=offset)


@dp.callback_query(F.data == "setfilter")
async def games_setfilter_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(GamesFilter.date)
    await bot.send_message(
        chat_id=query.from_user.id,
        text=(
            "Фильтр по дате: отправь дату в формате ДД.ММ.ГГГГ,\n"
            "или напиши «Любая», чтобы не фильтровать по дате."
        ),
    )


@dp.message(GamesFilter.date)
async def filter_date(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if text == "любая":
        await state.update_data(date=None)
    else:
        d = parse_date_ddmmyyyy(text)
        if not d:
            await message.answer(
                "Не получилось распознать дату.\n"
                "Используй формат ДД.ММ.ГГГГ или напиши «Любая».",
            )
            return
        await state.update_data(date=d)

    await state.set_state(GamesFilter.level)
    await message.answer(
        "Фильтр по уровню: введи диапазон NTRP, например 3.0-3.75,\n"
        "или напиши «Любой», чтобы не фильтровать по уровню.",
    )


@dp.message(GamesFilter.level)
async def filter_level(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if text == "любой":
        await state.update_data(rating_min=None, rating_max=None)
    else:
        rng = parse_rating_range(text)
        if not rng:
            await message.answer(
                "Не удалось распознать диапазон.\n"
                "Используй формат 3.0-3.75 или напиши «Любой».",
            )
            return
        rmin, rmax = rng
        await state.update_data(rating_min=rmin, rating_max=rmax)

    await state.set_state(GamesFilter.home_only)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Показывать только игры на твоих домашних кортах?",
        reply_markup=kb,
    )


@dp.message(GamesFilter.home_only)
async def filter_home_only(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if text.startswith("да"):
        home_only = True
    elif text.startswith("нет"):
        home_only = False
    else:
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(
            "Пожалуйста, выбери «Да» или «Нет».",
            reply_markup=kb,
        )
        return

    data = await state.get_data()
    await state.clear()

    user_game_filters[message.from_user.id] = {
        "date": data.get("date"),
        "rating_min": data.get("rating_min"),
        "rating_max": data.get("rating_max"),
        "home_only": home_only,
    }

    await message.answer(
        "Фильтр обновлён ✅\nПоказываю игры с учётом фильтра:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_games_list(message, message.from_user.id, offset=0)

# -----------------------------------------
# Обработка заявок на игру (callback apply)
# -----------------------------------------

@dp.callback_query(F.data.startswith("apply:"))
async def apply_game_callback(query: CallbackQuery):
    try:
        game_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Ошибка параметров.")
        return

    user = await get_user(query.from_user.id)
    if not user:
        await query.answer("Сначала заполни профиль через /start.", show_alert=True)
        return

    if not user["ntrp"]:
        await query.answer(
            "У тебя нет рейтинга NTRP. Пройди онбординг через /reset.",
            show_alert=True,
        )
        return

    game = await get_game(game_id)
    if not game or game["status"] != "open":
        await query.answer("Игра уже недоступна.", show_alert=True)
        return

    user_rating = float(user["ntrp"])
    g_min = game["rating_min"]
    g_max = game["rating_max"]

    if g_min is not None and g_max is not None:
        if user_rating < g_min or user_rating > g_max:
            await query.answer(
                f"Создатель игры ограничил рейтинг участников: {g_min:.2f}-{g_max:.2f}.",
                show_alert=True,
            )
            return

    ok = await add_game_application(game_id, query.from_user.id)
    if not ok:
        await query.answer("Ты уже подавал заявку на эту игру.", show_alert=True)
        return

    await query.answer("Заявка отправлена ✅", show_alert=False)

    # Уведомляем создателя игры
    try:
        creator_id = game["creator_id"]
        text = (
            "Новая заявка на игру 🎾\n\n"
            f"Игра #{game_id} — {game['game_type']} {game['game_date']} {game['game_time']}\n"
            f"Корт: {game['court_name']}\n"
            f"Игрок: @{query.from_user.username or 'без username'} "
            f"(ID {query.from_user.id}, рейтинг {user_rating:.2f})"
        )
        await bot.send_message(creator_id, text)
    except Exception as e:
        logger.exception("Failed to notify game creator: %s", e)

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