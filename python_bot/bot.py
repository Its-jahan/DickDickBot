import hashlib
import hmac
import logging
import random
import datetime
from datetime import time
from zoneinfo import ZoneInfo
import math
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import Forbidden
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, InlineQueryHandler, CallbackQueryHandler, TypeHandler
from uuid import uuid4

IRAN_TZ = ZoneInfo("Asia/Tehran")

# Dedicated OS-entropy-backed RNG for match-deciding dice rolls, so the outcome can't
# be tied to any in-process PRNG state - each roll is drawn fresh from the OS.
_dice_rng = random.SystemRandom()

def tehran_today_str():
    """The current date (YYYY-MM-DD) in Iran time, used as the daily reset key for growth."""
    return datetime.datetime.now(IRAN_TZ).date().isoformat()

import db

async def midnight_tasks(context: ContextTypes.DEFAULT_TYPE):
    """Everything that closes out a Tehran day, in the order that keeps the books
    straight: settle yesterday's lottery, let the surviving bosses run, then collect
    the crown's tax on the fresh day, and finally nudge everyone to come grow."""
    today_str = tehran_today_str()
    yesterday_str = (datetime.datetime.now(IRAN_TZ).date() - datetime.timedelta(days=1)).isoformat()

    try:
        await draw_lottery(context, yesterday_str)
    except Exception:
        logging.exception("lottery draw failed")
    try:
        await expire_bosses_job(context)
    except Exception:
        logging.exception("boss expiry failed")

    for chat_id in db.get_all_chats():
        try:
            await collect_king_tax(context, chat_id, today_str)
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception:
            logging.exception(f"king tax failed for {chat_id}")

    await midnight_reminder(context)


async def midnight_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = db.get_all_chats()
    msg = "⏰ وقتشه دودولاتون رو بلند کنید!\nروز جدید شروع شده و می‌تونید دوباره سایزتون رو رشد بدید."
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=msg)
        except Forbidden as e:
            # Kicked from the group / group deleted: stop trying it every night forever.
            logging.info(f"Dropping unreachable chat {cid} from reminders: {e}")
            db.remove_chat(cid)
        except Exception as e:
            logging.error(f"Failed to send reminder to {cid}: {e}")


async def log_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs one line per update Telegram actually delivers. Registered in handler group
    -1 so it runs before (and independently of) the real handlers.

    Without this there is no way to tell "Telegram never delivered the message" apart
    from "a handler ran and crashed" - the two very different causes behind the same
    user-visible symptom of the bot ignoring a command. The bot previously logged only
    its *outgoing* API calls, which is why a total absence of replies was ambiguous."""
    try:
        if update.message is not None:
            logging.info("RX message chat=%s user=%s text=%r",
                         update.message.chat.id,
                         update.message.from_user.id if update.message.from_user else None,
                         (update.message.text or update.message.caption or '')[:64])
        elif update.edited_message is not None:
            logging.info("RX edited_message chat=%s", update.edited_message.chat.id)
        elif update.inline_query is not None:
            logging.info("RX inline_query user=%s q=%r",
                         update.inline_query.from_user.id, update.inline_query.query[:64])
        elif update.callback_query is not None:
            logging.info("RX callback_query user=%s data=%r",
                         update.callback_query.from_user.id, update.callback_query.data)
        elif update.my_chat_member is not None:
            m = update.my_chat_member
            logging.info("RX my_chat_member chat=%s %s -> %s",
                         m.chat.id, m.old_chat_member.status, m.new_chat_member.status)
        else:
            logging.info("RX other update: %s",
                         [k for k, v in update.to_dict().items() if k != 'update_id'])
    except Exception:
        logging.exception("failed to log an incoming update")


def cmd(pattern):
    """Filter for a text command. Gating on UpdateType.MESSAGE matters: a bare
    filters.Regex also fires for edited_message/channel_post updates, where
    update.message is None - every one of those crashed its handler mid-flight
    (the recurring "'NoneType' object has no attribute 'text'" in production,
    which users experienced as the bot ignoring a command)."""
    return filters.UpdateType.MESSAGE & filters.Regex(pattern)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler: without one, an exception in any handler is swallowed
    with just a log line and the user gets dead silence - the single biggest source
    of 'the bot ignores my commands' reports. Log it loudly and tell the user."""
    logging.error("Unhandled exception while processing an update", exc_info=context.error)
    try:
        if isinstance(update, Update):
            if update.callback_query:
                await update.callback_query.answer("⚠️ یه مشکل موقت پیش اومد؛ چند لحظه دیگه دوباره امتحان کن.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⚠️ یه مشکل موقت پیش اومد؛ چند لحظه دیگه دوباره امتحان کن.")
    except Exception:
        pass  # never let the error handler itself crash

def roll_nonzero(low, high):
    """random.randint(low, high) but re-rolled until it's not 0 (growth must always change something)."""
    delta = random.randint(low, high)
    while delta == 0:
        delta = random.randint(low, high)
    return delta

def get_dick_name(size):
    if size is None:
        size = 0
    if size < 0:
        return "شاه کص"
    elif size < 100:
        return "دودول"
    elif size < 500:
        return "شومبول"
    elif size < 1000:
        return "معامله"
    else:
        return "کیررررر"

def build_top_text(chat_id):
    """Build the group leaderboard message (every participant). Returns None if the group has no players."""
    rows = db.get_top_users_full(chat_id)
    if not rows:
        return None
    kingdom = db.get_kingdom(chat_id)
    king_id = kingdom[0] if kingdom else None
    consort_id = kingdom[2] if kingdom else None
    badge_counts = db.get_achievement_counts(chat_id)

    msg = "🏆 برترین‌های این گروه:\n\n"
    for i, (user_id, first_name, size, streak) in enumerate(rows, 1):
        size = size or 0
        d_name = get_dick_name(size)
        if i == 1:
            title = f"🥇 {d_name} طلا"
        elif i == 2:
            title = f"🥈 {d_name} نقره"
        elif i == 3:
            title = f"🥉 {d_name} برنزی"
        else:
            title = f"💩 {d_name} رعیت"
        marks = ""
        if user_id == king_id:
            marks += " 👑"
        if user_id == consort_id:
            marks += " 💍"
        if streak >= 3:
            marks += f" 🔥{streak}"
        if badge_counts.get(user_id):
            marks += f" 🏅{badge_counts[user_id]}"
        msg += f"{i}. {first_name}{marks} ({title}): {int(size)} سانتی‌متر\n"
    return msg

def build_inventory_view(user_id, chat_id):
    """Builds (message_text, keyboard) for a user's inventory in a group. Returns (None, None) if empty."""
    items = db.get_inventory(user_id, chat_id)
    active_item = db.get_user_active_item(user_id, chat_id)
    if not items and not active_item:
        return None, None

    msg = "🎒 **آیتم‌های شما در این گروه:**\n\n"
    keyboard = []
    for item_name, qty in items:
        desc = ITEM_DESCRIPTIONS.get(item_name, '')
        msg += f"- {item_name}: {qty} عدد\n  └ {desc}\n"
        # Passive items fire on their own, so offering an "use it" button for them
        # would just be a button that says "you can't press this".
        if item_name in PASSIVE_ITEMS:
            continue
        keyboard.append([InlineKeyboardButton(f"استفاده از {item_name}", callback_data=f"useitem_{user_id}_{item_name}")])
    if active_item:
        msg += f"\n🔥 آیتم فعال برای چالش بعدی: **{active_item}**"

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return msg, reply_markup

# Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = '8802494355:AAFYiGyKph3R8wLiZoeDsELOPx07Q9ZvuVw'

BET_AMOUNTS = [5, 10, 50, 100]


def sign_payload(payload):
    """Short HMAC tag over a callback_data payload. Telegram's callback_data is
    supplied by the client, so anything the bot *trusts* from it (a stake, an amount)
    has to be signed - otherwise a patched client can simply send
    `chal_<victim>_999999` and the bot would honour it."""
    return hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()[:10]


def verify_payload(payload, tag):
    return hmac.compare_digest(sign_payload(payload), tag)


BET_WINDOW_SECONDS = 20
# A rematch has no spectator betting window - just a short beat of "rolling the
# dice..." before it settles through the same persisted path as a normal match.
REMATCH_ROLL_SECONDS = 2

def render_bet_message(match_state):
    lines = [
        f"⚔️ مسابقه بین {match_state['challenger_name']} و {match_state['acceptor_name']} بر سر {match_state['bet']} سانتی‌متر آغاز شد!",
        f"🎰 تا {BET_WINDOW_SECONDS} ثانیه بقیهٔ اعضای گروه می‌تونن رو نتیجه شرط ببندن (شروع‌کننده = {match_state['challenger_name']}):",
    ]
    if match_state["bets"]:
        lines.append("\n📋 شرط‌های ثبت‌شده تا الان:")
        for side, amount, name in match_state["bets"].values():
            side_fa = "برد" if side == "win" else "باخت"
            lines.append(f"- {name}: {amount} سانت گذاشت روی {side_fa} {match_state['challenger_name']}")
    return "\n".join(lines)

def build_challenge_data(challenger_id, bet):
    """callback_data for a challenge button: the stake is signed so it can't be edited
    by a patched client, and the nonce makes the button single-accept (see
    db.claim_challenge). Stays well inside Telegram's 64-byte callback_data limit."""
    nonce = uuid4().hex[:10]
    payload = f"{challenger_id}_{int(bet)}_{nonce}"
    return f"chal_{payload}_{sign_payload(payload)}"


def build_bet_keyboard(match_id):
    # One row per amount (win/lose side by side) so labels stay readable on small screens,
    # instead of cramming 4 buttons into a single row.
    rows = [
        [
            InlineKeyboardButton(f"✅ برد {amt}", callback_data=f"bet_{match_id}_win_{amt}"),
            InlineKeyboardButton(f"❌ باخت {amt}", callback_data=f"bet_{match_id}_lose_{amt}"),
        ]
        for amt in BET_AMOUNTS
    ]
    return InlineKeyboardMarkup(rows)

PERK_DESCRIPTIONS = {
    "عادی": "شما امروز پرک خاصی نگرفتید (عادی 👤).",
    "جاکش": "شما پرک **جاکش 🤡** گرفتید! (از بردهای چالش ۵۰٪ کمتر سایز میگیرید).",
    "کص‌کش": "شما پرک **کص‌کش 😈** گرفتید! (از بردهای چالش ۲۰٪ بیشتر سایز میگیرید).",
    "حرومزاده": "شما پرک **حرومزاده 🥶** گرفتید! (دودول شما یخ زد و امروز نمی‌تونید چالش بدید یا بگیرید).",
    "لاشی": "شما پرک **لاشی 🦅** گرفتید! (اگه تو چالش ببازید ۵۰٪ کمتر سایز از دست میدید).",
    "خایه‌مال": "شما پرک **خایه‌مال 🤲** گرفتید! (+۵ سانت هدیه بلافاصله اضافه شد).",
    "کون‌گشاد": "شما پرک **کون‌گشاد 🦥** گرفتید! (تاس‌های شما همیشه ۱ دونه کمتر محاسبه میشه).",
    "زن جنده": "به صورت رندوم یکی از اعضای گروه رو انتخاب می‌کنه و اگه سایزش بیشتر از ۱۰ باشه ۵ سانت از اون کم می‌کنه و به تو اضافه می‌کنه.",
    "جقی": "موقع چالش ممکنه عدد تاس رو رندوم به شدت بالا یا پایین ببره!",
    "کیرکلفت": "شما پرک **کیرکلفت 💪** گرفتید! (یه رشد اضافه هم بلافاصله گیرت اومد).",
    "کص‌شانس": "شما پرک **کص‌شانس 🍀** گرفتید! (امروز شانس پیدا کردن آیتم دو برابره).",
    "کیرشکسته": "شما پرک **کیرشکسته 💔** گرفتید! (یه مقدار سانت هم بلافاصله از دست دادید).",
    "کون‌سوخته": "شما پرک **کون‌سوخته 🔥** گرفتید! (امروز نمی‌تونید از هیچ آیتمی استفاده کنید).",
    "حروم‌دست": "شما پرک **حروم‌دست 🎲** گرفتید! (تاس‌های امروزتون تو چالش ۲ عدد کمتر محاسبه میشه)."
}

ITEM_DESCRIPTIONS = {
    "ویاگرا": "بده به یکی تا ۴۰ سانت بره رو کیرش! (/use ویاگرا @username)",
    "قرص اورژانسی": "بده به یکی تا ۴۰ سانت از کیرش کم بشه! (/use قرص اورژانسی @username)",
    "زعفرون": "بده به یکی تا ۵۰ الی ۱۵۰ سانت از کیرش کم بشه! (/use زعفرون @username)",
    "کاندوم": "فعالش کن تا اگه تو چالش باختی ۵۰٪ شانس داشته باشی هیچی ازت کم نشه.",
    "شیر موز": "فعالش کن تا اگه تو چالش بردی ۵ تا ۱۵ سانت بیشتر از حریف بدزدی.",
    "سوزن": "فعالش کن تا کاندوم حریفت رو تو چالش پاره کنی.",
    "طلسم": "فعالش کن تا اثر شیر موز حریفت رو باطل کنی.",
    "اسپری": "فعالش کن تا تاس حریفت رو تو چالش یکی کم کنی.",
    "قفل": "خودکار عمل می‌کنه: جلوی یه دزدی رو می‌گیره و بعدش مصرف می‌شه."
}

# Challenge items are "activated" for your next challenge; direct items are applied
# straight onto a target's size (need someone to target, so no plain "استفاده از X"
# button); passive items just sit in the bag and fire on their own when relevant.
CHALLENGE_ITEMS = ["کاندوم", "شیر موز", "سوزن", "طلسم", "اسپری"]
DIRECT_ITEMS = ["ویاگرا", "قرص اورژانسی", "زعفرون"]
PASSIVE_ITEMS = ["قفل"]

# Buying is the game's only real size *sink* - everything else (daily growth, boss
# rewards, one-sided spectator books) only ever creates size. Prices are deliberately
# steep enough that buying is a real trade-off against saving for a challenge.
SHOP_PRICES = {
    "ویاگرا": 60,
    "قرص اورژانسی": 60,
    "زعفرون": 220,
    "کاندوم": 45,
    "شیر موز": 45,
    "سوزن": 35,
    "طلسم": 35,
    "اسپری": 30,
    "قفل": 40,
}

