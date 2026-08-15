"""
PairMind — Telegram Bot + HTTP API
pip3 install python-telegram-bot openai aiosqlite python-dotenv nest_asyncio aiohttp

Модель AI:
- По умолчанию: OpenAI gpt-4o-mini (твой текущий ключ)
- Если добавить ANTHROPIC_API_KEY в .env — переключится на Claude Fable 5
"""

import os, json, asyncio, aiosqlite, time, nest_asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, LabeledPrice
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          PreCheckoutQueryHandler, filters, ContextTypes)
from openai import OpenAI
from aiohttp import web
import aiohttp as aiohttp_client

nest_asyncio.apply()
load_dotenv()

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # опционально — включает Claude Fable 5
WEBAPP_URL        = os.getenv("WEBAPP_URL")
DB_PATH    = "pairmind.db"
HTTP_PORT  = int(os.getenv('PORT', 8080))  # Railway задаёт PORT сам
PRICE_STARS = 150      # цена за ОБЕ карточки (своя + партнёра)
CHAT_FREE_DAILY = 8    # бесплатных сообщений AI-психологу в день
CHAT_MSG_PRICE  = 10   # цена одного сообщения сверх лимита, ⭐
UNLOCK_DAY  = 3        # карточка открывается на 3-й день
CARD_TTL    = 3*24*3600*1000  # карточка обновляется каждые 3 дня (мс)

USE_ANTHROPIC = bool(ANTHROPIC_API_KEY)
AI_MODEL = "claude-fable-5" if USE_ANTHROPIC else "gpt-4o-mini"
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

BASE_PROMPT = """Ты — AI-психолог PairMind для пар. Говоришь на русском. Тепло, коротко, без клише.
Максимум 120 слов. Без markdown, звёздочек и списков.
В первом ответе на новую тему задай уточняющий вопрос — не давай совет сразу.
Если человек расстроен — сначала признай чувство."""

