import hashlib
import hmac
import logging
import os
import random
import datetime
from datetime import time
from zoneinfo import ZoneInfo
import math
import asyncio
from telegram import Update, Chat, InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import Forbidden
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, InlineQueryHandler, CallbackQueryHandler
from uuid import uuid4

IRAN_TZ = ZoneInfo("Asia/Tehran")

# Dedicated OS-entropy-backed RNG for match-deciding dice rolls, so the outcome can't
# be tied to any in-process PRNG state - each roll is drawn fresh from the OS.
_dice_rng = random.SystemRandom()

def tehran_today_str():
    """The current date (YYYY-MM-DD) in Iran time, used as the daily reset key for growth."""
    return datetime.datetime.now(IRAN_TZ).date().isoformat()

import db
import football

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
    rows = db.get_top_users(chat_id)
    if not rows:
        return None
    msg = "🏆 برترین‌های این گروه:\n\n"
    for i, (first_name, size) in enumerate(rows, 1):
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
        msg += f"{i}. {first_name} ({title}): {int(size)} سانتی‌متر\n"
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
    supplied by the client, so anything the bot *trusts* from it (a stake, a price)
    has to be signed - otherwise a patched client can send `chal_<victim>_999999`
    or a football bet at odds ×50 that the bot would honour."""
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
    "اسپری": "فعالش کن تا تاس حریفت رو تو چالش یکی کم کنی."
}

# Challenge items are "activated" for your next challenge; direct items are applied
# straight onto a target's size (need someone to target, so no plain "استفاده از X" button).
CHALLENGE_ITEMS = ["کاندوم", "شیر موز", "سوزن", "طلسم", "اسپری"]
DIRECT_ITEMS = ["ویاگرا", "قرص اورژانسی", "زعفرون"]

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! به ربات رشد دودول خوش آمدید. برای شروع /dick یا /grow را بزنید. همچنین می‌توانید مرا با @username در هر چتی منشن کنید!")

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

    remaining = db.get_consensus_protection_remaining(chat_id, target_user_id)
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

FOOTBALL_POLL_INTERVAL_SECONDS = int(os.environ.get('FOOTBALL_POLL_INTERVAL_SECONDS', '120'))
BETTING_OPENS_HOURS_BEFORE = 2

async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ('creator', 'administrator')
    except Exception:
        return False

def encode_odds(odds):
    return int(round(odds * 100))

def decode_odds(odds_cents):
    return odds_cents / 100

def football_status_label(short):
    return {
        '1H': 'نیمهٔ اول', 'HT': 'نیمه‌وقت', '2H': 'نیمهٔ دوم',
        'ET': 'وقت اضافه', 'BT': 'استراحت وقت اضافه', 'P': 'پنالتی',
    }.get(short, 'زنده')

def build_football_keyboard(market_id, home_team, away_team, odds_home, odds_away):
    rows = [
        [
            InlineKeyboardButton(f"⚽️ {home_team} {amt} (×{odds_home:.2f})", callback_data=f"fbet_{market_id}_h_{amt}_{encode_odds(odds_home)}"),
            InlineKeyboardButton(f"⚽️ {away_team} {amt} (×{odds_away:.2f})", callback_data=f"fbet_{market_id}_a_{amt}_{encode_odds(odds_away)}"),
        ]
        for amt in BET_AMOUNTS
    ]
    return InlineKeyboardMarkup(rows)

def group_football_bets(bets):
    """Merges bets placed by the same person, on the same side, at the exact same
    locked-in odds into a single combined line (summed amount) - bets at different
    odds (even by the same person) stay separate, since they're priced differently.
    Returns (user_id, first_name, side, total_amount, odds), in first-seen order."""
    groups = {}
    order = []
    for user_id, first_name, side, amount, odds in bets:
        key = (user_id, side, odds)
        if key not in groups:
            groups[key] = [user_id, first_name, side, 0.0, odds]
            order.append(key)
        groups[key][3] += amount
    return [tuple(groups[key]) for key in order]

def render_football_message(home_team, away_team, status_label, elapsed, home_score, away_score, odds_home, odds_away, bets):
    lines = [
        f"⚽️ {home_team} {home_score} - {away_score} {away_team}",
        f"🕐 {status_label}" + (f" (دقیقه {elapsed}')" if elapsed else ""),
        f"📊 ضریب زنده: {home_team} ×{odds_home:.2f}  |  {away_team} ×{odds_away:.2f}",
    ]
    if bets:
        lines.append("\n📋 شرط‌های ثبت‌شده:")
        for bettor_id, first_name, side, amount, odds in bets:
            side_name = home_team if side == 'home' else away_team
            lines.append(f"- {first_name}: {int(amount)} سانت روی {side_name} (ضریب {odds:.2f})")
    return "\n".join(lines)

async def football_bet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این قابلیت فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)

    if not await is_group_admin(context, chat_id, user.id):
        await update.message.reply_text("فقط ادمین‌های گروه می‌تونن بت فوتبالی راه بندازن!")
        return

    text = update.message.text
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or '-' not in parts[1]:
        await update.message.reply_text(
            "استفاده صحیح:\n/fbet تیم۱ - تیم۲\nمثال: /fbet اسپانیا - فرانسه"
        )
        return

    team1_text, team2_text = parts[1].split('-', 1)
    team1_text, team2_text = team1_text.strip(), team2_text.strip()
    if not team1_text or not team2_text:
        await update.message.reply_text("لطفا هر دو اسم تیم رو وارد کنید.")
        return

    await update.message.reply_text("🔎 در حال جستجوی بازی...")

    today = datetime.datetime.now(datetime.timezone.utc).date()
    fixture = None
    try:
        for offset in (0, 1):
            date_str = (today + datetime.timedelta(days=offset)).isoformat()
            fixture = await football.find_fixture(team1_text, team2_text, date_str)
            if fixture:
                break
    except football.FootballApiError as e:
        await update.message.reply_text(str(e))
        return
    except Exception as e:
        logging.error(f"Football API error: {e}")
        await update.message.reply_text("⚠️ خطا در ارتباط با سرویس اطلاعات فوتبال. بعدا دوباره امتحان کنید.")
        return

    if not fixture:
        await update.message.reply_text(
            f"❌ بازی‌ای بین «{team1_text}» و «{team2_text}» برای امروز یا فردا پیدا نشد."
        )
        return

    prior_home_prob = None
    try:
        prior_home_prob = await football.get_prematch_home_probability(fixture['fixture_id'])
    except Exception as e:
        logging.error(f"Football prematch odds error: {e}")

    kickoff_dt = None
    try:
        kickoff_dt = datetime.datetime.fromisoformat(fixture['kickoff_iso'])
    except Exception:
        pass

    db.create_football_market(
        chat_id, fixture['fixture_id'], fixture['home_team'], fixture['away_team'], user.id,
        prior_home_prob=prior_home_prob if prior_home_prob is not None else 0.5,
        kickoff_at=kickoff_dt
    )

    kickoff_str = ""
    if kickoff_dt:
        kickoff_local = kickoff_dt.astimezone(IRAN_TZ)
        kickoff_str = (
            f"\n🕐 ساعت شروع: {kickoff_local.strftime('%H:%M')} (به وقت ایران)"
            f"\n🎰 از {BETTING_OPENS_HOURS_BEFORE} ساعت قبل از شروع بازی می‌شه شرط بست."
        )

    if prior_home_prob is not None:
        opening_home, opening_away = football.compute_odds(0, 0, 0, prior_home_prob)
        odds_str = f"\n📊 ضریب اولیه (بر اساس قدرت تیم‌ها): {fixture['home_team']} ×{opening_home:.2f}  |  {fixture['away_team']} ×{opening_away:.2f}"
    else:
        odds_str = "\n📊 ضریب اولیه بازار در دسترس نبود؛ با شانس برابر (۲.۰۰ / ۲.۰۰) شروع می‌شه."

    await update.message.reply_text(
        f"✅ بازی {fixture['home_team']} - {fixture['away_team']} پیدا شد و بت‌گیری براش فعال شد!"
        f"{kickoff_str}"
        f"{odds_str}\n"
        f"⏰ به محض شروع بازی، پیام شرط‌بندی همینجا فرستاده می‌شه."
    )

async def poll_football_markets(context: ContextTypes.DEFAULT_TYPE):
    markets = db.get_active_football_markets()
    now = datetime.datetime.now(datetime.timezone.utc)
    for market_id, chat_id, fixture_id, home_team, away_team, status, halftime_announced, message_id, prior_home_prob, kickoff_at, match_started_announced in markets:
        try:
            fstatus = await football.get_fixture_status(fixture_id)
        except Exception as e:
            logging.error(f"Football poll error for fixture {fixture_id}: {e}")
            continue
        if not fstatus:
            continue

        short = fstatus['status']
        elapsed = fstatus['elapsed']
        home_score = fstatus['home_score']
        away_score = fstatus['away_score']

        # Record what we just saw: bets are priced from this stored state, not from
        # the odds baked into whatever button the user tapped.
        db.set_football_market_state(market_id, elapsed, home_score, away_score)

        if short in football.FINISHED_STATUSES:
            if home_score > away_score:
                result = 'home'
            elif away_score > home_score:
                result = 'away'
            else:
                result = 'draw'

            # Pay first, mark the market finished afterwards. Claiming the bets flips
            # them to settled atomically, so a crash mid-payout leaves the market
            # unfinished and the *unpaid* remainder is retried on the next poll,
            # instead of the old order which lost every unpaid bet forever.
            claimed = db.claim_unsettled_football_bets(market_id)
            bets = group_football_bets([(uid, name, side, amount, odds)
                                        for _bid, uid, name, side, amount, odds in claimed])
            win_lines, lose_lines = [], []
            for bettor_id, first_name, side, amount, odds in bets:
                if result == 'draw':
                    db.update_size(bettor_id, chat_id, amount)  # full refund, nobody wins a 2-way market on a draw
                elif side == result:
                    payout = amount * odds
                    db.update_size(bettor_id, chat_id, payout)
                    win_lines.append(f"✅ {first_name}: {int(amount)} سانت با ضریب {odds:.2f} بست و {int(payout)} سانت گرفت (سود {int(payout - amount)})")
                else:
                    lose_lines.append(f"❌ {first_name}: {int(amount)} سانت از دست داد")
            db.finish_football_market(market_id, result)

            winner_name = home_team if result == 'home' else (away_team if result == 'away' else None)
            msg = f"🏁 بازی {home_team} {home_score} - {away_score} {away_team} تموم شد!\n"
            msg += f"🎉 به نفع {winner_name}!" if winner_name else "🤝 مساوی شد!"
            if bets:
                msg += "\n\n📋 شرط‌های ثبت‌شده:"
                for bettor_id, first_name, side, amount, odds in bets:
                    side_name = home_team if side == 'home' else away_team
                    msg += f"\n- {first_name}: {int(amount)} سانت روی {side_name} (ضریب {odds:.2f})"
                if result == 'draw':
                    msg += "\n\n🎰 چون مساوی شد، همهٔ شرط‌ها باطل شد و سانتی که گذاشته بودن بهشون برگشت."
                else:
                    if win_lines:
                        msg += "\n\n" + "\n".join(win_lines)
                    if lose_lines:
                        msg += "\n\n" + "\n".join(lose_lines)
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg)
            except Exception as e:
                logging.error(f"Failed to send football result to {chat_id}: {e}")
            continue

        # Open the betting window either BETTING_OPENS_HOURS_BEFORE kickoff, or right
        # away if the match is already underway (e.g. the market was created late).
        if status == 'scheduled':
            should_open = short in football.LIVE_STATUSES
            if not should_open and kickoff_at:
                should_open = now >= kickoff_at - datetime.timedelta(hours=BETTING_OPENS_HOURS_BEFORE)
            if should_open:
                odds_home, odds_away = football.compute_odds(elapsed, home_score, away_score, prior_home_prob)
                bets = group_football_bets(db.get_football_bets(market_id))
                status_label = football_status_label(short) if short in football.LIVE_STATUSES else "قبل از شروع بازی"
                text = render_football_message(home_team, away_team, status_label, elapsed, home_score, away_score, odds_home, odds_away, bets)
                keyboard = build_football_keyboard(market_id, home_team, away_team, odds_home, odds_away)
                try:
                    sent = await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🎰 بت‌گیری برای بازی {home_team} - {away_team} باز شد!\n\n" + text,
                        reply_markup=keyboard
                    )
                except Exception as e:
                    # Only flip to 'live' once the buttons actually reached the group -
                    # flipping first left the market permanently live with no message
                    # (and so unbettable) whenever this send failed.
                    logging.error(f"Failed to send football betting-open message to {chat_id}; will retry next poll: {e}")
                    continue
                db.set_football_market_message(market_id, sent.message_id)
                message_id = sent.message_id
                db.set_football_market_status(market_id, 'live')
                status = 'live'

        if short in football.LIVE_STATUSES and not match_started_announced:
            db.mark_football_match_started(market_id)
            match_started_announced = True
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"⚽️ بازی {home_team} - {away_team} شروع شد!")
            except Exception as e:
                logging.error(f"Failed to send football kickoff message to {chat_id}: {e}")

        if short in football.HALFTIME_STATUSES and not halftime_announced:
            db.mark_football_halftime_announced(market_id)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏸️ نیمهٔ اول {home_team} - {away_team} تموم شد!\nنتیجه: {home_score} - {away_score}"
                )
            except Exception as e:
                logging.error(f"Failed to send halftime message to {chat_id}: {e}")

        if status == 'live' and message_id:
            odds_home, odds_away = football.compute_odds(elapsed, home_score, away_score, prior_home_prob)
            bets = group_football_bets(db.get_football_bets(market_id))
            status_label = football_status_label(short) if short in football.LIVE_STATUSES else "قبل از شروع بازی"
            text = render_football_message(home_team, away_team, status_label, elapsed, home_score, away_score, odds_home, odds_away, bets)
            keyboard = build_football_keyboard(market_id, home_team, away_team, odds_home, odds_away)
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard)
            except Exception:
                pass  # unchanged content or message too old to edit - not fatal

async def place_football_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن تا ربات گروه رو بشناسه!", show_alert=True)
        return

    data = query.data.split('_')
    if len(data) != 5 or data[0] != 'fbet':
        return
    try:
        market_id = int(data[1])
        amount = int(data[3])
    except ValueError:
        return
    if data[2] not in ('h', 'a'):
        return
    side = 'home' if data[2] == 'h' else 'away'
    # callback_data is client-supplied: honour only the stakes the bot actually offers.
    if amount not in BET_AMOUNTS:
        await query.answer("مبلغ شرط نامعتبره!", show_alert=True)
        return

    market = db.get_football_market(market_id)
    if not market:
        await query.answer("این بازار وجود نداره!", show_alert=True)
        return
    (m_chat_id, fixture_id, home_team, away_team, status, halftime_announced, result, message_id,
     created_by, prior_home_prob, kickoff_at, match_started_announced,
     last_elapsed, last_home_score, last_away_score) = market
    if status != 'live':
        await query.answer("این بازی الان قابل شرط‌بندی نیست!", show_alert=True)
        return
    # A market belongs to the group it was opened in; don't let a button forwarded (or
    # a callback forged) elsewhere settle against a different group's balances.
    if m_chat_id != chat_id:
        await query.answer("این بازار مال این گروه نیست!", show_alert=True)
        return

    # Price the bet from the server's own latest view of the match, never from the odds
    # encoded in the tapped button: those go stale between polls (a goal at 89' left a
    # ×2.00 button live for up to two minutes) and, being client-supplied, could simply
    # be edited to ×50.
    odds_home, odds_away = football.compute_odds(last_elapsed, last_home_score, last_away_score, prior_home_prob)
    odds = odds_home if side == 'home' else odds_away
    shown_odds = decode_odds(int(data[4])) if data[4].isdigit() else odds
    if abs(shown_odds - odds) > 0.005:
        await query.answer(
            f"ضریب همین الان تغییر کرد ({shown_odds:.2f} ← {odds:.2f})! دوباره بزن تا با ضریب جدید ثبت بشه.",
            show_alert=True
        )
        return

    user_size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)

    # Escrow first with an atomic check-and-take (no double-spend window), then
    # record the bet; if recording fails the stake is handed straight back.
    if not db.try_deduct_size(user.id, chat_id, amount):
        await query.answer(f"شما به اندازه کافی سایز ندارید! سایز فعلی: {int(user_size)}", show_alert=True)
        return
    try:
        db.place_football_bet(market_id, user.id, user.first_name, side, amount, odds)
    except Exception:
        db.update_size(user.id, chat_id, amount)
        raise
    side_name = home_team if side == 'home' else away_team

    prior_bets = [b for b in db.get_football_bets(market_id) if b[0] == user.id]
    total_staked = sum(b[3] for b in prior_bets)
    msg = f"{amount} سانت روی {side_name} با ضریب {odds:.2f} بستید! در صورت برد {int(amount * odds)} سانت می‌گیرید."
    if len(prior_bets) > 1:
        msg += f"\nمجموع شرط‌های شما روی این بازی تا الان: {int(total_staked)} سانت."
    await query.answer(msg, show_alert=True)

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
    # two clients) would otherwise pass the check above twice and grow twice.
    if not db.claim_daily_growth(user.id, chat_id, today_str):
        await query.answer("شما امروز دودول خود را در این گروه رشد داده‌اید! تا فردا صبر کنید.", show_alert=True)
        return

    if current_size < 50:
        delta = roll_nonzero(-5, 20)
    elif current_size < 150:
        delta = roll_nonzero(-3, 20)
    else:
        delta = roll_nonzero(-6, 10)

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

    verb = "بزرگ شد" if delta >= 0 else "کوچک شد"
    d_name = get_dick_name(current_size)
    msg = f"🍆 {d_name} {user.first_name} {abs(delta)} سانتی‌متر {verb}!\nاندازه فعلی: {int(current_size)} سانتی‌متر.\n\n✨ پرک امروز: {PERK_DESCRIPTIONS.get(new_perk, '')}{perk_extra_msg}{item_msg}"
    
    await query.answer(f"{d_name} شما تغییر کرد!")
    try:
        await query.edit_message_text(msg)
    except:
        pass

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
            input_message_content=InputTextMessageContent(
                "📖 **راهنمای بازی دودول**\n\n🌱 `/d` - رشد دادن دودول\n⚔️ `/c 10` - ایجاد چالش\n🏆 `/t` - برترین‌های گروه\n🎒 `/i` - آیتم‌های من\n💉 `/u آیتم` - استفاده از آیتم\n🎁 `/dd @user 10` - اهدای سایز\n\nیا از منوی اینلاین `@dickchallengerbot` استفاده کنید!"
            )
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

if __name__ == '__main__':
    db.init_db()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    
    app.job_queue.run_daily(midnight_reminder, time=time(hour=0, minute=0, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_repeating(poll_football_markets, interval=FOOTBALL_POLL_INTERVAL_SECONDS, first=10)
    app.job_queue.run_once(recover_stuck_pvp_matches, when=5)
    app.job_queue.run_once(recover_expired_consensus, when=7)

    app.add_error_handler(on_error)

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
    app.add_handler(MessageHandler(cmd(r'^/fbet\b'), football_bet_cmd))

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
    app.add_handler(CallbackQueryHandler(place_football_bet_callback, pattern=r'^fbet_'))
    
    app.add_handler(InlineQueryHandler(inline_query))
    
    print("Bot is running...")
    app.run_polling()