def apply_direct_item(item_name, target_user_id, target_name, chat_id):
    """Applies a direct item's effect to a target and returns the result message."""
    if item_name == "ویاگرا":
        db.update_size(target_user_id, chat_id, 40)
        return f"شما با ویاگرا ۴۰ سانت به {target_name} اضافه کردید!"
    elif item_name == "قرص اورژانسی":
        db.update_size(target_user_id, chat_id, -40)
        return f"شما با قرص اورژانسی ۴۰ سانت از {target_name} کم کردید!"
    elif item_name == "زعفرون":
        loss = random.randint(50, 150)
        db.update_size(target_user_id, chat_id, -loss)
        return f"شما با زعفرون {loss} سانت از {target_name} کم کردید!"
    return "آیتم نامشخص."

def get_target_user(update: Update, text: str, chat_id: int):
    """Helper to find the target user of a command (either by reply or username)"""
    target_user_id = None
    target_first_name = None

    if update.message and update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        target_first_name = update.message.reply_to_message.from_user.first_name
        # Ensure target exists in DB
        db.get_user(target_user_id, chat_id, update.message.reply_to_message.from_user.username, target_first_name)
    else:
        parts = text.split()
        # The target isn't always the first argument: "/use ویاگرا @user" puts the
        # item name in parts[1]. Prefer an explicit @mention anywhere in the text,
        # falling back to the first argument for the bare "/dd username 10" style.
        candidates = [p for p in parts[1:] if p.startswith('@')] or parts[1:2]
        if candidates:
            row = db.find_user_by_username(candidates[0], chat_id)
            if row:
                target_user_id, target_first_name, _ = row

    return target_user_id, target_first_name

def drop_item(user_id, chat_id, chance=0.3):
    if random.random() > chance:
        return None
    
    # Pool weights: viagra 24, pill 10, saffron 1, condom 15, milk 15, needle 10, spell 15, spray 10
    pool = ["ویاگرا"]*24 + ["قرص اورژانسی"]*10 + ["زعفرون"]*1 + ["کاندوم"]*15 + ["شیر موز"]*15 + ["سوزن"]*10 + ["طلسم"]*15 + ["اسپری"]*10
    item = random.choice(pool)
    db.add_inventory(user_id, chat_id, item)
    return item

def resolve_chat_id(callback_query):
    """Resolve chat_id from a callback query, whether from a direct message or inline message.
    Also saves the chat_instance -> chat_id mapping for future lookups."""
    if callback_query.message and callback_query.message.chat:
        chat_id = callback_query.message.chat.id
        # Save mapping for future inline callbacks
        if callback_query.chat_instance:
            db.track_chat_instance(callback_query.chat_instance, chat_id)
        return chat_id
    
    # Inline message - try to resolve from saved chat_instance
    if callback_query.chat_instance:
        chat_id = db.get_chat_id_from_instance(callback_query.chat_instance)
        if chat_id:
            return chat_id
    
    return None

