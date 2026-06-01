import asyncio
import csv
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)


def load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = Path(os.getenv("DB_PATH", "expenses.sqlite3"))
PRO_PRICE_STARS = 1
PRO_PAYLOAD_PREFIX = "pro_forever"
BUY_PRO_CALLBACK = "buy_pro"
MENU_CALLBACK_PREFIX = "menu:"
PRO_CALLBACK_PREFIX = "pro:"
LIMIT_CALLBACK_PREFIX = "limit:"
RECURRING_CALLBACK_PREFIX = "recur:"
AUTOCAT_CALLBACK_PREFIX = "autocat:"

EXPENSE_CATEGORIES = [
    ("food", "🍓 Еда"),
    ("coffee", "☕️ Кофе"),
    ("taxi", "🚕 Такси"),
    ("transport", "🚌 Транспорт"),
    ("beauty", "💅 Красота"),
    ("clothes", "👗 Одежда"),
    ("health", "🩷 Здоровье"),
    ("home", "🏠 Дом"),
    ("fun", "🎀 Развлечения"),
    ("other", "✨ Другое"),
]

INCOME_CATEGORIES = [
    ("salary", "💼 Зарплата"),
    ("gift", "🎁 Подарок"),
    ("cashback", "💳 Кэшбэк"),
    ("other_income", "✨ Другое"),
]

MENU_BUTTONS = {
    "➖ Расход",
    "➕ Доход",
    "💰 Баланс",
    "📊 Статистика",
    "🗂 Категории",
    "🧾 История",
    "💎 Купить Pro",
    "↩️ Удалить последнюю",
}

pending_transactions: Dict[int, Dict[str, Union[int, str]]] = {}
input_modes: Dict[int, str] = {}

AUTO_CATEGORY_KEYWORDS = {
    "coffee": ["кофе", "латте", "капуч", "раф", "эспресс", "матча"],
    "taxi": ["такси", "яндекс go", "uber", "убер", "bolt"],
    "food": ["еда", "обед", "ужин", "завтрак", "кафе", "ресторан", "продукт", "пятерочка", "перекресток", "вкусвилл", "самокат", "лавка"],
    "transport": ["метро", "автобус", "транспорт", "карта тройка", "тройка"],
    "beauty": ["маникюр", "салон", "брови", "ресницы", "космет", "укладка", "стрижка"],
    "clothes": ["одежда", "платье", "юбка", "джинсы", "обувь", "кроссов", "zara", "hm"],
    "health": ["аптека", "врач", "анализ", "лекар", "стомат", "клиника"],
    "home": ["аренда", "квартира", "дом", "интернет", "телефон", "коммун", "жкх"],
    "fun": ["кино", "театр", "бар", "концерт", "подписка", "netflix", "spotify", "яндекс плюс", "спортзал", "фитнес"],
}
RECURRING_KEYWORDS = [
    "аренда",
    "интернет",
    "телефон",
    "связь",
    "спортзал",
    "фитнес",
    "подписка",
    "яндекс плюс",
    "netflix",
    "spotify",
    "icloud",
]
pending_recurring_suggestions: Dict[int, Dict[str, Union[int, str]]] = {}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_pro INTEGER NOT NULL DEFAULT 0,
                pro_purchased_at TEXT,
                telegram_payment_charge_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'expense',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS category_limits (
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, category)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                period TEXT NOT NULL DEFAULT 'monthly',
                day_of_month INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = conn.execute("PRAGMA table_info(expenses)").fetchall()
        column_names = {column["name"] for column in columns}
        if "kind" not in column_names:
            conn.execute("ALTER TABLE expenses ADD COLUMN kind TEXT NOT NULL DEFAULT 'expense'")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_expenses_user_created
            ON expenses (user_id, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recurring_user
            ON recurring_payments (user_id)
            """
        )
        conn.commit()


def ensure_user(user_id: int) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )
        conn.commit()


def user_is_pro(user_id: int) -> bool:
    ensure_user(user_id)
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT is_pro FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return bool(row["is_pro"]) if row else False


def set_user_pro(user_id: int, telegram_payment_charge_id: str) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO users (
                user_id,
                is_pro,
                pro_purchased_at,
                telegram_payment_charge_id
            )
            VALUES (?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                is_pro = 1,
                pro_purchased_at = excluded.pro_purchased_at,
                telegram_payment_charge_id = excluded.telegram_payment_charge_id
            """,
            (
                user_id,
                datetime.now().isoformat(timespec="seconds"),
                telegram_payment_charge_id,
            ),
        )
        conn.commit()


def parse_transaction(text: str) -> Optional[Tuple[str, int]]:
    match = re.fullmatch(r"\s*(.+?)\s+((?:\d[\d ]*)(?:[,.]\d{1,2})?)\s*", text)
    if not match:
        return None

    title = match.group(1).strip()
    amount_text = match.group(2).replace(" ", "").replace(",", ".")
    amount = round(float(amount_text))
    if not title or amount <= 0:
        return None

    return title, amount