# ── DB ───────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            ptype TEXT, pname TEXT, trigger TEXT, conflict TEXT, insight TEXT,
            completed_at INTEGER,
            partner_code TEXT, partner_user_id INTEGER,
            card_paid INTEGER DEFAULT 0, card_paid_at INTEGER,
            partner_card TEXT, partner_card_at INTEGER,
            my_card TEXT, my_card_at INTEGER,
            chat_msgs_today INTEGER DEFAULT 0, chat_msgs_date TEXT,
            chat_paid_extra INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, role TEXT, content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_uid INTEGER, to_uid INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER)""")
        # миграция: добавляем новые колонки, если таблица users создана раньше этого обновления
        async with db.execute("PRAGMA table_info(users)") as c:
            existing_cols = {row[1] for row in await c.fetchall()}
        for col, decl in [("chat_msgs_today","INTEGER DEFAULT 0"),
                           ("chat_msgs_date","TEXT"),
                           ("chat_paid_extra","INTEGER DEFAULT 0")]:
            if col not in existing_cols:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def save_user(uid, **kw):
    u = await get_user(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        if u:
            sets = ", ".join(f"{k}=?" for k in kw)
            await db.execute(f"UPDATE users SET {sets} WHERE user_id=?", [*kw.values(), uid])
        else:
            kw["user_id"] = uid
            await db.execute(f"INSERT INTO users ({','.join(kw)}) VALUES ({','.join('?'*len(kw))})", list(kw.values()))
        await db.commit()

async def save_msg(uid, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (user_id,role,content) VALUES (?,?,?)", (uid, role, content))
        await db.commit()

async def get_history(uid, limit=12):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role,content FROM messages WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
                              (uid, limit)) as c:
            rows = await c.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

async def get_user_texts(uid, limit=30):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT content FROM messages WHERE user_id=? AND role='user' ORDER BY rowid DESC LIMIT ?",
                              (uid, limit)) as c:
            return [r[0] for r in await c.fetchall()]

async def find_user_by_username(username):
    """username без @, регистронезависимо"""
    username = username.lstrip('@').strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE LOWER(username)=?", (username,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def create_invite(from_uid, to_uid):
    async with aiosqlite.connect(DB_PATH) as db:
        # если уже есть висящее приглашение между этими же двумя людьми — не плодим дубликаты
        async with db.execute(
            "SELECT id FROM invites WHERE from_uid=? AND to_uid=? AND status='pending'",
            (from_uid, to_uid)) as c:
            existing = await c.fetchone()
        if existing:
            return existing[0]
        cur = await db.execute(
            "INSERT INTO invites (from_uid,to_uid,status,created_at) VALUES (?,?,?,?)",
            (from_uid, to_uid, 'pending', int(time.time()*1000)))
        await db.commit()
        return cur.lastrowid

async def get_invite(invite_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM invites WHERE id=?", (invite_id,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def set_invite_status(invite_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE invites SET status=? WHERE id=?", (status, invite_id))
        await db.commit()

async def get_pending_invite_from(from_uid):
    """Есть ли у меня отправленное и всё ещё висящее приглашение (для статуса на фронте)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM invites WHERE from_uid=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (from_uid,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

def today_str():
    return time.strftime("%Y-%m-%d", time.gmtime())

async def check_and_consume_chat_msg(uid):
    """Возвращает (allowed: bool, used_free: int, remaining_free: int, paid_extra: int).
    Если сообщений в дневном лимите ещё нет — списывает бесплатное.
    Если лимит исчерпан, но есть купленные сверху — списывает одно оплаченное.
    Если нет ни того ни другого — allowed=False, отвечать нельзя, показываем оплату."""
    u = await get_user(uid) or {}
    date = u.get("chat_msgs_date")
    used = u.get("chat_msgs_today") or 0
    if date != today_str():
        used = 0  # новый день — счётчик сбрасывается
    paid_extra = u.get("chat_paid_extra") or 0

    if used < CHAT_FREE_DAILY:
        await save_user(uid, chat_msgs_today=used+1, chat_msgs_date=today_str())
        return True, used+1, CHAT_FREE_DAILY-(used+1), paid_extra
    if paid_extra > 0:
        await save_user(uid, chat_msgs_today=used, chat_msgs_date=today_str(), chat_paid_extra=paid_extra-1)
        return True, used, 0, paid_extra-1
    await save_user(uid, chat_msgs_today=used, chat_msgs_date=today_str())
    return False, used, 0, paid_extra


# ── AI ───────────────────────────────────────────────────
async def ask_ai(system, messages, max_tokens=280):
    if USE_ANTHROPIC:
        async with aiohttp_client.ClientSession() as s:
            async with s.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": max_tokens,
                      "system": system, "messages": messages}) as r:
                data = await r.json()
                if r.status != 200:
                    raise Exception(str(data))
                return data["content"][0]["text"]
    else:
        msgs = [{"role":"system","content":system}] + messages
        r = await asyncio.to_thread(openai_client.chat.completions.create,
            model=AI_MODEL, messages=msgs, max_tokens=max_tokens, temperature=0.75)
        return r.choices[0].message.content

async def chat_reply(uid, message, mode="chat"):
    u = await get_user(uid)
    history = await get_history(uid)
    system = f"{BASE_PROMPT}\nТип пользователя: {u.get('ptype','?') if u else '?'} — {u.get('pname','') if u else ''}. Триггер: {u.get('trigger','?') if u else '?'}."

    if mode == "partner_card" and u and u.get("partner_user_id"):
        p = await get_user(u["partner_user_id"])
        if p:
            system += f"""
Сейчас пользователь смотрит карточку своего партнёра и задаёт вопросы о нём.
Партнёр: тип {p.get('ptype','?')} — {p.get('pname','')}. Триггер: {p.get('trigger','?')}. В конфликте: {p.get('conflict','?')}.
Объясняй что важно партнёру, почему он так реагирует, и предлагай конкретные решения."""

    await save_msg(uid, "user", message)
    reply = await ask_ai(system, history + [{"role":"user","content":message}])
    await save_msg(uid, "assistant", reply)
    return reply