HELP_TEXT = (
    "🍆 **راهنمای بازی دودول**\n\n"
    "**پایه**\n"
    "🌱 /d — رشد روزانه (هر روز پیاپی، پاداش استریک بیشتر 🔥)\n"
    "🏆 /t — لیدربرد گروه\n"
    "🎒 /i — آیتم‌های من\n"
    "💉 /u <آیتم> @کاربر — استفاده از آیتم\n"
    "🎁 /dd @کاربر <مقدار> — اهدای سایز\n"
    "📊 /wr — آمار برد و باخت\n\n"
    "**رقابت**\n"
    "⚔️ /c <مقدار> — ایجاد چالش\n"
    "⚖️ /ejma @کاربر — رای‌گیری برای کم‌کردن سایز یکی\n"
    "🥷 /dozdi @کاربر — دزدی از یکی (هر ۶ ساعت یک بار)\n\n"
    "**سلطنت**\n"
    "👑 /king — پادشاه و همسرش کیه و قوانین تاج\n"
    "💍 /hamsar @کاربر — پادشاه همسر انتخاب می‌کنه\n"
    "🗡️ /khianat @کاربر — همسر پادشاه خیانت می‌کنه!\n"
    "💔 /talagh — پادشاه همسرش رو طلاق می‌ده\n\n"
    "**اقتصاد و سرگرمی**\n"
    "🏪 /shop — خرید آیتم با سانت\n"
    "🎟️ /lottery — لاتاری روزانه (قرعه‌کشی نیمه‌شب)\n"
    "🏅 /ach — نشان‌های من\n"
    "🐉 هر شب ساعت ۲۰ یه باس میاد؛ همه با هم بزنیدش!\n\n"
    "می‌تونید با @dickchallengerbot به صورت اینلاین هم بازی کنید."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def dick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    
    current_size, last_grown, current_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    
    today_str = tehran_today_str()
    if last_grown == today_str:
        await update.message.reply_text("شما امروز دودول خود را در این گروه رشد داده‌اید! تا فردا صبر کنید.")
        return
        
    keyboard = [[InlineKeyboardButton("بمالش تا بزرگ شه 💦", callback_data=f"grow_self_{user.id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🌱 {user.first_name} می‌خواد دودولش رو بماله...",
        reply_markup=reply_markup
    )

async def inventory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    db.get_user(user.id, chat_id, user.username, user.first_name)

    msg, reply_markup = build_inventory_view(user.id, chat_id)
    if not msg:
        await update.message.reply_text("کیف پول شما در این گروه خالی است!")
        return

    await update.message.reply_text(msg, reply_markup=reply_markup)

async def use_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return
    
    data = query.data.split('_')
    if len(data) < 3 or data[0] != 'useitem':
        return
        
    target_id = int(data[1])
    item_name = data[2]
    
    if user.id != target_id:
        await query.answer("شما فقط می‌توانید از آیتم‌های خودتان استفاده کنید!", show_alert=True)
        return

    _, _, user_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    if user_perk == "کون‌سوخته":
        await query.answer("شما امروز پرک کون‌سوخته 🔥 رو دارید و نمی‌تونید از هیچ آیتمی استفاده کنید!", show_alert=True)
        return


    if item_name in DIRECT_ITEMS:
        await query.answer(f"برای استفاده از {item_name} باید تو گروه بنویسی:\n/use {item_name} @username", show_alert=True)
        return
        
    if item_name in CHALLENGE_ITEMS:
        current_active = db.get_user_active_item(user.id, chat_id)
        if current_active:
            await query.answer("شما از قبل یک آیتم چالشی فعال دارید! اول در یک چالش شرکت کنید.", show_alert=True)
            return
            
        success = db.use_inventory(user.id, chat_id, item_name)
        if not success:
            await query.answer("شما این آیتم را ندارید!", show_alert=True)
            return
            
        db.set_user_active_item(user.id, chat_id, item_name)
        await query.answer(f"آیتم {item_name} با موفقیت فعال شد! تو چالش بعدی اعمال میشه.", show_alert=True)

        # update inventory message
        msg, reply_markup = build_inventory_view(user.id, chat_id)
        await query.edit_message_text(msg, reply_markup=reply_markup)

async def use_direct_item_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirms a direct item picked from the "@username" inline flow and applies it."""
    query = update.callback_query
    user = query.from_user

    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return

    data = query.data.split('_')
    if len(data) != 4 or data[0] != 'udi':
        return
    actor_id, target_id, item_name = int(data[1]), int(data[2]), data[3]

    if user.id != actor_id:
        await query.answer("این دکمه مال شما نیست!", show_alert=True)
        return

    _, _, user_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    if user_perk == "کون‌سوخته":
        await query.answer("شما امروز پرک کون‌سوخته 🔥 رو دارید و نمی‌تونید از هیچ آیتمی استفاده کنید!", show_alert=True)
        return

    success = db.use_inventory(user.id, chat_id, item_name)
    if not success:
        await query.answer("این آیتم رو دیگه ندارید!", show_alert=True)
        return

    target_info = db.get_user_info(target_id, chat_id)
    target_name = target_info[0] if target_info else "ناشناس"

    msg = apply_direct_item(item_name, target_id, target_name, chat_id)
    await query.answer("انجام شد!")
    await query.edit_message_text(msg)

async def use_item_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    _, _, user_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    if user_perk == "کون‌سوخته":
        await update.message.reply_text("شما امروز پرک کون‌سوخته 🔥 رو دارید و نمی‌تونید از هیچ آیتمی استفاده کنید!")
        return

    text = update.message.text
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("استفاده: `/use نام_آیتم`\nمثال: `/use کاندوم`")
        return
        
    # item name might be multiple words
    item_name = " ".join([p for p in parts[1:] if not p.startswith('@')])

    if item_name not in CHALLENGE_ITEMS and item_name not in DIRECT_ITEMS:
        return  # not a real item name at all - stay silent instead of replying

    # check if user has item
    items = db.get_inventory(user.id, chat_id)
    has_item = False
    for i_name, qty in items:
        if i_name == item_name and qty > 0:
            has_item = True
            break
            
    if not has_item:
        await update.message.reply_text(f"شما آیتم '{item_name}' را در این گروه ندارید!")
        return
        
    
    if item_name in CHALLENGE_ITEMS:
        current_active = db.get_user_active_item(user.id, chat_id)
        if current_active:
            await update.message.reply_text("شما از قبل یک آیتم چالشی فعال دارید! اول در یک چالش شرکت کنید.")
            return
        
        db.use_inventory(user.id, chat_id, item_name)
        db.set_user_active_item(user.id, chat_id, item_name)
        try:
            await context.bot.send_message(chat_id=user.id, text=f"آیتم چالشی **{item_name}** برای شما در گروه فعال شد!")
            await update.message.reply_text("آیتم فعال شد. چک پی‌وی.")
        except:
            await update.message.reply_text("آیتم چالشی فعال شد! 🤫 (به دلیل بسته بودن پی‌وی اینجا اعلام کردم)")
            
    elif item_name in DIRECT_ITEMS:
        target_user_id, target_first_name = get_target_user(update, text, chat_id)
        if not target_user_id:
            await update.message.reply_text("باید روی یک نفر ریپلای کنید یا یوزرنیمش رو منشن کنید!")
            return

        # Consume the item BEFORE applying its effect - the other order let a race
        # (or a failed decrement) apply the effect for free.
        if not db.use_inventory(user.id, chat_id, item_name):
            await update.message.reply_text(f"شما آیتم '{item_name}' را در این گروه ندارید!")
            return
        msg = apply_direct_item(item_name, target_user_id, target_first_name, chat_id)
        await update.message.reply_text(msg)

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    msg = build_top_text(chat_id)

    if not msg:
        await update.message.reply_text("هنوز هیچکس در این گروه در بازی شرکت نکرده است!")
        return

    await update.message.reply_text(msg)

async def winrate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    text = update.message.text

    target_user_id, target_first_name = get_target_user(update, text, chat_id)
    if not target_user_id:
        target_user_id, target_first_name = user.id, user.first_name
        db.get_user(user.id, chat_id, user.username, user.first_name)

    wins, losses = db.get_win_loss(target_user_id, chat_id)
    total = wins + losses
    if total == 0:
        await update.message.reply_text(f"{target_first_name} هنوز هیچ چالشی رو تموم نکرده!")
        return

    win_rate = round(wins / total * 100)
    await update.message.reply_text(
        f"🎲 آمار چالش‌های {target_first_name} در این گروه:\n"
        f"✅ برد: {wins}\n❌ باخت: {losses}\n📊 وین‌ریت: {win_rate}٪ (از {total} چالش)"
    )

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    text = update.message.text
    
    db.get_user(user.id, chat_id, user.username, user.first_name)

    wait_remaining = db.get_donation_wait_remaining(user.id, chat_id)
    if wait_remaining is not None:
        days = max(1, int(wait_remaining.total_seconds() // 86400) + 1)
        await update.message.reply_text(
            f"تازه به این گروه پیوسته‌اید! تا {days} روز دیگر می‌توانید سایز اهدا کنید."
        )
        return

    target_user_id, target_first_name = get_target_user(update, text, chat_id)

    if not target_user_id:
        await update.message.reply_text("استفاده صحیح:\n/dd @username <مقدار>\nیا ریپلای کردن روی پیام شخص و تایپ /dd <مقدار>")
        return

    if target_user_id == user.id:
        await update.message.reply_text("نمی‌توانید به خودتان اهدا کنید!")
        return
        
    parts = text.split()
    amount_str = parts[-1] if len(parts) > 1 else ""
    try:
        amount = float(amount_str)
        # isfinite blocks "nan" (which passes every <= comparison and would poison
        # both users' sizes into NaN forever) and "inf".
        if not math.isfinite(amount) or amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("لطفا یک مقدار معتبر وارد کنید.")
        return

    if not db.try_deduct_size(user.id, chat_id, amount):
        await update.message.reply_text("شما به اندازه کافی سانتی‌متر برای اهدا در این گروه ندارید!")
        return
    db.update_size(target_user_id, chat_id, amount)
    
    new_size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    await update.message.reply_text(f"شما {int(amount)} سانتی‌متر به {target_first_name} اهدا کردید!\nسایز جدید شما: {int(new_size)} سانتی‌متر.")

MIN_CONSENSUS_PLAYERS = 3
CONSENSUS_STEAL_RATIO = 0.30
CONSENSUS_VOTE_WINDOW_SECONDS = 3600

def render_consensus_message(target_name, amount, required_votes, total_players, voters):
    lines = [
        f"⚖️ اجماع علیه {target_name}",
        f"در صورت رای‌آوری، {int(amount)} سانتی‌متر از {target_name} کم می‌شود.",
        f"برای موفقیت نیاز به {required_votes} رای موافق از {total_players} نفری است که امروز فعال بوده‌اند.",
        "⏰ مهلت رای‌گیری: ۱ ساعت.",
    ]
    if voters:
        lines.append("\n🗳️ رای‌ها:")
        lines.extend(f"{'✅' if choice == 'yes' else '❌'} {name}" for name, choice in voters)
    return "\n".join(lines)

async def consensus_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    vote_id = job_data["vote_id"]
    chat_id = job_data["chat_id"]
    message_id = job_data["message_id"]

    consensus = db.get_consensus(vote_id)
    if not consensus:
        return
    v_chat_id, target_id, target_name, initiator_id, amount, required_votes, total_players, status, _ = consensus
    if status != 'open':
        return  # already resolved (success or early failure) by a vote before the deadline

    db.fail_open_consensus(chat_id, target_id, target_name)
    voters = db.get_consensus_voters(vote_id)
    msg = render_consensus_message(target_name, amount, required_votes, total_players, voters)
    msg += "\n\n⏰ مهلت یک‌ساعتهٔ اجماع تمام شد و به حد نصاب نرسید! اجماع شکست خورد."
    msg += f"\n🛡️ {target_name} تا ۳ روز در برابر اجماع جدید محافظت می‌شود."
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=msg)
    except:
        pass

def build_consensus_keyboard(vote_id, yes_count, no_count):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ موافقم ({yes_count})", callback_data=f"ejmavote_{vote_id}_yes"),
        InlineKeyboardButton(f"❌ مخالفم ({no_count})", callback_data=f"ejmavote_{vote_id}_no"),
    ]])

async def consensus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    text = update.message.text

    _, initiator_last_grown, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    today_str = tehran_today_str()
    if initiator_last_grown != today_str:
        await update.message.reply_text("فقط کسایی که امروز دودولشونو مالیدن (/d زدن) می‌تونن اجماع راه بندازن!")
        return

    target_user_id, target_first_name = get_target_user(update, text, chat_id)

    if not target_user_id:
        await update.message.reply_text("استفاده صحیح:\n/ejma @username\nیا ریپلای کردن روی پیام شخص و تایپ /ejma")
        return

    if target_user_id == user.id:
        await update.message.reply_text("نمی‌توانید علیه خودتان اجماع کنید!")
        return

    target_info = db.get_user_info(target_user_id, chat_id)
    target_size = target_info[1] if target_info else 0.0
    if target_size <= 0:
        await update.message.reply_text(f"{target_first_name} سایز کافی برای اجماع ندارد!")
        return

    # The crown buys no shelter: the king is the one player the group can always
    # gang up on, no matter how recently they were last targeted. Recompute it first,
    # so a player who was dethroned an hour ago isn't still treated as the king.
    kingdom, _ = refresh_king(chat_id)
    king_is_target = bool(kingdom and kingdom[0] == target_user_id)

    remaining = None if king_is_target else db.get_consensus_protection_remaining(chat_id, target_user_id)
    if remaining is not None:
        hours = max(1, int(remaining.total_seconds() // 3600))
        await update.message.reply_text(
            f"{target_first_name} در حال حاضر در برابر اجماع محافظت‌شده است! تا حدود {hours} ساعت دیگر نمی‌شود دوباره علیه او اجماع کرد."
        )
        return

    existing_open = db.get_open_consensus(chat_id, target_user_id)
    if existing_open:
        _, elapsed_seconds = existing_open
        if elapsed_seconds >= CONSENSUS_VOTE_WINDOW_SECONDS:
            db.fail_open_consensus(chat_id, target_user_id, target_first_name)
            await update.message.reply_text(
                f"اجماع قبلی علیه {target_first_name} به حد نصاب رای نرسیده بود و شکست خورد!\n"
                f"تا ۳ روز دیگر نمی‌شود علیه او اجماع جدیدی راه انداخت."
            )
        else:
            await update.message.reply_text(
                f"یک اجماع علیه {target_first_name} هم‌اکنون در حال رای‌گیری است! صبر کنید تا نتیجه‌اش مشخص شود."
            )
        return

    player_count = db.get_active_today_count(chat_id, today_str)
    if player_count < MIN_CONSENSUS_PLAYERS:
        await update.message.reply_text(f"برای اجماع حداقل به {MIN_CONSENSUS_PLAYERS} نفر که امروز دودولشونو مالیدن نیاز است!")
        return

    required_votes = player_count // 2 + 1
    amount = max(1, round(target_size * CONSENSUS_STEAL_RATIO))

    vote_id = db.create_consensus(chat_id, target_user_id, target_first_name, user.id, user.first_name, amount, required_votes, player_count)

    voters = db.get_consensus_voters(vote_id)
    sent_message = await update.message.reply_text(
        render_consensus_message(target_first_name, amount, required_votes, player_count, voters),
        reply_markup=build_consensus_keyboard(vote_id, 1, 0)
    )
    context.job_queue.run_once(
        consensus_timeout_job,
        when=CONSENSUS_VOTE_WINDOW_SECONDS,
        data={"vote_id": vote_id, "chat_id": chat_id, "message_id": sent_message.message_id},
        name=f"consensus_timeout_{vote_id}"
    )

async def consensus_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return

    data = query.data.split('_')
    if len(data) != 3 or data[0] != 'ejmavote':
        return
    vote_id = int(data[1])
    choice = data[2]

    consensus = db.get_consensus(vote_id)
    if not consensus:
        await query.answer("این رای‌گیری وجود ندارد!", show_alert=True)
        return

    v_chat_id, target_id, target_name, initiator_id, amount, required_votes, total_players, status, elapsed_seconds = consensus

    # A vote id is just a number in client-supplied callback_data: without this check a
    # member of group B could vote on (and swing) a consensus running in group A, where
    # they were never eligible.
    if v_chat_id != chat_id:
        await query.answer("این رای‌گیری مال این گروه نیست!", show_alert=True)
        return

    if status != 'open':
        await query.answer("این رای‌گیری دیگر فعال نیست!", show_alert=True)
        return

    if elapsed_seconds >= CONSENSUS_VOTE_WINDOW_SECONDS:
        db.fail_open_consensus(chat_id, target_id, target_name)
        await query.answer("مهلت این اجماع تمام شده بود!", show_alert=True)
        voters = db.get_consensus_voters(vote_id)
        msg = render_consensus_message(target_name, amount, required_votes, total_players, voters)
        msg += "\n\n⏰ مهلت یک‌ساعتهٔ اجماع تمام شد و به حد نصاب نرسید! اجماع شکست خورد."
        msg += f"\n🛡️ {target_name} تا ۳ روز در برابر اجماع جدید محافظت می‌شود."
        try:
            await query.edit_message_text(msg)
        except:
            pass
        return

    if user.id == target_id:
        await query.answer("نمی‌توانید به اجماع علیه خودتان رای بدهید!", show_alert=True)
        return

    _, voter_last_grown, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    today_str = tehran_today_str()
    if voter_last_grown != today_str:
        await query.answer("فقط کسایی که امروز دودولشونو مالیدن (/d زدن) می‌تونن به اجماع رای بدن!", show_alert=True)
        return

    is_new_vote = db.cast_consensus_vote(vote_id, user.id, user.first_name, choice)
    if not is_new_vote:
        await query.answer("شما قبلاً رای داده بودید!", show_alert=True)
        return

    yes_count, no_count = db.get_consensus_vote_counts(vote_id)
    voters = db.get_consensus_voters(vote_id)

    if yes_count >= required_votes:
        if db.resolve_consensus_success(vote_id, chat_id, target_id, target_name):
            db.update_size(target_id, chat_id, -amount)
            new_size, _, _ = db.get_user(target_id, chat_id, None, None)
            await query.answer("اجماع موفق شد!")
            msg = render_consensus_message(target_name, amount, required_votes, total_players, voters)
            msg += f"\n\n🎉 اجماع با {yes_count} رای موافق موفق شد!"
            msg += f"\n📉 {int(amount)} سانتی‌متر از {target_name} کم شد. اندازه جدید: {int(new_size)} سانتی‌متر."
            msg += f"\n🛡️ {target_name} تا ۶ روز در برابر اجماع جدید محافظت می‌شود."
            await query.edit_message_text(msg)
        return

    # Early failure: if the remaining eligible voters could never push "yes" to the required threshold, stop now.
    # The target can't vote, so exclude them from the pool - but only if they're actually
    # IN the pool (grew today); subtracting 1 unconditionally could declare failure one
    # voter too early when the target wasn't part of the active count.
    _, target_last_grown, _ = db.get_user(target_id, chat_id, None, None)
    target_in_pool = 1 if target_last_grown == today_str else 0
    remaining_pool = max(0, total_players - target_in_pool - (yes_count + no_count))
    if yes_count + remaining_pool < required_votes:
        db.fail_open_consensus(chat_id, target_id, target_name)
        await query.answer("اجماع شکست خورد!")
        msg = render_consensus_message(target_name, amount, required_votes, total_players, voters)
        msg += f"\n\n💔 اجماع دیگر شانسی برای رای‌آوری نداشت و شکست خورد!"
        msg += f"\n🛡️ {target_name} تا ۳ روز در برابر اجماع جدید محافظت می‌شود."
        await query.edit_message_text(msg)
        return

    await query.answer(f"رای شما ({'موافق' if choice == 'yes' else 'مخالف'}) ثبت شد!")
    # Re-read the status: the one-hour timeout job may have resolved this vote while we
    # were awaiting above, and re-attaching live buttons here would resurrect a vote
    # that already failed and already granted the target its protection.
    still_open = db.get_consensus(vote_id)
    if not still_open or still_open[7] != 'open':
        return
    try:
        await query.edit_message_text(
            render_consensus_message(target_name, amount, required_votes, total_players, voters),
            reply_markup=build_consensus_keyboard(vote_id, yes_count, no_count)
        )
    except:
        pass

async def challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    text = update.message.text
    
    db.get_user(user.id, chat_id, user.username, user.first_name)
    
    parts = text.split()
    bet = 10 # default 10 cm
    if len(parts) > 1:
        try:
            bet = int(parts[1])
            if bet <= 0:
                raise ValueError
        except ValueError:
            pass
            
    user_size, _, user_perk = db.get_user(user.id, chat_id, None, None)
    if user_perk == "حرومزاده":
        await update.message.reply_text("شما امروز پرک حرومزاده 🥶 رو دارید و کیرتون فیریز شده! نمی‌تونید چالش ایجاد کنید.")
        return
        
    if user_perk == "جقی":
        bet = random.randint(max(1, bet - int(bet/2)), bet + int(bet))
        
    if user_size < bet:
        if user_perk == "جقی":
            await update.message.reply_text(f"پرک جقی باعث شد شرط شما بشه {bet} سانت، ولی شما اینقدر سانت در این گروه ندارید!")
        else:
            await update.message.reply_text(f"شما به اندازه کافی سایز برای شرط {bet} سانتی‌متری در این گروه ندارید! سایز فعلی شما: {int(user_size)}")
        return
        
    keyboard = [[InlineKeyboardButton("بیا کیرمو بخور ⚔️", callback_data=build_challenge_data(user.id, bet))]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"⚔️ {user.first_name} یک چالش با شرط {bet} سانتی‌متر در این گروه ایجاد کرد!\nاولین نفری که دکمه زیر را فشار دهد وارد مسابقه می‌شود.",
        reply_markup=reply_markup
    )

async def accept_challenge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return
    
    db.get_user(user.id, chat_id, user.username, user.first_name)

    data = query.data.split('_')
    if len(data) != 5 or data[0] != 'chal':
        return
    challenger_id, bet_str, nonce, tag = data[1], data[2], data[3], data[4]
    # Reject anything whose stake/challenger was edited client-side before trusting it.
    if not verify_payload(f"{challenger_id}_{bet_str}_{nonce}", tag):
        await query.answer("این دکمه معتبر نیست!", show_alert=True)
        return
    challenger_id, bet = int(challenger_id), int(bet_str)

    # Claim the button before touching any money: only the first tapper wins the race,
    # everyone else (including the same user double-tapping) bounces off here.
    if not db.claim_challenge(nonce):
        await query.answer("این چالش قبلاً پذیرفته شده!", show_alert=True)
        return

    def _release():
        db.release_challenge(nonce)

    if user.id == challenger_id:
        _release()
        await query.answer("شما نمی‌توانید چالش خودتان را بپذیرید!", show_alert=True)
        return

    challenger_row = db.get_user(challenger_id, chat_id, None, None)
    if not challenger_row or challenger_row[0] < bet:
        _release()
        await query.answer("شروع‌کننده چالش در حال حاضر سایز کافی ندارد!", show_alert=True)
        return

    if challenger_row[2] == "حرومزاده":
        _release()
        await query.answer("کیر شروع‌کننده امروز فیریز شده (پرک حرومزاده)! نمی‌تواند چالش انجام دهد.", show_alert=True)
        return

    user_size, _, user_perk = db.get_user(user.id, chat_id, None, None)
    if user_perk == "حرومزاده":
        _release()
        await query.answer("شما امروز پرک حرومزاده 🥶 رو دارید و کیرتون فیریز شده! نمی‌تونید چالش رو بپذیرید.", show_alert=True)
        return

    # Stake both sides' bet immediately the moment the match actually starts, so nobody
    # can accept multiple challenges at once using the same not-yet-deducted size. The
    # deductions are atomic check-and-take, so a concurrent stake elsewhere can't spend
    # the same centimeters twice.
    if not db.try_deduct_size(challenger_id, chat_id, bet):
        _release()
        await query.answer("شروع‌کننده چالش در حال حاضر سایز کافی ندارد!", show_alert=True)
        return
    if not db.try_deduct_size(user.id, chat_id, bet):
        db.update_size(challenger_id, chat_id, bet)  # hand the challenger's stake back
        _release()
        await query.answer(f"شما حداقل {int(bet)} سانتی‌متر برای شرکت در این گروه نیاز دارید!", show_alert=True)
        return

    challenger_info = db.get_user_info(challenger_id, chat_id)
    challenger_name = challenger_info[0] if challenger_info else "ناشناس"

    match_id = str(uuid4())
    db.create_pvp_match(match_id, chat_id, challenger_id, challenger_name, user.id, user.first_name, bet)

    match_state = {
        "challenger_name": challenger_name,
        "acceptor_name": user.first_name,
        "bet": bet,
        "bets": {},
    }

    await query.answer("چالش پذیرفته شد!")
    await query.edit_message_text(
        render_bet_message(match_state),
        reply_markup=build_bet_keyboard(match_id)
    )
    # query.message is None for a callback on a genuinely inline-posted message (the
    # "via @dickchallengerbot" flow) - only inline_message_id is available then. Reading
    # query.message.message_id unconditionally crashed this handler for every such
    # challenge, right after the betting-window message was already shown but before the
    # resolution job below got scheduled - leaving the match stuck forever with no dice
    # ever rolled. Persist whichever id is actually available.
    if query.message:
        db.set_pvp_match_message(match_id, message_id=query.message.message_id)
    else:
        db.set_pvp_match_message(match_id, inline_message_id=query.inline_message_id)

    # Resolved by a scheduled job rather than an inline sleep here, so a bot restart
    # mid-window (e.g. a deploy landing right in the middle of the 20s betting window)
    # can't silently kill this handler and leave the match orphaned - see resolve_pvp_match.
    context.job_queue.run_once(
        pvp_resolve_job, when=BET_WINDOW_SECONDS, data={"match_id": match_id}, name=f"pvp_resolve_{match_id}"
    )

async def deliver_pvp_message(context: ContextTypes.DEFAULT_TYPE, chat_id, message_id, text, reply_markup=None, inline_message_id=None):
    """Edits the original match message if possible, falling back to a fresh message
    (e.g. the old message can no longer be edited after a restart, or there was never a
    normal message_id to begin with - just an inline_message_id)."""
    try:
        if inline_message_id:
            await context.bot.edit_message_text(inline_message_id=inline_message_id, text=text, reply_markup=reply_markup)
        else:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except Exception:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Failed to deliver PvP match result to {chat_id}: {e}")

async def resolve_pvp_match(context: ContextTypes.DEFAULT_TYPE, match_id):
    """Settles a PvP match's dice roll BET_WINDOW_SECONDS after acceptance. This is the
    single place both the normal post-acceptance job and the startup recovery sweep (for
    matches orphaned by a bot restart mid-window) call to decide a winner - it reads
    perks/items fresh from the DB instead of capturing them at accept-time, so it behaves
    identically whether it fires right on schedule or late, after a restart."""
    if not db.claim_pvp_match(match_id):
        return  # already resolved (race with another caller), or doesn't exist

    match = db.get_pvp_match(match_id)
    if not match:
        return
    chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, bet, message_id, inline_message_id, _status = match
    bet = int(bet)
    bets = {}
    # Flipped the moment settlement money starts moving: past that point the except
    # branch must never blanket-refund the escrow again (it would double-pay).
    settled = False

    try:
        bets = {uid: (side, amount, name) for uid, side, amount, name in db.get_pvp_bets(match_id)}

        _, _, c_perk = db.get_user(challenger_id, chat_id, None, None)
        _, _, user_perk = db.get_user(acceptor_id, chat_id, None, None)

        val1 = _dice_rng.randint(1, 6)
        val2 = _dice_rng.randint(1, 6)

        # Active Items
        c_item = db.get_user_active_item(challenger_id, chat_id)
        u_item = db.get_user_active_item(acceptor_id, chat_id)

        # Clear active items
        db.clear_user_active_item(challenger_id, chat_id)
        db.clear_user_active_item(acceptor_id, chat_id)

        # Process Item Interactions
        # Spray: reduces opponent dice by 1
        if c_item == "اسپری": val2 = max(1, val2 - 1)
        if u_item == "اسپری": val1 = max(1, val1 - 1)

        # Apply Dice Perks
        if c_perk == "کون‌گشاد": val1 = max(1, val1 - 1)
        elif c_perk == "زن جنده": val1 = min(6, val1 + 1)
        elif c_perk == "حروم‌دست": val1 = max(1, val1 - 2)

        if user_perk == "کون‌گشاد": val2 = max(1, val2 - 1)
        elif user_perk == "زن جنده": val2 = min(6, val2 + 1)
        elif user_perk == "حروم‌دست": val2 = max(1, val2 - 2)

        msg_item_log = ""
        if c_item or u_item:
            msg_item_log += "\n🎒 **گزارش آیتم‌ها:**\n"

        if c_item == "اسپری": msg_item_log += f"- {challenger_name} اسپری زد و تاس حریف کم شد.\n"
        if u_item == "اسپری": msg_item_log += f"- {acceptor_name} اسپری زد و تاس حریف کم شد.\n"

        # Needle pierces condom
        c_condom = True if c_item == "کاندوم" else False
        u_condom = True if u_item == "کاندوم" else False

        if c_item == "سوزن" and u_condom:
            u_condom = False
            msg_item_log += f"- {challenger_name} سوزن داشت و کاندوم {acceptor_name} پاره شد!\n"
        if u_item == "سوزن" and c_condom:
            c_condom = False
            msg_item_log += f"- {acceptor_name} سوزن داشت و کاندوم {challenger_name} پاره شد!\n"

        # Shield blocks milk
        c_milk = True if c_item == "شیر موز" else False
        u_milk = True if u_item == "شیر موز" else False

        if c_item == "طلسم" and u_milk:
            u_milk = False
            msg_item_log += f"- {challenger_name} با طلسم اثر شیر موز {acceptor_name} را باطل کرد!\n"
        if u_item == "طلسم" and c_milk:
            c_milk = False
            msg_item_log += f"- {acceptor_name} با طلسم اثر شیر موز {challenger_name} را باطل کرد!\n"

        winner_id, loser_id = None, None
        winner_name, loser_name = "", ""
        winner_perk, loser_perk = "", ""
        winner_condom, loser_condom = False, False
        winner_milk, loser_milk = False, False

        if val1 > val2:
            winner_id, loser_id = challenger_id, acceptor_id
            winner_name, loser_name = challenger_name, acceptor_name
            winner_perk, loser_perk = c_perk, user_perk
            winner_condom, loser_condom = c_condom, u_condom
            winner_milk, loser_milk = c_milk, u_milk
            msg = f"⚔️ مسابقه بین {challenger_name} و {acceptor_name}\n🎲 تاس {challenger_name}: {val1}\n🎲 تاس {acceptor_name}: {val2}\n\n🎉 {challenger_name} برنده چالش شد!"
        elif val2 > val1:
            winner_id, loser_id = acceptor_id, challenger_id
            winner_name, loser_name = acceptor_name, challenger_name
            winner_perk, loser_perk = user_perk, c_perk
            winner_condom, loser_condom = u_condom, c_condom
            winner_milk, loser_milk = u_milk, c_milk
            msg = f"⚔️ مسابقه بین {challenger_name} و {acceptor_name}\n🎲 تاس {challenger_name}: {val1}\n🎲 تاس {acceptor_name}: {val2}\n\n🎉 {acceptor_name} برنده چالش شد!"
        else:
            # Refund the escrowed main bet to both sides since nobody actually won or lost it.
            settled = True
            db.update_size(challenger_id, chat_id, bet)
            db.update_size(acceptor_id, chat_id, bet)

            msg = f"⚔️ مسابقه بین {challenger_name} و {acceptor_name}\n🎲 تاس {challenger_name}: {val1}\n🎲 تاس {acceptor_name}: {val2}\n\n🤝 مساوی شد! هیچکس چیزی از دست نداد."
            msg += msg_item_log
            if bets:
                for bettor_id, (_, amount, _) in bets.items():
                    db.update_size(bettor_id, chat_id, amount)  # refund the staked amount
                msg += "\n\n🎰 چون مساوی شد، شرط‌بندی‌های تماشاگران باطل شد و سانتی که گذاشته بودن بهشون برگشت."
            msg += "\n\nبرای ریمچ هر دو طرف باید دکمه زیر رو بزنن:"
            keyboard = [[InlineKeyboardButton("🔄 موافقم با ریمچ!", callback_data=build_rematch_data(challenger_id, acceptor_id, bet))]]
            await deliver_pvp_message(context, chat_id, message_id, msg, InlineKeyboardMarkup(keyboard), inline_message_id=inline_message_id)
            return

        winner_gain = bet
        loser_loss = bet

        if winner_perk == "کص‌کش":
            winner_gain = int(bet * 1.2)
            loser_loss = winner_gain
        elif winner_perk == "جاکش":
            winner_gain = int(bet * 0.5)

        if loser_perk == "لاشی":
            loser_loss = int(bet * 0.5)

        # The crown is a target: whoever wears it bleeds double, which is what makes
        # taking a swing at the leader worth it and stops the top spot from ossifying.
        # The winner's take doubles with it - otherwise the extra would simply vanish
        # and dethroning the king would pay no better than any other win.
        kingdom = db.get_kingdom(chat_id)
        if kingdom and kingdom[0] == loser_id:
            loser_loss *= 2
            winner_gain *= 2
            msg_item_log += f"- 👑 {loser_name} پادشاهه و دو برابر ضرر کرد (و برنده دو برابر برد)!\n"

        if winner_milk:
            extra = random.randint(5, 15)
            winner_gain += extra
            loser_loss += extra
            msg_item_log += f"- {winner_name} به لطف شیر موز {extra} سانت بیشتر دزدید!\n"

        if loser_condom:
            if random.random() < 0.5:
                loser_loss = 0
                winner_gain = 0
                msg_item_log += f"- {loser_name} کاندوم داشت و سایزش کم نشد! (برنده هم چیزی نگرفت)\n"
            else:
                msg_item_log += f"- کاندوم {loser_name} عمل نکرد (احتمال ۵۰٪)!\n"

        # Zero-sum guard: the winner only ever receives what the loser actually lost, so
        # anything that shields the loser (لاشی، کاندوم) shrinks the winner's take to
        # match instead of minting size out of thin air.
        winner_gain = min(winner_gain, loser_loss)

        # Both sides already had `bet` deducted at acceptance time (escrow). The winner gets
        # their own stake back plus their net winnings; the loser gets back whatever their
        # final loss (after perks/items) came out short of their already-staked bet.
        settled = True
        db.update_size(winner_id, chat_id, winner_gain + bet)
        db.update_size(loser_id, chat_id, bet - loser_loss)
        db.record_match_result(winner_id, loser_id, chat_id)

        wins, _ = db.get_win_loss(winner_id, chat_id)
        badges = award(winner_id, chat_id, 'win_10') if wins >= 10 else []
        w_size_now, _, _ = db.get_user(winner_id, chat_id, None, None)
        if w_size_now >= 1000:
            badges += award(winner_id, chat_id, 'first_1000')
        l_size_now, _, _ = db.get_user(loser_id, chat_id, None, None)
        loser_badges = award(loser_id, chat_id, 'rock_bottom') if l_size_now < 0 else []

        msg += f"\n💰 شرط اصلی: {bet} سانت"
        msg += msg_item_log

        if winner_perk in ["کص‌کش", "جاکش"] and winner_gain > 0:
            msg += f"\n({winner_perk} باعث شد برنده {winner_gain} سانت گیرش بیاد)"
        if loser_perk == "لاشی" and loser_loss > 0:
            msg += f"\n(لاشی باعث شد بازنده فقط {loser_loss} سانت از دست بده و برنده هم فقط همون‌قدر بگیره)"

        # Fetch new stats
        winner_size, _, _ = db.get_user(winner_id, chat_id, None, None)
        loser_size, _, _ = db.get_user(loser_id, chat_id, None, None)

        w_dname = get_dick_name(winner_size)
        l_dname = get_dick_name(loser_size)

        msg += f"\n\n📈 {w_dname} {winner_name} شد {int(winner_size)} سانتی‌متر!"
        msg += f"\n📉 {l_dname} {loser_name} شد {int(loser_size)} سانتی‌متر!"

        if bets:
            # Bettors already had their stake deducted the moment they placed the bet (see
            # place_bet_callback), so a correct guess pays back double the stake (stake + winnings)
            # and a wrong guess pays back nothing - the staked amount is simply gone.
            correct_side = "win" if winner_id == challenger_id else "lose"
            msg += "\n\n🎰 نتیجهٔ شرط‌بندی‌ها:"
            for bettor_id, (side, amount, bettor_name) in bets.items():
                if side == correct_side:
                    db.update_size(bettor_id, chat_id, amount * 2)
                    msg += f"\n✅ {bettor_name}: {int(amount)} گذاشت و {int(amount * 2)} گرفت (سود {int(amount)})"
                else:
                    msg += f"\n❌ {bettor_name}: {int(amount)} گذاشت و از دست داد"

        await deliver_pvp_message(context, chat_id, message_id, msg, inline_message_id=inline_message_id)
        await announce_achievements(context, chat_id, winner_name, badges)
        await announce_achievements(context, chat_id, loser_name, loser_badges)

        # A decided match moves size, so the crown may well have changed hands.
        old_king_name = kingdom[1] if kingdom else None
        _, new_king = refresh_king(chat_id)
        if new_king:
            await announce_coronation(context, chat_id, new_king, old_king_name)
    except Exception:
        if settled:
            # Money already moved (payout or tie-refund done); refunding again here
            # would mint size out of thin air. Log it - the failure was in the
            # post-settlement reporting, not the settlement itself.
            logging.exception(f"PvP match {match_id} settled but post-settlement reporting failed")
            return
        logging.exception(f"Failed to resolve PvP match {match_id}; refunding escrowed bets")
        db.update_size(challenger_id, chat_id, bet)
        db.update_size(acceptor_id, chat_id, bet)
        for bettor_id, (_, amount, _) in bets.items():
            db.update_size(bettor_id, chat_id, amount)
        await deliver_pvp_message(
            context, chat_id, message_id,
            f"⚠️ مشکلی در تعیین نتیجهٔ مسابقهٔ {challenger_name} و {acceptor_name} پیش اومد؛ "
            f"شرط اصلی و شرط‌های تماشاگران بهشون برگردونده شد.",
            inline_message_id=inline_message_id
        )

async def pvp_resolve_job(context: ContextTypes.DEFAULT_TYPE):
    await resolve_pvp_match(context, context.job.data["match_id"])

async def recover_stuck_pvp_matches(context: ContextTypes.DEFAULT_TYPE):
    """Runs once shortly after startup: settles any PvP match whose betting window had
    already closed before the process died mid-flight (e.g. a deploy restart landing
    right in the middle of someone's 20-second betting window), so it never gets left
    showing a dead betting message forever."""
    for match_id in db.get_stale_pending_pvp_matches(BET_WINDOW_SECONDS):
        try:
            await resolve_pvp_match(context, match_id)
        except Exception as e:
            logging.error(f"Failed to recover stuck PvP match {match_id}: {e}")

async def recover_pending_lotteries(context: ContextTypes.DEFAULT_TYPE):
    """Startup sweep for lottery draws whose midnight job never ran (a restart, an
    outage). The tickets were already paid for, so without this the pot would stay
    escrowed and the money would simply be gone."""
    for chat_id, draw_date in db.get_pending_lottery_draws(tehran_today_str()):
        try:
            await draw_lottery(context, draw_date)
        except Exception as e:
            logging.error(f"Failed to recover lottery {chat_id}/{draw_date}: {e}")


async def recover_expired_consensus(context: ContextTypes.DEFAULT_TYPE):
    """Startup sweep for consensus votes whose one-hour timeout job died with the
    process (the job queue is in-memory). Without this, a vote started before a deploy
    keeps showing live buttons forever and never resolves on its own."""
    for vote_id, chat_id, target_id, target_name in db.get_expired_open_consensus(CONSENSUS_VOTE_WINDOW_SECONDS):
        try:
            db.fail_open_consensus(chat_id, target_id, target_name)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"⏰ مهلت اجماع علیه {target_name} تموم شد و به حد نصاب نرسید؛ اجماع شکست خورد.\n"
                      f"🛡️ {target_name} تا ۳ روز در برابر اجماع جدید محافظت می‌شود.")
            )
        except Exception as e:
            logging.error(f"Failed to expire stale consensus {vote_id}: {e}")


async def place_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    data = query.data.split('_')
    if len(data) != 4 or data[0] != 'bet':
        return
    match_id, side, amount_str = data[1], data[2], data[3]
    # callback_data is client-supplied: only the amounts the bot actually offers, and
    # only the two real sides, are accepted.
    if side not in ('win', 'lose'):
        return
    try:
        amount = int(amount_str)
    except ValueError:
        return
    if amount not in BET_AMOUNTS:
        await query.answer("مبلغ شرط نامعتبره!", show_alert=True)
        return

    match = db.get_pvp_match(match_id)
    if not match or match[8] != 'pending':
        await query.answer("زمان شرط‌بندی این مسابقه تموم شده یا نامعتبره!", show_alert=True)
        return
    chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, match_bet, _message_id, _inline_message_id, _status = match

    if user.id in (challenger_id, acceptor_id):
        await query.answer("شرکت‌کننده‌های مسابقه نمی‌تونن روی مسابقه خودشون شرط ببندن!", show_alert=True)
        return

    user_size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)

    # Stake the bet immediately: deducted now, paid back double on a correct guess,
    # gone for good on a wrong one (see the settlement logic in resolve_pvp_match).
    # The deduction is an atomic check-and-take so two rapid taps can't both pass a
    # balance check first; a duplicate bet refunds the stake instead of losing it
    # to a primary-key violation like before.
    if not db.try_deduct_size(user.id, chat_id, amount):
        await query.answer(f"شما به اندازه کافی سانتی‌متر ندارید! سایز فعلی شما: {int(user_size)}", show_alert=True)
        return
    try:
        placed = db.place_pvp_bet(match_id, user.id, user.first_name, side, amount)
    except Exception:
        db.update_size(user.id, chat_id, amount)  # give the escrowed stake back
        raise
    if not placed:
        db.update_size(user.id, chat_id, amount)
        await query.answer("شما قبلاً روی این مسابقه شرط بسته‌اید!", show_alert=True)
        return
    side_fa = "برد" if side == "win" else "باخت"
    await query.answer(
        f"{amount} سانت گذاشتید روی {side_fa} {challenger_name}! "
        f"اگه درست حدس بزنید {amount * 2} سانت می‌گیرید، وگرنه همین {amount} سانت از دست میره.",
        show_alert=True
    )

    match_state = {
        "challenger_name": challenger_name,
        "acceptor_name": acceptor_name,
        "bet": int(match_bet),
        "bets": {uid: (s, a, n) for uid, s, a, n in db.get_pvp_bets(match_id)},
    }
    try:
        await query.edit_message_text(
            render_bet_message(match_state),
            reply_markup=build_bet_keyboard(match_id)
        )
    except:
        pass

# Who has agreed to a rematch, keyed by "p1_p2_bet_chat" -> {user_ids}. Entries expire
# (REMATCH_AGREEMENT_TTL) so a one-sided agreement can't sit here for days and then
# turn a single press by the other player into an instantly-started rematch.
rematch_agreements = {}
REMATCH_AGREEMENT_TTL = datetime.timedelta(minutes=10)


def record_rematch_agreement(key, user_id):
    """Adds a user's agreement and returns the set of currently-valid agreers."""
    now = datetime.datetime.now(datetime.timezone.utc)
    for k, (agreed_at, _) in list(rematch_agreements.items()):
        if now - agreed_at > REMATCH_AGREEMENT_TTL:
            del rematch_agreements[k]
    _, agreers = rematch_agreements.get(key, (now, set()))
    agreers.add(user_id)
    rematch_agreements[key] = (now, agreers)
    return agreers


def build_rematch_data(p1_id, p2_id, bet):
    payload = f"{p1_id}_{p2_id}_{int(bet)}"
    return f"rematch_{payload}_{sign_payload(payload)}"


async def rematch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ خطا!", show_alert=True)
        return

    data = query.data.split('_')
    if len(data) != 5 or data[0] != 'rematch':
        return
    if not verify_payload(f"{data[1]}_{data[2]}_{data[3]}", data[4]):
        await query.answer("این دکمه معتبر نیست!", show_alert=True)
        return

    p1_id = int(data[1])
    p2_id = int(data[2])
    bet = int(data[3])

    if user.id != p1_id and user.id != p2_id:
        await query.answer("شما عضو این چالش نیستید!", show_alert=True)
        return

    key = f"{p1_id}_{p2_id}_{bet}_{chat_id}"
    agreers = record_rematch_agreement(key, user.id)

    if len(agreers) < 2:
        await query.answer("موافقت شما ثبت شد! منتظر موافقت طرف مقابل...", show_alert=True)
        # Get names
        p1_info = db.get_user_info(p1_id, chat_id)
        p2_info = db.get_user_info(p2_id, chat_id)
        p1_name = p1_info[0] if p1_info else "نفر ۱"
        p2_name = p2_info[0] if p2_info else "نفر ۲"
        
        agreed_name = user.first_name
        waiting_name = p2_name if user.id == p1_id else p1_name
        
        msg = f"🔄 ریمچ بین {p1_name} و {p2_name} (شرط: {bet} سانت)\n\n"
        msg += f"✅ {agreed_name} موافقت کرد\n⏳ منتظر {waiting_name}..."
        keyboard = [[InlineKeyboardButton("🔄 موافقم با ریمچ!", callback_data=build_rematch_data(p1_id, p2_id, bet))]]
        try:
            await query.edit_message_text(text=msg, reply_markup=InlineKeyboardMarkup(keyboard))
        except:
            pass
        return

    # Both agreed! Re-roll!
    del rematch_agreements[key]

    p1_info = db.get_user_info(p1_id, chat_id)
    p2_info = db.get_user_info(p2_id, chat_id)
    p1_name = p1_info[0] if p1_info else "نفر ۱"
    p2_name = p2_info[0] if p2_info else "نفر ۲"

    # Stake both sides' bet immediately, same as a fresh challenge, so this can't be
    # combined with another pending challenge/rematch to over-commit past real balance.
    # Atomic check-and-take: no balance can be spent twice by concurrent stakes.
    if not db.try_deduct_size(p1_id, chat_id, bet):
        await query.answer(f"{p1_name} دیگه به اندازه کافی سایز برای این شرط نداره! ریمچ لغو شد.", show_alert=True)
        try:
            await query.edit_message_text(f"🔄 ریمچ بین {p1_name} و {p2_name} لغو شد؛ {p1_name} دیگه به اندازه کافی سایز نداره.")
        except:
            pass
        return
    if not db.try_deduct_size(p2_id, chat_id, bet):
        db.update_size(p1_id, chat_id, bet)  # hand p1's stake back
        await query.answer(f"{p2_name} دیگه به اندازه کافی سایز برای این شرط نداره! ریمچ لغو شد.", show_alert=True)
        try:
            await query.edit_message_text(f"🔄 ریمچ بین {p1_name} و {p2_name} لغو شد؛ {p2_name} دیگه به اندازه کافی سایز نداره.")
        except:
            pass
        return

    # A rematch is settled through the same persisted path as a normal challenge
    # (pvp_matches + resolve_pvp_match) instead of an in-process sleep: it now applies
    # perks and items exactly like a first match, keeps the zero-sum guard, survives a
    # restart mid-roll via recover_stuck_pvp_matches, and can't lose both stakes.
    match_id = str(uuid4())
    db.create_pvp_match(match_id, chat_id, p1_id, p1_name, p2_id, p2_name, bet)

    await query.answer("هر دو موافقت کردن! تاس‌ها دوباره ریخته میشه...")
    try:
        await query.edit_message_text(f"🔄 ریمچ بین {p1_name} و {p2_name}!\nدر حال ریختن تاس...")
    except:
        pass
    if query.message:
        db.set_pvp_match_message(match_id, message_id=query.message.message_id)
    else:
        db.set_pvp_match_message(match_id, inline_message_id=query.inline_message_id)

    context.job_queue.run_once(
        pvp_resolve_job, when=REMATCH_ROLL_SECONDS, data={"match_id": match_id}, name=f"pvp_resolve_{match_id}"
    )

async def grow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه، بعد اینلاین کار می‌کنه!", show_alert=True)
        return
        
    data = query.data.split('_')
    if len(data) != 3 or data[0] != 'grow' or data[1] != 'self':
        return
        
    target_id = int(data[2])
    if user.id != target_id:
        await query.answer("شما فقط می‌توانید دودول خودتان را رشد دهید!", show_alert=True)
        return
        
    current_size, last_grown, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    today_str = tehran_today_str()
    if last_grown == today_str:
        await query.answer("شما امروز دودول خود را در این گروه رشد داده‌اید! تا فردا صبر کنید.", show_alert=True)
        return

    # Atomically stamp today's date first: a rapid double-tap (or the same button in
    # two clients) would otherwise pass the check above twice and grow twice. The same
    # statement rolls the daily streak forward (or resets it if yesterday was missed).
    yesterday_str = (datetime.datetime.now(IRAN_TZ).date() - datetime.timedelta(days=1)).isoformat()
    streak = db.claim_daily_growth_with_streak(user.id, chat_id, today_str, yesterday_str)
    if streak is None:
        await query.answer("شما امروز دودول خود را در این گروه رشد داده‌اید! تا فردا صبر کنید.", show_alert=True)
        return

    if current_size < 50:
        delta = roll_nonzero(-5, 20)
    elif current_size < 150:
        delta = roll_nonzero(-3, 20)
    else:
        delta = roll_nonzero(-6, 10)

    # Showing up every day compounds: each consecutive day adds a centimetre on top of
    # the roll, capped so a long streak stays an edge rather than a runaway lead.
    streak_bonus = min(max(streak - 1, 0), STREAK_MAX_BONUS)
    delta += streak_bonus
    if delta == 0:
        delta = 1  # growth must always move the number - see roll_nonzero

    db.update_size(user.id, chat_id, delta)

    current_size = current_size + delta
    
    perk_pool = [
        "عادی", "عادی", "عادی", "عادی", "عادی", "عادی", "عادی",
        "جاکش", "کص‌کش", "حرومزاده", "لاشی", "خایه‌مال", "کون‌گشاد", "زن جنده", "جقی",
        "کیرکلفت", "کص‌شانس", "کیرشکسته", "کون‌سوخته", "حروم‌دست"
    ]
    new_perk = random.choice(perk_pool)
    db.set_user_perk(user.id, chat_id, new_perk)
    if new_perk == "خایه‌مال":
        current_size += 5
        db.update_size(user.id, chat_id, 5)

    perk_extra_msg = ""
    if new_perk == "کیرکلفت":
        bonus = random.randint(10, 25)
        current_size += bonus
        db.update_size(user.id, chat_id, bonus)
        perk_extra_msg = f"\n💪 علاوه بر این، {bonus} سانت اضافه هم گیرت اومد!"
    elif new_perk == "کیرشکسته":
        penalty = random.randint(10, 20)
        current_size -= penalty
        db.update_size(user.id, chat_id, -penalty)
        perk_extra_msg = f"\n💔 علاوه بر این، {penalty} سانت هم بلافاصله از دست دادی!"
    elif new_perk == "زن جنده":
        victim = db.get_random_victim(chat_id, user.id, 10)
        if victim:
            victim_id, victim_name, _ = victim
            db.update_size(victim_id, chat_id, -5)
            db.update_size(user.id, chat_id, 5)
            current_size += 5
            perk_extra_msg = f"\n😈 شانس آوردی! از **{victim_name}** به‌زور ۵ سانت گرفتی!"
        else:
            perk_extra_msg = "\n😅 دنبال قربانی گشتی ولی کسی با سایز کافی پیدا نشد!"

    drop_chance = 0.6 if new_perk == "کص‌شانس" else 0.3
    dropped_item = drop_item(user.id, chat_id, drop_chance)
    item_msg = f"\n🎁 شما یک آیتم پیدا کردید: **{dropped_item}**\n📝 توضیحات: {ITEM_DESCRIPTIONS.get(dropped_item, '')}" if dropped_item else ""

    streak_msg = ""
    if streak >= 2:
        streak_msg = f"\n🔥 استریک: {streak} روز پیاپی"
        if streak_bonus:
            streak_msg += f" (+{streak_bonus} سانت پاداش)"
        if streak == STREAK_MAX_BONUS + 1:
            streak_msg += "\n(به سقف پاداش استریک رسیدی!)"

    verb = "بزرگ شد" if delta >= 0 else "کوچک شد"
    d_name = get_dick_name(current_size)
    msg = f"🍆 {d_name} {user.first_name} {abs(delta)} سانتی‌متر {verb}!\nاندازه فعلی: {int(current_size)} سانتی‌متر.{streak_msg}\n\n✨ پرک امروز: {PERK_DESCRIPTIONS.get(new_perk, '')}{perk_extra_msg}{item_msg}"

    await query.answer(f"{d_name} شما تغییر کرد!")
    try:
        await query.edit_message_text(msg)
    except:
        pass

    earned = []
    if streak >= 7:
        earned += award(user.id, chat_id, 'streak_7')
    if current_size >= 1000:
        earned += award(user.id, chat_id, 'first_1000')
    if current_size < 0:
        earned += award(user.id, chat_id, 'rock_bottom')
    await announce_achievements(context, chat_id, user.first_name, earned)

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.inline_query.from_user
    query = update.inline_query.query.strip()

    # Telegram inline queries never carry "replying to X" context - the bot only ever
    # sees the typed query text, never which message (if any) you're replying to. So
    # targeting someone for a direct item has to be done by typing @username in the
    # inline query itself: "@dickchallengerbot @username" lists your direct items to
    # use on them, each with a confirm button that applies the effect once tapped.
    if query.startswith('@') and query[1:].split():
        target_username = query[1:].split()[0]
        last_chat = db.get_last_chat(user.id)
        results = []

        if not last_chat:
            results = [InlineQueryResultArticle(
                id=str(uuid4()),
                title="⚠️ گروه شما مشخص نیست",
                description="یا هنوز تو گروهی بازی نکردید، یا عضو چند گروه هستید",
                input_message_content=InputTextMessageContent(
                    "⚠️ نمی‌تونم مطمئن بشم منظورتون کدوم گروهه (یا هنوز تو هیچ گروهی بازی نکردید، یا عضو چند گروه هستید که ربات توشونه).\n"
                    "برای استفادهٔ مطمئن از آیتم مستقیم، داخل خودِ گروه از `/u نام_آیتم @username` استفاده کنید."
                )
            )]
        else:
            target_row = db.find_user_by_username(target_username, last_chat)
            if not target_row:
                results = [InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="❌ این فرد پیدا نشد",
                    description=f"@{target_username} تو این گروه شناخته نشده",
                    input_message_content=InputTextMessageContent(f"❌ @{target_username} تو این گروه پیدا نشد.")
                )]
            elif target_row[0] == user.id:
                results = [InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="❌ نمی‌شه رو خودت استفاده کنی",
                    description="یه نفر دیگه رو هدف بگیر",
                    input_message_content=InputTextMessageContent("❌ نمی‌تونی آیتم رو روی خودت استفاده کنی!")
                )]
            else:
                target_id, target_name, _ = target_row
                owned_direct = [(n, q) for n, q in db.get_inventory(user.id, last_chat) if n in DIRECT_ITEMS]
                if not owned_direct:
                    results = [InlineQueryResultArticle(
                        id=str(uuid4()),
                        title="🎒 شما آیتمی ندارید",
                        description="هیچ آیتم قابل‌استفاده‌ای روی این فرد ندارید",
                        input_message_content=InputTextMessageContent(f"🎒 شما هیچ آیتمی برای استفاده روی {target_name} ندارید.")
                    )]
                else:
                    for item_name, qty in owned_direct:
                        results.append(InlineQueryResultArticle(
                            id=str(uuid4()),
                            title=f"{item_name} ({qty} عدد) روی {target_name}",
                            description=ITEM_DESCRIPTIONS.get(item_name, ''),
                            input_message_content=InputTextMessageContent(
                                f"💊 {user.first_name} می‌خواد از {item_name} روی {target_name} استفاده کنه...\nبرای تایید دکمه زیر رو بزن:"
                            ),
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                f"✅ تایید استفاده از {item_name}", callback_data=f"udi_{user.id}_{target_id}_{item_name}"
                            )]])
                        ))

        await update.inline_query.answer(results, cache_time=0)
        return

    # We don't have chat_id in inline query, but buttons will resolve it on click

    bet = 10
    is_number = False
    if query.isdigit() and int(query) > 0:
        bet = int(query)
        is_number = True
        
    chal_article = InlineQueryResultArticle(
        id=str(uuid4()),
        title=f"⚔️ چالش ({bet} سانت)",
        description=f"ایجاد چالش با شرط {bet} سانتی‌متر",
        input_message_content=InputTextMessageContent(f"⚔️ {user.first_name} یک چالش با شرط {bet} سانتی‌متر ایجاد کرد!\nاولین نفری که دکمه زیر را فشار دهد وارد مسابقه می‌شود."),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بیا کیرمو بخور ⚔️", callback_data=build_challenge_data(user.id, bet))]])
    )

    # Leaderboard: render directly for the user's last active group so no extra
    # button press is needed. Fall back to a button only if we don't know the group.
    last_chat = db.get_last_chat(user.id)
    top_text = build_top_text(last_chat) if last_chat else None
    if top_text:
        top_article = InlineQueryResultArticle(
            id=str(uuid4()),
            title="🏆 برترین‌های گروه",
            description="نمایش لیدربرد این گروه",
            input_message_content=InputTextMessageContent(top_text)
        )
    else:
        top_article = InlineQueryResultArticle(
            id=str(uuid4()),
            title="🏆 برترین‌های گروه",
            description="نمایش لیدربرد این گروه",
            input_message_content=InputTextMessageContent("🏆 در حال بارگذاری لیدربرد گروه..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نمایش برترین‌ها 👁️", callback_data=f"showtop_{user.id}")]])
        )

    # Inventory: same trick - render the actual items with "استفاده از X" buttons
    # directly, so an item can be activated with one tap instead of two.
    inv_text, inv_keyboard = build_inventory_view(user.id, last_chat) if last_chat else (None, None)
    if inv_text:
        inv_article = InlineQueryResultArticle(
            id=str(uuid4()),
            title="🎒 آیتم‌های من",
            description="استفاده مستقیم از آیتم‌هات",
            input_message_content=InputTextMessageContent(inv_text),
            reply_markup=inv_keyboard
        )
    else:
        inv_article = InlineQueryResultArticle(
            id=str(uuid4()),
            title="🎒 آیتم‌های من",
            description="آیتم‌هات رو تو این گروه ببین",
            input_message_content=InputTextMessageContent(f"🎒 {user.first_name} می‌خواد کیف پولش رو چک کنه..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نمایش آیتم‌ها 👁️", callback_data=f"showinv_{user.id}")]])
        )

    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="🌱 رشد دادن دودول",
            description="سایز دودولت رو تو این گروه بزرگ کن!",
            input_message_content=InputTextMessageContent(f"🌱 {user.first_name} می‌خواد دودولش رو بماله..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بمالش تا بزرگ شه 💦", callback_data=f"grow_self_{user.id}")]])
        ),
        top_article,
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="📏 نمایش سایز من",
            description="سایز دودولت رو تو این گروه ببین",
            input_message_content=InputTextMessageContent(f"📏 {user.first_name} می‌خواد سایز دودولش رو ببینه..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نمایش سایز 👁️", callback_data=f"showsize_{user.id}")]])
        ),
        inv_article,
        chal_article,
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="🎁 اهدای سایز",
            description="مقداری از سایزت رو به کسی اهدا کن",
            input_message_content=InputTextMessageContent(
                "🎁 **اهدای سایز**\nبرای اهدا از دستور زیر در گروه استفاده کنید:\n`/dd @username مقدار`\nمثال: `/dd @ali 10`"
            )
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="❓ راهنمای بازی",
            description="لیست تمام دستورات و نحوه بازی",
            input_message_content=InputTextMessageContent(HELP_TEXT)
        )
    ]
    if is_number:
        # Show ONLY the challenge option
        results = [chal_article]
    await update.inline_query.answer(results, cache_time=0)

