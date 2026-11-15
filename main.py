import os, json, threading, time, requests, traceback, asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, MessageHandler, filters
)
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- Settings ----------
DELAY_MINUTES = 2

REGIONS = {
    "bereal-us-central":  "Americas",
    "bereal-europe-west": "Europe",
    "bereal-asia-east":   "East Asia",
    "bereal-asia-west":   "West Asia",
}


RELATIONS = ["close friend", "friend", "acquaintance", "family", "romantic partner", "stranger"]

BYSTANDER_BASE = os.environ.get("BYSTANDER_SURVEY_URL", "")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "researcher@example.com")

# ---------- Devin BeReal Time History API ----------
DEVIN_API_KEY = os.environ.get("DEVIN_API_KEY", "")
DEVIN_LATEST_URL = "https://bereal.devin.rest/v1/moments/latest"

REGION_TO_DEVIN = {
    "Americas":   "us-central",
    "Europe":     "europe-west",
    "East Asia":  "asia-east",
    "West Asia":  "asia-west",
}

def fetch_devin_moment(region_label):
    devin_region = REGION_TO_DEVIN.get(region_label)
    if not devin_region or not DEVIN_API_KEY:
        return None
    try:
        r = requests.get(DEVIN_LATEST_URL, params={"api_key": DEVIN_API_KEY}, timeout=10)
        r.raise_for_status()
        data = r.json()
        moment = data.get("regions", {}).get(devin_region)
        if moment:
            return {"id": moment.get("id"), "ts": moment.get("ts"), "utc": moment.get("utc")}
    except Exception as e:
        print(f"[Devin API] fetch failed for {region_label}: {e}")
    return None

# ---------- Firebase ----------
SECRET_FILE_PATH = "/etc/secrets/serviceAccount.json"
if os.path.exists(SECRET_FILE_PATH):
    cred = credentials.Certificate(SECRET_FILE_PATH)
else:
    firebase_credentials_raw = os.environ.get("FIREBASE_CREDENTIALS")
    if not firebase_credentials_raw:
        raise ValueError(
            "No Firebase credentials found. Expected a Secret File at "
            f"{SECRET_FILE_PATH} or a FIREBASE_CREDENTIALS environment variable."
        )
    cred = credentials.Certificate(json.loads(firebase_credentials_raw))
firebase_admin.initialize_app(cred)
db = firestore.client()

TOKEN = os.environ["BOT_TOKEN"]

# ---------- Conversation states (Daily ESM) ----------
(GATE_NUM, WHO, ACTIVITY, GENERAL_Q) = range(4)
COMPENSATION_THRESHOLD = 10

# ---------- Daily ESM: context activity + general items ----------
CONTEXT_ACTIVITY_OPTIONS = [
    "Chatting or hanging out", "Eating or drinking", "Studying or working",
    "Watching or listening to something", "Playing games", "Exercising or being active",
    "Traveling or commuting", "Errands or chores", "Something else",
]

# All 10 general items: fixed 7-point agree/disagree battery, no branching/conditionals.
GENERAL_ITEMS = [
    {"id": "q1a_disruption", "text": "The BeReal moment strongly disrupted the ongoing interaction for me."},
    {"id": "q1b_naturalness", "text": "The BeReal moment felt like a natural part of the ongoing interaction for me."},
    {"id": "q2_recovery", "text": "After the BeReal moment, it was very hard to resume the previous interaction."},
    {"id": "q3_attention_shift", "text": "Due to the BeReal moment, my attention shifted away from the people around me."},
    {"id": "q4_enjoyment", "text": "The BeReal moment had a very positive effect on the ongoing interaction for me."},
    {"id": "q5_connectedness", "text": "I felt more connected to the people around me due to the BeReal moment."},
    {"id": "q6_inclusion", "text": "The BeReal moment made me feel very included in the ongoing interaction."},
    {"id": "q7_shared_experience", "text": "The BeReal moment made me feel like it was a shared experience for our group."},
    {"id": "q8_discomfort", "text": "The BeReal moment made me feel very uncomfortable."},
    {"id": "q9_group_participation", "text": "The people around me became involved in the BeReal moment."},
]

