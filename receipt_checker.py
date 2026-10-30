"""Automatic card-to-card receipt verification using only free libraries.

Pipeline (cheap checks first):
  1. image sanity (size, ratio, editor tag in metadata)
  2. OCR with Tesseract (OpenCV preprocessing) -> keywords, amount, tracking number
  3. layout comparison with the reference receipts in receipt_refs/ (ORB features)
  4. exact amount match, tracking number present, trivial/fake tracking pattern

Duplicate detection needs the database, so main.py does it using result.tracking
and result.sha256 (atomic INSERT into the receipts table).
"""
import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

log = logging.getLogger("receipt")

try:
    import cv2
    import numpy as np
    import pytesseract
    from PIL import Image

    LIBS_OK = True
except ImportError:
    LIBS_OK = False

OCR_LANG = os.environ.get("OCR_LANG", "fas+eng")
OCR_TIMEOUT = 40  # seconds per OCR pass
REF_DIR = Path(os.environ.get("RECEIPT_REF_DIR", "receipt_refs"))
MIN_KEYWORDS = int(os.environ.get("RECEIPT_MIN_KEYWORDS", "3"))
STRONG_KEYWORDS = int(os.environ.get("RECEIPT_STRONG_KEYWORDS", "5"))
REF_MIN_SCORE = float(os.environ.get("RECEIPT_REF_MIN_SCORE", "0.05"))

EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

KEYWORDS = (
    "رسید", "انتقال", "مبلغ", "ریال", "تومان", "پیگیری", "بانک", "موفق", "کارت",
    "مقصد", "مبدا", "تاریخ", "ساعت", "حساب", "شبا", "ارجاع", "تراکنش", "پرداخت",
    "واریز", "ملت",
)
TRACK_LABEL = re.compile(
    r"پیگیری|ارجاع|مرجع|رهگیری|شماره\s*تراکنش|شناسه\s*تراکنش|کد\s*تراکنش|شماره\s*سند"
)
CARD_PREFIXES = ("6104", "6037", "6219", "5022", "6274", "6221", "5859", "6393", "6280")
EDITOR_WORDS = (
    "photoshop", "canva", "picsart", "snapseed", "gimp", "lightroom", "pixlr",
    "fotor", "receipt", "fake",
)


@dataclass
class Result:
    ok: bool
    code: str  # ok | not_receipt | fake | amount | no_tracking | unavailable
    tracking: Optional[str] = None
    sha256: str = ""


# ------------------------------------------------------------------ text helpers
def normalize(text: str) -> str:
    t = text.translate(EN_DIGITS)
    t = t.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return re.sub(r"[\u064B-\u065F\u0640]", "", t)


def numbers_in(text: str) -> set:
    """All integers in the text; 1,500,000 and 1500000 both become 1500000."""
    out = set()
    for tok in re.findall(r"\d[\d,٬،]*\d|\d", text):
        digits = re.sub(r"\D", "", tok)
        if digits:
            out.add(int(digits))
    return out