async def show_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return
    msg = build_top_text(chat_id)

    if not msg:
        await query.edit_message_text("هنوز هیچکس در این گروه در بازی شرکت نکرده است!")
        return

    await query.edit_message_text(msg)

async def show_size_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return
    
    data = query.data.split('_')
    target_id = int(data[1])
    if user.id != target_id:
        await query.answer("فقط خود شخص می‌تونه سایزشو ببینه!", show_alert=True)
        return
        
    # chat_id already resolved above
    current_size, _, current_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    d_name = get_dick_name(current_size)
    rank = db.get_user_rank(user.id, chat_id)
    
    msg = f"📏 سایز {d_name} {user.first_name} در این گروه:\n\n"
    msg += f"📐 اندازه: {int(current_size)} سانتی‌متر\n"
    msg += f"🏅 رتبه: {rank}\n"
    msg += f"✨ پرک امروز: {current_perk}"
    
    await query.edit_message_text(msg)

async def show_inv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return
    
    data = query.data.split('_')
    target_id = int(data[1])
    if user.id != target_id:
        await query.answer("فقط خود شخص می‌تونه آیتم‌هاشو ببینه!", show_alert=True)
        return
        
    # chat_id already resolved above
    db.get_user(user.id, chat_id, user.username, user.first_name)
    msg, reply_markup = build_inventory_view(user.id, chat_id)
    if not msg:
        await query.edit_message_text("🎒 کیف پول شما در این گروه خالی است!")
        return

    await query.edit_message_text(msg, reply_markup=reply_markup)

