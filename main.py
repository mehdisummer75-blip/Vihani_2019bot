import asyncio
import contextvars
import functools
import hashlib
import json
import logging
import os
import queue
import random
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import unquote, urlparse

import aiohttp
import pg8000.dbapi
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.fsm.storage.base import BaseStorage
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiohttp import web

try:
    from aiogram.types import CopyTextButton
except ImportError:  # aiogram قدیمی
    CopyTextButton = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

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
    # خرید عمده (تخفیف باشگاه شامل این‌ها نمی‌شه)
    "bulk_pro": ("مدل پرو (عمده)", 699),
    "bulk_promax": ("مدل پرومکس (عمده)", 999),
    "bulk_premium": ("مدل پرمیوم (عمده)", 1099),
}


BULK_MIN_QTY = 15


def is_bulk(key):
    return str(key).startswith("bulk_")


# عکس مدل‌ها: پوشهٔ models کنار main.py → pro.png / promax.png / premium.png (jpg و webp هم ok)
# مدل‌های عمده از عکس همون مدل تکی استفاده می‌کنن. اگه عکسی نبود، ربات فقط متن می‌فرسته.
MODEL_PHOTO_DIR = "models"
_photo_ids = {}  # کش file_id تلگرام تا هر بار آپلود نشه


def model_photo(key):
    base = str(key).removeprefix("bulk_")
    for ext in ("jpg", "jpeg", "png", "webp"):
        path = os.path.join(MODEL_PHOTO_DIR, f"{base}.{ext}")
        if os.path.exists(path):
            return path
    return None


_photo_bytes = {}  # عکس‌های بهینه‌شده (فقط یک بار ساخته می‌شن)


def _prepare_photo(path):
    """عکس رو برای تلگرام بهینه می‌کنه: حداکثر ۱۲۸۰ پیکسل، JPEG. اگه Pillow نبود، فایل اصلی."""
    if path in _photo_bytes:
        return _photo_bytes[path]
    try:
        import io
        from PIL import Image

        img = Image.open(path)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88, optimize=True)
        data = buf.getvalue()
    except Exception:
        log.exception("photo prepare failed (%s); using original file", path)
        data = None
    _photo_bytes[path] = data
    return data


def _photo_input(path):
    if path in _photo_ids:
        return _photo_ids[path]
    data = _prepare_photo(path)
    if data:
        return BufferedInputFile(data, filename=os.path.splitext(os.path.basename(path))[0] + ".jpg")
    return FSInputFile(path)


def _remember_photo(path, msg):
    if msg is not None and msg.photo:
        _photo_ids[path] = msg.photo[-1].file_id

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
    "\n💡 اگه نیمه‌کاره موند، از «فاکتورهای من» تو منوی اصلی "
    "می‌تونی بعداً ادامه‌اش بدی."
)

class PgStorage(BaseStorage):
    """مرحلهٔ مشتری رو توی دیتابیس نگه می‌داره تا با ری‌استارت/دیپلوی ربات گم نشه."""

    @staticmethod
    def _k(key):
        return ":".join(
            str(getattr(key, a, "") or "")
            for a in ("bot_id", "chat_id", "user_id", "thread_id", "destiny")
        )

    async def set_state(self, key, state=None):
        st = getattr(state, "state", state)
        (await run_db(q_exec, 
            "INSERT INTO fsm (k, state) VALUES (%s, %s)"
            " ON CONFLICT (k) DO UPDATE SET state = EXCLUDED.state",
            (self._k(key), st),
        ))

    async def get_state(self, key):
        row = (await run_db(q_one, "SELECT state FROM fsm WHERE k = %s", (self._k(key),)))
        return row["state"] if row else None

    async def set_data(self, key, data):
        (await run_db(q_exec, 
            "INSERT INTO fsm (k, data) VALUES (%s, %s)"
            " ON CONFLICT (k) DO UPDATE SET data = EXCLUDED.data",
            (self._k(key), json.dumps(data, ensure_ascii=False)),
        ))

    async def get_data(self, key):
        row = (await run_db(q_one, "SELECT data FROM fsm WHERE k = %s", (self._k(key),)))
        return json.loads(row["data"]) if row and row["data"] else {}

    async def close(self):
        pass


dp = Dispatcher(storage=PgStorage())


# ---------------------------------------------------------------- پاک‌سازی خودکار چت
# فقط «فاکتور موفق» توی چت مشتری می‌مونه؛ بقیهٔ پیام‌های ربات و پیام‌های خود مشتری پاک می‌شن.
KEEP = contextvars.ContextVar("keep", default=False)  # پیامی که نباید پاک بشه
TRANSIENT = contextvars.ContextVar("transient", default=False)  # پیام خطا/موقت
NEW = contextvars.ContextVar("new", default=None)
TRACK = {}  # chat_id -> [[message_id, kind]]  (kind: s=صفحه، t=موقت)
_bg = set()


@contextmanager
def keeping():
    tok = KEEP.set(True)
    try:
        yield
    finally:
        KEEP.reset(tok)


async def err(message, text, **kw):
    """پیام خطای موقت: با اولین حرکت بعدی مشتری پاک می‌شه، ولی صفحهٔ اصلی سر جاش می‌مونه."""
    tok = TRANSIENT.set(True)
    try:
        return await message.answer(text, **kw)
    finally:
        TRANSIENT.reset(tok)


# ---------------------------------------------------------------- دکمه‌های رنگی
# آبی = primary ، سبز = success ، قرمز = danger  (بقیه همون خاکستری می‌مونن)
BACK_WORDS = ("بازگشت", "مرحله قبل", "منوی اصلی", "بستن لیست")
GREEN_CB = ("paycard:", "paywallet:", "wcard:", "wsend:", "invsend:", "wallet_add_pay")


def pick_style(text, cb):
    t, cb = text or "", cb or ""
    # قرمز: همهٔ دکمه‌های بازگشت / مرحله قبل / منوی اصلی + باشگاه VIP
    if cb == "main" or cb.startswith("invmain:") or t.startswith("🔙") or any(w in t for w in BACK_WORDS):
        return "danger"
    if cb == "vip_club":
        return "danger"
    # سبز: پشتیبانی + همهٔ گزینه‌های بخش پرداخت
    if "پشتیبانی" in t or cb.startswith(GREEN_CB) or t.startswith("📋 کپی"):
        return "success"
    # آبی: سوالات متداول
    if cb == "faq":
        return "primary"
    return None  # بقیه بی‌رنگ


def paint(markup):
    """به همهٔ دکمه‌های شیشه‌ای قبل از ارسال، رنگ می‌ده."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    # منوی اصلی کاملاً بی‌رنگ می‌مونه (تشخیص: دکمهٔ cod_home فقط توی منوی اصلی هست)
    if any(b.callback_data == "cod_home" for r in markup.inline_keyboard for b in r):
        return markup
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            st = None if getattr(b, "style", None) else pick_style(b.text, b.callback_data)
            new_row.append(b.model_copy(update={"style": st}) if st else b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TrackRequests(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        mk = getattr(method, "reply_markup", None)
        if mk is not None:
            try:
                method.reply_markup = paint(mk)
            except Exception:
                log.exception("paint failed")
        res = await make_request(bot, method)
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            new = NEW.get()
            mid = getattr(method, "message_id", None)
            cid = getattr(method, "chat_id", None)
            if new is not None and mid and isinstance(cid, int):
                new.append((cid, mid, "e"))
            return res
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)) and getattr(res, "message_id", None):
            chat = getattr(res, "chat", None)
            if chat is not None and chat.type == "private":
                kind = "k" if KEEP.get() else ("t" if TRANSIENT.get() else "s")
                if kind != "k":
                    TRACK.setdefault(chat.id, []).append([res.message_id, kind])
                else:
                    try:
                        (await run_db(q_exec, 
                            "INSERT INTO kept_msgs (chat_id, msg_id) VALUES (%s, %s)"
                            " ON CONFLICT DO NOTHING",
                            (chat.id, res.message_id),
                        ))
                    except Exception:
                        log.exception("kept_msgs insert failed")
                new = NEW.get()
                if new is not None:
                    new.append((chat.id, res.message_id, kind))
        return res


async def _del(bot, chat_id, ids):
    async def one(i):
        try:
            await bot.delete_message(chat_id, i)
        except Exception:
            pass

    await asyncio.gather(*(one(i) for i in ids))


def _bg_delete(bot, chat_id, ids):
    if not ids:
        return
    t = asyncio.create_task(_del(bot, chat_id, ids))
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def purge(bot, chat_id):
    """همهٔ پیام‌های ردگیری‌شدهٔ ربات توی این چت رو پاک می‌کنه."""
    items = TRACK.pop(chat_id, [])
    await _del(bot, chat_id, [i for i, _ in items])


async def sweep(bot, chat_id, around_id, exclude_id=None):
    """پیام‌های مونده (مثلاً بعد از ری‌استارت) دور و بر این پیام رو پاک می‌کنه؛
    فقط فاکتورهای ثبت‌شده و پیام جاری سر جاشون می‌مونن. باید قبل از فرستادن پیام جدید صدا زده بشه."""
    if not chat_id or chat_id == ADMIN_ID:
        return
    lo, hi = max(1, around_id - 90), around_id + 9
    try:
        rows = (await run_db(q_all, 
            "SELECT msg_id FROM kept_msgs WHERE chat_id = %s AND msg_id BETWEEN %s AND %s",
            (chat_id, lo, hi),
        ))
        protected = {r["msg_id"] for r in rows}
        protected.add(exclude_id)
        ids = [i for i in range(lo, hi + 1) if i not in protected]
        if ids:
            await bot.delete_messages(chat_id, ids)
    except Exception:
        log.exception("sweep failed")


async def sweep_before(bot, chat_id, anchor_id, span=40):
    """همهٔ پیام‌های قدیمی‌تر از anchor (به‌جز فاکتورهای ثبت‌شده) رو پاک می‌کنه.
    به حافظه وابسته نیست، پس بعد از ری‌استارت هم کار می‌کنه."""
    lo, hi = max(1, anchor_id - span), anchor_id - 1
    if hi < lo:
        return
    try:
        rows = (await run_db(q_all, 
            "SELECT msg_id FROM kept_msgs WHERE chat_id = %s AND msg_id BETWEEN %s AND %s",
            (chat_id, lo, hi),
        ))
        protected = {r["msg_id"] for r in rows}
        ids = [i for i in range(lo, hi + 1) if i not in protected]
        if ids:
            await bot.delete_messages(chat_id, ids)
    except Exception:
        log.exception("sweep_before failed")


def _bg_sweep(bot, chat_id, anchor_id):
    t = asyncio.create_task(sweep_before(bot, chat_id, anchor_id))
    _bg.add(t)
    t.add_done_callback(_bg.discard)


LOCKS = {}  # chat_id -> asyncio.Lock  (هر چت فقط یک آپدیت همزمان)


def _chat_lock(chat_id):
    lk = LOCKS.get(chat_id)
    if lk is None:
        lk = LOCKS[chat_id] = asyncio.Lock()
    return lk


class GroupGuard(BaseMiddleware):
    """ربات توی گروه‌ها فقط به دکمه‌های آماده‌سازی پرسنل جواب می‌ده؛ بقیه چیز رو نادیده می‌گیره."""

    STAFF_PREFIXES = ("pstart", "pdone", "prel")

    async def __call__(self, handler, event, data):
        is_cb = isinstance(event, CallbackQuery)
        msg = event.message if is_cb else event
        chat = getattr(msg, "chat", None)
        if chat is None or chat.type == "private":
            return await handler(event, data)
        if is_cb and (event.data or "").split(":")[0] in self.STAFF_PREFIXES:
            return await handler(event, data)
        # گروه پرسنل: فقط دستورهای گزارش مجازه (و دکمهٔ یادآوری که خودش چک می‌کنه ادمینه)
        if (ORDERS_CHAT_ID and chat.id == ORDERS_CHAT_ID) or (READY_CHAT_ID and chat.id == READY_CHAT_ID):
            if is_cb and (event.data or "").startswith("remind:"):
                return await handler(event, data)
            if not is_cb and (event.text or "").startswith("/"):
                cmd = (event.text.split(maxsplit=1) or [""])[0].lstrip("/").split("@")[0].lower()
                if cmd in STAFF_CMDS:
                    return await handler(event, data)
        if (
            not is_cb
            and ADMIN_ID
            and getattr(event.from_user, "id", None) == ADMIN_ID
            and (event.text or "").startswith(("/groupid", "/staffreport"))
        ):
            return await handler(event, data)
        if is_cb:
            try:
                await event.answer()
            except Exception:
                pass
        return None


class Clean(BaseMiddleware):
    async def __call__(self, handler, event, data):
        is_cb = isinstance(event, CallbackQuery)
        msg = event.message if is_cb else event
        if msg is None or msg.chat.type != "private":
            return await handler(event, data)
        chat = msg.chat.id
        bot = data.get("bot") or event.bot
        cb_id = msg.message_id if is_cb else None
        # آپدیت‌های یک چت پشت سر هم اجرا بشن؛ وگرنه کلیک سریع/عوض‌کردن مسیر باعث می‌شه
        # صفحهٔ یکی از دو هندلر از لیست پاک‌سازی جا بمونه.
        async with _chat_lock(chat):
            token = NEW.set([])
            try:
                result = await handler(event, data)
                new = [n for n in NEW.get() if n[0] == chat]
            finally:
                NEW.reset(token)

            new_ids = {n[1] for n in new}
            sent_screen = any(n[2] in ("s", "k") for n in new)
            edited_screen = any(n[2] == "e" for n in new)
            has_screen = sent_screen or edited_screen

            # لیست «الان» رو می‌گیریم (نه قبل از هندلر) تا چیزی جا نمونه
            current = [(i, k) for i, k in TRACK.get(chat, [])]
            to_del = [
                i for i, k in current
                if i not in new_ids and i != cb_id and (has_screen or k == "t")
            ]
            if to_del:
                gone = set(to_del)
                TRACK[chat] = [e for e in TRACK.get(chat, []) if e[0] not in gone]
                _bg_delete(bot, chat, to_del)

            # جارو: هر چیز مونده (قبل و بعد از پیام جاری) به‌جز فاکتورها و صفحهٔ فعلی
            if chat != ADMIN_ID and has_screen:
                keep_now = set(new_ids)
                if cb_id is not None:
                    keep_now.add(cb_id)
                top = max([i for i, _ in TRACK.get(chat, [])] + list(keep_now) + [event.message_id if not is_cb else cb_id])
                await sweep_range(bot, chat, top, keep_now)

            if is_cb:  # پیامی که ویرایش شد الان «صفحهٔ فعلی»ه
                if edited_screen or sent_screen:
                    for e in TRACK.get(chat, []):
                        if e[0] == cb_id:
                            e[1] = "s"
            else:
                _bg_delete(bot, chat, [event.message_id])
            return result


async def sweep_range(bot, chat_id, top_id, keep, span=100):
    """همهٔ پیام‌های بین top-span و top (به‌جز فاکتورهای ثبت‌شده و keep) رو پاک می‌کنه."""
    lo, hi = max(1, top_id - span), top_id
    try:
        rows = (await run_db(q_all, 
            "SELECT msg_id FROM kept_msgs WHERE chat_id = %s AND msg_id BETWEEN %s AND %s",
            (chat_id, lo, hi),
        ))
        protected = {r["msg_id"] for r in rows} | set(keep)
        ids = [i for i in range(lo, hi + 1) if i not in protected]
        for n in range(0, len(ids), 100):
            await bot.delete_messages(chat_id, ids[n:n + 100])
    except Exception:
        log.exception("sweep_range failed")


# ---------------------------------------------------------------- دیتابیس (Postgres روی Neon)
CUSTOMER_FIELDS = ("full_name", "phone", "landline", "address", "postal", "emergency")
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


# ----- لایهٔ اتصال: خواندن‌ها هم‌زمان روی چند اتصال، نوشتن‌ها پشت یک قفل و روی یک اتصال -----
# نوشتن‌ها مثل قبل پشت‌سرهم انجام می‌شن (رفتار تراکنش‌ها عوض نمی‌شه)؛
# فقط خواندن‌های ساده (SELECT) می‌تونن هم‌زمان باشن.
DB_READ_CONNS = 4
_wconn = None
_wlock = threading.RLock()
_read_pool = queue.LifoQueue()
_read_created = 0
_read_create_lock = threading.Lock()
DB_EXEC = ThreadPoolExecutor(max_workers=DB_READ_CONNS + 2, thread_name_prefix="db")


async def run_db(fn, *args, **kwargs):
    """تابع دیتابیسی (sync) رو توی یک thread اجرا می‌کنه تا حلقهٔ اصلی ربات منتظر دیتابیس نمونه."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(DB_EXEC, functools.partial(fn, *args, **kwargs))


def _close_quiet(c):
    try:
        c.close()
    except Exception:
        pass


def tx(work):
    """work(cur) رو داخل یک تراکنش نوشتنی اجرا می‌کنه (پشت قفل). اگه اتصال قطع شده بود، یک بار دوباره وصل می‌شه."""
    global _wconn
    with _wlock:
        for attempt in (1, 2):
            try:
                if _wconn is None:
                    _wconn = _connect()
                cur = _wconn.cursor()
                result = work(cur)
                _wconn.commit()
                return result
            except Exception:
                if _wconn is not None:
                    _close_quiet(_wconn)
                _wconn = None
                if attempt == 2:
                    raise


def _read_tx(work):
    """تراکنش فقط‌خواندنی روی یکی از اتصال‌های استخر؛ هم‌زمان با بقیهٔ خواندن‌ها."""
    global _read_created
    conn = None
    try:
        conn = _read_pool.get_nowait()
    except queue.Empty:
        with _read_create_lock:
            if _read_created < DB_READ_CONNS:
                _read_created += 1
                try:
                    conn = _connect()
                except Exception:
                    _read_created -= 1
                    raise
        if conn is None:
            conn = _read_pool.get(timeout=30)
    for attempt in (1, 2):
        try:
            cur = conn.cursor()
            result = work(cur)
            conn.commit()
            _read_pool.put(conn)
            return result
        except Exception:
            _close_quiet(conn)
            if attempt == 2:
                with _read_create_lock:
                    _read_created -= 1
                raise
            conn = _connect()


def _is_read_sql(sql):
    u = sql.lstrip().upper()
    return u.startswith("SELECT") and "FOR UPDATE" not in u and "NEXTVAL" not in u


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

    return _read_tx(work) if _is_read_sql(sql) else tx(work)


