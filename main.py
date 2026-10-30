import asyncio
import logging
import os
import random
from html import escape
from urllib.parse import unquote, urlparse

import aiohttp
import pg8000.dbapi
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)  # آیدی عددی تلگرام خودت
SUPPORT_URL = "https://t.me/Mahdi_hosseini1994"
DATABASE_URL = os.environ["DATABASE_URL"]  # آدرس اتصال Neon

# باشگاه مشتریان
CLUB_URL = "https://t.me/+CSKtwO1aC2ExM2Rk"
CLUB_DISCOUNT = 5  # درصد تخفیف دائمی اعضا
# آیدی عددی کانال خصوصی (مثل -1001234567890). ربات باید ادمین کانال باشه.
CLUB_CHAT_ID = int(os.environ.get("CLUB_CHAT_ID", "0") or 0)
CLUB_LEFT_TEXT = (
    "مشتری گرامی، تخفیف ۵٪ دائمی و دسترسی زودهنگام به کمپین‌ها مخصوص اعضای کانال "
    "CLUB VIHANI است. لطفاً برای فعال شدن مجدد امتیازات، مجدداً در کانال عضو شوید."
)
# متن بخش جشنواره‌ها (هر وقت کمپین داشتی اینجا عوضش کن)
CAMPAIGNS_TEXT = (
    "🎉 <b>جشنواره‌ها و کمپین‌های فروش</b>\n"
    f"{'━' * 18}\n"
    "👑 شما عضو فعال باشگاه هستی و به کمپین‌ها زودتر دسترسی داری.\n"
    "در حال حاضر کمپین فعالی نداریم؛ به‌محض شروع، همین‌جا و داخل کانال خبرت می‌کنیم."
)
CLUB_BTN = "VIP 💎 CLUB VIHANI"
CLUB_SHARE_BTN = "📱 ارسال شماره من"
CLUB_CANCEL_BTN = "🏠 بازگشت به منوی اصلی"
CLUB_TEXT = (
    "روی تمام سفارش‌های شما تا وقتی عضو این کانال باشین ۵ درصد تخفیف اعمال می‌شود. "
    "همچنین از دسترسی سریع‌تر یک‌روزه به جشنواره‌ها و کمپین‌های فروش نسبت به مشتریان عادی "
    "برخوردار خواهید بود. 👑"
)

# مشخصات کارت (برای پرداخت کارت به کارت)
CARD_IMAGE = "card.png"
CARD_NUMBER = "6104337375248422"
CARD_OWNER = "مهدی حسینی"
CARD_BANK = "بانک ملت"

# مدل‌ها: کلید -> (اسم، قیمت به هزار تومن)
MODELS = {
    "pro": ("مدل پرو", 998),
    "promax": ("مدل پرومکس", 1399),
    "premium": ("مدل پرمیوم", 1599),
}

STATUS = {
    "pending": "🟠 در انتظار پرداخت",
    "awaiting_receipt": "🟡 در انتظار ارسال فیش",
    "reviewing": "🔵 فیش در حال بررسی",
    "paid": "🟢 پرداخت شده",
    "rejected": "⛔️ پرداخت تأیید نشد",
    "cancelled": "🔴 لغو شده",
}
PAYABLE = ("pending", "awaiting_receipt")

LINE = "━━━━━━━━━━━━━━━━━━"
MAX_QTY_DIGITS = 6

FRESH_NOTE = (
    "\n💡 شناسه فاکتور رو نگه دار؛ از «پیگیری سفارش» تو منوی اصلی "
    "می‌تونی وضعیتش رو ببینی."
    "\n⚠️ در صورت بازگشت به منوی اصلی یا مرحلهٔ قبل، فاکتور لغو می‌شود."
)

dp = Dispatcher()


# ---------------------------------------------------------------- دیتابیس (Postgres روی Neon)
CUSTOMER_FIELDS = ("full_name", "phone", "address", "postal", "emergency")
_conn = None


def _connect():
    u = urlparse(DATABASE_URL)
    return pg8000.dbapi.connect(
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        host=u.hostname,
        port=u.port or 5432,
        database=(u.path or "/").lstrip("/") or None,
        ssl_context=True,
        timeout=20,
    )


def tx(work):
    """work(cur) رو داخل یک تراکنش اجرا می‌کنه. اگه اتصال قطع شده بود، یک بار دوباره وصل می‌شه."""
    global _conn
    for attempt in (1, 2):
        try:
            if _conn is None:
                _conn = _connect()
            cur = _conn.cursor()
            result = work(cur)
            _conn.commit()
            return result
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            if attempt == 2:
                raise