# ==========================================================================
# Crown, consort, theft, shop, boss, lottery, random events, achievements
# ==========================================================================

STREAK_MAX_BONUS = 10

# The crown taxes the group daily, which is what makes being #1 worth chasing - but it
# also makes its wearer the only player consensus protection doesn't cover and doubles
# what they lose in a challenge. The point is that the top spot should be contested,
# not a seat someone parks in forever.
KING_TAX_RATIO = 0.01
KING_TAX_MIN_SIZE = 20
CONSORT_TAX_SHARE = 0.30
KHIANAT_STEAL_RATIO = 0.15
TRAITOR_DAYS = 3

THEFT_COOLDOWN_SECONDS = 6 * 3600
THEFT_MIN_TARGET_SIZE = 25
THEFT_MIN_RATIO, THEFT_MAX_RATIO = 0.05, 0.15
THEFT_LUCKY_PERK_BONUS = 0.10

BOSS_SPAWN_HOUR = 20  # Tehran
BOSS_HP_PER_PLAYER = 40
BOSS_MIN_HP = 80
BOSS_REWARD_BASE = 25
BOSS_TOP_DAMAGE_BONUS = 25
BOSS_NAMES = [
    "کیرِ غول‌پیکر سیاه", "اژدهای دودولی", "شومبول‌خوارِ اعظم",
    "هیولای خایه‌دار", "کصِ کهکشانی", "دیوِ سه‌متری",
]

