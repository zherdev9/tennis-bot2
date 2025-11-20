#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TENNIS bot.

Версия бота с поддержкой:
- создания игры с режимом создателя (creator_mode: "self" / "others");
- подсчёта укомплектованности матча (Игроки X из Y);
- откликов игроков через заявки (game_applications);
- запрета на переполнение: заявку подать можно всегда, но принять её нельзя, если мест больше нет.
"""

import logging
import os
from datetime import datetime, date, time

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

import aiosqlite

API_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot, storage=MemoryStorage())


# ==========================
# База данных
# ==========================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id           INTEGER UNIQUE,
                name            TEXT,
                rating          REAL,
                created_at      TEXT
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS courts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                address     TEXT
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id              INTEGER PRIMARY PRIMARY KEY AUTOINCREMENT,
                creator_id      INTEGER NOT NULL,
                court_id        INTEGER NOT NULL,
                match_date      TEXT NOT NULL,
                match_time      TEXT NOT NULL,
                game_type       TEXT NOT NULL,     -- 'singles' / 'doubles'
                rating_min      REAL,
                rating_max      REAL,
                players_count   INTEGER NOT NULL,
                comment         TEXT,
                is_court_booked INTEGER NOT NULL DEFAULT 0,
                visibility      TEXT NOT NULL DEFAULT 'public',
                creator_mode    TEXT NOT NULL DEFAULT 'self',  -- 'self' / 'others'
                created_at      TEXT NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_home_courts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                court_id    INTEGER NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_applications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id         INTEGER NOT NULL,
                applicant_id    INTEGER NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected
                created_at      TEXT NOT NULL
            );
            """
        )

        await db.commit()


async def create_user_if_not_exists(tg_user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id FROM users WHERE tg_id = ?;", (tg_user.id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return row["id"]

        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO users (tg_id, name, rating, created_at) VALUES (?, ?, ?, ?);",
            (tg_user.id, tg_user.full_name, None, now),
        )
        await db.commit()

        cursor = await db.execute("SELECT id FROM users WHERE tg_id = ?;", (tg_user.id,))
        row = await cursor.fetchone()
        await cursor.close()
        return row["id"]


async def get_courts():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, title, address FROM courts ORDER BY id;")
        rows = await cursor.fetchall()
        await cursor.close()
        return rows


