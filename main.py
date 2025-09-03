# main.py
import os
import sqlite3
import threading
import hmac
import hashlib
import json
import datetime
import pathlib
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "X_Reward_Bot").strip()
ADMINS_ENV = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMINS_ENV.split(",") if x.strip().isdigit()] or []
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")  # required for WebApp button to open nicely
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/X_Reward_botChannel")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/xrewardchannel")
PORT = int(os.getenv("PORT", "8080"))

# Persistent data location (Railway: mount persistent volume to /data)
DATA_DIR = os.getenv("DATA_DIR", "/data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, os.getenv("DB_PATH", "bot.db"))
UPLOAD_DIR = os.path.join(DATA_DIR, os.getenv("UPLOAD_DIR", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN not set. Bot will not start.")

# Telegram Bot client (for membership checks). If token missing, this will be None.
tg_bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

# ---------- DB ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def migrate():
    con = get_db(); cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY,
      username TEXT,
      coins INTEGER DEFAULT 0,
      referrer_id INTEGER,
      joined_at TEXT,
      ads_watched INTEGER DEFAULT 0,
      ad_counter INTEGER DEFAULT 0,
      boost_until TEXT,
      last_daily TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      task_id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT,
      description TEXT,
      link TEXT,
      reward INTEGER
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_submissions (
      submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      task_id INTEGER,
      file_path TEXT,
      status TEXT DEFAULT 'pending',
      submitted_at TEXT,
      reviewed_by INTEGER,
      review_reason TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS verifiers (
      verifier_id INTEGER PRIMARY KEY
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
      referrer_id INTEGER,
      referred_id INTEGER,
      PRIMARY KEY (referrer_id, referred_id)
    )""")
    con.commit(); con.close()
    logger.info("DB migrated.")

# ---------- Telegram WebApp init_data verification ----------
def verify_init_data(init_data: str, bot_token: str) -> dict:
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    if 'hash' not in parsed:
        raise ValueError("Missing hash")
    check_hash = parsed.pop('hash')
    data_check_list = [f"{k}={parsed[k]}" for k in sorted(parsed.keys())]
    data_check_string = "\n".join(data_check_list)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if calculated_hash != check_hash:
        raise ValueError("Invalid hash")
    if 'user' in parsed:
        try:
            parsed['user'] = json.loads(parsed['user'])
        except:
            pass
    return parsed

# ---------- Channel membership helper ----------
def extract_channel_username(link: str) -> str:
    return link.rstrip('/').split('/')[-1]

def is_member_of_channel(user_id: int) -> bool:
    if not tg_bot:
        return False
    channel = extract_channel_username(CHANNEL_LINK)
    if not channel:
        return False
    # ensure an @ prefix
    chat_id = channel if channel.startswith('@') else f"@{channel}"
    try:
        member = tg_bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.debug("Channel membership check error: %s", e)
        return False

# ---------- FastAPI app ----------
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Serve index.html
@app.get("/", response_class=HTMLResponse)
async def index():
    if os.path.exists("index.html"):
        return HTMLResponse(open("index.html", "r", encoding="utf-8").read())
    return HTMLResponse("<h3>Upload index.html to project root.</h3>")

# Serve uploaded files
@app.get("/uploads/{fname}")
async def serve_upload(fname: str):
    safe = os.path.join(UPLOAD_DIR, os.path.basename(fname))
    if not os.path.exists(safe):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(safe)

# Check join endpoint
@app.post("/webapp/check_join")
async def webapp_check_join(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id"))
    member = is_member_of_channel(uid)
    return {"ok": True, "member": member, "channel": CHANNEL_LINK}

# Get tasks with pagination
@app.get("/webapp/get_tasks")
async def get_tasks(page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50)):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM tasks")
    total = cur.fetchone()[0]
    offset = (page - 1) * per_page
    cur.execute("SELECT task_id, title, description, link, reward FROM tasks ORDER BY task_id DESC LIMIT ? OFFSET ?", (per_page, offset))
    rows = cur.fetchall(); con.close()
    tasks = [{"task_id": r[0], "title": r[1], "description": r[2], "link": r[3], "reward": r[4]} for r in rows]
    return {"ok": True, "tasks": tasks, "page": page, "per_page": per_page, "total": total}

# User balance endpoint
@app.get("/balance/{user_id}")
async def balance(user_id: int):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT COALESCE(coins,0), COALESCE(ads_watched,0), boost_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); con.close()
    if not row:
        return {"ok": True, "coins": 0, "ads_watched": 0, "boost_active": False}
    coins, ads_watched, boost_until = row
    boost_active = False
    if boost_until:
        try:
            until = datetime.datetime.fromisoformat(boost_until.replace("Z", ""))
            boost_active = until > datetime.datetime.utcnow()
        except:
            boost_active = False
    return {"ok": True, "coins": coins, "ads_watched": ads_watched, "boost_active": boost_active}

# Ad watched endpoint
@app.post("/webapp/ad_watched")
async def ad_watched(req: Request):
    payload = await req.json()
    init_data = payload.get("init_data", "")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id"))
    # require channel membership
    if not is_member_of_channel(uid):
        return JSONResponse({"ok": False, "error": "join_channel", "channel": CHANNEL_LINK})
    username = user.get("username") or user.get("first_name") or f"user{uid}"
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, ads_watched, ad_counter) VALUES (?, ?, 0, 0, 0)", (uid, username))
    coins_awarded = 100
    cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ?, ads_watched = COALESCE(ads_watched,0) + 1 WHERE user_id = ?", (coins_awarded, uid))
    cur.execute("SELECT COALESCE(ad_counter,0) FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    ad_counter = (row[0] or 0) + 1
    boost_activated = None
    if ad_counter >= 3:
        until = (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).isoformat() + "Z"
        cur.execute("UPDATE users SET boost_until = ?, ad_counter = 0 WHERE user_id = ?", (until, uid))
        boost_activated = until
        ads_to_next = 3
    else:
        cur.execute("UPDATE users SET ad_counter = ? WHERE user_id = ?", (ad_counter, uid))
        ads_to_next = 3 - ad_counter
    con.commit()
    cur.execute("SELECT coins, ads_watched FROM users WHERE user_id = ?", (uid,))
    coins, ads_watched = cur.fetchone()
    con.close()
    return {"ok": True, "coins_awarded": coins_awarded, "coins_total": coins, "ads_watched": ads_watched, "ads_to_next_boost": ads_to_next, "boost_until": boost_activated}

# Submit proof (image file)
@app.post("/webapp/submit_proof")
async def submit_proof(init_data: str = Form(...), task_id: int = Form(...), file: UploadFile = File(...)):
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id"))
    if not is_member_of_channel(uid):
        raise HTTPException(status_code=403, detail="join_channel")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    ext = pathlib.Path(file.filename).suffix or ".jpg"
    fname = f"{uid}_{ts}{ext}"
    fpath = os.path.join(UPLOAD_DIR, fname)
    content = await file.read()
    with open(fpath, "wb") as f:
        f.write(content)
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (uid, user.get("username") or user.get("first_name") or f"user{uid}"))
    cur.execute("INSERT INTO task_submissions (user_id, task_id, file_path, submitted_at) VALUES (?, ?, ?, ?)", (uid, task_id, fpath, datetime.datetime.utcnow().isoformat()))
    con.commit(); con.close()
    return {"ok": True, "msg": "Proof submitted (image). Waiting for review."}

# Get pending submissions (admin/verifier)
@app.post("/webapp/submissions")
async def get_submissions(payload: dict):
    init_data = payload.get("init_data", "")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT verifier_id FROM verifiers")
    ver_rows = [r[0] for r in cur.fetchall()]
    if uid not in ADMIN_IDS and uid not in ver_rows:
        con.close(); raise HTTPException(status_code=403, detail="Not authorized")
    cur.execute("SELECT submission_id, user_id, task_id, file_path, status, submitted_at FROM task_submissions ORDER BY submitted_at DESC")
    rows = cur.fetchall(); con.close()
    subs = []
    for r in rows:
        file_url = f"/uploads/{os.path.basename(r['file_path'])}" if r['file_path'] else ""
        subs.append({"submission_id": r["submission_id"], "user_id": r["user_id"], "task_id": r["task_id"], "file_path": file_url, "status": r["status"], "submitted_at": r["submitted_at"]})
    return {"ok": True, "submissions": subs}

# Review submission
@app.post("/webapp/review_submission")
async def review_submission(payload: dict):
    init_data = payload.get("init_data", ""); sub_id = payload.get("submission_id"); action = payload.get("action"); reason = payload.get("reason", "")
    if not init_data or not sub_id or action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="init_data, submission_id and valid action required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT verifier_id FROM verifiers"); ver_rows = [r[0] for r in cur.fetchall()]
    if uid not in ADMIN_IDS and uid not in ver_rows:
        con.close(); raise HTTPException(status_code=403, detail="Not authorized")
    cur.execute("SELECT user_id, task_id, status FROM task_submissions WHERE submission_id = ?", (sub_id,))
    row = cur.fetchone()
    if not row:
        con.close(); raise HTTPException(status_code=404, detail="Not found")
    target_uid, task_id, status_now = row[0], row[1], row[2]
    if status_now != "pending":
        con.close(); return {"ok": False, "error": "already reviewed"}
    if action == "approve":
        cur.execute("SELECT reward FROM tasks WHERE task_id = ?", (task_id,))
        r = cur.fetchone(); reward = r[0] if r else 0
        cur.execute("SELECT boost_until FROM users WHERE user_id = ?", (target_uid,))
        brow = cur.fetchone()
        if_b = False
        if brow and brow[0]:
            try:
                until = datetime.datetime.fromisoformat(brow[0].replace("Z", ""))
                if until > datetime.datetime.utcnow():
                    if_b = True
            except:
                if_b = False
        if if_b:
            reward = reward * 2
        cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ? WHERE user_id = ?", (reward, target_uid))
        cur.execute("UPDATE task_submissions SET status = 'approved', reviewed_by = ?, review_reason = ? WHERE submission_id = ?", (uid, reason, sub_id))
        con.commit(); con.close()
        return {"ok": True, "awarded": reward}
    else:
        cur.execute("UPDATE task_submissions SET status = 'rejected', reviewed_by = ?, review_reason = ? WHERE submission_id = ?", (uid, reason, sub_id))
        con.commit(); con.close()
        return {"ok": True, "msg": "Rejected"}

# Admin: add / delete tasks
@app.post("/webapp/add_task")
async def add_task(payload: dict):
    init_data = payload.get("init_data", ""); title = payload.get("title","").strip(); description = payload.get("description","").strip(); link = payload.get("link","").strip(); reward = int(payload.get("reward",0))
    if not init_data or not title or reward <= 0:
        raise HTTPException(status_code=400, detail="init_data, title and positive reward required")
    try: parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e: raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Not authorized")
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT INTO tasks (title, description, link, reward) VALUES (?, ?, ?, ?)", (title, description, link, reward))
    con.commit(); con.close()
    return {"ok": True}

@app.post("/webapp/delete_task")
async def delete_task(payload: dict):
    init_data = payload.get("init_data", ""); task_id = payload.get("task_id")
    if not init_data or not task_id:
        raise HTTPException(status_code=400, detail="init_data and task_id required")
    try: parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e: raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Not authorized")
    con = get_db(); cur = con.cursor()
    cur.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    con.commit(); con.close()
    return {"ok": True}

# Verifier management
@app.post("/webapp/add_verifier")
async def add_verifier(payload: dict):
    init_data = payload.get("init_data", ""); vid = payload.get("verifier_id")
    if not init_data or not vid:
        raise HTTPException(status_code=400, detail="init_data and verifier_id required")
    try: parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e: raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Not authorized")
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO verifiers (verifier_id) VALUES (?)", (vid,))
    con.commit(); con.close()
    return {"ok": True}

@app.post("/webapp/remove_verifier")
async def remove_verifier(payload: dict):
    init_data = payload.get("init_data", ""); vid = payload.get("verifier_id")
    if not init_data or not vid:
        raise HTTPException(status_code=400, detail="init_data and verifier_id required")
    try: parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e: raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Not authorized")
    con = get_db(); cur = con.cursor()
    cur.execute("DELETE FROM verifiers WHERE verifier_id = ?", (vid,))
    con.commit(); con.close()
    return {"ok": True}

# Leaderboards with pagination
@app.get("/webapp/leaderboards")
async def leaderboards(type: str = Query("coins"), page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50)):
    con = get_db(); cur = con.cursor()
    offset = (page - 1) * per_page
    if type == "coins":
        cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
        cur.execute("SELECT username, coins FROM users ORDER BY coins DESC LIMIT ? OFFSET ?", (per_page, offset))
        rows = cur.fetchall()
        items = [{"username": r[0], "coins": r[1]} for r in rows]
    elif type == "invites":
        # count referrals per referrer
        cur.execute("SELECT COUNT(DISTINCT referrer_id) FROM referrals"); total = 0
        cur.execute("SELECT r.referrer_id, COUNT(r.referred_id) as cnt, u.username FROM referrals r JOIN users u ON u.user_id = r.referrer_id GROUP BY r.referrer_id ORDER BY cnt DESC LIMIT ? OFFSET ?", (per_page, offset))
        rows = cur.fetchall()
        items = [{"user_id": r[0], "username": r[2] or "Unknown", "invites": r[1]} for r in rows]
        cur.execute("SELECT COUNT(DISTINCT referrer_id) FROM referrals"); total = cur.fetchone()[0] or 0
    else:  # ads
        cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
        cur.execute("SELECT username, ads_watched, coins FROM users ORDER BY ads_watched DESC LIMIT ? OFFSET ?", (per_page, offset))
        rows = cur.fetchall()
        items = [{"username": r[0], "ads": r[1], "coins": r[2]} for r in rows]
    con.close()
    return {"ok": True, "items": items, "page": page, "per_page": per_page, "total": total}

# Daily claim endpoint
@app.post("/webapp/daily_claim")
async def daily_claim(payload: dict):
    init_data = payload.get("init_data", "")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try: parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e: raise HTTPException(status_code=401, detail=str(e))
    uid = int(parsed.get("user", {}).get("id"))
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, joined_at) VALUES (?, ?, 0, ?)", (uid, parsed.get("user", {}).get("username") or parsed.get("user", {}).get("first_name") or f"user{uid}", datetime.datetime.utcnow().isoformat()))
    cur.execute("SELECT last_daily FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    last_daily = row[0] if row else None
    now_date = datetime.datetime.utcnow().date().isoformat()
    if last_daily == now_date:
        con.close(); return {"ok": False, "error": "already_claimed"}
    # award daily
    reward = 50
    cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ?, last_daily = ? WHERE user_id = ?", (reward, now_date, uid))
    con.commit(); con.close()
    return {"ok": True, "awarded": reward}

# ---------- Telegram bot ----------
async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    args = context.args
    referrer_id = int(args[0]) if args and args[0].isdigit() else None
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, joined_at) VALUES (?, ?, 100, ?)", (u.id, u.username or u.first_name or f"user{u.id}", datetime.datetime.utcnow().isoformat()))
    if referrer_id and referrer_id != u.id:
        try:
            cur.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, u.id))
            cur.execute("UPDATE users SET coins = coins + 200 WHERE user_id = ?", (referrer_id,))
        except Exception as e:
            logger.warning("referral error: %s", e)
    con.commit(); con.close()

    # check channel membership
    member = is_member_of_channel(u.id)
    if not member:
        kb = [[InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)], [InlineKeyboardButton("✅ Check Join", callback_data="check_join")]]
        await update.message.reply_text(f"Please join our channel first: {CHANNEL_LINK}\nAfter joining click 'Check Join'.", reply_markup=InlineKeyboardMarkup(kb))
        return

    webapp_url = WEBAPP_URL or ""
    kb = [
        [InlineKeyboardButton("📋 Tasks", callback_data="tasks"), InlineKeyboardButton("💰 Coins", callback_data="coins")],
        [InlineKeyboardButton("🏆 Leaderboards", callback_data="leaderboards"), InlineKeyboardButton("📺 Dashboard", web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton("🎁 Daily", callback_data="daily"), InlineKeyboardButton("🚀 Power Mode", callback_data="power_info")],
        [InlineKeyboardButton("🎁 Mystery Box", callback_data="mystery"), InlineKeyboardButton("🤝 Referral", callback_data="refer")],
        [InlineKeyboardButton("🛟 Support", url=SUPPORT_LINK)]
    ]
    text = f"Welcome {u.first_name}! 👋\nOpen Dashboard to access Tasks, Ads, Leaderboards and Admin Panel (if you're admin).\nReferral link: https://t.me/{BOT_USERNAME}?start={u.id}"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data; uid = q.from_user.id
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]])
    if data == "check_join":
        if is_member_of_channel(uid):
            await q.message.edit_text("Thanks for joining! Now use /start to open the dashboard.", reply_markup=back_kb)
        else:
            kb = [[InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)], [InlineKeyboardButton("✅ Check Join", callback_data="check_join")]]
            await q.message.edit_text(f"You haven't joined yet. Join here: {CHANNEL_LINK}", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "back":
        await bot_start(update, context); return
    if data == "coins":
        con = get_db(); cur = con.cursor()
        cur.execute("SELECT COALESCE(coins,0), COALESCE(ads_watched,0), COALESCE(ad_counter,0), boost_until FROM users WHERE user_id = ?", (uid,))
        row = cur.fetchone(); con.close()
        if not row:
            await q.message.edit_text("Please /start first.", reply_markup=back_kb); return
        coins, ads_watched, ad_counter, boost_until = row[0], row[1], row[2], row[3]
        boost_line = ""
        if boost_until:
            try:
                dt = datetime.datetime.fromisoformat(boost_until.replace("Z", ""))
                if dt > datetime.datetime.utcnow():
                    boost_line = f"\n🚀 Power Mode active until (UTC): {dt}"
            except:
                pass
        await q.message.edit_text(f"💰 Your coins: {coins}\n📺 Ads watched: {ads_watched}\nAds towards next Power Mode: {ad_counter}/3{boost_line}", reply_markup=back_kb)
        return
    if data == "leaderboards":
        con = get_db(); cur = con.cursor()
        cur.execute("SELECT username, coins FROM users ORDER BY coins DESC LIMIT 10"); top_coins = cur.fetchall()
        cur.execute("SELECT u.username, COUNT(r.referred_id) as cnt FROM referrals r JOIN users u ON u.user_id = r.referrer_id GROUP BY r.referrer_id ORDER BY cnt DESC LIMIT 10"); top_inv = cur.fetchall()
        cur.execute("SELECT username, ads_watched, coins FROM users ORDER BY ads_watched DESC LIMIT 10"); top_ads = cur.fetchall(); con.close()
        msg = "🏆 *Top Coin Holders:*\n"
        for i,(name,c) in enumerate(top_coins,1): msg += f"{i}. @{name} — {c} coins\n"
        msg += "\n🚀 *Top Inviters:*\n"
        for i,(name,cnt) in enumerate(top_inv,1): msg += f"{i}. @{name} — {cnt} invites\n"
        msg += "\n📺 *Top Ad Watchers:*\n"
        for i,(name,ads,coins2) in enumerate(top_ads,1): msg += f"{i}. @{name} — {ads} ads — {coins2} coins\n"
        await q.message.edit_text(msg, parse_mode="Markdown", reply_markup=back_kb); return
    if data == "daily":
        # use API endpoint to perform daily claim
        await q.message.edit_text("Open the Dashboard and claim Daily from there.", reply_markup=back_kb); return
    if data == "power_info":
        await q.message.edit_text("🚀 Power Mode: Watch 3 Mystery Box ads to unlock Power Mode for 2 hours (2× task rewards). Open the Dashboard to start.", reply_markup=back_kb); return
    if data == "mystery":
        await q.message.edit_text("🎁 Mystery Box: Open the Dashboard inside Telegram and watch an ad to claim +100 coins.", reply_markup=back_kb); return
    if data == "refer":
        await q.message.edit_text(f"🔗 Your referral link:\nhttps://t.me/{BOT_USERNAME}?start={uid}\nEarn 200 coins per referral!", reply_markup=back_kb); return
    if data == "support":
        await q.message.edit_text(f"📞 Support group: {SUPPORT_LINK}", reply_markup=back_kb); return
    await q.message.edit_text("Not implemented.", reply_markup=back_kb)

def run_bot():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing — bot will not start.")
        return
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", bot_start))
    application.add_handler(CallbackQueryHandler(bot_callback))
    logger.info("Bot polling starting...")
    application.run_polling()

# ---------- start ----------
if __name__ == "__main__":
    migrate()
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    logger.info(f"Starting HTTP server on port {PORT} ...")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