LOTTERY_TICKET_PRICE = 10
LOTTERY_BURN_RATIO = 0.10  # burned, so the lottery is a sink and not just a shuffle

RANDOM_EVENT_INTERVAL_SECONDS = 3 * 3600
RANDOM_EVENT_CHANCE = 0.18

ACHIEVEMENTS = {
    'first_1000': ('🏆', 'هزارتایی', 'برای اولین بار به ۱۰۰۰ سانت رسید'),
    'streak_7': ('🔥', 'هفتهٔ کامل', '۷ روز پشت سر هم دودولش رو مالید'),
    'king': ('👑', 'پادشاه', 'تاج گروه رو گرفت'),
    'consort': ('💍', 'همسر پادشاه', 'به عقد پادشاه دراومد'),
    'traitor': ('🗡️', 'خائن', 'به پادشاه خیانت کرد'),
    'thief': ('🥷', 'دزد', 'یه دزدی موفق انجام داد'),
    'robbed': ('😭', 'مالباخته', 'ازش دزدی شد'),
    'boss_slayer': ('🐉', 'اژدهاکش', 'تو کشتن باس شرکت کرد'),
    'lottery_winner': ('🎟️', 'خوش‌شانس', 'لاتاری رو برد'),
    'rock_bottom': ('💀', 'ته جدول', 'سایزش منفی شد'),
    'win_10': ('⚔️', 'ده‌برده', '۱۰ تا چالش برد'),
}


def award(user_id, chat_id, code):
    """Grants a badge and returns [(emoji, title)] the first time only, [] afterwards.
    Callers collect these and hand them to announce_achievements."""
    if code not in ACHIEVEMENTS:
        return []
    if not db.grant_achievement(user_id, chat_id, code):
        return []
    emoji, title, _ = ACHIEVEMENTS[code]
    return [(emoji, title)]


async def announce_achievements(context, chat_id, who, earned):
    if not earned:
        return
    lines = "\n".join(f"{emoji} {title}" for emoji, title in earned)
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"🏅 {who} نشان جدید گرفت:\n{lines}")
    except Exception as e:
        logging.error(f"Failed to announce achievements in {chat_id}: {e}")


def refresh_king(chat_id):
    """Recomputes who holds the crown from the current leaderboard.

    Returns (kingdom_row, new_king) where new_king is (id, name) only when the crown
    actually changed hands, so the caller can announce a coronation exactly once. The
    consort seat is emptied by db.crown_king on a change of ruler - the consort belongs
    to the throne, not to the person who was sitting on it."""
    rows = db.get_top_users_full(chat_id)
    rows = [r for r in rows if (r[2] or 0) > 0]
    if not rows:
        return db.get_kingdom(chat_id), None
    top_id, top_name = rows[0][0], rows[0][1]
    current = db.get_kingdom(chat_id)
    if current and current[0] == top_id:
        return current, None
    db.crown_king(chat_id, top_id, top_name)
    return db.get_kingdom(chat_id), (top_id, top_name)


async def announce_coronation(context, chat_id, new_king, old_king_name):
    king_id, king_name = new_king
    msg = f"👑 تاج جابه‌جا شد!\n{king_name} پادشاه جدید گروهه."
    if old_king_name:
        msg += f"\n{old_king_name} از تخت افتاد و همسرش هم از قصر انداخته شد بیرون."
    msg += (f"\n\nپادشاه روزانه {int(KING_TAX_RATIO * 100)}٪ از سایز بقیه مالیات می‌گیره،"
            f"\nولی تو چالش دو برابر ضرر می‌کنه و سپر اجماع براش کار نمی‌کنه."
            f"\nبا /hamsar می‌تونه برای خودش همسر انتخاب کنه.")
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        logging.error(f"Failed to announce coronation in {chat_id}: {e}")
    await announce_achievements(context, chat_id, king_name, award(king_id, chat_id, 'king'))


async def king_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    before = db.get_kingdom(chat_id)
    kingdom, new_king = refresh_king(chat_id)
    if new_king:
        await announce_coronation(context, chat_id, new_king, before[1] if before else None)
    if not kingdom or not kingdom[0]:
        await update.message.reply_text("هنوز هیچکس تو این گروه تاج نگرفته! اول یه کم رشد کنید.")
        return

    king_id, king_name, consort_id, consort_name, _, _ = kingdom
    info = db.get_user_info(king_id, chat_id)
    king_size = int(info[1]) if info else 0
    msg = f"👑 پادشاه گروه: {king_name} ({king_size} سانتی‌متر)\n"
    if consort_id:
        c_info = db.get_user_info(consort_id, chat_id)
        c_size = int(c_info[1]) if c_info else 0
        msg += f"💍 همسر پادشاه: {consort_name} ({c_size} سانتی‌متر)\n"
        msg += f"   └ روزانه {int(CONSORT_TAX_SHARE * 100)}٪ از مالیات پادشاه بهش می‌رسه و کسی نمی‌تونه ازش دزدی کنه.\n"
        msg += f"   └ ولی هر لحظه می‌تونه با /khianat به پادشاه خیانت کنه!\n"
    else:
        msg += "💍 همسر پادشاه: ندارد\n   └ پادشاه با `/hamsar @username` می‌تونه انتخاب کنه.\n"
    msg += (f"\n📜 قوانین تاج:\n"
            f"• روزانه {int(KING_TAX_RATIO * 100)}٪ از سایز هر بازیکن به پادشاه می‌رسه\n"
            f"• پادشاه تو چالش دو برابر ضرر می‌کنه\n"
            f"• سپر ۳ روزهٔ اجماع برای پادشاه کار نمی‌کنه")
    await update.message.reply_text(msg)


