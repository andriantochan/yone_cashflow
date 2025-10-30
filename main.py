import os
import logging
import re
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from supabase import create_client, Client
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import pytesseract
from pytesseract import Output
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)

load_dotenv()
DEBUG_OCR = os.getenv("DEBUG_OCR", "").lower() in {"1", "true", "yes"}
DEBUG_OCR = os.getenv("DEBUG_OCR", "").lower() in {"1", "true", "yes"}
logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Jakarta")
LOCAL_TZ = ZoneInfo(APP_TIMEZONE)

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- Helpers ----------
def _format_db_dt(dt: datetime) -> str:
    """Format aware datetime to 'YYYY-MM-DD HH:MM:SS+07:00'."""
    aware = dt.astimezone(LOCAL_TZ)
    s = aware.strftime("%Y-%m-%d %H:%M:%S%z")  # e.g., +0700
    if len(s) >= 5:
        # add colon to timezone offset: +0700 -> +07:00
        return s[:-2] + ":" + s[-2:]
    return s

def _now_iso() -> str:
    """Return current timestamp with local timezone offset for DB storage."""
    return _format_db_dt(datetime.now(LOCAL_TZ).replace(microsecond=0))

async def get_or_create_app_user_id(update: Update) -> str:
    tg = update.effective_user
    telegram_id = int(getattr(tg, "id", 0))
    username = getattr(tg, "username", None)
    first_name = getattr(tg, "first_name", None)
    last_name = getattr(tg, "last_name", None)
    # try get
    res = sb.table("app_user").select("id").eq("telegram_id", telegram_id).limit(1).execute()
    if res.data:
        return res.data[0]["id"]
    # else create
    ins = sb.table("app_user").insert({
        "telegram_id": telegram_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
    }).execute()
    try:
        if ins.data and isinstance(ins.data, list) and ins.data and "id" in ins.data[0]:
            return ins.data[0]["id"]
    except Exception:
        pass
    res2 = sb.table("app_user").select("id").eq("telegram_id", telegram_id).limit(1).execute()
    if res2.data:
        return res2.data[0]["id"]
    raise RuntimeError("Gagal membuat/menemukan user aplikasi")

async def get_or_create_id(table: str, name: str, user_id: str | None = None) -> str:
    # try get
    q = sb.table(table).select("id").eq("name", name)
    if user_id is not None:
        q = q.eq("user_id", user_id)
    res = q.limit(1).execute()
    if res.data:
        return res.data[0]["id"]
    # else create
    payload = {"name": name}
    if user_id is not None:
        payload["user_id"] = user_id
    ins = sb.table(table).insert(payload).execute()
    # Prefer returned id if server returns representation
    try:
        if ins.data and isinstance(ins.data, list) and ins.data and "id" in ins.data[0]:
            return ins.data[0]["id"]
    except Exception:
        pass
    # Fallback: fetch the newly inserted row
    q2 = sb.table(table).select("id").eq("name", name)
    if user_id is not None:
        q2 = q2.eq("user_id", user_id)
    res2 = q2.limit(1).execute()
    if res2.data:
        return res2.data[0]["id"]
    raise RuntimeError(f"Gagal membuat {table} '{name}'")

def parse_kv_args(text: str) -> dict:
    """
    Parse /add bank=BCA category=Gaji type=income desc="Gaji bulan ini"
    - mendukung kutip untuk value dengan spasi
    """
    import shlex
    parts = shlex.split(text)
    out = {}
    for p in parts[1:]:  # skip command
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.lower()] = v
    return out

# ---------- Handlers ----------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await _show_menu(update)

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = await get_or_create_app_user_id(update)
        args = parse_kv_args(update.message.text)
        bank = args.get("bank")
        category = args.get("category")
        tx_type = args.get("type")
        desc = args.get("desc")
        tx_at = None
        raw_date = args.get("date") or args.get("transaction_date")
        if raw_date:
            try:
                tx_at = _parse_datetime_input(raw_date)
            except Exception:
                return await update.message.reply_text(
                    "Tanggal/waktu tidak valid. Contoh: 2025-10-30 14:30 atau 30-10-2025."
                )

        if not bank or not category or tx_type not in {"income", "outcome"}:
            return await update.message.reply_text(
                "Format salah.\nContoh:\n"
                "/add bank=BCA category=Gaji type=income desc=\"Gaji bulan ini\""
            )

        bank_id = await get_or_create_id("bank", bank, None)
        category_id = await get_or_create_id("category", category, None)

        # insert transaksi
        payload = {
            "bank_id": bank_id,
            "category_id": category_id,
            "type": tx_type,                 # ENUM di Supabase: 'income'/'outcome'
            "description": desc or None,
            "transaction_date": tx_at or _now_iso(),
            "user_id": user_id,
        }
        sb.table("transaction").insert(payload).execute()

        ts = _format_dt_for_display(tx_at or _now_iso())
        await update.message.reply_text(
            f"✅ Tersimpan: [{tx_type}] {desc or '-'} — {ts} — {category} @ {bank}"
        )

    except Exception as e:
        logging.exception("add failed")
        await update.message.reply_text(f"❌ Gagal menyimpan: {e}")