def _rows(cur):
    if cur.description is None:
        return []
    cols = [d[0].decode() if isinstance(d[0], bytes) else d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def q_one(sql, params=()):
    def work(cur):
        cur.execute(sql, params)
        rows = _rows(cur)
        return rows[0] if rows else None

    return tx(work)


def q_exec(sql, params=()):
    def work(cur):
        cur.execute(sql, params)
        return cur.rowcount

    return tx(work)


def init_db():
    q_exec(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_no BIGINT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            model TEXT NOT NULL,
            qty BIGINT NOT NULL,
            unit_price BIGINT NOT NULL,
            total BIGINT NOT NULL,
            status TEXT NOT NULL,
            full_name TEXT,
            phone TEXT,
            address TEXT,
            postal TEXT,
            emergency TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    q_exec(
        "CREATE TABLE IF NOT EXISTS wallets ("
        "user_id BIGINT PRIMARY KEY, balance BIGINT NOT NULL DEFAULT 0)"
    )
    q_exec(
        "CREATE TABLE IF NOT EXISTS club_members ("
        "user_id BIGINT PRIMARY KEY, phone TEXT NOT NULL, joined_at TIMESTAMPTZ DEFAULT now())"
    )
    q_exec(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS discount_percent BIGINT NOT NULL DEFAULT 0"
    )


init_db()


def get_member(user_id):
    return q_one("SELECT user_id, phone FROM club_members WHERE user_id = %s", (user_id,))


def save_member(user_id, phone):
    q_exec(
        "INSERT INTO club_members (user_id, phone) VALUES (%s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET phone = EXCLUDED.phone",
        (user_id, phone),
    )


def create_invoice(user_id, key, qty, discount=0):
    unit = MODELS[key][1] * 1000
    total = unit * qty * (100 - discount) // 100
    for _ in range(50):
        no = random.randint(100000, 999999)
        added = q_exec(
            "INSERT INTO invoices"
            " (invoice_no, user_id, model, qty, unit_price, total, status, discount_percent)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (invoice_no) DO NOTHING",
            (no, user_id, key, qty, unit, total, "pending", discount),
        )
        if added == 1:
            return no
    raise RuntimeError("could not create invoice")


def get_invoice(no):
    if no is None or not (0 < no < 10**12):
        return None
    return q_one("SELECT * FROM invoices WHERE invoice_no = %s", (no,))


def set_status(no, status):
    q_exec("UPDATE invoices SET status = %s WHERE invoice_no = %s", (status, no))


def set_field(no, field, value):
    assert field in CUSTOMER_FIELDS
    q_exec(f"UPDATE invoices SET {field} = %s WHERE invoice_no = %s", (value, no))


def cancel_if_pending(no, user_id):
    """فاکتور پرداخت‌نشدهٔ همین کاربر رو لغو می‌کنه و فاکتور رو برمی‌گردونه."""
    inv = get_invoice(no)
    if inv is None or inv["user_id"] != user_id:
        return None
    if inv["status"] in PAYABLE:
        set_status(no, "cancelled")
    return inv


def own_invoice(no, user_id, statuses=PAYABLE):
    """فاکتورِ معتبرِ همین کاربر (یا None)."""
    try:
        inv = get_invoice(int(no))
    except (TypeError, ValueError):
        return None
    if inv is None or inv["user_id"] != user_id or inv["status"] not in statuses:
        return None
    return inv


def get_balance(user_id):
    row = q_one("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    return row["balance"] if row else 0


def add_credit(user_id, amount):
    def work(cur):
        cur.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        cur.execute(
            "UPDATE wallets SET balance = balance + %s WHERE user_id = %s", (amount, user_id)
        )
        cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]

    return tx(work)


def pay_with_wallet(no, user_id, total):
    def work(cur):
        cur.execute(
            "UPDATE wallets SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
            (total, user_id, total),
        )
        if cur.rowcount != 1:
            return False
        cur.execute("UPDATE invoices SET status = 'paid' WHERE invoice_no = %s", (no,))
        return True

    return tx(work)


# ---------------------------------------------------------------- ابزارها
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fa(n):
    return f"{n:,}".replace(",", "٬").translate(FA_DIGITS)


def to_int(text):
    text = text.strip().translate(EN_DIGITS)
    return int(text) if text.isdigit() else None


def invoice_text(inv, fresh=False):
    name = MODELS.get(inv["model"], (inv["model"],))[0]
    pct = inv.get("discount_percent") or 0
    discount_line = ""
    if pct:
        saved = inv["unit_price"] * inv["qty"] - inv["total"]
        discount_line = f"👑 تخفیف باشگاه ({fa(pct)}٪): {fa(saved)} تومن\n"
    return (
        f"🧾 <b>فاکتور سفارش</b>\n"
        f"{LINE}\n"
        f"🔖 شناسه فاکتور: <code>{inv['invoice_no']}</code>\n"
        f"📦 محصول: <b>{name}</b>\n"
        f"💎 قیمت واحد: {fa(inv['unit_price'] // 1000)} هزار تومن\n"
        f"🔢 تعداد: {fa(inv['qty'])} عدد\n"
        f"{discount_line}"
        f"{LINE}\n"
        f"💰 <b>جمع کل: {fa(inv['total'])} تومن</b>\n"
        f"📌 وضعیت: {STATUS.get(inv['status'], inv['status'])}\n"
        f"{LINE}"
    ) + (FRESH_NOTE if fresh else "")


def order_details(inv):
    name = MODELS.get(inv["model"], (inv["model"],))[0]
    return (
        f"🔖 فاکتور: <code>{inv['invoice_no']}</code>\n"
        f"📦 {name} × {fa(inv['qty'])}\n"
        f"💰 مبلغ: <b>{fa(inv['total'])} تومن</b>\n"
        + (f"👑 عضو باشگاه VIP ({fa(inv['discount_percent'])}٪ تخفیف)\n" if inv.get("discount_percent") else "")
        + f"{LINE}\n"
        f"👤 {escape(inv['full_name'] or '—')}\n"
        f"📞 {escape(inv['phone'] or '—')}\n"
        f"📍 {escape(inv['address'] or '—')}\n"
        f"📮 کدپستی: {escape(inv['postal'] or '—')}\n"
        f"🆘 اضطراری: {escape(inv['emergency'] or '—')}\n"
        f"🆔 آیدی تلگرام: <code>{inv['user_id']}</code>"
    )


async def show(call, text, kb):
    """پیام فعلی رو ویرایش می‌کنه؛ اگه عکس بود حذفش می‌کنه و پیام تازه می‌فرسته."""
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=kb)


async def send(target, text, kb):
    if isinstance(target, CallbackQuery):
        await show(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


# ---------------------------------------------------------------- دکمه‌ها
class Order(StatesGroup):
    waiting_custom_qty = State()
    waiting_track = State()


class Club(StatesGroup):
    contact = State()


class Checkout(StatesGroup):
    name = State()
    phone = State()
    address = State()
    postal = State()
    emergency = State()
    receipt = State()


def nav_row(back_to, main_cb="main"):
    return [
        InlineKeyboardButton(text="🔙 بازگشت به مرحله قبل", callback_data=back_to),
        InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data=main_cb),
    ]


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛍 خرید تکی", callback_data="buy_single"),
                InlineKeyboardButton(text="📦 خرید عمده", callback_data="buy_bulk"),
            ],
            [InlineKeyboardButton(text=CLUB_BTN, callback_data="vip_club")],
            [InlineKeyboardButton(text="🎉 جشنواره‌ها و کمپین‌های فروش", callback_data="campaigns")],
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


