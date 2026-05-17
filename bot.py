import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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
PINK_IMAGE_PATH = Path("assets/pink-wallet.png")

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
    "🌸 Картинка",
    "↩️ Удалить последнюю",
}

pending_transactions: Dict[int, Dict[str, Union[int, str]]] = {}
input_modes: Dict[int, str] = {}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn:
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


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➖ Расход"), KeyboardButton(text="➕ Доход")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🗂 Категории"), KeyboardButton(text="🧾 История")],
            [KeyboardButton(text="🌸 Картинка"), KeyboardButton(text="↩️ Удалить последнюю")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Например: кофе 300",
    )


def category_keyboard(kind: str) -> InlineKeyboardMarkup:
    categories = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    rows = []
    for category_id, title in categories:
        rows.append([InlineKeyboardButton(text=title, callback_data=f"cat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


async def answer_with_pink_image(message: Message, caption: str) -> None:
    if PINK_IMAGE_PATH.exists():
        await message.answer_photo(
            FSInputFile(PINK_IMAGE_PATH),
            caption=caption,
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return

    await message.answer(caption, parse_mode="Markdown", reply_markup=main_keyboard())


async def start_handler(message: Message) -> None:
    await answer_with_pink_image(
        message,
        "Привет, я твой розовый кошелек 🩷\n\n"
        "Быстро добавить расход: `кофе 300`, `такси 1000`.\n"
        "Доход можно так: `+ зарплата 50000`.\n\n"
        "А еще можно пользоваться кнопками снизу.",
    )


async def stats_handler(message: Message) -> None:
    await answer_with_pink_image(message, stats_text(message.from_user.id))


async def balance_handler(message: Message) -> None:
    await answer_with_pink_image(message, balance_text(message.from_user.id))


async def history_handler(message: Message) -> None:
    await message.answer(history_text(message.from_user.id), parse_mode="Markdown")


async def categories_handler(message: Message) -> None:
    await message.answer(
        "🗂 Выбери категорию расходов, покажу траты за месяц:",
        reply_markup=categories_keyboard(),
    )


async def image_handler(message: Message) -> None:
    await answer_with_pink_image(
        message,
        "🌸 Немного розового финансового настроения.\n\n"
        "Пиши расход или доход, а я все аккуратно сохраню.",
    )


async def delete_last_handler(message: Message) -> None:
    result = delete_last_transaction(message.from_user.id)
    await message.answer(result or "Удалять пока нечего.", reply_markup=main_keyboard())


async def add_expense_button_handler(message: Message) -> None:
    input_modes[message.from_user.id] = "expense"
    await message.answer(
        "➖ Напиши расход в формате `название сумма`.\nНапример: `кофе 300`",
        parse_mode="Markdown",
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

    kind = input_modes.get(user_id, "expense")
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
            reply_markup=main_keyboard(),
        )
        return

    input_modes.pop(user_id, None)
    title, amount = parsed
    pending_transactions[user_id] = {"title": title, "amount": amount, "kind": kind}

    action = "доход" if kind == "income" else "расход"
    await message.answer(
        f"Записываем {action}: {title} — {money(amount)}\nВыбери категорию:",
        reply_markup=category_keyboard(kind),
    )


async def category_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    transaction = pending_transactions.pop(user_id, None)

    if transaction is None:
        await callback.answer("Эта операция уже не ожидает категорию", show_alert=True)
        return

    category_id = callback.data.removeprefix("cat:")
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
    await callback.answer()


async def category_details_handler(callback: CallbackQuery) -> None:
    category_id = callback.data.removeprefix("showcat:")
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
    dp.message.register(image_handler, Command("image"))
    dp.message.register(add_expense_button_handler, F.text == "➖ Расход")
    dp.message.register(add_income_button_handler, F.text == "➕ Доход")
    dp.message.register(balance_handler, F.text == "💰 Баланс")
    dp.message.register(stats_handler, F.text == "📊 Статистика")
    dp.message.register(categories_handler, F.text == "🗂 Категории")
    dp.message.register(history_handler, F.text == "🧾 История")
    dp.message.register(image_handler, F.text == "🌸 Картинка")
    dp.message.register(delete_last_handler, F.text == "↩️ Удалить последнюю")
    dp.message.register(expense_handler, F.text)
    dp.callback_query.register(category_handler, F.data.startswith("cat:"))
    dp.callback_query.register(category_details_handler, F.data.startswith("showcat:"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