INSTRUCTIONS_TEXT = f(
    "\U0001F389 You're all set!\n\n"
    "Here's what happens each time: about 2 minutes after your BeReal notification, I'll message "
    "you with the survey and QR code. If you were alone, there's nothing else to do. If you were "
    "with others, you'll answer a short survey (~3\u20135 min) and share the QR code with everyone "
    "who was there \u2014 not just the people in the photo \u2014 so they can fill in their own.\n\n"
    "This runs for about 2\u20133 weeks. Use /new to log extra moments, /stop to pause anytime.\n\n"
    "\U0001F4B0 Compensation (\u20ac12 or 1 study participation hour) unlocks once you've completed "
    "10 entries AND at least 10 bystanders have filled out their survey via your QR code.\n\n"
    "Your answers are anonymous. Questions? {CONTACT_EMAIL}"
)

last_moment = {region: {"ts": None, "utc": None, "id": None} for region in REGIONS.values()}

# ==========================================================
#  REGISTRATION
# ==========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    chat_id = str(update.message.chat_id)
    # If the user arrived via the Qualtrics end-of-survey link
    # (t.me/bereal_study_bot?start=HE0498IN01), Telegram passes that payload
    # here as context.args[0]. A manual /start (no link) just leaves this empty.
    participant_id = context.args[0] if context.args else None

    data = {
        "chat_id": chat_id,
        "name": user.first_name or "",
        "username": user.username or "",
        "notifications_active": True,
    }
    if participant_id:
        data["participant_id"] = participant_id

    db.collection("participants").document(chat_id).set(data, merge=True)
    await send_region_prompt(context.bot, chat_id)


async def send_region_prompt(bot, chat_id: str):
    keyboard = [
        [InlineKeyboardButton("Americas", callback_data="region:Americas"),
         InlineKeyboardButton("Europe", callback_data="region:Europe")],
        [InlineKeyboardButton("East Asia", callback_data="region:East Asia"),
         InlineKeyboardButton("West Asia", callback_data="region:West Asia")],
    ]
    await bot.send_message(chat_id, "Which region are you in?", reply_markup=InlineKeyboardMarkup(keyboard))