async def consort_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/hamsar @username` - the king seats a consort. Once per Tehran day, so the
    throne can't cycle partners to farm anything."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    db.get_user(user.id, chat_id, user.username, user.first_name)

    kingdom, new_king = refresh_king(chat_id)
    if new_king:
        await announce_coronation(context, chat_id, new_king, None)
    if not kingdom or kingdom[0] != user.id:
        king_name = kingdom[1] if kingdom and kingdom[1] else "کسی"
        await update.message.reply_text(f"فقط پادشاه می‌تونه همسر انتخاب کنه! الان {king_name} پادشاهه 👑")
        return

    target_id, target_name = get_target_user(update, update.message.text, chat_id)
    if not target_id:
        await update.message.reply_text("استفاده صحیح:\n/hamsar @username\nیا ریپلای روی پیام شخص و تایپ /hamsar")
        return
    if target_id == user.id:
        await update.message.reply_text("نمی‌تونی با خودت ازدواج کنی! 😐")
        return
    if db.is_traitor(target_id, chat_id):
        await update.message.reply_text(f"{target_name} خائنه! تا چند روز هیچ پادشاهی قبولش نمی‌کنه 🗡️")
        return

    today_str = tehran_today_str()
    if not db.set_consort(chat_id, user.id, target_id, target_name, today_str):
        await update.message.reply_text("امروز یه بار همسر انتخاب کردی! فردا دوباره می‌تونی.")
        return

    await update.message.reply_text(
        f"💍 پادشاه {user.first_name} رسماً {target_name} رو به همسری انتخاب کرد!\n\n"
        f"از این به بعد {int(CONSORT_TAX_SHARE * 100)}٪ از مالیات روزانهٔ پادشاه به {target_name} می‌رسه "
        f"و گارد سلطنتی جلوی دزدی ازش رو می‌گیره.\n"
        f"⚠️ ولی حواست باشه — همسرِ پادشاه هر وقت بخواد می‌تونه خیانت کنه..."
    )
    await announce_achievements(context, chat_id, target_name, award(target_id, chat_id, 'consort'))


async def divorce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/talagh` - the king dismisses the consort before they get the chance to betray."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    kingdom = db.get_kingdom(chat_id)
    if not kingdom or kingdom[0] != user.id:
        await update.message.reply_text("فقط پادشاه می‌تونه طلاق بده!")
        return
    if not kingdom[2]:
        await update.message.reply_text("تو که همسری نداری 😐")
        return
    consort_name = kingdom[3]
    if not db.clear_consort(chat_id):
        await update.message.reply_text("تو که همسری نداری 😐")
        return
    await update.message.reply_text(
        f"💔 پادشاه {user.first_name} همسرش {consort_name} رو طلاق داد و از قصر انداخت بیرون!"
    )


async def betray_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/khianat @username` - the consort defects, walking off with a slice of the
    king's size and splitting it with whoever they left him for."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    db.get_user(user.id, chat_id, user.username, user.first_name)

    kingdom = db.get_kingdom(chat_id)
    if not kingdom or kingdom[2] != user.id:
        await update.message.reply_text("تو همسر پادشاه نیستی که بخوای خیانت کنی! 😏")
        return
    king_id, king_name = kingdom[0], kingdom[1]

    lover_id, lover_name = get_target_user(update, update.message.text, chat_id)
    if not lover_id:
        await update.message.reply_text("با کی می‌خوای بری؟\n/khianat @username")
        return
    if lover_id == king_id:
        await update.message.reply_text("با خودِ پادشاه که نمی‌شه بهش خیانت کرد 😐")
        return
    if lover_id == user.id:
        await update.message.reply_text("با خودت؟ 😐")
        return

    king_info = db.get_user_info(king_id, chat_id)
    king_size = king_info[1] if king_info else 0
    loot = max(1, int(king_size * KHIANAT_STEAL_RATIO))
    # Only take what the crown actually has, so betrayal can never mint size.
    if not db.try_deduct_size(king_id, chat_id, loot):
        loot = max(0, int(king_size))
        if loot <= 0 or not db.try_deduct_size(king_id, chat_id, loot):
            await update.message.reply_text("خزانهٔ پادشاه خالیه! چیزی برای بردن نیست 😂")
            return

    traitor_cut = loot // 2
    lover_cut = loot - traitor_cut
    db.update_size(user.id, chat_id, traitor_cut)
    db.update_size(lover_id, chat_id, lover_cut)
    db.clear_consort(chat_id)
    db.mark_traitor(user.id, chat_id, TRAITOR_DAYS)

    await update.message.reply_text(
        f"🗡️💔 خیانت!\n\n"
        f"{user.first_name} رفت به {lover_name} داد و خیانت کرد!\n\n"
        f"👑 {king_name} تنها موند و {int(loot)} سانت از خزانه‌اش رفت.\n"
        f"🥷 {user.first_name}: +{int(traitor_cut)} سانت\n"
        f"😏 {lover_name}: +{int(lover_cut)} سانت\n\n"
        f"🗡️ {user.first_name} تا {TRAITOR_DAYS} روز داغ «خائن» رو داره: "
        f"هیچ پادشاهی همسرش نمی‌کنه و دزدی ازش راحت‌تره."
    )
    await announce_achievements(context, chat_id, user.first_name, award(user.id, chat_id, 'traitor'))


async def collect_king_tax(context: ContextTypes.DEFAULT_TYPE, chat_id, today_str):
    """The crown's daily income. Taxes every player who can afford it, hands the
    consort their cut, and reports the take. Claimed once per day per group."""
    kingdom, new_king = refresh_king(chat_id)
    if not kingdom or not kingdom[0]:
        return
    if not db.mark_tax_collected(chat_id, today_str):
        return
    king_id, king_name, consort_id, consort_name = kingdom[0], kingdom[1], kingdom[2], kingdom[3]

    total = 0
    payers = 0
    try:
        for uid, _name, size in db.get_taxable_players(chat_id, king_id, KING_TAX_MIN_SIZE):
            amount = max(1, int((size or 0) * KING_TAX_RATIO))
            if db.try_deduct_size(uid, chat_id, amount):
                total += amount
                payers += 1
    finally:
        # Pay out whatever was actually collected even if the loop blew up part way:
        # otherwise the players already debited would have their size destroyed rather
        # than transferred, and the day is already stamped as collected.
        if total > 0:
            consort_cut = int(total * CONSORT_TAX_SHARE) if consort_id else 0
            db.update_size(king_id, chat_id, total - consort_cut)
            if consort_cut:
                db.update_size(consort_id, chat_id, consort_cut)
    if total <= 0:
        return
    consort_cut = int(total * CONSORT_TAX_SHARE) if consort_id else 0

    msg = (f"👑 مالیات روزانهٔ سلطنتی\n\n"
           f"{king_name} از {payers} نفر مجموعاً {int(total)} سانت مالیات گرفت.")
    if consort_cut:
        msg += f"\n💍 سهم همسرش {consort_name}: {int(consort_cut)} سانت"
    msg += "\n\n(دوست نداری مالیات بدی؟ تاج رو ازش بگیر 😈)"
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        logging.error(f"Failed to announce king tax in {chat_id}: {e}")


async def steal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/dozdi @username` - attempt to rob someone. Robbing a bigger player is harder,
    a failed attempt pays a fine to the victim, and the whole thing is zero-sum."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    thief_size, thief_last_grown, thief_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    # Same gate /ejma uses: only people actually playing today can move other people's
    # size around, so a throwaway account can't be spun up purely to rob someone.
    if thief_last_grown != tehran_today_str():
        await update.message.reply_text("اول باید امروز دودولت رو بمالی (/d) بعد بری دزدی! 🥷")
        return

    target_id, target_name = get_target_user(update, update.message.text, chat_id)
    if not target_id:
        await update.message.reply_text("از کی می‌خوای بدزدی؟\n/dozdi @username\nیا ریپلای روی پیامش و تایپ /dozdi")
        return
    if target_id == user.id:
        await update.message.reply_text("از جیب خودت؟ 😐")
        return

    target_info = db.get_user_info(target_id, chat_id)
    target_size = (target_info[1] if target_info else 0) or 0
    if target_size < THEFT_MIN_TARGET_SIZE:
        await update.message.reply_text(
            f"{target_name} فقیرتر از اونیه که ازش بدزدی! (حداقل {THEFT_MIN_TARGET_SIZE} سانت لازمه)"
        )
        return

    kingdom, _ = refresh_king(chat_id)
    if kingdom and kingdom[2] == target_id:
        await update.message.reply_text(
            f"🛡️ {target_name} همسر پادشاهه و گارد سلطنتی نمی‌ذاره بهش دست بزنی!"
        )
        return

    ok, remaining = db.try_start_theft(user.id, chat_id, THEFT_COOLDOWN_SECONDS)
    if not ok:
        hours, minutes = remaining // 3600, (remaining % 3600) // 60
        await update.message.reply_text(
            f"تازه دزدی کردی! تا {hours} ساعت و {minutes} دقیقهٔ دیگه دستت بستس 🥷"
        )
        return

    # Robbing up is meant to be a long shot and robbing down easy money, so the odds
    # follow the size gap rather than being a flat coin flip.
    ratio = target_size / max(1.0, thief_size + target_size)
    chance = 0.65 - 0.4 * ratio
    if thief_perk == "کص‌شانس":
        chance += THEFT_LUCKY_PERK_BONUS
    if db.is_traitor(target_id, chat_id):
        chance += 0.20  # nobody guards a traitor
    chance = min(max(chance, 0.15), 0.75)

    loot = max(1, int(target_size * random.uniform(THEFT_MIN_RATIO, THEFT_MAX_RATIO)))

    # قفل is bought precisely for this moment: it eats one theft attempt and is spent.
    # Checked before the roll, so a lock is never wasted on an attempt that would have
    # failed anyway - the thief still burns their cooldown either way.
    if db.use_inventory(target_id, chat_id, "قفل"):
        await update.message.reply_text(
            f"🔒 {target_name} قفل داشت!\n{user.first_name} به در بسته خورد و دست خالی برگشت.\n"
            f"(قفل {target_name} مصرف شد)"
        )
        return

    if random.random() < chance:
        if not db.try_deduct_size(target_id, chat_id, loot):
            await update.message.reply_text(f"{target_name} همین الان سایزش کم شد؛ دزدی بی‌نتیجه موند!")
            return
        db.update_size(user.id, chat_id, loot)
        await update.message.reply_text(
            f"🥷 دزدی موفق!\n\n{user.first_name} زد و {int(loot)} سانت از {target_name} بالا کشید!\n"
            f"(شانس موفقیت: {int(chance * 100)}٪)"
        )
        earned = award(user.id, chat_id, 'thief')
        await announce_achievements(context, chat_id, user.first_name, earned)
        await announce_achievements(context, chat_id, target_name, award(target_id, chat_id, 'robbed'))
    else:
        fine = max(1, loot // 2)
        if db.try_deduct_size(user.id, chat_id, fine):
            db.update_size(target_id, chat_id, fine)
            await update.message.reply_text(
                f"🚨 مچ‌گیری!\n\n{user.first_name} می‌خواست از {target_name} بدزده ولی گیر افتاد "
                f"و {int(fine)} سانت غرامت داد!\n(شانس موفقیت: {int(chance * 100)}٪)"
            )
        else:
            await update.message.reply_text(
                f"🚨 {user.first_name} گیر افتاد ولی اونقدر فقیره که غرامتی هم نداشت بده 😂"
            )


def build_shop_keyboard(user_id):
    rows = []
    items = list(SHOP_PRICES.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(f"{name} — {price}", callback_data=f"buy_{user_id}_{name}")
               for name, price in items[i:i + 2]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    lines = ["🏪 **فروشگاه دودول**\n", f"💰 موجودی شما: {int(size)} سانتی‌متر\n"]
    for name, price in SHOP_PRICES.items():
        lines.append(f"• {name} — {price} سانت\n  └ {ITEM_DESCRIPTIONS.get(name, '')}")
    lines.append("\nروی دکمه بزن تا بخری 👇")
    await update.message.reply_text("\n".join(lines), reply_markup=build_shop_keyboard(user.id))


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return

    data = query.data.split('_', 2)
    if len(data) != 3 or data[0] != 'buy':
        return
    try:
        owner_id = int(data[1])
    except ValueError:
        return
    item_name = data[2]
    if user.id != owner_id:
        await query.answer("این فروشگاه مال شما نیست! خودت /shop بزن.", show_alert=True)
        return
    price = SHOP_PRICES.get(item_name)
    if price is None:
        await query.answer("این آیتم تو فروشگاه نیست!", show_alert=True)
        return

    db.get_user(user.id, chat_id, user.username, user.first_name)
    # Pay first, then hand over the goods; if the insert somehow fails the size goes back.
    if not db.try_deduct_size(user.id, chat_id, price):
        size, _, _ = db.get_user(user.id, chat_id, None, None)
        await query.answer(f"پول کافی نداری! قیمت {price} سانته، تو {int(size)} داری.", show_alert=True)
        return
    try:
        db.add_inventory(user.id, chat_id, item_name)
    except Exception:
        db.update_size(user.id, chat_id, price)
        raise

    size, _, _ = db.get_user(user.id, chat_id, None, None)
    await query.answer(f"{item_name} خریدی! 🛍️\nموجودی جدید: {int(size)} سانت", show_alert=True)


async def spawn_daily_bosses(context: ContextTypes.DEFAULT_TYPE):
    """Drops one boss per active group each evening. Co-op, unlike everything else in
    this game: the whole group chips damage in and shares the reward if it dies."""
    today_str = tehran_today_str()
    for chat_id in db.get_all_chats():
        try:
            players = db.get_active_today_count(chat_id, today_str)
            if players < 2:
                continue
            hp = max(BOSS_MIN_HP, BOSS_HP_PER_PLAYER * players)
            name = random.choice(BOSS_NAMES)
            boss_id = db.spawn_boss(chat_id, name, hp, today_str)
            if not boss_id:
                continue
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=render_boss_message(name, hp, hp, []),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚔️ حمله!", callback_data=f"bosshit_{boss_id}")]])
            )
            db.set_boss_message(boss_id, sent.message_id)
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception as e:
            logging.error(f"Failed to spawn boss in {chat_id}: {e}")


def render_boss_message(name, hp, max_hp, hits):
    filled = int(10 * hp / max_hp) if max_hp else 0
    bar = "🟩" * filled + "⬛️" * (10 - filled)
    lines = [
        f"🐉 **{name}** به گروه حمله کرد!",
        f"❤️ جون: {hp}/{max_hp}",
        bar,
        "",
        "هر نفر فقط یه بار می‌تونه بزنه. تا نیمه‌شب وقت دارید بکشیدش!",
    ]
    if hits:
        lines.append("\n⚔️ ضربه‌ها:")
        for _uid, fname, dmg in hits[:10]:
            lines.append(f"- {fname}: {dmg} دمیج")
    return "\n".join(lines)


async def boss_hit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return

    data = query.data.split('_')
    if len(data) != 2 or data[0] != 'bosshit':
        return
    try:
        boss_id = int(data[1])
    except ValueError:
        return

    boss = db.get_boss(boss_id)
    if not boss:
        await query.answer("این باس دیگه وجود نداره!", show_alert=True)
        return
    b_chat_id, name, max_hp, hp, status, message_id = boss
    if b_chat_id != chat_id:
        await query.answer("این باس مال این گروه نیست!", show_alert=True)
        return
    if status != 'alive':
        await query.answer("این باس دیگه زنده نیست!", show_alert=True)
        return

    db.get_user(user.id, chat_id, user.username, user.first_name)
    streak, _ = db.get_streak(user.id, chat_id)
    damage = _dice_rng.randint(8, 30) + min(streak, 10)

    accepted, remaining_hp = db.hit_boss(boss_id, user.id, user.first_name, damage)
    if not accepted:
        await query.answer("تو قبلاً به این باس زدی! نوبت بقیه‌ست.", show_alert=True)
        return

    await query.answer(f"⚔️ {damage} دمیج زدی!")
    hits = db.get_boss_hits(boss_id)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=render_boss_message(name, max(0, remaining_hp or 0), max_hp, hits),
            reply_markup=(InlineKeyboardMarkup([[InlineKeyboardButton("⚔️ حمله!", callback_data=f"bosshit_{boss_id}")]])
                          if (remaining_hp or 0) > 0 else None)
        )
    except Exception:
        pass

    if (remaining_hp or 0) > 0:
        return
    # Only the hit that actually brought it down pays the group out.
    if not db.claim_boss_kill(boss_id):
        return

    # Re-read the hits AFTER claiming the kill: the edit above is an await, so another
    # player's hit can land in between, and paying from the older snapshot would leave
    # them out of the split entirely.
    hits = db.get_boss_hits(boss_id)
    lines = [f"🎉 گروه **{name}** رو کشت!", "", "💰 جایزه‌ها:"]
    top_damage = max((h[2] for h in hits), default=0)
    for uid, fname, dmg in hits:
        reward = BOSS_REWARD_BASE + dmg // 2
        if dmg == top_damage:
            reward += BOSS_TOP_DAMAGE_BONUS
        db.update_size(uid, chat_id, reward)
        star = " 🏆" if dmg == top_damage else ""
        lines.append(f"• {fname}{star}: {dmg} دمیج → +{reward} سانت")
        await announce_achievements(context, chat_id, fname, award(uid, chat_id, 'boss_slayer'))
    try:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    except Exception as e:
        logging.error(f"Failed to announce boss kill in {chat_id}: {e}")