def extract_tracking(text: str, exclude=()) -> Optional[str]:
    """Digits found on (or right after) a line that has a tracking/reference label."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not TRACK_LABEL.search(line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        for chunk in (line, nxt):
            runs = [
                r for r in re.findall(r"\d{5,24}", chunk)
                if r not in exclude and not (len(r) == 16 and r.startswith(CARD_PREFIXES))
            ]
            if runs:
                return max(runs, key=len)
    return None


def trivial_tracking(t: str) -> bool:
    return (
        len(set(t)) <= 2
        or t in "0123456789" * 3
        or t in "9876543210" * 3
    )


# ------------------------------------------------------------------ image helpers
def editor_tag(pil) -> bool:
    vals = []
    try:
        vals.append(str(pil.getexif().get(305, "")))
    except Exception:
        pass
    for k in ("Software", "software", "Comment", "comment"):
        v = pil.info.get(k)
        if v:
            vals.append(v.decode(errors="ignore") if isinstance(v, bytes) else str(v))
    s = " ".join(vals).lower()
    return any(w in s for w in EDITOR_WORDS)


def ocr_texts(img_bgr) -> list:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if w < 1000:
        f = min(3.0, 1000 / w)
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    if gray.mean() < 110:  # dark-mode screenshot
        gray = 255 - gray
    thresh = cv2.adaptiveThreshold(
        cv2.GaussianBlur(gray, (3, 3), 0), 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 15,
    )
    return [
        pytesseract.image_to_string(v, lang=OCR_LANG, config="--oem 1 --psm 6", timeout=OCR_TIMEOUT)
        for v in (gray, thresh)
    ]


def _orb(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    gray = cv2.resize(gray, (600, max(1, int(h * 600 / w))))
    _, des = cv2.ORB_create(nfeatures=800).detectAndCompute(gray, None)
    return des


_refs_cache = None


def reference_descriptors() -> list:
    global _refs_cache
    if _refs_cache is None:
        _refs_cache = []
        if LIBS_OK and REF_DIR.is_dir():
            for p in sorted(REF_DIR.iterdir()):
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    im = cv2.imread(str(p), cv2.IMREAD_COLOR)
                    des = _orb(im) if im is not None else None
                    if des is not None and len(des) >= 2:
                        _refs_cache.append(des)
        log.info("loaded %d reference receipts", len(_refs_cache))
    return _refs_cache


def reference_score(img_bgr, refs) -> float:
    des = _orb(img_bgr)
    if des is None or len(des) < 2:
        return 0.0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    best = 0.0
    for rd in refs:
        pairs = bf.knnMatch(des, rd, k=2)
        good = sum(1 for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance)
        best = max(best, good / max(1, min(len(des), len(rd))))
    return best


# ------------------------------------------------------------------ main check
def analyze(data: bytes, expected_total: int) -> Result:
    """expected_total is in toman; the receipt may show toman or rial."""
    sha = hashlib.sha256(data).hexdigest()
    if not LIBS_OK:
        return Result(False, "unavailable", sha256=sha)
    try:
        pil = Image.open(BytesIO(data))
        pil.load()
    except Exception:
        return Result(False, "not_receipt", sha256=sha)
    w, h = pil.size
    if min(w, h) < 300 or max(w, h) < 400 or not (0.25 <= h / w <= 5):
        return Result(False, "not_receipt", sha256=sha)
    if editor_tag(pil):
        return Result(False, "fake", sha256=sha)

    try:
        img = cv2.cvtColor(np.array(pil.convert("RGB")), cv2.COLOR_RGB2BGR)
        texts = [normalize(t) for t in ocr_texts(img)]
        refs = reference_descriptors()
        ref_score = reference_score(img, refs) if refs else None
    except Exception:
        log.exception("receipt OCR failed")
        return Result(False, "unavailable", sha256=sha)

    joined = "\n".join(texts)
    kw = sum(1 for k in KEYWORDS if k in joined)
    log.info("receipt kw=%s ref_score=%s", kw, ref_score)
    if kw < MIN_KEYWORDS:
        return Result(False, "not_receipt", sha256=sha)
    if ref_score is not None and ref_score < REF_MIN_SCORE and kw < STRONG_KEYWORDS:
        return Result(False, "not_receipt", sha256=sha)

    nums = set()
    for t in texts:
        nums |= numbers_in(t)
    if expected_total not in nums and expected_total * 10 not in nums:
        return Result(False, "amount", sha256=sha)

    exclude = {str(expected_total), str(expected_total * 10)}
    tracking = None
    for t in texts:
        tracking = extract_tracking(t, exclude)
        if tracking:
            break
    if not tracking:
        return Result(False, "no_tracking", sha256=sha)
    if trivial_tracking(tracking):
        return Result(False, "fake", tracking, sha)
    return Result(True, "ok", tracking, sha)


_sem = asyncio.Semaphore(1)  # one OCR at a time keeps a small server responsive


async def check_receipt(data: bytes, expected_total: int) -> Result:
    async with _sem:
        return await asyncio.to_thread(analyze, data, expected_total)
