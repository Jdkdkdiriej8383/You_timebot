# main.py
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3
import re

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    PreCheckoutQuery,
    SuccessfulPayment
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from config import Config

logging.basicConfig(level=Config.LOG_LEVEL)
logger = logging.getLogger(__name__)

bot = Bot(token=Config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# === FSM States ===
class EventStates:
    waiting_title = "waiting_title"
    waiting_description = "waiting_description"
    waiting_year = "waiting_year"
    waiting_month = "waiting_month"
    waiting_day = "waiting_day"
    waiting_hour_minute = "waiting_hour_minute"
    creating_group_name = "creating_group_name"
    joining_group_id = "joining_group_id"
    waiting_scope = "waiting_scope"
    waiting_curated_client = "waiting_curated_client"


# === Города для определения часового пояса ===
CITIES_DB = [
    {"name": "Москва", "lat": 55.7558, "lon": 37.6176, "tz": "Europe/Moscow"},
    {"name": "Екатеринбург", "lat": 56.8389, "lon": 60.6057, "tz": "Asia/Yekaterinburg"},
]


def find_closest_timezone(lat: float, lon: float):
    import math
    def distance(lat1, lon1, lat2, lon2):
        return math.sqrt((lat1 - lat2)**2 + (lon1 - lon2)**2)
    closest = None
    min_dist = float("inf")
    for city in CITIES_DB:
        d = distance(lat, lon, city["lat"], city["lon"])
        if d < min_dist:
            min_dist = d
            closest = city
    return closest["tz"], closest["name"]


TIMEZONES_LIST = [
    ("Europe/Kaliningrad", "UTC+2 — Калининград"),
    ("Europe/Moscow", "UTC+3 — Москва"),
    ("Asia/Yekaterinburg", "UTC+5 — Екатеринбург"),
    ("Asia/Vladivostok", "UTC+11 — Владивосток"),
]


# === Инициализация базы данных ===
def init_db():
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'Europe/Moscow',
            username TEXT,
            first_name TEXT,
            subscription_type TEXT DEFAULT 'free',
            subscription_expire TEXT,
            auto_renew INTEGER DEFAULT 1,
            subscription_start TEXT
        );
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            event_time TEXT,
            created_by INTEGER,
            chat_type TEXT,
            chat_id INTEGER,
            notified_7d INTEGER DEFAULT 1,
            notified_1 INTEGER DEFAULT 1,
            notified_15m INTEGER DEFAULT 1,
            file_type TEXT,
            file_id TEXT,
            recurrence TEXT
        );
        CREATE TABLE IF NOT EXISTS curator_client (
            curator_id INTEGER,
            client_id INTEGER,
            added_at TEXT,
            PRIMARY KEY (curator_id, client_id)
        );
    """)

    for col in ["notified_7d", "notified_1", "notified_15m"]:
        try: cursor.execute(f"ALTER TABLE events ADD COLUMN {col} INTEGER DEFAULT 1")
        except: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN auto_renew INTEGER DEFAULT 1")
    except: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN subscription_start TEXT")
    except: pass

    conn.commit()
    conn.close()


def register_user(user):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, username, first_name, timezone)
        VALUES (?, ?, ?, COALESCE((SELECT timezone FROM users WHERE user_id = ?), 'Europe/Moscow'))
    """, (user.id, user.username, user.first_name, user.id))
    conn.commit()
    conn.close()


def get_subscription_status(user_id: int):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT subscription_type, subscription_expire, auto_renew FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return "free", None, 1
    sub_type, expire_str, auto_renew = row
    if sub_type == "premium":
        if expire_str:
            try:
                expire = datetime.strptime(expire_str, "%Y-%m-%d %H:%M")
                if expire > datetime.now():
                    return "premium", expire_str, auto_renew
            except: pass
    return "free", None, auto_renew


def has_access(user_id: int) -> bool:
    if user_id == Config.OWNER_ID:
        return True
    status, _, _ = get_subscription_status(user_id)
    return status == "premium"


def get_user_timezone(user_id: int) -> str:
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "Europe/Moscow"