async def expire_bosses_job(context: ContextTypes.DEFAULT_TYPE):
    for boss_id, chat_id, name, message_id, max_hp, hp in db.expire_bosses():
        try:
            if message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id,
                        text=f"🐉 {name} فرار کرد!\nگروه نتونست بکشتش ({hp}/{max_hp} جون براش مونده بود)."
                    )
                except Exception:
                    pass
            await context.bot.send_message(chat_id=chat_id, text=f"🐉 {name} تا صبح فرار کرد! امشب دوباره یکی میاد.")
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception as e:
            logging.error(f"Failed to expire boss {boss_id}: {e}")


async def lottery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    db.get_user(user.id, chat_id, user.username, user.first_name)
    text, keyboard = build_lottery_view(chat_id, user.id)
    await update.message.reply_text(text, reply_markup=keyboard)


def build_lottery_view(chat_id, user_id):
    entries = db.get_lottery_entries(chat_id, tehran_today_str())
    pot = sum(t for _, _, t in entries) * LOTTERY_TICKET_PRICE
    mine = next((t for uid, _, t in entries if uid == user_id), 0)
    lines = [
        "🎟️ **لاتاری امشب**",
        f"💰 جایزه: {int(pot * (1 - LOTTERY_BURN_RATIO))} سانتی‌متر",
        f"🎫 قیمت هر بلیت: {LOTTERY_TICKET_PRICE} سانت",
        f"🎫 بلیت‌های تو: {mine}",
        "",
        "قرعه‌کشی نیمه‌شب به وقت تهران. هرچی بلیت بیشتر، شانس بیشتر.",
    ]
    if entries:
        lines.append("\n👥 شرکت‌کننده‌ها:")
        for _uid, fname, t in entries:
            lines.append(f"- {fname}: {t} بلیت")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎫 ۱ بلیت", callback_data=f"lot_{user_id}_1"),
        InlineKeyboardButton("🎫 ۵ بلیت", callback_data=f"lot_{user_id}_5"),
        InlineKeyboardButton("🎫 ۱۰ بلیت", callback_data=f"lot_{user_id}_10"),
    ]])
    return "\n".join(lines), keyboard


async def lottery_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return

    data = query.data.split('_')
    if len(data) != 3 or data[0] != 'lot':
        return
    try:
        owner_id, count = int(data[1]), int(data[2])
    except ValueError:
        return
    if user.id != owner_id:
        await query.answer("این دکمه مال شما نیست! خودت /lottery بزن.", show_alert=True)
        return
    if count not in (1, 5, 10):
        return

    cost = count * LOTTERY_TICKET_PRICE
    db.get_user(user.id, chat_id, user.username, user.first_name)
    if not db.try_deduct_size(user.id, chat_id, cost):
        size, _, _ = db.get_user(user.id, chat_id, None, None)
        await query.answer(f"پول کافی نداری! {count} بلیت {cost} سانته، تو {int(size)} داری.", show_alert=True)
        return
    try:
        db.buy_lottery_tickets(chat_id, tehran_today_str(), user.id, user.first_name, count)
    except Exception:
        db.update_size(user.id, chat_id, cost)
        raise

    await query.answer(f"{count} بلیت خریدی! 🎟️")
    text, keyboard = build_lottery_view(chat_id, user.id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception:
        pass


async def draw_lottery(context: ContextTypes.DEFAULT_TYPE, draw_date):
    """Midnight draw. claim_lottery_draw removes the day's tickets as it reads them, so
    a re-run of the midnight job can never pay a second winner from the same pot."""
    for chat_id in db.get_lottery_chats(draw_date):
        try:
            entries = db.claim_lottery_draw(chat_id, draw_date)
            if not entries:
                continue
            pot = sum(t for _, _, t in entries) * LOTTERY_TICKET_PRICE
            prize = int(pot * (1 - LOTTERY_BURN_RATIO))
            pool = []
            for uid, fname, tickets in entries:
                pool.extend([(uid, fname)] * tickets)
            if not pool or prize <= 0:
                continue
            winner_id, winner_name = _dice_rng.choice(pool)
            db.update_size(winner_id, chat_id, prize)
            odds = sum(t for uid, _, t in entries if uid == winner_id) / len(pool) * 100
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"🎟️ قرعه‌کشی لاتاری!\n\n"
                      f"🎉 برنده: {winner_name}\n"
                      f"💰 جایزه: {prize} سانتی‌متر\n"
                      f"🎲 شانسش: {odds:.0f}٪ از {len(pool)} بلیت")
            )
            await announce_achievements(context, chat_id, winner_name, award(winner_id, chat_id, 'lottery_winner'))
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception as e:
            logging.error(f"Lottery draw failed for {chat_id}: {e}")


async def random_event_job(context: ContextTypes.DEFAULT_TYPE):
    """Every few hours each group has a small chance of something happening to it.
    Besides the chaos, this is a deliberate lever on the size supply: roughly half the
    events remove size and half add it."""
    for chat_id in db.get_all_chats():
        try:
            if random.random() > RANDOM_EVENT_CHANCE:
                continue
            players = db.get_all_players(chat_id)
            players = [p for p in players if (p[2] or 0) != 0]
            if len(players) < 2:
                continue
            event = random.choice(['earthquake', 'viagra_rain', 'storm', 'treasure', 'blessing'])

            if event == 'earthquake':
                for uid, _n, size in players:
                    cut = max(1, int((size or 0) * 0.08))
                    if (size or 0) > 0:
                        db.try_deduct_size(uid, chat_id, cut)
                text = "🌍 زلزله!\nزمین لرزید و ۸٪ از سایز همه ریخت پایین."
            elif event == 'viagra_rain':
                for uid, _n, _s in players:
                    db.update_size(uid, chat_id, 15)
                text = "💊 بارون ویاگرا!\nاز آسمون ویاگرا بارید و همه ۱۵ سانت گرفتن."
            elif event == 'storm':
                kingdom = db.get_kingdom(chat_id)
                if not kingdom or not kingdom[0]:
                    continue
                info = db.get_user_info(kingdom[0], chat_id)
                k_size = (info[1] if info else 0) or 0
                cut = max(1, int(k_size * 0.12))
                if not db.try_deduct_size(kingdom[0], chat_id, cut):
                    continue
                text = f"⛈️ طوفان به قصر زد!\n👑 {kingdom[1]} ‌{int(cut)} سانت از دست داد. تاج سنگینه..."
            elif event == 'treasure':
                uid, fname, _s = random.choice(players)
                db.update_size(uid, chat_id, 60)
                text = f"💎 گنج!\n{fname} یه صندوق گنج پیدا کرد و ۶۰ سانت گرفت!"
            else:
                lucky = []
                for uid, fname, _s in players:
                    streak, _ = db.get_streak(uid, chat_id)
                    if streak >= 3:
                        db.update_size(uid, chat_id, 20)
                        lucky.append(fname)
                if not lucky:
                    continue
                text = "🔥 برکت استریک!\nهر کی ۳ روز پیاپی اومده بود ۲۰ سانت گرفت:\n" + "، ".join(lucky)

            await context.bot.send_message(chat_id=chat_id, text=text)
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception as e:
            logging.error(f"Random event failed for {chat_id}: {e}")


async def achievements_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    target_id, target_name = get_target_user(update, update.message.text, chat_id)
    if not target_id:
        target_id, target_name = user.id, user.first_name
        db.get_user(user.id, chat_id, user.username, user.first_name)

    earned = set(db.get_achievements(target_id, chat_id))
    streak, best = db.get_streak(target_id, chat_id)
    lines = [f"🏅 نشان‌های {target_name} ({len(earned)} از {len(ACHIEVEMENTS)})", ""]
    for code, (emoji, title, desc) in ACHIEVEMENTS.items():
        mark = "✅" if code in earned else "🔒"
        lines.append(f"{mark} {emoji} {title} — {desc}")
    lines.append(f"\n🔥 استریک فعلی: {streak} روز (رکورد: {best})")
    await update.message.reply_text("\n".join(lines))


if __name__ == '__main__':
    db.init_db()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    
    app.job_queue.run_daily(midnight_tasks, time=time(hour=0, minute=0, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_daily(spawn_daily_bosses, time=time(hour=BOSS_SPAWN_HOUR, minute=0, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_repeating(random_event_job, interval=RANDOM_EVENT_INTERVAL_SECONDS, first=300)
    app.job_queue.run_once(recover_stuck_pvp_matches, when=5)
    app.job_queue.run_once(recover_expired_consensus, when=7)
    app.job_queue.run_once(recover_pending_lotteries, when=9)

    app.add_error_handler(on_error)

    # Group -1 runs ahead of every real handler and never blocks them.
    app.add_handler(TypeHandler(Update, log_incoming), group=-1)

    app.add_handler(CommandHandler('start', start, filters=filters.UpdateType.MESSAGE))
    app.add_handler(CommandHandler('help', start, filters=filters.UpdateType.MESSAGE))

    app.add_handler(MessageHandler(cmd(r'^/(dick|grow|d)\b'), dick))
    app.add_handler(MessageHandler(cmd(r'^/(top|t)\b'), top))
    app.add_handler(MessageHandler(cmd(r'^/(donate|dd)\b'), donate))
    app.add_handler(MessageHandler(cmd(r'^/(challenge|c)\b'), challenge))
    app.add_handler(MessageHandler(cmd(r'^/(inv|inventory|i)\b'), inventory_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(use|u)\b'), use_item_cmd))
    app.add_handler(MessageHandler(cmd(r'^/ejma\b'), consensus_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(wr|winrate)\b'), winrate_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(king|shah)\b'), king_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(hamsar|malake)\b'), consort_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(khianat|khiyanat)\b'), betray_cmd))
    app.add_handler(MessageHandler(cmd(r'^/talagh\b'), divorce_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(dozdi|steal)\b'), steal_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(shop|forushgah)\b'), shop_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(lottery|lotari)\b'), lottery_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(ach|achievements|neshan)\b'), achievements_cmd))

    app.add_handler(CallbackQueryHandler(accept_challenge_callback, pattern=r'^chal_'))
    app.add_handler(CallbackQueryHandler(rematch_callback, pattern=r'^rematch_'))
    app.add_handler(CallbackQueryHandler(grow_callback, pattern=r'^grow_self_'))
    app.add_handler(CallbackQueryHandler(use_item_callback, pattern=r'^useitem_'))
    app.add_handler(CallbackQueryHandler(consensus_vote_callback, pattern=r'^ejmavote_'))
    app.add_handler(CallbackQueryHandler(place_bet_callback, pattern=r'^bet_'))
    app.add_handler(CallbackQueryHandler(use_direct_item_inline_callback, pattern=r'^udi_'))
    app.add_handler(CallbackQueryHandler(show_top_callback, pattern=r'^showtop_'))
    app.add_handler(CallbackQueryHandler(show_size_callback, pattern=r'^showsize_'))
    app.add_handler(CallbackQueryHandler(show_inv_callback, pattern=r'^showinv_'))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy_'))
    app.add_handler(CallbackQueryHandler(boss_hit_callback, pattern=r'^bosshit_'))
    app.add_handler(CallbackQueryHandler(lottery_buy_callback, pattern=r'^lot_'))
    
    app.add_handler(InlineQueryHandler(inline_query))
    
    print("Bot is running...")
    app.run_polling()