def _format_rp(amount: float | int | Decimal | None) -> str:
    if amount is None:
        return "-"
    try:
        n = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:
        return "-"
    # format thousands with dot and decimal comma
    sign = "-" if n < 0 else ""
    n = abs(n)
    q = int(n)
    r = int((n - q) * 100)
    s = f"{q:,}".replace(",", ".")
    if r == 0:
        return f"{sign}Rp {s}"
    return f"{sign}Rp {s},{r:02d}"

def _format_dt_for_display(value: str | None) -> str:
    s = str(value or "")
    if not s:
        return "-"
    try:
        # Normalize to ISO for parser and display in local timezone
        iso = s.replace("Z", "+00:00")
        if "T" not in iso and "+" in iso:
            # turn 'YYYY-MM-DD HH:MM:SS+07:00' into ISO '...T...'
            idx = iso.find("+")
            iso = iso[:19].replace(" ", "T") + iso[19:]
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(LOCAL_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s.replace("T", " ")

def _parse_amount(text: str) -> Decimal:
    s = (text or "").strip()
    # Keep digits and separators, strip any spaces including NBSP
    filtered = "".join(ch for ch in s if ch.isdigit() or ch in ",.")
    if not filtered:
        raise ValueError("Nominal tidak valid")

    has_comma = "," in filtered
    has_dot = "." in filtered

    if has_comma and has_dot:
        # Decide decimal by the rightmost separator among comma/dot
        last_comma = filtered.rfind(",")
        last_dot = filtered.rfind(".")
        if last_comma > last_dot:
            # comma is decimal: drop all dots, replace last comma with dot
            filtered = filtered.replace(".", "")
            # replace ALL commas with dot, since previous commas could be decimals in OCR noise
            filtered = filtered.replace(",", ".")
        else:
            # dot is decimal: drop all commas
            filtered = filtered.replace(",", "")
    elif has_comma and not has_dot:
        # Only commas present. If multiple commas, treat last as decimal and others as thousands
        if filtered.count(",") > 1:
            last = filtered.rfind(",")
            filtered = filtered[:last].replace(",", "") + "." + filtered[last+1:]
        else:
            filtered = filtered.replace(",", ".")
    elif has_dot and not has_comma:
        # Only dots present. Decide whether they are thousands separators or decimal.
        parts = filtered.split(".")
        if len(parts) == 1:
            pass  # no-op
        elif len(parts) == 2:
            # One dot: if right side has length 3, treat as thousands (e.g., 20.000 -> 20000)
            if len(parts[1]) == 3 and len(parts[0]) >= 1:
                filtered = parts[0] + parts[1]
            else:
                # Treat as decimal (e.g., 20.5)
                filtered = parts[0] + "." + parts[1]
        else:
            # Multiple dots
            right_len = len(parts[-1])
            left_ok = 1 <= len(parts[0]) <= 3
            middle_ok = all(len(p) == 3 for p in parts[1:-1])
            if right_len == 3 and left_ok and middle_ok:
                # Pure thousands grouping: 1.234.567 -> 1234567
                filtered = "".join(parts)
            elif right_len in (1, 2):
                # Likely decimal with thousands before: 1.234.56 or 1.234.5 -> 1234.56 / 1234.5
                filtered = "".join(parts[:-1]) + "." + parts[-1]
            else:
                # Ambiguous; prefer treating as thousands: 12.3456.789 -> 123456789
                filtered = "".join(parts)
    # else: only digits

    try:
        return Decimal(filtered)
    except InvalidOperation:
        raise ValueError("Nominal tidak valid")

def _first_sentence(text: str) -> str:
    s = (text or "").strip()
    for sep in [".", "!", "?", "\n"]:
        idx = s.find(sep)
        if idx != -1:
            s = s[:idx]
            break
    return s.strip()

def _parse_datetime_input(text: str) -> str:
    """Parse user-provided date/time and return DB-friendly datetime with TZ offset.
    Supports:
    - Empty/0/'hari ini'/'today'/'now' -> now
    - 'kemarin'/'yesterday' -> now - 1 day (same time)
    - Dates: YYYY-MM-DD or DD-MM-YYYY (00:00:00 time)
    - Datetime: 'YYYY-MM-DD HH:MM[:SS]' or 'DD-MM-YYYY HH:MM[:SS]'
    - Also accepts '/' as separator in date part
    """
    raw = (text or "").strip()
    t = raw.lower()
    if not t or t == "0" or t in {"hari ini", "today", "now"}:
        return _now_iso()
    if t in {"kemarin", "yesterday"}:
        return _format_db_dt((datetime.now(LOCAL_TZ) - timedelta(days=1)).replace(microsecond=0))

    s = raw.replace("/", "-").strip()
    # Try datetime formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(microsecond=0)
            # interpret as local time
            dt = dt.replace(tzinfo=LOCAL_TZ)
            return _format_db_dt(dt)
        except Exception:
            pass
    # Try date-only formats
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            # default to 00:00:00 if time not provided
            return _format_db_dt(datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=LOCAL_TZ))
        except Exception:
            pass
    raise ValueError("Format tanggal/waktu tidak dikenali. Contoh: 2025-10-30 14:30")

