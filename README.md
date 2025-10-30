## Yone Cashflow – Telegram Bot

Simple personal cashflow tracker via Telegram. Stores data in Supabase. Supports free-text input, one-line quick add, and OCR from receipts. Multi-user safe: each Telegram user sees only their own transactions. `bank` and `category` are shared globally.

### Features
- Add transactions by conversation or one-line message
- OCR receipts (optional) to guess description/amount
- Datetime support (e.g., `2025-10-24 14:30`), stored with timezone offset
- Per-user isolation for `transaction` using Telegram ID (shared taxonomy)

### Prerequisites
- Python 3.11+
- Tesseract OCR (for image reading) – optional but recommended
  - macOS: `brew install tesseract`
  - Linux (Debian/Ubuntu): `sudo apt-get install tesseract-ocr`

### Environment variables (.env)
Create a `.env` file next to `main.py`:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_SERVICE_KEY=YOUR_SERVICE_ROLE_KEY

# Optional
APP_TIMEZONE=Asia/Jakarta   # default Asia/Jakarta
DEBUG_OCR=true              # show OCR text when image parsing fails
```

### Supabase schema (minimum)
You need these tables:

```
app_user(id uuid PK default gen_random_uuid(), telegram_id bigint unique, ...)
bank(id uuid PK default gen_random_uuid(), name text unique not null)
category(id uuid PK default gen_random_uuid(), name text unique not null)
transaction(
  id uuid PK default gen_random_uuid(),
  bank_id uuid references bank(id),
  category_id uuid references category(id),
  user_id uuid references app_user(id),
  type text check (type in ('income','outcome')),
  amount numeric,
  description text,
  transaction_date timestamptz
)
```

Notes:
- `bank` and `category` are SHARED; uniqueness is by `name` only.
- `transaction.user_id` links a row to the Telegram user using the bot.
- `transaction_date` should be `timestamptz` so timezone offsets are preserved.

If you already have data and want to backfill one user:

```
-- Replace 123456789 with your Telegram numeric ID
with u as (
  insert into app_user (telegram_id)
  values (123456789)
  on conflict (telegram_id) do nothing
  returning id
), u2 as (
  select id from u
  union all select id from app_user where telegram_id = 123456789
)
update "transaction" t set user_id = u2.id from u2 where t.user_id is null;
```

### Install and run
```
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install python-telegram-bot==21.6 supabase==2.6.0 python-dotenv==1.0.1 Pillow==10.4.0 pytesseract==0.3.13

python main.py
```

### Usage
- Start: send `/start` to the bot.
- One-line quick add format:
  - `<desc> <income|outcome> <amount> <optional-date> <category> <bank>`
  - Example: `Beli kopi sore ini outcome 12.500 2025-10-24 14:30 food BCA`
  - Amount accepts thousand separators like `12.500` (parsed as 12500).
  - Date/time examples: `2025-10-24 14:30`, `2025-10-24`, `today`, `yesterday`.
- Conversation flow (no command): just type; the bot will ask step-by-step.
- OCR: send a photo of a receipt. The bot attempts to extract amount/description.

### Timezone behavior
- Local timezone is controlled by `APP_TIMEZONE` (default `Asia/Jakarta`).
- Stored format includes offset, e.g., `2025-10-24 14:30:00+07:00`.
- Lists are displayed back in the same local timezone.

### Notes on sharing the bot
- You can share the bot link. Each Telegram user gets their own `app_user` entry automatically on first use, and only sees their own transactions.
- `bank` and `category` are shared across users; names are global.

### Troubleshooting
- Cannot OCR: ensure Tesseract is installed and accessible in PATH.
- Supabase permissions: if using RLS, add policies allowing the bot service role to read/write, or add proper user-scoped policies using `user_id`.


