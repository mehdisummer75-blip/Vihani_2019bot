import asyncio
import os

from aiogram import Bot, Dispatcher, F
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

MODELS = {
    "pro": ("مدل پرو", "۹۹۸ هزار تومن"),
    "promax": ("مدل پرومکس", "۱۳۹۹ هزار تومن"),
    "premium": ("مدل پرمیوم", "۱۵۹۹ هزار تومن"),
}

dp = Dispatcher()


class Order(StatesGroup):
    waiting_custom_qty = State()


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
        [InlineKeyboardButton(text=f"{name} | {price}", callback_data=f"model:{key}")]
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


def order_summary(key, qty):
    name, price = MODELS[key]
    return (
        f"✅ {name}\n"
        f"💰 قیمت: {price}\n"
        f"🔢 تعداد: {qty} عدد\n\n"
        "(مرحلهٔ بعد: تأیید سفارش و پرداخت)"
    )


MAIN_TEXT = "سلام، به ربات ما خوش اومدی 👋\nلطفاً یکی از گزینه‌های زیر رو انتخاب کن:"
MODELS_TEXT = "🛍 خرید تکی\nلطفاً مدل مورد نظرت رو انتخاب کن:"


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
    name, price = MODELS[key]
    text = f"✅ {name} | {price}\n\nچند عدد می‌خوای؟ تعداد سفارش رو انتخاب کن:"
    await call.message.edit_text(text, reply_markup=qty_menu(key))
    await call.answer()


@dp.callback_query(F.data.startswith("qty:"))
async def choose_qty(call: CallbackQuery):
    _, key, qty = call.data.split(":")
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"model:{key}")])
    await call.message.edit_text(order_summary(key, qty), reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("qty_custom:"))
async def custom_qty(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    await state.set_state(Order.waiting_custom_qty)
    await state.update_data(model=key)
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"model:{key}")])
    await call.message.edit_text(
        "✏️ تعداد دلخواهت رو به صورت عدد بفرست (مثلاً 5):", reply_markup=kb
    )
    await call.answer()


DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


@dp.message(Order.waiting_custom_qty, F.text)
async def receive_custom_qty(message: Message, state: FSMContext):
    text = message.text.strip().translate(DIGITS)
    if not text.isdigit() or int(text) < 1:
        await message.answer("لطفاً فقط یه عدد صحیح بفرست، مثلاً 5")
        return
    data = await state.get_data()
    key = data["model"]
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row(f"model:{key}")])
    await message.answer(order_summary(key, int(text)), reply_markup=kb)


@dp.callback_query(F.data.in_({"buy_bulk", "free_sample", "partnership", "faq"}))
async def placeholder(call: CallbackQuery):
    await call.answer("این بخش به‌زودی فعال می‌شه 🙏", show_alert=True)


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
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