def _try_parse_inline_full(text: str):
    """
    Try parse: <desc> <income|outcome> <amount> <category> <bank>
    Supports quotes around category/bank via shlex.
    Returns dict or None if not matched.
    """
    import shlex
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except Exception:
        return None
    # find income/outcome token
    type_idx = None
    for i, p in enumerate(parts):
        lp = p.lower()
        if lp in {"income", "outcome"}:
            type_idx = i
            break
    if type_idx is None:
        return None
    # need at least type, amount, category, bank (date between amount and category is optional)
    if len(parts) < type_idx + 4:
        return None
    desc_tokens = parts[:type_idx]
    if not desc_tokens:
        return None
    tx_type = parts[type_idx].lower()
    amount_token = parts[type_idx + 1]
    # check optional date/datetime token (supports two tokens like 'YYYY-MM-DD HH:MM')
    tx_at = None
    cat_idx = type_idx + 2
    try:
        # Try two-token datetime first
        if cat_idx + 1 < len(parts):
            probe2 = parts[cat_idx] + " " + parts[cat_idx + 1]
            try:
                tx_at = _parse_datetime_input(probe2)
                cat_idx += 2
            except Exception:
                tx_at = None
        # If two-token failed, try single token
        if tx_at is None:
            probe1 = parts[cat_idx]
            tx_at = _parse_datetime_input(probe1)
            cat_idx += 1
    except Exception:
        tx_at = None
    category = parts[cat_idx]
    bank = parts[cat_idx + 1] if len(parts) > cat_idx + 1 else None
    if bank is None:
        return None
    # parse amount
    try:
        amount = _parse_amount(amount_token)
    except Exception:
        return None
    desc = " ".join(desc_tokens)
    if len(desc.strip()) < 3:
        return None
    return {
        "desc": _first_sentence(desc),
        "type": tx_type,
        "amount": amount,
        "tx_at": tx_at,
        "category": category,
        "bank": bank,
    }

def _ocr_image_to_text(image_path: str) -> str:
    try:
        img = Image.open(image_path)
        # Preprocess: grayscale, autocontrast, increase contrast, sharpen, light threshold
        g = ImageOps.grayscale(img)
        g = ImageOps.autocontrast(g)
        g = ImageEnhance.Contrast(g).enhance(1.5)
        g = g.filter(ImageFilter.SHARPEN)
        # upscale if small
        try:
            if min(g.size) < 1200:
                scale = max(1.8, 1200 / float(min(g.size)))
                g = g.resize((int(g.width * scale), int(g.height * scale)), Image.LANCZOS)
        except Exception:
            pass
        try:
            g_bin = g.point(lambda x: 255 if x > 180 else 0)
        except Exception:
            g_bin = g

        texts: list[str] = []
        for lang in ("eng", "eng+ind"):
            for psm in (6, 4, 11):
                config = f"--oem 3 --psm {psm} -c preserve_interword_spaces=1"
                try:
                    texts.append(pytesseract.image_to_string(g, lang=lang, config=config) or "")
                    texts.append(pytesseract.image_to_string(g_bin, lang=lang, config=config) or "")
                except Exception:
                    continue
        # return the longest non-empty result
        best = max(texts, key=len) if texts else ""
        return best
    except Exception:
        return ""

