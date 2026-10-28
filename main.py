import telebot
from telebot import types

# توکن و اطلاعات ادمین
TOKEN = "9133301747:AAHhCl_YTppqCmlUi2NKjb1ioC1gKEjEa9Q"
ADMIN_ID = "8517565749"
CHANNEL_ID = "6104337375248422"
ADMIN_NAME = "Mehdi Hosseini"
CARD_INFO = "Bank Mellat"

bot = telebot.TeleBot(TOKEN)

# ساختار محصولات (خرده‌فروشی و عمده‌فروشی)
PRODUCTS = {
    "retail": {
        "shirt_minima": {"name": "تیشرت مینیمال باکیفیت", "price": "180,000 تومان"},
        "hoodie_sport": {"name": "هودی لش اسپرت", "price": "350,000 تومان"},
        "pants_classic": {"name": "شلوار کتان کلاسیک", "price": "290,000 تومان"}
    },
    "wholesale": {
        "shirt_pack": {"name": "تخفیف عمده - هماهنگی با امین", "price": "پک عمده تیشرت مینیمال"},
        "hoodie_pack": {"name": "تخفیف عمده - هماهنگی با امین", "price": "پک عمده هودی لش"},
        "pants_pack": {"name": "تخفیف عمده - هماهنگی با امین", "price": "پک عمده شلوار کتان"}
    }
}

user_orders = {}

def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("🛍 منوی اصلی خرید"),
        types.KeyboardButton("🛒 سبد خرید / وضعیت سفارش")
    )
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    if user_id not in user_orders:
        user_orders[user_id] = {"items": [], "type": None}
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🛒 خرده‌فروشی", callback_data="type_retail"),
        types.InlineKeyboardButton("📦 عمده‌فروشی", callback_data="type_wholesale")
    )
    bot.send_message(
        message.chat.id, 
        "سلام! به فروشگاه «Vihani» خوش آمدید.\nلطفاً نوع خرید خود را انتخاب کنید:", 
        reply_markup=markup
    )

@bot.message_handler(func=lambda message: message.text == "🛍 منوی اصلی خرید")
def main_menu_handler(message):
    send_welcome(message)

@bot.message_handler(func=lambda message: message.text == "🛒 سبد خرید / وضعیت سفارش")
def show_cart(message):
    user_id = message.from_user.id
    if user_id not in user_orders or not user_orders[user_id]["items"]:
        bot.send_message(message.chat.id, "🛒 سبد خرید شما خالی است.", reply_markup=get_main_menu())
        return
    
    text = "🛒 **سبد خرید شما:**\n\n"
    for item in user_orders[user_id]["items"]:
        text += f"- {item['name']} (تعداد: {item['qty']})\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ ثبت نهایی سفارش", callback_data="checkout"))
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("type_"))
def select_type(call):
    user_id = call.from_user.id
    if user_id not in user_orders:
        user_orders[user_id] = {"items": [], "type": None}
        
    buying_type = call.data.split("_")[1]
    user_orders[user_id]["type"] = buying_type
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    if buying_type == "retail":
        for key, val in PRODUCTS["retail"].items():
            markup.add(types.InlineKeyboardButton(f"{val['name']} - {val['price']}", callback_data=f"add_{key}"))
        bot.edit_message_text("محصولات بخش خرده‌فروشی:\nلطفاً یک محصول را انتخاب کنید:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    else:
        for key, val in PRODUCTS["wholesale"].items():
            markup.add(types.InlineKeyboardButton(f"{val['name']}", callback_data=f"add_{key}"))
        bot.edit_message_text("محصولات بخش عمده‌فروشی:\nلطفاً پک عمده را انتخاب کنید:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_"))
def add_to_cart(call):
    user_id = call.from_user.id
    product_key = call.data.split("_", 1)[1]
    
    # پیدا کردن محصول در خرده یا عمده
    product = PRODUCTS["retail"].get(product_key) or PRODUCTS["wholesale"].get(product_key)
    if not product:
        return
        
    if user_id not in user_orders:
        user_orders[user_id] = {"items": [], "type": "retail"}
        
    user_orders[user_id]["items"].append({"name": product["name"], "qty": 1})
    bot.answer_callback_query(call.id, "✅ به سبد خرید اضافه شد!")
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🛒 مشاهده سبد خرید", callback_data="view_cart"),
        types.InlineKeyboardButton("🛍 ادامه خرید", callback_data=f"type_{user_orders[user_id]['type']}")
    )
    bot.send_message(call.message.chat.id, f"محصول «{product['name']}» به سبد خرید اضافه شد.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def view_cart_callback(call):
    user_id = call.from_user.id
    if user_id not in user_orders or not user_orders[user_id]["items"]:
        bot.send_message(call.message.chat.id, "🛒 سبد خرید شما خالی است.", reply_markup=get_main_menu())
        return
    
    text = "🛒 **سبد خرید شما:**\n\n"
    for item in user_orders[user_id]["items"]:
        text += f"- {item['name']} (تعداد: {item['qty']})\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ ثبت نهایی سفارش", callback_data="checkout"))
    bot.send_message(call.message.chat.id, text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "checkout")
def checkout(call):
    user_id = call.from_user.id
    bot.send_message(call.message.chat.id, "لطفاً نام و نام خانوادگی خود را ارسال کنید:")
    bot.register_next_step_handler(call.message, get_name)

def get_name(message):
    user_id = message.from_user.id
    user_orders[user_id]["name"] = message.text
    bot.send_message(message.chat.id, "لطفاً شماره تماس خود را ارسال کنید:")
    bot.register_next_step_handler(message, get_phone)

def get_phone(message):
    user_id = message.from_user.id
    user_orders[user_id]["phone"] = message.text
    bot.send_message(message.chat.id, "لطفاً آدرس دقیق پستی خود را ارسال کنید:")
    bot.register_next_step_handler(message, get_address)

def get_address(message):
    user_id = message.from_user.id
    user_orders[user_id]["address"] = message.text
    
    order = user_orders[user_id]
    summary = f"📋 **فاکتور نهایی سفارش:**\n\n"
    summary += f"نام: {order['name']}\nتلفن: {order['phone']}\nآدرس: {order['address']}\n\nمحصولات:\n"
    for item in order["items"]:
        summary += f"- {item['name']}\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 پرداخت و ثبت نهایی", callback_data="final_pay"))
    bot.send_message(message.chat.id, summary, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "final_pay")
def final_pay(call):
    user_id = call.from_user.id
    bot.send_message(call.message.chat.id, f"لطفاً مبلغ را به کارت {CARD_INFO} واریز کرده و تصویر فیش واریزی را همینجا بفرستید.")

if __name__ == "__main__":
    print("Bot is running...")
    bot.infinity_polling()
    