async def set_region(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    region = query.data.split(":", 1)[1]
    chat_id = str(query.message.chat_id)
    doc_ref = db.collection("participants").document(chat_id)
    data = doc_ref.get().to_dict() or {}
    doc_ref.set({"region": region}, merge=True)

    if data.get("instructions_sent"):
        await query.edit_message_text(f"Got it \u2014 your region is now {region}. \u2705")
        return

    doc_ref.set({"instructions_sent": True}, merge=True)
    await query.edit_message_text(f"Got it \u2014 your region is {region}. \u2705")
    await context.bot.send_message(chat_id, INSTRUCTIONS_TEXT)

    try:
        with open("bystander_comic.png", "rb") as f:
            await context.bot.send_photo(
                chat_id, photo=f,
                caption="Here's a quick visual to help you understand the whole flow \u2014 take a look!"
            )
    except FileNotFoundError:
        print("[WARN] bystander_comic.png not found, skipping comic send")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    doc = db.collection("participants").document(chat_id).get()
    if not doc.exists:
        await update.message.reply_text("You're not registered. Use /start to register.")
        return
    db.collection("participants").document(chat_id).set({"notifications_active": False}, merge=True)
    await update.message.reply_text(
        "\U0001F515 Notifications paused. Use /start anytime to turn them back on."
    )

# ==========================================================
#  SURVEY helpers
# ==========================================================
async def send_bystander_link(context, chat_id, moment_id=None, moment_ts=None):
    params = f"STUDY_ID={chat_id}"
    if moment_id:
        params += f"&MOMENT_ID={moment_id}"
    if moment_ts:
        params += f"&MOMENT_TS={moment_ts}"
    bystander_url = f"{BYSTANDER_BASE}?{params}"
    # Plain text on purpose (no parse_mode): the URL contains dynamic values
    # (Qualtrics survey ID, MOMENT_ID) that can include underscores, which
    # Telegram's Markdown parser treats as italic markers — an odd number of
    # them breaks the whole message with "Can't parse entities". Telegram
    # still auto-links plain http(s) URLs, so we don't lose functionality.
    await context.bot.send_message(
        chat_id,
        "\U0001F4F1 Bystander Survey\n\n"
        "Please show this QR code (or share the link below) to everyone physically present with you \u2014 "
        "they can fill it in right now while you complete yours:\n\n"
        f"{bystander_url}",
    )
    try:
        import qrcode, io
        qr = qrcode.make(bystander_url)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        await context.bot.send_photo(
            chat_id, photo=buf,
            caption="Each person scans this and fills in their own response. Multiple people can use it at the same time."
        )
    except Exception as e:
        print(f"QR generation failed (install qrcode[pil]): {e}")

def who_keyboard(selected):
    rows = []
    for r in RELATIONS:
        mark = "\u2705 " if r in selected else ""
        rows.append([InlineKeyboardButton(mark + r, callback_data=f"who:{r}")])
    rows.append([InlineKeyboardButton("Done \u25B6", callback_data="who_done")])
    return InlineKeyboardMarkup(rows)

async def ask_who(context, chat_id):
    context.user_data["who"] = []
    await context.bot.send_message(
        chat_id, "Who were you with? (tap all that apply, then Done)",
        reply_markup=who_keyboard([]))

async def ask_rating(context, chat_id, text):
    btns = [InlineKeyboardButton(str(n), callback_data=f"r:{n}") for n in range(1, 8)]
    kb = [btns[:4], btns[4:]]
    await context.bot.send_message(
        chat_id, text + "\n(1 = strongly disagree, 7 = strongly agree)",
        reply_markup=InlineKeyboardMarkup(kb))

async def ask_activity(context, chat_id):
    kb = [[InlineKeyboardButton(opt, callback_data=f"act:{opt}")] for opt in CONTEXT_ACTIVITY_OPTIONS]
    await context.bot.send_message(
        chat_id, "What were you doing when the BeReal went off?",
        reply_markup=InlineKeyboardMarkup(kb))

def save_response(context, chat_id):
    moment_ts = context.user_data.get("moment_ts")
    responses_ref = db.collection("participants").document(str(chat_id)).collection("responses")
    # Mark as duplicate if a response for the same moment already exists
    is_duplicate = False
    if moment_ts is not None:
        existing = responses_ref.where("moment_ts", "==", moment_ts).limit(1).stream()
        is_duplicate = any(True for _ in existing)
    responses_ref.add({
        "answers": context.user_data.get("answers", {}),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "moment_ts": moment_ts,
        "moment_utc": context.user_data.get("moment_utc"),
        "moment_id": context.user_data.get("moment_id"),
        "duplicate": is_duplicate,
    })

# ==========================================================
#  SURVEY flow
# ==========================================================
async def begin(chat_id, context):
    context.user_data["answers"] = {}
    doc = db.collection("participants").document(str(chat_id)).get()
    region = (doc.to_dict() or {}).get("region")
    m = last_moment.get(region, {"ts": None, "utc": None, "id": None})
    context.user_data["moment_ts"] = m["ts"]
    context.user_data["moment_utc"] = m["utc"]
    context.user_data["moment_id"] = m.get("id")
    opts = ["None", "1", "2\u20133", "4\u20136", "7+"]
    kb = [[InlineKeyboardButton(o, callback_data=f"gn:{o}")] for o in opts]
    await context.bot.send_message(
        chat_id, "How many people were physically around you when the BeReal went off?",
        reply_markup=InlineKeyboardMarkup(kb))
    return GATE_NUM

async def survey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await begin(update.message.chat_id, context)

async def survey_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    return await begin(q.message.chat_id, context)

async def gate_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    val = q.data.split(":", 1)[1]
    context.user_data["answers"]["num_people"] = val
    if val == "None":
        context.user_data["answers"]["with_others"] = "no"
        save_response(context, q.message.chat_id)
        await context.bot.send_message(
            q.message.chat_id, "Thanks! Since you were alone, that's all for this moment. \u2705")
        return ConversationHandler.END
    # 1+ people around — send bystander link immediately, then continue user survey
    context.user_data["answers"]["with_others"] = "yes"
    await send_bystander_link(
        context, q.message.chat_id,
        moment_id=context.user_data.get("moment_id"),
        moment_ts=context.user_data.get("moment_ts"),
    )
    await ask_who(context, q.message.chat_id)
    return WHO

async def who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "who_done":
        context.user_data["answers"]["who_with"] = context.user_data.get("who", [])
        await q.edit_message_reply_markup(reply_markup=None)
        await ask_activity(context, q.message.chat_id)
        return ACTIVITY
    rel = q.data.split(":", 1)[1]
    sel = context.user_data.get("who", [])
    if rel in sel:
        sel.remove(rel)
    else:
        sel.append(rel)
    context.user_data["who"] = sel
    await q.edit_message_reply_markup(reply_markup=who_keyboard(sel))
    return WHO

async def activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    context.user_data["answers"]["context_activity"] = q.data.split(":", 1)[1]
    context.user_data["general_idx"] = 0
    await ask_rating(context, q.message.chat_id, GENERAL_ITEMS[0]["text"])
    return GENERAL_Q

async def general_item_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    idx = context.user_data.get("general_idx", 0)
    qid = GENERAL_ITEMS[idx]["id"]
    context.user_data["answers"][qid] = int(q.data.split(":", 1)[1])
    idx += 1
    context.user_data["general_idx"] = idx
    chat_id = q.message.chat_id

    if idx >= len(GENERAL_ITEMS):
        save_response(context, chat_id)

        # Count DISTINCT DAYS with a completed entry, not raw completions —
        # otherwise multiple same-day entries (e.g. a BeLate logged via /new)
        # would inflate the count without actually reflecting 10 different days.
        moment_utc = context.user_data.get("moment_utc")
        if moment_utc:
            day_key = moment_utc[:10]  # "2026-07-26 14:17:00" -> "2026-07-26"
        else:
            day_key = datetime.now(timezone.utc).date().isoformat()

        doc_ref = db.collection("participants").document(str(chat_id))
        doc_ref.update({"completed_days": firestore.ArrayUnion([day_key])})

        await context.bot.send_message(chat_id, "Thank you! Your response has been saved. \u2705")
        await maybe_trigger_compensation(context.bot, str(chat_id))
        return ConversationHandler.END

    await ask_rating(context, chat_id, GENERAL_ITEMS[idx]["text"])
    return GENERAL_Q


# ---------- Compensation + closing-interview flow ----------
# "Successful participation" now requires BOTH 10 distinct days of entries
# AND 10 bystander responses (per advisor decision). This check is triggered
# from two independent places: (1) general_item_answer above, whenever a
# participant completes their Nth distinct day, and (2) the Qualtrics
# bystander webhook (see HealthHandler.do_POST + handle_bystander_completed
# below), whenever a bystander response comes in for them. Compensation only
# actually goes out once both thresholds are met.
#
# Trigger (2) can fire while the user isn't in an active Telegram
# conversation, so this whole flow is deliberately built OUTSIDE the
# ConversationHandler (which can only react to updates for a chat it's
# already mid-conversation with). Instead, progress is tracked via a
# `pending_step` field on the participant's Firestore doc, and handled by
# plain top-level handlers registered directly on `app`.

async def maybe_trigger_compensation(bot, chat_id: str):
    doc_ref = db.collection("participants").document(chat_id)
    data = doc_ref.get().to_dict() or {}
    days_count = len(data.get("completed_days", []))
    bystander_count = data.get("bystander_count", 0)
    already_prompted = data.get("compensation_prompted", False)

    if (days_count >= COMPENSATION_THRESHOLD
            and bystander_count >= COMPENSATION_THRESHOLD
            and not already_prompted):
        doc_ref.set({"compensation_prompted": True, "pending_step": "comp_method"}, merge=True)
        kb = [[InlineKeyboardButton("\U0001F4B6 Money", callback_data="comp:money")],
              [InlineKeyboardButton("\U0001F393 Study participation hours", callback_data="comp:hours")]]
        await bot.send_message(
            chat_id,
            "\U0001F389 You've completed 10 entries and recruited 10 bystanders! "
            "How would you like to receive your compensation?",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return True
    return False


async def compensation_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    chat_id = str(q.message.chat_id)
    method = q.data.split(":", 1)[1]  # "money" or "hours"

    db.collection("participants").document(chat_id).set(
        {"comp_method_choice": method, "pending_step": "comp_details"}, merge=True
    )

    if method == "money":
        await context.bot.send_message(
            chat_id,
            "Please send your name along with either your IBAN or your PayPal email "
            "\u2014 whichever you prefer."
        )
    else:
        await context.bot.send_message(chat_id, "Please send your name along with your Matrikelnummer.")


async def interview_interest_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    chat_id = str(q.message.chat_id)
    interested = q.data.split(":", 1)[1]  # "yes" or "no"

    if interested == "yes":
        db.collection("participants").document(chat_id).set(
            {"interview_interested": True, "pending_step": "interview_email"}, merge=True
        )
        await context.bot.send_message(
            chat_id, "Great! Please send your email so we can reach out to schedule it.")
    else:
        db.collection("participants").document(chat_id).set(
            {"interview_interested": False, "pending_step": firestore.DELETE_FIELD}, merge=True
        )
        await context.bot.send_message(chat_id, "No problem \u2014 thanks again for your participation! \u2705")


async def pending_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles free-text replies for the compensation/interview mini-flow,
    routed purely by the `pending_step` field in Firestore — not tied to any
    ConversationHandler state, so it works regardless of whether the user
    happens to be mid-conversation."""
    chat_id = str(update.message.chat_id)
    doc_ref = db.collection("participants").document(chat_id)
    data = doc_ref.get().to_dict() or {}
    step = data.get("pending_step")

    if step == "comp_details":
        method = data.get("comp_method_choice", "unknown")
        doc_ref.set({
            "compensation_method": method,
            "compensation_details": update.message.text.strip(),
            "compensation_details_submitted_at": datetime.now(timezone.utc).isoformat(),
            "pending_step": "interview_interest",
        }, merge=True)
        await update.message.reply_text("Thanks, got it! \u2705")
        kb = [[InlineKeyboardButton("Yes", callback_data="interview:yes"),
               InlineKeyboardButton("No", callback_data="interview:no")]]
        await context.bot.send_message(
            chat_id,
            "One more thing \u2014 would you be interested in an optional closing interview about your "
            "experience? It's compensated separately, on top of what you're already receiving.",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif step == "interview_email":
        doc_ref.set({
            "interview_email": update.message.text.strip(),
            "pending_step": firestore.DELETE_FIELD,
        }, merge=True)
        await update.message.reply_text("Thanks, got it! We'll be in touch. \u2705")

    # else: no pending step we care about right now — ignore silently.


async def handle_bystander_completed(chat_id: str):
    """Called from the Qualtrics bystander-survey webhook (see
    HealthHandler.do_POST) whenever a bystander response comes in. Runs
    inside the bot's own event loop via asyncio.run_coroutine_threadsafe."""
    try:
        doc_ref = db.collection("participants").document(chat_id)
        doc_ref.update({"bystander_count": firestore.Increment(1)})
        await maybe_trigger_compensation(app.bot, chat_id)
    except Exception as e:
        print(f"[ERROR] handle_bystander_completed failed for {chat_id}: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Survey cancelled.")
    return ConversationHandler.END

# ==========================================================
#  MOMENT LISTENER (background)
# ==========================================================
def send_start_button_to_region(region):
    send_url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    reply_markup = {"inline_keyboard": [[{"text": "\U0001F4CB Start survey", "callback_data": "begin_survey"}]]}
    users = db.collection("participants").where("region", "==", region).stream()
    for doc in users:
        data = doc.to_dict()
        chat_id = data.get("chat_id")
        if not chat_id or data.get("notifications_active") is False:
            continue
        try:
            requests.post(send_url, json={
                "chat_id": chat_id,
                "text": "\U0001F4F8 A BeReal moment just happened! Tap below to tell us about it.",
                "reply_markup": reply_markup,
            })
            print(f"Survey prompt sent to {chat_id} ({region})")
        except Exception as e:
            print(f"Failed to send to {chat_id}: {e}")

def listen_region(topic, region):
    url = f"https://ntfy.sh/{topic}/json"
    print(f"Listening: {region} ({topic})")
    while True:
        try:
            # timeout=(connect, read): ntfy sends a keepalive every ~45s by
            # default, so a 120s read timeout gives plenty of margin for
            # jitter while still catching a genuinely stalled connection.
            # Without this, a silently-dropped connection would make
            # iter_lines() block forever with no exception and no log line
            # — this is what happened on 2026-07-24 (listener just stopped
            # producing output, no error, bot itself stayed responsive).
            with requests.get(url, stream=True, timeout=(10, 120)) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("event") == "message":
                        ts = msg.get("time")
                        # ntfy fallback values
                        last_moment[region]["ts"] = str(ts) if ts else None
                        last_moment[region]["utc"] = (
                            datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None)
                        last_moment[region]["id"] = None
                        # Prefer precise data from Devin's API
                        devin = fetch_devin_moment(region)
                        if devin:
                            last_moment[region]["ts"] = devin["ts"]
                            last_moment[region]["utc"] = devin["utc"]
                            last_moment[region]["id"] = devin["id"]
                            print(f">>> [{region}] moment {devin['id']} at {devin['utc']} (Devin). Waiting {DELAY_MINUTES} min...")
                        else:
                            print(f">>> [{region}] moment at {last_moment[region]['utc']} (ntfy only). Waiting {DELAY_MINUTES} min...")
                        time.sleep(DELAY_MINUTES * 60)
                        print(f">>> [{region}] delay over. Sending...")
                        send_start_button_to_region(region)
        except Exception as e:
            print(f"[{region}] listener error (will retry in 10s): {e}")
            time.sleep(10)

# ==========================================================
#  START
# ==========================================================
# Captured once the bot's event loop starts, so the (synchronous) bystander
# webhook handler in HealthHandler.do_POST can schedule an async send safely
# via asyncio.run_coroutine_threadsafe(...).
BOT_LOOP = None

async def _capture_bot_loop(application):
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()

app = Application.builder().token(TOKEN).post_init(_capture_bot_loop).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("stop", stop))
app.add_handler(CallbackQueryHandler(set_region, pattern="^region:"))

conv = ConversationHandler(
    entry_points=[
        CommandHandler("survey", survey_cmd),
        CommandHandler("new", survey_cmd),
        CallbackQueryHandler(survey_btn, pattern="^begin_survey$"),
    ],
    states={
        GATE_NUM:  [CallbackQueryHandler(gate_num, pattern="^gn:")],
        WHO:       [CallbackQueryHandler(who, pattern="^who")],
        ACTIVITY:  [CallbackQueryHandler(activity, pattern="^act:")],
        GENERAL_Q: [CallbackQueryHandler(general_item_answer, pattern="^r:")],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    allow_reentry=True,
)
app.add_handler(conv)

# Compensation + closing-interview mini-flow — deliberately standalone
# (outside `conv`), since it can be triggered proactively by the bystander
# webhook while the user isn't mid-conversation. Progress is tracked via the
# `pending_step` field in Firestore instead of ConversationHandler state.
app.add_handler(CallbackQueryHandler(compensation_method_chosen, pattern="^comp:"))
app.add_handler(CallbackQueryHandler(interview_interest_chosen, pattern="^interview:"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pending_text_router))

# ---------- Global error handler ----------
# Catches any exception raised inside a handler so the bot keeps running
# instead of silently dropping the update (or crashing the handler loop).
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"[ERROR] Exception while handling an update: {context.error}")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
    # Try to let the user know something went wrong, without exposing details.
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Something went wrong on our side. Please try again in a moment.",
            )
    except Exception as notify_err:
        print(f"[ERROR] Failed to notify user about the error: {notify_err}")

