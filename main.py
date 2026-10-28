import telebot
from telebot import types

# مشخصات ربات و ادمین
TOKEN = "9133301747:AAHhCl_YTppqCmlUi2NKjb1ioC1gKEjEa9Q"
ADMIN_ID = 8517565749

bot = telebot.TeleBot(TOKEN)

# اطلاعات پرداخت
CARD_NUMBER = "6104337375248422"
CARD_OWNER = "Mehdi Hosseini"
BANK_NAME = "Bank Mellat"

# ساختار محصولات (خرده‌فروشی و عمده‌فروشی)
PRODUCTS = {
    "retail": {
        "tshirt": {"name": "تیشرت مینیمال باکیفیت", "price": "180,000 تومان"},
        "hoodie": {"name": "هودی لش اسپرت", "price": "350,000 تومان"},
        "pants": {"name": "شلوار کتان کلاسیک", "price": "290,000 تومان"}
    },
    "wholesale": {
        "tshirt_pack": {"name": "پک عمده تیشرت مینیمال", "price": "تخفیف عمده - هماهنگی با ادمین"},
        "hoodie_pack": {"name": "پک عمده هودی لش", "price": "تخفیف عمده - هماهنگی با ادمین"},
        "pants_pack": {"name": "پک عمده شلوار کتان", "price": "تخفیف عمده - هماهنگی با ادمین"}
    }
}

# حافظه موقت برای ذخیره سفارشات کاربران
user_orders = {}