def _pick_desc_from_text(text: str) -> str:
    # choose the first non-trivial line
    for line in (text or "").splitlines():
        s = line.strip()
        if len(s) >= 3 and not re.fullmatch(r"[0-9\s\-:./,]+", s):
            return _first_sentence(s)
    # fallback to first sentence of whole text
    return _first_sentence(text)

def _pick_amount_from_text(text: str) -> Decimal | None:
    """Heuristics to pick amount from OCR text.
    Priority:
    1) Tokens near currency markers (IDR/Rp)
    2) Tokens containing separators (comma/dot)
    Avoid long integer strings (likely reference numbers).
    """
    t = text or ""
    # 1) Contextual: after IDR/Rp
    ctx_tokens = re.findall(r"(?:IDR|Rp)\s*([0-9][0-9.,\s\u00A0\u202F]{1,})", t, re.I)
    for tok in ctx_tokens:
        try:
            return _parse_amount(tok)
        except Exception:
            continue
    # 2) Any number that has thousand/decimal separators
    tokens = re.findall(r"([0-9][0-9.,]{2,})", t)
    best: Decimal | None = None
    for tok in tokens:
        # skip plain long integers (no separators)
        if "," not in tok and "." not in tok:
            continue
        try:
            val = _parse_amount(tok)
        except Exception:
            continue
        if best is None or val > best:
            best = val
    return best

def _ocr_amount_via_data(image_path: str) -> Decimal | None:
    """Fallback: inspect word-level OCR to find amount near IDR/Rp tokens."""
    try:
        img = Image.open(image_path)
        g = ImageOps.grayscale(img)
        g = ImageOps.autocontrast(g)
        g = ImageEnhance.Contrast(g).enhance(1.5)
        try:
            if min(g.size) < 1200:
                scale = max(1.8, 1200 / float(min(g.size)))
                g = g.resize((int(g.width * scale), int(g.height * scale)), Image.LANCZOS)
        except Exception:
            pass
        data = pytesseract.image_to_data(g, lang="eng", config="--oem 3 --psm 6", output_type=Output.DICT)
        words = data.get("text", [])

        # 1) look for IDR/Rp then next few tokens
        for i, w in enumerate(words):
            if not w:
                continue
            t = w.strip()
            if t.upper() in {"IDR", "RP"}:
                buf = []
                for j in range(1, 6):
                    k = i + j
                    if k >= len(words):
                        break
                    t2 = (words[k] or "").strip()
                    if not t2:
                        continue
                    buf.append(t2)
                candidate = " ".join(buf)
                try:
                    val = _parse_amount(candidate)
                    return val
                except Exception:
                    continue

        # 2) fallback: any numeric token with separators
        best: Decimal | None = None
        for w in words:
            t = (w or "").strip()
            if not t:
                continue
            if "," in t or "." in t:
                try:
                    v = _parse_amount(t)
                except Exception:
                    continue
                if best is None or v > best:
                    best = v
        return best
    except Exception:
        return None

def _normalize_ocr_amount(amount: Decimal) -> Decimal:
    """For OCR sources, drop fractional part (e.g., 27,500.00 -> 27500)."""
    try:
        return amount.quantize(Decimal("1"), rounding=ROUND_DOWN)
    except Exception:
        return amount

def _detect_bank_from_text(text: str) -> str | None:
    t = (text or "").upper()
    if "BCA" in t or "BANK CENTRAL ASIA" in t:
        return "BCA"
    return None

