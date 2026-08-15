"""
PairMind — Telegram Bot + HTTP API server for Mini App
pip3 install python-telegram-bot openai aiosqlite python-dotenv nest_asyncio aiohttp
"""

import os, json, asyncio, aiosqlite, base64, time, nest_asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import OpenAI
from aiohttp import web
import aiohttp

nest_asyncio.apply()
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBAPP_URL     = os.getenv("WEBAPP_URL")
DB_PATH        = "pairmind.db"
HTTP_PORT      = 8080

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """Ты — AI-психолог PairMind для пар. Говоришь на русском. Коротко, тепло, без клише.
Максимум 120 слов. Без markdown и звёздочек. Без списков.
В первом ответе задай уточняющий вопрос — не давай совет сразу.
Если человек расстроен — сначала признай чувство."""

# ── DB ───────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            personality_type TEXT, personality_name TEXT,
            trigger TEXT, conflict_style TEXT, insight TEXT,
            session_count INTEGER DEFAULT 0,
            partner_user_id INTEGER, partner_code TEXT,
            completed_at INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, role TEXT, content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def save_user(user_id, **kw):
    u = await get_user(user_id)
    if u:
        sets = ", ".join(f"{k}=?" for k in kw)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE users SET {sets} WHERE user_id=?", [*kw.values(), user_id])
            await db.commit()
    else:
        kw["user_id"] = user_id
        cols = ", ".join(kw.keys())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"INSERT INTO users ({cols}) VALUES ({','.join('?'*len(kw))})", list(kw.values()))
            await db.commit()

async def get_history(user_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role,content FROM messages WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
            (user_id, limit)) as c:
            rows = await c.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

async def save_msg(user_id, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (user_id,role,content) VALUES (?,?,?)", (user_id, role, content))
        await db.commit()

# ── AI ───────────────────────────────────────────────────
async def ask_openai(user_id, message, specialist, profile_type, profile_trigger, history):
    system = f"""{SYSTEM_PROMPT}
Тип пользователя: {profile_type}. Триггер: {profile_trigger}.
Специалист: {specialist}."""

    msgs = [{"role": "system", "content": system}]
    msgs += history
    msgs.append({"role": "user", "content": message})

    r = await asyncio.to_thread(
        openai_client.chat.completions.create,
        model="gpt-4o-mini", messages=msgs, max_tokens=250, temperature=0.75
    )
    return r.choices[0].message.content

# ── HTTP API for Mini App ────────────────────────────────
async def handle_chat(request):
    # CORS headers
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
    }

    if request.method == 'OPTIONS':
        return web.Response(headers=headers)

    try:
        data = await request.json()
        user_id   = data.get('user_id', 0)
        message   = data.get('message', '')
        specialist = data.get('specialist', 'Психолог')
        profile_type    = data.get('profile_type', 'неизвестен')
        profile_trigger = data.get('profile_trigger', 'неизвестен')
        history   = data.get('history', [])

        if not message:
            return web.json_response({'error': 'no message'}, headers=headers, status=400)

        # Save user if new
        if user_id:
            await save_user(user_id, username='miniapp_user')
            await save_msg(user_id, 'user', message)

        reply = await ask_openai(user_id, message, specialist, profile_type, profile_trigger, history)

        if user_id:
            await save_msg(user_id, 'assistant', reply)
            u = await get_user(user_id)
            await save_user(user_id, session_count=(u.get('session_count') or 0) + 1)

        return web.json_response({'reply': reply}, headers=headers)

    except Exception as e:
        print(f"HTTP error: {e}")
        return web.json_response({'error': str(e)}, headers=headers, status=500)

async def handle_options(request):
    return web.Response(headers={
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
    })

# ── Telegram handlers ────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await save_user(user_id, username=update.effective_user.username or "")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌿 Открыть PairMind", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(
        "Привет 👋\n\nPairMind — AI-психолог для пар.\nНажми кнопку чтобы начать:",
        reply_markup=kb
    )

async def handle_webapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except:
        return
    await save_user(user_id,
        personality_type=data.get("type",""),
        personality_name=data.get("name",""),
        trigger=data.get("trigger",""),
        conflict_style=data.get("conflict",""),
        insight=data.get("insight",""),
        session_count=1,
        completed_at=int(time.time()*1000)
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💌 Пригласить партнёра", callback_data="invite")],
        [InlineKeyboardButton("💬 Продолжить", callback_data="chat")]
    ])
    await update.message.reply_text(
        f"Твой тип — {data.get('type')} 🌿\nМожем поговорить прямо здесь или в приложении.",
        reply_markup=kb
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    u = await get_user(user_id)
    if not u or not u.get("personality_type"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌿 Пройти тест", web_app=WebAppInfo(url=WEBAPP_URL))]])
        await update.message.reply_text("Сначала пройди тест 🌿", reply_markup=kb)
        return
    await ctx.bot.send_chat_action(chat_id=user_id, action="typing")
    history = await get_history(user_id)
    await save_msg(user_id, "user", text)
    reply = await ask_openai(user_id, text, "Психолог", u.get("personality_type","?"), u.get("trigger","?"), history)
    await save_msg(user_id, "assistant", reply)
    await update.message.reply_text(reply)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    if q.data == "invite":
        await q.message.reply_text(f"Отправь партнёру:\nhttps://t.me/{ctx.bot.username}?start=partner_{user_id} 💌")
    elif q.data == "chat":
        await q.message.reply_text("Напиши что тебя беспокоит — разберём вместе.")

# ── Main ─────────────────────────────────────────────────
async def main():
    await init_db()

    # Start HTTP server
    app_web = web.Application()
    app_web.router.add_post('/chat', handle_chat)
    app_web.router.add_route('OPTIONS', '/chat', handle_options)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    print(f"🌐 HTTP сервер запущен на порту {HTTP_PORT}")

    # Start Telegram bot
    app_tg = Application.builder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app_tg.add_handler(CallbackQueryHandler(handle_callback))
    print("🌿 PairMind бот запущен")
    await app_tg.run_polling()

if __name__ == "__main__":
    asyncio.run(main())