def invoice_kb(no):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ادامه خرید", callback_data=f"ask:name:{no}")],
            nav_row(f"invback:{no}", f"invmain:{no}"),
        ]
    )


MAIN_TEXT = "سلام، به ربات ما خوش اومدی 👋\nلطفاً یکی از گزینه‌های زیر رو انتخاب کن:"
MODELS_TEXT = "🛍 <b>خرید تکی</b>\nلطفاً مدل مورد نظرت رو انتخاب کن:"
INVALID_TEXT = "این فاکتور دیگه معتبر نیست. لطفاً از منوی اصلی دوباره شروع کن."


# ---------------------------------------------------------------- بررسی آنی عضویت باشگاه
_last_club_err = 0.0


async def club_status(bot, user_id):
    """'none' = ثبت‌نام نکرده | 'active' = عضو کانال | 'left' = از کانال خارج شده | 'error' = بررسی ممکن نشد"""
    global _last_club_err
    if not get_member(user_id):
        return "none"
    try:
        if not CLUB_CHAT_ID:
            raise RuntimeError("CLUB_CHAT_ID is not set")
        m = await bot.get_chat_member(CLUB_CHAT_ID, user_id)
        if m.status in ("creator", "administrator", "member"):
            return "active"
        if m.status == "restricted" and getattr(m, "is_member", False):
            return "active"
        return "left"
    except TelegramBadRequest as e:
        if "user not found" in str(e).lower() or "participant_id_invalid" in str(e).lower():
            return "left"
        err = e
    except Exception as e:
        err = e
    # خطا: حداکثر ساعتی یک بار به ادمین خبر بده
    now = asyncio.get_event_loop().time()
    if ADMIN_ID and now - _last_club_err > 3600:
        _last_club_err = now
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ بررسی عضویت کانال باشگاه ناموفق بود:\n<code>{escape(str(err))}</code>\n"
                "مطمئن شو ربات ادمین کانال هست و CLUB_CHAT_ID درسته.",
            )
        except Exception:
            pass
    return "error"