def _parse_bca_receipt(text: str) -> dict:
    """
    Heuristics for BCA receipts/transfer slips. Returns partial fields.
    Keys: desc, amount, bank
    """
    out: dict = {"bank": "BCA"}
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    # amount candidates from lines containing typical BCA labels
    amount_labels = [
        "NOMINAL TUJUAN", "JUMLAH", "NOMINAL", "TOTAL", "JUMLAH TRANSFER",
        "TOTAL BAYAR", "TOTAL TRANSAKSI", "TOTAL PEMBAYARAN"
    ]
    amount_val: Decimal | None = None
    for ln in lines:
        u = ln.upper()
        if any(lbl in u for lbl in amount_labels) or "RP" in u or "IDR" in u:
            m = re.search(r"(?:RP|IDR)?\s*([0-9][0-9.,]{2,})", ln, re.I)
            if m:
                try:
                    amount_val = _parse_amount(m.group(1))
                    break
                except Exception:
                    pass
    if amount_val is None:
        amount_val = _pick_amount_from_text(text)
    if amount_val is not None:
        out["amount"] = amount_val

    # description: prefer BERITA/KETERANGAN block; else "Transfer ke <name>"; else first non-numeric line
    desc: str | None = None
    berita_seen = False
    berita_has_content = False
    # try BERITA section (can span multiple lines until next label)
    for i, ln in enumerate(lines):
        if re.search(r"^\s*BERITA\s*$", ln, re.I) or re.search(r"berita\s*[:\-]", ln, re.I):
            collected: list[str] = []
            # If line has content after separator, use that too
            parts = re.split(r":|-", ln, maxsplit=1)
            if len(parts) > 1 and parts[1].strip():
                collected.append(parts[1].strip())
            # collect following lines until next section header
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if re.search(r"^(?:NO\.?\s*REFERENSI|NOMOR\s*REFERENSI|REKENING\s*TUJUAN|JENIS\s*TRANSAKSI|MATA\s*UANG|DARI\s*REKENING|TRANSFER\s*BERHASIL)$", nxt, re.I):
                    break
                collected.append(nxt)
            candidate = " ".join(s for s in collected if s).strip()
            berita_seen = True
            berita_has_content = len(candidate) >= 3
            if len(candidate) >= 3:
                desc = _first_sentence(candidate)
            break
    for ln in lines:
        if desc:
            break
        if re.search(r"keterangan", ln, re.I):
            # take after separator or the rest of line
            part = re.split(r":|-", ln, maxsplit=1)
            candidate = part[-1].strip() if len(part) > 1 else ln
            if len(candidate) >= 3:
                desc = _first_sentence(candidate)
                break
    if not desc and not (berita_seen and not berita_has_content):
        # Transfer ke / Penerima
        for idx, ln in enumerate(lines):
            m = re.search(r"(transfer\s+ke|nama\s+penerima|penerima|kredit\s+ke)\s*[:\-]?\s*(.+)", ln, re.I)
            if m:
                candidate = m.group(2).strip()
                # stop at obvious trailing tokens
                candidate = re.split(r"\s{2,}|\s*Rp\s*", candidate)[0].strip()
                if len(candidate) >= 3:
                    desc = _first_sentence(f"Transfer ke {candidate}")
                    break
            # handle case label then next line is the value (like screenshot)
            if re.fullmatch(r"nama\s+penerima", ln, re.I) and idx + 1 < len(lines):
                candidate = lines[idx + 1].strip()
                if len(candidate) >= 3:
                    desc = _first_sentence(f"Transfer ke {candidate}")
                    break
    if not desc:
        desc = _pick_desc_from_text(text)
    if desc:
        out["desc"] = desc
    if berita_seen and not berita_has_content:
        out["berita_empty"] = True

    return out

def _parse_menu_choice(text: str) -> str | None:
    """Return '0'..'4' if text is a menu choice even with minor punctuation/space.
    '0' means cancel.
    """
    s = (text or "").strip()
    if not s:
        return None
    import re
    m = re.fullmatch(r"\s*([0-4])\s*[\.)\-:]*\s*", s)
    if m:
        return m.group(1)
    return None

async def list_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = await get_or_create_app_user_id(update)
        # join via dot notation
        sel = (
            'id, type, description, transaction_date, '
            'bank:bank_id(name), category:category_id(name)'
        )
        res = sb.table("transaction") \
                .select(sel) \
                .eq("user_id", user_id) \
                .order("transaction_date", desc=True) \
                .limit(10).execute()

        if not res.data:
            await update.message.reply_text("Belum ada transaksi.")
            await _show_menu(update)
            return

        lines = ["📜 10 transaksi terakhir:"]
        for r in res.data:
            ts = _format_dt_for_display(r.get('transaction_date'))
            lines.append(
                f"• {ts} [{r['type']}] "
                f"{r.get('description') or '-'} — {r['category']['name']} @ {r['bank']['name']}"
            )
        await update.message.reply_text("\n".join(lines))
        await _show_menu(update)

    except Exception as e:
        logging.exception("list failed")
        await update.message.reply_text(f"❌ Gagal mengambil data: {e}")
        await _show_menu(update)