def strip_prefix(text: str, prefix: str) -> str:
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def parse_amount(text: str) -> Optional[int]:
    amount_text = text.strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", amount_text):
        return None

    amount = round(float(amount_text))
    return amount if amount > 0 else None


def month_prefix(offset: int = 0) -> str:
    today = datetime.now()
    year = today.year
    month = today.month + offset
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return f"{year:04d}-{month:02d}"


def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="➖ Расход", callback_data=f"{MENU_CALLBACK_PREFIX}expense"),
            InlineKeyboardButton(text="➕ Доход", callback_data=f"{MENU_CALLBACK_PREFIX}income"),
        ],
        [
            InlineKeyboardButton(text="💰 Баланс", callback_data=f"{MENU_CALLBACK_PREFIX}balance"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"{MENU_CALLBACK_PREFIX}stats"),
        ],
        [
            InlineKeyboardButton(text="🗂 Категории", callback_data=f"{MENU_CALLBACK_PREFIX}categories"),
            InlineKeyboardButton(text="🧾 История", callback_data=f"{MENU_CALLBACK_PREFIX}history"),
        ],
    ]
    last_row = [
        InlineKeyboardButton(
            text="↩️ Удалить последнюю",
            callback_data=f"{MENU_CALLBACK_PREFIX}delete_last",
        )
    ]
    if user_id is not None and user_is_pro(user_id):
        last_row.insert(
            0,
            InlineKeyboardButton(text="💎 Pro", callback_data=f"{MENU_CALLBACK_PREFIX}pro"),
        )
    elif user_id is not None:
        last_row.insert(
            0,
            InlineKeyboardButton(
                text=f"💎 Купить Pro за {PRO_PRICE_STARS} ⭐",
                callback_data=BUY_PRO_CALLBACK,
            ),
        )
    rows.append(last_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def pro_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Экспорт CSV", callback_data=f"{PRO_CALLBACK_PREFIX}export_csv"),
                InlineKeyboardButton(text="📅 Отчет", callback_data=f"{PRO_CALLBACK_PREFIX}report"),
            ],
            [
                InlineKeyboardButton(text="🎯 Лимиты", callback_data=f"{PRO_CALLBACK_PREFIX}limits"),
                InlineKeyboardButton(text="🔁 Регулярные", callback_data=f"{PRO_CALLBACK_PREFIX}recurring"),
            ],
            [
                InlineKeyboardButton(text="✨ Авто-категории", callback_data=f"{PRO_CALLBACK_PREFIX}autocat"),
                InlineKeyboardButton(text="🔮 Инсайт", callback_data=f"{PRO_CALLBACK_PREFIX}insight"),
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data=f"{PRO_CALLBACK_PREFIX}main")],
        ]
    )


def category_keyboard(kind: str) -> InlineKeyboardMarkup:
    categories = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    rows = []
    for category_id, title in categories:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"cat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def auto_category_keyboard(category_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Да, {category_title(category_id)}",
                    callback_data=f"{AUTOCAT_CALLBACK_PREFIX}save:{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Выбрать другую",
                    callback_data=f"{AUTOCAT_CALLBACK_PREFIX}choose",
                )
            ],
        ]
    )


def limit_categories_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for category_id, title in EXPENSE_CATEGORIES:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"{LIMIT_CALLBACK_PREFIX}set:{category_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Pro", callback_data=f"{PRO_CALLBACK_PREFIX}menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recurring_category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for category_id, title in EXPENSE_CATEGORIES:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"{RECURRING_CALLBACK_PREFIX}cat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recurring_suggestion_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, добавить в регулярные",
                    callback_data=f"{RECURRING_CALLBACK_PREFIX}quick_yes",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нет",
                    callback_data=f"{RECURRING_CALLBACK_PREFIX}quick_no",
                )
            ],
        ]
    )


def category_title(category_id: str) -> str:
    for current_id, title in EXPENSE_CATEGORIES + INCOME_CATEGORIES:
        if current_id == category_id:
            return title
    return "✨ Другое"


def save_transaction(user_id: int, title: str, amount: int, category: str, kind: str) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO expenses (user_id, title, amount, category, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                amount,
                category,
                kind,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


def save_category_limit(user_id: int, category: str, amount: int) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO category_limits (user_id, category, amount, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET
                amount = excluded.amount,
                created_at = excluded.created_at
            """,
            (user_id, category, amount, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def delete_category_limit(user_id: int, category: str) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            "DELETE FROM category_limits WHERE user_id = ? AND category = ?",
            (user_id, category),
        )
        conn.commit()


def category_spent_this_month(user_id: int, category: str) -> int:
    with closing(db_connect()) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND category = ? AND created_at LIKE ?
            """,
            (user_id, category, f"{month_prefix()}%"),
        ).fetchone()
    return int(row["total"])


def get_category_limit(user_id: int, category: str) -> Optional[int]:
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT amount FROM category_limits WHERE user_id = ? AND category = ?",
            (user_id, category),
        ).fetchone()
    return int(row["amount"]) if row else None