def q_all(sql, params=()):
    def work(cur):
        cur.execute(sql, params)
        return _rows(cur)

    return _read_tx(work) if _is_read_sql(sql) else tx(work)


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
        "CREATE TABLE IF NOT EXISTS users (\n"
        "user_id BIGINT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now())"
    )
    q_exec(
        "CREATE TABLE IF NOT EXISTS topups (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL,"
        " amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TIMESTAMPTZ DEFAULT now())"
    )
    q_exec(
        "CREATE TABLE IF NOT EXISTS kept_msgs ("
        "chat_id BIGINT NOT NULL, msg_id BIGINT NOT NULL, PRIMARY KEY (chat_id, msg_id))"
    )
    q_exec(
        "CREATE TABLE IF NOT EXISTS fsm ("
        "k TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT '{}')"
    )
    q_exec(
        "CREATE TABLE IF NOT EXISTS club_members ("
        "user_id BIGINT PRIMARY KEY, phone TEXT NOT NULL, joined_at TIMESTAMPTZ DEFAULT now())"
    )
    q_exec(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS discount_percent BIGINT NOT NULL DEFAULT 0"
    )
    q_exec(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS reached_payment BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # شمارندهٔ ترتیب ثبت: فاکتور جدیدتر همیشه seq بزرگ‌تر داره (برای مرتب‌سازی مطمئن)
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS seq BIGSERIAL")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS landline TEXT")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS printed BOOLEAN NOT NULL DEFAULT FALSE")
    # وضعیت آماده‌سازی: new -> preparing -> done
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS prep_status TEXT NOT NULL DEFAULT 'new'")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS prep_by BIGINT")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS prep_by_name TEXT")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS prep_done_at TIMESTAMPTZ")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS prep_started_at TIMESTAMPTZ")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pay_amount BIGINT")
    q_exec(
        "CREATE TABLE IF NOT EXISTS bank_sms ("
        " id SERIAL PRIMARY KEY, body_hash TEXT, amount BIGINT, invoice_no BIGINT,"
        " received_at TIMESTAMPTZ DEFAULT now())"
    )
    # موجودی انبار: هر مدل یک ردیف. ردیف نداشتن = بدون محدودیت
    q_exec("CREATE TABLE IF NOT EXISTS stock (model TEXT PRIMARY KEY, qty BIGINT NOT NULL DEFAULT 0)")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS stock_at TIMESTAMPTZ")
    # برای گزارش درآمد: زمان پرداخت، روش پرداخت، و آخرین یادآوری فاکتور نیمه‌تمام
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pay_method TEXT")
    q_exec("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ")
    q_exec(
        "CREATE OR REPLACE FUNCTION trg_set_paid_at() RETURNS trigger AS $$ "
        "BEGIN "
        "IF NEW.status = 'paid' AND OLD.status IS DISTINCT FROM 'paid' THEN NEW.paid_at := now(); END IF; "
        "RETURN NEW; "
        "END; $$ LANGUAGE plpgsql"
    )
    q_exec("DROP TRIGGER IF EXISTS trg_paid_at ON invoices")
    q_exec(
        "CREATE TRIGGER trg_paid_at BEFORE UPDATE OF status ON invoices "
        "FOR EACH ROW EXECUTE FUNCTION trg_set_paid_at()"
    )
    # ایندکس‌ها: کوئری‌های پرتکرار (فاکتورهای کاربر، بررسی مهلت، گزارش‌ها، تطبیق پیامک بانک) رو سریع می‌کنن.
    # اگه یکی‌شون ساخته نشد، ربات بالا میاد و فقط همون یکی بی‌ایندکس می‌مونه.
    for _idx in (
        "CREATE INDEX IF NOT EXISTS idx_invoices_user_seq ON invoices (user_id, seq DESC)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_status_created ON invoices (status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_paid_at ON invoices (paid_at) WHERE status = 'paid'",
        "CREATE INDEX IF NOT EXISTS idx_invoices_prep_done ON invoices (prep_done_at) WHERE prep_status = 'done'",
        "CREATE INDEX IF NOT EXISTS idx_invoices_pay_amount ON invoices (pay_amount) WHERE pay_amount IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_invoices_stock ON invoices (status, stock_at) WHERE stock_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_bank_sms_invoice ON bank_sms (invoice_no)",
        "CREATE INDEX IF NOT EXISTS idx_bank_sms_hash ON bank_sms (body_hash, received_at)",
        "CREATE INDEX IF NOT EXISTS idx_topups_status_created ON topups (status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_topups_user ON topups (user_id)",
    ):
        try:
            q_exec(_idx)
        except Exception:
            log.exception("index creation failed: %s", _idx)
    # فاکتورهای پرداخت‌شدهٔ قدیمی (قبل از این آپدیت) زمان پرداختشون تقریباً = زمان ساخت فاکتور
    q_exec("UPDATE invoices SET paid_at = created_at WHERE status = 'paid' AND paid_at IS NULL")


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
    # همیشه فقط آخرین فاکتور فعاله؛ فاکتورهای نیمه‌تمام قبلی لغو می‌شن (توی «فاکتورهای من» هستن)
    q_exec(
        "UPDATE invoices SET status = 'cancelled'"
        " WHERE user_id = %s AND status IN ('pending', 'awaiting_receipt')",
        (user_id,),
    )
    for _ in range(50):
        no = random.randint(100000, 999999)
        added = q_exec(
            "INSERT INTO invoices"
            " (invoice_no, user_id, model, qty, unit_price, total, status, discount_percent, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp()) ON CONFLICT (invoice_no) DO NOTHING",
            (no, user_id, key, qty, unit, total, "pending", discount),
        )
        if added == 1:
            return no
    raise RuntimeError("could not create invoice")


PAY_SURCHARGE_MIN = 100  # تومن
PAY_SURCHARGE_MAX = 10000
PAY_SURCHARGE_STEP = 50


PAY_AMOUNT_LOCK = 7002


def assign_pay_amount(inv):
    """مبلغ یکتای واریز کارت‌به‌کارت: مبلغ فاکتور + ۱۰۰، ۱۵۰، ۲۰۰ ... تومن.
    تا وقتی فاکتور بازه، هیچ فاکتور باز دیگه‌ای همین مبلغ رو نداره. اگه قبلاً گرفته باشه، همون می‌مونه.
    کل کار توی یک تراکنش با قفل انجام می‌شه تا دو مشتری هم‌زمان مبلغ یکسان نگیرن."""
    no, total = inv["invoice_no"], int(inv["total"])

    def work(cur):
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (PAY_AMOUNT_LOCK,))
        cur.execute(
            "SELECT pay_amount FROM invoices WHERE invoice_no <> %s AND pay_amount IS NOT NULL"
            " AND status IN ('awaiting_receipt', 'reviewing')"
            " AND created_at > now() - interval '48 hours'",
            (no,),
        )
        used = {int(r[0]) for r in cur.fetchall()}
        cur_amt = inv.get("pay_amount")
        if cur_amt:
            cur_amt = int(cur_amt)
            extra = cur_amt - total
            if (
                PAY_SURCHARGE_MIN <= extra <= PAY_SURCHARGE_MAX
                and extra % PAY_SURCHARGE_STEP == 0
                and cur_amt not in used
            ):
                return cur_amt
        for extra in range(PAY_SURCHARGE_MIN, PAY_SURCHARGE_MAX + 1, PAY_SURCHARGE_STEP):
            if total + extra not in used:
                cur.execute("UPDATE invoices SET pay_amount = %s WHERE invoice_no = %s", (total + extra, no))
                return total + extra
        return total  # خیلی بعید: همه مبلغ‌ها پر بودن

    return tx(work)


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
    """فاکتور همین کاربر رو برمی‌گردونه (دیگه لغو نمی‌کنه؛ فاکتور نیمه‌کاره می‌مونه تا بعداً تکمیل بشه)."""
    inv = get_invoice(no)
    if inv is None or inv["user_id"] != user_id:
        return None
    return inv


# فاکتور «نصفه‌مونده» = آخرین فاکتور مشتری، اگه تا صفحهٔ پرداخت رفته و تموم نشده (۱۴ روز اخیر)
OPEN_SQL = (
    "status IN ('pending', 'awaiting_receipt')"
    " AND (reached_payment OR status = 'awaiting_receipt')"
    " AND created_at > now() - interval '14 days'"
)


def mark_payment(no):
    q_exec("UPDATE invoices SET reached_payment = TRUE WHERE invoice_no = %s", (no,))


def latest_open_invoice(user_id):
    """شمارهٔ آخرین فاکتور مشتری، فقط اگه نیمه‌تمام باشه؛ وگرنه None."""
    row = q_one(
        "SELECT invoice_no, (" + OPEN_SQL + ") AS is_open FROM invoices"
        " WHERE user_id = %s ORDER BY created_at DESC, seq DESC LIMIT 1",
        (user_id,),
    )
    return row["invoice_no"] if row and row["is_open"] else None


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


# ---------------------------------------------------------------- موجودی انبار
# موجودی هر مدل = (عددی که ادمین داده) منهای فاکتورهایی که ازش برداشتن:
#   پرداخت‌شده / در حال بررسی فیش  → همیشه کم می‌شن
#   منتظر فیش (کارت به کارت)        → فقط PAY_DEADLINE_MIN دقیقه رزرو می‌مونن
# فاکتور لغو یا ردشده خودکار آزاد می‌شه. مدل‌های عمده از موجودی همون مدل تکی کم می‌کنن.
STOCK_MODELS = ("pro", "promax", "premium")
# مهلت واریز و ارسال فیش (دقیقه). بعد از ارسال فیش دیگه هیچ مهلتی نیست و فاکتور تا تأیید می‌مونه.
PAY_DEADLINE_MIN = 10
STOCK_LOCK = 7001

_USED_SQL = (
    "SELECT COALESCE(SUM(qty), 0) FROM invoices"
    " WHERE REPLACE(model, 'bulk_', '') = %s AND invoice_no <> %s AND stock_at IS NOT NULL"
    " AND (status IN ('reviewing', 'paid')"
    f" OR (status = 'awaiting_receipt' AND stock_at > now() - interval '{int(PAY_DEADLINE_MIN)} minutes'))"
)


def stock_base(key):
    return str(key).removeprefix("bulk_")


def _stock_row(cur, base):
    cur.execute("SELECT qty FROM stock WHERE model = %s", (base,))
    r = cur.fetchone()
    return None if r is None else int(r[0])


def _stock_used(cur, base, exclude=0):
    cur.execute(_USED_SQL, (base, exclude))
    return int(cur.fetchone()[0])


def _stock_left(cur, base, exclude=0):
    qty = _stock_row(cur, base)
    if qty is None:
        return None
    return max(0, qty - _stock_used(cur, base, exclude))


def stock_available(key, exclude=0):
    """None = برای این مدل محدودیتی ست نشده؛ وگرنه موجودی فعلی (۰ یا بیشتر)."""
    return tx(lambda cur: _stock_left(cur, stock_base(key), exclude))