# ---------- Summary ----------
async def show_summary(update: Update):
    try:
        # Note: show_summary is called via command, not conversation handler
        # We derive user via update
        user_id = await get_or_create_app_user_id(update)
        res = sb.table("transaction").select("type, amount").eq("user_id", user_id).execute()
        income = Decimal("0")
        outcome = Decimal("0")
        for r in (res.data or []):
            amt = r.get("amount")
            try:
                val = Decimal(str(amt)) if amt is not None else Decimal("0")
            except Exception:
                val = Decimal("0")
            if r.get("type") == "income":
                income += val
            elif r.get("type") == "outcome":
                outcome += val
        saldo = income - outcome
        msg = (
            "📊 Ringkasan:\n"
            f"• Total income: {_format_rp(income)}\n"
            f"• Total outcome: {_format_rp(outcome)}\n"
            f"• Saldo: {_format_rp(saldo)}"
        )
        await update.message.reply_text(msg)
        await _show_menu(update)
    except Exception as e:
        logging.exception("summary failed")
        await update.message.reply_text(f"❌ Gagal menghitung ringkasan: {e}")
        await _show_menu(update)

# ---------- Bootstrap ----------
# Conversational free-text flow (no command, numeric choices)
DESC, AMOUNT, TXDATE, TYPE, BANK, CATEGORY = range(6)

async def _show_menu(update: Update):
    user = update.effective_user
    fname = (getattr(user, "first_name", None) or getattr(user, "username", "")).strip()
    greet = f"Halo, {fname}! 👋" if fname else "Halo! 👋"
    await update.message.reply_text(
        greet + "\n\n"
        "Anda bisa memasukkan transaksi dalam satu baris dengan format:\n"
        "<deskripsi> <income|outcome> <nominal> <tanggal-opsional> <kategori> <bank>\n\n"
        "Contoh:\n"
        "Beli kopi sore ini outcome 12.500 2025-10-24 14:30 food BCA\n\n"
        "Catatan: jika ada spasi pada kategori/bank, gunakan kutip, misal: \"Transport Online\" atau \"BCA Digital\".\n"
        "Tanggal/waktu opsional (YYYY-MM-DD HH:MM / DD-MM-YYYY HH:MM / today / yesterday). Kosong/0=sekarang."
    )
    await update.message.reply_text(
        "Pilih menu:\n"
        "1) Tambah transaksi\n"
        "2) Lihat 10 transaksi terakhir\n"
        "3) Ringkasan total income/outcome\n"
        "4) Batal"
    )

async def free_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return ConversationHandler.END
    choice = _parse_menu_choice(text)
    if choice in {"0", "1", "2", "3", "4"}:
        if choice == "0":
            await free_cancel(update, context)
            return ConversationHandler.END
        if choice == "1":
            await update.message.reply_text("Tulis deskripsi transaksi:")
            return DESC
        if choice == "2":
            await list_tx(update, context)
            return ConversationHandler.END
        if choice == "3":
            await show_summary(update)
            return ConversationHandler.END
        if choice == "4":
            await free_cancel(update, context)
            return ConversationHandler.END
    # full inline format: <desc> <income|outcome> <amount> <category> <bank>
    parsed = _try_parse_inline_full(text)
    if parsed is not None:
        try:
            user_id = await get_or_create_app_user_id(update)
            bank_id = await get_or_create_id("bank", parsed["bank"], None)
            category_id = await get_or_create_id("category", parsed["category"], None)
            sb.table("transaction").insert({
                "bank_id": bank_id,
                "category_id": category_id,
                "type": parsed["type"],
                "amount": float(parsed["amount"]),
                "description": parsed["desc"],
                "transaction_date": parsed.get("tx_at") or _now_iso(),
                "user_id": user_id,
            }).execute()
            ts = _format_dt_for_display(parsed.get("tx_at") or _now_iso())
            await update.message.reply_text(
                f"✅ Tersimpan { _format_rp(parsed['amount']) }: [{parsed['type']}] "
                f"{parsed['desc']} — {ts} — {parsed['category']} @ {parsed['bank']}"
            )
            await _show_menu(update)
            return ConversationHandler.END
        except Exception as e:
            logging.exception("inline full-add failed")
            await update.message.reply_text(
                f"❌ Gagal menyimpan dari format satu baris: {e}"
            )
            await _show_menu(update)
            return ConversationHandler.END
    # support: "1 <deskripsi>" in a single message
    if text.startswith("1 ") and len(text) > 2:
        desc = _first_sentence(text[2:])
        if len(desc) < 3:
            await update.message.reply_text("Deskripsi terlalu pendek. Tulis deskripsi transaksi:")
            return DESC
        context.user_data["desc"] = desc
        await update.message.reply_text("Nominal? (contoh: 12.500)")
        return AMOUNT

    # treat as quick add: whole text becomes description
    context.user_data["desc"] = _first_sentence(text)
    await update.message.reply_text("Nominal? (contoh: 12.500)")
    return AMOUNT