# دکمه منوی اصلی
def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🛍️ منوی اصلی خرید"), types.KeyboardButton("🛒 سبد خرید / وضعیت سفارش"))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    user_orders[user_id] = {"items": [], "mode": None, "step": None}
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👤 خرده‌فروشی (تک‌فروش)", callback_data="mode_retail"))
    markup.add(types.InlineKeyboardButton("📦 عمده‌فروشی (پخش)", callback_data="mode_wholesale"))
    
    bot.send_message(
        message.chat.id, 
        "سلام! به فروشگاه «یاگه استایل» (Yaghe Style) خوش آمدید.\nلطفاً نوع خرید خود را انتخاب کنید:",
        reply_markup=get_main_menu()
    )
    bot.send_message(message.chat.id, "لطفاً از دکمه‌های زیر استفاده کنید:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "🛍️ منوی اصلی خرید")
def back_to_main(message):
    send_welcome(message)

@bot.message_handler(func=lambda message: message.text == "🛒 سبد خرید / وضعیت سفارش")
def show_cart(message):
    user_id = message.from_user.id
    if user_id not in user_orders or not user_orders[user_id]["items"]:
        bot.send_message(message.chat.id, "سبد خرید شما خالی است.", reply_markup=get_main_menu())
        return
    
    cart_text = "🛒 **سبد خرید شما:**\n\n"
    for item in user_orders[user_id]["items"]:
        cart_text += f"- {item['name']} (تعداد: {item['qty']})\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ نهایی کردن و پرداخت", callback_data="checkout"))
    markup.add(types.InlineKeyboardButton("🗑️ پاک کردن سبد", callback_data="clear_cart"))
    
    bot.send_message(message.chat.id, cart_text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def set_shopping_mode(call):
    user_id = call.from_user.id
    if user_id not in user_orders:
        user_orders[user_id] = {"items": [], "mode": None, "step": None}
        
    mode = call.data.split("_")[1]
    user_orders[user_id]["mode"] = mode
    
    markup = types.InlineKeyboardMarkup()
    prods = PRODUCTS[mode]
    for key, val in prods.items():
        markup.add(types.InlineKeyboardButton(val["name"], callback_data=f"prod_{key}"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"محصولات بخش {'خرده‌فروشی' if mode == 'retail' else 'عمده‌فروشی'}:\nلطفاً یک محصول را انتخاب کنید:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("prod_"))
def select_product(call):
    user_id = call.from_user.id
    prod_key = call.data.split("_", 1)[1]
    mode = user_orders.get(user_id, {}).get("mode", "retail")
    
    product = PRODUCTS[mode][prod_key]
    user_orders[user_id]["selected_product"] = product
    
    if mode == "retail":
        # خرده‌فروشی: تعداد پیش‌فرض 1 است، مستقیم می‌رویم سراغ ثبت مشخصات
        user_orders[user_id]["items"].append({"name": product["name"], "qty": 1})
        ask_shipping_info(call.message)
    else:
        # عمده‌فروشی: انتخاب تعداد (15، 30، 50، 100 یا دلخواه)
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("15 عدد", callback_data="qty_15"),
            types.InlineKeyboardButton("30 عدد", callback_data="qty_30")
        )
        markup.add(
            types.InlineKeyboardButton("50 عدد", callback_data="qty_50"),
            types.InlineKeyboardButton("100 عدد", callback_data="qty_100")
        )
        markup.add(types.InlineKeyboardButton("✏️ تعداد دلخواه (سفارشی)", callback_data="qty_custom"))
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"محصول: {product['name']}\nلطفاً تعداد عمده مورد نظر را انتخاب کنید یا تعداد دلخواه وارد نمایید:",
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("qty_"))
def select_quantity(call):
    user_id = call.from_user.id
    action = call.data.split("_")[1]
    
    if action == "custom":
        user_orders[user_id]["step"] = "waiting_for_custom_qty"
        bot.send_message(call.message.chat.id, "لطفاً تعداد مورد نظر خود را به عدد در قالب پیام ارسال کنید:")
    else:
        qty = int(action)
        product = user_orders[user_id]["selected_product"]
        user_orders[user_id]["items"].append({"name": product["name"], "qty": qty})
        ask_shipping_info(call.message)

@bot.message_handler(func=lambda message: user_orders.get(message.from_user.id, {}).get("step") == "waiting_for_custom_qty")
def get_custom_quantity(message):
    user_id = message.from_user.id
    try:
        qty = int(message.text)
        if qty <= 0:
            raise ValueError()
        
        product = user_orders[user_id]["selected_product"]
        user_orders[user_id]["items"].append({"name": product["name"], "qty": qty})
        user_orders[user_id]["step"] = None
        ask_shipping_info(message)
    except ValueError:
        bot.send_message(message.chat.id, "لطفاً فقط یک عدد معتبر وارد کنید:")

def ask_shipping_info(message):
    user_id = message.from_user.id
    user_orders[user_id]["step"] = "waiting_for_address"
    bot.send_message(
        message.chat.id,
        "محصول به سبد خرید اضافه شد. 🛒\n\nلطفاً **نام کامل، شماره تماس و آدرس دقیق پستی** خود را برای ارسال ارسال کنید:",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: user_orders.get(message.from_user.id, {}).get("step") == "waiting_for_address")
def get_shipping_address(message):
    user_id = message.from_user.id
    user_orders[user_id]["address"] = message.text
    user_orders[user_id]["step"] = None
    
    # رفتن به مرحله پرداخت
    send_payment_info(message)

def send_payment_info(message):
    pay_text = (
        f"💳 **اطلاعات پرداخت کارت به کارت**\n\n"
        f"بانک: {BANK_NAME}\n"
        f"شماره کارت: `{CARD_NUMBER}`\n"
        f"به نام: {CARD_OWNER}\n\n"
        f"لطفاً پس از واریز وجه، **تصویر رسید یا اسکرین‌شات واریزی** را همینجا برای ما بفرستید تا سفارش شما ثبت نهایی شود."
    )
    user_orders[message.from_user.id]["step"] = "waiting_for_receipt"
    bot.send_message(message.chat.id, pay_text, parse_mode="Markdown")

@bot.message_handler(content_types=['photo'], func=lambda message: user_orders.get(message.from_user.id, {}).get("step") == "waiting_for_receipt")
def get_receipt(message):
    user_id = message.from_user.id
    user_info = user_orders.get(user_id, {})
    
    # ساخت متن فاکتور بدون نمایش قیمت در فاکتور نهایی به کاربر
    invoice_text = f"🚨 **سفارش جدید ثبت شد!**\n\n"
    invoice_text += f"👤 کاربر: @{message.from_user.username or 'ندارد'} (ID: {user_id})\n"
    invoice_text += f"📍 آدرس و مشخصات:\n{user_info.get('address', 'ثبت نشده')}\n\n"
    invoice_text += "📦 **محصولات خریداری شده:**\n"
    for item in user_info.get("items", []):
        invoice_text += f"- {item['name']} | تعداد: {item['qty']}\n"
    
    # ارسال رسید و فاکتور به ادمین
    bot.send_message(ADMIN_ID, invoice_text, parse_mode="Markdown")
    bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    
    # تایید نهایی به کاربر
    bot.send_message(
        message.chat.id,
        "✅ رسید شما با موفقیت دریافت شد و برای بررسی به مدیریت ارسال گردید.\nاز خرید شما از «یاگه استایل» سپاسگزاریم! 🙏",
        reply_markup=get_main_menu()
    )
    
    # پاکسازی سبد خرید کاربر
    user_orders[user_id] = {"items": [], "mode": None, "step": None}

if __name__ == '__main__':
    print("Bot is running...")
    bot.infinity_polling()
                          