def _reserve_in_tx(cur, no):
    """اگه موجودی کافی باشه برای این فاکتور رزروش می‌کنه (داخل تراکنش، با قفل)."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (STOCK_LOCK,))
    cur.execute("SELECT model, qty FROM invoices WHERE invoice_no = %s", (no,))
    r = cur.fetchone()
    if r is None:
        return False
    left = _stock_left(cur, stock_base(r[0]), no)
    if left is not None and left < int(r[1]):
        return False
    cur.execute("UPDATE invoices SET stock_at = now() WHERE invoice_no = %s", (no,))
    return True


def reserve_stock(no):
    return tx(lambda cur: _reserve_in_tx(cur, no))


def stock_short_note(asked, avail):
    return (
        "⚠️ <b>موجودی کافی نیست</b>\n"
        f"تعداد درخواستی شما {fa(asked)} عدد بود، ولی تعداد موجودی فعلی {fa(avail)} عدد می‌باشد؛ "
        f"برای همین فاکتور برای {fa(avail)} عدد صادر شد.\n\n"
        "🙏 واقعاً متأسفم بابت این موضوع، ولی به‌زودی بار جدید شارژ می‌شه و از طریق اینستاگرام و کلاب ویهانی اطلاع‌رسانی می‌شه.\n"
        f"{LINE}\n"
    )


def soldout_text(key, avail=0):
    name = MODELS[key][0]
    if is_bulk(key) and avail:
        body = (
            f"موجودی فعلی {fa(avail)} عدد است و حداقل سفارش عمده {fa(BULK_MIN_QTY)} عدد می‌باشد.\n"
            "می‌تونی از «خرید تکی» تا سقف موجودی سفارش بدی."
        )
    else:
        body = "موجودی این مدل فعلاً تموم شده."
    return (
        f"😔 <b>{name}</b>\n{LINE}\n{body}\n\n"
        "🙏 واقعاً متأسفم بابت این موضوع، ولی به‌زودی بار جدید شارژ می‌شه و از طریق اینستاگرام و کلاب ویهانی اطلاع‌رسانی می‌شه."
    )


def soldout_kb(key):
    return InlineKeyboardMarkup(inline_keyboard=[nav_row("buy_bulk" if is_bulk(key) else "buy_single")])


def stock_fit(no):
    """فاکتور رو با موجودی هماهنگ می‌کنه. برمی‌گردونه (inv, note):
    inv=None یعنی موجودی برای این فاکتور نیست (فاکتور لغو شد)؛ note پر یعنی تعداد فاکتور کم شد."""
    inv = get_invoice(no)
    avail = stock_available(inv["model"], exclude=no)
    if avail is None or avail >= inv["qty"]:
        return inv, ""
    if avail < (BULK_MIN_QTY if is_bulk(inv["model"]) else 1):
        set_status(no, "cancelled")
        return None, ""
    asked = inv["qty"]
    total = inv["unit_price"] * avail * (100 - (inv.get("discount_percent") or 0)) // 100
    q_exec(
        "UPDATE invoices SET qty = %s, total = %s, pay_amount = NULL WHERE invoice_no = %s",
        (avail, total, no),
    )
    return get_invoice(no), stock_short_note(asked, avail)


async def show_payment(target, no, prefix="", only_if_changed=False):
    """صفحهٔ روش پرداخت؛ قبلش موجودی چک می‌شه.
    only_if_changed=True: فقط اگه چیزی عوض شد (تعداد کم شد / موجودی تموم شد) پیام نشون می‌ده و False برمی‌گردونه."""
    before = (await run_db(get_invoice, no))
    inv, note = (await run_db(stock_fit, no))
    if inv is None:
        key = before["model"]
        await send(target, soldout_text(key, (await run_db(stock_available, key)) or 0), soldout_kb(key))
        return False
    if only_if_changed and not note:
        return True
    balance = await run_db(get_balance, inv["user_id"])
    await send(target, prefix + note + payment_text(inv, balance), (await run_db(payment_kb, no)))
    return not only_if_changed


def pay_with_wallet(no, user_id, total):
    """True = پرداخت شد | False = موجودی کیف پول کافی نیست | "nostock" = موجودی انبار کافی نیست."""
    def work(cur):
        if not _reserve_in_tx(cur, no):
            return "nostock"
        cur.execute(
            "UPDATE wallets SET balance = balance - %s WHERE user_id = %s AND balance >= %s",
            (total, user_id, total),
        )
        if cur.rowcount != 1:
            return False
        cur.execute("UPDATE invoices SET status = 'paid', pay_method = 'wallet' WHERE invoice_no = %s", (no,))
        return True

    return tx(work)


# ---------------------------------------------------------------- ابزارها
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fa(n):
    return f"{n:,}".replace(",", "٬").translate(FA_DIGITS)


def en(text):
    """اعداد فارسی → انگلیسی (برای صفحه‌های کارت‌به‌کارت که بانک‌ها اعداد انگلیسی رو قبول می‌کنن)."""
    return text.translate(EN_DIGITS).replace("٬", ",")


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
        f"🎨 رنگ: <b>سفید</b>\n"
        f"💎 قیمت واحد: {fa(inv['unit_price'] // 1000)} هزار تومن\n"
        f"🔢 تعداد: {fa(inv['qty'])} عدد\n"
        f"{discount_line}"
        f"{LINE}\n"
        f"🚚 {'هزینه ارسال پس‌کرایه و به عهدهٔ خریدار' if is_bulk(inv['model']) else 'هزینه ارسال رایگان'}\n"
        f"💰 <b>جمع کل: {fa(inv['total'])} تومن</b>\n"
        f"📌 وضعیت: {STATUS.get(inv['status'], inv['status'])}\n"
        f"{LINE}"
    ) + (FRESH_NOTE if fresh else "")


def order_details(inv, bank=False):
    name = MODELS.get(inv["model"], (inv["model"],))[0]
    return (
        f"🔖 فاکتور: <code>{inv['invoice_no']}</code>\n"
        f"📦 {name} × {fa(inv['qty'])} — رنگ سفید\n"
        f"💰 مبلغ: <b>{fa(inv['total'])} تومن</b>\n"
        + (f"💳 مبلغ واریزی تعیین‌شده: <b>{fa(inv['pay_amount'])} تومن</b>\n" if bank and inv.get("pay_amount") else "")
        + (f"👑 عضو باشگاه VIP ({fa(inv['discount_percent'])}٪ تخفیف)\n" if inv.get("discount_percent") else "")
        + f"{LINE}\n"
        f"👤 {escape(inv['full_name'] or '—')}\n"
        f"📞 {escape(inv['phone'] or '—')}\n"
        + (f"☎️ ثابت: {escape(inv.get('landline') or '—')}\n" if is_bulk(inv["model"]) else "")
        + f"📍 {escape(inv['address'] or '—')}\n"
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
    landline = State()
    address = State()
    postal = State()
    emergency = State()
    receipt = State()


def menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main")]]
    )


def nav_row(back_to, main_cb="main"):
    return [
        InlineKeyboardButton(text="🔙 بازگشت به مرحله قبل", callback_data=back_to),
        InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data=main_cb),
    ]


def main_menu(uid=None):
    open_rows = []
    if uid:
        try:
            has_open = latest_open_invoice(uid) is not None
        except Exception:
            has_open = False
        if has_open:
            open_rows = [
                [InlineKeyboardButton(text="🚨 فاکتور نیمه‌تمام دارین", callback_data="openinv:0")]
            ]
    admin_rows = []
    if uid and ADMIN_ID and uid == ADMIN_ID:
        admin_rows = [[InlineKeyboardButton(text="📦 افزودن موجودی (ادمین)", callback_data="stk:menu")]]
    return InlineKeyboardMarkup(
        inline_keyboard=open_rows
        + [
            [
                InlineKeyboardButton(text="🛍 خرید تکی", callback_data="buy_single"),
                InlineKeyboardButton(text="📦 خرید عمده", callback_data="buy_bulk"),
            ],
            [InlineKeyboardButton(text="🏠 پرداخت درب منزل", callback_data="cod_home")],
            [InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet")],
            [InlineKeyboardButton(text=CLUB_BTN, callback_data="vip_club")],
            [InlineKeyboardButton(text="🎉 جشنواره‌ها و کمپین‌های فروش", callback_data="campaigns")],
            [
                InlineKeyboardButton(text="📋 فاکتورهای من", callback_data="myinv:0"),
                InlineKeyboardButton(text="🔍 پیگیری سفارش", callback_data="track"),
            ],
            [InlineKeyboardButton(text="🎁 دریافت نمونه کار رایگان", callback_data="free_sample")],
            [InlineKeyboardButton(text="🤝 همکاری در فروش", callback_data="partnership")],
            [
                InlineKeyboardButton(text="❓ سوالات متداول", callback_data="faq"),
                InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL),
            ],
        ]
        + admin_rows
    )


def models_menu(bulk=False):
    rows = [
        [InlineKeyboardButton(text=f"{name} | {fa(price)} هزار تومن", callback_data=f"model:{key}")]
        for key, (name, price) in MODELS.items()
        if is_bulk(key) == bulk
    ]
    rows.append(nav_row("main"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qty_menu(key):
    bulk = is_bulk(key)
    quick = (15, 30, 45) if bulk else (1, 2, 3)
    rows = [
        [InlineKeyboardButton(text=f"{n} عدد", callback_data=f"qty:{key}:{n}")]
        for n in quick
    ]
    rows.append([InlineKeyboardButton(text="✏️ انتخاب تعداد دلخواه", callback_data=f"qty_custom:{key}")])
    rows.append(nav_row("buy_bulk" if bulk else "buy_single"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qty_text(key):
    name, price = MODELS[key]
    return (
        f"{'📦' if is_bulk(key) else '🛍'} <b>{name}</b>\n"
        f"{LINE}\n"
        f"💎 قیمت واحد: {fa(price)} هزار تومن\n"
        f"{LINE}\n"
        + (f"⚠️ حداقل سفارش عمده: {fa(BULK_MIN_QTY)} عدد\n{LINE}\n" if is_bulk(key) else "")
        + "🔢 چند عدد نیاز داری؟\n"
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
COLOR_NOTE = "🎨 <b>نکته:</b> تمام مدل‌ها در حال حاضر فقط به‌صورت <b>تک‌رنگ سفید</b> قابل سفارش هستن."
MODELS_TEXT = f"🛍 <b>خرید تکی</b>\n{COLOR_NOTE}\n\nلطفاً مدل مورد نظرت رو انتخاب کن:"
BULK_MODELS_TEXT = f"📦 <b>خرید عمده</b>\n{COLOR_NOTE}\n\nلطفاً مدل مورد نظرت رو انتخاب کن:"
INVALID_TEXT = "این فاکتور دیگه معتبر نیست. لطفاً از منوی اصلی دوباره شروع کن."


# ---------------------------------------------------------------- بررسی آنی عضویت باشگاه
_last_club_err = 0.0


async def club_status(bot, user_id):
    """'none' = عضو نیست | 'active' = عضو کانال | 'left' = ثبت‌نام کرده ولی از کانال خارج شده | 'error' = بررسی ممکن نشد"""
    global _last_club_err
    registered = (await run_db(get_member, user_id)) is not None
    try:
        if not CLUB_CHAT_ID:
            raise RuntimeError("CLUB_CHAT_ID is not set")
        m = await bot.get_chat_member(CLUB_CHAT_ID, user_id)
        if m.status in ("creator", "administrator", "member"):
            return "active"
        if m.status == "restricted" and getattr(m, "is_member", False):
            return "active"
        return "left" if registered else "none"
    except TelegramBadRequest as e:
        if "user not found" in str(e).lower() or "participant_id_invalid" in str(e).lower():
            return "left" if registered else "none"
        err = e
    except Exception as e:
        err = e
    # خطا: حداکثر ساعتی یک بار به ادمین خبر بده
    now = asyncio.get_event_loop().time()
    if ADMIN_ID and now - _last_club_err > 3600:
        _last_club_err = now
        try:
            with keeping():
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ بررسی عضویت کانال باشگاه ناموفق بود:\n<code>{escape(str(err))}</code>\n"
                    "مطمئن شو ربات ادمین کانال هست و CLUB_CHAT_ID درسته.",
                )
        except Exception:
            pass
    return "error"


async def club_discount(bot, user_id, key=None):
    """(درصد تخفیف، وضعیت) رو برمی‌گردونه؛ تخفیف فقط برای عضو فعال کانال. خرید عمده تخفیف نداره."""
    if key is not None and is_bulk(key):
        return 0, "none"
    status = await club_status(bot, user_id)
    return (CLUB_DISCOUNT if status == "active" else 0), status


async def club_left_notice(message, status):
    if status == "left":
        await message.answer(CLUB_LEFT_TEXT, reply_markup=club_kb())


# ---------------------------------------------------------------- منو و خرید
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    try:
        (await run_db(touch_user, message.from_user.id))
    except Exception:
        log.exception("touch_user failed")
    await sweep(message.bot, message.chat.id, message.message_id)
    await message.answer(MAIN_TEXT, reply_markup=(await run_db(main_menu, message.from_user.id)))


# ---------------------------------------------------------------- دستورهای اسلشی (منوی همبرگری)
# مشتری: فقط /start. ادمین (پیوی) و گروه پرسنل: دستورهای گزارش.
# درآمد (پولی) فقط برای ادمین و فقط توی پیوی؛ هیچ‌وقت توی گروه نمایش داده نمی‌شه.
USER_COMMANDS = [
    BotCommand(command="start", description="🏠 منوی اصلی"),
]
STAFF_COMMANDS = [
    BotCommand(command="stock", description="📦 موجودی فعلی"),
    BotCommand(command="sales_1", description="📈 گزارش فروش امروز"),
    BotCommand(command="sales_7", description="📈 گزارش فروش ۷ روز گذشته"),
    BotCommand(command="sales_30", description="📈 گزارش فروش ۳۰ روز گذشته"),
    BotCommand(command="unpaid", description="🛒 فاکتورهای نیمه‌تمام امروز"),
    BotCommand(command="unpaid_7", description="🛒 فاکتورهای نیمه‌تمام ۷ روز گذشته"),
    BotCommand(command="staff_1", description="👥 کارکرد پرسنل امروز"),
    BotCommand(command="staff_7", description="👥 کارکرد پرسنل ۷ روز گذشته"),
    BotCommand(command="staff_30", description="👥 کارکرد پرسنل ۳۰ روز گذشته"),
]
INCOME_COMMANDS = [
    BotCommand(command="income_1", description="💰 درآمد روز گذشته"),
    BotCommand(command="income_7", description="💰 درآمد ۷ روز گذشته"),
    BotCommand(command="income_14", description="💰 درآمد ۱۴ روز گذشته"),
    BotCommand(command="income_30", description="💰 درآمد ۳۰ روز گذشته"),
]
STAFF_CMDS = {c.command for c in STAFF_COMMANDS}


async def _cmd_reset(message, state):
    """مثل /start: وضعیت قبلی پاک می‌شه و پیام‌های قدیمی چت تمیز می‌شن."""
    await state.clear()
    try:
        (await run_db(touch_user, message.from_user.id))
    except Exception:
        log.exception("touch_user failed")
    await sweep(message.bot, message.chat.id, message.message_id)


# ---------------------------------------------------------------- گزارش‌ها (ادمین + گروه پرسنل)
def _is_admin_msg(message):
    return bool(ADMIN_ID) and message.from_user is not None and message.from_user.id == ADMIN_ID


def _staff_allowed(message):
    """ادمین (هرجا) یا هر عضو گروه پرسنل (فقط توی همون گروه)."""
    if _is_admin_msg(message):
        return True
    return message.chat.id in {c for c in (ORDERS_CHAT_ID, READY_CHAT_ID) if c}


def _today_start():
    """شروع امروز (ساعت ۰۰ به وقت تهران) به UTC."""
    now_t = datetime.now(timezone.utc) + _TEHRAN
    return now_t.replace(hour=0, minute=0, second=0, microsecond=0) - _TEHRAN


def _days_start(days):
    """n روز گذشته = امروز + (n-1) روز قبلش، از ساعت ۰۰ تهران."""
    return _today_start() - timedelta(days=days - 1)


def _period_title(days):
    return "امروز" if days == 1 else f"{fa(days)} روز گذشته"


def _short_jdate(dt):
    jy, jm, jd = to_jalali(dt.year, dt.month, dt.day)
    return f"{jm:02d}/{jd:02d}".translate(FA_DIGITS)


def _pct(cur, prev):
    if prev <= 0:
        return "🆕" if cur > 0 else "—"
    ch = round((cur - prev) * 100 / prev)
    return f"{'▲' if ch >= 0 else '▼'} {fa(abs(ch))}٪"


def _bar(v, top):
    if top <= 0 or v <= 0:
        return "▁"
    return "▇" * max(1, round(10 * v / top))


def _model_name(base):
    return MODELS.get(base, (base,))[0]


@dp.message(Command("stock"))
async def stock_cmd(message: Message, state: FSMContext):
    if not _staff_allowed(message):
        return
    if message.chat.type == "private":
        await state.clear()
    await message.answer((await run_db(stock_report, )))


# ---- گزارش فروش (بدون مبلغ؛ مبلغ‌ها فقط توی /income_*)
def sales_report_text(days):
    now_utc = datetime.now(timezone.utc)
    since = _days_start(days)
    prev_since = since - timedelta(days=days)
    rows = q_all(
        "SELECT REPLACE(model, 'bulk_', '') AS base, (LEFT(model, 5) = 'bulk_') AS bulk, "
        "COUNT(*) AS orders, COALESCE(SUM(qty), 0) AS units FROM invoices "
        "WHERE status = 'paid' AND paid_at >= %s GROUP BY 1, 2",
        (since,),
    )
    prev = q_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS units FROM invoices "
        "WHERE status = 'paid' AND paid_at >= %s AND paid_at < %s",
        (prev_since, now_utc - timedelta(days=days)),
    )
    waiting = q_one(
        "SELECT COUNT(*) AS n FROM invoices WHERE status = 'paid' AND prep_status IN ('new', 'preparing')"
    )
    title = _period_title(days)
    lines = [f"📈 <b>گزارش فروش — {title}</b>", f"📅 {jdate(since)} تا {jdate(now_utc)}", LINE]
    if not rows:
        lines.append("برای این بازه هنوز فروشی ثبت نشده.")
    else:
        orders = sum(int(r["orders"]) for r in rows)
        units = sum(int(r["units"]) for r in rows)
        singles = sum(int(r["orders"]) for r in rows if not r["bulk"])
        bulks = orders - singles
        lines += [
            f"🧾 سفارش پرداخت‌شده: <b>{fa(orders)}</b>  ({_pct(orders, int(prev['n']))})",
            f"📦 تعداد کل محصول: <b>{fa(units)}</b> عدد  ({_pct(units, int(prev['units']))})",
            f"🛍 تکی: {fa(singles)} سفارش  |  📦 عمده: {fa(bulks)} سفارش",
            f"🧮 میانگین هر سفارش: {fa(round(units / orders, 1)).replace('.', '٫')} عدد",
            LINE,
        ]
        by_model = {}
        for r in rows:
            m = by_model.setdefault(r["base"], {"s_o": 0, "s_u": 0, "b_o": 0, "b_u": 0})
            k = "b" if r["bulk"] else "s"
            m[k + "_o"] += int(r["orders"])
            m[k + "_u"] += int(r["units"])
        ranked = sorted(by_model.items(), key=lambda kv: -(kv[1]["s_u"] + kv[1]["b_u"]))
        for base, m in ranked:
            lines.append(f"<b>{_model_name(base)}</b> — {fa(m['s_u'] + m['b_u'])} عدد")
            if m["s_o"]:
                lines.append(f"   🛍 تکی: {fa(m['s_o'])} سفارش ({fa(m['s_u'])} عدد)")
            if m["b_o"]:
                lines.append(f"   📦 عمده: {fa(m['b_o'])} سفارش ({fa(m['b_u'])} عدد)")
        top_base, top_m = ranked[0]
        lines += [LINE, f"🏆 پرفروش‌ترین: {_model_name(top_base)} ({fa(top_m['s_u'] + top_m['b_u'])} عدد)"]
    lines.append(f"⏳ سفارش‌های آماده‌نشده الان: <b>{fa(int((waiting or {}).get('n') or 0))}</b>")
    if days > 1:
        lines.append("ℹ️ درصدها نسبت به همین‌قدر روزِ قبلشه.")
    else:
        lines.append("ℹ️ درصد نسبت به دیروز تا همین ساعته.")
    return "\n".join(lines)


# ---- گزارش کارکرد پرسنل (هر نفر یک پیام جدا)
def _kind_label(bulk, qty, n):
    if bulk:
        return f"📦 {fa(n)} سفارش {fa(qty)} عددی عمده"
    if qty == 1:
        return f"🛍 {fa(n)} سفارش تک‌عددی"
    return f"🛍 {fa(n)} سفارش {fa(qty)} عددی"


def staff_messages(days):
    now_utc = datetime.now(timezone.utc)
    since = _days_start(days)
    per = q_all(
        "SELECT prep_by, MAX(prep_by_name) AS name, COUNT(*) AS orders, COALESCE(SUM(qty), 0) AS units, "
        "AVG(EXTRACT(EPOCH FROM (prep_done_at - prep_started_at))) FILTER (WHERE prep_started_at IS NOT NULL) AS avg_sec, "
        "MIN(EXTRACT(EPOCH FROM (prep_done_at - prep_started_at))) FILTER (WHERE prep_started_at IS NOT NULL) AS min_sec "
        "FROM invoices WHERE prep_status = 'done' AND prep_done_at >= %s AND prep_by IS NOT NULL "
        "GROUP BY prep_by ORDER BY orders DESC, units DESC",
        (since,),
    )
    kinds = q_all(
        "SELECT prep_by, (LEFT(model, 5) = 'bulk_') AS bulk, qty, COUNT(*) AS n FROM invoices "
        "WHERE prep_status = 'done' AND prep_done_at >= %s AND prep_by IS NOT NULL "
        "GROUP BY prep_by, 2, qty ORDER BY 2, qty",
        (since,),
    )
    left = q_one("SELECT COUNT(*) AS n FROM invoices WHERE status = 'paid' AND prep_status IN ('new', 'preparing')")
    head = f"👥 <b>گزارش کارکرد پرسنل — {_period_title(days)}</b>\n📅 {jdate(since)} تا {jdate(now_utc)}"
    if not per:
        return [head + f"\n{LINE}\nبرای این بازه هنوز سفارشی آماده ارسال ثبت نشده.\n⏳ آماده‌نشده الان: {fa(int((left or {}).get('n') or 0))}"]
    total_orders = sum(int(r["orders"]) for r in per)
    total_units = sum(int(r["units"]) for r in per)
    msgs = [head]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(per):
        icon = medals[i] if i < 3 else "👤"
        lines = [f"{icon} <b>{escape(r['name'] or '—')}</b>", LINE]
        for k in kinds:
            if k["prep_by"] == r["prep_by"]:
                lines.append(_kind_label(bool(k["bulk"]), int(k["qty"]), int(k["n"])))
        orders, units = int(r["orders"]), int(r["units"])
        lines += [LINE, f"✅ جمع: <b>{fa(orders)}</b> سفارش — <b>{fa(units)}</b> عدد"]
        if total_orders:
            lines.append(f"📊 سهم از کل تیم: {fa(round(orders * 100 / total_orders))}٪")
        if r.get("avg_sec") is not None:
            lines.append(f"⏱ میانگین زمان هر سفارش: {_fa_minutes(float(r['avg_sec']))}")
        if r.get("min_sec") is not None:
            lines.append(f"⚡ سریع‌ترین سفارش: {_fa_minutes(float(r['min_sec']))}")
        msgs.append("\n".join(lines))
    summary = [
        "🏁 <b>جمع‌بندی تیم</b>",
        LINE,
        f"✅ کل سفارش‌های آماده‌شده: <b>{fa(total_orders)}</b> ({fa(total_units)} عدد)",
        f"👥 تعداد پرسنل فعال: {fa(len(per))} نفر",
        f"🌟 نفر اول: {escape(per[0]['name'] or '—')} ({fa(int(per[0]['orders']))} سفارش)",
        f"⏳ آماده‌نشده الان: <b>{fa(int((left or {}).get('n') or 0))}</b>",
    ]
    if days > 1:
        summary.append(f"📆 میانگین روزانهٔ تیم: {fa(round(total_orders / days, 1)).replace('.', '٫')} سفارش")
    msgs.append("\n".join(summary))
    return msgs


# ---- فاکتورهای نیمه‌تمام امروز + یادآوری به مشتری
REMIND_GAP_HOURS = 12
REMIND_TEXT = (
    "👋 سلام {name}!\n\n"
    "فاکتور <code>{no}</code> ({model} × {qty} عدد) هنوز نهایی نشده 🛒\n"
    "موجودی محدوده؛ برای اینکه سفارشت از دست نره، روی دکمهٔ زیر بزن و خریدت رو تکمیل کن 👇"
)


def unpaid_today(days=1):
    return q_all(
        "SELECT invoice_no, user_id, model, qty, total, status, full_name, phone, created_at, reminded_at "
        "FROM invoices WHERE " + OPEN_SQL + " AND created_at >= %s ORDER BY created_at DESC",
        (_days_start(days),),
    )


def unpaid_text(rows, money=False, days=1):
    now_utc = datetime.now(timezone.utc)
    if days == 1:
        lines = [f"🛒 <b>فاکتورهای نیمه‌تمام امروز</b> — {jdate(now_utc)}", LINE]
    else:
        lines = [
            f"🛒 <b>فاکتورهای نیمه‌تمام {_period_title(days)}</b>",
            f"📅 {jdate(_days_start(days))} تا {jdate(now_utc)}",
            LINE,
        ]
    if not rows:
        lines.append("فاکتور نیمه‌تمامی نداریم 🎉")
        return "\n".join(lines)
    shown = 0
    for r in rows:
        if sum(len(x) + 1 for x in lines) > 3300:  # محدودیت ۴۰۹۶ کاراکتری تلگرام
            break
        shown += 1
        name = escape(r["full_name"] or "مشتری")
        who = f'<a href="tg://user?id={r["user_id"]}">{name}</a>'
        sent = " 📨" if r.get("reminded_at") else ""
        step = "منتظر فیش" if r["status"] == "awaiting_receipt" else "صفحهٔ پرداخت"
        cash = f"💵 {fa(int(r['total']))} تومن | " if money else ""
        hhmm = jtime(r["created_at"])[:5]
        when = f"ساعت {hhmm}" if days == 1 else f"{jdate(r['created_at'])[5:]} ساعت {hhmm}"
        lines.append(
            f"• {who} — {_model_name(r['model'])} × {fa(int(r['qty']))}\n"
            f"   {cash}{step} | {when}{sent}"
        )
    if shown < len(rows):
        lines.append(f"… و {fa(len(rows) - shown)} مورد دیگه")
    total = sum(int(r["total"]) for r in rows)
    cash = f"  |  💵 ارزش کل: <b>{fa(total)}</b> تومن" if money else ""
    lines += [LINE, f"🧾 تعداد: <b>{fa(len(rows))}</b>{cash}", "📨 = قبلاً یادآوری شده"]
    return "\n".join(lines)


def unpaid_kb(rows, days=1):
    btns = [
        [InlineKeyboardButton(text=f"✉️ یادآوری به همه ({fa(len(rows))})", callback_data=f"remind:all:{days}")]
    ]
    pair = []
    for r in rows[:20]:
        label = (r["full_name"] or f"فاکتور {r['invoice_no']}")[:18]
        pair.append(InlineKeyboardButton(text=f"✉️ {label}", callback_data=f"remind:{r['invoice_no']}"))
        if len(pair) == 2:
            btns.append(pair)
            pair = []
    if pair:
        btns.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def _send_reminder(bot, r):
    text = REMIND_TEXT.format(
        name=escape(r["full_name"] or ""), no=r["invoice_no"], model=_model_name(r["model"]), qty=fa(int(r["qty"]))
    ).replace("سلام !", "سلام!")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🧾 تکمیل خرید", callback_data="openinv:0")]]
    )
    try:
        await bot.send_message(r["user_id"], text, reply_markup=kb)
    except Exception:
        return False
    (await run_db(q_exec, "UPDATE invoices SET reminded_at = now() WHERE invoice_no = %s", (r["invoice_no"],)))
    return True


@dp.callback_query(F.data.startswith("remind:"))
async def remind_cb(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("⛔️ فقط مدیر می‌تونه پیام بفرسته.", show_alert=True)
        return
    parts = call.data.split(":")
    arg = parts[1]
    days = 7 if arg == "all" and len(parts) > 2 and parts[2] == "7" else 1
    # دکمهٔ تکی از هر دو لیست می‌تونه اومده باشه، پس از لیست ۷ روزه پیداش می‌کنیم
    rows = (await run_db(unpaid_today, days if arg == "all" else 7))
    if arg == "all":
        cutoff = datetime.now(timezone.utc) - timedelta(hours=REMIND_GAP_HOURS)
        todo = [r for r in rows if not r.get("reminded_at") or r["reminded_at"] < cutoff]
        skipped = len(rows) - len(todo)
    else:
        todo = [r for r in rows if str(r["invoice_no"]) == arg]
        skipped = 0
        if not todo:
            await call.answer("این فاکتور دیگه نیمه‌تمام نیست (احتمالاً پرداخت یا لغو شده).", show_alert=True)
            return
    await call.answer("⏳ در حال ارسال...")
    ok = bad = 0
    for r in todo:
        if await _send_reminder(call.bot, r):
            ok += 1
        else:
            bad += 1
        await asyncio.sleep(0.05)
    note = f"📨 یادآوری برای {fa(ok)} مشتری ارسال شد."
    if bad:
        note += f"\n⚠️ {fa(bad)} نفر پیام رو دریافت نکردن (ربات رو بلاک کردن)."
    if skipped:
        note += f"\n⏭ {fa(skipped)} نفر در {fa(REMIND_GAP_HOURS)} ساعت اخیر یادآوری گرفته بودن و رد شدن."
    await call.message.answer(note)


# ---- درآمد (فقط ادمین، فقط پیوی)
def _income_sum(a, b):
    return q_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS units, COALESCE(SUM(total), 0) AS rev, "
        "COALESCE(SUM(unit_price * qty), 0) AS gross, "
        "COUNT(*) FILTER (WHERE m = 'wallet') AS wallet_n, "
        "COALESCE(SUM(total) FILTER (WHERE m = 'wallet'), 0) AS wallet_rev "
        "FROM (SELECT total, qty, unit_price, COALESCE(pay_method, CASE WHEN pay_amount IS NOT NULL "
        "THEN 'card' ELSE 'wallet' END) AS m FROM invoices "
        "WHERE status = 'paid' AND paid_at >= %s AND paid_at < %s) t",
        (a, b),
    )


def income_text(days):
    now_utc = datetime.now(timezone.utc)
    today0 = _today_start()
    if days == 1:  # «روز گذشته» = دیروز (کامل)
        a, b = today0 - timedelta(days=1), today0
        pa, pb = a - timedelta(days=1), a
        title = "دیروز"
        cmp_label = "نسبت به روز قبلش"
    else:
        a, b = _days_start(days), now_utc
        pa, pb = a - timedelta(days=days), now_utc - timedelta(days=days)
        title = f"{fa(days)} روز گذشته"
        cmp_label = f"نسبت به {fa(days)} روز قبلش"
    cur, prev = _income_sum(a, b), _income_sum(pa, pb)
    n, units, rev, gross = int(cur["n"]), int(cur["units"]), int(cur["rev"]), int(cur["gross"])
    lines = [f"💰 <b>درآمد {title}</b>", f"📅 {jdate(a)}" + ("" if days == 1 else f" تا {jdate(now_utc)}"), LINE]
    if n == 0:
        lines.append("در این بازه هنوز فروش پرداخت‌شده‌ای ثبت نشده.")
    else:
        wallet_n, wallet_rev = int(cur["wallet_n"]), int(cur["wallet_rev"])
        lines += [
            f"💵 درآمد کل: <b>{fa(rev)}</b> تومن  ({_pct(rev, int(prev['rev']))})",
            f"🧾 تعداد سفارش: <b>{fa(n)}</b>  ({_pct(n, int(prev['n']))})",
            f"📦 تعداد محصول: <b>{fa(units)}</b> عدد",
            f"🧮 میانگین هر سفارش: {fa(rev // n)} تومن",
            LINE,
            f"💳 کارت‌به‌کارت: {fa(n - wallet_n)} سفارش — {fa(rev - wallet_rev)} تومن",
            f"💰 از کیف پول: {fa(wallet_n)} سفارش — {fa(wallet_rev)} تومن",
        ]
        if gross > rev:
            lines.append(f"🎁 تخفیف باشگاه داده‌شده: {fa(gross - rev)} تومن")
        per_day = {
            r["d"]: (int(r["n"]), int(r["rev"]))
            for r in q_all(
                "SELECT (paid_at AT TIME ZONE 'Asia/Tehran')::date AS d, COUNT(*) AS n, COALESCE(SUM(total), 0) AS rev "
                "FROM invoices WHERE status = 'paid' AND paid_at >= %s AND paid_at < %s GROUP BY 1 ORDER BY 1",
                (a, b),
            )
        }
        if days > 1:
            first = (a + _TEHRAN).date()
            span = [first + timedelta(days=i) for i in range(days)]
            top = max((v[1] for v in per_day.values()), default=0)
            if days <= 14:
                lines += [LINE, "📆 <b>روزانه:</b>"]
                for d in span:
                    v = per_day.get(d, (0, 0))
                    lines.append(f"{_short_jdate(d)}  {_bar(v[1], top)}  {fa(v[1])}")
            best = max(per_day.items(), key=lambda kv: kv[1][1])
            lines += [
                LINE,
                f"🏆 بهترین روز: {_short_jdate(best[0])} با {fa(best[1][1])} تومن ({fa(best[1][0])} سفارش)",
                f"📊 میانگین روزانه: {fa(rev // days)} تومن",
            ]
        models = q_all(
            "SELECT REPLACE(model, 'bulk_', '') AS base, COALESCE(SUM(total), 0) AS rev, COALESCE(SUM(qty), 0) AS units "
            "FROM invoices WHERE status = 'paid' AND paid_at >= %s AND paid_at < %s GROUP BY 1 ORDER BY 2 DESC",
            (a, b),
        )
        lines += [LINE, "🛍 <b>به تفکیک مدل:</b>"]
        for r in models:
            share = round(int(r["rev"]) * 100 / rev) if rev else 0
            lines.append(f"• {_model_name(r['base'])}: {fa(int(r['rev']))} تومن ({fa(int(r['units']))} عدد، {fa(share)}٪)")
    if days == 1:
        t = _income_sum(today0, now_utc)
        lines += [LINE, f"☀️ امروز تا همین الان: <b>{fa(int(t['rev']))}</b> تومن ({fa(int(t['n']))} سفارش)"]
    lines += [LINE, f"ℹ️ {cmp_label}" + (" تا همین ساعت." if days != 1 else "."),
              "ℹ️ سفارش‌های کیف پول از اعتبارِ قبلاً شارژشده پرداخت شدن."]
    return "\n".join(lines)


# ---- هندلرها
@dp.message(Command("sales_1", "sales_7", "sales_30"))
async def cmd_sales(message: Message, command: CommandObject, state: FSMContext):
    if not _staff_allowed(message):
        return
    if message.chat.type == "private":
        await state.clear()
    await message.answer((await run_db(sales_report_text, int(command.command.split("_")[1]))))


@dp.message(Command("staff_1", "staff_7", "staff_30"))
async def cmd_staff(message: Message, command: CommandObject, state: FSMContext):
    if not _staff_allowed(message):
        return
    if message.chat.type == "private":
        await state.clear()
    for text in (await run_db(staff_messages, int(command.command.split("_")[1]))):
        await message.answer(text)
        await asyncio.sleep(0.3)


@dp.message(Command("unpaid", "unpaid_7"))
async def cmd_unpaid(message: Message, command: CommandObject, state: FSMContext):
    if not _staff_allowed(message):
        return
    if message.chat.type == "private":
        await state.clear()
    days = 7 if command.command == "unpaid_7" else 1
    rows = (await run_db(unpaid_today, days))
    # مبلغ‌ها و دکمهٔ یادآوری فقط توی چت خصوصی ادمین؛ توی گروه پرسنل فقط لیست ساده
    admin_dm = _is_admin_msg(message) and message.chat.type == "private"
    kb = unpaid_kb(rows, days) if rows and admin_dm else None
    await message.answer(unpaid_text(rows, money=admin_dm, days=days), reply_markup=kb)


@dp.message(Command("income_1", "income_7", "income_14", "income_30"), F.chat.type == "private")
async def cmd_income(message: Message, command: CommandObject, state: FSMContext):
    if not _is_admin_msg(message):
        return
    await state.clear()
    await message.answer((await run_db(income_text, int(command.command.split("_")[1]))))


async def setup_commands(bot):
    """منوی دستورها (دکمهٔ همبرگری) رو توی تلگرام ثبت می‌کنه:
    مشتری ← فقط /start | ادمین (پیوی) ← همه | گروه پرسنل ← گزارش‌ها (بدون درآمد)."""
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        if ADMIN_ID:
            await bot.set_my_commands(
                USER_COMMANDS[:1] + STAFF_COMMANDS + INCOME_COMMANDS,
                scope=BotCommandScopeChat(chat_id=ADMIN_ID),
            )
        if ORDERS_CHAT_ID:
            await bot.set_my_commands(STAFF_COMMANDS, scope=BotCommandScopeChat(chat_id=ORDERS_CHAT_ID))
        if READY_CHAT_ID:
            await bot.set_my_commands(STAFF_COMMANDS, scope=BotCommandScopeChat(chat_id=READY_CHAT_ID))
    except Exception:
        log.exception("set_my_commands failed")


@dp.callback_query(F.data == "main")
async def go_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await sweep(call.bot, call.message.chat.id, call.message.message_id, call.message.message_id)
    await show(call, MAIN_TEXT, (await run_db(main_menu, call.from_user.id)))
    await call.answer()


@dp.callback_query(F.data == "buy_single")
async def buy_single(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await sweep(call.bot, call.message.chat.id, call.message.message_id, call.message.message_id)
    await show(call, MODELS_TEXT, models_menu())
    await call.answer()


@dp.callback_query(F.data == "buy_bulk")
async def buy_bulk(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await sweep(call.bot, call.message.chat.id, call.message.message_id, call.message.message_id)
    await show(call, BULK_MODELS_TEXT, models_menu(bulk=True))
    await call.answer()


async def show_qty(call, key):
    """مرحلهٔ تعداد؛ اگه عکس مدل موجود بود، با عکس نشون می‌ده."""
    avail = (await run_db(stock_available, key))
    if avail is not None and avail < (BULK_MIN_QTY if is_bulk(key) else 1):
        await show(call, soldout_text(key, avail), soldout_kb(key))
        return
    path = model_photo(key)
    if path:
        try:
            try:
                await call.message.delete()
            except Exception:
                pass
            msg = await call.message.answer_photo(
                _photo_input(path), caption=qty_text(key), reply_markup=qty_menu(key)
            )
            _remember_photo(path, msg)
            return
        except Exception as e:
            log.exception("show_qty photo failed: %s: %s", type(e).__name__, e)
    await show(call, qty_text(key), qty_menu(key))


@dp.callback_query(F.data.startswith("model:"))
async def choose_model(call: CallbackQuery, state: FSMContext):
    await state.clear()
    key = call.data.split(":", 1)[1]
    await show_qty(call, key)
    await call.answer()


@dp.callback_query(F.data.startswith("qty:"))
async def choose_qty(call: CallbackQuery):
    _, key, qty = call.data.split(":")
    if key not in MODELS or int(qty) < 1 or (is_bulk(key) and int(qty) < BULK_MIN_QTY):
        await call.answer()
        return
    qty = int(qty)
    note = ""
    avail = (await run_db(stock_available, key))
    if avail is not None:
        if avail < (BULK_MIN_QTY if is_bulk(key) else 1):
            await show(call, soldout_text(key, avail), soldout_kb(key))
            await call.answer()
            return
        if qty > avail:
            note = stock_short_note(qty, avail)
            qty = avail
    discount, status = await club_discount(call.bot, call.from_user.id, key)
    no = (await run_db(create_invoice, call.from_user.id, key, qty, discount))
    await show(call, note + invoice_text((await run_db(get_invoice, no)), fresh=True), invoice_kb(no))
    await club_left_notice(call.message, status)
    await call.answer()


@dp.callback_query(F.data.startswith("invback:"))
async def invoice_back(call: CallbackQuery):
    no = int(call.data.split(":", 1)[1])
    inv = (await run_db(cancel_if_pending, no, call.from_user.id))
    if inv is None:
        await call.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    key = inv["model"]
    await show_qty(call, key)
    await call.answer()


@dp.callback_query(F.data.startswith("invmain:"))
async def invoice_main(call: CallbackQuery, state: FSMContext):
    no = int(call.data.split(":", 1)[1])
    (await run_db(cancel_if_pending, no, call.from_user.id))
    await state.clear()
    await show(call, MAIN_TEXT, (await run_db(main_menu, call.from_user.id)))
    await call.answer()


@dp.callback_query(F.data.startswith("invshow:"))
async def invoice_show(call: CallbackQuery, state: FSMContext):
    await state.clear()
    no = call.data.split(":", 1)[1]
    inv = (await run_db(own_invoice, no, call.from_user.id))
    if inv is None:
        await show(call, INVALID_TEXT, (await run_db(main_menu, call.from_user.id)))
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
        + (f"⚠️ حداقل {fa(BULK_MIN_QTY)} عدد\n📝 مثال: 60" if is_bulk(key) else "📝 مثال: 5"),
        kb,
    )
    await call.answer()


@dp.message(Order.waiting_custom_qty, F.text)
async def receive_custom_qty(message: Message, state: FSMContext):
    raw = message.text.strip().translate(EN_DIGITS)
    qty = to_int(raw)
    if qty is None or qty < 1:
        await err(message, "❌ لطفاً فقط یه عدد صحیح بفرست، مثلاً 5", reply_markup=menu_kb())
        return
    if len(raw) > MAX_QTY_DIGITS:
        await err(message, "❌ این تعداد خیلی زیاده. لطفاً عدد کوچک‌تری بفرست.", reply_markup=menu_kb())
        return
    data = await state.get_data()
    key = data["model"]
    if is_bulk(key) and qty < BULK_MIN_QTY:
        await err(message, f"❌ حداقل سفارش عمده {fa(BULK_MIN_QTY)} عدده. عدد بزرگ‌تری بفرست.", reply_markup=menu_kb())
        return
    await state.clear()
    note = ""
    avail = (await run_db(stock_available, key))
    if avail is not None:
        if avail < (BULK_MIN_QTY if is_bulk(key) else 1):
            await message.answer(soldout_text(key, avail), reply_markup=soldout_kb(key))
            return
        if qty > avail:
            note = stock_short_note(qty, avail)
            qty = avail
    discount, status = await club_discount(message.bot, message.from_user.id, key)
    no = (await run_db(create_invoice, message.from_user.id, key, qty, discount))
    await message.answer(note + invoice_text((await run_db(get_invoice, no)), fresh=True), reply_markup=invoice_kb(no))
    await club_left_notice(message, status)


# ---------------------------------------------------------------- مشخصات مشتری
STEPS = ["name", "phone", "address", "postal", "emergency"]
STEPS_BULK = ["name", "phone", "landline", "address", "postal", "emergency"]


def steps_for(no):
    try:
        inv = get_invoice(int(no))
    except (TypeError, ValueError):
        inv = None
    return STEPS_BULK if inv and is_bulk(inv["model"]) else STEPS
FIELD = {
    "name": "full_name",
    "phone": "phone",
    "landline": "landline",
    "address": "address",
    "postal": "postal",
    "emergency": "emergency",
}
QUESTIONS = {
    "name": ("👤 <b>نام و نام خانوادگی</b>", "نام و نام خانوادگی گیرنده رو بفرست.", False),
    "phone": ("📞 <b>شماره تلفن</b>", "شماره تماست رو بفرست (مثلاً 09123456789).", False),
    "landline": (
        "☎️ <b>شماره تلفن ثابت</b>",
        "شماره تلفن ثابت (با کد شهر) رو بفرست، مثلاً 02112345678.\n\n"
        "⚠️ مهمه: وقتی موبایلت در دسترس نباشه، پست و باربری از طریق تلفن ثابت باهات هماهنگ می‌کنن "
        "و تحویل سفارش سریع‌تر و بدون دردسر انجام می‌شه. اگه تلفن ثابت نداری، «رد کردن» رو بزن.",
        True,
    ),
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
STEP_STATES = [getattr(Checkout, s) for s in STEPS_BULK]


def clean_phone(text):
    d = text.strip().translate(EN_DIGITS).replace(" ", "").replace("-", "").lstrip("+")
    return d if d.isdigit() and 10 <= len(d) <= 13 else None


def clean_landline(text):
    d = text.strip().translate(EN_DIGITS).replace(" ", "").replace("-", "")
    return d if d.isdigit() and d.startswith("0") and not d.startswith("09") and 10 <= len(d) <= 11 else None


def validate(step, text):
    """(مقدار، پیام خطا) رو برمی‌گردونه."""
    t = text.replace("\x00", "").strip()
    if step == "name":
        if 3 <= len(t) <= 60 and any(ch.isalpha() for ch in t):
            return t, None
        return None, "❌ لطفاً نام و نام خانوادگی رو کامل و درست بفرست."
    if step == "landline":
        d = clean_landline(t)
        if d:
            return d, None
        return None, "❌ شماره ثابت معتبر نیست. با کد شهر بفرست (مثلاً 02112345678) یا «رد کردن» رو بزن."
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
    steps = (await run_db(steps_for, no))
    idx = steps.index(step)
    title, hint, optional = QUESTIONS[step]
    text = f"{title}\n{LINE}\n{hint}\n\n📍 مرحله {fa(idx + 1)} از {fa(len(steps))}"
    back = f"invshow:{no}" if idx == 0 else f"ask:{steps[idx - 1]}:{no}"
    rows = []
    if optional:
        rows.append([InlineKeyboardButton(text="⏭ رد کردن", callback_data=f"skip:{no}")])
    rows.append(nav_row(back, f"invmain:{no}"))
    await send(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


BULK_SHIP_NOTE = "🚚 هزینه ارسال به‌صورت پس‌کرایه و به عهدهٔ خریدار می‌باشد.\n"
FREE_SHIP_NOTE = "🚚 هزینه ارسال رایگان است.\n"


def ship_note(inv):
    return BULK_SHIP_NOTE if is_bulk(inv["model"]) else FREE_SHIP_NOTE


def wallet_note(inv, balance):
    """موجودی کیف پول مشتری رو زیر مبلغ نشون می‌ده؛ اگه برای این سفارش کافی بود، پیشنهاد پرداخت با کیف پول."""
    if balance is None:
        return ""
    text = f"👛 موجودی کیف پول شما: <b>{balance:,} تومن</b>\n"
    if balance >= int(inv["total"]):
        text += "✅ موجودی کیف پول برای این سفارش کافیه، می‌تونید با کیف پول پرداخت کنید.\n"
    return text


def payment_text(inv, balance=None):
    return (
        f"💳 <b>انتخاب نحوه پرداخت</b>\n"
        f"{LINE}\n"
        f"🔖 شناسه فاکتور: <code>{inv['invoice_no']}</code>\n"
        f"💰 مبلغ قابل پرداخت: <b>{inv['total']:,} تومن</b>\n"
        + wallet_note(inv, balance)
        + ship_note(inv)
        + f"{LINE}\n"
        "لطفاً روش پرداخت رو انتخاب کن:"
    )


def payment_kb(no):
    last = steps_for(no)[-1]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"paycard:{no}")],
            [InlineKeyboardButton(text="💰 پرداخت از کیف پول", callback_data=f"paywallet:{no}")],
            nav_row(f"ask:{last}:{no}", f"invmain:{no}"),
        ]
    )


async def after_step(target, state, step, no):
    steps = (await run_db(steps_for, no))
    idx = steps.index(step)
    if idx + 1 < len(steps):
        await ask_step(target, state, steps[idx + 1], no)
        return
    await state.clear()
    (await run_db(mark_payment, no))
    await show_payment(target, no)


@dp.callback_query(F.data.startswith("ask:"))
async def ask_callback(call: CallbackQuery, state: FSMContext):
    _, step, no = call.data.split(":")
    if step not in STEPS_BULK or (await run_db(own_invoice, no, call.from_user.id)) is None or step not in (await run_db(steps_for, no)):
        await show(call, INVALID_TEXT, (await run_db(main_menu, call.from_user.id)))
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
    if no is None or (await run_db(own_invoice, no, message.from_user.id)) is None:
        await state.clear()
        await message.answer(INVALID_TEXT, reply_markup=(await run_db(main_menu, message.from_user.id)))
        return
    value, error = validate(step, message.text)
    if error:
        await err(message, error, reply_markup=menu_kb())
        return
    (await run_db(set_field, no, FIELD[step], value))
    await after_step(message, state, step, no)


@dp.callback_query(F.data.startswith("skip:"))
async def skip_step(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    cur = await state.get_state()
    step = cur.split(":")[1] if cur else None
    if (await run_db(own_invoice, no, call.from_user.id)) is None:
        await show(call, INVALID_TEXT, (await run_db(main_menu, call.from_user.id)))
        await call.answer()
        return
    if step not in ("landline", "postal", "emergency"):
        await call.answer()
        return
    await after_step(call, state, step, int(no))
    await call.answer()


# ---------------------------------------------------------------- پرداخت
@dp.callback_query(F.data.startswith("pay:"))
async def pay_screen(call: CallbackQuery, state: FSMContext):
    await state.clear()
    no = call.data.split(":", 1)[1]
    inv = (await run_db(own_invoice, no, call.from_user.id))
    if inv is None:
        await show(call, INVALID_TEXT, (await run_db(main_menu, call.from_user.id)))
    else:
        (await run_db(mark_payment, inv["invoice_no"]))
        await show_payment(call, inv["invoice_no"])
    await call.answer()


@dp.callback_query(F.data.startswith("paycard:"))
async def pay_card(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    inv = (await run_db(own_invoice, no, call.from_user.id))
    if inv is None:
        await call.answer("این فاکتور دیگه معتبر نیست.", show_alert=True)
        return
    no = inv["invoice_no"]
    if not await show_payment(call, no, only_if_changed=True):
        await call.answer()
        return
    if not (await run_db(reserve_stock, no)):  # یکی همزمان آخرین موجودی رو گرفته
        await show_payment(call, no)
        await call.answer()
        return
    (await run_db(set_status, no, "awaiting_receipt"))
    await state.set_state(Checkout.receipt)
    await state.update_data(no=no)
    pay_amount = (await run_db(assign_pay_amount, inv))
    ship_line = "🚚 پس‌کرایه (به عهدهٔ خریدار)" if is_bulk(inv["model"]) else "🎁 رایگان"
    caption = en(
        f"💳 شماره کارت {CARD_BANK}:\n"
        f"<b>{CARD_NUMBER}</b>\n"
        f"👤 صاحب حساب: {CARD_OWNER}\n\n"
        "💰 مبلغ قابل پرداخت:\n"
        f"<b>{pay_amount * 10:,} ریال</b>\n"
        "⚠️ (جهت تایید لحظه‌ای رسید توسط ربات، دقیقاً همین مبلغ را واریز کنید)\n\n"
        "📋 اطلاعات سفارش شما:\n\n"
        f"• شماره فاکتور: <code>{no}</code>\n\n"
        f"• هزینه ارسال: {ship_line}\n\n"
        f"⏳ مهلت ارسال رسید: {fa(PAY_DEADLINE_MIN)} دقیقه\n"
        "(بعد از این زمان، در صورت عدم ارسال رسید، اقلام این فاکتور به‌صورت خودکار برای سایر مشتریان آزاد می‌گردد)\n\n"
        "📸 لطفاً پس از واریز، تصویر فیش را ارسال کنید."
    )
    rows = []
    if CopyTextButton is not None:
        rows.append(
            [
                InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=CARD_NUMBER)),
                InlineKeyboardButton(text="📋 کپی مبلغ", copy_text=CopyTextButton(text=str(pay_amount * 10))),
            ]
        )
    rows.append([InlineKeyboardButton(text="✅ پرداخت کردم | ارسال رسید", callback_data=f"invsend:{no}")])
    rows.append(nav_row(f"pay:{no}", f"invmain:{no}"))
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(caption, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("invsend:"))
async def invoice_send(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    inv = (await run_db(own_invoice, no, call.from_user.id, statuses=("awaiting_receipt",)))
    if inv is None:
        await call.answer("این فاکتور دیگه معتبر نیست.", show_alert=True)
        return
    no = inv["invoice_no"]
    await state.set_state(Checkout.receipt)
    await state.update_data(no=no)
    await show(
        call,
        f"📸 <b>ارسال رسید</b>\n{LINE}\nعکس فیش واریزی (یا متن کامل رسید) رو همین‌جا و بدون هیچ توضیح اضافه‌ای بفرست.",
        InlineKeyboardMarkup(inline_keyboard=[nav_row(f"paycard:{no}", f"invmain:{no}")]),
    )
    await call.answer()


def support_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)]]
    )


async def send_receipt_to_admin(message, caption, kb=None):
    """فیش (عکس، فایل تصویری یا متن) رو برای مدیر می‌فرسته."""
    bot = message.bot
    if message.photo:
        await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption, reply_markup=kb)
    elif message.document:
        await bot.send_document(ADMIN_ID, message.document.file_id, caption=caption, reply_markup=kb)
    else:
        txt = escape((message.text or "")[:700])
        await bot.send_message(ADMIN_ID, f"{caption}\n\n📝 <b>رسید متنی:</b>\n{txt}", reply_markup=kb)


@dp.message(Checkout.receipt, F.photo | F.document | (F.text & ~F.text.startswith("/")))
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    no = data.get("no")
    inv = (await run_db(own_invoice, no, message.from_user.id, statuses=("awaiting_receipt",))) if no else None
    if inv is None:
        await state.clear()
        await message.answer(INVALID_TEXT, reply_markup=(await run_db(main_menu, message.from_user.id)))
        return
    if not ADMIN_ID:
        await err(message, "⚠️ مشکل فنی پیش اومد. لطفاً با پشتیبانی تماس بگیر.")
        return
    if message.document and not (message.document.mime_type or "").lower().startswith("image/"):
        await err(message, "📸 لطفاً فیش رو به صورت عکس یا اسکرین‌شات بفرست.", reply_markup=support_kb())
        return
    if message.text and (len(message.text.strip()) < 6 or not re.search(r"[\d۰-۹]", message.text)):
        await err(message, "🧾 لطفاً عکس فیش یا متن کامل رسید (شامل مبلغ و ساعت یا شماره پیگیری) رو بفرست.")
        return

    (await run_db(set_status, no, "reviewing"))
    uname = message.from_user.username
    caption = (
        f"🧾 <b>فیش جدید برای بررسی</b>\n{LINE}\n"
        + order_details((await run_db(get_invoice, no)), bank=True)
        + (f"\n🔗 @{escape(uname)}" if uname else "")
    )
    (await run_db(q_exec, "UPDATE invoices SET stock_at = COALESCE(stock_at, now()) WHERE invoice_no = %s", (no,)))
    left = (await run_db(stock_available, inv["model"], exclude=no))
    if left is not None and left < inv["qty"]:
        caption += "\n\n⚠️ <b>هشدار موجودی:</b> موجودی ثبت‌شده برای این مدل کمتر از تعداد این سفارشه (رزرو منقضی شده بود)."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تایید", callback_data=f"adm:ok:{no}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"adm:no:{no}"),
            ]
        ]
    )
    if (await run_db(q_one, "SELECT 1 AS x FROM bank_sms WHERE invoice_no = %s", (no,))):
        # پیامک بانک قبلاً رسیده: فیش برای بایگانی به مدیر می‌ره و سفارش همین‌جا تأیید می‌شه
        note = "\n\n✅ <b>تأیید خودکار (مطابقت با پیامک بانک)</b>"
        try:
            with keeping():
                await send_receipt_to_admin(message, caption + note)
        except Exception:
            pass
        await state.clear()
        if await auto_approve(message.bot, no, notify_admin=False):
            return
    try:
        with keeping():  # پیام فیش توی چت ادمین پاک نشه
            await send_receipt_to_admin(message, caption, kb)
    except Exception:
        (await run_db(set_status, no, "awaiting_receipt"))
        await err(message, "⚠️ ارسال فیش انجام نشد. لطفاً دوباره بفرست یا با پشتیبانی تماس بگیر.")
        return
    await state.clear()
    await message.answer(
        "✅ فیش شما دریافت شد و در حال بررسیه.\nنتیجه همین‌جا بهت اعلام می‌شه.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main")]]
        ),
    )


@dp.message(Checkout.receipt)
async def receipt_wrong(message: Message):
    await err(message, "📸 لطفاً عکس فیش واریزی یا متن رسید رو بفرست.", reply_markup=menu_kb())


async def send_success(chat_id, no, bot=None, message=None):
    bot = bot or message.bot
    chat_id = chat_id or message.chat.id
    await purge(bot, chat_id)
    text1 = (
        f"سفارش شما با شناسه {no} با موفقیت ثبت شد ✅\n"
        "سفارش شما در حال آماده سازی است🧑‍💻\n"
        "بعد تحویل بسته به پست ، کد پیگیری براتون ارسال میشه📬\n\n"
        + invoice_text((await run_db(get_invoice, no)))
    )
    text2 = "تشکر از محبت سلطان 😎"
    with keeping():  # این پیام (فاکتور) همیشه توی چت می‌مونه
        m1 = await bot.send_message(chat_id, text1)
    m2 = await bot.send_message(chat_id, text2, reply_markup=menu_kb())
    # هر پیام قدیمی که توی ردگیری نبود (مثلاً بعد از ری‌استارت ربات) هم جارو بشه
    await sweep_range(bot, chat_id, m2.message_id, {m1.message_id, m2.message_id})


ORDERS_CHAT_ID = int(os.environ.get("ORDERS_CHAT_ID", "0") or 0)  # کانال/گروه پرسنل
READY_CHAT_ID = int(os.environ.get("READY_CHAT_ID", "0") or 0)  # گروه «آماده ارسال»


def staff_order_text(inv):
    name, _ = MODELS.get(inv["model"], (inv["model"], 0))
    ship = "پس‌کرایه (به عهدهٔ گیرنده)" if is_bulk(inv["model"]) else "ارسال رایگان"
    lines = [
        f"📦 <b>سفارش جدید</b> — <code>{inv['invoice_no']}</code>",
        LINE,
        f"🛍 مدل: <b>{escape(name)}</b>",
        f"🔢 تعداد: <b>{fa(inv['qty'])} عدد</b>",
        f"🎨 رنگ: <b>سفید</b>",
        f"🚚 {ship}",
        LINE,
        f"👤 {escape(inv['full_name'] or '—')}",
        f"📞 {escape(inv['phone'] or '—')}",
    ]
    if inv.get("landline"):
        lines.append(f"☎️ {escape(inv['landline'])}")
    lines.append(f"📍 {escape(inv['address'] or '—')}")
    lines.append(f"📮 کدپستی: {escape(inv['postal'] or '—')}")
    if inv.get("emergency"):
        lines.append(f"🆘 {escape(inv['emergency'])}")
    return "\n".join(lines)


def staff_kb(inv):
    """دکمه‌ها بر اساس وضعیت فعلی آماده‌سازی."""
    no = inv["invoice_no"]
    st = inv.get("prep_status") or "new"
    if st == "new":
        rows = [[InlineKeyboardButton(text="🔒 شروع آماده‌سازی این سفارش", callback_data=f"pstart:{no}")]]
    elif st == "preparing":
        rows = [[
            InlineKeyboardButton(text="✅ آماده ارسال شد", callback_data=f"pdone:{no}"),
            InlineKeyboardButton(text="↩️ رها کردن", callback_data=f"prel:{no}"),
        ]]
    else:
        return None  # تمام‌شده: بدون دکمه
    return InlineKeyboardMarkup(inline_keyboard=rows)


def staff_full_text(inv):
    st = inv.get("prep_status") or "new"
    who = escape(inv.get("prep_by_name") or "—")
    tail = {
        "new": "🟢 <b>آزاد</b> — منتظر برداشتن",
        "preparing": f"🟡 <b>در حال آماده‌سازی توسط {who}</b>",
        "done": f"📦 <b>آماده ارسال</b> — {who}",
    }[st]
    return f"{staff_order_text(inv)}\n{LINE}\n{tail}"


async def post_order_to_staff(bot, no):
    """سفارش تأییدشده رو توی گروه پرسنل می‌فرسته؛ اگه نشد، علتش رو به ادمین می‌گه."""
    problem = None
    if not ORDERS_CHAT_ID:
        problem = "متغیر ORDERS_CHAT_ID توی Render تنظیم نشده."
    else:
        inv = (await run_db(get_invoice, int(no)))
        if inv is None:
            return
        try:
            text = staff_full_text(inv)
            path = model_photo(inv["model"])
            if path and len(text) <= 1000:
                try:
                    msg = await bot.send_photo(
                        ORDERS_CHAT_ID, _photo_input(path), caption=text, reply_markup=staff_kb(inv)
                    )
                    _remember_photo(path, msg)
                    return
                except Exception:
                    log.exception("staff photo send failed; falling back to text")
            await bot.send_message(ORDERS_CHAT_ID, text, reply_markup=staff_kb(inv))
            return
        except Exception as e:
            log.exception("post_order_to_staff failed")
            problem = f"{type(e).__name__}: {e}"
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ سفارش {no} به گروه پرسنل ارسال نشد.\n{problem}",
            )
        except Exception:
            pass


def ready_order_text(inv):
    """متن سفارش آماده‌شده برای گروه «آماده ارسال»، با مشخصات آماده‌کننده (برای ثبت کارکرد و مسئولیت)."""
    who = escape(inv.get("prep_by_name") or "—")
    if inv.get("prep_by"):
        who = f'<a href="tg://user?id={inv["prep_by"]}">{who}</a>'
    when = ""
    if inv.get("prep_done_at"):
        when = f"\n🕒 {jdate(inv['prep_done_at'])} — {jtime(inv['prep_done_at'])}"
    return (
        f"{staff_order_text(inv).replace('سفارش جدید', 'آماده ارسال', 1)}\n{LINE}\n"
        f"👷 آماده‌کننده: <b>{who}</b>{when}"
    )


async def post_order_to_ready(bot, inv):
    """سفارش آماده‌شده رو به گروه «آماده ارسال» می‌فرسته. True یعنی با موفقیت رسید."""
    if not READY_CHAT_ID:
        return False
    try:
        text = ready_order_text(inv)
        path = model_photo(inv["model"])
        if path and len(text) <= 1000:
            try:
                msg = await bot.send_photo(READY_CHAT_ID, _photo_input(path), caption=text)
                _remember_photo(path, msg)
                return True
            except Exception:
                log.exception("ready photo send failed; falling back to text")
        await bot.send_message(READY_CHAT_ID, text)
        return True
    except Exception as e:
        log.exception("post_order_to_ready failed")
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ سفارش {inv['invoice_no']} به گروه آماده ارسال نرسید و توی گروه پرسنل موند.\n"
                    f"{type(e).__name__}: {e}",
                )
            except Exception:
                pass
        return False


def _prep_update(no, new_status, expect_status, user_id, name, require_owner=False):
    """
    تغییر وضعیت به‌صورت اتمیک: فقط وقتی انجام می‌شه که وضعیت فعلی همون expect_status باشه.
    اگه دو نفر هم‌زمان بزنن، دیتابیس فقط یه UPDATE رو موفق می‌کنه (rowcount=1)، بقیه ۰ می‌گیرن.
    """
    def work(cur):
        sql = "UPDATE invoices SET prep_status = %s"
        params = [new_status]
        if new_status == "preparing":
            sql += ", prep_by = %s, prep_by_name = %s, prep_started_at = now()"
            params += [user_id, name]
        elif new_status == "new":
            sql += ", prep_by = NULL, prep_by_name = NULL, prep_started_at = NULL"
        sql += " WHERE invoice_no = %s AND status = 'paid' AND prep_status = %s"
        params += [no, expect_status]
        if require_owner:
            sql += " AND prep_by = %s"
            params.append(user_id)
        cur.execute(sql, params)
        return cur.rowcount == 1

    return tx(work)


async def _refresh_staff_msg(call, no):
    inv = (await run_db(get_invoice, no))
    if inv is None:
        return
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=staff_full_text(inv), reply_markup=staff_kb(inv))
        else:
            await call.message.edit_text(staff_full_text(inv), reply_markup=staff_kb(inv))
    except TelegramBadRequest:
        pass  # message is not modified


def _staff_ok(call):
    return bool(ORDERS_CHAT_ID) and call.message is not None and call.message.chat.id == ORDERS_CHAT_ID


@dp.callback_query(F.data.startswith("pstart:"))
async def prep_start(call: CallbackQuery):
    if not _staff_ok(call):
        await call.answer()
        return
    no = int(call.data.split(":", 1)[1])
    name = call.from_user.full_name or "پرسنل"
    if (await run_db(_prep_update, no, "preparing", "new", call.from_user.id, name)):
        await call.answer("این سفارش برای شما قفل شد ✅")
    else:
        inv = (await run_db(get_invoice, no))
        owner = (inv or {}).get("prep_by_name") or "یکی دیگه"
        await call.answer(f"🔒 این سفارش رو {owner} برداشته؛ سراغ فاکتور دیگه برو.", show_alert=True)
    await _refresh_staff_msg(call, no)


@dp.callback_query(F.data.startswith("pdone:"))
async def prep_done(call: CallbackQuery):
    if not _staff_ok(call):
        await call.answer()
        return
    no = int(call.data.split(":", 1)[1])
    inv = (await run_db(get_invoice, no))
    is_owner = inv is not None and inv.get("prep_by") == call.from_user.id
    if not (is_owner or (ADMIN_ID and call.from_user.id == ADMIN_ID)):
        await call.answer("⛔️ فقط کسی که این سفارش رو برداشته می‌تونه تأیید کنه.", show_alert=True)
        return
    def work(cur):
        cur.execute(
            "UPDATE invoices SET prep_status = 'done', prep_done_at = now()"
            " WHERE invoice_no = %s AND prep_status = 'preparing'",
            (no,),
        )
        return cur.rowcount == 1
    if not (await run_db(tx, work)):
        await call.answer("این سفارش دیگه در حال آماده‌سازی نیست.", show_alert=True)
        await _refresh_staff_msg(call, no)
        return
    await call.answer("📦 ثبت شد: آماده ارسال")
    done_inv = (await run_db(get_invoice, no))
    # اول به گروه «آماده ارسال» می‌فرستیم؛ فقط اگه رسید از گروه پرسنل حذف می‌شه، تا سفارشی گم نشه
    if done_inv is not None and await post_order_to_ready(call.bot, done_inv):
        try:
            await call.message.delete()
            return
        except Exception:
            log.exception("could not delete staff message")
    await _refresh_staff_msg(call, no)


@dp.callback_query(F.data.startswith("prel:"))
async def prep_release(call: CallbackQuery):
    if not _staff_ok(call):
        await call.answer()
        return
    no = int(call.data.split(":", 1)[1])
    inv = (await run_db(get_invoice, no))
    is_owner = inv is not None and inv.get("prep_by") == call.from_user.id
    if not (is_owner or (ADMIN_ID and call.from_user.id == ADMIN_ID)):
        await call.answer("⛔️ فقط کسی که این سفارش رو برداشته می‌تونه رهاش کنه.", show_alert=True)
        return
    def work(cur):
        cur.execute(
            "UPDATE invoices SET prep_status = 'new', prep_by = NULL, prep_by_name = NULL, prep_started_at = NULL"
            " WHERE invoice_no = %s AND prep_status = 'preparing'",
            (no,),
        )
        return cur.rowcount == 1
    (await run_db(tx, work))
    await call.answer("سفارش دوباره آزاد شد")
    await _refresh_staff_msg(call, no)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_decision(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True)
        return
    _, action, no = call.data.split(":")
    inv = (await run_db(get_invoice, int(no)))
    if inv is None or inv["status"] != "reviewing":
        await call.answer("این فیش قبلاً بررسی شده.", show_alert=True)
        return
    old = call.message.html_text or ""
    if action == "ok":
        (await run_db(set_status, inv["invoice_no"], "paid"))
        mark = "✅ <b>تأیید شد</b>"
    else:
        (await run_db(set_status, inv["invoice_no"], "rejected"))
        mark = "❌ <b>رد شد</b>"
    try:
        await call.message.edit_caption(caption=f"{old}\n\n{mark}", reply_markup=None)
    except Exception:
        pass
    if action == "ok":
        # اول گروه پرسنل؛ حتی اگه پیام مشتری نرسه، سفارش گم نشه
        await post_order_to_staff(call.bot, inv["invoice_no"])
    try:
        if action == "ok":
            await send_success(inv["user_id"], inv["invoice_no"], bot=call.bot)
        else:
            await purge(call.bot, inv["user_id"])
            await call.bot.send_message(
                inv["user_id"],
                f"❌ فیش ارسالی برای فاکتور {inv['invoice_no']} تأیید نشد.\n"
                "اگه واریز انجام شده، لطفاً با پشتیبانی در تماس باش.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)],
                        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main")],
                    ]
                ),
            )
    except Exception:
        await call.message.answer("⚠️ پیام به مشتری ارسال نشد (شاید ربات رو بلاک کرده).")
    await call.answer()


@dp.callback_query(F.data.startswith("paywallet:"))
async def pay_wallet(call: CallbackQuery, state: FSMContext):
    no = call.data.split(":", 1)[1]
    inv = (await run_db(own_invoice, no, call.from_user.id))
    if inv is None:
        await call.answer("این فاکتور دیگه معتبر نیست.", show_alert=True)
        return
    no = inv["invoice_no"]
    if not await show_payment(call, no, only_if_changed=True):
        await call.answer()
        return
    balance = (await run_db(get_balance, call.from_user.id))
    paid = False if balance < inv["total"] else (await run_db(pay_with_wallet, no, call.from_user.id, inv["total"]))
    if paid == "nostock":
        await show_payment(call, no)
        await call.answer()
        return
    if not paid:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"paycard:{no}")],
                [InlineKeyboardButton(text="💰 افزایش موجودی", callback_data="wallet_add_pay")],
                nav_row(f"pay:{no}", f"invmain:{no}"),
            ]
        )
        await show(
            call,
            "❌ <b>موجودی کیف پول کافی نیست</b>\n"
            f"{LINE}\n"
            f"💰 موجودی شما: {fa(balance)} تومن\n"
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
    await post_order_to_staff(call.bot, no)
    if ADMIN_ID:
        try:
            with keeping():
                await call.bot.send_message(
                    ADMIN_ID,
                    f"🛎 <b>سفارش جدید (پرداخت از کیف پول)</b>\n{LINE}\n" + order_details((await run_db(get_invoice, no))),
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
        await err(message, "فرمت: /addcredit آیدی_عددی_مشتری مبلغ_به_تومن\nمثال: /addcredit 123456789 500000")
        return
    balance = (await run_db(add_credit, uid, amount))
    await err(message, f"✅ {fa(amount)} تومن اعتبار اضافه شد.\n💰 موجودی جدید: {fa(balance)} تومن")
    try:
        await message.bot.send_message(uid, f"🎁 {fa(amount)} تومن اعتبار به کیف پولت اضافه شد.")
    except Exception:
        pass


def stock_report():
    lines = ["📦 <b>موجودی انبار</b>", LINE]
    for key in STOCK_MODELS:
        left = stock_available(key)
        lines.append(f"{MODELS[key][0]}: " + ("♾ بدون محدودیت" if left is None else f"<b>{fa(left)}</b> عدد"))
    lines += [
        LINE,
        "ℹ️ مدل‌های عمده از موجودی همون مدل تکی کم می‌کنن.\n"
        f"فاکتورِ منتظر فیش تا {fa(PAY_DEADLINE_MIN)} دقیقه موجودی رو رزرو نگه می‌داره؛ "
        "فاکتورهای در حال بررسی و پرداخت‌شده همیشه کم می‌شن.",
    ]
    return "\n".join(lines)


STOCK_ALIASES = {"پرو": "pro", "پرومکس": "promax", "پرمیوم": "premium"}


def stock_apply(base, n, mode):
    """mode: add = اضافه | sub = کم کردن | set = «از الان n عدد موجوده».
    خروجی: (وضعیت، باقی‌مونده) — وضعیت: ok / unlimited / short"""

    def work(cur):
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (STOCK_LOCK,))
        used = _stock_used(cur, base)
        cur_qty = _stock_row(cur, base)
        if mode == "sub":
            if cur_qty is None:
                return "unlimited", None
            left = max(0, cur_qty - used)
            if n > left:
                return "short", left
            new = cur_qty - n
        elif mode == "add":
            new = (used if cur_qty is None else max(cur_qty, used)) + n
        else:
            new = used + n
        cur.execute(
            "INSERT INTO stock (model, qty) VALUES (%s, %s)"
            " ON CONFLICT (model) DO UPDATE SET qty = EXCLUDED.qty",
            (base, new),
        )
        return "ok", new - used

    return tx(work)


# ---- پنل ساده: /stock رو بزن، مدل رو انتخاب کن، فقط عدد بفرست
class StockAdd(StatesGroup):
    qty = State()


def stock_models_kb():
    rows = [[InlineKeyboardButton(text=MODELS[k][0], callback_data=f"stk:{k}")] for k in STOCK_MODELS]
    rows.append(menu_kb().inline_keyboard[0])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("stk:"))
async def stock_pick(call: CallbackQuery, state: FSMContext):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    key = call.data.split(":", 1)[1]
    if key == "menu":
        await state.clear()
        await show(call, (await run_db(stock_report, )) + "\n\n👇 مدلی که می‌خوای موجودیش رو تغییر بدی انتخاب کن:", stock_models_kb())
        await call.answer()
        return
    if key not in STOCK_MODELS:
        await call.answer()
        return
    await state.set_state(StockAdd.qty)
    await state.update_data(stock_model=key)
    await call.message.answer(
        f"📦 <b>{MODELS[key][0]}</b>\n"
        f"{LINE}\n"
        "تعداد جدید رو بفرست:\n\n"
        "➕ فقط عدد = اضافه به موجودی\n"
        "مثال: <code>50</code>\n\n"
        "➖ عدد و بعدش down = کم کردن از موجودی\n"
        "مثال: <code>50 down</code>\n\n"
        "(برای انصراف دوباره /stock رو بزن)"
    )
    await call.answer()


@dp.message(StockAdd.qty)
async def stock_qty(message: Message, state: FSMContext):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    m = re.fullmatch(r"\s*([0-9۰-۹٠-٩]+)\s*(down|dn|کم)?\s*", (message.text or "").lower())
    n = to_int(m.group(1)) if m else None
    if n is None or n < 1 or n > 1_000_000:
        await err(message, "فقط عدد بفرست؛ مثلاً 50 برای اضافه کردن یا 50 down برای کم کردن")
        return
    data = await state.get_data()
    base = data.get("stock_model")
    if base not in STOCK_MODELS:
        await state.clear()
        return
    status, left = (await run_db(stock_apply, base, n, "sub" if m.group(2) else "add"))
    if status == "unlimited":
        await err(message, "این مدل الان «بدون محدودیت» هست و چیزی برای کم کردن نداره. اول با یه عدد ساده موجودیش رو تعیین کن.")
        return
    if status == "short":
        await err(message, f"فقط {left} عدد موجودی داره؛ بیشتر از این نمی‌شه کم کرد.")
        return
    await state.clear()
    await message.answer((await run_db(stock_report, )), reply_markup=stock_models_kb())


# ---------------------------------------------------------------- کیف پول
WALLET_MIN = 50_000  # تومن
WALLET_MAX = 10_000_000


class Wallet(StatesGroup):
    amount = State()
    receipt = State()


def touch_user(uid):
    q_exec(
        "INSERT INTO users (user_id, created_at)"
        " VALUES (%s, COALESCE((SELECT MIN(created_at) FROM invoices WHERE user_id = %s), now()))"
        " ON CONFLICT (user_id) DO NOTHING",
        (uid, uid),
    )


def create_topup(uid, amount):
    row = q_one("INSERT INTO topups (user_id, amount) VALUES (%s, %s) RETURNING id", (uid, amount))
    return row["id"]


def topup_get(tid, uid, statuses):
    try:
        row = q_one("SELECT * FROM topups WHERE id = %s", (int(tid),))
    except (TypeError, ValueError):
        return None
    if row is None or row["user_id"] != uid or row["status"] not in statuses:
        return None
    return row


def wallet_text(user, status):
    uid = user.id
    touch_user(uid)
    since = q_one("SELECT created_at FROM users WHERE user_id = %s", (uid,))
    member = get_member(uid)
    paid = q_one("SELECT COUNT(*) AS n FROM invoices WHERE user_id = %s AND status = 'paid'", (uid,))
    now = datetime.now(timezone.utc)
    phone = escape(member["phone"]) if member else "🔴 ارسال نشده است 🔴"
    group = "👑 عضو باشگاه VIP" if status == "active" else "عادی"
    return (
        f"🗂 <b>اطلاعات حساب کاربری شما:</b>\n"
        f"{LINE}\n"
        f"🆔 شناسه کاربری: <code>{uid}</code>\n"
        f"👤 نام: {escape(user.full_name or '—')}\n"
        f"📞 شماره تماس: {phone}\n"
        f"🕒 زمان ثبت نام: {jdate(since['created_at'])} {jtime(since['created_at'])}\n"
        f"💰 موجودی: <b>{fa(get_balance(uid))} تومان</b>\n"
        f"🛍 تعداد سفارش‌های پرداخت‌شده: {fa(int(paid['n']))} عدد\n"
        f"👑 گروه کاربری: {group}\n"
        f"{LINE}\n"
        f"📅 {jdate(now)} → ⏰ {jtime(now)}"
    )


def wallet_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎁 کد هدیه", callback_data="walletgift"),
                InlineKeyboardButton(text="💰 افزایش موجودی", callback_data="wallet_add"),
            ],
            [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main")],
        ]
    )


def wallet_back_kb(to="wallet"):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ بازگشت", callback_data=to)]]
    )


@dp.callback_query(F.data == "wallet")
async def wallet_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    status = await club_status(call.bot, call.from_user.id)
    await show(call, (await run_db(wallet_text, call.from_user, status)), wallet_kb())
    await call.answer()


@dp.callback_query(F.data.in_({"wallet_add", "wallet_add_pay"}))
async def wallet_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(Wallet.amount)
    await show(
        call,
        "💵 <b>مبلغ را به تومان وارد کنید:</b>\n"
        f"✅ حداقل مبلغ {fa(WALLET_MIN)} و حداکثر مبلغ {fa(WALLET_MAX)} تومان می‌باشد",
        wallet_back_kb(),
    )
    await call.answer()


@dp.message(Wallet.amount, F.text)
async def wallet_receive_amount(message: Message, state: FSMContext):
    raw = message.text.strip().translate(EN_DIGITS)
    for ch in ",٬، ":
        raw = raw.replace(ch, "")
    amount = to_int(raw) if len(raw) <= 12 else None
    if amount is None:
        await err(message, "❌ لطفاً فقط مبلغ رو به‌صورت عدد بفرست، مثلاً 1000000", reply_markup=wallet_back_kb())
        return
    if not (WALLET_MIN <= amount <= WALLET_MAX):
        await err(
            message,
            f"❌ مبلغ باید بین {fa(WALLET_MIN)} تا {fa(WALLET_MAX)} تومان باشه.",
            reply_markup=wallet_back_kb(),
        )
        return
    tid = (await run_db(create_topup, message.from_user.id, amount))
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"wcard:{tid}")],
            [InlineKeyboardButton(text="❌ بستن لیست", callback_data="wallet")],
        ]
    )
    await message.answer(
        f"💵 <b>روش پرداخت خود را انتخاب نمایید</b>\n💰 مبلغ: <b>{fa(amount)} تومان</b>",
        reply_markup=kb,
    )


@dp.message(Wallet.amount)
async def wallet_amount_wrong(message: Message):
    await err(message, "❌ لطفاً مبلغ رو به‌صورت عدد بفرست.", reply_markup=wallet_back_kb())


@dp.callback_query(F.data.startswith("wcard:"))
async def wallet_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    t = (await run_db(topup_get, call.data.split(":", 1)[1], call.from_user.id, ("pending", "awaiting_receipt")))
    if t is None:
        await call.answer("این درخواست دیگه معتبر نیست.", show_alert=True)
        return
    amount = int(t["amount"])
    text = en(
        "💳 <b>پرداخت از طریق کارت به کارت</b>\n\n"
        "لطفاً مبلغ زیر رو دقیقاً به کارت زیر واریز کن:\n\n"
        f"🏦 بانک: {CARD_BANK}\n"
        f"👤 به نام: <b>{CARD_OWNER}</b>\n"
        f"💳 شماره کارت:\n{CARD_NUMBER}\n\n"
        f"💰 مبلغ: <b>{amount * 10:,} ریال</b>\n\n"
        "بعد از واریز، روی «ارسال رسید» بزن و عکس فیش رو بفرست.\n"
        f"⏳ مهلت واریز و ارسال فیش: {PAY_DEADLINE_MIN} دقیقه — بعد از اتمام مهلت، درخواست شارژ لغو می‌شه.\n"
        "✅ بعد از ارسال فیش دیگه محدودیت زمانی نداری."
    )
    rows = []
    if CopyTextButton is not None:
        rows.append(
            [
                InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=CARD_NUMBER)),
                InlineKeyboardButton(text="📋 کپی مبلغ", copy_text=CopyTextButton(text=str(amount * 10))),
            ]
        )
    rows.append([InlineKeyboardButton(text="✅ پرداخت کردم | ارسال رسید", callback_data=f"wsend:{t['id']}")])
    rows.append([InlineKeyboardButton(text="↩️ بازگشت", callback_data="wallet")])
    await show(call, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("wsend:"))
async def wallet_send(call: CallbackQuery, state: FSMContext):
    t = (await run_db(topup_get, call.data.split(":", 1)[1], call.from_user.id, ("pending", "awaiting_receipt")))
    if t is None:
        await call.answer("این درخواست دیگه معتبر نیست.", show_alert=True)
        return
    (await run_db(q_exec, "UPDATE topups SET status = 'awaiting_receipt' WHERE id = %s AND status = 'pending'", (t["id"],)))
    await state.set_state(Wallet.receipt)
    await state.update_data(topup=t["id"])
    await show(
        call,
        f"📸 <b>ارسال رسید</b>\n{LINE}\nعکس فیش واریزی (یا متن کامل رسید) رو همین‌جا بفرست.",
        wallet_back_kb(f"wcard:{t['id']}"),
    )
    await call.answer()


@dp.message(Wallet.receipt, F.photo | F.document | (F.text & ~F.text.startswith("/")))
async def wallet_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    t = (await run_db(topup_get, data.get("topup"), message.from_user.id, ("awaiting_receipt",)))
    if t is None:
        await state.clear()
        await message.answer(INVALID_TEXT, reply_markup=(await run_db(main_menu, message.from_user.id)))
        return
    if not ADMIN_ID:
        await err(message, "⚠️ مشکل فنی پیش اومد. لطفاً با پشتیبانی تماس بگیر.")
        return
    if message.document and not (message.document.mime_type or "").lower().startswith("image/"):
        await err(message, "📸 لطفاً فیش رو به صورت عکس یا اسکرین‌شات بفرست.", reply_markup=support_kb())
        return
    if message.text and (len(message.text.strip()) < 6 or not re.search(r"[\d۰-۹]", message.text)):
        await err(message, "🧾 لطفاً عکس فیش یا متن کامل رسید (شامل مبلغ و ساعت یا شماره پیگیری) رو بفرست.")
        return
    if (await run_db(q_exec, "UPDATE topups SET status = 'reviewing' WHERE id = %s AND status = 'awaiting_receipt'", (t["id"],))) != 1:
        await state.clear()
        return
    uname = message.from_user.username
    caption = (
        f"💰 <b>فیش افزایش موجودی</b>\n{LINE}\n"
        f"🔖 شناسه درخواست: <code>{t['id']}</code>\n"
        f"💰 مبلغ: <b>{fa(int(t['amount']))} تومان</b>\n"
        f"👤 {escape(message.from_user.full_name or '—')}\n"
        f"🆔 آیدی تلگرام: <code>{message.from_user.id}</code>"
        + (f"\n🔗 @{escape(uname)}" if uname else "")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تایید", callback_data=f"wadm:ok:{t['id']}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"wadm:no:{t['id']}"),
            ]
        ]
    )
    try:
        with keeping():
            await send_receipt_to_admin(message, caption, kb)
    except Exception:
        (await run_db(q_exec, "UPDATE topups SET status = 'awaiting_receipt' WHERE id = %s", (t["id"],)))
        await err(message, "⚠️ ارسال فیش انجام نشد. لطفاً دوباره بفرست یا با پشتیبانی تماس بگیر.")
        return
    await state.clear()
    await message.answer(
        "✅ فیش شما دریافت شد و در حال بررسیه.\nنتیجه همین‌جا بهت اعلام می‌شه.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet")],
                [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main")],
            ]
        ),
    )


@dp.message(Wallet.receipt)
async def wallet_receipt_wrong(message: Message):
    await err(message, "📸 لطفاً عکس فیش واریزی یا متن رسید رو بفرست.", reply_markup=wallet_back_kb())


@dp.callback_query(F.data.startswith("wadm:"))
async def wallet_admin(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True)
        return
    _, action, tid = call.data.split(":")
    t = (await run_db(q_one, "SELECT * FROM topups WHERE id = %s", (int(tid),)))
    new = "paid" if action == "ok" else "rejected"
    if t is None or (await run_db(q_exec, 
        "UPDATE topups SET status = %s WHERE id = %s AND status = 'reviewing'", (new, t["id"])
    )) != 1:
        await call.answer("این فیش قبلاً بررسی شده.", show_alert=True)
        return
    uid, amount = t["user_id"], int(t["amount"])
    balance = (await run_db(add_credit, uid, amount)) if action == "ok" else None
    mark = "✅ <b>تأیید شد</b>" if action == "ok" else "❌ <b>رد شد</b>"
    old = call.message.html_text or ""
    try:
        await call.message.edit_caption(caption=f"{old}\n\n{mark}", reply_markup=None)
    except Exception:
        try:
            await call.message.edit_text(f"{old}\n\n{mark}", reply_markup=None)
        except Exception:
            pass
    try:
        await purge(call.bot, uid)
        if action == "ok":
            text = f"✅ مبلغ {fa(amount)} تومان به کیف پولت اضافه شد.\n💰 موجودی جدید: <b>{fa(balance)} تومان</b>"
        else:
            text = (
                f"❌ فیش ارسالی برای افزایش موجودی ({fa(amount)} تومان) تأیید نشد.\n"
                "اگه واریز انجام شده، لطفاً با پشتیبانی در تماس باش."
            )
        await call.bot.send_message(
            uid,
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet")],
                    [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main")],
                ]
            ),
        )
    except Exception:
        await call.message.answer("⚠️ پیام به مشتری ارسال نشد (شاید ربات رو بلاک کرده).")
    await call.answer()


# ---------------------------------------------------------------- باشگاه مشتریان
def club_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👑 ورود به کانال کلاب ویهانی", url=CLUB_URL)],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="main")],
        ]
    )


def club_welcome_text():
    return CLUB_TEXT


def norm_contact_phone(raw):
    d = (raw or "").translate(EN_DIGITS).replace("+", "").replace(" ", "").replace("-", "")
    if d.startswith("98") and len(d) == 12:
        d = "0" + d[2:]
    return d


@dp.callback_query(F.data == "vip_club")
async def vip_club(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if (await run_db(get_member, call.from_user.id)):
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
        await err(message, "❌ فقط شمارهٔ خودت قابل قبوله. لطفاً با دکمهٔ «ارسال شماره من» بفرست.")
        return
    phone = norm_contact_phone(c.phone_number)
    if not phone:
        await err(message, "❌ شماره معتبر نبود. دوباره تلاش کن.")
        return
    (await run_db(save_member, message.from_user.id, phone))
    await state.clear()
    await message.answer("✅ شمارهٔ شما ثبت شد.", reply_markup=ReplyKeyboardRemove())
    await message.answer(club_welcome_text(), reply_markup=club_kb())


@dp.message(Club.contact, F.text == CLUB_CANCEL_BTN)
async def club_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("باشه 👌", reply_markup=ReplyKeyboardRemove())
    await message.answer(MAIN_TEXT, reply_markup=(await run_db(main_menu, message.from_user.id)))


@dp.message(Club.contact)
async def club_wrong_input(message: Message):
    await err(message, "📱 لطفاً با دکمهٔ «ارسال شماره من» شمارهٔ خودت رو بفرست.")


@dp.callback_query(F.data == "campaigns")
async def campaigns(call: CallbackQuery, state: FSMContext):
    await state.clear()
    status = await club_status(call.bot, call.from_user.id)
    if status == "active":
        await show(call, CAMPAIGNS_TEXT, InlineKeyboardMarkup(inline_keyboard=[nav_row("main")]))
    elif status == "left":
        await show(call, CLUB_LEFT_TEXT, club_kb())
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
    inv = (await run_db(get_invoice, no)) if no is not None else None
    if inv is None or inv["user_id"] != message.from_user.id:
        await err(
            message,
            "❌ فاکتوری با این شناسه پیدا نشد.\n"
            "شناسه رو دوباره بررسی کن و بفرست.",
            reply_markup=menu_kb(),
        )
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[nav_row("main")])
    await message.answer(invoice_text(inv), reply_markup=kb)


# ---------------------------------------------------------------- فاکتورهای من
def to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def jdate(dt):
    """تاریخ شمسی (به وقت تهران) مثل ۱۴۰۵/۰۸/۰۹"""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc) + timedelta(hours=3, minutes=30)
    jy, jm, jd = to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}".translate(FA_DIGITS)


def jtime(dt):
    """ساعت (به وقت تهران) مثل ۱۹:۰۵:۱۷"""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc) + timedelta(hours=3, minutes=30)
    return dt.strftime("%H:%M:%S").translate(FA_DIGITS)


MYINV_PER_PAGE = 8
SHORT_STATUS = {
    "pending": "🟠 در انتظار",
    "awaiting_receipt": "🟡 منتظر فیش",
    "reviewing": "🔵 در بررسی",
    "paid": "🟢 پرداخت شد",
    "rejected": "⛔ رد شد",
    "cancelled": "🔴 لغو شد",
}


def my_invoices(user_id, only_open=False):
    extra = f" AND {OPEN_SQL}" if only_open else ""
    return q_all(
        "SELECT invoice_no, total, status, created_at FROM invoices"
        f" WHERE user_id = %s{extra} ORDER BY created_at DESC, seq DESC LIMIT 200",
        (user_id,),
    )


def short_jdate(dt):
    return jdate(dt)[2:]  # ۱۴۰۵/۰۸/۰۹ -> ۰۵/۰۸/۰۹


KIND_TITLES = {
    "paid": "✅ <b>فاکتورهای پرداخت‌شده</b>",
    "cancelled": "🔴 <b>فاکتورهای لغوشده</b>",
    "open": "📝 <b>فاکتورهای نیمه‌تمام</b>",
}
KIND_ACTIONS = {
    "paid": ("🔁 سفارش مجدد", "reorder"),
    "cancelled": ("♻️ فعال کردن فاکتور و پرداخت", "resume"),
    "open": ("✅ ادامه خرید", "resume"),
}
KIND_EMPTY = {
    "paid": "فاکتور پرداخت‌شده‌ای نداری.",
    "cancelled": "فاکتور لغوشده‌ای نداری.",
    "open": "فاکتور نیمه‌تمامی نداری.",
}
FILTERED_PER_PAGE = 8


def _kind_match(kind, status):
    if kind == "paid":
        return status == "paid"
    if kind == "cancelled":
        return status == "cancelled"
    if kind == "open":
        return status in PAYABLE
    return True


def myinv_page(user_id, page, kind=None):
    """kind: None = همهٔ فاکتورها | paid / cancelled / open = فقط همون دسته (با دکمهٔ اقدام زیر هر فاکتور)."""
    all_rows = my_invoices(user_id)
    rows = [r for r in all_rows if _kind_match(kind, r["status"])]
    if not rows:
        text = (
            (KIND_TITLES[kind] if kind else "📋 <b>فاکتورهای من</b>")
            + f"\n{LINE}\n"
            + (KIND_EMPTY[kind] if kind else "هنوز فاکتوری ثبت نکردی.\nبرای شروع از منوی اصلی «خرید تکی» رو بزن.")
        )
        back = [[InlineKeyboardButton(text="🔙 فاکتورهای من", callback_data="myinv:0")]] if kind else []
        return text, InlineKeyboardMarkup(inline_keyboard=back + [menu_kb().inline_keyboard[0]])
    per = FILTERED_PER_PAGE if kind else MYINV_PER_PAGE
    pages = (len(rows) + per - 1) // per
    page = max(0, min(page, pages - 1))
    chunk = rows[page * per : (page + 1) * per]
    if kind:
        title = KIND_TITLES[kind]
        legend = ""
        hint = "👇 برای دیدن فاکتور و ادامهٔ اون، روی هر ردیف بزن."
    else:
        title = "📋 <b>لیست فاکتورها</b>"
        legend = "🟢 پرداخت شده\n🔴 لغو شده\n🟠 در انتظار پرداخت\n🔵 فیش در حال بررسی\n\n"
        hint = "👇 یکی از دسته‌ها رو بزن، یا روی هر ردیف بزن تا فاکتور رو ببینی."
    text = (
        f"{title}\n"
        f"{LINE}\n"
        f"{legend}"
        f"{hint}"
        + (f"\n📄 صفحهٔ {fa(page + 1)} از {fa(pages)}" if pages > 1 else "")
    )
    kb_rows = []
    if not kind:
        cnt = {k: sum(1 for r in all_rows if _kind_match(k, r["status"])) for k in KIND_TITLES}
        kb_rows.append(
            [
                InlineKeyboardButton(text=f"✅ پرداخت شده ({cnt['paid']})", callback_data="myf:paid:0"),
                InlineKeyboardButton(text=f"🔴 لغو شده ({cnt['cancelled']})", callback_data="myf:cancelled:0"),
                InlineKeyboardButton(text=f"📝 نیمه‌تمام ({cnt['open']})", callback_data="myf:open:0"),
            ]
        )
    kb_rows.append(
        [
            InlineKeyboardButton(text="📌 وضعیت", callback_data="noop"),
            InlineKeyboardButton(text="💰 مبلغ", callback_data="noop"),
            InlineKeyboardButton(text="📅 تاریخ", callback_data="noop"),
            InlineKeyboardButton(text="🆔 شناسه", callback_data="noop"),
        ]
    )
    for r in chunk:
        cb = f"myinv_show:{r['invoice_no']}:{page}" + (f":{kind}" if kind else "")
        kb_rows.append(
            [
                InlineKeyboardButton(text=SHORT_STATUS.get(r["status"], r["status"]), callback_data=cb),
                InlineKeyboardButton(text=fa(r["total"]), callback_data=cb),
                InlineKeyboardButton(text=short_jdate(r["created_at"]), callback_data=cb),
                InlineKeyboardButton(text=str(r["invoice_no"]), callback_data=cb),
            ]
        )
    base = f"myf:{kind}" if kind else "myinv"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"{base}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"{base}:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    if kind:
        kb_rows.append([InlineKeyboardButton(text="🔙 فاکتورهای من", callback_data="myinv:0")])
    kb_rows.append(menu_kb().inline_keyboard[0])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("myinv:"))
async def my_invoices_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        page = int(call.data.split(":", 1)[1])
    except ValueError:
        page = 0
    text, kb = (await run_db(myinv_page, call.from_user.id, page))
    await show(call, text, kb)
    await call.answer()


@dp.callback_query(F.data.startswith("myf:"))
async def my_invoices_filtered(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        _, kind, page = call.data.split(":")
        page = int(page)
    except ValueError:
        await call.answer()
        return
    if kind not in KIND_TITLES:
        await call.answer()
        return
    text, kb = (await run_db(myinv_page, call.from_user.id, page, kind))
    await show(call, text, kb)
    await call.answer()


@dp.callback_query(F.data.startswith("openinv:"))
async def open_invoice_cb(call: CallbackQuery, state: FSMContext):
    """آخرین فاکتور نیمه‌تمام مشتری رو مستقیم باز می‌کنه."""
    await state.clear()
    no = (await run_db(latest_open_invoice, call.from_user.id))
    if no is None:
        await show(call, MAIN_TEXT, (await run_db(main_menu, call.from_user.id)))
        await call.answer("فاکتور نیمه‌تمامی نداری.")
        return
    await show_invoice_detail(call, no, 0)


@dp.callback_query(F.data.startswith("myinv_show:"))
async def my_invoice_detail(call: CallbackQuery):
    parts = call.data.split(":")
    try:
        no, page = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    kind = parts[3] if len(parts) > 3 and parts[3] in KIND_TITLES else None
    await show_invoice_detail(call, no, page, kind)


async def show_invoice_detail(call, no, page, kind=None):
    inv = (await run_db(get_invoice, no))
    if inv is None or inv["user_id"] != call.from_user.id:
        await call.answer("فاکتور پیدا نشد.", show_alert=True)
        return
    extra = f"\n📅 تاریخ ثبت: {jdate(inv['created_at'])}"
    rows = []
    if inv["status"] in PAYABLE or inv["status"] == "cancelled":
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ ادامه خرید" if inv["status"] in PAYABLE else "♻️ فعال کردن فاکتور و پرداخت",
                    callback_data=f"resume:{no}",
                )
            ]
        )
        extra += (
            "\n⏳ این سفارش هنوز نهایی نشده."
            if inv["status"] in PAYABLE
            else "\n🔴 این فاکتور لغو شده؛ اگه هنوز همین سفارش رو می‌خوای، «فعال کردن فاکتور و پرداخت» رو بزن "
            "(مبلغ با قیمت روز محاسبه می‌شه)."
        )
    elif inv["status"] == "paid":
        extra += "\n📬 کد رهگیری پست بعد از ارسال سفارش برات فرستاده می‌شه."
        rows.append([InlineKeyboardButton(text="🔁 سفارش مجدد", callback_data=f"reorder:{no}")])
    elif inv["status"] == "reviewing":
        extra += "\n⏳ فیش دریافت شده و منتظر تأیید هست."
    elif inv["status"] == "rejected":
        rows.append([InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)])
    back_cb = f"myf:{kind}:{page}" if kind else f"myinv:{page}"
    rows.append([InlineKeyboardButton(text="🔙 فاکتورهای من" if not kind else "🔙 بازگشت به لیست", callback_data=back_cb)])
    rows.append(menu_kb().inline_keyboard[0])
    await show(call, invoice_text(inv) + extra, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("resume:"))
async def resume_invoice(call: CallbackQuery, state: FSMContext):
    """فاکتور نیمه‌تمام یا لغوشده رو دوباره فعال می‌کنه و مشتری رو به مرحلهٔ بعدی می‌بره."""
    await state.clear()
    try:
        no = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    inv = (await run_db(get_invoice, no))
    if inv is None or inv["user_id"] != call.from_user.id or (
        inv["status"] not in PAYABLE and inv["status"] != "cancelled"
    ) or inv["model"] not in MODELS:
        await call.answer("این فاکتور قابل ادامه نیست.", show_alert=True)
        return
    # همیشه فقط یک فاکتور فعال داریم
    (await run_db(q_exec, 
        "UPDATE invoices SET status = 'cancelled'"
        " WHERE user_id = %s AND invoice_no <> %s AND status IN ('pending', 'awaiting_receipt')",
        (call.from_user.id, no),
    ))
    notice = ""
    if inv["status"] == "cancelled":
        # فاکتور لغوشده با قیمت روز و تخفیف فعلی باشگاه دوباره محاسبه می‌شه
        old_total = inv["total"]
        discount, _ = await club_discount(call.bot, call.from_user.id, inv["model"])
        unit = MODELS[inv["model"]][1] * 1000
        total = unit * inv["qty"] * (100 - discount) // 100
        (await run_db(q_exec, 
            "UPDATE invoices SET status = 'pending', unit_price = %s, total = %s, discount_percent = %s"
            " WHERE invoice_no = %s",
            (unit, total, discount, no),
        ))
        if total != old_total:
            notice = f"♻️ قیمت‌ها به‌روز شد (مبلغ قبلی: {fa(old_total)} تومن)\n\n"
    inv = (await run_db(get_invoice, no))
    if inv["full_name"] and inv["phone"] and inv["address"]:
        (await run_db(mark_payment, no))
        await show_payment(call, no, notice)
    else:
        await ask_step(call, state, "name", no)
    await call.answer()


def _reorder_source(call, no):
    inv = get_invoice(no)
    if inv is None or inv["user_id"] != call.from_user.id or inv["status"] != "paid" or inv["model"] not in MODELS:
        return None
    return inv


async def _reorder_create(call, inv):
    discount, _ = await club_discount(call.bot, call.from_user.id, inv["model"])
    new_no = (await run_db(create_invoice, call.from_user.id, inv["model"], inv["qty"], discount))
    for field in CUSTOMER_FIELDS:
        if inv.get(field):
            (await run_db(set_field, new_no, field, inv[field]))
    return new_no


@dp.callback_query(F.data.startswith("reorder:"))
async def reorder(call: CallbackQuery, state: FSMContext):
    """قبل از ساخت فاکتور جدید، آدرس و شمارهٔ قبلی رو برای تأیید نشون می‌ده."""
    await state.clear()
    try:
        no = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    inv = (await run_db(_reorder_source, call, no))
    if inv is None:
        await call.answer("این فاکتور قابل سفارش مجدد نیست.", show_alert=True)
        return
    name = MODELS[inv["model"]][0]
    text = (
        "🔁 <b>سفارش مجدد</b>\n"
        f"{LINE}\n"
        f"📦 {name} × {fa(inv['qty'])} — رنگ سفید\n"
        f"{LINE}\n"
        "⚠️ <b>قبل از پرداخت، مشخصات ارسال رو چک کن:</b>\n\n"
        f"👤 {escape(inv['full_name'] or '—')}\n"
        f"📞 {escape(inv['phone'] or '—')}\n"
        + (f"☎️ ثابت: {escape(inv.get('landline') or '—')}\n" if is_bulk(inv["model"]) else "")
        + f"📍 {escape(inv['address'] or '—')}\n"
        f"📮 کدپستی: {escape(inv['postal'] or '—')}\n"
        f"🆘 اضطراری: {escape(inv['emergency'] or '—')}\n"
        f"{LINE}\n"
        "اگه آدرس یا شماره‌ات عوض شده، «ویرایش مشخصات» رو بزن."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ درسته، ادامه پرداخت", callback_data=f"reorder_ok:{no}")],
            [InlineKeyboardButton(text="✏️ ویرایش مشخصات", callback_data=f"reorder_edit:{no}")],
            [InlineKeyboardButton(text="🔙 فاکتور قبلی", callback_data=f"myinv_show:{no}:0")],
            menu_kb().inline_keyboard[0],
        ]
    )
    await show(call, text, kb)
    await call.answer()


@dp.callback_query(F.data.startswith("reorder_ok:"))
async def reorder_ok(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        no = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    inv = (await run_db(_reorder_source, call, no))
    if inv is None:
        await call.answer("این فاکتور قابل سفارش مجدد نیست.", show_alert=True)
        return
    new_no = await _reorder_create(call, inv)
    (await run_db(mark_payment, new_no))
    await show_payment(call, new_no)
    await call.answer()


@dp.callback_query(F.data.startswith("reorder_edit:"))
async def reorder_edit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        no = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    inv = (await run_db(_reorder_source, call, no))
    if inv is None:
        await call.answer("این فاکتور قابل سفارش مجدد نیست.", show_alert=True)
        return
    new_no = await _reorder_create(call, inv)
    await ask_step(call, state, "name", new_no)
    await call.answer()


@dp.callback_query(F.data.in_({"free_sample", "partnership", "faq", "cod_home", "walletgift"}))
async def placeholder(call: CallbackQuery):
    await call.answer("این بخش به‌زودی فعال می‌شه 🙏", show_alert=True)


# ---------------------------------------------------------------- عکس خارج از مرحله فیش
@dp.message(StateFilter(None), F.photo | F.document)
async def stray_photo(message: Message):
    await message.answer(
        "⌛️ این مرحله منقضی شده. لطفاً از منوی اصلی دوباره فاکتور رو باز کن و روی «کارت به کارت» بزن.",
        reply_markup=(await run_db(main_menu, message.from_user.id)),
    )


@dp.message(Command("clubtest"))
async def club_test(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    lines = [f"CLUB_CHAT_ID = {CLUB_CHAT_ID}"]
    if not CLUB_CHAT_ID:
        lines.append("❌ متغیر CLUB_CHAT_ID توی Render ست نشده (یا صفره).")
    else:
        me = await message.bot.get_me()
        for label, uid in (("ربات", me.id), ("تو", message.from_user.id)):
            try:
                m = await message.bot.get_chat_member(CLUB_CHAT_ID, uid)
                lines.append(f"{label}: {m.status}")
            except Exception as e:
                lines.append(f"{label}: ❌ {type(e).__name__}: {e}")
        lines.append(f"نتیجهٔ نهایی: {await club_status(message.bot, message.from_user.id)}")
    await message.answer("<pre>" + escape("\n".join(lines)) + "</pre>")


@dp.message(Command("staffreport"))
async def staffreport_cmd(message: Message):
    """فقط مدیر: گزارش کامل پرسنل با انتخاب بازه. اگه توی گروه زده بشه، گزارش به پیوی مدیر می‌ره."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    text = (await run_db(staff_report_text, "1", detail=True))
    if message.chat.type == "private":
        await message.answer(text, reply_markup=report_kb())
        return
    try:
        await message.bot.send_message(ADMIN_ID, text, reply_markup=report_kb())
        await message.answer("📩 گزارش به پیوی مدیر ارسال شد.")
    except Exception:
        await message.answer("ابتدا ربات رو تو چت خصوصی استارت کن (/start) تا گزارش رو برات بفرستم.")