async def ocr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Download the highest resolution photo
    try:
        status_msg = await update.message.reply_text("🔎 Membaca gambar…")
        photo = update.message.photo[-1]
        f = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await f.download_to_drive(custom_path=tmp_path)
        await status_msg.edit_text("🧠 Memproses OCR…")
        text = _ocr_image_to_text(tmp_path)
        # bank-specific parsing (BCA) if detected
        bank_hint = _detect_bank_from_text(text)
        desc = None
        amount = None
        if bank_hint == "BCA":
            parsed = _parse_bca_receipt(text)
            desc = parsed.get("desc")
            amount = parsed.get("amount")
            context.user_data["bank"] = parsed.get("bank") or "BCA"
            if parsed.get("berita_empty") and not desc:
                # explicitly ask for manual description if BERITA exists but empty
                if amount is not None:
                    context.user_data["amount"] = _normalize_ocr_amount(amount)
                await update.message.reply_text(
                    "Bagian 'Berita' kosong. Tulis deskripsi transaksi:"
                )
                return DESC
        if not desc:
            desc = _pick_desc_from_text(text)
        if amount is None:
            amount = _pick_amount_from_text(text)
        if amount is None:
            amount = _ocr_amount_via_data(tmp_path)
        if amount is not None:
            amount = _normalize_ocr_amount(amount)

        if desc and len(desc) >= 3:
            context.user_data["desc"] = desc
        if amount is not None:
            context.user_data["amount"] = amount

        # Decide next step
        if "amount" not in context.user_data:
            await update.message.reply_text(
                "Tidak menemukan nominal di gambar. Ketik nominal (contoh: 12.500)"
            )
            if DEBUG_OCR:
                snippet = (text or "").strip().replace("\n\n", "\n")
                await update.message.reply_text(
                    "[Debug OCR]\n" + (snippet[:1000] + ("…" if len(snippet) > 1000 else ""))
                )
            await status_msg.edit_text("ℹ️ Nominal belum terbaca. Mohon ketik nominal.")
            return AMOUNT
        if "desc" not in context.user_data:
            await update.message.reply_text(
                "Tidak menemukan deskripsi. Tulis deskripsi transaksi:"
            )
            await status_msg.edit_text("ℹ️ Deskripsi belum terbaca. Mohon ketik deskripsi.")
            return DESC

        # For BCA receipts, default to outcome and skip type step
        if bank_hint == "BCA":
            context.user_data["type"] = "outcome"
            await status_msg.edit_text("✅ OCR selesai.")
            # proceed to bank selection
            banks = sb.table("bank").select("name").order("name").execute().data or []
            names = [b["name"] for b in banks]
            context.user_data["bank_options"] = names
            header = (
                f"Terbaca: { _format_rp(context.user_data['amount']) } — {context.user_data['desc']}\n"
                f"Tipe: outcome"
            )
            if context.user_data.get("bank"):
                header += f"\nBank: {context.user_data['bank']} (bisa ubah)"
            if names:
                lines = [header, "", "Pilih bank:"]
                for i, n in enumerate(names, 1):
                    lines.append(f"{i}) {n}")
                await update.message.reply_text("\n".join(lines))
            else:
                await update.message.reply_text(header + "\nBelum ada bank. Ketik nama bank baru:")
            return BANK

        await update.message.reply_text(
            f"Terbaca: { _format_rp(context.user_data['amount']) } — {context.user_data['desc']}\n"
            "Pilih tipe: 1) income  2) outcome"
            + (f"\nBank: {context.user_data.get('bank')} (bisa ubah nanti)" if context.user_data.get('bank') else "")
        )
        await status_msg.edit_text("✅ OCR selesai.")
        return TYPE
    except Exception as e:
        logging.exception("ocr failed")
        await update.message.reply_text(
            "Maaf, gagal membaca gambar. Silakan input manual atau kirim foto lain."
        )
        try:
            await status_msg.edit_text("❌ Gagal memproses OCR.")
        except Exception:
            pass
        await _show_menu(update)
        return ConversationHandler.END

async def free_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip() == "0":
        await free_cancel(update, context)
        return ConversationHandler.END
    desc = _first_sentence(update.message.text)
    if not desc or len(desc) < 3:
        await update.message.reply_text("Deskripsi tidak boleh kosong. Tulis deskripsi transaksi:")
        return DESC
    context.user_data["desc"] = desc
    await update.message.reply_text("Nominal? (contoh: 12.500)")
    return AMOUNT