def limit_warning_text(user_id: int, category: str) -> Optional[str]:
    limit = get_category_limit(user_id, category)
    if not limit:
        return None

    spent = category_spent_this_month(user_id, category)
    percent = spent / limit
    if percent >= 1:
        return (
            f"🎯 Лимит по {category_title(category)} превышен.\n"
            f"Потрачено {money(spent)} из {money(limit)}."
        )
    if percent >= 0.8:
        return (
            f"🎯 Осторожно: по {category_title(category)} уже {round(percent * 100)}% лимита.\n"
            f"Потрачено {money(spent)} из {money(limit)}."
        )
    return None


def limits_text(user_id: int) -> str:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT category, amount
            FROM category_limits
            WHERE user_id = ?
            ORDER BY category
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        return (
            "🎯 Лимиты\n\n"
            "Пока лимитов нет. Выбери категорию, и я попрошу сумму на месяц.\n"
            "Если потом введешь `0`, лимит удалится."
        )

    lines = ["🎯 Лимиты на месяц", ""]
    for row in rows:
        spent = category_spent_this_month(user_id, row["category"])
        limit = int(row["amount"])
        percent = min(round(spent / limit * 100), 999) if limit else 0
        lines.append(f"{category_title(row['category'])}: {money(spent)} / {money(limit)} ({percent}%)")
    lines.append("")
    lines.append("Выбери категорию, чтобы изменить лимит.")
    return "\n".join(lines)


def add_recurring_payment(user_id: int, title: str, amount: int, category: str) -> None:
    today = datetime.now()
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO recurring_payments (
                user_id,
                title,
                amount,
                category,
                period,
                day_of_month,
                created_at
            )
            VALUES (?, ?, ?, ?, 'monthly', ?, ?)
            """,
            (
                user_id,
                title,
                amount,
                category,
                today.day,
                today.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


def recurring_text(user_id: int) -> str:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT title, amount, category, day_of_month
            FROM recurring_payments
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        return (
            "🔁 Регулярные платежи\n\n"
            "Пока пусто. Можно добавить аренду, интернет, телефон, спортзал или подписку."
        )

    lines = ["🔁 Регулярные платежи", ""]
    for row in rows:
        lines.append(
            f"{category_title(row['category'])} · {row['title']} · "
            f"{money(row['amount'])} · каждый месяц {row['day_of_month']} числа"
        )
    return "\n".join(lines)


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def balance_text(user_id: int) -> str:
    with closing(db_connect()) as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM expenses
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    income = row["income"]
    expense = row["expense"]
    balance = income - expense
    return (
        "💰 Твой кошелек\n\n"
        f"➕ Доходы: {money(income)}\n"
        f"➖ Расходы: {money(expense)}\n"
        f"🩷 Баланс: {money(balance)}"
    )


def stats_text(user_id: int) -> str:
    month_prefix = datetime.now().strftime("%Y-%m")
    with closing(db_connect()) as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM expenses
            WHERE user_id = ? AND created_at LIKE ?
            """,
            (user_id, f"{month_prefix}%"),
        ).fetchone()

        rows = conn.execute(
            """
            SELECT category, SUM(amount) AS amount
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            GROUP BY category
            ORDER BY amount DESC
            """,
            (user_id, f"{month_prefix}%"),
        ).fetchall()

    income = row["income"]
    expense = row["expense"]
    if income == 0 and expense == 0:
        return "За этот месяц пока пусто. Добавь расход: `кофе 300`"

    lines = [
        "📊 Статистика за месяц",
        "",
        f"➕ Доходы: {money(income)}",
        f"➖ Расходы: {money(expense)}",
        f"🩷 Итог: {money(income - expense)}",
        "",
        "Расходы по категориям:",
    ]
    for row in rows:
        lines.append(f"{category_title(row['category'])}: {money(row['amount'])}")
    return "\n".join(lines)