async def club_discount(bot, user_id):
    """(درصد تخفیف، وضعیت) رو برمی‌گردونه؛ تخفیف فقط برای عضو فعال کانال."""
    status = await club_status(bot, user_id)
    return (CLUB_DISCOUNT if status == "active" else 0), status


async def club_left_notice(message, status):
    if status == "left":
        await message.answer(CLUB_LEFT_TEXT + f"\n\n🔗 {CLUB_URL}", reply_markup=club_kb())


# ---------------------------------------------------------------- منو و خرید
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(MAIN_TEXT, reply_markup=main_menu())


@dp.callback_query(F.data == "main")
async def go_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, MAIN_TEXT, main_menu())
    await call.answer()


@dp.callback_query(F.data == "buy_single")
async def buy_single(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, MODELS_TEXT, models_menu())
    await call.answer()


@dp.callback_query(F.data.startswith("model:"))
async def choose_model(call: CallbackQuery, state: FSMContext):
    await state.clear()
    key = call.data.split(":", 1)[1]
    await show(call, qty_text(key), qty_menu(key))
    await call.answer()


@dp.callback_query(F.data.startswith("qty:"))
async def choose_qty(call: CallbackQuery):
    _, key, qty = call.data.split(":")
    discount, status = await club_discount(call.bot, call.from_user.id)
    no = create_invoice(call.from_user.id, key, int(qty), discount)
    await show(call, invoice_text(get_invoice(no), fresh=True), invoice_kb(no))
    await club_left_notice(call.message, status)
    await call.answer()


@dp.callback_query(F.data.startswith("invback:"))
async def invoice_back(call: CallbackQuery):
    no = int(call.data.split(":", 1)[1])
    inv = cancel_if_pending(no, call.from_user.id)
    if inv is None:
        await call.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    key = inv["model"]
    await show(call, qty_text(key), qty_menu(key))
    await call.answer()


@dp.callback_query(F.data.startswith("invmain:"))
async def invoice_main(call: CallbackQuery, state: FSMContext):
    no = int(call.data.split(":", 1)[1])
    cancel_if_pending(no, call.from_user.id)
    await state.clear()
    await show(call, MAIN_TEXT, main_menu())
    await call.answer()


@dp.callback_query(F.data.startswith("invshow:"))
async def invoice_show(call: CallbackQuery, state: FSMContext):
    await state.clear()
    no = call.data.split(":", 1)[1]
    inv = own_invoice(no, call.from_user.id)
    if inv is None:
        await show(call, INVALID_TEXT, main_menu())
    else:
        await show(call, invoice_text(inv, fresh=True), invoice_kb(inv["invoice_no"]))
    await call.answer()