def add_event(chat_type: str, chat_id: int, creator_id: int, title: str, desc: str,
              local_time_str: str, tz_name: str, file_type=None, file_id=None, recurrence=None):
    try:
        local_tz = ZoneInfo(tz_name)
        utc_tz = ZoneInfo("UTC")
        local_dt = datetime.strptime(local_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        utc_dt = local_dt.astimezone(utc_tz)
        utc_time_str = utc_dt.strftime("%Y-%m-%d %H:%M")

        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO events (title, description, event_time, created_by, chat_type, chat_id, file_type, file_id, recurrence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, desc, utc_time_str, creator_id, chat_type, chat_id, file_type, file_id, recurrence))
        conn.commit()
        conn.close()
        return True, utc_dt
    except Exception as e:
        logger.error(f"Ошибка добавления события: {e}")
        return False, None


# === Главное меню ===
def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="➕ Создать событие"), KeyboardButton(text="📋 Мои события")],
        [KeyboardButton(text="👥 Группы"), KeyboardButton(text="💳 Оплатить")],
        [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="⚙️ Профиль")]
    ]

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM curator_client WHERE curator_id = ?", (user_id,))
    is_curator = cursor.fetchone()
    conn.close()

    if is_curator:
        kb.insert(2, [KeyboardButton(text="👨‍🏫 Курируемые")])

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# === /start ===
@dp.message(Command("start"))
async def start(message: Message):
    register_user(message.from_user)
    await message.answer(
        f"Привет, {message.from_user.first_name}! 🎉\n\n"
        "Я помогу тебе не забыть важное — события, встречи, дедлайны.\n"
        "Выбери действие в меню ниже.",
        reply_markup=get_main_menu(message.from_user.id)
    )


# === Кнопки ===
@dp.message(F.text == "🔙 Назад")
async def go_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))


