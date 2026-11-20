"""خواندن ورق پست (بارکد + اسم گیرنده + شهر) و ثبت خودکار کد پیگیری.

بارکدها با zxing-cpp خوانده می‌شن (دقیق، بدون حدس).
اسم و شهر با Tesseract فارسی خوانده می‌شن و با rapidfuzz به سفارش‌ها وصل می‌شن.
عکس فقط توی رم پردازش می‌شه و ذخیره نمی‌شه.
"""
import asyncio
import io
import logging
import re
from html import escape

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("bot")

MAX_SIDE = 2400  # بزرگ‌ترین ضلع عکس بعد از کوچک‌کردن (برای صرفه‌جویی در رم)
SCORE_MIN = 78  # حداقل امتیاز کل برای تطبیق
NAME_MIN = 70  # حداقل شباهت اسم
AMBIG_GAP = 6  # اگه دو سفارش اینقدر به هم نزدیک بودن، تطبیق مشکوکه
CODE_RE = re.compile(r"[0-9A-Za-z]{8,30}")
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm(s):
    s = (s or "").replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه").replace("آ", "ا").replace("أ", "ا")
    return re.sub(r"[\s\u200c\u200e\u200f\-_.,،:؛()/]+", "", s)


def _load(data):
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def _read_one(arr, dy, out, seen):
    import zxingcpp

    for b in zxingcpp.read_barcodes(arr):
        code = re.sub(r"[^0-9A-Za-z]", "", (b.text or "").translate(_DIG))
        if not CODE_RE.fullmatch(code) or code in seen:
            continue
        seen.add(code)
        p = b.position
        pts = [p.top_left, p.top_right, p.bottom_left, p.bottom_right]
        xs, ys = [q.x for q in pts], [q.y + dy for q in pts]
        out.append({"code": code, "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys)})


def _find_barcodes(img):
    import numpy as np

    arr = np.asarray(img)
    out, seen = [], set()
    _read_one(arr, 0, out, seen)
    h = arr.shape[0]
    # بارکدهای کوچیک گاهی توی اسکن کل عکس گم می‌شن؛ تکه‌تکه هم اسکن می‌کنیم
    for parts in (3, 5, 8):
        step = h // parts
        if step < 120:
            continue
        for i in range(parts):
            y0 = max(0, i * step - step // 2)
            y1 = min(h, (i + 1) * step + step // 2)
            try:
                _read_one(np.ascontiguousarray(arr[y0:y1]), y0, out, seen)
            except Exception:
                log.exception("scan: strip failed")
    out.sort(key=lambda r: r["y0"] + r["y1"])
    return out


def _ocr_words(img, bars):
    import pytesseract
    from PIL import ImageDraw, ImageOps

    g = ImageOps.autocontrast(img.convert("L"))
    d = ImageDraw.Draw(g)
    for b in bars:  # بارکد و عدد زیرش رو سفید می‌کنیم تا OCR باهاش درگیر نشه
        h = b["y1"] - b["y0"]
        d.rectangle([b["x0"] - 6, b["y0"] - 4, b["x1"] + 6, b["y1"] + h * 1.2], fill=255)
    data = pytesseract.image_to_data(
        g, lang="fas", config="--psm 11", output_type=pytesseract.Output.DICT
    )
    words = []
    for i, t in enumerate(data["text"]):
        t = (t or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < 20 or re.search(r"\d", t.translate(_DIG)):
            continue
        x = data["left"][i] + data["width"][i] / 2
        y = data["top"][i] + data["height"][i] / 2
        words.append((x, y, t))
    return words


def _build_rows(bars, words):
    centers = [(b["y0"] + b["y1"]) / 2 for b in bars]
    if len(centers) >= 2:
        gaps = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
        spacing = gaps[len(gaps) // 2]
    else:
        spacing = (bars[0]["y1"] - bars[0]["y0"]) * 2
    rows = [{"code": b["code"], "items": []} for b in bars]
    for x, y, t in words:
        k = min(range(len(centers)), key=lambda i: abs(centers[i] - y))
        if abs(centers[k] - y) <= spacing * 0.55:
            rows[k]["items"].append((x, t))
    for r in rows:
        r["words"] = [t for _, t in sorted(r.pop("items"), key=lambda w: -w[0])]  # راست به چپ
        r["text"] = " ".join(r["words"])
    return rows


def _match(rows, cands):
    from rapidfuzz import fuzz

    cand_n = [(c, _norm(c.get("full_name")), _norm(c.get("address"))) for c in cands]
    cand_n = [x for x in cand_n if x[1]]
    per_row = []
    for r in rows:
        rt = _norm(r["text"])
        ws = [w for w in (_norm(w) for w in r["words"]) if len(w) >= 3]
        scored = []
        if len(rt) >= 3:
            for c, nm, ad in cand_n:
                ns = fuzz.partial_ratio(nm, rt)
                if len(rt) < 0.6 * len(nm):
                    ns *= len(rt) / (0.6 * len(nm))
                # روی ورق ممکنه فقط فامیلی باشه: هر بخش اسم جدا با کلمه‌های ردیف سنجیده می‌شه
                toks = [t for t in (_norm(t) for t in (c.get("full_name") or "").split()) if len(t) >= 3]
                if toks and ws:
                    best = [max(fuzz.ratio(t, w) for w in ws) for t in toks]
                    ns = max(ns, 0.7 * max(best) + 0.3 * (sum(best) / len(best)))
                cs = max((fuzz.partial_ratio(w, ad) for w in ws), default=0)
                scored.append((0.65 * ns + 0.35 * cs, ns, c))
            scored.sort(key=lambda s: -s[0])
        per_row.append(scored)
    pairs = []
    for ri, scored in enumerate(per_row):
        if not scored:
            continue
        best = scored[0]
        second = scored[1] if len(scored) > 1 else None
        ambiguous = second is not None and best[0] - second[0] < AMBIG_GAP and second[0] >= SCORE_MIN
        if best[0] >= SCORE_MIN and best[1] >= NAME_MIN and not ambiguous:
            pairs.append((best[0], ri, best[2]))
    pairs.sort(key=lambda p: -p[0])
    used_r, used_i, matched = set(), set(), {}
    for sc, ri, c in pairs:
        if ri in used_r or c["invoice_no"] in used_i:
            continue
        used_r.add(ri)
        used_i.add(c["invoice_no"])
        matched[ri] = (c, sc)
    return matched


def scan_sheet(data, cands):
    """عکس ورق + لیست سفارش‌های کاندید → {'matched': [...], 'unmatched': [...]} یا {'error': ...}"""
    img = _load(data)
    bars = _find_barcodes(img)
    if not bars:
        return {"error": "no_barcodes"}
    rows = _build_rows(bars, _ocr_words(img, bars))
    matched = _match(rows, cands)
    return {
        "ncand": len(cands),
        "matched": [(rows[i], c, sc) for i, (c, sc) in sorted(matched.items())],
        "unmatched": [r for i, r in enumerate(rows) if i not in matched],
    }


CAND_SQL = (
    "SELECT invoice_no, full_name, address FROM invoices"
    " WHERE status = 'paid' AND prep_status = 'done' AND (tracking_code IS NULL OR tracking_code = '')"
    " ORDER BY prep_done_at DESC NULLS LAST LIMIT 400"
)


def register(dp, d):
    ready = d["READY_CHAT_ID"]
    pending = {}  # message_id پیش‌نمایش -> [(invoice_no, code)]
    lock = asyncio.Lock()  # هر بار فقط یک عکس (برای رم)

    def is_img(m):
        return bool(m.photo) or bool(m.document and (m.document.mime_type or "").lower().startswith("image/"))

    async def apply_one(bot, no, code, by):
        inv = await d["run_db"](d["get_invoice"], no)
        if inv is None or inv["status"] != "paid" or inv.get("tracking_code"):
            return "skip"
        if await d["run_db"](d["q_one"], "SELECT 1 AS x FROM invoices WHERE tracking_code = %s", (code,)):
            return "dup"
        n = await d["run_db"](
            d["q_exec"],
            "UPDATE invoices SET tracking_code = %s, tracking_at = now(), tracking_by_name = %s"
            " WHERE invoice_no = %s AND (tracking_code IS NULL OR tracking_code = '')",
            (code, by, no),
        )
        if n != 1:
            return "skip"
        sent = True
        try:
            await bot.send_message(
                inv["user_id"],
                d["track_customer_text"](inv, code, False),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=d["track_url_rows"](code) + [d["menu_kb"]().inline_keyboard[0]]
                ),
            )
        except Exception:
            log.exception("scan: tracking message to customer failed")
            sent = False
        fresh = (await d["run_db"](d["get_invoice"], no)) or inv
        text = d["ready_order_text"](fresh)
        if not sent:
            text += "\n⚠️ پیام به مشتری نرسید (احتمالاً ربات رو بلاک کرده)."
        if inv.get("ready_msg_id"):
            for fn, field in ((bot.edit_message_text, "text"), (bot.edit_message_caption, "caption")):
                try:
                    await fn(
                        chat_id=ready,
                        message_id=inv["ready_msg_id"],
                        reply_markup=d["track_kb"](no, True),
                        **{field: text},
                    )
                    break
                except TelegramBadRequest as e:
                    if "not modified" in str(e):
                        break
                except Exception:
                    log.exception("scan: editing ready message failed")
        return "ok" if sent else "ok_nosend"

    @dp.message(F.chat.type.in_({"group", "supergroup"}), F.photo | F.document)
    async def sheet_photo(message):
        if not ready or message.chat.id != ready or not is_img(message):
            return
        async with lock:
            note = await message.reply("⏳ در حال خواندن ورق ...")
            try:
                buf = io.BytesIO()
                fid = message.photo[-1].file_id if message.photo else message.document.file_id
                await message.bot.download(fid, destination=buf)
                cands = await d["run_db"](d["q_all"], CAND_SQL)
                res = await asyncio.to_thread(scan_sheet, buf.getvalue(), cands)
                buf.close()
            except ImportError:
                log.exception("scan: missing library")
                await note.edit_text("❌ کتابخونه‌های خواندن ورق نصب نیستن. دیپلوی رو چک کن.")
                return
            except Exception as e:
                log.exception("scan failed")
                await note.edit_text(
                    "❌ خواندن ورق انجام نشد. دوباره بفرست یا کدها رو دستی ثبت کن.\n"
                    f"<code>{escape(type(e).__name__)}: {escape(str(e)[:300])}</code>",
                    parse_mode="HTML",
                )
                return
        if res.get("error") == "no_barcodes":
            await note.edit_text(
                "⚠️ هیچ بارکدی پیدا نشد.\n"
                "ورق رو صاف، روشن و بدون سایه از بالا عکس بگیر و حتماً به‌صورت «فایل» (Send as file) بفرست "
                "تا کیفیت کم نشه."
            )
            return
        matched, unmatched = res["matched"], res["unmatched"]
        lines = [
            f"🔎 <b>نتیجهٔ خواندن ورق</b> — {len(matched) + len(unmatched)} بارکد",
            f"📋 سفارش آماده ارسال برای تطبیق: {res.get('ncand', 0)}",
            "━━━━━━━━━━━━━━━━━━",
        ]
        if matched:
            lines.append(f"✅ <b>تطبیق‌خورده ({len(matched)})</b>")
            for row, c, sc in matched:
                lines.append(
                    f"• {escape(c['full_name'] or '—')} ← فاکتور <code>{c['invoice_no']}</code>\n"
                    f"   کد: <code>{escape(row['code'])}</code>"
                )
        if unmatched:
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append(f"⚠️ <b>نیازمند ثبت دستی ({len(unmatched)})</b>")
            for row in unmatched:
                guess = escape(row["text"][:40]) if row["text"] else "—"
                lines.append(f"• کد: <code>{escape(row['code'])}</code>\n   متن خوانده‌شده: {guess}")
            lines.append("برای این‌ها دکمهٔ «ثبت کد پیگیری» روی پیام همون سفارش رو بزن.")
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        kb = None
        if matched:
            pending[note.message_id] = [(c["invoice_no"], row["code"]) for row, c, _ in matched]
            while len(pending) > 30:
                pending.pop(next(iter(pending)))
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"✅ ثبت و ارسال برای مشتری‌ها ({len(matched)})", callback_data="scanok")],
                    [InlineKeyboardButton(text="❌ لغو", callback_data="scanno")],
                ]
            )
        await note.edit_text(text, reply_markup=kb)

    @dp.callback_query(F.data.in_({"scanok", "scanno"}))
    async def scan_confirm(call):
        if not ready or call.message is None or call.message.chat.id != ready:
            await call.answer()
            return
        job = pending.pop(call.message.message_id, None)
        if job is None:
            await call.answer("این پیش‌نمایش منقضی شده؛ عکس ورق رو دوباره بفرست.", show_alert=True)
            return
        if call.data == "scanno":
            await call.message.edit_text("❌ لغو شد. چیزی ثبت نشد.")
            await call.answer()
            return
        await call.answer("⏳ در حال ثبت ...")
        by = call.from_user.full_name or ""
        ok = nosend = bad = 0
        for no, code in job:
            try:
                r = await apply_one(call.bot, no, code, by)
            except Exception:
                log.exception("scan: apply failed for %s", no)
                r = "skip"
            if r == "ok":
                ok += 1
            elif r == "ok_nosend":
                nosend += 1
            else:
                bad += 1
            await asyncio.sleep(0.1)
        out = [f"✅ {ok + nosend} کد پیگیری ثبت شد."]
        if nosend:
            out.append(f"⚠️ {nosend} مورد ثبت شد ولی پیام به مشتری نرسید.")
        if bad:
            out.append(f"⏭ {bad} مورد رد شد (قبلاً کد داشت یا کد تکراری بود).")
        await call.message.edit_text("\n".join(out))