@dp.callback_query(F.data.startswith("qty_custom:"))
async def custom_qty(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.set_state(Order.waiting_custom_qty)
    await state.update_data(model=key)
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"model:{key}")])
    await show(
        call,
        "✏️ <b>تعداد دلخواه</b>\n"
        f"{LINE}\n"
        "تعداد سفارشت رو به صورت عدد برام بفرست.\n"
        "📝 مثال: 5",
        kb,
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
    discount, status = await club_discount(message.bot, message.from_user.id)
    no = create_invoice(message.from_user.id, key, qty, discount)
    await message.answer(invoice_text(get_invoice(no), fresh=True), reply_markup=invoice_kb(no))
    await club_left_notice(message, status)


# ---------------------------------------------------------------- مشخصات مشتری
STEPS = ["name", "phone", "address", "postal", "emergency"]
FIELD = {
    "name": "full_name",
    "phone": "phone",
    "address": "address",
    "postal": "postal",
    "emergency": "emergency",
}
QUESTIONS = {
    "name": ("👤 <b>نام و نام خانوادگی</b>", "نام و نام خانوادگی گیرنده رو بفرست.", False),
    "phone": ("📞 <b>شماره تلفن</b>", "شماره تماست رو بفرست (مثلاً 09123456789).", False),
    "address": (
        "📍 <b>آدرس دقیق</b>",
        "آدرس کامل پستی رو بفرست (استان، شهر، خیابان، پلاک، واحد).",
        False,
    ),
    "postal": (
        "📮 <b>کد پستی</b> (اختیاری)",
        "کد پستی ۱۰ رقمی رو بفرست، یا «رد کردن» رو بزن.",
        True,
    ),
    "emergency": (
        "🆘 <b>شماره اضطراری</b> (اختیاری)",
        "یه شماره دیگه برای وقتی که در دسترس نیستی (برای تحویل پست) بفرست، یا «رد کردن» رو بزن.",
        True,
    ),
}
STEP_STATES = [getattr(Checkout, s) for s in STEPS]


def clean_phone(text):
    d = text.strip().translate(EN_DIGITS).replace(" ", "").replace("-", "").lstrip("+")
    return d if d.isdigit() and 10 <= len(d) <= 13 else None


def validate(step, text):
    """(مقدار، پیام خطا) رو برمی‌گردونه."""
    t = text.replace("\x00", "").strip()
    if step == "name":
        if 3 <= len(t) <= 60 and any(ch.isalpha() for ch in t):
            return t, None
        return None, "❌ لطفاً نام و نام خانوادگی رو کامل و درست بفرست."
    if step in ("phone", "emergency"):
        d = clean_phone(t)
        if d:
            return d, None
        return None, "❌ شماره معتبر نیست. لطفاً شماره رو درست بفرست، مثلاً 09123456789"
    if step == "address":
        if 10 <= len(t) <= 300:
            return t, None
        return None, "❌ آدرس باید کامل‌تر باشه (حداقل ۱۰ و حداکثر ۳۰۰ حرف)."
    if step == "postal":
        d = t.translate(EN_DIGITS).replace(" ", "").replace("-", "")
        if d.isdigit() and len(d) == 10:
            return d, None
        return None, "❌ کد پستی باید ۱۰ رقم باشه. دوباره بفرست یا «رد کردن» رو بزن."
    return None, "❌ ورودی معتبر نیست."


async def ask_step(target, state, step, no):
    await state.set_state(getattr(Checkout, step))
    await state.update_data(no=no)
    idx = STEPS.index(step)
    title, hint, optional = QUESTIONS[step]
    text = f"{title}\n{LINE}\n{hint}\n\n📍 مرحله {fa(idx + 1)} از {fa(len(STEPS))}"
    back = f"invshow:{no}" if idx == 0 else f"ask:{STEPS[idx - 1]}:{no}"
    rows = []
    if optional:
        rows.append([InlineKeyboardButton(text="⏭ رد کردن", callback_data=f"skip:{no}")])
    rows.append(nav_row(back, f"invmain:{no}"))
    await send(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


def payment_text(inv):
    return (
        f"💳 <b>انتخاب نحوه پرداخت</b>\n"
        f"{LINE}\n"
        f"🔖 شناسه فاکتور: <code>{inv['invoice_no']}</code>\n"
        f"💰 مبلغ قابل پرداخت: <b>{fa(inv['total'])} تومن</b>\n"
        f"{LINE}\n"
        "لطفاً روش پرداخت رو انتخاب کن:"
    )


def payment_kb(no):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"paycard:{no}")],
            [InlineKeyboardButton(text="👛 پرداخت از کیف پول", callback_data=f"paywallet:{no}")],
            nav_row(f"ask:emergency:{no}", f"invmain:{no}"),
        ]
    )


async def after_step(target, state, step, no):
    idx = STEPS.index(step)
    if idx + 1 < len(STEPS):
        await ask_step(target, state, STEPS[idx + 1], no)
        return
    await state.clear()
    inv = get_invoice(no)
    await send(target, payment_text(inv), payment_kb(no))


@dp.callback_query(F.data.startswith("ask:"))
async def ask_callback(call: CallbackQuery, state: FSMContext):
    _, step, no = call.data.split(":")
    if step not in STEPS or own_invoice(no, call.from_user.id) is None:
        await show(call, INVALID_TEXT, main_menu())
        await call.answer()
        return
    await ask_step(call, state, step, int(no))
    await call.answer()