def pro_monthly_report_text(user_id: int) -> str:
    current_prefix = month_prefix()
    previous_prefix = month_prefix(-1)
    with closing(db_connect()) as conn:
        current = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM expenses
            WHERE user_id = ? AND created_at LIKE ?
            """,
            (user_id, f"{current_prefix}%"),
        ).fetchone()
        previous = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS expense
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            """,
            (user_id, f"{previous_prefix}%"),
        ).fetchone()
        top_categories = conn.execute(
            """
            SELECT category, SUM(amount) AS amount
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            GROUP BY category
            ORDER BY amount DESC
            LIMIT 3
            """,
            (user_id, f"{current_prefix}%"),
        ).fetchall()
        biggest = conn.execute(
            """
            SELECT title, amount, category
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            ORDER BY amount DESC
            LIMIT 1
            """,
            (user_id, f"{current_prefix}%"),
        ).fetchone()

    income = int(current["income"])
    expense = int(current["expense"])
    if income == 0 and expense == 0:
        return "📅 Pro-отчет\n\nВ этом месяце пока нет операций."

    previous_expense = int(previous["expense"])
    if previous_expense:
        diff = round((expense - previous_expense) / previous_expense * 100)
        compare = f"{diff:+d}% к прошлому месяцу"
    else:
        compare = "прошлый месяц пустой"

    avg_day = round(expense / max(datetime.now().day, 1))
    lines = [
        "📅 Pro-отчет за месяц",
        "",
        f"➕ Доходы: {money(income)}",
        f"➖ Расходы: {money(expense)}",
        f"🩷 Итог: {money(income - expense)}",
        f"📈 Динамика расходов: {compare}",
        f"🗓 Средний расход в день: {money(avg_day)}",
    ]
    if biggest:
        lines.append(
            f"💥 Самая большая трата: {biggest['title']} · "
            f"{category_title(biggest['category'])} · {money(biggest['amount'])}"
        )
    if top_categories:
        lines.append("")
        lines.append("Топ категорий:")
        for row in top_categories:
            lines.append(f"{category_title(row['category'])}: {money(row['amount'])}")
    return "\n".join(lines)


def insight_text(user_id: int) -> str:
    current_prefix = month_prefix()
    previous_prefix = month_prefix(-1)
    with closing(db_connect()) as conn:
        top = conn.execute(
            """
            SELECT category, SUM(amount) AS amount, COUNT(*) AS count
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            GROUP BY category
            ORDER BY amount DESC
            LIMIT 1
            """,
            (user_id, f"{current_prefix}%"),
        ).fetchone()
        frequent = conn.execute(
            """
            SELECT category, COUNT(*) AS count, SUM(amount) AS amount
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            GROUP BY category
            ORDER BY count DESC, amount DESC
            LIMIT 1
            """,
            (user_id, f"{current_prefix}%"),
        ).fetchone()
        current_total = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            """,
            (user_id, f"{current_prefix}%"),
        ).fetchone()["total"]
        previous_total = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND created_at LIKE ?
            """,
            (user_id, f"{previous_prefix}%"),
        ).fetchone()["total"]

    if not current_total:
        return "🔮 Инсайт\n\nПока мало данных. Добавь несколько расходов, и я найду закономерности."

    if previous_total:
        diff = round((current_total - previous_total) / previous_total * 100)
        if abs(diff) >= 10:
            direction = "больше" if diff > 0 else "меньше"
            return (
                "🔮 Инсайт\n\n"
                f"В этом месяце расходы на {abs(diff)}% {direction}, чем в прошлом. "
                f"Сейчас: {money(current_total)}, прошлый месяц: {money(previous_total)}."
            )

    if top:
        return (
            "🔮 Инсайт\n\n"
            f"Главная категория месяца — {category_title(top['category'])}: {money(top['amount'])}. "
            "Если хочешь больше контроля, поставь на нее лимит в Pro-разделе."
        )

    if frequent:
        return (
            "🔮 Инсайт\n\n"
            f"Чаще всего повторяется {category_title(frequent['category'])}: "
            f"{frequent['count']} операций на {money(frequent['amount'])}."
        )
    return "🔮 Инсайт\n\nПока все выглядит спокойно."


def suggest_expense_category(title: str) -> Optional[str]:
    normalized = title.lower()
    for category_id, keywords in AUTO_CATEGORY_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return category_id
    return None


def looks_recurring(title: str) -> bool:
    normalized = title.lower()
    return any(keyword in normalized for keyword in RECURRING_KEYWORDS)


def categories_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for category_id, title in EXPENSE_CATEGORIES:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"showcat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_details_text(user_id: int, category_id: str) -> str:
    month_prefix = datetime.now().strftime("%Y-%m")
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT title, amount, created_at
            FROM expenses
            WHERE user_id = ? AND kind = 'expense' AND category = ? AND created_at LIKE ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (user_id, category_id, f"{month_prefix}%"),
        ).fetchall()

    if not rows:
        return f"{category_title(category_id)}\n\nВ этом месяце тут пока пусто."

    total = sum(row["amount"] for row in rows)
    lines = [f"{category_title(category_id)} за месяц: {money(total)}", ""]
    for row in rows:
        day = datetime.fromisoformat(row["created_at"]).strftime("%d.%m")
        lines.append(f"{day} · {row['title']} · {money(row['amount'])}")
    return "\n".join(lines)


