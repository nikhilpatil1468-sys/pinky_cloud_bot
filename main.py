import os, time, json, sqlite3, secrets, asyncio, hmac, hashlib, logging
from aiohttp import web, ClientSession, BasicAuth
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
TG_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
RZP_KEY_ID = os.environ["RAZORPAY_KEY_ID"]
RZP_KEY_SECRET = os.environ["RAZORPAY_KEY_SECRET"]
RZP_WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]
DB_PATH = os.getenv("DB_PATH", "bot.db")
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
PORT = int(os.getenv("PORT", "8080"))

# Duration is seconds; amount is paise (₹1 = 100 paise).
PLANS = {
    "hour":  {"label": "1 Hour",  "seconds": 3600,      "amount": 800},
    "day":   {"label": "1 Day",   "seconds": 86400,     "amount": 2000},
    "week":  {"label": "7 Days",  "seconds": 604800,    "amount": 4000},
    "month": {"label": "1 Month", "seconds": 2592000,   "amount": 9900},
    "year":  {"label": "1 Year",  "seconds": 31536000,  "amount": 29900},
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pinky")
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', joined INTEGER,
          banned INTEGER DEFAULT 0, paid_until INTEGER DEFAULT 0,
          daily_count INTEGER DEFAULT 0, daily_date TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS files(
          code TEXT PRIMARY KEY, owner_id INTEGER, file_id TEXT, file_type TEXT,
          caption TEXT, created INTEGER, downloads INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments(
          payment_link_id TEXT PRIMARY KEY, user_id INTEGER, amount_paise INTEGER,
          plan_key TEXT, status TEXT, created INTEGER, payload TEXT
        );
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (OWNER_ID,))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('free_limit',?)", (str(FREE_LIMIT),))

def is_admin(uid):
    with db() as c:
        return uid == OWNER_ID or c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def ensure_user(m):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,username,joined) VALUES(?,?,?)",
                  (m.from_user.id, m.from_user.username or "", int(time.time())))
        c.execute("UPDATE users SET username=? WHERE user_id=?", (m.from_user.username or "", m.from_user.id))

def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def plans_keyboard():
    kb = InlineKeyboardBuilder()
    for key, plan in PLANS.items():
        kb.button(text=f"{plan['label']} — ₹{plan['amount']/100:.0f}", callback_data=f"buy:{key}")
    kb.adjust(1)
    return kb.as_markup()

async def create_payment_link(uid, plan_key):
    plan = PLANS[plan_key]
    body = {
        "amount": plan["amount"],
        "currency": "INR",
        "accept_partial": False,
        "description": f"Telegram bot {plan['label']} access for user {uid}",
        "reference_id": f"tg{uid}_{plan_key}_{secrets.token_hex(5)}",
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {
            "telegram_user_id": str(uid),
            "plan_key": plan_key,
            "plan_seconds": str(plan["seconds"]),
            "plan_amount_paise": str(plan["amount"])
        },
        "callback_url": f"{PUBLIC_URL}/payment-return",
        "callback_method": "get"
    }
    async with ClientSession(auth=BasicAuth(RZP_KEY_ID, RZP_KEY_SECRET)) as s:
        async with s.post("https://api.razorpay.com/v1/payment_links", json=body) as r:
            data = await r.json()
            if r.status >= 300:
                log.error("Razorpay API error: %s", data)
                raise RuntimeError(data.get("error", {}).get("description", "Razorpay error"))
            return data