@dp.message(StateFilter(*STEP_STATES), F.text)
async def receive_step(message: Message, state: FSMContext):
    cur = await state.get_state()
    step = cur.split(":")[1]
    data = await state.get_data()
    no = data.get("no")
    if no is None or own_invoice(no, message.from_user.id) is None:
        await state.clear()
        await message.answer(INVALID_TEXT, reply_markup=main_menu())
        return
    value, error = validate(step, message.text)
    if error:
        await message.answer(error)
        return
    set_field(no, FIELD[step], value)
    await after_step(message, state, step, no)


@dp.callback_query(F.data.startswith("skip:"))
async def skip_step(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    cur = await state.get_state()
    step = cur.split(":")[1] if cur else None
    if own_invoice(no, call.from_user.id) is None:
        await show(call, INVALID_TEXT, main_menu())
        await call.answer()
        return
    if step not in ("postal", "emergency"):
        await call.answer()
        return
    await after_step(call, state, step, int(no))
    await call.answer()


# ---------------------------------------------------------------- پرداخت
@dp.callback_query(F.data.startswith("pay:"))
async def pay_screen(call: CallbackQuery, state: FSMContext):
    await state.clear()
    no = call.data.split(":", 1)[1]
    inv = own_invoice(no, call.from_user.id)
    if inv is None:
        await show(call, INVALID_TEXT, main_menu())
    else:
        await show(call, payment_text(inv), payment_kb(inv["invoice_no"]))
    await call.answer()


@dp.callback_query(F.data.startswith("paycard:"))
async def pay_card(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    inv = own_invoice(no, call.from_user.id)
    if inv is None:
        await call.answer("این فاکتور دیگه معتبر نیست.", show_alert=True)
        return
    no = inv["invoice_no"]
    set_status(no, "awaiting_receipt")
    await state.set_state(Checkout.receipt)
    await state.update_data(no=no)
    caption = (
        f"💳 <b>پرداخت کارت به کارت</b>\n"
        f"{LINE}\n"
        f"💰 مبلغ قابل پرداخت: <b>{fa(inv['total'])} تومن</b>\n"
        f"🔖 شناسه فاکتور: <code>{no}</code>\n"
        f"{LINE}\n"
        f"💳 شماره کارت:\n<code>{CARD_NUMBER}</code>\n"
        f"👤 به نام: <b>{CARD_OWNER}</b>\n"
        f"🏦 {CARD_BANK}\n"
        f"{LINE}\n"
        "📸 بعد از واریز، عکس فیش رو همین‌جا و بدون هیچ متنی ارسال کن."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"pay:{no}", f"invmain:{no}")])
    try:
        await call.message.delete()
    except Exception:
        pass
    if os.path.exists(CARD_IMAGE):
        await call.message.answer_photo(FSInputFile(CARD_IMAGE), caption=caption, reply_markup=kb)
    else:
        await call.message.answer(caption, reply_markup=kb)
    await call.answer()


def support_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)]]
    )


@dp.message(Checkout.receipt, F.photo | F.document)
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    no = data.get("no")
    inv = own_invoice(no, message.from_user.id, statuses=("awaiting_receipt",)) if no else None
    if inv is None:
        await state.clear()
        await message.answer(INVALID_TEXT, reply_markup=main_menu())
        return
    if not ADMIN_ID:
        await message.answer("⚠️ مشکل فنی پیش اومد. لطفاً با پشتیبانی تماس بگیر.")
        return
    if message.document and not (message.document.mime_type or "").lower().startswith("image/"):
        await message.answer("📸 لطفاً فیش رو به صورت عکس یا اسکرین‌شات بفرست.", reply_markup=support_kb())
        return

    set_status(no, "reviewing")
    uname = message.from_user.username
    caption = (
        f"🧾 <b>فیش جدید برای بررسی</b>\n{LINE}\n"
        + order_details(get_invoice(no))
        + (f"\n🔗 @{escape(uname)}" if uname else "")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تایید", callback_data=f"adm:ok:{no}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"adm:no:{no}"),
            ]
        ]
    )
    try:
        if message.photo:
            await message.bot.send_photo(
                ADMIN_ID, message.photo[-1].file_id, caption=caption, reply_markup=kb
            )
        else:
            await message.bot.send_document(
                ADMIN_ID, message.document.file_id, caption=caption, reply_markup=kb
            )
    except Exception:
        set_status(no, "awaiting_receipt")
        await message.answer("⚠️ ارسال فیش انجام نشد. لطفاً دوباره بفرست یا با پشتیبانی تماس بگیر.")
        return
    await state.clear()
    await message.answer(
        "✅ فیش شما دریافت شد و در حال بررسیه.\nنتیجه همین‌جا بهت اعلام می‌شه.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="main")]]
        ),
    )