async def generate_traits(ptype, pname, scores, answers):
    """AI генерирует триггер/конфликт/инсайт под конкретные баллы, чтобы результаты не повторялись у людей одного типа"""
    system = ("Ты — тонкий психолог по теории привязанности, пишешь на русском для мобильного приложения. "
               "Без markdown, без звёздочек, без списков. Живой, тёплый, конкретный тон — не общие фразы.")
    prompt = f"""Психотип человека по тесту: {ptype} ({pname}).
Сырые баллы по шкалам (чем выше — тем сильнее выражено): избегание={scores['AV']}, тревожность={scores['AN']}, интроверсия={scores['I']}, эмоциональность={scores['F']}.
Ответы на 8 вопросов теста (индексы вариантов 0-3): {answers}

Сгенерируй три поля JSON, каждое максимально конкретное именно под ЭТИ баллы (не общие фразы про тип в целом — два человека одного типа с разными баллами должны получить заметно разный текст):
- trigger: 3-6 слов, что именно выбивает из колеи (используй конкретную деталь, не шаблон)
- conflict: 4-8 слов, поведенческий паттерн в момент конфликта
- insight: 1-2 предложения, неочевидное наблюдение о человеке, которое цепляет и звучит правдиво лично для него

Ответь СТРОГО валидным JSON без markdown-обёртки: {{"trigger":"...","conflict":"...","insight":"..."}}"""
    raw = await ask_ai(system, [{"role":"user","content":prompt}], max_tokens=300)
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except Exception:
        return None

async def generate_card(target_uid, viewer_is_partner):
    """Карточка: профиль + инсайты из переписки"""
    t = await get_user(target_uid)
    if not t or not t.get("ptype"):
        return None
    texts = await get_user_texts(target_uid)
    convo = "\n".join(f"— {x}" for x in texts[:25]) or "— (ещё не общался с психологом)"
    who = "о партнёре пользователя" if viewer_is_partner else "о самом пользователе"
    system = "Ты пишешь короткую психологическую карточку на русском. Без markdown и звёздочек."
    prompt = f"""Составь карточку {who}.
Тип: {t.get('ptype')} — {t.get('pname')}. Триггер: {t.get('trigger')}. В конфликте: {t.get('conflict')}.
Фразы из его разговоров с психологом:
{convo}

Структура (каждый раздел с новой строки, заголовок заглавными):
ЧТО ЕГО БЕСПОКОИТ — 2-3 предложения на основе разговоров
В ЧЁМ НУЖДАЕТСЯ — 2-3 предложения
ТРИГГЕРЫ — что ранит, чего избегать
КАК ПОДДЕРЖАТЬ — 3 конкретных совета
Максимум 200 слов всего."""
    return await ask_ai(system, [{"role":"user","content":prompt}], max_tokens=450)

# ── HTTP API ─────────────────────────────────────────────
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

@web.middleware
async def cors_mw(request, handler):
    if request.method == 'OPTIONS':
        return cors(web.Response())
    try:
        resp = await handler(request)
    except Exception as e:
        print("HTTP error:", e)
        resp = web.json_response({'error': str(e)}, status=500)
    return cors(resp)

async def ep_profile(request):
    uid = int(request.query.get('user_id', 0))
    u = await get_user(uid)
    now = int(time.time()*1000)
    profile = None; partner = {"linked": False, "has_profile": False, "pending_invite": None}; card = {}
    if u:
        if u.get("ptype"):
            profile = {k: u.get(k) for k in ("ptype","pname","trigger","conflict","insight","completed_at")}
        if u.get("partner_user_id"):
            p = await get_user(u["partner_user_id"])
            partner = {"linked": True, "has_profile": bool(p and p.get("ptype")),
                       "ptype": p.get("ptype") if p else None, "pname": p.get("pname") if p else None,
                       "pending_invite": None}
        else:
            inv = await get_pending_invite_from(uid)
            if inv:
                to_user = await get_user(inv["to_uid"])
                partner["pending_invite"] = {"username": (to_user or {}).get("username","")}
        days = (now - (u.get("completed_at") or now)) / 86400000
        card = {"day_unlocked": days >= (UNLOCK_DAY-1), "days_left": max(0, round(UNLOCK_DAY-1-days, 1)),
                "paid": bool(u.get("card_paid")), "price": PRICE_STARS}
    return web.json_response({"profile": profile, "partner": partner, "card": card})