def history_text(user_id: int) -> str:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT title, amount, category, kind, created_at
            FROM expenses
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 15
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        return "История пока пустая. Начни с `кофе 300`"

    lines = ["🧾 Последние операции", ""]
    for row in rows:
        sign = "➕" if row["kind"] == "income" else "➖"
        day = datetime.fromisoformat(row["created_at"]).strftime("%d.%m %H:%M")
        lines.append(
            f"{sign} {day} · {category_title(row['category'])} · "
            f"{row['title']} · {money(row['amount'])}"
        )
    return "\n".join(lines)


def delete_last_transaction(user_id: int) -> Optional[str]:
    with closing(db_connect()) as conn:
        row = conn.execute(
            """
            SELECT id, title, amount, category, kind
            FROM expenses
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return None

        conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
        conn.commit()

    sign = "доход" if row["kind"] == "income" else "расход"
    return f"Удалила последний {sign}: {row['title']} · {money(row['amount'])}"


async def start_handler(message: Message) -> None:
    ensure_user(message.from_user.id)
    await message.answer(
        "Привет, я твой розовый кошелек 🩷\n\n"
        "Быстро добавить расход: `кофе 300`, `такси 1000`.\n"
        "Доход можно так: `+ зарплата 50000`.\n\n"
        "А еще можно пользоваться кнопками под этим сообщением.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


async def stats_handler(message: Message) -> None:
    await message.answer(
        stats_text(message.from_user.id),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


async def balance_handler(message: Message) -> None:
    await message.answer(
        balance_text(message.from_user.id),
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


async def history_handler(message: Message) -> None:
    await message.answer(
        history_text(message.from_user.id),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


async def categories_handler(message: Message) -> None:
    await message.answer(
        "🗂 Выбери категорию расходов, покажу траты за месяц:",
        reply_markup=categories_keyboard(),
    )


async def delete_last_handler(message: Message) -> None:
    result = delete_last_transaction(message.from_user.id)
    await message.answer(
        result or "Удалять пока нечего.",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


async def add_expense_button_handler(message: Message) -> None:
    input_modes[message.from_user.id] = "expense"
    await message.answer(
        "➖ Напиши расход в формате `название сумма`.\nНапример: `кофе 300`",
        parse_mode="Markdown",
    )


async def buy_pro_handler(message: Message) -> None:
    user_id = message.from_user.id
    if user_is_pro(user_id):
        await message.answer(
            "💎 Pro уже включен навсегда.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    await message.answer_invoice(
        **pro_invoice_kwargs(user_id),
    )


def pro_invoice_kwargs(user_id: int) -> Dict[str, object]:
    return {
        "title": "💎 Pro навсегда",
        "description": "Доступ Pro для этого бота навсегда. Пока без новых функций, они появятся позже.",
        "payload": f"{PRO_PAYLOAD_PREFIX}:{user_id}",
        "currency": "XTR",
        "prices": [LabeledPrice(label="Pro навсегда", amount=PRO_PRICE_STARS)],
    }


def export_csv_file(user_id: int) -> str:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT created_at, kind, category, title, amount
            FROM expenses
            WHERE user_id = ?
            ORDER BY created_at ASC
            """,
            (user_id,),
        ).fetchall()

    export_path = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        newline="",
        suffix=".csv",
        delete=False,
    ).name
    with open(export_path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["created_at", "type", "category", "category_title", "title", "amount"])
        for row in rows:
            writer.writerow(
                [
                    row["created_at"],
                    row["kind"],
                    row["category"],
                    category_title(row["category"]),
                    row["title"],
                    row["amount"],
                ]
            )
    return export_path


def pro_menu_text() -> str:
    return (
        "💎 Pro-зона\n\n"
        "У тебя включен Pro навсегда.\n\n"
        "Доступно сейчас:\n"
        "📤 Экспорт расходов в CSV и другие форматы\n"
        "📅 Умные месячные отчеты\n"
        "🎯 Лимиты и предупреждения по категориям\n"
        "✨ Авто-категоризацию расходов\n"
        "🔁 Регулярные платежи\n"
        "🔮 Финансовые инсайты"
    )


async def buy_pro_callback_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_is_pro(user_id):
        await callback.answer("Pro уже включен навсегда.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer_invoice(
        **pro_invoice_kwargs(user_id),
    )


async def require_pro_or_invoice(callback: CallbackQuery) -> bool:
    if user_is_pro(callback.from_user.id):
        return True

    await callback.answer("Эта функция в Pro.", show_alert=True)
    await callback.message.answer_invoice(
        **pro_invoice_kwargs(callback.from_user.id),
    )
    return False


