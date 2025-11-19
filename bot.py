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
# Заглушка: список кортов Москвы
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
    ntrp = State()              # базовый уровень по шкале NTRP
    play_experience = State()   # играли ли в падел/теннис/сквош
    matches_6m = State()        # сколько матчей за 6 месяцев
    fitness = State()           # физподготовка
    tournaments = State()       # турнирный опыт: Tour / Masters / не участвовал
    birth_date = State()        # дата рождения
    about = State()
    photo = State()

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
            KeyboardButton(text="5.0–5.5: сильный любитель, турниры и матчи"),
        ],
        [
            KeyboardButton(text="6.0–7.0: очень сильный, почти профи/профи"),
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
        # users
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                gender TEXT,
                city TEXT,
                ntrp REAL,          -- итоговый рейтинг
                ntrp_self REAL,     -- самооценка пользователя
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

    # На случай, если таблица создавалась старой версией
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
    username: str | None,
    name: str | None,
    gender: str | None,
    city: str | None,
    ntrp: float | None,
    ntrp_self: float | None,
    play_experience: str | None,
    matches_6m: str | None,
    fitness: str | None,
    tournaments: str | None,
    birth_date: str | None,
    about: str | None,
    photo_file_id: str | None,
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
# Логика пересчёта рейтинга
# -----------------------------------------

def parse_ntrp_from_button(text: str) -> float | None:
    if not text:
        return None
    head = text.split(":", 1)[0].strip()
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


def compute_final_ntrp(
    base_ntrp: float,
    play_experience: str | None,
    matches_6m: str | None,
    fitness: str | None,
    tournaments: str | None,
) -> float:
    """
    Простая модель:
    - базовый рейтинг = выбор пользователя;
    - модификаторы дают ±0.1–0.3 суммарно.
    Пример: выбрал 3.0, играл в этом году и участвовал в Tour → рейтинг ~3.2–3.3.
    """
    mod = 0.0
    pe = (play_experience or "").lower()
    m6 = (matches_6m or "").lower()
    fit = (fitness or "").lower()
    tour = (tournaments or "").lower()

    # Опыт игры
    if "никогда" in pe:
        mod -= 0.3
    elif "в этом году" in pe:
        mod += 0.1
    elif "более года" in pe:
        mod -= 0.05
    elif "более пяти" in pe:
        mod -= 0.15

    # Кол-во матчей за 6 мес
    if "0–10" in m6 or "0-10" in m6:
        mod += 0.0
    elif "10–100" in m6 or "10-100" in m6:
        mod += 0.15
    elif "100" in m6:
        mod += 0.3

    # Физподготовка
    if "низкая" in fit:
        mod -= 0.15
    elif "хорошая" in fit:
        mod += 0.0
    elif "отличная" in fit:
        mod += 0.15

    # Турниры
    if "tour" in tour:
        mod += 0.15
    elif "masters" in tour:
        mod += 0.3

    final = base_ntrp + mod
    if final < 1.0:
        final = 1.0
    if final > 7.0:
        final = 7.0

    # Округлим до сотых
    return round(final, 2)

# -----------------------------------------
# Хэндлеры онбординга
# -----------------------------------------

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if user:
        await state.clear()
        await message.answer(
            "Привет 👋\n"
            "Ты уже проходил анкету.\n"
            "Посмотреть профиль → /me",
        )
        return

    await message.answer(
        "Привет 👋\nМеня зовут TennisBot.\n"
        "Сейчас за минуту настроим твой профиль.\n\n"
        "Как тебя подписывать?",
        reply_markup=ReplyKeyboardRemove(),
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
        "В каком городе ты обычно играешь?\nПока основной фокус — Москва 😊",
        reply_markup=city_kb,
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
        "Кнопки ниже с описанием навыков: как держишь розыгрыш, "
        "направление и глубину мяча и т.д.",
        reply_markup=ntrp_kb,
    )
    await state.set_state(Onboarding.ntrp)


@dp.message(Onboarding.ntrp)
async def get_ntrp(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    base_ntrp = parse_ntrp_from_button(raw)

    if base_ntrp is None:
        try:
            base_ntrp = float(raw.replace(",", "."))
        except ValueError:
            await message.answer(
                "Это не похоже на уровень NTRP 🤔\n"
                "Выбери одну из кнопок или введи число от 1.0 до 7.0.",
            )
            return

    if not (1.0 <= base_ntrp <= 7.0):
        await message.answer(
            "Шкала NTRP от 1.0 до 7.0.\n"
            "Попробуй ещё раз 🙂",
        )
        return

    await state.update_data(ntrp_self=base_ntrp)

    await message.answer(
        "Играешь ли ты в падел, теннис или сквош?",
        reply_markup=play_experience_kb,
    )
    await state.set_state(Onboarding.play_experience)


@dp.message(Onboarding.play_experience)
async def get_play_experience(message: Message, state: FSMContext):
    await state.update_data(play_experience=message.text)

    await message.answer(
        "Сколько матчей ты сыграл за последние 6 месяцев?",
        reply_markup=matches_6m_kb,
    )
    await state.set_state(Onboarding.matches_6m)


@dp.message(Onboarding.matches_6m)
async def get_matches_6m(message: Message, state: FSMContext):
    await state.update_data(matches_6m=message.text)

    await message.answer(
        "Оцени свою общую физическую подготовку:",
        reply_markup=fitness_kb,
    )
    await state.set_state(Onboarding.fitness)


@dp.message(Onboarding.fitness)
async def get_fitness(message: Message, state: FSMContext):
    await state.update_data(fitness=message.text)

    await message.answer(
        "Участвовал ли ты в турнирах?\n\n"
        "• Tour — массовые любительские турниры\n"
        "• Masters — более высокий уровень\n",
        reply_markup=tournaments_kb,
    )
    await state.set_state(Onboarding.tournaments)


@dp.message(Onboarding.tournaments)
async def get_tournaments(message: Message, state: FSMContext):
    await state.update_data(tournaments=message.text)

    await message.answer(
        "Укажи дату рождения в формате ДД.ММ.ГГГГ\n"
        "Или нажми «Пропустить».",
        reply_markup=skip_kb,
    )
    await state.set_state(Onboarding.birth_date)


@dp.message(Onboarding.birth_date)
async def get_birth_date(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower().startswith("пропус"):
        birth_date = None
    else:
        birth_date = text  # пока просто храндим строкой, без валидации
    await state.update_data(birth_date=birth_date)

    await message.answer(
        "Напиши немного о себе: как играешь и что ищешь.\n"
        "Или нажми «Пропустить».",
        reply_markup=skip_kb,
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
        "Финальный штрих — добавь фото для профиля 📷\n\n"
        "Отправь фото или нажми «Пропустить».",
        reply_markup=skip_kb,
    )
    await state.set_state(Onboarding.photo)


@dp.message(Onboarding.photo)
async def get_photo(message: Message, state: FSMContext):
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    else:
        text = (message.text or "").lower()
        if not text.startswith("пропус"):
            await message.answer("Отправь фото или нажми «Пропустить» 🙂")
            return

    data = await state.get_data()
    await state.clear()

    base_ntrp = data.get("ntrp_self")  # float, мы сохраняли выше
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

    text = (
        f"Профиль сохранён! 🎾\n\n"
        f"Твоя самооценка: {base_ntrp:.2f}\n"
        f"Начальный рейтинг в боте (с учётом опыта и турниров): {final_ntrp:.2f}\n\n"
        f"Посмотреть профиль → /me"
    )

    await message.answer(text, reply_markup=ReplyKeyboardRemove())

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
        f"Самооценка NTRP: {user['ntrp_self'] or '—'}\n"
        f"Начальный рейтинг бота: {user['ntrp'] or '—'}\n"
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