app.add_error_handler(error_handler)

# ---------- Health server (keeps Render's free web service happy) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"BeReal study bot is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length) if length else b""

        # Always ack quickly so Qualtrics doesn't retry/timeout on us.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

        if self.path.startswith("/qualtrics-bystander-webhook"):
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                # NOTE: exact payload shape depends on how the "JSON Event"
                # task is configured in Qualtrics Workflows — this checks a
                # few likely spots for the STUDY_ID embedded data value.
                study_id = (
                    payload.get("STUDY_ID")
                    or payload.get("values", {}).get("STUDY_ID")
                    or payload.get("embeddedData", {}).get("STUDY_ID")
                )
                if study_id and BOT_LOOP:
                    asyncio.run_coroutine_threadsafe(
                        handle_bystander_completed(str(study_id)), BOT_LOOP
                    )
                else:
                    print(f"[WARN] Bystander webhook fired but no STUDY_ID found in payload: {payload}")
            except Exception as e:
                print(f"[ERROR] Bystander webhook handling failed: {e}")

    def log_message(self, *args):
        pass  # silence per-request logging

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server listening on port {port}")
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

for topic, region in REGIONS.items():
    threading.Thread(target=listen_region, args=(topic, region), daemon=True).start()

print("Main bot is running (registration + v4 survey + bystander QR + 4-region listener)... Ctrl+C to stop")

# NOTE: run_polling() manages its own asyncio event loop internally and is
# NOT safe to call more than once in the same process — if it exits (crash
# or otherwise), that event loop is closed for good. An earlier version of
# this file wrapped this call in a retry loop to survive transient network
# errors, but that actually made things worse: the second run_polling() call
# would immediately fail with "RuntimeError: Event loop is closed" every
# time, while the health-check server (a separate thread) kept responding
# fine — so Render/UptimeRobot would report the service as healthy while the
# bot was silently completely dead. Render already restarts a crashed
# container automatically (fresh process, fresh event loop), so we let a
# real crash exit the process and rely on that instead of retrying in-process.
app.run_polling()