async def pro_callback_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    action = strip_prefix(callback.data, PRO_CALLBACK_PREFIX)

    if action == "main":
        await callback.answer()
        await callback.message.answer(
            "Главное меню 🩷",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if not await require_pro_or_invoice(callback):
        return

    await callback.answer()
    if action == "menu":
        await callback.message.answer(pro_menu_text(), reply_markup=pro_menu_keyboard())
        return

    if action == "export_csv":
        export_path = export_csv_file(user_id)
        try:
            await callback.message.answer_document(
                FSInputFile(export_path, filename="expenses.csv"),
                caption="📤 Экспорт расходов и доходов в CSV",
                reply_markup=pro_menu_keyboard(),
            )
        finally:
            try:
                os.unlink(export_path)
            except OSError:
                pass
        return

    if action == "report":
        await callback.message.answer(
            pro_monthly_report_text(user_id),
            reply_markup=pro_menu_keyboard(),
        )
        return

    if action == "limits":
        await callback.message.answer(
            limits_text(user_id),
            reply_markup=limit_categories_keyboard(),
        )
        return

    if action == "recurring":
        await callback.message.answer(
            recurring_text(user_id),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить регулярный", callback_data=f"{RECURRING_CALLBACK_PREFIX}add")],
                    [InlineKeyboardButton(text="⬅️ Pro", callback_data=f"{PRO_CALLBACK_PREFIX}menu")],
                ]
            ),
        )
        return

    if action == "autocat":
        await callback.message.answer(
            "✨ Авто-категории включены.\n\n"
            "Пиши расходы как обычно: `латте 350`, `такси 1000`, `маникюр 3000`.\n"
            "Если я узнаю категорию, предложу сохранить в один тап.",
            parse_mode="Markdown",
            reply_markup=pro_menu_keyboard(),
        )
        return

    if action == "insight":
        await callback.message.answer(
            insight_text(user_id),
            reply_markup=pro_menu_keyboard(),
        )


async def limit_callback_handler(callback: CallbackQuery) -> None:
    if not await require_pro_or_invoice(callback):
        return

    action_value = strip_prefix(callback.data, LIMIT_CALLBACK_PREFIX)
    action, category_id = action_value.split(":", 1)
    if action != "set":
        await callback.answer()
        return

    input_modes[callback.from_user.id] = f"limit:{category_id}"
    await callback.answer()
    await callback.message.answer(
        f"🎯 Напиши месячный лимит для {category_title(category_id)}.\n"
        "Например: `15000`.\n\n"
        "Чтобы удалить лимит, напиши `0`.",
        parse_mode="Markdown",
    )


async def recurring_callback_handler(callback: CallbackQuery) -> None:
    if not await require_pro_or_invoice(callback):
        return

    user_id = callback.from_user.id
    action_value = strip_prefix(callback.data, RECURRING_CALLBACK_PREFIX)
    await callback.answer()

    if action_value == "add":
        input_modes[user_id] = "recurring_add"
        await callback.message.answer(
            "🔁 Напиши регулярный платеж в формате `название сумма`.\n"
            "Например: `интернет 900` или `спортзал 5000`.",
            parse_mode="Markdown",
        )
        return

    if action_value == "quick_no":
        pending_recurring_suggestions.pop(user_id, None)
        await callback.message.edit_text("Окей, не добавляю в регулярные.")
        return

    if action_value == "quick_yes":
        suggestion = pending_recurring_suggestions.pop(user_id, None)
        if not suggestion:
            await callback.message.answer("Это предложение уже неактуально.")
            return

        add_recurring_payment(
            user_id,
            str(suggestion["title"]),
            int(suggestion["amount"]),
            str(suggestion["category"]),
        )
        await callback.message.edit_text(
            "🔁 Добавила в регулярные платежи.\n\n"
            f"{category_title(str(suggestion['category']))}\n"
            f"{suggestion['title']} — {money(int(suggestion['amount']))} каждый месяц"
        )
        return

    if action_value.startswith("cat:"):
        category_id = strip_prefix(action_value, "cat:")
        recurring = pending_transactions.pop(user_id, None)
        if not recurring or recurring.get("kind") != "recurring":
            await callback.message.answer("Этот регулярный платеж уже не ожидает категорию.")
            return

        add_recurring_payment(user_id, str(recurring["title"]), int(recurring["amount"]), category_id)
        await callback.message.answer(
            "🔁 Готово, добавила регулярный платеж.\n\n"
            f"{category_title(category_id)}\n"
            f"{recurring['title']} — {money(int(recurring['amount']))} каждый месяц",
            reply_markup=pro_menu_keyboard(),
        )