@dp.message(Checkout.receipt)
async def receipt_wrong(message: Message):
    await message.answer("📸 لطفاً فقط عکس فیش واریزی رو بفرست (بدون متن).")


async def send_success(chat_id, no, bot=None, message=None):
    text1 = (
        f"سفارش شما با شناسه {no} با موفقیت ثبت شد ✅\n"
        "سفارش شما در حال آماده سازی است🧑‍💻\n"
        "بعد ارسال کد پیگیری سفارش براتون ارسال میشه🎫"
    )
    text2 = "تشکر از محبت سلطان 😎"
    if message is not None:
        await message.answer(text1)
        await message.answer(text2)
    else:
        await bot.send_message(chat_id, text1)
        await bot.send_message(chat_id, text2)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_decision(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True)
        return
    _, action, no = call.data.split(":")
    inv = get_invoice(int(no))
    if inv is None or inv["status"] != "reviewing":
        await call.answer("این فیش قبلاً بررسی شده.", show_alert=True)
        return
    old = call.message.html_text or ""
    if action == "ok":
        set_status(inv["invoice_no"], "paid")
        mark = "✅ <b>تأیید شد</b>"
    else:
        set_status(inv["invoice_no"], "rejected")
        mark = "❌ <b>رد شد</b>"
    try:
        await call.message.edit_caption(caption=f"{old}\n\n{mark}", reply_markup=None)
    except Exception:
        pass
    try:
        if action == "ok":
            await send_success(inv["user_id"], inv["invoice_no"], bot=call.bot)
        else:
            await call.bot.send_message(
                inv["user_id"],
                f"❌ فیش ارسالی برای فاکتور {inv['invoice_no']} تأیید نشد.\n"
                "اگه واریز انجام شده، لطفاً با پشتیبانی در تماس باش.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)]]
                ),
            )
    except Exception:
        await call.message.answer("⚠️ پیام به مشتری ارسال نشد (شاید ربات رو بلاک کرده).")
    await call.answer()


@dp.callback_query(F.data.startswith("paywallet:"))
async def pay_wallet(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    inv = own_invoice(no, call.from_user.id)
    if inv is None:
        await call.answer("این فاکتور دیگه معتبر نیست.", show_alert=True)
        return
    no = inv["invoice_no"]
    balance = get_balance(call.from_user.id)
    if balance < inv["total"] or not pay_with_wallet(no, call.from_user.id, inv["total"]):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"paycard:{no}")],
                nav_row(f"pay:{no}", f"invmain:{no}"),
            ]
        )
        await show(
            call,
            "❌ <b>موجودی کیف پول کافی نیست</b>\n"
            f"{LINE}\n"
            f"👛 موجودی شما: {fa(balance)} تومن\n"
            f"💰 مبلغ فاکتور: {fa(inv['total'])} تومن\n"
            f"{LINE}\n"
            "می‌تونی با کارت به کارت پرداخت کنی.",
            kb,
        )
        await call.answer()
        return
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_success(None, no, message=call.message)
    if ADMIN_ID:
        try:
            await call.bot.send_message(
                ADMIN_ID,
                f"🛎 <b>سفارش جدید (پرداخت از کیف پول)</b>\n{LINE}\n" + order_details(get_invoice(no)),
            )
        except Exception:
            pass
    await call.answer()


@dp.message(Command("addcredit"))
async def add_credit_cmd(message: Message, command: CommandObject):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (command.args or "").split()
    uid = to_int(parts[0]) if len(parts) == 2 else None
    amount = to_int(parts[1]) if len(parts) == 2 else None
    if uid is None or amount is None or amount <= 0:
        await message.answer("فرمت: /addcredit آیدی_عددی_مشتری مبلغ_به_تومن\nمثال: /addcredit 123456789 500000")
        return
    balance = add_credit(uid, amount)
    await message.answer(f"✅ {fa(amount)} تومن اعتبار اضافه شد.\n👛 موجودی جدید: {fa(balance)} تومن")
    try:
        await message.bot.send_message(uid, f"🎁 {fa(amount)} تومن اعتبار به کیف پولت اضافه شد.")
    except Exception:
        pass


# ---------------------------------------------------------------- باشگاه مشتریان
def club_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👑 ورود به کانال کلاب ویهانی", url=CLUB_URL)],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="main")],
        ]
    )


