"""
============================================================================
تلگرام بات — ماژول یکپارچه ربات تلگرام برای Spider Panel
============================================================================
این ماژول شامل دو بخش است:

۱. API مدیریت ربات (GET/POST /api/telegram/*) که از طریق پنل Spider قابل
   کنترل است (تنظیم توکن، راه‌اندازی/توقف، تست، آمار).

۲. پیاده‌سازی کامل ربات با python-telegram-bot v21:
   - /start: خوش‌آمدگویی + نمایش کانفیگ کاربر
   - /myconfig: دریافت کانفیگ اختصاصی
   - /help: راهنما
   - /stats: آمار سرور (فقط ادمین)
   - /admin: پنل ادمین
   - سیستم فروشگاه با پلن‌ها، موجودی، و ساخت کانفیگ واقعی

تنظیمات ربات در SETTINGS اصلی تحت کلید 'telegram_bot' ذخیره می‌شود.
تمام ایمپورت‌های main به صورت lazy (داخل تابع) انجام می‌شود تا از
import دوری (circular import) جلوگیری شود.
============================================================================
"""

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger("Spider-Telegram")

# ── Module-level bot state ─────────────────────────────────────────────────
_bot_app: Optional[object] = None          # Instance of Application (or None)
_bot_task: Optional[asyncio.Task] = None    # Background asyncio task
_bot_lock = asyncio.Lock()                  # Concurrency guard
_bot_stats = {                              # Basic runtime stats
    "started_at": None,
    "users_count": 0,
    "messages_sent": 0,
    "errors": 0,
}

# ── Default plans ──────────────────────────────────────────────────────────
DEFAULT_PLANS = [
    {"name": "Basic", "gb": 5, "days": 30, "price": 50000},
    {"name": "Pro", "gb": 15, "days": 30, "price": 120000},
    {"name": "Premium", "gb": 50, "days": 30, "price": 300000},
]
DEFAULT_CURRENCY = "تومان"

# ── Router ─────────────────────────────────────────────────────────────────
router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# Helpers (lazy imports from main)
# ══════════════════════════════════════════════════════════════════════════════

def _get_settings():
    """Lazy import SETTINGS / SETTINGS_LOCK from main to avoid circular imports."""
    import main as _m
    return _m.SETTINGS, _m.SETTINGS_LOCK


def _get_users():
    """Lazy import USERS / USERS_LOCK / generate_user_config from main."""
    import main as _m
    return _m.USERS, _m.USERS_LOCK, _m.generate_user_config


def _get_links():
    """Lazy import LINKS / LINKS_LOCK from main."""
    import main as _m
    return _m.LINKS, _m.LINKS_LOCK


def _get_inbounds():
    import main as _m
    return _m.INBOUNDS, _m.INBOUNDS_LOCK


def _get_state_helpers():
    import main as _m
    return _m.save_state, _m.log_activity, _m.hash_password, _m.generate_short_id, _m.generate_uuid


async def _require_auth(request: Request):
    """Wrapper that can be used as FastAPI dependency in this module.

    Delegates to main.require_auth (imported lazily to avoid circular imports).
    """
    import main as _m
    return await _m.require_auth(request)


# ══════════════════════════════════════════════════════════════════════════════
# Bot settings helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_telegram_settings():
    """Ensure the telegram_bot key exists in SETTINGS with defaults."""
    SETTINGS, SETTINGS_LOCK = _get_settings()
    if "telegram_bot" not in SETTINGS:
        SETTINGS["telegram_bot"] = {
            "bot_token": "",
            "admin_ids": [],
            "webhook_url": "",
            "enabled": False,
            "mock_mode": False,
            "plans": list(DEFAULT_PLANS),
            "currency": DEFAULT_CURRENCY,
        }


def _mask_token(token: str) -> str:
    """Mask bot token for display (e.g. '12345...6789')."""
    if not token or len(token) < 10:
        return "********"
    return token[:6] + "..." + token[-4:]