async def create_game(
    creator_id: int,
    court_id: int,
    match_date: date,
    match_time: time,
    game_type: str,
    rating_min: float | None,
    rating_max: float | None,
    players_count: int,
    comment: str | None,
    is_court_booked: bool,
    visibility: str,
    creator_mode: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        await db.execute(
            """
            INSERT INTO games (
                creator_id, court_id, match_date, match_time,
                game_type, rating_min, rating_max,
                players_count, comment, is_court_booked,
                visibility, creator_mode, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                creator_id,
                court_id,
                match_date.isoformat(),
                match_time.strftime("%H:%M"),
                game_type,
                rating_min,
                rating_max,
                players_count,
                comment,
                1 if is_court_booked else 0,
                visibility,
                creator_mode,
                now,
            ),
        )
        await db.commit()

        cursor = await db.execute("SELECT last_insert_rowid();")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0])


async def get_games_for_listing(limit: int = 20):
    """Список ближайших публичных матчей для вывода в /games."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                g.id,
                g.creator_id,
                g.court_id,
                g.match_date,
                g.match_time,
                g.game_type,
                g.rating_min,
                g.rating_max,
                g.players_count,
                g.comment,
                g.is_court_booked,
                g.visibility,
                g.creator_mode,
                c.title AS court_title,
                c.address AS court_address
            FROM games g
            JOIN courts c ON c.id = g.court_id
            WHERE g.visibility = 'public'
            ORDER BY g.match_date, g.match_time
            LIMIT ?;
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows


async def get_game_by_id(game_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                g.*,
                c.title AS court_title,
                c.address AS court_address
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
    Возвращает (занято, всего) для матча:
    - creator_mode = 'self' → создатель занимает 1 место;
    - creator_mode = 'others' → создатель не считается участником;
    - плюс количество заявок со статусом 'accepted'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT players_count, creator_mode FROM games WHERE id = ?;",
            (game_id,),
        )
        game_row = await cursor.fetchone()
        await cursor.close()
        if not game_row:
            return 0, 0

        total = int(game_row["players_count"])
        base = 1 if game_row["creator_mode"] == "self" else 0

        cursor = await db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM game_applications
            WHERE game_id = ? AND status = 'accepted';
            """,
            (game_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        accepted = int(row["cnt"]) if row and row["cnt"] is not None else 0

        occupied = base + accepted
        return occupied, total


# ==========================
# FSM состояния
# ==========================

class NewGame(StatesGroup):
    creator_mode = State()
    court = State()
    date = State()
    time = State()
    game_type = State()
    rating = State()
    players_count = State()
    comment = State()
    confirm = State()


# ==========================
# Клавиатуры
# ==========================

def main_menu_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("Создать матч 🎾"))
    kb.add(types.KeyboardButton("Список матчей 📋"))
    return kb


creator_mode_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Создаю игру для себя")],
        [types.KeyboardButton(text="Создаю игру для других")],
        [types.KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def game_type_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(types.KeyboardButton("Одиночка (2 игрока)"))
    kb.row(types.KeyboardButton("Пары (4 игрока)"))
    return kb


def cancel_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("Отмена"))
    return kb


# ==========================
# Хендлеры
# ==========================


@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message, state: FSMContext):
    await create_user_if_not_exists(message.from_user)
    await state.finish()
    await message.answer(
        "Привет! Это теннис-бот 🎾\n"
        "Можно создавать матчи и собирать игроков.\n\n"
        "Что делаем?",
        reply_markup=main_menu_kb(),
    )


@dp.message_handler(lambda m: m.text == "Создать матч 🎾")
async def start_new_game(message: types.Message, state: FSMContext):
    await NewGame.creator_mode.set()
    await message.answer(
        "Создаём новую игру 🎾\n\n"
        "Кого ты записываешь на матч?",
        reply_markup=creator_mode_kb,
    )


@dp.message_handler(state=NewGame.creator_mode)
async def newgame_creator_mode(message: types.Message, state: FSMContext):
    text = message.text.strip()

    if text == "Отмена":
        await state.finish()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    if text == "Создаю игру для себя":
        mode = "self"
    elif text == "Создаю игру для других":
        mode = "others"
    else:
        await message.answer("Пожалуйста, выбери один из вариантов кнопками ниже.")
        return

    await state.update_data(creator_mode=mode)

    # Для простоты: пока просим вручную ввести ID корта
    await NewGame.court.set()
    await message.answer(
        "Введи ID корта (пока без справочников, просто число):",
        reply_markup=cancel_kb(),
    )


@dp.message_handler(state=NewGame.court)
async def newgame_court(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "Отмена":
        await state.finish()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    if not text.isdigit():
        await message.answer("ID корта должен быть числом. Попробуй ещё раз.")
        return

    await state.update_data(court_id=int(text))

    await NewGame.date.set()
    await message.answer(
        "На какую дату создаём матч? Формат: ДД.ММ.ГГГГ",
        reply_markup=cancel_kb(),
    )


@dp.message_handler(state=NewGame.date)
async def newgame_date(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "Отмена":
        await state.finish()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    try:
        d = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Не понимаю дату. Нужен формат ДД.ММ.ГГГГ. Попробуй ещё.")
        return

    await state.update_data(match_date=d)

    await NewGame.time.set()
    await message.answer(
        "Во сколько играем? Формат: ЧЧ:ММ (24 часа)",
        reply_markup=cancel_kb(),
    )


@dp.message_handler(state=NewGame.time)
async def newgame_time(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "Отмена":
        await state.finish()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        await message.answer("Не понимаю время. Нужен формат ЧЧ:ММ. Попробуй ещё.")
        return

    await state.update_data(match_time=t)

    await NewGame.game_type.set()
    await message.answer(
        "Это одиночка или парный матч?",
        reply_markup=game_type_kb(),
    )


@dp.message_handler(state=NewGame.game_type)
async def newgame_game_type(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "Отмена":
        await state.finish()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    if text.startswith("Одиночка"):
        game_type = "singles"
        players_count = 2
    elif text.startswith("Пары"):
        game_type = "doubles"
        players_count = 4
    else:
        await message.answer("Выбери тип матча с помощью кнопок.")
        return

    await state.update_data(game_type=game_type, players_count=players_count)

    await NewGame.comment.set()
    await message.answer(
        "Напиши комментарий к матчу (или '-' если без комментария):",
        reply_markup=cancel_kb(),
    )


@dp.message_handler(state=NewGame.comment)
async def newgame_comment(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "Отмена":
        await state.finish()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return

    comment = None if text == "-" else text
    data = await state.get_data()

    creator_mode = data.get("creator_mode", "self")
    court_id = data["court_id"]
    match_date = data["match_date"]
    match_time = data["match_time"]
    game_type = data["game_type"]
    players_count = data["players_count"]

    # В этой версии рейтинг/бронь/видимость фиксированные
    rating_min = None
    rating_max = None
    is_court_booked = False
    visibility = "public"

    game_id = await create_game(
        creator_id=message.from_user.id,
        court_id=court_id,
        match_date=match_date,
        match_time=match_time,
        game_type=game_type,
        rating_min=rating_min,
        rating_max=rating_max,
        players_count=players_count,
        comment=comment,
        is_court_booked=is_court_booked,
        visibility=visibility,
        creator_mode=creator_mode,
    )

    occupied, total = await get_game_occupancy(game_id)

    game_type_txt = "Одиночка" if game_type == "singles" else "Пары"

    txt = (
        "Игра создана ✅\n\n"
        f"ID игры: <b>{game_id}</b>\n"
        f"Тип: {game_type_txt}\n"
        f"Дата: {match_date.strftime('%d.%m.%Y')}\n"
        f"Время: {match_time.strftime('%H:%M')}\n"
        f"Корт ID: {court_id}\n\n"
        f"Игроки: <b>{occupied} из {total}</b>\n"
    )
    if comment:
        txt += f"\nКомментарий:\n{comment}"

    await state.finish()
    await message.answer(txt, reply_markup=main_menu_kb())


@dp.message_handler(lambda m: m.text == "Список матчей 📋")
async def list_games(message: types.Message):
    games = await get_games_for_listing()

    if not games:
        await message.answer("Пока нет доступных матчей.", reply_markup=main_menu_kb())
        return

    for g in games:
        occupied, total = await get_game_occupancy(g["id"])

        game_type_txt = "Одиночка" if g["game_type"] == "singles" else "Пары"
        dt_txt = datetime.fromisoformat(g["match_date"]).strftime("%d.%m.%Y")

        text = (
            f"<b>Матч #{g['id']}</b>\n"
            f"{game_type_txt}\n"
            f"Дата: {dt_txt}\n"
            f"Время: {g['match_time']}\n"
        )

        if g["court_title"]:
            text += f"Корт: {g['court_title']}\n"
        else:
            text += f"Корт ID: {g['court_id']}\n"

        text += f"\nИгроки: <b>{occupied} из {total}</b>\n"

        if g["comment"]:
            text += f"\nКомментарий: {g['comment']}\n"

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                text="Подать заявку на матч",
                callback_data=f"apply_game:{g['id']}",
            )
        )

        await message.answer(text, reply_markup=kb)


# ==========================
# Обработка заявок
# ==========================

@dp.callback_query_handler(lambda c: c.data.startswith("apply_game:"))
async def apply_game_callback(callback: types.CallbackQuery):
    game_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    game = await get_game_by_id(game_id)
    if not game:
        await callback.answer("Матч не найден.", show_alert=True)
        return

    if game["creator_id"] == user_id:
        await callback.answer("Это твой матч, ты и так его создатель 🙂", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Проверяем, не подавал ли уже заявку
        cursor = await db.execute(
            """
            SELECT id, status
            FROM game_applications
            WHERE game_id = ? AND applicant_id = ?;
            """,
            (game_id, user_id),
        )
        existing = await cursor.fetchone()
        await cursor.close()

        if existing:
            if existing["status"] == "pending":
                await callback.answer("У тебя уже есть заявка на этот матч, ждём решения создателя.", show_alert=True)
                return
            elif existing["status"] == "accepted":
                await callback.answer("Ты уже принят в этот матч ✅", show_alert=True)
                return
            # rejected → позволяем подать заново
            await db.execute(
                """
                UPDATE game_applications
                SET status = 'pending', created_at = ?
                WHERE id = ?;
                """,
                (datetime.utcnow().isoformat(), existing["id"]),
            )
        else:
            await db.execute(
                """
                INSERT INTO game_applications (game_id, applicant_id, status, created_at)
                VALUES (?, ?, 'pending', ?);
                """,
                (game_id, user_id, datetime.utcnow().isoformat()),
            )

        await db.commit()

    await callback.answer("Заявка отправлена создателю матча ✅", show_alert=True)

    # Уведомим создателя (если возможно)
    creator_id = game["creator_id"]
    try:
        occupied, total = await get_game_occupancy(game_id)
        txt = (
            f"Новая заявка на матч #{game_id} 🎾\n\n"
            f"От: <b>{callback.from_user.full_name}</b> (ID: {user_id})\n\n"
            f"Текущая укомплектованность: {occupied} из {total}\n\n"
            f"Принять или отклонить заявку?"
        )

        kb = types.InlineKeyboardMarkup()
        # Нужно идентифицировать конкретную заявку.
        # Упростим: повторно найдём её ID.
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id
                FROM game_applications
                WHERE game_id = ? AND applicant_id = ? AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1;
                """,
                (game_id, user_id),
            )
            app = await cursor.fetchone()
            await cursor.close()

        if app:
            app_id = app["id"]
            kb.add(
                types.InlineKeyboardButton(
                    text="Принять ✅",
                    callback_data=f"app_decision:{app_id}:accepted",
                ),
                types.InlineKeyboardButton(
                    text="Отклонить ❌",
                    callback_data=f"app_decision:{app_id}:rejected",
                ),
            )

            await bot.send_message(chat_id=creator_id, text=txt, reply_markup=kb)
    except Exception as e:
        logger.exception("Не удалось отправить уведомление создателю матча: %s", e)


@dp.callback_query_handler(lambda c: c.data.startswith("app_decision:"))
async def app_decision_callback(callback: types.CallbackQuery):
    """
    Создатель матча подтверждает/отклоняет заявку.

    ВАЖНО: здесь мы запрещаем переполнять матч.
    - Заявку можно создать всегда (apply_game_callback не проверяет лимит).
    - Но принять заявку нельзя, если матч уже укомплектован (occupied >= total).
    """
    try:
        _, app_id_str, action = callback.data.split(":")
        app_id = int(app_id_str)
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT ga.id, ga.game_id, ga.applicant_id, ga.status,
                   g.creator_id
            FROM game_applications ga
            JOIN games g ON g.id = ga.game_id
            WHERE ga.id = ?;
            """,
            (app_id,),
        )
        app = await cursor.fetchone()
        await cursor.close()

        if not app:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        game_id = app["game_id"]
        applicant_id = app["applicant_id"]
        creator_id = app["creator_id"]

        # Проверяем, что решение принимает именно создатель матча
        if callback.from_user.id != creator_id:
            await callback.answer("Только создатель матча может решать по заявкам.", show_alert=True)
            return

        # Если заявка уже не в pending — повторно ничего не делаем
        if app["status"] != "pending":
            await callback.answer("По этой заявке уже принято решение.", show_alert=True)
            return

        if action == "accepted":
            # Проверяем заполненность
            occupied, total = await get_game_occupancy(game_id)
            if occupied >= total:
                await callback.message.answer(
                    f"❗ Мест больше нет.\n"
                    f"Матч уже укомплектован: {occupied} из {total}.\n\n"
                    f"Принять нового игрока невозможно."
                )
                await callback.answer()
                return

            await db.execute(
                "UPDATE game_applications SET status = 'accepted' WHERE id = ?;",
                (app_id,),
            )
            await db.commit()

            # После успешного приёма ещё раз покажем укомплектованность
            occupied_after, total_after = await get_game_occupancy(game_id)

            await callback.message.answer(
                f"Заявка игрока ID {applicant_id} принята ✅\n"
                f"Текущая укомплектованность матча #{game_id}: "
                f"<b>{occupied_after} из {total_after}</b>"
            )

            # Уведомим игрока
            try:
                await bot.send_message(
                    chat_id=applicant_id,
                    text=(
                        f"Твоя заявка на матч #{game_id} принята ✅\n"
                        f"Увидимся на корте! 🎾"
                    ),
                )
            except Exception as e:
                logger.exception("Не удалось уведомить игрока: %s", e)

            await callback.answer("Заявка принята.")
            return

        elif action == "rejected":
            await db.execute(
                "UPDATE game_applications SET status = 'rejected' WHERE id = ?;",
                (app_id,),
            )
            await db.commit()

            await callback.message.answer(
                f"Заявка игрока ID {applicant_id} отклонена ❌"
            )

            # Уведомим игрока
            try:
                await bot.send_message(
                    chat_id=applicant_id,
                    text=(
                        f"К сожалению, твоя заявка на матч #{game_id} отклонена ❌"
                    ),
                )
            except Exception as e:
                logger.exception("Не удалось уведомить игрока: %s", e)

            await callback.answer("Заявка отклонена.")
            return

        else:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return


# ==========================
# Запуск
# ==========================

async def on_startup(dispatcher: Dispatcher):
    await init_db()
    logger.info("Бот запущен и БД инициализирована.")


def main():
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)


if __name__ == "__main__":
    main()