def club_welcome_text():
    return f"{CLUB_TEXT}\n\n🔗 لینک خصوصی کانال کلاب ویهانی:\n{CLUB_URL}"


def norm_contact_phone(raw):
    d = (raw or "").translate(EN_DIGITS).replace("+", "").replace(" ", "").replace("-", "")
    if d.startswith("98") and len(d) == 12:
        d = "0" + d[2:]
    return d


@dp.callback_query(F.data == "vip_club")
async def vip_club(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_member(call.from_user.id):
        await show(call, club_welcome_text(), club_kb())
        await call.answer()
        return
    await state.set_state(Club.contact)
    try:
        await call.message.delete()
    except Exception:
        pass
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=CLUB_SHARE_BTN, request_contact=True)],
            [KeyboardButton(text=CLUB_CANCEL_BTN)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await call.message.answer(
        f"💎 <b>{CLUB_BTN}</b>\n{LINE}\n"
        "برای عضویت، با دکمهٔ پایین شمارهٔ موبایل خودت رو بفرست.",
        reply_markup=kb,
    )
    await call.answer()


@dp.message(Club.contact, F.contact)
async def club_receive_contact(message: Message, state: FSMContext):
    c = message.contact
    if c.user_id != message.from_user.id:
        await message.answer("❌ فقط شمارهٔ خودت قابل قبوله. لطفاً با دکمهٔ «ارسال شماره من» بفرست.")
        return
    phone = norm_contact_phone(c.phone_number)
    if not phone:
        await message.answer("❌ شماره معتبر نبود. دوباره تلاش کن.")
        return
    save_member(message.from_user.id, phone)
    await state.clear()
    await message.answer("✅ شمارهٔ شما ثبت شد.", reply_markup=ReplyKeyboardRemove())
    await message.answer(club_welcome_text(), reply_markup=club_kb())


@dp.message(Club.contact, F.text == CLUB_CANCEL_BTN)
async def club_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("باشه 👌", reply_markup=ReplyKeyboardRemove())
    await message.answer(MAIN_TEXT, reply_markup=main_menu())


@dp.message(Club.contact)
async def club_wrong_input(message: Message):
    await message.answer("📱 لطفاً با دکمهٔ «ارسال شماره من» شمارهٔ خودت رو بفرست.")


@dp.callback_query(F.data == "campaigns")
async def campaigns(call: CallbackQuery, state: FSMContext):
    await state.clear()
    status = await club_status(call.bot, call.from_user.id)
    if status == "active":
        await show(call, CAMPAIGNS_TEXT, InlineKeyboardMarkup(inline_keyboard=[nav_row("main")]))
    elif status == "left":
        await show(call, CLUB_LEFT_TEXT + f"\n\n🔗 {CLUB_URL}", club_kb())
    elif status == "none":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=CLUB_BTN, callback_data="vip_club")],
                nav_row("main"),
            ]
        )
        await show(
            call,
            "🎉 دسترسی زودهنگام به جشنواره‌ها و کمپین‌ها مخصوص اعضای باشگاه است.\n"
            "برای عضویت، دکمهٔ زیر رو بزن.",
            kb,
        )
    else:
        await show(
            call,
            "⚠️ الان امکان بررسی عضویت نیست. کمی بعد دوباره امتحان کن.",
            InlineKeyboardMarkup(inline_keyboard=[nav_row("main")]),
        )
    await call.answer()


# ---------------------------------------------------------------- پیگیری
@dp.callback_query(F.data == "track")
async def track(call: CallbackQuery, state: FSMContext):
    await state.set_state(Order.waiting_track)
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row("main")])
    await show(
        call,
        "🔍 <b>پیگیری سفارش</b>\n"
        f"{LINE}\n"
        "شناسهٔ فاکتورت رو برام بفرست (عدد شش‌رقمی).",
        kb,
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


# ---------------------------------------------------------------- عکس خارج از مرحله فیش
@dp.message(StateFilter(None), F.photo | F.document)
async def stray_photo(message: Message):
    await message.answer(
        "⌛️ این مرحله منقضی شده. لطفاً از منوی اصلی دوباره فاکتور رو باز کن و روی «کارت به کارت» بزن.",
        reply_markup=main_menu(),
    )


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


async def keep_alive():
    # هر ۱۰ دقیقه به آدرس خود سرویس سر می‌زنه تا Render ربات رو نخوابونه
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(url) as resp:
                    await resp.read()
            except Exception:
                pass


async def main():
    await run_web()
    asyncio.create_task(keep_alive())
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