@dp.callback_query(F.data.startswith("srep:"))
async def staffreport_cb(call: CallbackQuery):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    period = call.data.split(":", 1)[1]
    try:
        await call.message.edit_text((await run_db(staff_report_text, period, detail=True)), reply_markup=report_kb())
    except TelegramBadRequest:
        pass  # message is not modified
    await call.answer()


@dp.message(Command("groupid"))
async def groupid(message: Message):
    """ادمین توی گروه پرسنل /groupid بزنه، آیدی گروه رو برای ORDERS_CHAT_ID می‌گیره."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await message.answer(f"آیدی این چت:\n<code>{message.chat.id}</code>")


@dp.message(Command("staffcheck"))
async def staffcheck(message: Message):
    """فقط ادمین، فقط توی چت خصوصی: تست ارسال به گروه پرسنل."""
    if message.chat.type != "private" or not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    if not ORDERS_CHAT_ID:
        await message.answer("❌ ORDERS_CHAT_ID توی Render تنظیم نشده.")
        return
    try:
        await message.bot.send_message(ORDERS_CHAT_ID, "✅ تست اتصال به گروه پرسنل")
        await message.answer(f"✅ ارسال به گروه پرسنل موفق بود (آیدی: {ORDERS_CHAT_ID}).")
    except Exception as e:
        await message.answer(f"❌ ارسال به گروه پرسنل نشد:\n{type(e).__name__}: {e}")
    if not READY_CHAT_ID:
        await message.answer("❌ READY_CHAT_ID توی Render تنظیم نشده.")
        return
    try:
        await message.bot.send_message(READY_CHAT_ID, "✅ تست اتصال به گروه آماده ارسال")
        await message.answer(f"✅ ارسال به گروه آماده ارسال موفق بود (آیدی: {READY_CHAT_ID}).")
    except Exception as e:
        await message.answer(f"❌ ارسال به گروه آماده ارسال نشد:\n{type(e).__name__}: {e}")


@dp.message()
async def fallback(message: Message):
    await message.answer("برای ادامه از دکمه‌های زیر استفاده کن 👇", reply_markup=(await run_db(main_menu, message.from_user.id)))


# ---------------------------------------------------------------- اجرا
SMS_KEY = os.environ.get("SMS_KEY", "")
_bot_ref = {}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def sms_norm(text):
    """حروف عربی (ي ك) رو فارسی می‌کنه و عددهای فارسی/عربی رو لاتین."""
    return text.replace("ي", "ی").replace("ك", "ک").translate(_DIGITS)


def sms_is_deposit(text):
    text = sms_norm(text)
    t = text.lower()
    if "برداشت" in text and "واریز" not in text:
        return False
    return "واریز" in text or "deposit" in t or bool(re.search(r"\+\s*[\d٬,۰-۹]{4,}|[\d٬,۰-۹]{4,}\s*\+", text))


def sms_amounts(text):
    t = sms_norm(text)
    t = t.split("مانده")[0]  # عدد موجودی حساب نباید با مبلغ فاکتور قاطی بشه
    t = re.sub(r"(?<=\d)[,٬،](?=\d)", "", t)
    return {int(x) for x in re.findall(r"\d{4,}", t)}


def find_invoices_for_sms(text):
    """فاکتورهای بازی که مبلغ واریزی‌شون (تومن) با یکی از عددهای پیامک (ریال ÷ ۱۰) برابره."""
    # مبلغ پیامک بانک به ریاله؛ مبلغ فاکتورها تومنه
    cands = {n // 10 for n in sms_amounts(text) if n % 10 == 0}
    if not cands:
        return []
    return q_all(
        "SELECT invoice_no, status, pay_amount FROM invoices WHERE pay_amount = ANY(%s)"
        " AND status IN ('awaiting_receipt', 'reviewing') AND created_at > now() - interval '48 hours'",
        (list(cands),),
    )


async def auto_approve(bot, no, notify_admin=True):
    """تأیید خودکار فیش: فقط اگه فاکتور هنوز «در حال بررسی» باشه (فقط یک بار)."""
    if (await run_db(q_exec, "UPDATE invoices SET status = 'paid' WHERE invoice_no = %s AND status = 'reviewing'", (no,))) != 1:
        return False
    inv = (await run_db(get_invoice, no))
    try:
        await post_order_to_staff(bot, no)
    except Exception:
        log.exception("auto_approve: post_order_to_staff failed")
    try:
        await send_success(inv["user_id"], no, bot=bot)
    except Exception:
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ فاکتور {no} تأیید شد ولی پیام به مشتری ارسال نشد.")
            except Exception:
                pass
    if notify_admin and ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"✅ فاکتور <code>{no}</code> خودکار تأیید شد (مطابقت با پیامک بانک)\n"
                f"💳 {fa(inv['pay_amount'] or inv['total'])} تومن",
            )
        except Exception:
            pass
    return True


async def process_bank_sms(bot, text):
    if not sms_is_deposit(text):
        return "ignored"
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if (await run_db(q_one, "SELECT 1 AS x FROM bank_sms WHERE body_hash = %s AND received_at > now() - interval '10 minutes'", (h,))):
        return "duplicate"
    rows = (await run_db(find_invoices_for_sms, text))
    if len(rows) != 1:
        (await run_db(q_exec, "INSERT INTO bank_sms (body_hash, amount, invoice_no) VALUES (%s, NULL, NULL)", (h,)))
        if len(rows) > 1 and ADMIN_ID:
            nos = "، ".join(str(r["invoice_no"]) for r in rows)
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ یه پیامک واریز با چند فاکتور مطابقت داشت ({nos}). دستی بررسی کن.")
            except Exception:
                pass
        return "no match" if not rows else "ambiguous"
    r = rows[0]
    (await run_db(q_exec, 
        "INSERT INTO bank_sms (body_hash, amount, invoice_no) VALUES (%s, %s, %s)",
        (h, r["pay_amount"], r["invoice_no"]),
    ))
    if r["status"] == "reviewing":
        await auto_approve(bot, r["invoice_no"])
        return "approved"
    return "waiting for receipt"


async def sms_hook(request):
    if not SMS_KEY:
        return web.Response(status=503, text="disabled")
    key = request.query.get("key") or request.headers.get("X-Key", "")
    if not secrets.compare_digest(key, SMS_KEY):
        return web.Response(status=403, text="forbidden")
    raw = (await request.text()) or ""
    text = raw
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            text = next((str(data[k]) for k in ("text", "message", "body", "sms", "content", "msg") if data.get(k)), raw)
    except Exception:
        try:
            from urllib.parse import parse_qs

            qs = parse_qs(raw)
            text = next((qs[k][0] for k in ("text", "message", "body", "sms", "content", "msg") if qs.get(k)), raw)
        except Exception:
            text = raw
    bot = _bot_ref.get("bot")
    if not text.strip() or bot is None:
        return web.Response(text="empty")
    try:
        result = await process_bank_sms(bot, text)
    except Exception:
        log.exception("process_bank_sms failed")
        return web.Response(status=500, text="error")
    return web.Response(text=result)


async def health(request):
    return web.Response(text="OK")


STAFF_REPORT_HOUR = 17  # ساعت ۱۷ به وقت ایران
_TEHRAN = timedelta(hours=3, minutes=30)


REPORT_PERIODS = {
    "1": "امروز",
    "3": "۳ روز اخیر",
    "7": "۷ روز اخیر",
    "25": "۲۵ روز اخیر",
    "all": "از ابتدا",
}


def _fa_minutes(sec):
    m = max(1, round(sec / 60))
    if m >= 60:
        return f"{fa(m // 60)} ساعت و {fa(m % 60)} دقیقه"
    return f"{fa(m)} دقیقه"


def staff_report_text(period="1", detail=False):
    """گزارش پرسنل: هر نفر چند سفارش آماده ارسال کرده (+ میانگین زمان آماده‌سازی)."""
    now_utc = datetime.now(timezone.utc)
    now_t = now_utc + _TEHRAN
    if period == "all":
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    else:
        if period not in REPORT_PERIODS:
            period = "1"
        # n روز اخیر = از ساعت ۰۰ به وقت تهران، (n-1) روز قبل تا الان
        since = (
            now_t.replace(hour=0, minute=0, second=0, microsecond=0)
            - _TEHRAN
            - timedelta(days=int(period) - 1)
        )
    rows = q_all(
        "SELECT prep_by, MAX(prep_by_name) AS name, COUNT(*) AS orders, COALESCE(SUM(qty), 0) AS units, "
        "AVG(EXTRACT(EPOCH FROM (prep_done_at - prep_started_at))) "
        "FILTER (WHERE prep_started_at IS NOT NULL) AS avg_sec "
        "FROM invoices WHERE prep_status = 'done' AND prep_done_at >= %s AND prep_by IS NOT NULL "
        "GROUP BY prep_by ORDER BY orders DESC, units DESC",
        (since,),
    )
    left = q_one(
        "SELECT COUNT(*) AS n FROM invoices WHERE status = 'paid' AND prep_status IN ('new', 'preparing')"
    )
    title = REPORT_PERIODS[period]
    lines = [f"📊 <b>گزارش آماده‌سازی — {title}</b> ({jdate(now_utc)})", LINE]
    if not rows:
        lines.append("برای این بازه هنوز سفارشی آماده ارسال ثبت نشده.")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(rows):
            icon = medals[i] if i < len(medals) else "▫️"
            line = f"{icon} {escape(r['name'] or '—')}: <b>{fa(int(r['orders']))}</b> سفارش ({fa(int(r['units']))} عدد)"
            if detail and r.get("avg_sec") is not None:
                line += f"\n    ⏱ میانگین زمان هر سفارش: {_fa_minutes(float(r['avg_sec']))}"
            lines.append(line)
        lines += [
            LINE,
            f"✅ جمع کل: <b>{fa(sum(int(r['orders']) for r in rows))}</b> سفارش "
            f"({fa(sum(int(r['units']) for r in rows))} عدد)",
        ]
    lines.append(f"⏳ سفارش‌های آماده‌نشده: <b>{fa(int((left or {}).get('n') or 0))}</b>")
    if detail:
        busy = q_all(
            "SELECT invoice_no, prep_by_name, prep_started_at FROM invoices "
            "WHERE prep_status = 'preparing' ORDER BY prep_started_at NULLS LAST"
        )
        if busy:
            lines += [LINE, "🟡 <b>همین الان در حال آماده‌سازی:</b>"]
            for r in busy:
                t = r.get("prep_started_at")
                age = f" — {_fa_minutes((now_utc - t).total_seconds())} است" if t else ""
                lines.append(f"• {escape(r['prep_by_name'] or '—')}: <code>{r['invoice_no']}</code>{age}")
    return "\n".join(lines)


def report_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="۱ روز", callback_data="srep:1"),
                InlineKeyboardButton(text="۳ روز", callback_data="srep:3"),
            ],
            [
                InlineKeyboardButton(text="۷ روز", callback_data="srep:7"),
                InlineKeyboardButton(text="۲۵ روز", callback_data="srep:25"),
            ],
            [InlineKeyboardButton(text="از ابتدا", callback_data="srep:all")],
        ]
    )


def expire_overdue():
    """فاکتورهای منتظر فیش و درخواست‌های شارژ که مهلتشون تموم شده لغو می‌شن.
    فاکتوری که فیشش ارسال شده (در حال بررسی) یا پیامک بانکش رسیده، دست نمی‌خوره."""
    mins = int(PAY_DEADLINE_MIN)
    invs = q_all(
        "UPDATE invoices SET status = 'cancelled' WHERE status = 'awaiting_receipt'"
        f" AND COALESCE(stock_at, created_at) < now() - interval '{mins} minutes'"
        " AND NOT EXISTS (SELECT 1 FROM bank_sms s WHERE s.invoice_no = invoices.invoice_no)"
        " RETURNING invoice_no, user_id"
    )
    # درخواست شارژی که مشتری «پرداخت کردم» زده خبر می‌گیره؛ ردیف‌های رهاشده (pending) بی‌صدا لغو می‌شن
    tops = q_all(
        "UPDATE topups SET status = 'cancelled' WHERE status = 'awaiting_receipt'"
        f" AND created_at < now() - interval '{mins} minutes'"
        " RETURNING id, user_id, amount"
    )
    q_exec(
        "UPDATE topups SET status = 'cancelled' WHERE status = 'pending'"
        f" AND created_at < now() - interval '{mins} minutes'"
    )
    return invs, tops


# آخرین باری که مشتری/کاربری با ربات کار کرده؛ تا وقتی کسی کار نکرده، فاکتور تازه‌ای هم ساخته نشده،
# پس لازم نیست هر دقیقه دیتابیس رو بیدار نگه داریم (سهمیهٔ ساعت پردازشی Neon حفظ می‌شه).
_last_activity = time.monotonic()
EXPIRE_IDLE_SEC = (int(PAY_DEADLINE_MIN) + 5) * 60


async def mark_activity(handler, event, data):
    global _last_activity
    _last_activity = time.monotonic()
    return await handler(event, data)


async def expire_loop(bot):
    while True:
        await asyncio.sleep(60)
        if time.monotonic() - _last_activity > EXPIRE_IDLE_SEC:
            continue
        try:
            invs, tops = (await run_db(expire_overdue, ))
        except Exception:
            log.exception("expire_overdue failed")
            continue
        for r in invs:
            try:
                await bot.send_message(
                    r["user_id"],
                    f"⏰ مهلت پرداخت فاکتور <code>{r['invoice_no']}</code> تموم شد و فاکتور لغو شد.\n"
                    "اگه هنوز می‌خوای سفارشت رو ثبت کنی، از «فاکتورهای من» همین فاکتور رو دوباره فعال کن.\n"
                    "اگه مبلغ رو واریز کردی، حتماً با پشتیبانی تماس بگیر.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="📋 فاکتورهای من", callback_data="myinv:0")],
                            [InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)],
                            menu_kb().inline_keyboard[0],
                        ]
                    ),
                )
            except Exception:
                pass
            await asyncio.sleep(0.05)
        for r in tops:
            try:
                await bot.send_message(
                    r["user_id"],
                    f"⏰ مهلت پرداخت درخواست شارژ {en(fa(int(r['amount'])))} تومن تموم شد و درخواست لغو شد.\n"
                    "اگه مبلغ رو واریز کردی، حتماً با پشتیبانی تماس بگیر.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet")],
                            [InlineKeyboardButton(text="📞 پشتیبانی", url=SUPPORT_URL)],
                        ]
                    ),
                )
            except Exception:
                pass
            await asyncio.sleep(0.05)


async def staff_report_loop(bot):
    """هر روز ساعت ۱۷ به وقت ایران گزارش رو توی گروه آماده ارسال (اگه تنظیم نبود، گروه پرسنل) می‌فرسته."""
    while True:
        now_t = datetime.now(timezone.utc) + _TEHRAN
        target = now_t.replace(hour=STAFF_REPORT_HOUR, minute=0, second=0, microsecond=0)
        if target <= now_t:
            target += timedelta(days=1)
        await asyncio.sleep((target - now_t).total_seconds())
        try:
            report_chat = READY_CHAT_ID or ORDERS_CHAT_ID
            if report_chat:
                await bot.send_message(report_chat, (await run_db(staff_report_text, )))
        except Exception as e:
            log.exception("staff report failed")
            if ADMIN_ID:
                try:
                    await bot.send_message(ADMIN_ID, f"⚠️ گزارش روزانه پرسنل ارسال نشد.\n{type(e).__name__}: {e}")
                except Exception:
                    pass
        await asyncio.sleep(60)


async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/sms", sms_hook)
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
    bot.session.middleware(TrackRequests())
    _bot_ref["bot"] = bot
    await setup_commands(bot)
    asyncio.create_task(staff_report_loop(bot))
    asyncio.create_task(expire_loop(bot))
    dp.message.outer_middleware(mark_activity)
    dp.callback_query.outer_middleware(mark_activity)
    dp.message.outer_middleware(GroupGuard())
    dp.callback_query.outer_middleware(GroupGuard())
    dp.message.middleware(Clean())
    dp.callback_query.middleware(Clean())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