@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m)
    parts = (m.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""
    u = get_user(m.from_user.id)
    if u["banned"]:
        return await m.answer("⛔ Your access is disabled.")
    if payload.startswith("file_"):
        code = payload[5:]
        with db() as c:
            f = c.execute("SELECT * FROM files WHERE code=?", (code,)).fetchone()
            if f:
                c.execute("UPDATE files SET downloads=downloads+1 WHERE code=?", (code,))
        if not f:
            return await m.answer("This file link is invalid or the file is unavailable.")
        await deliver(m.chat.id, f)
        return
    await m.answer(
        f"👋 Welcome!\nSend a photo, video, document, audio, GIF or voice message to create a share link.\n\n"
        f"Free users: {FREE_LIMIT} links/day.\nUse /plans to view paid plans.",
        reply_markup=main_keyboard()
    )

def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📁 My files", callback_data="myfiles")
    kb.button(text="💳 Plans", callback_data="plans")
    kb.button(text="👤 My plan", callback_data="myplan")
    kb.adjust(2, 1)
    return kb.as_markup()

async def deliver(chat_id, row):
    methods = {
        "photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document,
        "audio": bot.send_audio, "animation": bot.send_animation, "voice": bot.send_voice
    }
    method = methods.get(row["file_type"])
    if method:
        await method(chat_id, row["file_id"], caption=row["caption"] or None)
    else:
        await bot.send_message(chat_id, "Unsupported file type.")

@dp.message(F.photo | F.video | F.document | F.audio | F.animation | F.voice)
async def save_file(m: Message):
    ensure_user(m)
    u = get_user(m.from_user.id)
    if u["banned"]:
        return await m.answer("⛔ Your access is disabled.")
    today = time.strftime("%Y-%m-%d", time.localtime())
    if u["paid_until"] <= int(time.time()) and not is_admin(m.from_user.id):
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key='free_limit'").fetchone()
            limit = int(row["value"]) if row else FREE_LIMIT
            current = c.execute("SELECT daily_count,daily_date FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
            used = current["daily_count"] if current["daily_date"] == today else 0
            if used >= limit:
                return await m.answer(f"Free limit reached ({limit}/day). Use /plans to upgrade.")
            c.execute("UPDATE users SET daily_count=?,daily_date=? WHERE user_id=?", (used + 1, today, m.from_user.id))
    if m.photo: fid, typ = m.photo[-1].file_id, "photo"
    elif m.video: fid, typ = m.video.file_id, "video"
    elif m.document: fid, typ = m.document.file_id, "document"
    elif m.audio: fid, typ = m.audio.file_id, "audio"
    elif m.animation: fid, typ = m.animation.file_id, "animation"
    else: fid, typ = m.voice.file_id, "voice"
    code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    with db() as c:
        c.execute("INSERT INTO files(code,owner_id,file_id,file_type,caption,created) VALUES(?,?,?,?,?,?)",
                  (code, m.from_user.id, fid, typ, m.caption or "", int(time.time())))
    me = await bot.get_me()
    await m.answer(f"✅ Your share link:\nhttps://t.me/{me.username}?start=file_{code}\n\nLink works while the bot database and Telegram file remain available.")

@dp.callback_query(F.data == "plans")
async def show_plans(c: CallbackQuery):
    text = "💳 Select a plan:\n\n" + "\n".join(f"• {p['label']}: ₹{p['amount']/100:.0f}" for p in PLANS.values())
    await c.message.answer(text, reply_markup=plans_keyboard())
    await c.answer()

@dp.message(Command("plans"))
@dp.message(Command("buy"))
async def plans_cmd(m: Message):
    ensure_user(m)
    text = "💳 Select a plan:\n\n" + "\n".join(f"• {p['label']}: ₹{p['amount']/100:.0f}" for p in PLANS.values())
    await m.answer(text, reply_markup=plans_keyboard())

@dp.callback_query(F.data.startswith("buy:"))
async def buy_plan(c: CallbackQuery):
    plan_key = c.data.split(":", 1)[1]
    if plan_key not in PLANS:
        return await c.answer("Unknown plan.", show_alert=True)
    plan = PLANS[plan_key]
    try:
        link = await create_payment_link(c.from_user.id, plan_key)
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO payments(payment_link_id,user_id,amount_paise,plan_key,status,created,payload) VALUES(?,?,?,?,?,?,?)",
                (link["id"], c.from_user.id, plan["amount"], plan_key, "created", int(time.time()), json.dumps(link))
            )
        kb = InlineKeyboardBuilder()
        kb.button(text=f"Pay ₹{plan['amount']/100:.0f}", url=link["short_url"])
        await c.message.answer(
            f"Plan: {plan['label']}\nPrice: ₹{plan['amount']/100:.0f}\n\n"
            "After successful payment, Razorpay sends a signed webhook and your plan activates automatically.",
            reply_markup=kb.as_markup()
        )
    except Exception:
        log.exception("Payment link creation failed")
        await c.message.answer("Couldn't create the payment link. Check Razorpay credentials and try again.")
    await c.answer()

@dp.message(Command("myplan"))
@dp.callback_query(F.data == "myplan")
async def myplan(event):
    uid = event.from_user.id
    if isinstance(event, Message):
        ensure_user(event)
    u = get_user(uid)
    paid = int(u["paid_until"]) > int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime())
    used = u["daily_count"] if u["daily_date"] == today else 0
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='free_limit'").fetchone()
    limit = int(r["value"]) if r else FREE_LIMIT
    msg = f"Plan: {'Paid' if paid else 'Free'}\nFree links used today: {used}/{limit}"
    if paid:
        msg += "\nAccess expires: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u["paid_until"]))
    if isinstance(event, Message):
        await event.answer(msg)
    else:
        await event.message.answer(msg)
        await event.answer()