async def auto_category_callback_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    action_value = strip_prefix(callback.data, AUTOCAT_CALLBACK_PREFIX)
    transaction = pending_transactions.get(user_id)
    if not transaction:
        await callback.answer("Эта операция уже не ожидает категорию.", show_alert=True)
        return

    await callback.answer()
    if action_value == "choose":
        await callback.message.answer(
            "Окей, выбери категорию:",
            reply_markup=category_keyboard("expense"),
        )
        return

    if action_value.startswith("save:"):
        category_id = strip_prefix(action_value, "save:")
        pending_transactions.pop(user_id, None)
        save_transaction(
            user_id,
            str(transaction["title"]),
            int(transaction["amount"]),
            category_id,
            str(transaction["kind"]),
        )
    await callback.message.edit_text(
        "Готово, сохранила 💖\n\n"
        f"➖ {category_title(category_id)}\n"
        f"{transaction['title']} — {money(int(transaction['amount']))}"
    )
    warning = limit_warning_text(user_id, category_id)
    if warning:
        await callback.message.answer(warning, reply_markup=main_menu_keyboard(user_id))
    if looks_recurring(str(transaction["title"])) and user_is_pro(user_id):
        pending_recurring_suggestions[user_id] = {
            "title": str(transaction["title"]),
            "amount": int(transaction["amount"]),
            "category": category_id,
        }
        await callback.message.answer(
            "🔁 Похоже, это регулярный платеж. Добавить в регулярные?",
            reply_markup=recurring_suggestion_keyboard(),
        )


async def pre_checkout_handler(query: PreCheckoutQuery) -> None:
    expected_payload = f"{PRO_PAYLOAD_PREFIX}:{query.from_user.id}"
    if query.invoice_payload != expected_payload:
        await query.answer(ok=False, error_message="Этот инвойс не подходит для твоего аккаунта.")
        return

    await query.answer(ok=True)