# === Помощь ===
@dp.message(F.text == "❓ Помощь")
async def help_command(message: Message):
    text = (
        "📘 *Помощь — как пользоваться*\n\n"
        "🎯 *Создать событие*\n"
        "Нажми «➕ Создать событие» → укажи название, дату, время → выбери напоминания.\n\n"
        
        "👥 *Группы*\n"
        "Создай группу → получи ID → отправь друзьям.\n"
        "Они вводят ID и становятся участниками.\n\n"
        
        "👨‍🏫 *Кураторство*\n"
        "Клиент: нажми «Добавить куратора» → получи команду.\n"
        "Куратор: введи команду → сможет назначать события.\n\n"
        
        "💳 *Подписка*\n"
        "Оплати премиум → получи 26 напоминаний, хранение файлов, приоритет.\n"
        "Автопродление можно отключить командой /off\n\n"
        
        "🛠 *Техподдержка*\n"
        "Если что-то не работает — пиши: @helper_tp"
    )
    kb = [[KeyboardButton(text="🔙 Назад")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


# === Оплата через ЮKassa ===
@dp.message(F.text == "💳 Оплатить")
async def show_pricing(message: Message):
    text = (
        "💎 *Выбери тариф*\n\n"
        "🔹 *30 дней* — 100₽\n"
        "🔸 *90 дней* — 270₽\n"
        "🔸 *365 дней* — 990₽\n\n"
        "Все тарифы с автопродлением. Можно отключить командой /off"
    )
    kb = [
        [KeyboardButton(text="30 дней — 100₽")],
        [KeyboardButton(text="90 дней — 270₽")],
        [KeyboardButton(text="365 дней — 990₽")],
        [KeyboardButton(text="🚫 Отключить автопродление")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@dp.message(F.text.contains("дней"))
async def handle_payment_choice(message: Message):
    user_id = message.from_user.id

    if "30 дней" in message.text:
        amount = 10000
        days = 30
        payload = f"premium_30_{user_id}"
    elif "90 дней" in message.text:
        amount = 27000
        days = 90
        payload = f"premium_90_{user_id}"
    elif "365 дней" in message.text:
        amount = 99000
        days = 365
        payload = f"premium_365_{user_id}"
    else:
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET auto_renew = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    if not Config.YOOKASSA_PROVIDER_TOKEN or "TEST" not in Config.YOOKASSA_PROVIDER_TOKEN:
        await message.answer("🔧 Оплата временно недоступна.")
        return

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Премиум-доступ",
        description=f"Доступ на {days} дней с автопродлением",
        payload=payload,
        provider_token=Config.YOOKASSA_PROVIDER_TOKEN,
        currency="RUB",
        prices=[{"label": f"{days} дней", "amount": amount}],
        start_parameter="premium",
        need_email=False,
        is_flexible=False,
        send_notification_to_bot=True,
        protect_content=True
    )


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    successful_payment: SuccessfulPayment = message.successful_payment
    payload = successful_payment.invoice_payload
    user_id = message.from_user.id

    if "30" in payload:
        days = 30
    elif "90" in payload:
        days = 90
    else:
        days = 365

    expire_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    start_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET subscription_type = 'premium', 
            subscription_expire = ?, 
            subscription_start = ?, 
            auto_renew = 1 
        WHERE user_id = ?
    """, (expire_date, start_date, user_id))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Подписка активирована до {expire_date}\n🔁 Автопродление включено")


# === /off — отключить автопродление ===
@dp.message(Command("off"))
async def disable_auto_renew(message: Message):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET auto_renew = 0 WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    await message.answer("❌ Автопродление отключено.")


# === Кнопка "Отключить автопродление" ===
@dp.message(F.text == "🚫 Отключить автопродление")
async def cancel_auto_renew_button(message: Message):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET auto_renew = 0 WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    await message.answer("❌ Автопродление отключено.", reply_markup=get_main_menu(message.from_user.id))


# === Профиль ===
@dp.message(F.text == "⚙️ Профиль")
async def profile(message: Message):
    tz = get_user_timezone(message.from_user.id)
    status, expire, auto_renew = get_subscription_status(message.from_user.id)

    if message.from_user.id == Config.OWNER_ID:
        sub_text = "💎 Премиум (владелец)"
    else:
        sub_text = "💎 Премиум" if status == "premium" else "🆓 Бесплатно"
        if expire and expire != "forever":
            sub_text += f"\nдо {expire}"
        if auto_renew == 1:
            sub_text += "\n🔁 Автопродление включено"

    kb = [
        [KeyboardButton(text="🌍 Сменить часовой пояс")],
        [KeyboardButton(text="📍 Определить по геолокации")]
    ]

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM curator_client WHERE client_id = ?", (message.from_user.id,))
    has_curators = cursor.fetchone()
    conn.close()

    if has_curators:
        kb.append([KeyboardButton(text="👥 Мои кураторы")])
    kb.append([KeyboardButton(text="➕ Добавить куратора")])
    kb.append([KeyboardButton(text="🔙 Назад")])

    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(
        f"🔧 Твой профиль:\n\n"
        f"🌍 Часовой пояс: `{tz}`\n"
        f"🎟 Подписка: {sub_text}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# === Геолокация ===
@dp.message(F.text == "📍 Определить по геолокации")
async def request_location(message: Message):
    kb = [[KeyboardButton(text="📍 Отправить мою геопозицию", request_location=True)], [KeyboardButton(text="❌ Отмена")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Отправь геопозицию:", reply_markup=keyboard)


@dp.message(F.location)
async def handle_location(message: Message):
    lat, lon = message.location.latitude, message.location.longitude
    tz, city = find_closest_timezone(lat, lon)
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (message.from_user.id,))
    row = cursor.fetchone()
    old_tz = row[0] if row else "Europe/Moscow"
    cursor.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (tz, message.from_user.id))
    conn.commit()
    conn.close()

    reschedule_events_for_user(message.from_user.id, old_tz, tz)

    await message.answer(f"✅ Часовой пояс: {tz} ({city})", reply_markup=get_main_menu(message.from_user.id))


def reschedule_events_for_user(user_id: int, old_tz: str, new_tz: str):
    try:
        old_zone = ZoneInfo(old_tz)
        new_zone = ZoneInfo(new_tz)
        now_utc = datetime.now(ZoneInfo("UTC"))

        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, event_time FROM events
            WHERE created_by = ? AND event_time > ?
        """, (user_id, now_utc.strftime("%Y-%m-%d %H:%M")))
        rows = cursor.fetchall()

        for event_id, utc_time_str in rows:
            utc_dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
            old_local = utc_dt.astimezone(old_zone)
            new_local = old_local.astimezone(new_zone)
            new_utc = new_local.astimezone(ZoneInfo("UTC"))
            new_utc_str = new_utc.strftime("%Y-%m-%d %H:%M")
            cursor.execute("UPDATE events SET event_time = ? WHERE id = ?", (new_utc_str, event_id))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка пересчёта: {e}")


# === Ручной выбор TZ ===
@dp.message(F.text == "🌍 Сменить часовой пояс")
async def select_timezone(message: Message):
    kb = [[KeyboardButton(text=name)] for _, name in TIMEZONES_LIST]
    kb.append([KeyboardButton(text="❌ Отмена")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("Выбери:", reply_markup=keyboard)


@dp.message(F.text.contains("UTC+"))
async def set_timezone(message: Message):
    for code, name in TIMEZONES_LIST:
        if name == message.text:
            conn = sqlite3.connect(Config.DATABASE_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (message.from_user.id,))
            row = cursor.fetchone()
            old_tz = row[0] if row else "Europe/Moscow"
            cursor.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (code, message.from_user.id))
            conn.commit()
            conn.close()

            reschedule_events_for_user(message.from_user.id, old_tz, code)

            await message.answer(f"✅ Установлено: {code}", reply_markup=get_main_menu(message.from_user.id))
            return
    await message.answer("❌ Ошибка.")


# === Кураторство ===
@dp.message(F.text == "➕ Добавить куратора")
async def add_curator_cmd(message: Message):
    cmd = f"/addclient_{message.from_user.id}"
    await message.answer(f"Отправь куратору:\n`{cmd}`", parse_mode="Markdown")


@dp.message(Command("addclient"))
async def add_client(message: Message):
    try:
        client_id = int(message.text.split("_")[1])
        if client_id == message.from_user.id:
            await message.answer("❌ Нельзя быть куратором самому себе.")
            return

        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO curator_client (curator_id, client_id, added_at) VALUES (?, ?, ?)",
                       (message.from_user.id, client_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        await message.answer("✅ Вы теперь куратор этого пользователя.")
        await bot.send_message(client_id, f"🔔 Вас добавили как клиента.")
    except:
        await message.answer("❌ Неверная команда.")


@dp.message(F.text == "👨‍🏫 Курируемые")
async def list_clients(message: Message):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.user_id, u.first_name FROM curator_client cc
        JOIN users u ON cc.client_id = u.user_id
        WHERE cc.curator_id = ?
    """, (message.from_user.id,))
    clients = cursor.fetchall()
    conn.close()

    if not clients:
        await message.answer("📭 Нет курируемых.")
        return

    kb = [[KeyboardButton(text=f"👤 {name} (ID: {uid})")] for uid, name in clients]
    kb.append([KeyboardButton(text="🔙 Назад")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("Выберите клиента:", reply_markup=keyboard)


@dp.message(F.text.startswith("👤 "))
async def view_client_profile(message: Message, state: FSMContext):
    try:
        client_id = int(message.text.split("ID: ")[1].strip(")"))
    except:
        await message.answer("❌ Ошибка ID.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM curator_client WHERE curator_id = ? AND client_id = ?", (message.from_user.id, client_id))
    if not cursor.fetchone():
        await message.answer("❌ Не ваш клиент.")
        conn.close()
        return

    cursor.execute("SELECT first_name FROM users WHERE user_id = ?", (client_id,))
    name = cursor.fetchone()[0]

    cursor.execute("""
        SELECT title, event_time FROM events
        WHERE chat_id = ? AND event_time > ?
        ORDER BY event_time LIMIT 1
    """, (client_id, datetime.now().strftime("%Y-%m-%d %H:%M")))
    event_row = cursor.fetchone()
    next_event = event_row[0] if event_row else "Нет"
    conn.close()

    kb = [
        [KeyboardButton(text="📅 Назначить событие")],
        [KeyboardButton(text="🗑 Удалить клиента")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(f"👨‍💼 {name}\n⏰ Следующее: {next_event}", reply_markup=keyboard)
    
    await state.update_data(curated_client_id=client_id)
    await state.set_state(EventStates.waiting_curated_client)


@dp.message(F.text == "🗑 Удалить клиента")
async def remove_client(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("curated_client_id")
    
    if not client_id:
        await message.answer("❌ Клиент не выбран.")
        await state.clear()
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM curator_client WHERE curator_id = ? AND client_id = ?", (message.from_user.id, client_id))
    conn.commit()
    conn.close()

    await message.answer("🗑 Клиент удалён.", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


# === Группы ===
@dp.message(F.text == "👥 Группы")
async def groups_menu(message: Message):
    kb = [
        [KeyboardButton(text="➕ Создать группу")],
        [KeyboardButton(text="🚪 Вступить по коду")],
        [KeyboardButton(text="🗂 Мои группы")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("🔧 Управление группами:", reply_markup=keyboard)


@dp.message(F.text == "➕ Создать группу")
async def create_group_prompt(message: Message, state: FSMContext):
    await state.set_state(EventStates.creating_group_name)
    kb = [[KeyboardButton(text="❌ Отмена")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("📝 Введите название группы:", reply_markup=keyboard)

@dp.message(EventStates.creating_group_name)
async def create_group_finish(message: Message, state: FSMContext):
    group_name = message.text.strip()
    if not group_name:
        await message.answer("❌ Название не может быть пустым.")
        return

    # Генерация уникального ID группы на основе хеша
    group_id = abs(hash(f"{message.from_user.id}_{group_name}")) % (10**10)

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    try:
        # Проверка, не превышен ли лимит групп у пользователя
        cursor.execute("SELECT COUNT(*) FROM groups WHERE owner_id = ?", (message.from_user.id,))
        count = cursor.fetchone()[0]
        if count >= 5:
            await message.answer("❌ Вы не можете создать больше 5 групп.")
            await state.clear()
            return

        # Создание группы
        cursor.execute("""
            INSERT INTO groups (group_id, group_name, owner_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (group_id, group_name, message.from_user.id, datetime.now().isoformat()))

        # Добавление создателя в участники
        cursor.execute("""
            INSERT INTO group_members (group_id, user_id) VALUES (?, ?)
        """, (group_id, message.from_user.id))

        conn.commit()
        await message.answer(
            f"✅ Группа *{group_name}* создана!\n"
            f"🔢 Код для вступления: `{group_id}`\n\n"
            f"Отправьте этот код своим друзьям.",
            parse_mode="Markdown",
            reply_markup=get_main_menu(message.from_user.id)
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ Группа с таким ID уже существует. Попробуйте другое название.")
    finally:
        conn.close()
        await state.clear()


@dp.message(F.text == "🚪 Вступить по коду")
async def join_group_prompt(message: Message, state: FSMContext):
    await state.set_state(EventStates.joining_group_id)
    kb = [[KeyboardButton(text="❌ Отмена")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("🔢 Введите ID группы:", reply_markup=keyboard)


@dp.message(EventStates.joining_group_id)
async def join_group_by_id(message: Message, state: FSMContext):
    try:
        group_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите число.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT group_name FROM groups WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        await message.answer("❌ Группа не найдена.")
        conn.close()
        return

    group_name = row[0]
    user_id = message.from_user.id

    try:
        cursor.execute("INSERT INTO group_members (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
        conn.commit()
        await message.answer(f"✅ Вы вступили в группу *{group_name}*", parse_mode="Markdown", reply_markup=get_main_menu(user_id))
    except sqlite3.IntegrityError:
        await message.answer(f"Вы уже состоите в группе *{group_name}*", parse_mode="Markdown")
    finally:
        conn.close()
        await state.clear()


@dp.message(F.text == "🗂 Мои группы")
async def my_groups(message: Message):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.group_name, g.group_id FROM group_members gm
        JOIN groups g ON gm.group_id = g.group_id
        WHERE gm.user_id = ?
    """, (message.from_user.id,))
    groups = cursor.fetchall()
    conn.close()

    if not groups:
        await message.answer("📭 Вы не состоите ни в одной группе.")
        return

    text = "🗂 *Ваши группы:*\n\n"
    for name, gid in groups:
        text += f"• `{gid}` — {name}\n"
    await message.answer(text, parse_mode="Markdown")


# === Создание события — с выбором группы ===
@dp.message(F.text == "➕ Создать событие")
async def create_event_start(message: Message, state: FSMContext):
    await state.set_state(EventStates.waiting_title)
    kb = [[KeyboardButton(text="❌ Отмена")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("🎯 Введите название события:", reply_markup=keyboard)


@dp.message(EventStates.waiting_title)
async def get_event_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if len(title) > 100:
        await message.answer("❌ Слишком длинное название. Максимум 100 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(EventStates.waiting_description)
    await message.answer("📝 Описание (или /skip):")


@dp.message(EventStates.waiting_description)
async def get_event_desc(message: Message, state: FSMContext):
    desc = message.text if message.text and not message.text.startswith("/") else ""
    if len(desc) > 500:
        await message.answer("❌ Описание слишком длинное. Максимум 500 символов.")
        return
    await state.update_data(description=desc)
    await state.set_state(EventStates.waiting_year)
    await message.answer("📅 Год (например, 2025):")


@dp.message(EventStates.waiting_year)
async def get_event_year(message: Message, state: FSMContext):
    try:
        year = int(message.text)
        if not (2023 <= year <= 2100):
            raise ValueError
        await state.update_data(year=year)
        await state.set_state(EventStates.waiting_month)
        kb = [
            [KeyboardButton(text="Январь (1)"), KeyboardButton(text="Февраль (2)"), KeyboardButton(text="Март (3)")],
            [KeyboardButton(text="Апрель (4)"), KeyboardButton(text="Май (5)"), KeyboardButton(text="Июнь (6)")],
            [KeyboardButton(text="Июль (7)"), KeyboardButton(text="Август (8)"), KeyboardButton(text="Сентябрь (9)")],
            [KeyboardButton(text="Октябрь (10)"), KeyboardButton(text="Ноябрь (11)"), KeyboardButton(text="Декабрь (12)")],
        ]
        keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
        await message.answer("📆 Выберите месяц:", reply_markup=keyboard)
    except:
        await message.answer("❌ Неверный год. Попробуйте снова:")


@dp.message(EventStates.waiting_month)
async def get_event_month(message: Message, state: FSMContext):
    try:
        text = message.text.split("(")[1].strip(")")
        month = int(text)
        if not (1 <= month <= 12):
            raise ValueError
        await state.update_data(month=month)
        await state.set_state(EventStates.waiting_day)
        days_kb = []
        for d in range(1, 29, 3):
            row = [KeyboardButton(text=str(d))]
            if d+1 <= 28: row.append(KeyboardButton(text=str(d+1)))
            if d+2 <= 28: row.append(KeyboardButton(text=str(d+2)))
            days_kb.append(row)
        for i in range(29, 32):
            days_kb.append([KeyboardButton(text=str(i))])
        days_kb.append([KeyboardButton(text="❌ Отмена")])
        keyboard = ReplyKeyboardMarkup(keyboard=days_kb, resize_keyboard=True)
        await message.answer("🔢 День месяца:", reply_markup=keyboard)
    except:
        await message.answer("❌ Выберите месяц из списка:")


@dp.message(EventStates.waiting_day)
async def get_event_day(message: Message, state: FSMContext):
    try:
        day = int(message.text)
        if not (1 <= day <= 31):
            raise ValueError
        await state.update_data(day=day)
        await state.set_state(EventStates.waiting_hour_minute)
        await message.answer("⏰ Время (в формате ЧЧ:ММ, например 14:30):")
    except:
        await message.answer("❌ Введите число от 1 до 31:")


@dp.message(EventStates.waiting_hour_minute)
async def get_event_time(message: Message, state: FSMContext):
    try:
        time_str = message.text.strip()
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        data = await state.get_data()
        title = data["title"]
        desc = data.get("description", "")
        year = data["year"]
        month = data["month"]
        day = data["day"]
        tz = get_user_timezone(message.from_user.id)

        local_time_str = f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"

        # Проверка, есть ли группы
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT g.group_name, g.group_id FROM group_members gm
            JOIN groups g ON gm.group_id = g.group_id
            WHERE gm.user_id = ?
        """, (message.from_user.id,))
        groups = cursor.fetchall()
        conn.close()

        scope_kb = [[KeyboardButton(text="👤 Только я")]]
        for name, gid in groups:
            scope_kb.append([KeyboardButton(text=f"👥 {name}")])

        scope_kb.append([KeyboardButton(text="❌ Отмена")])
        keyboard = ReplyKeyboardMarkup(keyboard=scope_kb, resize_keyboard=True)

        await state.update_data(local_time_str=local_time_str, tz=tz)
        await state.set_state(EventStates.waiting_scope)
        await message.answer("📬 Куда отправить событие?", reply_markup=keyboard)
    except:
        await message.answer("❌ Неверный формат времени. Используйте ЧЧ:ММ:")


@dp.message(F.text.startswith("👤") | F.text.startswith("👥"))
async def send_event_to_scope(message: Message, state: FSMContext):
    data = await state.get_data()
    title = data["title"]
    desc = data["description"]
    local_time_str = data["local_time_str"]
    tz = data["tz"]

    if message.text.startswith("👤"):
        chat_type = "private"
        chat_id = message.from_user.id
        target = "личные события"
    else:
        try:
            group_name = message.text.split(" ", 1)[1]
            conn = sqlite3.connect(Config.DATABASE_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT group_id FROM groups WHERE group_name = ?", (group_name,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                await message.answer("❌ Группа не найдена.")
                await state.clear()
                return
            chat_type = "group"
            chat_id = row[0]
            target = f"группу *{group_name}*"
        except Exception as e:
            logger.error(f"Ошибка при выборе группы: {e}")
            await message.answer("❌ Ошибка при выборе группы.")
            await state.clear()
            return

    success, utc_dt = add_event(
        chat_type=chat_type,
        chat_id=chat_id,
        creator_id=message.from_user.id,
        title=title,
        desc=desc,
        local_time_str=local_time_str,
        tz_name=tz
    )

    if success:
        local_tz = ZoneInfo(tz)
        local_time = utc_dt.astimezone(local_tz).strftime("%d.%m.%Y в %H:%M")
        await message.answer(
            f"✅ Событие «{title}» создано на {local_time}\n"
            f"📨 Направлено в: {target}",
            parse_mode="Markdown",
            reply_markup=get_main_menu(message.from_user.id)
        )
    else:
        await message.answer("❌ Ошибка при создании события.")

    await state.clear()


# === Мои события ===
@dp.message(F.text == "📋 Мои события")
async def my_events(message: Message):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT title, event_time FROM events
        WHERE chat_id = ? AND event_time > ?
        ORDER BY event_time
        LIMIT 5
    """, (message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M")))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await message.answer("📭 У вас нет предстоящих событий.")
        return

    text = "📅 *Ваши события:*\n\n"
    for title, utc_time_str in rows:
        try:
            utc_dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
            local_tz = ZoneInfo(get_user_timezone(message.from_user.id))
            local_time = utc_dt.astimezone(local_tz).strftime("%d.%m.%Y %H:%M")
            text += f"• {title} — {local_time}\n"
        except Exception as e:
            logger.error(f"Ошибка форматирования времени: {e}")
            text += f"• {title} — (время недоступно)\n"
    await message.answer(text, parse_mode="Markdown")


# === Запуск бота ===
async def main():
    init_db()
    logger.info("Бот запущен и готов к работе")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