@dp.message(Command("myfiles"))
async def myfiles(m: Message):
    with db() as c:
        rows = c.execute("SELECT * FROM files WHERE owner_id=? ORDER BY created DESC LIMIT 20", (m.from_user.id,)).fetchall()
    if not rows:
        return await m.answer("You haven't created any file links yet.")
    me = await bot.get_me()
    lines = [f"• {r['file_type']} — https://t.me/{me.username}?start=file_{r['code']} (opens: {r['downloads']})" for r in rows]
    await m.answer("\n".join(lines), disable_web_page_preview=True)

@dp.callback_query(F.data == "myfiles")
async def myfiles_cb(c: CallbackQuery):
    # Build the list directly; callback message is not the user's message.
    with db() as conn:
        rows = conn.execute("SELECT * FROM files WHERE owner_id=? ORDER BY created DESC LIMIT 20", (c.from_user.id,)).fetchall()
    if not rows:
        await c.message.answer("You haven't created any file links yet.")
    else:
        me = await bot.get_me()
        await c.message.answer("\n".join(f"• {r['file_type']} — https://t.me/{me.username}?start=file_{r['code']} (opens: {r['downloads']})" for r in rows), disable_web_page_preview=True)
    await c.answer()

@dp.message(Command("users"))
async def users_cmd(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("Admins only.")
    with db() as c: n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    await m.answer(f"Registered users: {n}")

@dp.message(Command("set_free_limit"))
async def set_limit(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("Admins only.")
    try: limit = int(m.text.split()[1]); assert limit >= 0
    except Exception: return await m.answer("Usage: /set_free_limit NUMBER")
    with db() as c: c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('free_limit',?)", (str(limit),))
    await m.answer(f"Free daily limit set to {limit}.")

@dp.message(Command("addpaid"))
async def add_paid(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("Admins only.")
    try: _, uid, days = m.text.split(); uid, days = int(uid), int(days)
    except Exception: return await m.answer("Usage: /addpaid USER_ID DAYS")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,joined) VALUES(?,?)", (uid, int(time.time())))
        row = c.execute("SELECT paid_until FROM users WHERE user_id=?", (uid,)).fetchone()
        c.execute("UPDATE users SET paid_until=? WHERE user_id=?", (max(row["paid_until"], int(time.time())) + days*86400, uid))
    await m.answer(f"Granted {days} days to {uid}.")

@dp.message(Command("ban"))
async def ban(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("Admins only.")
    try: uid = int(m.text.split()[1])
    except Exception: return await m.answer("Usage: /ban USER_ID")
    with db() as c: c.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await m.answer("User banned.")

@dp.message(Command("unban"))
async def unban(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("Admins only.")
    try: uid = int(m.text.split()[1])
    except Exception: return await m.answer("Usage: /unban USER_ID")
    with db() as c: c.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await m.answer("User unbanned.")

@dp.message(Command("broadcast"))
async def broadcast(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("Admins only.")
    message = m.text.partition(" ")[2].strip()
    if not message: return await m.answer("Usage: /broadcast MESSAGE")
    with db() as c: users = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    sent = failed = 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], message)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await m.answer(f"Broadcast complete. Sent: {sent}; failed: {failed}.")

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("User commands: /start, /plans, /buy, /myplan, /myfiles, /help\nAdmin: /users, /set_free_limit N, /addpaid USER_ID DAYS, /ban USER_ID, /unban USER_ID, /broadcast MESSAGE")

async def telegram_webhook(request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, TG_WEBHOOK_SECRET):
        raise web.HTTPUnauthorized()
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Telegram update error")
        raise web.HTTPBadRequest(text="bad update")
    return web.Response(text="ok")

async def razorpay_webhook(request):
    raw = await request.read()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RZP_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise web.HTTPUnauthorized(text="invalid signature")
    try:
        event = json.loads(raw.decode())
    except Exception:
        raise web.HTTPBadRequest(text="invalid JSON")
    if event.get("event") != "payment_link.paid":
        return web.Response(text="ignored")
    entity = event.get("payload", {}).get("payment_link", {}).get("entity", {})
    link_id = entity.get("id")
    notes = entity.get("notes") or {}
    try:
        uid = int(notes["telegram_user_id"])
        plan_key = notes["plan_key"]
        plan = PLANS[plan_key]
        seconds = int(notes["plan_seconds"])
        expected_amount = int(notes["plan_amount_paise"])
    except (KeyError, ValueError, TypeError):
        log.error("Missing or invalid plan notes in payment webhook")
        return web.Response(text="missing plan metadata", status=400)
    amount = int(entity.get("amount", 0))
    if entity.get("status") != "paid" or amount != expected_amount or amount != plan["amount"] or seconds != plan["seconds"]:
        log.warning("Payment validation failed: status=%s amount=%s plan=%s", entity.get("status"), amount, plan_key)
        return web.Response(text="payment validation failed", status=400)
    with db() as c:
        old = c.execute("SELECT status FROM payments WHERE payment_link_id=?", (link_id,)).fetchone()
        if old and old["status"] == "paid":
            return web.Response(text="already processed")
        c.execute("INSERT OR IGNORE INTO users(user_id,joined) VALUES(?,?)", (uid, int(time.time())))
        row = c.execute("SELECT paid_until FROM users WHERE user_id=?", (uid,)).fetchone()
        until = max(int(row["paid_until"]), int(time.time())) + seconds
        c.execute("UPDATE users SET paid_until=? WHERE user_id=?", (until, uid))
        c.execute("""INSERT INTO payments(payment_link_id,user_id,amount_paise,plan_key,status,created,payload)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(payment_link_id) DO UPDATE SET status='paid',payload=excluded.payload""",
          (link_id, uid, amount, plan_key, "paid", int(time.time()), json.dumps(event)))
    try:
        await bot.send_message(uid, f"✅ Payment verified! Your {plan['label']} plan is active.")
        await bot.send_message(OWNER_ID, f"💰 Payment received: user {uid}, {plan['label']}, ₹{amount/100:.2f}.")
    except Exception:
        log.exception("Unable to send payment confirmation")
    return web.Response(text="ok")

async def payment_return(request):
    return web.Response(text="Payment return received. Your plan activates after the verified Razorpay webhook.")

async def health(request):
    return web.json_response({"status": "ok"})

async def startup(app):
    init_db()
    await bot.set_webhook(
        f"{PUBLIC_URL}/telegram/{TG_WEBHOOK_SECRET}",
        secret_token=TG_WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types()
    )

async def cleanup(app):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()

def create_app():
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_get("/", health)
    app.router.add_get("/payment-return", payment_return)
    app.router.add_post(f"/telegram/{TG_WEBHOOK_SECRET}", telegram_webhook)
    app.router.add_post("/razorpay-webhook", razorpay_webhook)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
