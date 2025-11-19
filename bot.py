import os
import re
import asyncio
import logging
from datetime import date
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

# -----------------------------------------
# Стартовый список кортов Москвы
# (админ потом может добавлять новые записи в courts через SQL)
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

HOME_DONE = "Готово ✅"
HOME_SKIP = "Пропустить"

# -----------------------------------------
# FSM анкеты, редактирования, поддержки
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

# -----------------------------------------
# Хелперы
# -----------------------------------------

def calculate_age_from_str(birth_date_str: str) -> Optional[int]:
    """
    birth_date_str: 'ДД.ММ.Г