async def successful_payment_handler(message: Message) -> None:
    payment = message.successful_payment
    if payment.currency != "XTR" or not payment.invoice_payload.startswith(PRO_PAYLOAD_PREFIX):
        return

    set_user_pro(message.from_user.id, payment.telegram_payment_charge_id)
    await message.answer(
        "💎 Готово, Pro включен навсегда.\n\n"
        "Пока новых функций нет, но статус уже сохранен в базе.",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


async def add_income_button_handler(message: Message) -> None:
    input_modes[message.from_user.id] = "income"
    await message.answer(
        "➕ Напиши доход в формате `название сумма`.\nНапример: `зарплата 50000`",
        parse_mode="Markdown",
    )


async def expense_handler(message: Message) -> None:
    text = (message.text or "").strip()
    user_id = message.from_user.id

    if text in MENU_BUTTONS:
        return

    mode = input_modes.get(user_id, "expense")
    if mode.startswith("limit:"):
        category_id = strip_prefix(mode, "limit:")
        if text in {"0", "удалить", "сбросить"}:
            delete_category_limit(user_id, category_id)
            input_modes.pop(user_id, None)
            await message.answer(
                f"🎯 Лимит по {category_title(category_id)} удален.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        amount = parse_amount(text)
        if amount is None:
            await message.answer("Напиши сумму лимита числом. Например: `15000`", parse_mode="Markdown")
            return

        save_category_limit(user_id, category_id, amount)
        input_modes.pop(user_id, None)
        await message.answer(
            f"🎯 Готово. Лимит по {category_title(category_id)}: {money(amount)} в месяц.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if mode == "recurring_add":
        parsed_recurring = parse_transaction(text)
        if not parsed_recurring:
            await message.answer(
                "Напиши регулярный платеж в формате `название сумма`.\nНапример: `интернет 900`",
                parse_mode="Markdown",
            )
            return

        title, amount = parsed_recurring
        pending_transactions[user_id] = {"title": title, "amount": amount, "kind": "recurring"}
        input_modes.pop(user_id, None)
        await message.answer(
            f"🔁 Регулярный платеж: {title} — {money(amount)}\nВыбери категорию:",
            reply_markup=recurring_category_keyboard(),
        )
        return

    kind = mode
    if text.startswith("+"):
        kind = "income"
        text = text[1:].strip()
    elif text.lower().startswith("доход "):
        kind = "income"
        text = text[6:].strip()

    parsed = parse_transaction(text)
    if not parsed:
        await message.answer(
            "Я понимаю формат: название и сумма в конце 💗\n"
            "Расход: `кофе 300`\n"
            "Доход: `+ зарплата 50000`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    input_modes.pop(user_id, None)
    title, amount = parsed
    pending_transactions[user_id] = {"title": title, "amount": amount, "kind": kind}

    suggested_category = suggest_expense_category(title) if kind == "expense" and user_is_pro(user_id) else None
    if suggested_category:
        await message.answer(
            f"✨ Похоже, это {category_title(suggested_category)}.\nСохранить так?",
            reply_markup=auto_category_keyboard(suggested_category),
        )
        return

    action = "доход" if kind == "income" else "расход"
    await message.answer(
        f"Записываем {action}: {title} — {money(amount)}\nВыбери категорию:",
        reply_markup=category_keyboard(kind),
    )


async def main_menu_callback_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    action = strip_prefix(callback.data, MENU_CALLBACK_PREFIX)
    await callback.answer()

    if action == "expense":
        input_modes[user_id] = "expense"
        await callback.message.answer(
            "➖ Напиши расход в формате `название сумма`.\nНапример: `кофе 300`",
            parse_mode="Markdown",
        )
        return

    if action == "income":
        input_modes[user_id] = "income"
        await callback.message.answer(
            "➕ Напиши доход в формате `название сумма`.\nНапример: `зарплата 50000`",
            parse_mode="Markdown",
        )
        return

    if action == "balance":
        await callback.message.answer(
            balance_text(user_id),
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action == "stats":
        await callback.message.answer(
            stats_text(user_id),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action == "categories":
        await callback.message.answer(
            "🗂 Выбери категорию расходов, покажу траты за месяц:",
            reply_markup=categories_keyboard(),
        )
        return

    if action == "history":
        await callback.message.answer(
            history_text(user_id),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action == "delete_last":
        result = delete_last_transaction(user_id)
        await callback.message.answer(
            result or "Удалять пока нечего.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if action == "pro":
        if not user_is_pro(user_id):
            await callback.message.answer_invoice(
                **pro_invoice_kwargs(user_id),
            )
            return

        await callback.message.answer(
            pro_menu_text(),
            reply_markup=pro_menu_keyboard(),
        )


async def category_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    transaction = pending_transactions.pop(user_id, None)

    if transaction is None:
        await callback.answer("Эта операция уже не ожидает категорию", show_alert=True)
        return

    category_id = strip_prefix(callback.data, "cat:")
    save_transaction(
        user_id,
        str(transaction["title"]),
        int(transaction["amount"]),
        category_id,
        str(transaction["kind"]),
    )

    sign = "➕" if transaction["kind"] == "income" else "➖"
    await callback.message.edit_text(
        "Готово, сохранила 💖\n\n"
        f"{sign} {category_title(category_id)}\n"
        f"{transaction['title']} — {money(int(transaction['amount']))}"
    )
    if transaction["kind"] == "expense":
        warning = limit_warning_text(user_id, category_id)
        if warning:
            await callback.message.answer(warning, reply_markup=main_menu_keyboard(user_id))
        if looks_recurring(str(transaction["title"])) and user_is_pro(user_id):
            pending_recurring_suggestions[user_id] = {
                "title": str(transaction["title"]),
                "amount": int(transaction["amount"]),
                "category": category_id,
            }
            await callback.message.answer(
                "🔁 Похоже, это регулярный платеж. Добавить в регулярные?",
                reply_markup=recurring_suggestion_keyboard(),
            )
    await callback.answer()


async def category_details_handler(callback: CallbackQuery) -> None:
    category_id = strip_prefix(callback.data, "showcat:")
    await callback.message.edit_text(
        category_details_text(callback.from_user.id, category_id),
        reply_markup=categories_keyboard(),
    )
    await callback.answer()


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN. Добавь его в переменные окружения.")

    logging.basicConfig(level=logging.INFO)
    init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(start_handler, Command("start"))
    dp.message.register(stats_handler, Command("stats"))
    dp.message.register(balance_handler, Command("balance"))
    dp.message.register(history_handler, Command("history"))
    dp.message.register(categories_handler, Command("categories"))
    dp.message.register(buy_pro_handler, Command("pro"))
    dp.message.register(successful_payment_handler, F.successful_payment)
    dp.message.register(add_expense_button_handler, F.text == "➖ Расход")
    dp.message.register(add_income_button_handler, F.text == "➕ Доход")
    dp.message.register(balance_handler, F.text == "💰 Баланс")
    dp.message.register(stats_handler, F.text == "📊 Статистика")
    dp.message.register(categories_handler, F.text == "🗂 Категории")
    dp.message.register(history_handler, F.text == "🧾 История")
    dp.message.register(buy_pro_handler, F.text == "💎 Купить Pro")
    dp.message.register(delete_last_handler, F.text == "↩️ Удалить последнюю")
    dp.message.register(expense_handler, F.text)
    dp.pre_checkout_query.register(pre_checkout_handler)
    dp.callback_query.register(buy_pro_callback_handler, F.data == BUY_PRO_CALLBACK)
    dp.callback_query.register(pro_callback_handler, F.data.startswith(PRO_CALLBACK_PREFIX))
    dp.callback_query.register(limit_callback_handler, F.data.startswith(LIMIT_CALLBACK_PREFIX))
    dp.callback_query.register(recurring_callback_handler, F.data.startswith(RECURRING_CALLBACK_PREFIX))
    dp.callback_query.register(auto_category_callback_handler, F.data.startswith(AUTOCAT_CALLBACK_PREFIX))
    dp.callback_query.register(main_menu_callback_handler, F.data.startswith(MENU_CALLBACK_PREFIX))
    dp.callback_query.register(category_handler, F.data.startswith("cat:"))
    dp.callback_query.register(category_details_handler, F.data.startswith("showcat:"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