# ══════════════════════════════════════════════════════════════════════════════
# Bot Database (SQLite for bot-side shop state)
# ══════════════════════════════════════════════════════════════════════════════

import aiosqlite
from pathlib import Path

_BOT_DB_PATH = Path(os.environ.get("BOT_DB_PATH", "/app/state/bot.db"))


class _BotDatabase:
    """SQLite store for Telegram bot shop state (balances, orders, configs)."""
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id   INTEGER PRIMARY KEY,
        username      TEXT,
        first_name    TEXT,
        balance       INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders (
        order_id      TEXT PRIMARY KEY,
        telegram_id   INTEGER NOT NULL,
        plan_id       TEXT NOT NULL,
        gb            INTEGER NOT NULL,
        days          INTEGER NOT NULL,
        amount        INTEGER NOT NULL,
        operator      TEXT,
        server        TEXT,
        status        TEXT NOT NULL DEFAULT 'pending',
        created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(telegram_id);
    CREATE TABLE IF NOT EXISTS configs (
        uuid          TEXT PRIMARY KEY,
        telegram_id   INTEGER NOT NULL,
        order_id      TEXT,
        server        TEXT NOT NULL,
        inbound_id    TEXT NOT NULL,
        operator      TEXT,
        vless_link    TEXT NOT NULL,
        traffic_limit_bytes INTEGER NOT NULL,
        expire_at     TEXT,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_configs_user ON configs(telegram_id);
    """

    def __init__(self, path: Path):
        self._path = path
        self._conn = None

    async def init(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(self.SCHEMA)
        await self._conn.commit()

    @property
    def conn(self):
        if self._conn is None:
            raise RuntimeError("Database not initialised")
        return self._conn

    async def upsert_user(self, telegram_id, username, first_name, now):
        await self.conn.execute(
            """INSERT INTO users (telegram_id, username, first_name, balance, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name, updated_at=excluded.updated_at""",
            (telegram_id, username, first_name, now, now))
        await self.conn.commit()

    async def add_balance(self, telegram_id, amount):
        await self.conn.execute(
            "UPDATE users SET balance = balance + ? WHERE telegram_id=?", (amount, telegram_id))
        await self.conn.commit()

    async def get_balance(self, telegram_id):
        cur = await self.conn.execute("SELECT balance FROM users WHERE telegram_id=?", (telegram_id,))
        row = await cur.fetchone()
        return int(row["balance"]) if row else 0

    async def create_order(self, order_id, telegram_id, plan_id, gb, days, amount, operator, server, created_at):
        await self.conn.execute(
            """INSERT INTO orders (order_id, telegram_id, plan_id, gb, days, amount, operator, server, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (order_id, telegram_id, plan_id, gb, days, amount, operator, server, created_at))
        await self.conn.commit()

    async def set_order_status(self, order_id, status):
        await self.conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        await self.conn.commit()

    async def save_config(self, uuid, telegram_id, order_id, server, inbound_id, operator, vless_link, traffic_limit_bytes, expire_at, created_at):
        await self.conn.execute(
            """INSERT INTO configs (uuid, telegram_id, order_id, server, inbound_id, operator, vless_link, traffic_limit_bytes, expire_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid, telegram_id, order_id, server, inbound_id, operator, vless_link, traffic_limit_bytes, expire_at, created_at))
        await self.conn.commit()

    async def list_user_configs(self, telegram_id):
        cur = await self.conn.execute(
            "SELECT * FROM configs WHERE telegram_id=? ORDER BY created_at DESC", (telegram_id,))
        return [dict(r) for r in await cur.fetchall()]


_bot_db: Optional[_BotDatabase] = None


def get_bot_db() -> _BotDatabase:
    global _bot_db
    if _bot_db is None:
        _bot_db = _BotDatabase(_BOT_DB_PATH)
    return _bot_db


# ══════════════════════════════════════════════════════════════════════════════
# i18n (Persian strings)
# ══════════════════════════════════════════════════════════════════════════════

_I18N = {
    "welcome": "به فروشگاه VPN خوش آمدید 🕷️\n\nیک پلن انتخاب کنید:",
    "choose_operator": "اپراتور مورد نظر خود را انتخاب کنید:",
    "choose_plan": "پلن مورد نظر را انتخاب کنید:",
    "balance": "💰 موجودی: {amount} {currency}",
    "insufficient_balance": "موجودی کافی نیست. لطفاً شارژ کنید.",
    "order_created": "✅ سفارش ثبت شد.\nشناسه: {order_id}",
    "config_ready": "⚡ کانفیگ شما آماده است:",
    "my_configs": "📋 کانفیگ‌های شما:",
    "no_configs": "هنوز کانفیگی ندارید.",
    "admin_panel": "🔐 پنل مدیریت",
    "admin_stats": "📊 آمار سرورها:",
    "invalid_input": "ورودی نامعتبر است.",
    "cancel": "لغو",
    "back": "بازگشت",
    "help": (
        "📖 <b>راهنمای ربات Spider</b>\n\n"
        "🚀 <b>دستورات:</b>\n"
        "/start - شروع و دریافت کانفیگ\n"
        "/myconfig - دریافت کانفیگ اختصاصی\n"
        "/help - نمایش این راهنما\n"
        "/balance - مشاهده موجودی\n"
        "/admin - پنل مدیریت (ادمین)\n\n"
        "💡 برای خرید پلن از دکمه‌های زیر استفاده کنید."
    ),
}

_OPERATORS = ["MTN", "MCI", "Irancell", "Hamrah", "Rightel"]


def _t(key: str, **kwargs) -> str:
    s = _I18N.get(key, key)
    try:
        return s.format(**kwargs)
    except Exception:
        return s


# ══════════════════════════════════════════════════════════════════════════════
# Bot Handlers (python-telegram-bot v21)
# ══════════════════════════════════════════════════════════════════════════════

def _now_ir():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Tehran"))


async def _run_bot(token: str, admin_ids: list[int], plans: list, currency: str, mock_mode: bool):
    """
    Run the Telegram bot in a background asyncio task.
    If python-telegram-bot is not installed, logs a warning.
    """
    global _bot_app, _bot_stats

    try:
        from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
        from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
        import qrcode
        from io import BytesIO
    except ImportError:
        logger.warning(
            "python-telegram-bot نصب نیست. "
            "برای نصب دستور زیر را اجرا کنید:\n"
            "  pip install python-telegram-bot"
        )
        return

    try:
        db = get_bot_db()
        await db.init()
    except Exception as e:
        logger.error(f"Failed to init bot DB: {e}")

    # ── Helpers ──────────────────────────────────────────────────────────
    def _is_admin(uid: int) -> bool:
        return uid in admin_ids

    def _plan_keyboard():
        buttons = []
        for i, p in enumerate(plans):
            label = f"{p.get('name', f'Plan {i+1}')} — {p.get('gb', 0)}GB / {p.get('days', 0)}روز — {p.get('price', 0)} {currency}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"plan:{i}")])
        return InlineKeyboardMarkup(buttons)

    def _operator_keyboard():
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(op, callback_data=f"op:{op}") for op in _OPERATORS
        ]])

    def _make_qr(vless_link: str) -> BytesIO:
        img = qrcode.make(vless_link)
        bio = BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio

    async def _build_config_for_user(telegram_id: int, plan: dict) -> dict:
        """Create a real VLESS config via Spider's internal API."""
        USERS, USERS_LOCK, gen_config = _get_users()
        username = f"tg_{telegram_id}"
        # Check if user already exists
        user_id = None
        async with USERS_LOCK:
            for uid, u in USERS.items():
                if u.get("telegram_id") == telegram_id or u.get("username") == username:
                    user_id = uid
                    break

        if user_id is None:
            # Create new user via internal API
            import main as _m
            try:
                new_user = await _m.create_user_internal(
                    username=username,
                    traffic_limit_gb=plan.get("gb", 5),
                    expire_days=plan.get("days", 30),
                    concurrent_connections=2,
                    telegram_id=telegram_id,
                )
                user_id = new_user["user_id"]
            except Exception as e:
                logger.error(f"Failed to create user: {e}")
                raise

        # Generate config
        user_data = USERS.get(user_id, {})
        config = gen_config(user_id, user_data, user_data.get("inbound_id"))
        config_uuid = user_data.get("config_uuid", "")
        import main as _m2
        host = _m2.SETTINGS.get("domain") or _m2.get_host()
        vless = f"vless://{config_uuid}@{host}:443?encryption=none&security=reality&type=tcp#{username}" if config_uuid else config
        return {"user_id": user_id, "vless_link": vless, "config": config}

    # ── Command handlers ──────────────────────────────────────────────────
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
        uid = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name or ""
        db = get_bot_db()
        await db.upsert_user(uid, username, update.effective_user.first_name, _now_ir().isoformat())
        await update.message.reply_text(_t("welcome"), reply_markup=_operator_keyboard())

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(_t("help"), parse_mode="HTML")

    async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        bal = await get_bot_db().get_balance(uid)
        await update.message.reply_text(_t("balance", amount=bal, currency=currency))

    async def cmd_myconfigs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        rows = await get_bot_db().list_user_configs(uid)
        if not rows:
            await update.message.reply_text(_t("no_configs"))
            return
        text = _t("my_configs") + "\n\n"
        for r in rows[:10]:
            text += f"• {r['operator'] or '—'} @ {r['server']}\n{r['vless_link']}\n\n"
        await update.message.reply_text(text)

    async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id):
            return
        await update.message.reply_text(_t("admin_panel"))

    async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update.effective_user.id):
            return
        import main as _m
        try:
            stats = await _m.get_server_stats()
            lines = [_t("admin_stats")]
            lines.append(f"• کاربران: {stats.get('total_users', 0)}")
            lines.append(f"• ترافیک: {stats.get('traffic_usage_gb', 0):.2f} GB")
            lines.append(f"• اتصالات: {stats.get('active_connections', 0)}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"خطا: {e}")

    # ── Callback handlers ─────────────────────────────────────────────────
    async def cb_operator(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        op = q.data.split(":", 1)[1]
        context.user_data["operator"] = op
        await q.edit_message_text(_t("choose_plan"), reply_markup=_plan_keyboard())

    async def cb_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        pid = int(q.data.split(":", 1)[1])
        if pid >= len(plans):
            await q.edit_message_text(_t("invalid_input"))
            return
        plan = plans[pid]
        uid = q.from_user.id
        db = get_bot_db()
        bal = await db.get_balance(uid)
        price = plan.get("price", 0)
        if bal < price:
            await q.edit_message_text(_t("insufficient_balance"))
            return

        # Deduct balance
        await db.add_balance(uid, -price)
        order_id = f"ord_{uid}_{int(_now_ir().timestamp())}"
        await db.create_order(
            order_id=order_id, telegram_id=uid, plan_id=str(pid),
            gb=plan.get("gb", 5), days=plan.get("days", 30), amount=price,
            operator=context.user_data.get("operator"), server=None,
            created_at=_now_ir().isoformat())
        await db.set_order_status(order_id, "paid")

        try:
            result = await _build_config_for_user(uid, plan)
            link = result["vless_link"]
            # Save config to DB
            expire = (_now_ir() + timedelta(days=plan.get("days", 30))).isoformat()
            await db.save_config(
                uuid=result["user_id"], telegram_id=uid, order_id=order_id,
                server="spider", inbound_id="default", operator=context.user_data.get("operator"),
                vless_link=link, traffic_limit_bytes=plan.get("gb", 5) * 1024**3,
                expire_at=expire, created_at=_now_ir().isoformat())

            # Send QR + link
            qr_bio = _make_qr(link)
            await q.message.reply_photo(
                InputFile(qr_bio, filename="config.png"),
                caption=_t("config_ready") + f"\n\n{link}")
            await q.edit_message_text(
                f"{_t('order_created', order_id=order_id)}\n⚡ {link}")
        except Exception as e:
            logger.error(f"Config build failed: {e}")
            await q.edit_message_text(f"❌ خطا در ساخت کانفیگ: {e}")

    # ── Build application ──────────────────────────────────────────────────
    builder = ApplicationBuilder().token(token)
    SETTINGS, _ = _get_settings()
    tb = SETTINGS.get("telegram_bot", {})
    webhook_url = tb.get("webhook_url", "")

    if webhook_url:
        builder = builder.connect_webhook(
            webhook_url=webhook_url.rstrip("/") + "/telegram-webhook"
        )

    app = builder.build()
    _bot_app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("myconfigs", cmd_myconfigs))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("adminstats", cmd_admin_stats))
    app.add_handler(CallbackQueryHandler(cb_operator, pattern=r"^op:"))
    app.add_handler(CallbackQueryHandler(cb_plan, pattern=r"^plan:"))

    _bot_stats["started_at"] = _now_ir().isoformat()
    _bot_stats["users_count"] = len(admin_ids)

    if webhook_url:
        await app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    else:
        await app.run_polling()


# ══════════════════════════════════════════════════════════════════════════════
# Bot Control (start/stop)
# ══════════════════════════════════════════════════════════════════════════════

async def _start_bot_task():
    """Start bot in background asyncio task."""
    global _bot_task, _bot_stats
    async with _bot_lock:
        if _bot_task and not _bot_task.done():
            logger.info("Bot already running")
            return

        SETTINGS, _ = _get_settings()
        tb = SETTINGS.get("telegram_bot", {})
        token = tb.get("bot_token", "")
        if not token:
            raise HTTPException(status_code=400, detail="Bot token not set")

        admin_ids = tb.get("admin_ids", [])
        plans = tb.get("plans", DEFAULT_PLANS)
        currency = tb.get("currency", DEFAULT_CURRENCY)
        mock_mode = tb.get("mock_mode", False)

        _bot_task = asyncio.create_task(
            _run_bot(token, admin_ids, plans, currency, mock_mode)
        )
        _bot_stats["started_at"] = _now_ir().isoformat()
        logger.info("Telegram bot started")


async def _stop_bot_task():
    """Stop running bot."""
    global _bot_app, _bot_task, _bot_stats
    async with _bot_lock:
        if _bot_app:
            try:
                await _bot_app.stop()
                await _bot_app.shutdown()
            except Exception as e:
                logger.warning(f"Error stopping bot: {e}")
            _bot_app = None
        if _bot_task and not _bot_task.done():
            _bot_task.cancel()
            try:
                await _bot_task
            except asyncio.CancelledError:
                pass
            _bot_task = None
        _bot_stats["started_at"] = None
        logger.info("Telegram bot stopped")


# ══════════════════════════════════════════════════════════════════════════════
# API Routes
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/telegram/settings")
async def get_telegram_settings(_=Depends(_require_auth)):
    """Get bot settings (mask token)."""
    _ensure_telegram_settings()
    SETTINGS, _ = _get_settings()
    tb = dict(SETTINGS.get("telegram_bot", {}))
    tb["bot_token"] = _mask_token(tb.get("bot_token", "")) if tb.get("bot_token") else ""
    tb["running"] = _bot_task is not None and not _bot_task.done()
    return tb


@router.post("/api/telegram/settings")
async def save_telegram_settings(request: Request, _=Depends(_require_auth)):
    """Save bot settings."""
    global _bot_stats
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    SETTINGS, SETTINGS_LOCK = _get_settings()
    async with SETTINGS_LOCK:
        tb = SETTINGS.setdefault("telegram_bot", {})
        tb["bot_token"] = body.get("bot_token", tb.get("bot_token", ""))
        tb["admin_ids"] = body.get("admin_ids", tb.get("admin_ids", []))
        tb["webhook_url"] = body.get("webhook_url", tb.get("webhook_url", ""))
        tb["enabled"] = body.get("enabled", tb.get("enabled", False))
        tb["mock_mode"] = body.get("mock_mode", tb.get("mock_mode", False))
        tb["currency"] = body.get("currency", tb.get("currency", DEFAULT_CURRENCY))

        # Plans: accept both array and dict formats
        plans_in = body.get("plans", tb.get("plans", DEFAULT_PLANS))
        if isinstance(plans_in, dict):
            tb["plans"] = [{"name": k, **v} if isinstance(v, dict) else v for k, v in plans_in.items()]
        elif isinstance(plans_in, list):
            tb["plans"] = plans_in
        else:
            tb["plans"] = list(DEFAULT_PLANS)

    # Save state
    save_state, _, _, _, _ = _get_state_helpers()
    await save_state()

    _bot_stats["users_count"] = len(tb.get("admin_ids", []))
    return {"ok": True, "settings": _mask_settings(tb)}


@router.post("/api/telegram/start")
async def start_telegram_bot(_=Depends(_require_auth)):
    """Start the bot in background."""
    try:
        await _start_bot_task()
        return {"ok": True, "status": "starting"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/telegram/stop")
async def stop_telegram_bot(_=Depends(_require_auth)):
    """Stop the running bot."""
    try:
        await _stop_bot_task()
        return {"ok": True, "status": "stopped"}
    except Exception as e:
        logger.error(f"Failed to stop bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/telegram/status")
async def telegram_bot_status(_=Depends(_require_auth)):
    """Get bot running status and basic stats."""
    running = _bot_task is not None and not _bot_task.done()
    return {
        "running": running,
        "mock_mode": _get_settings()[0].get("telegram_bot", {}).get("mock_mode", False),
        "stats": {
            "started_at": _bot_stats.get("started_at"),
            "users_count": _bot_stats.get("users_count", 0),
            "messages_sent": _bot_stats.get("messages_sent", 0),
            "errors": _bot_stats.get("errors", 0),
        },
        "uptime_seconds": (
            int((datetime.now() - datetime.fromisoformat(_bot_stats["started_at"])).total_seconds())
            if _bot_stats.get("started_at") else 0
        ),
    }


@router.post("/api/telegram/test")
async def test_telegram_bot(_=Depends(_require_auth)):
    """Test bot connection by calling getMe API."""
    SETTINGS, _ = _get_settings()
    tb = SETTINGS.get("telegram_bot", {})
    token = tb.get("bot_token", "")
    if not token:
        raise HTTPException(status_code=400, detail="Bot token not set")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if r.status_code == 200:
                data = r.json()
                if data.get("ok"):
                    return {"ok": True, "bot": data.get("result", {})}
            raise HTTPException(status_code=400, detail=f"Telegram API error: {r.text}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Telegram API timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/telegram/sync-users")
async def sync_users_to_telegram_bot(_=Depends(_require_auth)):
    """Sync Spider users to Telegram bot (link telegram_id)."""
    USERS, USERS_LOCK, _ = _get_users()
    synced = 0
    async with USERS_LOCK:
        for uid, u in USERS.items():
            if u.get("telegram_id"):
                synced += 1
    return {"ok": True, "synced": synced}


def _mask_settings(tb: dict) -> dict:
    """Return settings with masked token for API response."""
    result = dict(tb)
    if result.get("bot_token"):
        result["bot_token"] = _mask_token(result["bot_token"])
    return result
