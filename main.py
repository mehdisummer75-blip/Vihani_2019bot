import asyncio
import os
import random
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPPORT_URL = "https://t.me/Mahdi_hosseini1994"
DB_PATH = os.environ.get("DB_PATH", "orders.db")

# مدل‌ها: کلید -> (اسم، قیمت به هزار تومن)
MODELS = {
    "pro": ("مدل پرو", 998),
    "promax": ("مدل پرومکس", 1399),
    "premium": ("مدل پرمیوم", 1599),
}

STATUS = {
    "pending": "🟠 در انتظار پرداخت",
    "cancelled": "🔴 لغو شده",
}

LINE = "━━━━━━━━━━━━━━━━━━"
MAX_QTY_DIGITS = 6

dp = Dispatcher()


# ---------------------------------------------------------------- دیتابیس
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute(
    """
    CREATE TABLE IF NOT EXISTS invoices (
        invoice_no INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        model TEXT NOT NULL,
        qty INTEGER NOT NULL,
        unit_price INTEGER NOT NULL,
        total INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """
)
db.commit()


def create_invoice(user_id, key, qty):
    unit = MODELS[key][1] * 1000
    total = unit * qty
    for _ in range(50):
        no = random.randint(100000, 999999)
        try:
            db.execute(
                "INSERT INTO invoices (invoice_no, user_id, model, qty, unit_price, total, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (no, user_id, key, qty, unit, total, "pending"),
            )
            db.commit()
            return no
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not create invoice")


def get_invoice(no):
    return db.execute("SELECT * FROM invoices WHERE invoice_no = ?", (no,)).fetchone()


def set_status(no, status):
    db.execute("UPDATE invoices SET status = ? WHERE invoice_no = ?", (status, no))
    db.commit()


# ---------------------------------------------------------------- ابزارها
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fa(n):
    return f"{n:,}".replace(",", "٬").translate(FA_DIGITS)


def to_int(text):
    text = text.strip().translate(EN_DIGITS)
    return int(text) if text.isdigit() else None


def invoice_text(inv):
    name = MODELS.get(inv["model"], (inv["model"],))[0]
    return (
        f"🧾 <b>فاکتور سفارش</b>\n"
        f"{LINE}\n"
        f"🔖 شناسه فاکتور: <code>{inv['invoice_no']}</code>\n"
        f"📦 محصول: <b>{name}</b>\n"
        f"💎 قیمت واحد: {fa(inv['unit_price'] // 1000)} هزار تومن\n"
        f"🔢 تعداد: {fa(inv['qty'])} عدد\n"
        f"{LINE}\n"
        f"💰 <b>جمع کل: {fa(inv['total'])} تومن</b>\n"
        f"📌 وضعیت: {STATUS.get(inv['status'], inv['status'])}\n"
        f"{LINE}\n"
        "💡 شناسه فاکتور رو نگه دار؛ از «پیگیری سفارش» تو منوی اصلی "
        "می‌تونی وضعیتش رو ببینی."
    )


# ---------------------------------------------------------------- دکمه‌ها
class Order(StatesGroup):
    waiting_custom_qty = State()
    waiting_track = State()


def nav_row(back_to):
    return [
        InlineKeyboardButton(text="🔙 بازگشت به مرحله قبل", callback_data=back_to),
        InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main"),
    ]


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛍 خرید تکی", callback_data="buy_single"),
                InlineKeyboardButton(text="📦 خرید عمده", callback_data="buy_bulk"),
            ],
            [InlineKeyboardButton(text="🔍 پیگیری سفارش", callback_data="track")],
            [InlineKeyboardButton(text="🎁 دریافت نمونه کار رایگان", callback_data="free_sample")],
            [InlineKeyboardButton(text="🤝 همکاری در فروش", callback_data="partnership")],
            [
                InlineKeyboardButton(text="❓ سوالات متداول", callback_data="faq"),
                InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL),
            ],
        ]
    )