async def ep_save_profile(request):
    d = await request.json()
    uid = int(d.get("user_id", 0))
    if not uid: return web.json_response({"error":"no user_id"}, status=400)
    u = await get_user(uid)
    completed = (u or {}).get("completed_at") or int(time.time()*1000)

    ptype = d.get("type","")
    pname = d.get("name","")
    scores = d.get("scores") or {}
    answers = d.get("answers") or []

    trigger, conflict, insight = d.get("trigger",""), d.get("conflict",""), d.get("insight","")
    if scores and answers:
        try:
            traits = await generate_traits(ptype, pname, scores, answers)
            if traits:
                trigger = traits.get("trigger", trigger)
                conflict = traits.get("conflict", conflict)
                insight = traits.get("insight", insight)
        except Exception as e:
            print("generate_traits failed, falling back to defaults:", e)

    await save_user(uid, ptype=ptype, pname=pname,
        trigger=trigger, conflict=conflict,
        insight=insight, completed_at=completed)
    return web.json_response({"ok": True,
                               "trigger": trigger, "conflict": conflict, "insight": insight})

async def ep_send_invite(request):
    d = await request.json()
    uid = int(d.get("user_id",0))
    username = (d.get("username") or "").strip()
    if not uid or not username:
        return web.json_response({"error":"Введи Telegram-username партнёра"}, status=400)
    me = await get_user(uid)
    if me and me.get("partner_user_id"):
        return web.json_response({"error":"Ты уже связан с партнёром"}, status=400)
    target = await find_user_by_username(username)
    if not target:
        return web.json_response({"error":"Этот человек ещё не запускал бота. Попроси его сначала нажать /start у @PairMind"}, status=404)
    if target["user_id"] == uid:
        return web.json_response({"error":"Нельзя пригласить самого себя"}, status=400)
    if target.get("partner_user_id"):
        return web.json_response({"error":"Этот человек уже связан с кем-то"}, status=400)
    invite_id = await create_invite(uid, target["user_id"])
    bot = request.app["bot"]
    me_name = (me or {}).get("username") or "кто-то"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💌 Принять", callback_data=f"inv_accept_{invite_id}"),
        InlineKeyboardButton("Отклонить", callback_data=f"inv_decline_{invite_id}")
    ]])
    try:
        await bot.send_message(
            chat_id=target["user_id"],
            text=f"💌 @{me_name} приглашает тебя в PairMind — связать профили, чтобы видеть карточку друг друга.",
            reply_markup=kb)
    except Exception as e:
        return web.json_response({"error":"Не удалось отправить приглашение. Возможно, этот человек заблокировал бота."}, status=400)
    return web.json_response({"ok": True, "sent_to": username})

async def ep_chat(request):
    d = await request.json()
    uid = int(d.get("user_id",0)); msg = d.get("message","")
    mode = d.get("mode","chat")
    if not msg: return web.json_response({"error":"no message"}, status=400)
    if not uid: return web.json_response({"error":"открой через Telegram"}, status=400)
    allowed, used, remaining, paid_extra = await check_and_consume_chat_msg(uid)
    if not allowed:
        return web.json_response({"need_payment": True, "price": CHAT_MSG_PRICE,
                                   "reason": "chat_limit"})
    reply = await chat_reply(uid, msg, mode)
    return web.json_response({"reply": reply, "chat_remaining_free": remaining})