async def free_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip() == "0":
        await free_cancel(update, context)
        return ConversationHandler.END
    try:
        amount = _parse_amount(update.message.text)
    except ValueError:
        await update.message.reply_text("Nominal tidak valid. Coba lagi, contoh: 12.500")
        return AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text(
        "Tanggal/waktu transaksi? (contoh: 2025-10-30 14:30, 30-10-2025, today, yesterday).\n"
        "Ketik 0 untuk sekarang."
    )
    return TXDATE

async def free_txdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip() == "0":
        context.user_data["tx_at"] = _now_iso()
        await update.message.reply_text("Tipe? 1) income  2) outcome")
        return TYPE
    try:
        txd = _parse_datetime_input(update.message.text)
    except ValueError:
        await update.message.reply_text(
            "Tanggal/waktu tidak valid. Contoh: 2025-10-30 14:30 atau 30-10-2025, atau 0 untuk sekarang."
        )
        return TXDATE
    context.user_data["tx_at"] = txd
    await update.message.reply_text("Tipe? 1) income  2) outcome")
    return TYPE

async def free_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()
    if t == "0":
        await free_cancel(update, context)
        return ConversationHandler.END
    if t in {"1", "income"}:
        t = "income"
    elif t in {"2", "outcome"}:
        t = "outcome"
    else:
        await update.message.reply_text("Pilih '1' untuk income atau '2' untuk outcome.")
        return TYPE
    context.user_data["type"] = t
    # list banks (shared)
    banks = sb.table("bank").select("name").order("name").execute().data or []
    names = [b["name"] for b in banks]
    context.user_data["bank_options"] = names
    if names:
        lines = ["Pilih bank (ketik angka atau tulis nama bank baru):"]
        for i, n in enumerate(names, 1):
            lines.append(f"{i}) {n}")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("Belum ada bank. Ketik nama bank baru:")
    return BANK

async def free_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    options = context.user_data.get("bank_options", [])
    chosen = None
    if text == "0":
        await free_cancel(update, context)
        return ConversationHandler.END
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(options):
            chosen = options[idx - 1]
    if chosen is None:
        if not text:
            await update.message.reply_text("Nama bank tidak boleh kosong. Ketik nama bank.")
            return BANK
        chosen = text
    context.user_data["bank"] = chosen
    # list categories (shared)
    cats = sb.table("category").select("name").order("name").execute().data or []
    names = [c["name"] for c in cats]
    context.user_data["cat_options"] = names
    if names:
        lines = ["Pilih kategori (ketik angka atau tulis nama kategori baru):"]
        for i, n in enumerate(names, 1):
            lines.append(f"{i}) {n}")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("Belum ada kategori. Ketik nama kategori baru:")
    return CATEGORY

async def free_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = (update.message.text or "").strip()
        options = context.user_data.get("cat_options", [])
        chosen = None
        if text == "0":
            await free_cancel(update, context)
            return ConversationHandler.END
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(options):
                chosen = options[idx - 1]
        if chosen is None:
            if not text:
                await update.message.reply_text("Kategori tidak boleh kosong. Ketik kategori.")
                return CATEGORY
            chosen = text

        tx_type = context.user_data.get("type")
        bank = context.user_data.get("bank")
        desc = context.user_data.get("desc")
        amount = context.user_data.get("amount")

        user_id = await get_or_create_app_user_id(update)
        bank_id = await get_or_create_id("bank", bank, None)
        category_id = await get_or_create_id("category", chosen, None)

        sb.table("transaction").insert({
            "bank_id": bank_id,
            "category_id": category_id,
            "type": tx_type,
            "amount": float(amount) if amount is not None else None,
            "description": desc or None,
            "transaction_date": context.user_data.get("tx_at") or _now_iso(),
            "user_id": user_id,
        }).execute()

        ts = _format_dt_for_display(context.user_data.get("tx_at") or _now_iso())
        await update.message.reply_text(
            f"✅ Tersimpan { _format_rp(amount) }: [{tx_type}] {desc or '-'} — {ts} — {chosen} @ {bank}"
        )
        await _show_menu(update)
    except Exception as e:
        logging.exception("free-text save failed")
        await update.message.reply_text(f"❌ Gagal menyimpan: {e}")
    finally:
        context.user_data.clear()
    return ConversationHandler.END

async def free_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Dibatalkan.")
    await _show_menu(update)
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation for free-text inputs (non-command messages)
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, free_entry),
            MessageHandler(filters.PHOTO, ocr_photo),
        ],
        states={
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_desc)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_amount)],
            TXDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_txdate)],
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_type)],
            BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_bank)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_category)],
        },
        fallbacks=[CommandHandler("cancel", free_cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_tx))
    app.run_polling()

if __name__ == "__main__":
    main()