def models_menu():
    rows = [
        [InlineKeyboardButton(text=f"{name} | {fa(price)} هزار تومن", callback_data=f"model:{key}")]
        for key, (name, price) in MODELS.items()
    ]
    rows.append(nav_row("main"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qty_menu(key):
    rows = [
        [InlineKeyboardButton(text="1 عدد", callback_data=f"qty:{key}:1")],
        [InlineKeyboardButton(text="2 عدد", callback_data=f"qty:{key}:2")],
        [InlineKeyboardButton(text="3 عدد", callback_data=f"qty:{key}:3")],
        [InlineKeyboardButton(text="✏️ انتخاب تعداد دلخواه", callback_data=f"qty_custom:{key}")],
        nav_row("buy_single"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qty_text(key):
    name, price = MODELS[key]
    return (
        f"🛍 <b>{name}</b>\n"
        f"{LINE}\n"
        f"💎 قیمت واحد: {fa(price)} هزار تومن\n"
        f"{LINE}\n"
        "🔢 چند عدد نیاز داری؟\n"
        "تعداد سفارش رو از گزینه‌های زیر انتخاب کن:"
    )


MAIN_TEXT = "سلام، به ربات ما خوش اومدی 👋\nلطفاً یکی از گزینه‌های زیر رو انتخاب کن:"
MODELS_TEXT = "🛍 <b>خرید تکی</b>\nلطفاً مدل مورد نظرت رو انتخاب کن:"


# ---------------------------------------------------------------- هندلرها
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(MAIN_TEXT, reply_markup=main_menu())


@dp.callback_query(F.data == "main")
async def go_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(MAIN_TEXT, reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "buy_single")
async def buy_single(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(MODELS_TEXT, reply_markup=models_menu())
    await call.answer()


@dp.callback_query(F.data.startswith("model:"))
async def choose_model(call: CallbackQuery, state: FSMContext):
    await state.clear()
    key = call.data.split(":", 1)[1]
    await call.message.edit_text(qty_text(key), reply_markup=qty_menu(key))
    await call.answer()


@dp.callback_query(F.data.startswith("qty:"))
async def choose_qty(call: CallbackQuery):
    _, key, qty = call.data.split(":")
    no = create_invoice(call.from_user.id, key, int(qty))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"invback:{no}")])
    await call.message.edit_text(invoice_text(get_invoice(no)), reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("invback:"))
async def invoice_back(call: CallbackQuery):
    no = int(call.data.split(":", 1)[1])
    inv = get_invoice(no)
    if inv is None or inv["user_id"] != call.from_user.id:
        await call.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if inv["status"] == "pending":
        set_status(no, "cancelled")  # برگشت یعنی تغییر سفارش
    key = inv["model"]
    await call.message.edit_text(qty_text(key), reply_markup=qty_menu(key))
    await call.answer()


@dp.callback_query(F.data.startswith("qty_custom:"))
async def custom_qty(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.set_state(Order.waiting_custom_qty)
    await state.update_data(model=key)
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"model:{key}")])
    await call.message.edit_text(
        "✏️ <b>تعداد دلخواه</b>\n"
        f"{LINE}\n"
        "تعداد سفارشت رو به صورت عدد برام بفرست.\n"
        "📝 مثال: 5",
        reply_markup=kb,
    )
    await call.answer()


@dp.message(Order.waiting_custom_qty, F.text)
async def receive_custom_qty(message: Message, state: FSMContext):
    raw = message.text.strip().translate(EN_DIGITS)
    qty = to_int(raw)
    if qty is None or qty < 1:
        await message.answer("❌ لطفاً فقط یه عدد صحیح بفرست، مثلاً 5")
        return
    if len(raw) > MAX_QTY_DIGITS:
        await message.answer("❌ این تعداد خیلی زیاده. لطفاً عدد کوچک‌تری بفرست.")
        return
    data = await state.get_data()
    key = data["model"]
    await state.clear()
    no = create_invoice(message.from_user.id, key, qty)
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"invback:{no}")])
    await message.answer(invoice_text(get_invoice(no)), reply_markup=kb)


@dp.callback_query(F.data == "track")
async def track(call: CallbackQuery, state: FSMContext):
    await state.set_state(Order.waiting_track)
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row("main")])
    await call.message.edit_text(
        "🔍 <b>پیگیری سفارش</b>\n"
        f"{LINE}\n"
        "شناسهٔ فاکتورت رو برام بفرست (عدد شش‌رقمی).",
        reply_markup=kb,
    )
    await call.answer()


@dp.message(Order.waiting_track, F.text)
async def receive_track(message: Message, state: FSMContext):
    no = to_int(message.text)
    inv = get_invoice(no) if no is not None else None
    if inv is None or inv["user_id"] != message.from_user.id:
        await message.answer(
            "❌ فاکتوری با این شناسه پیدا نشد.\n"
            "شناسه رو دوباره بررسی کن و بفرست."
        )
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row("main")])
    await message.answer(invoice_text(inv), reply_markup=kb)


@dp.callback_query(F.data.in_({"buy_bulk", "free_sample", "partnership", "faq"}))
async def placeholder(call: CallbackQuery):
    await call.answer("این بخش به‌زودی فعال می‌شه 🙏", show_alert=True)


# ---------------------------------------------------------------- اجرا
async def health(request):
    return web.Response(text="OK")


async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()


async def main():
    await run_web()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