async def ep_partner_card(request):
    uid = int(request.query.get("user_id",0))
    u = await get_user(uid)
    now = int(time.time()*1000)
    if not u: return web.json_response({"error":"нет профиля"}, status=400)
    if not u.get("partner_user_id"):
        return web.json_response({"no_partner": True})
    days = (now - (u.get("completed_at") or now)) / 86400000
    if days < (UNLOCK_DAY-1):
        return web.json_response({"locked": True, "days_left": round(UNLOCK_DAY-1-days,1)})
    if not u.get("card_paid"):
        return web.json_response({"need_payment": True, "price": PRICE_STARS})
    p = await get_user(u["partner_user_id"])
    if not p or not p.get("ptype"):
        return web.json_response({"partner_no_profile": True})
    # регенерация каждые 3 дня
    if not u.get("partner_card") or now - (u.get("partner_card_at") or 0) > CARD_TTL:
        card = await generate_card(u["partner_user_id"], True)
        await save_user(uid, partner_card=card, partner_card_at=now)
    else:
        card = u["partner_card"]
    return web.json_response({"card": card, "partner_type": p.get("ptype"), "partner_name": p.get("pname")})

async def ep_my_card(request):
    uid = int(request.query.get("user_id",0))
    u = await get_user(uid)
    now = int(time.time()*1000)
    if not u or not u.get("ptype"):
        return web.json_response({"error":"нет профиля"}, status=400)
    days = (now - (u.get("completed_at") or now)) / 86400000
    if days < (UNLOCK_DAY-1):
        return web.json_response({"locked": True, "days_left": round(UNLOCK_DAY-1-days,1)})
    if not u.get("card_paid"):
        return web.json_response({"need_payment": True, "price": PRICE_STARS})
    if not u.get("my_card") or now - (u.get("my_card_at") or 0) > CARD_TTL:
        card = await generate_card(uid, False)
        await save_user(uid, my_card=card, my_card_at=now)
    else:
        card = u["my_card"]
    return web.json_response({"card": card})

async def ep_invoice(request):
    d = await request.json()
    uid = int(d.get("user_id",0))
    bot = request.app["bot"]
    link = await bot.create_invoice_link(
        title="Две карточки PairMind",
        description="Твоя карточка + карточка партнёра: что беспокоит, в чём нуждаетесь, триггеры и как поддержать друг друга. Обновляются каждые 3 дня.",
        payload=f"cards_{uid}",
        currency="XTR",
        prices=[LabeledPrice("Две карточки", PRICE_STARS)])
    return web.json_response({"invoice_url": link})

async def ep_chat_invoice(request):
    d = await request.json()
    uid = int(d.get("user_id",0))
    bot = request.app["bot"]
    link = await bot.create_invoice_link(
        title="Сообщение AI-психологу",
        description=f"Дневной лимит из {CHAT_FREE_DAILY} бесплатных сообщений исчерпан. Это разблокирует одно дополнительное сообщение.",
        payload=f"chatmsg_{uid}",
        currency="XTR",
        prices=[LabeledPrice("1 сообщение", CHAT_MSG_PRICE)])
    return web.json_response({"invoice_url": link})

