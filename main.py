# main.py
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# === Импорт конфигурации (уже существует) ===
from config import Config


# === Логирование ===
logging.basicConfig(level=Config.LOG_LEVEL)
logger = logging.getLogger(__name__)


# === Бот ===
bot = Bot(token=Config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# === FSM Состояния ===
class EventStates(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_file = State()
    waiting_recurrence = State()
    waiting_scope = State()
    waiting_year = State()
    waiting_month = State()
    waiting_day = State()
    waiting_hour_minute = State()
    waiting_reminders = State()
    waiting_group_select = State()
    waiting_curated_client = State()  # Для назначения события от куратора


# === База городов для геолокации ===
CITIES_DB = [
    {"name": "Калининград",      "lat": 54.7109, "lon": 20.4510, "tz": "Europe/Kaliningrad"},
    {"name": "Москва",           "lat": 55.7558, "lon": 37.6176, "tz": "Europe/Moscow"},
    {"name": "Санкт-Петербург",  "lat": 59.9343, "lon": 30.3351, "tz": "Europe/Moscow"},
    {"name": "Екатеринбург",     "lat": 56.8389, "lon": 60.6057, "tz": "Asia/Yekaterinburg"},
    {"name": "Новосибирск",      "lat": 55.0084, "lon": 82.9357, "tz": "Asia/Novosibirsk"},
    {"name": "Владивосток",      "lat": 43.1155, "lon": 131.8855, "tz": "Asia/Vladivostok"},
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


# === Часовые пояса для ручного выбора ===
TIMEZONES_LIST = [
    ("Europe/Kaliningrad", "UTC+2 — Калининград"),
    ("Europe/Moscow", "UTC+3 — Москва"),
    ("Europe/Samara", "UTC+4 — Самара"),
    ("Asia/Yekaterinburg", "UTC+5 — Екатеринбург"),
    ("Asia/Omsk", "UTC+6 — Омск"),
    ("Asia/Novosibirsk", "UTC+7 — Новосибирск"),
    ("Asia/Krasnoyarsk", "UTC+8 — Красноярск"),
    ("Asia/Irkutsk", "UTC+9 — Иркутск"),
    ("Asia/Yakutsk", "UTC+10 — Якутск"),
    ("Asia/Vladivostok", "UTC+11 — Владивосток"),
    ("Asia/Magadan", "UTC+12 — Магадан"),
    ("Asia/Kamchatka", "UTC+13 — Петропавловск-Камчатский")
]


# === Инициализация базы данных ===
def init_db():
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'UTC',
            username TEXT,
            first_name TEXT,
            subscription_type TEXT DEFAULT 'free',
            subscription_expire TEXT
        );
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            group_name TEXT,
            owner_id INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (group_id, user_id)
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
            notified_3d INTEGER DEFAULT 1,
            notified_2d INTEGER DEFAULT 1,
            notified_24 INTEGER DEFAULT 1,
            notified_6h INTEGER DEFAULT 1,
            notified_2h INTEGER DEFAULT 1,
            notified_1 INTEGER DEFAULT 1,
            notified_45m INTEGER DEFAULT 1,
            notified_30m INTEGER DEFAULT 1,
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

    columns = [
        ("notified_7d", "INTEGER DEFAULT 1"),
        ("notified_2d", "INTEGER DEFAULT 1"),
        ("notified_2h", "INTEGER DEFAULT 1"),
        ("notified_45m", "INTEGER DEFAULT 1"),
    ]
    for col, spec in columns:
        try: cursor.execute(f"ALTER TABLE events ADD COLUMN {col} {spec}")
        except: pass
    for col in ["file_type", "file_id", "recurrence"]:
        try: cursor.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        except: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN subscription_type TEXT DEFAULT 'free'")
    except: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN subscription_expire TEXT")
    except: pass

    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")


# === Регистрация пользователя ===
def register_user(user: types.User):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, username, first_name, timezone)
        VALUES (?, ?, ?, COALESCE((SELECT timezone FROM users WHERE user_id = ?), ?))
    """, (user.id, user.username, user.first_name, user.id, Config.DEFAULT_TIMEZONE))
    conn.commit()
    conn.close()


# === Проверка подписки ===
def get_subscription_status(user_id: int):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT subscription_type, subscription_expire FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return "free", None
    sub_type, expire_str = row
    if sub_type == "lifetime":
        return "premium", "forever"
    if expire_str:
        try:
            expire = datetime.strptime(expire_str, "%Y-%m-%d %H:%M")
            if expire > datetime.now():
                return "premium", expire_str
        except: pass
    return "free", None


def has_access(user_id: int) -> bool:
    if user_id == Config.OWNER_ID:
        return True
    status, _ = get_subscription_status(user_id)
    return status == "premium"


# === Главное меню (с проверкой кураторства) ===
def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="➕ Создать событие")],
        [KeyboardButton(text="📋 Мои события"), KeyboardButton(text="👥 Группы")],
        [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="⚙️ Профиль")]
    ]

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM curator_client WHERE curator_id = ?", (user_id,))
    is_curator = cursor.fetchone()
    conn.close()

    if is_curator:
        kb.insert(1, [KeyboardButton(text="👨‍🏫 Курируемые")])  # после первой строки

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# === /start ===
@dp.message(Command("start"))
async def start(message: types.Message):
    user = message.from_user
    register_user(user)
    await message.answer(
        f"Привет, {user.first_name}! 🎉\n\n"
        "Я помогу тебе не забыть важное — события, встречи, дедлайны.\n"
        "Выбери действие в меню ниже.",
        reply_markup=get_main_menu(user.id)
    )


# === Кнопки "Назад" и "Отмена" ===
@dp.message(F.text == "🔙 Назад")
async def go_back(message: types.Message):
    await message.answer("Главное меню:", reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))


# === Помощь + Техподдержка ===
@dp.message(F.text == "❓ Помощь")
async def help_command(message: types.Message):
    text = (
        "📘 *Справка*\n\n"
        "Я — бот для управления событиями и напоминаниями.\n\n"
        "🔹 *Бесплатно*:\n"
        "• Создание событий\n"
        "• До 6 напоминаний\n"
        "• Группы\n\n"
        "💎 *Премиум-подписка (100₽/мес)*:\n"
        "• До 26 напоминаний\n"
        "• Авто-перенос событий при смене часового пояса\n"
        "• Хранение файлов до 365 дней\n"
        "• Приоритетная поддержка"
    )
    kb = [
        [KeyboardButton(text="💳 Оформить подписку")],
        [KeyboardButton(text="🛠 Техподдержка")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@dp.message(F.text == "🛠 Техподдержка")
async def support_link(message: types.Message):
    await message.answer(
        "🛠 Связаться с техподдержкой:\n"
        "[Перейти в чат поддержки](https://t.me/helper_tp)",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=get_main_menu(message.from_user.id)
    )


# === Подписка ===
@dp.message(F.text == "💳 Оформить подписку")
async def start_payment(message: types.Message):
    if message.from_user.id == Config.OWNER_ID:
        await message.answer("Вы — владелец бота. У вас уже есть полный доступ.")
        return

    status, _ = get_subscription_status(message.from_user.id)
    if status == "premium":
        await message.answer("У вас уже есть активная подписка!")
        return

    if not Config.PAYMENT_PROVIDER_TOKEN or "YOUR" in Config.PAYMENT_PROVIDER_TOKEN:
        await message.answer(
            "💳 Подписка временно недоступна\n\n"
            "Скоро будет доступна оплата через ЮKassa.\n"
            "Следи за обновлениями!",
            reply_markup=get_main_menu(message.from_user.id)
        )
        return

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="💎 Премиум-подписка",
        description="Доступ ко всем функциям бота на 30 дней",
        payload="subscription_30_days",
        provider_token=Config.PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=[types.LabeledPrice(label="Премиум", amount=10000)],
        start_parameter="subscribe",
        need_email=False,
        is_flexible=False,
        protect_content=True
    )


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.content_type == "successful_payment")
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    expire_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M")

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET subscription_type = 'paid', subscription_expire = ? WHERE user_id = ?",
        (expire_date, user_id)
    )
    conn.commit()
    conn.close()

    await message.answer(f"✅ Оплата прошла! Подписка активна до {expire_date}")


# === Профиль ===
@dp.message(F.text == "⚙️ Профиль")
async def profile(message: types.Message):
    tz = get_user_timezone(message.from_user.id)
    status, expire = get_subscription_status(message.from_user.id)

    if message.from_user.id == Config.OWNER_ID:
        sub_text = "💎 Премиум (автоматически)"
    else:
        sub_text = "💎 Премиум" if status == "premium" else "🆓 Бесплатно"
        if expire and expire != "forever":
            sub_text += f" до {expire}"

    kb = [
        [KeyboardButton(text="🌍 Сменить часовой пояс")],
        [KeyboardButton(text="📍 Определить по геолокации")]
    ]

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT c.curator_id FROM curator_client c WHERE c.client_id = ?", (message.from_user.id,))
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


# === Получение часового пояса ===
def get_user_timezone(user_id: int) -> str:
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else Config.DEFAULT_TIMEZONE


# === Добавление события ===
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
            INSERT INTO events (
                title, description, event_time, created_by, chat_type, chat_id,
                file_type, file_id, recurrence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, desc, utc_time_str, creator_id, chat_type, chat_id, file_type, file_id, recurrence))
        conn.commit()
        conn.close()
        return True, utc_dt
    except Exception as e:
        logger.error(f"Ошибка добавления события: {e}")
        return False, None


# === Пересчёт событий при смене TZ ===
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
        logger.error(f"Ошибка пересчёта событий: {e}")


# === Геолокация с кнопкой "Отмена" ===
@dp.message(F.text == "📍 Определить по геолокации")
async def request_location(message: types.Message):
    kb = [
        [KeyboardButton(text="📍 Отправить мою геопозицию", request_location=True)],
        [KeyboardButton(text="❌ Отмена")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer(
        "Нажми кнопку ниже, чтобы отправить свою геолокацию.\n"
        "Я определю приблизительный часовой пояс.",
        reply_markup=keyboard
    )


@dp.message(F.location)
async def handle_location(message: types.Message):
    lat = message.location.latitude
    lon = message.location.longitude

    try:
        tz, city_name = find_closest_timezone(lat, lon)
        ZoneInfo(tz)

        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (message.from_user.id,))
        row = cursor.fetchone()
        old_tz = row[0] if row else Config.DEFAULT_TIMEZONE

        cursor.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (tz, message.from_user.id))
        conn.commit()
        conn.close()

        reschedule_events_for_user(message.from_user.id, old_tz, tz)

        await message.answer(
            f"✅ Геолокация обработана!\n\n"
            f"📍 Ближайший город: **{city_name}**\n"
            f"🌍 Часовой пояс: `{tz}`",
            parse_mode="Markdown",
            reply_markup=get_main_menu(message.from_user.id)
        )
    except Exception as e:
        logger.error(e)
        await message.answer(
            "❌ Не удалось определить часовой пояс.\n"
            "Попробуй выбрать вручную.",
            reply_markup=get_main_menu(message.from_user.id)
        )


# === Ручная настройка часового пояса ===
@dp.message(F.text == "🌍 Сменить часовой пояс")
async def select_timezone(message: types.Message):
    kb = []
    for _, display_name in TIMEZONES_LIST:
        kb.append([KeyboardButton(text=display_name)])
    kb.append([KeyboardButton(text="❌ Отмена")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("Выбери часовой пояс:", reply_markup=keyboard)


@dp.message(F.text.startswith("UTC+"))
async def handle_timezone_choice(message: types.Message):
    selected_display = message.text.strip()

    new_tz = None
    for tz_code, tz_name in TIMEZONES_LIST:
        if tz_name == selected_display:
            new_tz = tz_code
            break

    if not new_tz:
        await message.answer("❌ Неизвестный часовой пояс.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (message.from_user.id,))
    row = cursor.fetchone()
    old_tz = row[0] if row else Config.DEFAULT_TIMEZONE
    conn.close()

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (new_tz, message.from_user.id))
    conn.commit()
    conn.close()

    reschedule_events_for_user(message.from_user.id, old_tz, new_tz)

    await message.answer(
        f"✅ Часовой пояс изменён:\n\n"
        f"📍 `{new_tz}`",
        parse_mode="Markdown",
        reply_markup=get_main_menu(message.from_user.id)
    )


# === Команда "Добавить куратора" ===
@dp.message(F.text == "➕ Добавить куратора")
async def add_curator_command(message: types.Message):
    cmd = f"/addclient_{message.from_user.id}"
    await message.answer(
        f"Отправь эту команду своему куратору:\n\n"
        f"`{cmd}`",
        parse_mode="Markdown",
        reply_markup=get_main_menu(message.from_user.id)
    )


# === Команда /addclient_<id> — куратор добавляет клиента ===
@dp.message(Command("addclient"))
async def add_client_by_command(message: types.Message):
    try:
        command = message.text.strip()
        client_id = int(command.split("_")[1])

        if client_id == message.from_user.id:
            await message.answer("❌ Нельзя быть куратором самому себе.")
            return

        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM curator_client WHERE curator_id = ? AND client_id = ?", 
                      (message.from_user.id, client_id))
        if cursor.fetchone():
            await message.answer("✅ Этот пользователь уже в списке ваших курируемых.")
            conn.close()
            return

        cursor.execute("""
            INSERT OR IGNORE INTO curator_client (curator_id, client_id, added_at)
            VALUES (?, ?, ?)
        """, (message.from_user.id, client_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        await message.answer(f"✅ Пользователь добавлен в курируемые.")
    except Exception as e:
        logger.error(e)
        await message.answer("❌ Неверная команда.")


# === Кнопка "Курируемые" ===
@dp.message(F.text == "👨‍🏫 Курируемые")
async def list_clients(message: types.Message):
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
        await message.answer("📭 У вас пока нет курируемых.")
        return

    kb = []
    for uid, name in clients:
        kb.append([KeyboardButton(text=f"👤 {name} (ID: {uid})")])
    kb.append([KeyboardButton(text="🔙 Назад")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("Выберите клиента:", reply_markup=keyboard)


# === Профиль клиента для куратора ===
@dp.message(F.text.startswith("👤 "))
async def view_client_profile(message: types.Message):
    try:
        client_id = int(message.text.split("ID: ")[1].strip(")"))
    except:
        await message.answer("❌ Ошибка чтения ID.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM curator_client WHERE curator_id = ? AND client_id = ?",
                   (message.from_user.id, client_id))
    if not cursor.fetchone():
        await message.answer("❌ Вы не куратор этого пользователя.")
        conn.close()
        return

    cursor.execute("SELECT first_name FROM users WHERE user_id = ?", (client_id,))
    row = cursor.fetchone()
    name = row[0] if row else "Клиент"

    cursor.execute("""
        SELECT title, event_time FROM events
        WHERE chat_type = 'private' AND chat_id = ? AND event_time > ?
        ORDER BY event_time LIMIT 1
    """, (client_id, datetime.now().strftime("%Y-%m-%d %H:%M")))
    event_row = cursor.fetchone()
    next_event = event_row[0] if event_row else "Нет предстоящих событий"
    conn.close()

    kb = [
        [KeyboardButton(text="📅 Назначить событие")],
        [KeyboardButton(text="🗑 Удалить клиента")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(
        f"👨‍💼 **{name}**\n\n"
        f"⏰ Ближайшее событие: {next_event}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

    await message.bot.send_message(client_id, f"🔔 Вас добавили как клиента куратором @{message.from_user.username or message.from_user.id}.")

    # Сохраняем ID клиента в контексте
    await message.bot.set_state(message.from_user.id, EventStates.waiting_curated_client)
    await message.bot.update_data(message.from_user.id, curated_client_id=client_id)


# === Удалить клиента ===
@dp.message(F.text == "🗑 Удалить клиента")
async def remove_client(message: types.Message, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("curated_client_id")
    if not client_id:
        await message.answer("❌ Контекст утерян.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM curator_client WHERE curator_id = ? AND client_id = ?", 
                   (message.from_user.id, client_id))
    conn.commit()
    conn.close()

    await message.answer("🗑 Клиент удалён.", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


# === Кнопка "Мои кураторы" у клиента ===
@dp.message(F.text == "👥 Мои кураторы")
async def list_curators(message: types.Message):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.user_id, u.first_name FROM curator_client cc
        JOIN users u ON cc.curator_id = u.user_id
        WHERE cc.client_id = ?
    """, (message.from_user.id,))
    curators = cursor.fetchall()
    conn.close()

    if not curators:
        await message.answer("📭 У вас нет кураторов.")
        return

    kb = []
    for uid, name in curators:
        kb.append([KeyboardButton(text=f"👨‍🏫 {name} (ID: {uid})")])
    kb.append([KeyboardButton(text="🔙 Назад")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("Ваши кураторы:", reply_markup=keyboard)


# === Профиль куратора для клиента ===
@dp.message(F.text.startswith("👨‍🏫 "))
async def view_curator_profile(message: types.Message):
    try:
        curator_id = int(message.text.split("ID: ")[1].strip(")"))
    except:
        await message.answer("❌ Ошибка чтения ID.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM curator_client WHERE curator_id = ? AND client_id = ?",
                   (curator_id, message.from_user.id))
    if not cursor.fetchone():
        await message.answer("❌ Этот пользователь не является вашим куратором.")
        conn.close()
        return

    cursor.execute("SELECT first_name FROM users WHERE user_id = ?", (curator_id,))
    row = cursor.fetchone()
    name = row[0] if row else "Куратор"

    cursor.execute("""
        SELECT title, event_time FROM events
        WHERE chat_type = 'private' AND chat_id = ? AND event_time > ?
        ORDER BY event_time LIMIT 1
    """, (message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M")))
    event_row = cursor.fetchone()
    next_event = event_row[0] if event_row else "Нет предстоящих событий"
    conn.close()

    kb = [
        [KeyboardButton(text="🗑 Удалить куратора")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(
        f"👨‍🏫 **{name}**\n\n"
        f"⏰ Ближайшее событие с вами: {next_event}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

    await message.bot.set_state(message.from_user.id, EventStates.waiting_curated_client)
    await message.bot.update_data(message.from_user.id, curated_client_id=curator_id)


# === Удалить куратора ===
@dp.message(F.text == "🗑 Удалить куратора")
async def remove_curator(message: types.Message, state: FSMContext):
    data = await state.get_data()
    curator_id = data.get("curated_client_id")
    if not curator_id:
        await message.answer("❌ Контекст утерян.")
        return

    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM curator_client WHERE curator_id = ? AND client_id = ?", 
                   (curator_id, message.from_user.id))
    conn.commit()
    conn.close()

    await message.answer("🗑 Куратор удалён.", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


# === Назначить событие (для клиента) ===
@dp.message(F.text == "📅 Назначить событие")
async def start_event_for_client(message: types.Message, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("curated_client_id")
    if not client_id:
        await message.answer("❌ Сначала выберите клиента.")
        return

    await state.set_state(EventStates.waiting_title)
    await state.update_data(curated_client_id=client_id)
    await message.answer(
        "🎯 Введите название события:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Отмена")]], 
            resize_keyboard=True, 
            one_time_keyboard=True
        )
    )


# === Создание события — оптимизировано, быстро, без зависаний ===
@dp.message(EventStates.waiting_title)
async def event_title(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return
    await state.update_data(title=message.text)
    await state.set_state(EventStates.waiting_description)
    await message.answer("📝 Введи описание:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True, one_time_keyboard=True))


@dp.message(EventStates.waiting_description)
async def event_desc(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return
    await state.update_data(description=message.text)
    kb = [[KeyboardButton(text="📎 Пропустить файл")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("📸 Отправь фото, документ или голосовое (или пропусти):", reply_markup=keyboard)
    await state.set_state(EventStates.waiting_file)


@dp.message(EventStates.waiting_file)
async def event_file(message: types.Message, state: FSMContext):
    file_type = file_id = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    elif message.voice:
        file_id = message.voice.file_id
        file_type = "voice"
    elif message.text != "📎 Пропустить файл":
        await message.answer("❌ Неподдерживаемый тип. Отправь фото, файл или голосовое.")
        return

    await state.update_data(file_type=file_type, file_id=file_id)
    kb = [
        [KeyboardButton(text="🚫 Нет")],
        [KeyboardButton(text="🔁 Ежедневно")],
        [KeyboardButton(text="📅 Еженедельно")],
        [KeyboardButton(text="📆 Ежемесячно")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("🔁 Будет ли событие повторяться?", reply_markup=keyboard)
    await state.set_state(EventStates.waiting_recurrence)


@dp.message(EventStates.waiting_recurrence)
async def event_recurrence(message: types.Message, state: FSMContext):
    recurrence_map = {
        "🚫 Нет": None,
        "🔁 Ежедневно": "daily",
        "📅 Еженедельно": "weekly",
        "📆 Ежемесячно": "monthly"
    }
    recurrence = recurrence_map.get(message.text, None)
    await state.update_data(recurrence=recurrence)

    # Автоматически личное событие для клиента
    data = await state.get_data()
    client_id = data["curated_client_id"]

    await state.update_data(chat_type="private", chat_id=client_id)
    current_year = datetime.now().year
    years = [str(current_year), str(current_year + 1)]
    kb = [[KeyboardButton(text=year)] for year in years]
    kb.append([KeyboardButton(text="❌ Отмена")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("📅 Введи год:", reply_markup=keyboard)
    await state.set_state(EventStates.waiting_year)


# ... (остальные шаги: год → месяц → день → время → напоминания) — те же, что у обычного создания события ...

@dp.message(EventStates.waiting_year)
async def event_year(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return

    try:
        year = int(text)
        if not (2020 <= year <= 2030):
            raise ValueError()
        await state.update_data(year=year)
    except:
        await message.answer("❌ Введите корректный год")
        return

    kb = [[KeyboardButton(text=f"{m:02d}") for m in range(1, 13)]]
    kb.append([KeyboardButton(text="❌ Отмена")])
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer("📆 Выбери месяц:", reply_markup=keyboard)
    await state.set_state(EventStates.waiting_month)


@dp.message(EventStates.waiting_month)
async def event_month(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return

    try:
        month = int(text)
        if not (1 <= month <= 12):
            raise ValueError()
        await state.update_data(month=f"{month:02d}")
    except:
        await message.answer("❌ Введите номер месяца от 01 до 12")
        return

    data = await state.get_data()
    year = data["year"]
    month = int(data["month"])

    if month in [1, 3, 5, 7, 8, 10, 12]:
        max_day = 31
    elif month in [4, 6, 9, 11]:
        max_day = 30
    else:
        max_day = 29 if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0) else 28

    day_buttons = []
    row = []
    for day in range(1, max_day + 1):
        row.append(KeyboardButton(text=str(day)))
        if len(row) == 7:
            day_buttons.append(row)
            row = []
    if row:
        day_buttons.append(row)
    day_buttons.append([KeyboardButton(text="❌ Отмена")])

    keyboard = ReplyKeyboardMarkup(keyboard=day_buttons, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("📅 Выбери день:", reply_markup=keyboard)
    await state.set_state(EventStates.waiting_day)


@dp.message(EventStates.waiting_day)
async def event_day(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return

    try:
        day = int(text)
        if not (1 <= day <= 31):
            raise ValueError()
        await state.update_data(day=f"{day:02d}")
    except:
        await message.answer("❌ Введите корректный день")
        return

    kb = [
        [KeyboardButton(text="09:00"), KeyboardButton(text="12:00"), KeyboardButton(text="15:00"), KeyboardButton(text="18:00")],
        [KeyboardButton(text="08:30"), KeyboardButton(text="10:00"), KeyboardButton(text="14:00"), KeyboardButton(text="20:00")],
        [KeyboardButton(text="❌ Отмена")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("⏰ Введи время (ЧЧ:ММ):", reply_markup=keyboard)
    await state.set_state(EventStates.waiting_hour_minute)


@dp.message(EventStates.waiting_hour_minute)
async def event_time_final(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return

    try:
        hour, minute = map(int, text.split(":"))
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError()
        time_str = f"{hour:02d}:{minute:02d}"
    except:
        await message.answer("❌ Введите время в формате ЧЧ:ММ (например, 14:30)")
        return

    await state.update_data(hour_minute=time_str)
    data = await state.get_data()
    full_date_str = f"{data['year']}-{data['month']}-{data['day']} {data['hour_minute']}"
    tz_name = get_user_timezone(data["chat_id"])  # часовой пояс клиента

    success, utc_dt = add_event(
        chat_type=data["chat_type"],
        chat_id=data["chat_id"],
        creator_id=message.from_user.id,
        title=data["title"],
        desc=data["description"],
        local_time_str=full_date_str,
        tz_name=tz_name,
        file_type=data.get("file_type"),
        file_id=data.get("file_id"),
        recurrence=data["recurrence"]
    )

    if success:
        local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
        formatted = local_dt.strftime("%d.%m.%Y в %H:%M")
        await message.answer(
            f"✅ Событие назначено!\n\n"
            f"🎯 **{data['title']}**\n"
            f"⏰ {formatted}\n"
            f"👤 Клиенту",
            parse_mode="Markdown"
        )

        # Показать выбор напоминаний
        kb = [
            [KeyboardButton(text="📅 За 7 дней"), KeyboardButton(text="📆 За 3 дня"), KeyboardButton(text="🗓 За 2 дня")],
            [KeyboardButton(text="📆 За 1 день"), KeyboardButton(text="🕰 За 2 часа"), KeyboardButton(text="⏰ За 6 часов")],
            [KeyboardButton(text="⏱ За 1 час"), KeyboardButton(text="📌 За 45 мин"), KeyboardButton(text="⏳ За 30 мин")],
            [KeyboardButton(text="🔔 За 15 мин")],
            [KeyboardButton(text="✅ Готово")],
            [KeyboardButton(text="❌ Не нужно")]
        ]

        keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
        await message.answer("Выбери напоминания:", reply_markup=keyboard)
        await state.set_state(EventStates.waiting_reminders)
    else:
        await message.answer("❌ Ошибка при создании события.")
        await state.clear()


@dp.message(EventStates.waiting_reminders)
async def select_reminders(message: types.Message, state: FSMContext):
    text = message.text.strip()

    reminders = {
        "📅 За 7 дней": "notified_7d",
        "📆 За 3 дня": "notified_3d",
        "🗓 За 2 дня": "notified_2d",
        "📆 За 1 день": "notified_24",
        "🕰 За 2 часа": "notified_2h",
        "⏰ За 6 часов": "notified_6h",
        "⏱ За 1 час": "notified_1",
        "📌 За 45 мин": "notified_45m",
        "⏳ За 30 мин": "notified_30m",
        "🔔 За 15 мин": "notified_15m"
    }

    if text == "❌ Не нужно":
        await message.answer("🔔 Напоминания отключены.", reply_markup=get_main_menu(message.from_user.id))
        await state.clear()
        return

    if text == "✅ Готово":
        await finish_reminders(message, state)
        return

    field = reminders.get(text)
    if not field:
        await message.answer("❌ Выбери из списка.")
        return

    data = await state.get_data()
    selected_fields = data.get("reminder_fields", [])

    max_count = 26 if has_access(message.from_user.id) else 6
    if len(selected_fields) >= max_count:
        await message.answer(f"❌ Лимит напоминаний: {max_count}. Нажми 'Готово'.")
        return

    if field not in selected_fields:
        selected_fields.append(field)
        await state.update_data(reminder_fields=selected_fields)

    await message.answer(
        f"✅ Добавлено: {text}\n"
        f"🔔 Выбрано: {len(selected_fields)} из {max_count}",
        reply_markup=message.reply_markup
    )


async def finish_reminders(message: types.Message, state: FSMContext):
    data = await state.get_data()
    fields = data.get("reminder_fields", [])

    if not fields:
        await message.answer("Напоминания не установлены.", reply_markup=get_main_menu(message.from_user.id))
    else:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        event_id = cursor.execute(
            "SELECT MAX(id) FROM events WHERE created_by = ?", (message.from_user.id,)
        ).fetchone()[0]

        if event_id:
            for col in [
                "notified_7d", "notified_3d", "notified_2d", "notified_24",
                "notified_2h", "notified_6h", "notified_1", "notified_45m",
                "notified_30m", "notified_15m"
            ]:
                value = 0 if col in fields else 1
                cursor.execute(f"UPDATE events SET {col} = ? WHERE id = ?", (value, event_id))
            conn.commit()
        conn.close()
        await message.answer(f"✅ Установлено {len(fields)} напоминаний!", reply_markup=get_main_menu(message.from_user.id))

    await state.clear()


# === Запуск бота ===
async def main():
    init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