# ── Telegram handlers ────────────────────────────────────
async def start(update: Update, ctx):
    uid = update.effective_user.id
    await save_user(uid, username=update.effective_user.username or "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌿 Открыть PairMind", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await update.message.reply_text("Привет 👋\n\nPairMind — AI-психолог для пар.\nНажми кнопку чтобы начать:", reply_markup=kb)

async def on_message(update: Update, ctx):
    uid = update.effective_user.id
    u = await get_user(uid)
    if not u or not u.get("ptype"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌿 Пройти тест", web_app=WebAppInfo(url=WEBAPP_URL))]])
        await update.message.reply_text("Сначала пройди тест 🌿", reply_markup=kb); return
    allowed, used, remaining, paid_extra = await check_and_consume_chat_msg(uid)
    if not allowed:
        link = await ctx.bot.create_invoice_link(
            title="Сообщение AI-психологу",
            description=f"Дневной лимит из {CHAT_FREE_DAILY} бесплатных сообщений исчерпан. Это разблокирует одно дополнительное сообщение.",
            payload=f"chatmsg_{uid}", currency="XTR",
            prices=[LabeledPrice("1 сообщение", CHAT_MSG_PRICE)])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Разблокировать за {CHAT_MSG_PRICE} ⭐", url=link)]])
        await update.message.reply_text(
            f"Бесплатный лимит на сегодня исчерпан ({CHAT_FREE_DAILY} сообщений). Новые появятся завтра, или разблокируй прямо сейчас:",
            reply_markup=kb)
        return
    await ctx.bot.send_chat_action(chat_id=uid, action="typing")
    reply = await chat_reply(uid, update.message.text)
    await update.message.reply_text(reply)

async def on_precheckout(update: Update, ctx):
    await update.pre_checkout_query.answer(ok=True)

async def on_paid(update: Update, ctx):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload or ""
    if payload.startswith("chatmsg_"):
        u = await get_user(uid) or {}
        await save_user(uid, chat_paid_extra=(u.get("chat_paid_extra") or 0)+1)
        await update.message.reply_text("✨ Сообщение разблокировано! Возвращайся в чат.")
    else:
        await save_user(uid, card_paid=1, card_paid_at=int(time.time()*1000))
        await update.message.reply_text("✨ Обе карточки разблокированы! Открой приложение → вкладка Карточка.")

async def on_invite_response(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    action, invite_id = q.data.rsplit('_', 1)  # 'inv_accept' | 'inv_decline', id
    invite_id = int(invite_id)
    invite = await get_invite(invite_id)
    if not invite or invite["status"] != "pending":
        await q.edit_message_text("Это приглашение уже недействительно.")
        return
    if q.from_user.id != invite["to_uid"]:
        return  # кнопку жмёт не тот человек — игнорируем молча
    if action == "inv_decline":
        await set_invite_status(invite_id, "declined")
        await q.edit_message_text("Приглашение отклонено.")
        try:
            await ctx.bot.send_message(invite["from_uid"], "Партнёр отклонил приглашение в PairMind.")
        except Exception:
            pass
        return
    # inv_accept
    from_u = await get_user(invite["from_uid"])
    if from_u and from_u.get("partner_user_id"):
        await q.edit_message_text("У приглашавшего уже есть партнёр — приглашение устарело.")
        await set_invite_status(invite_id, "declined")
        return
    await set_invite_status(invite_id, "accepted")
    await save_user(invite["from_uid"], partner_user_id=invite["to_uid"])
    await save_user(invite["to_uid"], partner_user_id=invite["from_uid"])
    await q.edit_message_text("💛 Вы связаны в PairMind! Открой приложение → вкладка Карточка.")
    try:
        await ctx.bot.send_message(invite["from_uid"], "💛 Партнёр принял приглашение! Вы связаны в PairMind.")
    except Exception:
        pass

# ── Main ─────────────────────────────────────────────────
async def main():
    await init_db()
    app_tg = Application.builder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app_tg.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app_tg.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    app_tg.add_handler(CallbackQueryHandler(on_invite_response, pattern=r'^inv_(accept|decline)_\d+$'))

    app_web = web.Application(middlewares=[cors_mw])
    app_web["bot"] = app_tg.bot
    app_web.router.add_get('/profile', ep_profile)
    app_web.router.add_post('/save_profile', ep_save_profile)
    app_web.router.add_post('/send_invite', ep_send_invite)
    app_web.router.add_post('/chat', ep_chat)
    app_web.router.add_get('/partner_card', ep_partner_card)
    app_web.router.add_get('/my_card', ep_my_card)
    app_web.router.add_post('/create_invoice', ep_invoice)
    app_web.router.add_post('/create_chat_invoice', ep_chat_invoice)
    for route in ['/profile','/save_profile','/send_invite','/chat','/partner_card','/my_card','/create_invoice','/create_chat_invoice']:
        app_web.router.add_route('OPTIONS', route, lambda r: web.Response())
    runner = web.AppRunner(app_web); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', HTTP_PORT).start()
    print(f"🌐 HTTP сервер на порту {HTTP_PORT} | AI: {AI_MODEL}")
    print("🌿 PairMind бот запущен")
    await app_tg.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
