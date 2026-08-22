import hashlib
import hmac
import html
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
import lottery
import decrees

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


# The `/` autocomplete menu Telegram shows. This is registered with setMyCommands, NOT
# derived from the handlers - so it drifts: the live menu still listed a dead `/stats`
# (no handler at all) and none of the crown/theft/shop/lottery commands, which is what
# users hit as "the / menu is full of commands that don't work". setup_commands re-pushes
# this on every startup, so a deploy always heals it. Only the canonical alias of each
# command goes here (Telegram shows one per row); the handlers still accept every alias.
BOT_COMMANDS = [
    ("d", "🌱 رشد روزانهٔ دودول"),
    ("t", "🏆 لیدربرد گروه"),
    ("c", "⚔️ چالش با شرط دلخواه — /c 50"),
    ("dd", "🎁 اهدای سایز — /dd @user 20"),
    ("i", "🎒 آیتم‌های من"),
    ("u", "💉 استفاده از آیتم — /u ویاگرا @user"),
    ("shop", "🏪 خرید آیتم با سانت"),
    ("ejma", "⚖️ رای‌گیری برای کم‌کردن سایز — /ejma @user"),
    ("dozdi", "🥷 دزدی از یکی — /dozdi @user"),
    ("king", "👑 پادشاه گروه و قوانین تاج"),
    ("hamsar", "💍 (پادشاه) انتخاب همسر — /hamsar @user"),
    ("khianat", "🗡️ (همسر پادشاه) خیانت — /khianat @user"),
    ("talagh", "💔 (پادشاه) طلاق همسر"),
    ("lottery", "🎟️ لاتاری روزانه"),
    ("bank", "🏦 بانک — سود روزانه و حساب امن"),
    ("variz", "📥 واریز به بانک — /variz 50"),
    ("bardasht", "🏧 برداشت از بانک — /bardasht 50"),
    ("sarghat", "🚨 سرقت از بانک گروه!"),
    ("nozul", "🤝 نزول دادن به یکی — /nozul @user 100 25"),
    ("vam", "🏛 وام از بانک — /vam 100"),
    ("bedehi", "📜 بدهی‌ها و طلب‌های من"),
    ("pardakht", "✅ تسویهٔ بدهی"),
    ("enteghal", "🔁 انتقال سایز به گروه دیگه (کارمزد بالا)"),
    ("etebar", "📊 اعتبارسنجی — /etebar @user (۵ سانت)"),
    ("farman", "👑 (پادشاه) فرمان امروز رو امضا کن"),
    ("eghtesad", "📊 وضعیت اقتصاد گروه"),
    ("farmanha", "📜 تاریخچهٔ فرمان‌های سلطنتی"),
    ("hokm", "🪖 (پادشاه) حکومت نظامی — انحلال اجماع"),
    ("dalghak", "🤡 دلقک‌های دربار"),
    ("ach", "🏅 نشان‌ها و استریک من"),
    ("wr", "📊 آمار برد و باخت"),
    ("help", "❓ راهنمای کامل بازی"),
]


# Telegram resolves the / menu through a precedence chain, and a list registered
# under a narrower scope (or under the viewer's exact language) wins over a broader
# one. Setting only all_group_chats/all_private_chats/default therefore fixed nothing
# for most people here: the old three-command menu was still registered under
# all_chat_administrators (which outranks all_group_chats, so every group admin kept
# seeing it) and under the en/fa/ru language variants (which outrank the
# language-agnostic list, so every Persian-language client kept seeing it too).
# So: write the canonical list to every broad scope, and explicitly delete the
# per-language overrides underneath them rather than trying to keep translations of
# them in sync.
COMMAND_SCOPE_LANGUAGES = ["en", "fa", "ru", "ar", "tr", "de", "fr", "es"]


async def setup_commands(application):
    """Push the / menu at startup so it always matches this build's handlers. Runs in
    post_init, once per process, without blocking polling."""
    from telegram import (BotCommand, BotCommandScopeAllGroupChats,
                          BotCommandScopeAllPrivateChats, BotCommandScopeAllChatAdministrators)
    cmds = [BotCommand(c, d) for c, d in BOT_COMMANDS]
    scopes = [
        None,  # default
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    ]
    cleared = 0
    try:
        for scope in scopes:
            for lang in COMMAND_SCOPE_LANGUAGES:
                try:
                    await application.bot.delete_my_commands(scope=scope, language_code=lang)
                    cleared += 1
                except Exception:
                    pass  # nothing registered for that combination - fine
            await application.bot.set_my_commands(cmds, scope=scope)
        logging.info("Bot command menu registered (%d commands, %d scopes, %d language overrides cleared)",
                     len(cmds), len(scopes), cleared)
    except Exception as e:
        logging.error("Failed to register command menu: %s", e)


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

    rows = [r for r in rows if r[0] != BOT_USER_ID]
    if not rows:
        return None
    jester_ids = {j[0] for j in db.get_jesters(chat_id)}

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
        if user_id in jester_ids:
            marks += " 🤡"
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
    active_theft = db.get_user_active_theft_item(user_id, chat_id)
    if not items and not active_item and not active_theft:
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
    if active_theft:
        msg += f"\n🥷 آیتم فعال برای دزدی بعدی: **{active_theft}**"

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return msg, reply_markup

# Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = '8802494355:AAFYiGyKph3R8wLiZoeDsELOPx07Q9ZvuVw'

# The numeric prefix of the token is the bot's own Telegram user id. The bot ends up
# with a users row of its own (anything that resolves a target registers one), which
# made it targetable like a player: it was appointed consort in a real group, and
# since it can never grow, betray or be dethroned, that parked the seat forever and
# quietly funnelled 30% of the daily tax into an account nobody controls.
BOT_USER_ID = int(TOKEN.split(':')[0])

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
    "زن جنده": "به صورت رندوم یکی از اعضای گروه رو انتخاب می‌کنه و اگه سایزش بیشتر از ۱۰ باشه ۵ سانت از اون کم می‌کنه و به تو اضافه می‌کنه (تاست هم تو چالش ۱ عدد بیشتر حساب می‌شه).",
    "جقی": "موقع چالش ممکنه عدد تاس رو رندوم به شدت بالا یا پایین ببره!",
    "کیرکلفت": "شما پرک **کیرکلفت 💪** گرفتید! (یه رشد اضافه هم بلافاصله گیرت اومد).",
    "کص‌شانس": "شما پرک **کص‌شانس 🍀** گرفتید! (امروز شانس پیدا کردن آیتم دو برابره).",
    "کیرشکسته": "شما پرک **کیرشکسته 💔** گرفتید! (یه مقدار سانت هم بلافاصله از دست دادید).",
    "کون‌سوخته": "شما پرک **کون‌سوخته 🔥** گرفتید! (امروز نمی‌تونید از هیچ آیتمی استفاده کنید).",
    "حروم‌دست": "شما پرک **حروم‌دست 🎲** گرفتید! (تاس‌های امروزتون تو چالش ۲ عدد کمتر محاسبه میشه).",
    # Theft- and lottery-facing perks. The daily perk used to matter almost only inside
    # a challenge, while most groups actually spend their day on /dozdi and /lottery -
    # so a perk roll was irrelevant to what people were really doing. These give the
    # roll teeth in the parts of the game that are actually being played.
    "جیب‌بر": "شما پرک **جیب‌بر 🧤** گرفتید! (امروز شانس دزدیتون بیشتره و غنیمت بیشتری می‌برید).",
    "شب‌رو": "شما پرک **شب‌رو 🌙** گرفتید! (امروز کول‌داون دزدی براتون نصفه).",
    "دست‌کج": "شما پرک **دست‌کج 🪤** گرفتید! (امروز شانس دزدیتون کمتره و اگه گیر بیفتید غرامت دوبرابر می‌دید).",
    "سوراخ‌جیب": "شما پرک **سوراخ‌جیب 🕳️** گرفتید! (امروز بقیه راحت‌تر و بیشتر از شما می‌دزدند).",
    "خرشانس": "شما پرک **خرشانس 🎰** گرفتید! (امروز هر بلیت لاتاری شما دوبار تو قرعه‌کشی حساب می‌شه).",
    "بدبیار": "شما پرک **بدبیار 🚫** گرفتید! (امروز نمی‌تونید بلیت لاتاری بخرید)."
}

# Perk effect tables, kept next to the descriptions so a perk's numbers and the text
# players are shown can't drift apart.
THEFT_CHANCE_PERKS = {"جیب‌بر": 0.15, "دست‌کج": -0.20}   # added to the thief's chance
THEFT_LOOT_PERKS = {"جیب‌بر": 1.25}                        # multiplies the thief's loot
VICTIM_SOFT_PERKS = {"سوراخ‌جیب": (0.20, 1.5)}             # (chance bonus, loot mult) vs this victim

ITEM_DESCRIPTIONS = {
    "ویاگرا": "بده به یکی تا ۴۰ سانت بره رو کیرش! (/use ویاگرا @username)",
    "قرص اورژانسی": "بده به یکی تا ۴۰ سانت از کیرش کم بشه! (/use قرص اورژانسی @username)",
    "زعفرون": "بده به یکی تا ۵۰ الی ۱۵۰ سانت از کیرش کم بشه! (/use زعفرون @username)",
    "کاندوم": "فعالش کن تا اگه تو چالش باختی ۵۰٪ شانس داشته باشی هیچی ازت کم نشه.",
    "شیر موز": "فعالش کن تا اگه تو چالش بردی ۵ تا ۱۵ سانت بیشتر از حریف بدزدی.",
    "سوزن": "فعالش کن تا کاندوم حریفت رو تو چالش پاره کنی.",
    "طلسم": "فعالش کن تا اثر شیر موز حریفت رو باطل کنی.",
    "اسپری": "فعالش کن تا تاس حریفت رو تو چالش یکی کم کنی.",
    "قفل": "خودکار عمل می‌کنه: جلوی یه دزدی رو می‌گیره و بعدش مصرف می‌شه.",
    "دستکش": "فعالش کن تا شانس دزدی بعدیت خیلی بیشتر بشه. (/use دستکش)",
    "کیسه": "فعالش کن تا غنیمت دزدی بعدیت دوبرابر بشه. (/use کیسه)",
    "آژیر": "خودکار عمل می‌کنه: جلوی یه دزدی رو می‌گیره و دزد رو هم جریمه می‌کنه.",
    "بلیت طلایی": "فعالش کن تا ۱۰ بلیت لاتاری رایگان برای امروز بگیری. (/use بلیت طلایی)"
}

# Challenge items are "activated" for your next challenge; direct items are applied
# straight onto a target's size (need someone to target, so no plain "استفاده از X"
# button); passive items just sit in the bag and fire on their own when relevant.
CHALLENGE_ITEMS = ["کاندوم", "شیر موز", "سوزن", "طلسم", "اسپری"]
DIRECT_ITEMS = ["ویاگرا", "قرص اورژانسی", "زعفرون"]
PASSIVE_ITEMS = ["قفل", "آژیر"]
# Theft items arm their own slot (users.active_theft_item), so arming one never
# disarms the condom someone is holding for a challenge.
THEFT_ITEMS = ["دستکش", "کیسه"]
# Applied the moment they're used rather than armed for later - there is nothing to
# wait for, the tickets land in today's pot immediately.
INSTANT_ITEMS = ["بلیت طلایی"]
THEFT_ITEM_CHANCE_BONUS = 0.25   # دستکش
THEFT_ITEM_LOOT_MULT = 2.0       # کیسه
GOLDEN_TICKET_TICKETS = 10       # بلیت طلایی
ALARM_FINE_RATIO = 0.20          # آژیر fines the thief this much of the loot they missed

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
    "دستکش": 50,
    "کیسه": 55,
    "آژیر": 70,
    "بلیت طلایی": 90,
}

# A player can only be dosed with one of these per 24h, counted on the receiving end.
# زعفرون is deliberately not included - it's the rare, expensive one, and it isn't
# what people were stacking.
DOSE_LIMITED_ITEMS = ["ویاگرا", "قرص اورژانسی"]


def claim_dose_slot(item_name, target_id, target_name, chat_id):
    """Reserves the target's daily dose slot for a limited item.

    Returns (True, None) when the item may be used, or (False, message) explaining how
    long is left. Call this BEFORE consuming the item from the giver's inventory, and
    db.release_dose() if that consume then fails - otherwise a blocked dose would still
    cost someone their item."""
    if item_name not in DOSE_LIMITED_ITEMS:
        return True, None
    ok, remaining = db.try_claim_dose(target_id, chat_id)
    if ok:
        return True, None
    hours = remaining // 3600
    minutes = (remaining % 3600) // 60
    return False, (
        f"💊 {target_name} امروز قبلاً ویاگرا یا قرص اورژانسی خورده!\n"
        f"تا {hours} ساعت و {minutes} دقیقهٔ دیگه نمی‌شه دوباره بهش داد."
    )


def apply_direct_item(item_name, target_user_id, target_name, chat_id):
    """Applies a direct item's effect to a target and returns the result message.

    Size these items take off a target is destroyed rather than transferred, so it goes
    into the treasury and comes back to the group later as bank interest."""
    if item_name == "ویاگرا":
        db.update_size(target_user_id, chat_id, 40)
        return f"شما با ویاگرا ۴۰ سانت به {target_name} اضافه کردید!"
    elif item_name == "قرص اورژانسی":
        db.update_size(target_user_id, chat_id, -40)
        db.treasury_add(chat_id, 40, note="قرص اورژانسی")
        return f"شما با قرص اورژانسی ۴۰ سانت از {target_name} کم کردید!"
    elif item_name == "زعفرون":
        loss = random.randint(50, 150)
        db.update_size(target_user_id, chat_id, -loss)
        db.treasury_add(chat_id, loss, note="زعفرون")
        return f"شما با زعفرون {loss} سانت از {target_name} کم کردید!"
    return "آیتم نامشخص."

def activate_special_item(user_id, chat_id, item_name, first_name=""):
    """Arms a theft item, or applies an instant one. Returns (ok, message).

    Shared by the inventory button and the /use command so the two entry points can't
    drift - the older challenge-item path already had two near-copies of its logic and
    they were not identical."""
    if item_name in THEFT_ITEMS:
        if db.get_user_active_theft_item(user_id, chat_id):
            return False, "شما از قبل یه آیتم دزدی فعال دارید! اول یه بار /dozdi بزنید."
        if not db.use_inventory(user_id, chat_id, item_name):
            return False, "شما این آیتم را ندارید!"
        db.set_user_active_theft_item(user_id, chat_id, item_name)
        return True, f"آیتم {item_name} فعال شد! تو دزدی بعدیت اعمال می‌شه. 🥷"

    if item_name == "بلیت طلایی":
        _, _, perk = db.get_user(user_id, chat_id, None, None)
        if perk == "بدبیار":
            return False, "امروز پرک بدبیار 🚫 داری و نمی‌تونی وارد لاتاری بشی!"
        if not db.use_inventory(user_id, chat_id, item_name):
            return False, "شما این آیتم را ندارید!"
        # paid=0: a golden ticket is odds, not prize money. See buy_lottery_tickets.
        db.buy_lottery_tickets(chat_id, tehran_today_str(), user_id, first_name,
                               GOLDEN_TICKET_TICKETS, paid=0)
        return True, f"🎫 بلیت طلایی خرج شد و {GOLDEN_TICKET_TICKETS} بلیت لاتاری برای امروز گرفتی!"

    return False, "این آیتم رو نمی‌شه اینطوری استفاده کرد."


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

    if target_user_id == BOT_USER_ID:
        return None, None  # the bot is not a player - see BOT_USER_ID

    return target_user_id, target_first_name

def drop_item(user_id, chat_id, chance=0.3):
    if random.random() > chance:
        return None
    
    # Pool weights. The theft/lottery items are in the drop table too, not shop-only:
    # /dozdi is by far the most-used verb in these groups, so the items that interact
    # with it have to be reachable by simply showing up daily, not only by saving up.
    pool = (["ویاگرا"]*24 + ["قرص اورژانسی"]*10 + ["زعفرون"]*1 + ["کاندوم"]*15
            + ["شیر موز"]*15 + ["سوزن"]*10 + ["طلسم"]*15 + ["اسپری"]*10
            + ["دستکش"]*12 + ["کیسه"]*10 + ["آژیر"]*8 + ["بلیت طلایی"]*4)
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
    "🏦 /bank — بانک: سود روزانه، امن از دزدی\n"
    "📥 /variz <مقدار> — واریز به بانک (سقف روزانه داره)\n"
    "🏧 /bardasht <مقدار> — برداشت از بانک\n"
    "🚨 /sarghat — سرقت از خزانه و سپرده‌های گروه!\n"    "🤝 /nozul @کاربر <مقدار> <درصد> — نزول دادن\n"
    "🏛 /vam <مقدار> — وام رسمی از بانک\n"
    "📜 /bedehi — بدهی‌ها و طلب‌های من\n"
    "✅ /pardakht — تسویهٔ زودتر بدهی\n"    "🔁 /enteghal <مقدار> — انتقال به گروه دیگه (کارمزد سنگین)\n"    "📊 /etebar @کاربر — اعتبارسنجی (۵ سانت)\n"    "👑 /farman — (پادشاه) فرمان روزانه\n"
    "📊 /eghtesad — تورم، خشم مردم و اهرم‌های تاج\n"
    "📜 /farmanha — تاریخچهٔ فرمان‌ها\n"    "🪖 /hokm — (پادشاه) حکومت نظامی، هر ۳ روز یک بار\n"
    "🤡 /dalghak — دلقک‌های دربار\n"
    "🎟️ /lottery — لاتاری روزانه (قرعه‌کشی نیمه‌شب)\n"
    "⚖️ تعادل خودکار: هر شب ربات از روی سود و زیان چند روز اخیر، ضریب رشد و شانس "
    "دزدی رو کم‌کم تنظیم می‌کنه تا صدرنشین‌ها بی‌رقیب نشن و عقب‌مونده‌ها جا بمونن.\n"
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
        
    if item_name in THEFT_ITEMS or item_name in INSTANT_ITEMS:
        ok, note = activate_special_item(user.id, chat_id, item_name, user.first_name)
        await query.answer(note, show_alert=True)
        if ok:
            msg, reply_markup = build_inventory_view(user.id, chat_id)
            await query.edit_message_text(msg, reply_markup=reply_markup)
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

    target_info = db.get_user_info(target_id, chat_id)
    target_name = target_info[0] if target_info else "ناشناس"

    # Claim the target's daily dose slot before spending the item, so a blocked dose
    # never costs the giver anything.
    allowed, reason = claim_dose_slot(item_name, target_id, target_name, chat_id)
    if not allowed:
        await query.answer(reason, show_alert=True)
        return

    success = db.use_inventory(user.id, chat_id, item_name)
    if not success:
        if item_name in DOSE_LIMITED_ITEMS:
            db.release_dose(target_id, chat_id)
        await query.answer("این آیتم رو دیگه ندارید!", show_alert=True)
        return

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

    if (item_name not in CHALLENGE_ITEMS and item_name not in DIRECT_ITEMS
            and item_name not in THEFT_ITEMS and item_name not in INSTANT_ITEMS):
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
        
    
    if item_name in THEFT_ITEMS or item_name in INSTANT_ITEMS:
        _ok, note = activate_special_item(user.id, chat_id, item_name, user.first_name)
        await update.message.reply_text(note)
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

        allowed, reason = claim_dose_slot(item_name, target_user_id, target_first_name, chat_id)
        if not allowed:
            await update.message.reply_text(reason)
            return

        # Consume the item BEFORE applying its effect - the other order let a race
        # (or a failed decrement) apply the effect for free.
        if not db.use_inventory(user.id, chat_id, item_name):
            if item_name in DOSE_LIMITED_ITEMS:
                db.release_dose(target_user_id, chat_id)
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

    # The waiting period applies to receiving as well as giving. Gating only the giver
    # left the obvious hole open: make a fresh account, have an established one feed it.
    target_wait = db.get_donation_wait_remaining(target_user_id, chat_id)
    if target_wait is not None:
        days = max(1, int(target_wait.total_seconds() // 86400) + 1)
        await update.message.reply_text(
            f"{target_first_name} تازه به این گروه پیوسته! تا {days} روز دیگر نمی‌شود به او سایز اهدا کرد."
        )
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

    # Consensus protection applies to everyone alike, the king included: back-to-back
    # consensus on one person is the thing the cooldown exists to stop, and being #1
    # isn't a reason to lose that. The crown still pays for itself through the daily
    # tax and the doubled challenge loss.
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
    if db.is_jester(user.id, chat_id):
        await update.message.reply_text(
            "🤡 تو دلقک درباری! پادشاه اجماع قبلیت رو منحل کرد.\n"
            "تا وقتی دلقکی نمی‌تونی اجماع راه بندازی."
        )
        return

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

    if db.is_jester(user.id, chat_id):
        await query.answer("🤡 دلقک دربار حق رأی نداره!", show_alert=True)
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
            # Shrinking someone by group vote destroys the size; it now lands in the
            # treasury so the group's collective spite pays everyone's interest.
            db.treasury_add(chat_id, amount, note="اجماع")
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
        
    # جقی deliberately does NOT touch the stake here. Its description promises a wild
    # swing on the *dice* during the challenge, and that is where it is now applied
    # (see resolve_pvp_match). Rewriting the bet instead meant the one perk players
    # were warned about did something else entirely - and, because the old roll was
    # randint(bet/2, 2*bet), it quietly inflated the average stake by 25%.
    if user_size < bet:
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
        elif c_perk == "جقی": val1 = _jaghi_swing(val1)

        if user_perk == "کون‌گشاد": val2 = max(1, val2 - 1)
        elif user_perk == "زن جنده": val2 = min(6, val2 + 1)
        elif user_perk == "حروم‌دست": val2 = max(1, val2 - 2)
        elif user_perk == "جقی": val2 = _jaghi_swing(val2)

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
            # The loser's loss comes down with the winner's take. Previously only the
            # gain was halved while the loser still paid the full bet, so half the
            # stake was quietly destroyed on every جاکش win - the mirror image of the
            # mint the zero-sum guard below exists to prevent, and just as wrong.
            winner_gain = int(bet * 0.5)
            loser_loss = winner_gain

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
        # The house takes a cut of the winnings - never of the stake, which is the
        # player's own size coming back out of escrow. Whatever a shielding perk stops
        # the winner from collecting (جاکش pays them half while the loser still pays
        # full) used to simply evaporate; it goes to the vault now too. Both are
        # transfers, so the match stays zero-sum: loser_loss leaves the loser and
        # exactly loser_loss arrives, split between the winner and the treasury.
        house_cut = int(winner_gain * fee_of(chat_id, CHALLENGE_FEE_RATIO))
        spread = int(max(0, loser_loss - winner_gain))
        winner_take = winner_gain - house_cut

        settled = True
        db.update_size(winner_id, chat_id, winner_take + bet)
        db.update_size(loser_id, chat_id, bet - loser_loss)
        if house_cut + spread > 0:
            db.treasury_add(chat_id, house_cut + spread, note="کارمزد چالش")
        db.record_match_result(winner_id, loser_id, chat_id)

        wins, _ = db.get_win_loss(winner_id, chat_id)
        badges = award(winner_id, chat_id, 'win_10') if wins >= 10 else []
        w_size_now, _, _ = db.get_user(winner_id, chat_id, None, None)
        if w_size_now >= 1000:
            badges += award(winner_id, chat_id, 'first_1000')
        l_size_now, _, _ = db.get_user(loser_id, chat_id, None, None)
        loser_badges = award(loser_id, chat_id, 'rock_bottom') if l_size_now < 0 else []

        msg += f"\n💰 شرط اصلی: {bet} سانت"
        if house_cut > 0:
            msg += f"\n🧾 کارمزد چالش ({int(CHALLENGE_FEE_RATIO*100)}٪): {house_cut} سانت → خزانهٔ بانک"
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
            # Parimutuel, not a fixed 2x. The old flat double paid every correct
            # guess out of nothing: with all the spectators on the same side - the
            # normal case, since people back the obvious favourite - the book had no
            # losing stakes to pay from and simply minted the difference. Winners now
            # split exactly what the losers staked, pro rata, so the book always
            # settles to zero no matter how one-sided it is.
            correct_side = "win" if winner_id == challenger_id else "lose"
            winning_bets = {uid: v for uid, v in bets.items() if v[0] == correct_side}
            losing_pool = sum(a for s, a, _ in bets.values() if s != correct_side)
            winning_stake = sum(a for _, a, _ in winning_bets.values())

            msg += "\n\n🎰 نتیجهٔ شرط‌بندی‌ها:"
            if not winning_bets:
                # Nobody backed the winner, so there is no one to pay the pool to.
                # Void the book and hand every stake back rather than quietly
                # destroying it - the same rule the tie branch above already follows,
                # and what a real parimutuel does when no ticket picks the winner.
                for uid, (_side, amount, bettor_name) in bets.items():
                    db.update_size(uid, chat_id, amount)
                msg += "\n↩️ هیچ‌کس برنده رو درست حدس نزده بود، پس شرط‌ها باطل شد و سانت همه برگشت."
            if winning_bets:
                # Integer split with the rounding remainder handed to the largest
                # stake, so the pool is distributed exactly and never over-paid.
                shares = {}
                for uid, (_side, amount, _n) in winning_bets.items():
                    shares[uid] = int(losing_pool * amount / winning_stake) if winning_stake else 0
                remainder = losing_pool - sum(shares.values())
                if remainder > 0 and shares:
                    top = max(shares, key=lambda u: winning_bets[u][1])
                    shares[top] += remainder
                for uid, (_side, amount, bettor_name) in winning_bets.items():
                    profit = shares.get(uid, 0)
                    db.update_size(uid, chat_id, amount + profit)
                    if profit > 0:
                        msg += f"\n✅ {bettor_name}: {int(amount)} گذاشت و {int(amount + profit)} گرفت (سود {int(profit)})"
                    else:
                        msg += f"\n➖ {bettor_name}: درست حدس زد ولی کسی مقابلش شرط نبسته بود؛ {int(amount)} سانتش برگشت"
            for uid, (side, amount, bettor_name) in bets.items():
                if side != correct_side:
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
        low, high = -5, 20
    elif current_size < 150:
        low, high = -3, 20
    else:
        low, high = -6, 10

    # Per-player growth dial (1.0 for everyone by default). It narrows the top of the
    # roll *before* the dice are thrown rather than shrinking the result afterwards:
    # post-scaling kept collapsing onto the minimum, so a throttled player saw a
    # suspicious run of identical numbers. Narrowing the range instead produces an
    # ordinary-looking spread that just happens to be lower - and whatever comes out is
    # exactly what gets credited and exactly what the player is shown. The downside
    # bound is left alone, so this only ever takes away good days.
    _, growth_mult = db.get_modifiers(user.id, chat_id)
    # Wages track prices: the group-wide dial the crown sets, and the price level itself,
    # both widen the roll. Without this, inflation would quietly impoverish everyone who
    # earns rather than only everyone who saves - which is not inflation, just a tax.
    econ = db.get_economy(chat_id)
    growth_mult *= econ[4] * max(INFLATION_PRICE_FLOOR, econ[0])
    if growth_mult != 1.0:
        high = max(1, int(round(high * growth_mult)))

    delta = roll_nonzero(low, high)

    # Showing up every day compounds: each consecutive day adds a centimetre on top of
    # the roll, capped so a long streak stays an edge rather than a runaway lead.
    streak_bonus = min(max(streak - 1, 0), STREAK_MAX_BONUS)
    if growth_mult != 1.0:
        # Scale the bonus too, or a long streak would swamp the narrowed roll.
        streak_bonus = int(round(streak_bonus * growth_mult))
    delta += streak_bonus
    if delta == 0:
        delta = 1  # growth must always move the number - see roll_nonzero

    db.update_size(user.id, chat_id, delta)

    # A jester performs for his king: part of the day's growth is skimmed as tribute.
    # Only ever taken from a positive roll - a bad day is punishment enough.
    jester_note = ""
    if delta > 0 and db.is_jester(user.id, chat_id):
        kingdom_now = db.get_kingdom(chat_id)
        if kingdom_now and kingdom_now[0] and kingdom_now[0] != user.id:
            tribute = int(delta * JESTER_TRIBUTE_RATIO)
            if tribute > 0:
                db.update_size(user.id, chat_id, -tribute)
                db.update_size(kingdom_now[0], chat_id, tribute)
                delta -= tribute
                jester_note = (f"\n🤡 دلقکِ درباری: {tribute} سانت از رشدت رفت "
                               f"تو جیب {kingdom_now[1]}!")

    current_size = current_size + delta
    
    perk_pool = [
        "عادی", "عادی", "عادی", "عادی", "عادی", "عادی", "عادی",
        "جاکش", "کص‌کش", "حرومزاده", "لاشی", "خایه‌مال", "کون‌گشاد", "زن جنده", "جقی",
        "کیرکلفت", "کص‌شانس", "کیرشکسته", "کون‌سوخته", "حروم‌دست",
        "جیب‌بر", "شب‌رو", "دست‌کج", "سوراخ‌جیب", "خرشانس", "بدبیار"
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
    msg = f"🍆 {d_name} {user.first_name} {abs(delta)} سانتی‌متر {verb}!\nاندازه فعلی: {int(current_size)} سانتی‌متر.{jester_note}{streak_msg}\n\n✨ پرک امروز: {PERK_DESCRIPTIONS.get(new_perk, '')}{perk_extra_msg}{item_msg}"

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


def _jaghi_swing(val):
    """جقی: a wild swing on the die, in whichever direction the coin lands. This is
    what the perk's description has always promised players."""
    return max(1, val - 3) if random.random() < 0.5 else min(6, val + 3)

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
# HP per active player has to sit well under the *average* damage one player deals
# (randint(8,30) + streak up to 10, so 19-29), not near the maximum. At 40 the boss
# needed every player to roll their theoretical best just to break even and was
# mathematically unkillable; at 16 the kill depends on turnout, which is the point of
# a co-op fight: ~85% if nine in ten play, ~45% at seven in ten, rarely at half.
BOSS_HP_PER_PLAYER = 16
BOSS_MIN_HP = 45
BOSS_REWARD_BASE = 25
BOSS_TOP_DAMAGE_BONUS = 25
BOSS_NAMES = [
    "کیرِ غول‌پیکر سیاه", "اژدهای دودولی", "شومبول‌خوارِ اعظم",
    "هیولای خایه‌دار", "کصِ کهکشانی", "دیوِ سه‌متری",
]

# Single source of truth lives in lottery.py, which the admin panel shares.
LOTTERY_TICKET_PRICE = lottery.TICKET_PRICE
LOTTERY_BURN_RATIO = lottery.BURN_RATIO

RANDOM_EVENT_INTERVAL_SECONDS = 3 * 3600
RANDOM_EVENT_CHANCE = 0.18

# ---------------------------------------------------------------- bank
# The bank buys safety, not size. Deposits sit outside users.size, so they are invisible
# to /dozdi, to the leaderboard and to the crown - hiding your size from thieves costs
# you your place on the table, and that trade is the entire design.
#
# Interest is paid strictly out of the group's treasury, which is filled only by real
# sinks (shop purchases, the lottery rake, /ejma, shrink items, earthquakes). An empty
# treasury simply pays nothing. The bank can never invent size.
BANK_INTEREST_RATE = 0.04          # 4% a day on your balance, if the vault can afford it
BANK_INTEREST_MAX_TREASURY_SHARE = 0.25  # never drain more than a quarter of the vault in one night
# You cannot shovel a whole balance in at once: a day's deposits are capped at a share
# of your wallet, with a floor so small players can still use the bank at all. This is
# what keeps size in circulation - and keeps /dozdi worth typing.
BANK_DAILY_DEPOSIT_RATIO = 0.30
BANK_DAILY_DEPOSIT_FLOOR = 25
BANK_MIN_DEPOSIT = 5

# A heist is the counterweight to all that safety. It is rare, it is group-wide, and it
# can fail expensively - but it reaches the deposits themselves, so no vault is ever a
# guaranteed hiding place.
HEIST_COOLDOWN_SECONDS = 20 * 3600
HEIST_MIN_VAULT = 60               # not worth cracking an empty vault
HEIST_BASE_CHANCE = 0.30
HEIST_TREASURY_RATIO = 0.50        # of the treasury on success
HEIST_DEPOSIT_RATIO = 0.15         # of every other depositor's balance on success
HEIST_FINE_RATIO = 0.25            # of the would-be loot, paid by a caught thief
HEIST_JAIL_HOURS = 12              # a caught thief also loses their next theft window

# ---------------------------------------------------------------- loans (نزول)
# Two lenders, one settlement path. /vam borrows from the group treasury at a flat
# official rate; /nozul is player-to-player usury where the lender names their own rate.
# Both are collected by the same nightly sweep and both split principal from interest in
# the ledger, so neither can be used to fool the handicap.
LOAN_TERM_DAYS = 2
LOAN_MIN_PRINCIPAL = 10
LOAN_OFFER_TTL_SECONDS = 30 * 60
# The borrower can never be lent more than they are currently worth. This is what bounds
# how deep a default can drive someone negative - without it, one loan could bury a
# player past any hope of digging out.
LOAN_MAX_PRINCIPAL_RATIO = 1.0
LOAN_MAX_BORROWER_LOANS = 2
LOAN_MAX_LENDER_LOANS = 5
# Usury rates are the lender's call, but bounded: below the floor it isn't نزول, and
# above the ceiling it stops being a deal anyone can survive.
NOZUL_MIN_RATE = 0.10
NOZUL_MAX_RATE = 1.00
# The official loan is cheap by comparison, and capped so one borrower cannot empty the
# vault that everyone else's interest is paid from.
BANK_LOAN_RATE = 0.20
BANK_LOAN_MAX_TREASURY_SHARE = 0.30

# ---------------------------------------------------------------- treasury fees
# The treasury only pays out what it takes in, so the yield depositors actually see is
# decided here. Every one of these is a *transfer* into the vault, never a deletion and
# never a mint: whatever a fee takes off one player lands in the treasury and comes back
# to the group as interest.
THEFT_FEE_RATIO = 0.10       # cut of a successful theft's loot
CHALLENGE_FEE_RATIO = 0.05   # cut of the winner's net winnings (never their own stake)
BANK_DEPOSIT_FEE_RATIO = 0.02
BANK_WITHDRAW_FEE_RATIO = 0.02

# Every group is otherwise an independent league. This is the single seam between them
# and it is priced to hurt: importing a lead you built somewhere else should cost you
# most of it, or the other group's game stops mattering.
XFER_FEE_RATIO = 0.30
XFER_COOLDOWN_SECONDS = 24 * 3600
XFER_MIN_AMOUNT = 50

# ---------------------------------------------------------------- credit scoring
# A borrower's score IS their borrowing limit: the cap is their size multiplied by
# score/100, so behaviour feeds straight back into how much money they can get hold of
# instead of sitting in a cosmetic stat. Paying on time climbs; paying late slips; being
# force-collected drops hard, and drops harder the further the collector had to reach.
CREDIT_CHECK_FEE = 5
CREDIT_MIN_FACTOR, CREDIT_MAX_FACTOR = 0.2, 1.5
# The official bank refuses bad credit outright. Loan sharks do not care - that is what
# /etebar is for, so a lender can price the risk themselves before offering.
BANK_LOAN_MIN_SCORE = 60

# ---------------------------------------------------------------- inflation
# The index is a real price level, not decoration. Everything the game charges is
# multiplied by it and everything it pays out is too, which is what makes inflation
# behave the way it does in life: flows keep pace, but STOCKS do not. A player sitting
# on a banked fortune watches it buy less every day, while anyone carrying a debt - the
# amount of which is fixed in nominal size - is quietly let off. That single asymmetry
# is the whole reason the king's choice between printing and squeezing matters to
# everyone, and it is why savers and debtors want opposite kings.
INFLATION_PRICE_FLOOR = 0.4   # never make things absurdly cheap
UNREST_REVOLT_THRESHOLD = 75  # above this, the throne starts wobbling nightly
UNREST_DAILY_COOLDOWN = 4     # anger fades a little on its own
# Three of each, every night. Offering a mixed handful let a night happen to be all
# corruption or all virtue; a fixed 3-and-3 means the king is always looking at the same
# trade - six ways to get richer or get thanked - and can never blame the draw.
DECREE_GOOD_CHOICES = 3
DECREE_BAD_CHOICES = 3
DECREE_OFFER_HOUR, DECREE_OFFER_MINUTE = 21, 30

# ---------------------------------------------------------------- martial law
# The crown's veto over mob rule. Rationed hard, because an unlimited version would make
# /ejma unusable against the one player it exists to check. The price is unrest, which
# ties it to the revolt clock - a king who dissolves every vote against his friends is
# buying each one with a slice of his own reign.
MARTIAL_COOLDOWN_SECONDS = 3 * 24 * 3600
MARTIAL_UNREST = 14
JESTER_HOURS = 24
# The jester performs for the court: this share of each daily growth roll is skimmed off
# and handed to the king while the motley is on.
JESTER_TRIBUTE_RATIO = 0.30

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
    rows = [r for r in rows if (r[2] or 0) > 0 and r[0] != BOT_USER_ID]
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

    cooldown = THEFT_COOLDOWN_SECONDS // 2 if thief_perk == "شب‌رو" else THEFT_COOLDOWN_SECONDS
    ok, remaining = db.try_start_theft(user.id, chat_id, cooldown)
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

    # The thief's own perk of the day.
    chance += THEFT_CHANCE_PERKS.get(thief_perk, 0.0)
    loot_mult = THEFT_LOOT_PERKS.get(thief_perk, 1.0)

    # ...and the victim's, which is what makes a bad perk roll something the whole
    # group can smell blood on rather than a private inconvenience.
    _, _, victim_perk = db.get_user(target_id, chat_id, None, None)
    v_chance, v_loot = VICTIM_SOFT_PERKS.get(victim_perk, (0.0, 1.0))
    chance += v_chance
    loot_mult *= v_loot

    # An armed theft item, consumed on this attempt whether or not it lands - same
    # bargain as a challenge item.
    theft_item = db.get_user_active_theft_item(user.id, chat_id)
    item_note = ""
    if theft_item:
        db.clear_user_active_theft_item(user.id, chat_id)
        if theft_item == "دستکش":
            chance += THEFT_ITEM_CHANCE_BONUS
            item_note = "\n🧤 دستکش دستت بود."
        elif theft_item == "کیسه":
            loot_mult *= THEFT_ITEM_LOOT_MULT
            item_note = "\n🎒 کیسه آورده بودی."

    chance = min(max(chance, 0.15), 0.75)

    # Per-player luck dial (1.0 for everyone by default), applied after the normal floor
    # so a throttled player can be taken below the usual 15% minimum. The bot no longer
    # publishes the success chance anywhere: with no number quoted there is nothing that
    # can disagree with reality, which is both honest and a better throttle than a
    # quoted number would be - a published percentage is exactly the thing a player can
    # check their own win/loss record against.
    theft_luck, _ = db.get_modifiers(user.id, chat_id)
    if theft_luck != 1.0:
        chance = min(max(chance * theft_luck, 0.0), 0.95)

    loot = max(1, int(target_size * random.uniform(THEFT_MIN_RATIO, THEFT_MAX_RATIO) * loot_mult))

    # قفل is bought precisely for this moment: it eats one theft attempt and is spent.
    # Checked before the roll, so a lock is never wasted on an attempt that would have
    # failed anyway - the thief still burns their cooldown either way.
    if db.use_inventory(target_id, chat_id, "آژیر"):
        fine = max(1, int(loot * ALARM_FINE_RATIO))
        if db.try_deduct_size(user.id, chat_id, fine):
            db.update_size(target_id, chat_id, fine)
            await update.message.reply_text(
                f"🚨 آژیر {target_name} به صدا در اومد!\n{user.first_name} فرار کرد ولی "
                f"{int(fine)} سانت جا گذاشت.\n(آژیر {target_name} مصرف شد){item_note}"
            )
        else:
            await update.message.reply_text(
                f"🚨 آژیر {target_name} به صدا در اومد و {user.first_name} دست خالی فرار کرد!\n"
                f"(آژیر {target_name} مصرف شد){item_note}"
            )
        return

    if db.use_inventory(target_id, chat_id, "قفل"):
        await update.message.reply_text(
            f"🔒 {target_name} قفل داشت!\n{user.first_name} به در بسته خورد و دست خالی برگشت.\n"
            f"(قفل {target_name} مصرف شد){item_note}"
        )
        return

    if random.random() < chance:
        if not db.try_deduct_size(target_id, chat_id, loot):
            await update.message.reply_text(f"{target_name} همین الان سایزش کم شد؛ دزدی بی‌نتیجه موند!")
            return
        # The vault takes its cut. Stolen size stays inside the group either way, but a
        # slice of it now funds everyone's deposit interest instead of all landing on
        # the thief.
        theft_fee = int(loot * fee_of(chat_id, THEFT_FEE_RATIO))
        db.update_size(user.id, chat_id, loot - theft_fee)
        if theft_fee > 0:
            db.treasury_add(chat_id, theft_fee, note="کارمزد دزدی")
        await update.message.reply_text(
            f"🥷 دزدی موفق!\n\n{user.first_name} زد و {int(loot)} سانت از {target_name} بالا کشید!{item_note}"
            + (f"\n🧾 کارمزد دزدی ({int(THEFT_FEE_RATIO*100)}٪): {theft_fee} سانت رفت تو خزانه."
               if theft_fee > 0 else "")
        )
        earned = award(user.id, chat_id, 'thief')
        await announce_achievements(context, chat_id, user.first_name, earned)
        await announce_achievements(context, chat_id, target_name, award(target_id, chat_id, 'robbed'))
    else:
        fine = max(1, loot // 2)
        if thief_perk == "دست‌کج":
            fine *= 2
        if db.try_deduct_size(user.id, chat_id, fine):
            db.update_size(target_id, chat_id, fine)
            await update.message.reply_text(
                f"🚨 مچ‌گیری!\n\n{user.first_name} می‌خواست از {target_name} بدزده ولی گیر افتاد "
                f"و {int(fine)} سانت غرامت داد!{item_note}"
            )
        else:
            await update.message.reply_text(
                f"🚨 {user.first_name} گیر افتاد ولی اونقدر فقیره که غرامتی هم نداشت بده 😂{item_note}"
            )



# ---------------------------------------------------------------- auto-handicap
#
# The problem this solves, measured from the ledger rather than guessed at: in the
# busiest group the top three players had captured 82% of every centimetre gained,
# and the leader sat at 7x the median. Nothing in the game pushed back on a runaway -
# a big balance made the next PvP stake safer to cover, a bigger stake won more, and
# the daily growth roll was the same 20cm ceiling for the player on 722 as for the
# player on 50.
#
# Rather than nerfing anyone by hand, this reads how much each player actually *gained*
# over the recent window and leans on the two dials that already exist: growth_mult
# (the daily roll's ceiling) and theft_luck (the /dozdi success chance). Whoever is
# running away gets a slightly lower ceiling, whoever is being left behind gets a
# slightly better one. Nobody is ever pushed outside HANDICAP_* bounds, the pull is
# proportional to how far from the group's median they are, and every decision is
# written to rebalance_log so "why did my growth drop?" has a real answer.
#
# Deliberate design choices:
#  - It reads *recent gains*, not balance. A player sitting on size they earned last
#    week is not the one currently running away with the game.
#  - The median, not the mean, is the reference point: one whale would otherwise drag
#    the average up and hand the whole group a catch-up bonus.
#  - Dials move a fraction of the way to their target each night rather than snapping,
#    so a single loud day doesn't swing someone's whole week.
#  - Players whose dials an owner pinned by hand (/setgrowth, /setluck) are skipped.
HANDICAP_WINDOW_DAYS = 3
HANDICAP_MIN_PLAYERS = 4        # below this a "median" is noise, so leave the group alone
HANDICAP_GROWTH_RANGE = (0.70, 1.35)
HANDICAP_LUCK_RANGE = (0.80, 1.25)
HANDICAP_SMOOTHING = 0.5        # how far a dial travels toward its target each night


def _handicap_targets(net, median, spread):
    """The dials this player's recent net earns them. Symmetric around the median: at
    the median both come out 1.0, and the further above it they are the lower they go."""
    if spread <= 0:
        return 1.0, 1.0
    z = (net - median) / spread
    z = max(-2.0, min(2.0, z))
    growth = 1.0 - 0.18 * z
    luck = 1.0 - 0.12 * z
    growth = max(HANDICAP_GROWTH_RANGE[0], min(HANDICAP_GROWTH_RANGE[1], growth))
    luck = max(HANDICAP_LUCK_RANGE[0], min(HANDICAP_LUCK_RANGE[1], luck))
    return growth, luck


async def auto_handicap_job(context: ContextTypes.DEFAULT_TYPE):
    """Nightly: re-derive every active player's dials from the recent ledger."""
    run_date = tehran_today_str()
    for chat_id in db.get_all_chats():
        try:
            rows = db.get_recent_net_by_user(chat_id, HANDICAP_WINDOW_DAYS)
            rows = [r for r in rows if r[0] != BOT_USER_ID]
            if len(rows) < HANDICAP_MIN_PLAYERS:
                continue

            nets = sorted(float(r[2] or 0) for r in rows)
            mid = len(nets) // 2
            median = nets[mid] if len(nets) % 2 else (nets[mid - 1] + nets[mid]) / 2.0
            # Mean absolute deviation from the median: a spread measure that one
            # runaway player can't inflate the way a standard deviation would.
            spread = sum(abs(n - median) for n in nets) / len(nets)
            if spread <= 0:
                continue

            for user_id, _name, net, _events in rows:
                if db.is_dials_locked(user_id, chat_id):
                    continue
                luck_before, growth_before = db.get_modifiers(user_id, chat_id)
                t_growth, t_luck = _handicap_targets(float(net or 0), median, spread)
                growth_after = round(growth_before + (t_growth - growth_before) * HANDICAP_SMOOTHING, 3)
                luck_after = round(luck_before + (t_luck - luck_before) * HANDICAP_SMOOTHING, 3)
                if abs(growth_after - growth_before) < 0.01 and abs(luck_after - luck_before) < 0.01:
                    continue
                db.set_modifier(user_id, chat_id, 'growth_mult', growth_after)
                db.set_modifier(user_id, chat_id, 'theft_luck', luck_after)
                db.record_rebalance(chat_id, user_id, run_date, float(net or 0), median,
                                    growth_before, growth_after, luck_before, luck_after)
        except Exception:
            logging.exception(f"auto-handicap failed for {chat_id}")


# ---------------------------------------------------------------- bank commands

def _bank_daily_cap(wallet_size):
    """A day's deposit allowance: a share of what you're holding, never below the floor
    (so someone with 12 sanet can still open an account) and never more than you have."""
    return max(BANK_DAILY_DEPOSIT_FLOOR, int(wallet_size * BANK_DAILY_DEPOSIT_RATIO))


def _parse_amount(parts, wallet_size, bank_balance, mode):
    """Reads the amount argument. Accepts a plain number or 'همه' / 'all' / 'max',
    which resolves against whichever balance the command is moving size out of."""
    if len(parts) < 2:
        return None
    raw = parts[1].strip()
    if raw in ('همه', 'all', 'max', 'هرچی'):
        return int(wallet_size if mode == 'deposit' else bank_balance)
    try:
        val = int(float(raw))
    except ValueError:
        return None
    return val if val > 0 else None


async def bank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/bank` - your account, the group vault, and what today's interest looked like."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("بانک فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    wallet, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    balance, dep_date, dep_today = db.get_bank(user.id, chat_id)
    if dep_date != tehran_today_str():
        dep_today = 0.0
    treasury, _, _ = db.get_treasury(chat_id)
    total_dep, holders = db.get_bank_totals(chat_id)
    cap = _bank_daily_cap(wallet)
    remaining = max(0, cap - int(dep_today))

    msg = (
        f"🏦 <b>بانک دودول</b>\n\n"
        f"👤 حساب {_esc(user.first_name)}\n"
        f"   💼 جیب (قابل دزدیدن): {int(wallet)} سانت\n"
        f"   🔒 بانک (امن از دزدی): {int(balance)} سانت\n"
        f"   📥 سقف واریز امروز: {remaining} از {cap} سانت\n\n"
        f"🏛 صندوق گروه\n"
        f"   💰 خزانه: {int(treasury)} سانت\n"
        f"   🧾 کل سپرده‌ها: {int(total_dep)} سانت از {holders} نفر\n\n"
        f"📈 سود روزانه: {int(BANK_INTEREST_RATE*100)}٪ — ولی فقط تا جایی که خزانه بکشه.\n"
        f"⚠️ سایزِ بانک تو لیدربرد و تاج حساب نمی‌شه.\n"
        f"🥷 خزانه و سپرده‌ها با /sarghat قابل سرقتن!\n\n"
        f"دستورها: /variz &lt;مقدار&gt; • /bardasht &lt;مقدار&gt; • /sarghat"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/variz <amount>` - move size from the wallet into the bank, up to today's cap."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("بانک فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    wallet, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    balance, dep_date, dep_today = db.get_bank(user.id, chat_id)
    if dep_date != tehran_today_str():
        dep_today = 0.0
    cap = _bank_daily_cap(wallet)
    remaining = max(0, cap - int(dep_today))

    amount = _parse_amount(update.message.text.split(), wallet, balance, 'deposit')
    if amount is None:
        await update.message.reply_text(
            f"چقدر می‌خوای بریزی تو بانک؟\n/variz 50\n\n"
            f"💼 جیبت: {int(wallet)} سانت\n📥 امروز تا {remaining} سانت می‌تونی بریزی."
        )
        return
    if amount < BANK_MIN_DEPOSIT:
        await update.message.reply_text(f"حداقل واریز {BANK_MIN_DEPOSIT} سانته.")
        return
    # Clamp to the allowance instead of rejecting, so "/variz همه" does the sensible thing.
    if amount > remaining:
        amount = remaining
    if amount < BANK_MIN_DEPOSIT:
        await update.message.reply_text(
            f"سقف واریز امروزت پر شده! 📥\nفردا دوباره تا {cap} سانت می‌تونی بریزی.\n"
            f"(یه‌جا نمی‌شه همه‌چیو ریخت تو بانک — بخشیش باید تو جیبت بمونه.)"
        )
        return

    ok, res, used, fee = db.bank_deposit(user.id, chat_id, amount, tehran_today_str(), cap,
                                         fee_of(chat_id, BANK_DEPOSIT_FEE_RATIO))
    if not ok:
        if res == 'cap':
            await update.message.reply_text(f"سقف واریز امروزت پر شده! فردا دوباره تا {cap} سانت.")
        else:
            await update.message.reply_text(f"جیبت این‌قدر سانت نداره! 💼 {int(wallet)} سانت داری.")
        return
    wallet_now, _, _ = db.get_user(user.id, chat_id, None, None)
    await update.message.reply_text(
        f"🏦 {int(amount)} سانت ریختی تو بانک.\n\n"
        f"🧾 کارمزد واریز ({int(BANK_DEPOSIT_FEE_RATIO*100)}٪): {int(fee)} سانت → خزانه\n"
        f"🔒 موجودی بانک: {int(res)} سانت\n💼 جیب: {int(wallet_now)} سانت\n"
        f"📥 سقف باقی‌مونده امروز: {max(0, cap - int(used))} سانت\n\n"
        f"از دزدی امنه، ولی تو لیدربرد حساب نمی‌شه."
    )


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/bardasht <amount>` - pull size back out of the bank into the wallet."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("بانک فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    wallet, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    balance, _, _ = db.get_bank(user.id, chat_id)

    amount = _parse_amount(update.message.text.split(), wallet, balance, 'withdraw')
    if amount is None:
        await update.message.reply_text(
            f"چقدر برداریم؟\n/bardasht 50  یا  /bardasht همه\n\n🔒 موجودی بانکت: {int(balance)} سانت"
        )
        return
    if balance <= 0:
        await update.message.reply_text("چیزی تو بانک نداری! 🏦")
        return
    if amount > balance:
        amount = int(balance)

    ok, new_balance, paid_out, fee = db.bank_withdraw(user.id, chat_id, amount,
                                                      fee_of(chat_id, BANK_WITHDRAW_FEE_RATIO))
    if not ok:
        await update.message.reply_text("موجودی بانکت کافی نیست!")
        return
    wallet_now, _, _ = db.get_user(user.id, chat_id, None, None)
    await update.message.reply_text(
        f"🏧 {int(amount)} سانت از بانک برداشتی.\n\n"
        f"🧾 کارمزد برداشت ({int(BANK_WITHDRAW_FEE_RATIO*100)}٪): {int(fee)} سانت → خزانه\n"
        f"💵 به جیبت رسید: {int(paid_out)} سانت\n"
        f"🔒 بانک: {int(new_balance)} سانت\n💼 جیب: {int(wallet_now)} سانت\n\n"
        f"حالا دوباره تو لیدربرد حساب می‌شه — ولی قابل دزدیدن هم هست!"
    )


async def heist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/sarghat` - try to crack the group vault. Hits the treasury AND everyone's
    deposits, which is what stops the bank being a risk-free hiding place.

    Deliberately hostile: one attempt per group per cooldown (not per player), a real
    chance of failure, and a fine for getting caught. Strictly zero-sum either way."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("سرقت از بانک فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    thief_size, thief_last_grown, thief_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    # Same gate as /dozdi: you have to actually be playing today to move other people's size.
    if thief_last_grown != tehran_today_str():
        await update.message.reply_text("اول امروز /d بزن بعد برو سراغ بانک! 🥷")
        return

    treasury, _, _ = db.get_treasury(chat_id)
    total_dep, holders = db.get_bank_totals(chat_id)
    # What's actually stealable: the treasury plus everyone else's deposits.
    others = max(0.0, total_dep - db.get_bank(user.id, chat_id)[0])
    vault = treasury + others
    if vault < HEIST_MIN_VAULT:
        await update.message.reply_text(
            f"🏦 صندوق تقریباً خالیه ({int(vault)} سانت) — ارزش سرقت نداره.\n"
            f"(حداقل {HEIST_MIN_VAULT} سانت باید توش باشه)"
        )
        return

    ok, remaining = db.try_start_heist(chat_id, HEIST_COOLDOWN_SECONDS)
    if not ok:
        hours, minutes = remaining // 3600, (remaining % 3600) // 60
        await update.message.reply_text(
            f"🚨 بانک هنوز تو حالت آماده‌باشه!\nتا {hours} ساعت و {minutes} دقیقهٔ دیگه کسی نمی‌تونه بزنه بهش."
        )
        return

    # Robbing a fat vault is harder - the more there is to protect, the better guarded
    # it is. Luck dials and the lucky perk apply, same as ordinary theft.
    chance = HEIST_BASE_CHANCE
    if thief_perk == "کص‌شانس":
        chance += THEFT_LUCKY_PERK_BONUS
    theft_luck, _ = db.get_modifiers(user.id, chat_id)
    if theft_luck != 1.0:
        chance *= theft_luck
    chance = min(max(chance, 0.05), 0.70)

    would_be = vault * (HEIST_TREASURY_RATIO if treasury else HEIST_DEPOSIT_RATIO)

    if random.random() < chance:
        total, treasury_part, victims = db.heist_take(
            chat_id, user.id, HEIST_TREASURY_RATIO, HEIST_DEPOSIT_RATIO
        )
        if total <= 0:
            await update.message.reply_text("🏦 صندوق خالی بود! دست خالی برگشتی.")
            return
        lines = [
            f"🚨💰 <b>سرقت از بانک!</b>\n",
            f"{_esc(user.first_name)} زد به صندوق گروه و <b>{int(total)} سانت</b> بالا کشید!\n",
            f"🏛 از خزانه: {int(treasury_part)} سانت",
        ]
        if victims:
            lines.append(f"🧾 از سپردهٔ {len(victims)} نفر:")
            for _uid, name, amount in victims[:8]:
                lines.append(f"   • {_esc(name)}: −{int(amount)} سانت")
            if len(victims) > 8:
                lines.append(f"   • و {len(victims) - 8} نفر دیگه")
        lines.append(f"\n😱 هیچ‌جا امن نیست! (تا {HEIST_COOLDOWN_SECONDS // 3600} ساعت دیگه بانک آماده‌باشه‌ست)")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        await announce_achievements(context, chat_id, user.first_name,
                                    award(user.id, chat_id, 'thief'))
    else:
        # Caught. The fine is a transfer into the treasury, not a deletion, so a failed
        # heist actually makes the next payday slightly better for the depositors.
        fine = max(1, int(would_be * HEIST_FINE_RATIO))
        if db.try_deduct_size(user.id, chat_id, fine):
            db.treasury_add(chat_id, fine, note="جریمهٔ سرقت ناموفق")
            await update.message.reply_text(
                f"🚔 <b>دزدگیر بانک زد!</b>\n\n"
                f"{_esc(user.first_name)} سر بزنگاه گیر افتاد و <b>{int(fine)} سانت</b> جریمه شد.\n"
                f"جریمه رفت تو خزانه — یعنی سود سپرده‌گذارها بیشتر شد. 😎",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"🚔 دزدگیر بانک زد و {_esc(user.first_name)} گیر افتاد!\n"
                f"(اون‌قدر فقیر بود که حتی جریمه هم نتونست بده 💀)"
            )


async def bank_interest_job(context: ContextTypes.DEFAULT_TYPE):
    """Nightly: pay each group's depositors out of that group's treasury, and nothing
    more. Claimed once per day per group so a restart can never pay twice."""
    today_str = tehran_today_str()
    for chat_id in db.get_all_chats():
        try:
            if not db.claim_interest_run(chat_id, today_str):
                continue
            econ = db.get_economy(chat_id)
            # The nominal rate is the crown's to set. Note it is NOT inflation-adjusted:
            # that is the point - depositors carry the inflation risk themselves, so a
            # money-printing king really does rob the savers.
            rate = max(0.0, min(0.50, BANK_INTEREST_RATE * econ[3]))
            rows, paid, left = db.pay_interest(
                chat_id, rate, BANK_INTEREST_MAX_TREASURY_SHARE
            )
            if rows <= 0 or paid <= 0:
                continue
            total_dep, _ = db.get_bank_totals(chat_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"🏦 <b>سود روزانهٔ بانک</b>\n\n"
                      f"به {rows} سپرده‌گذار در مجموع {int(paid)} سانت سود داده شد.\n"
                      f"🧾 کل سپرده‌ها: {int(total_dep)} سانت\n"
                      f"💰 باقی‌ماندهٔ خزانه: {int(left)} سانت\n\n"
                      f"با /bank حسابت رو ببین."),
                parse_mode="HTML"
            )
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception:
            logging.exception(f"bank interest failed for {chat_id}")


# ---------------------------------------------------------------- loan commands

def _fmt_due(due_at):
    """Time left on a loan, in the group's own words."""
    if not due_at:
        return "؟"
    delta = due_at - datetime.datetime.now(datetime.timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "سررسید گذشته!"
    h, m = secs // 3600, (secs % 3600) // 60
    if h >= 24:
        return f"{h // 24} روز و {h % 24} ساعت"
    return f"{h} ساعت و {m} دقیقه"


async def nozul_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/nozul @user <amount> <rate%>` - offer someone a loan at a rate you pick.

    The lender is not charged anything until the borrower actually accepts, so an
    ignored offer costs nothing and cannot be used to tie up a rival's balance."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("نزول فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    lender_size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)

    target_id, target_name = get_target_user(update, update.message.text, chat_id)
    if not target_id:
        await update.message.reply_text(
            "به کی می‌خوای نزول بدی؟\n"
            "/nozul @username <مقدار> <درصد>\n"
            "مثال: /nozul @ali 100 25  →  ۱۰۰ سانت قرض بده، ۱۲۵ پس بگیر"
        )
        return
    if target_id == user.id:
        await update.message.reply_text("به خودت نزول بدی؟ 😐")
        return

    nums = [p for p in update.message.text.split()[1:] if p.lstrip('-').replace('.', '', 1).isdigit()]
    if len(nums) < 2:
        await update.message.reply_text(
            "مقدار و درصد رو بگو!\n/nozul @username <مقدار> <درصد>\n"
            f"درصد بین {int(NOZUL_MIN_RATE*100)} تا {int(NOZUL_MAX_RATE*100)}."
        )
        return
    try:
        principal = int(float(nums[0])); rate_pct = float(nums[1])
    except ValueError:
        await update.message.reply_text("عدد نامعتبره!")
        return

    if principal < LOAN_MIN_PRINCIPAL:
        await update.message.reply_text(f"حداقل مبلغ نزول {LOAN_MIN_PRINCIPAL} سانته.")
        return
    if lender_size < principal:
        await update.message.reply_text(
            f"خودت این‌قدر سانت نداری! 💼 {int(lender_size)} سانت داری."
        )
        return
    rate = rate_pct / 100.0
    if not (NOZUL_MIN_RATE - 1e-9 <= rate <= NOZUL_MAX_RATE + 1e-9):
        await update.message.reply_text(
            f"درصد باید بین {int(NOZUL_MIN_RATE*100)} تا {int(NOZUL_MAX_RATE*100)} باشه."
        )
        return

    borrower_size = (db.get_user_info(target_id, chat_id) or (None, 0))[1] or 0
    b_score, b_repaid, b_late, b_defaults = db.get_credit(target_id, chat_id)
    max_principal = _credit_cap(borrower_size, b_score)
    if principal > max_principal:
        await update.message.reply_text(
            f"بیشتر از توانِ {_esc(target_name)} نمی‌شه بهش قرض داد!\n"
            f"سقف براش الان <b>{max_principal}</b> سانته "
            f"(سایز {int(borrower_size)} × ضریب اعتبار {_credit_factor(b_score):.2f}).\n"
            f"📊 امتیاز اعتباریش: {b_score}/200 — {_credit_grade(b_score)}",
            parse_mode="HTML"
        )
        return
    if db.count_active_loans(chat_id, target_id, as_lender=False) >= LOAN_MAX_BORROWER_LOANS:
        await update.message.reply_text(f"{target_name} همین الان بدهی باز داره؛ اول اونا رو تسویه کنه.")
        return
    if db.count_active_loans(chat_id, user.id, as_lender=True) >= LOAN_MAX_LENDER_LOANS:
        await update.message.reply_text("تو همین الان کلی نزول باز داری! اول جمعشون کن.")
        return

    loan_id, due_amount = db.create_loan_offer(
        chat_id, user.id, user.first_name, target_id, target_name, principal, rate, LOAN_TERM_DAYS
    )
    warn = (f"\n📊 اعتبار {_esc(target_name)}: <b>{b_score}</b>/200 — {_credit_grade(b_score)}"
            f" (سروقت {b_repaid - b_late} / با تأخیر {b_late} / نکول {b_defaults})")
    keyboard = [[InlineKeyboardButton("✍️ قبول می‌کنم", callback_data=f"loanok_{loan_id}")]]
    await update.message.reply_text(
        f"🤝 <b>پیشنهاد نزول</b>\n\n"
        f"{_esc(user.first_name)} به {_esc(target_name)} پیشنهاد داد:\n"
        f"💵 الان بگیر: <b>{principal}</b> سانت\n"
        f"💸 تا {LOAN_TERM_DAYS} روز دیگه پس بده: <b>{int(due_amount)}</b> سانت "
        f"(سود {int(rate*100)}٪){warn}\n\n"
        f"⛔️ سر موعد نداشته باشی، اول از جیبت، بعد از <b>سپردهٔ بانکیت</b> برداشته می‌شه — "
        f"و اگه بازم کم بیاد سایزت می‌ره زیر صفر.\n\n"
        f"فقط {_esc(target_name)} می‌تونه قبول کنه. ({LOAN_OFFER_TTL_SECONDS // 60} دقیقه اعتبار)",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )


async def loan_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن!", show_alert=True)
        return
    try:
        loan_id = int(query.data.split('_', 1)[1])
    except (IndexError, ValueError):
        return

    loan = db.get_loan(loan_id)
    if not loan or loan[10] != 'offered':
        await query.answer("این پیشنهاد دیگه معتبر نیست!", show_alert=True)
        return
    # The borrower is checked here AND again inside accept_loan's conditional UPDATE, so
    # a forged callback_data cannot make someone else's loan land on this user.
    if user.id != loan[4]:
        await query.answer("این پیشنهاد برای تو نیست!", show_alert=True)
        return

    ok, principal, due_amount = db.accept_loan(loan_id, user.id, LOAN_TERM_DAYS)
    if not ok:
        reasons = {
            'gone': "این پیشنهاد دیگه معتبر نیست!",
            'treasury': "خزانهٔ بانک الان این‌قدر پول نداره!",
            'lender_broke': "نزول‌خور دیگه این‌قدر سانت نداره! 😂",
        }
        await query.answer(reasons.get(principal, "نشد!"), show_alert=True)
        return

    lender_label = "بانک" if loan[2] is None else loan[3]
    await query.answer(f"{int(principal)} سانت گرفتی! یادت نره پس بدی 😈")
    try:
        await query.edit_message_text(
            f"🤝 <b>نزول بسته شد!</b>\n\n"
            f"{_esc(loan[5])} از {_esc(lender_label)} <b>{int(principal)}</b> سانت گرفت.\n"
            f"💸 باید تا {LOAN_TERM_DAYS} روز دیگه <b>{int(due_amount)}</b> سانت پس بده.\n\n"
            f"با /pardakht زودتر تسویه کن، وگرنه خودکار وصول می‌شه.",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def vam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/vam <amount>` - the official loan, funded by the group treasury at a flat rate.
    Repayment goes back into the treasury, which is what pays everyone's bank interest."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("وام فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    treasury, _, _ = db.get_treasury(chat_id)
    score, repaid, late, defaults = db.get_credit(user.id, chat_id)
    # The official bank is the strict lender: bad credit is refused outright rather
    # than priced. Loan sharks are still an option, which is the point.
    if score < BANK_LOAN_MIN_SCORE:
        await update.message.reply_text(
            f"🏛 بانک به تو وام نمی‌ده!\n\n"
            f"📊 امتیاز اعتباریت: <b>{score}</b>/200 — {_credit_grade(score)}\n"
            f"حداقل لازم برای وام بانکی: {BANK_LOAN_MIN_SCORE}\n\n"
            f"🚔 نکول: {defaults} بار | 🐌 تأخیر: {late} بار\n\n"
            f"با تسویهٔ سروقت بدهی‌هات امتیازت بالا میره. فعلاً باید بری سراغ نزول‌خورها.",
            parse_mode="HTML"
        )
        return
    ceiling = int(min(_credit_cap(size, score), treasury * BANK_LOAN_MAX_TREASURY_SHARE))

    parts = update.message.text.split()
    amount = None
    if len(parts) > 1:
        try:
            amount = int(float(parts[1]))
        except ValueError:
            amount = None
    if amount is None or amount <= 0:
        await update.message.reply_text(
            f"🏛 وام رسمی بانک\n\n"
            f"نرخ: {int(BANK_LOAN_RATE*100)}٪ — سررسید {LOAN_TERM_DAYS} روز\n"
            f"📊 امتیاز اعتباریت: {score}/200 — {_credit_grade(score)}\n"
            f"💰 خزانه: {int(treasury)} سانت\n"
            f"📈 سقف وام تو الان: {ceiling} سانت\n\n"
            f"/vam <مقدار>"
        )
        return
    if amount < LOAN_MIN_PRINCIPAL:
        await update.message.reply_text(f"حداقل وام {LOAN_MIN_PRINCIPAL} سانته.")
        return
    if amount > ceiling:
        await update.message.reply_text(
            f"سقف وام تو الان {ceiling} سانته.\n"
            f"(سایز {int(size)} × ضریب اعتبار {_credit_factor(score):.2f}، "
            f"و سقف خزانه: {int(treasury)} سانت)"
        )
        return
    if db.count_active_loans(chat_id, user.id, as_lender=False) >= LOAN_MAX_BORROWER_LOANS:
        await update.message.reply_text("بدهی بازِ زیادی داری! اول تسویه کن.")
        return

    loan_id, due_amount = db.create_loan_offer(
        chat_id, None, "بانک", user.id, user.first_name, amount, BANK_LOAN_RATE, LOAN_TERM_DAYS
    )
    keyboard = [[InlineKeyboardButton("✍️ امضا می‌کنم", callback_data=f"loanok_{loan_id}")]]
    await update.message.reply_text(
        f"🏛 <b>وام بانکی</b>\n\n"
        f"{_esc(user.first_name)} می‌خواد <b>{amount}</b> سانت وام بگیره.\n"
        f"💸 بازپرداخت: <b>{int(due_amount)}</b> سانت تا {LOAN_TERM_DAYS} روز دیگه "
        f"(سود {int(BANK_LOAN_RATE*100)}٪)\n\n"
        f"⛔️ سر موعد نداشته باشی، از جیب و بعد از سپردهٔ بانکیت وصول می‌شه.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )


async def debts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/bedehi` - what you owe and what you're owed."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این دستور فقط داخل گروه‌ها کار می‌کند!")
        return
    borrowed, lent = db.get_user_loans(chat_id, user.id)
    score, repaid, late, defaults = db.get_credit(user.id, chat_id)
    size, _, _ = db.get_user(user.id, chat_id, None, None)
    lines = [f"📜 <b>دفتر بدهی {_esc(user.first_name)}</b>",
             f"📊 اعتبار: <b>{score}</b>/200 — {_credit_grade(score)} "
             f"(سقف وام: {_credit_cap(size, score)} سانت)", ""]
    if borrowed:
        lines.append("💸 <b>بدهکاری:</b>")
        for lid, lname, l_id, due, due_at, principal, rate in borrowed:
            who = "بانک" if l_id is None else lname
            lines.append(f"  #{lid} به {_esc(who or '?')}: <b>{int(due)}</b> سانت "
                         f"(اصل {int(principal)}، سود {int(rate*100)}٪) — {_fmt_due(due_at)}")
    if lent:
        lines.append("\n💰 <b>طلبکاری:</b>")
        for lid, bname, due, due_at, principal, rate in lent:
            lines.append(f"  #{lid} از {_esc(bname or '?')}: <b>{int(due)}</b> سانت "
                         f"(اصل {int(principal)}، سود {int(rate*100)}٪) — {_fmt_due(due_at)}")
    if not borrowed and not lent:
        lines.append("نه بدهکاری، نه طلبکاری. پاک و تمیز 😇")
    if defaults or late:
        lines.append(f"\n⚠️ سابقه: {defaults} نکول، {late} تأخیر")
    if borrowed:
        lines.append("\nبرای تسویهٔ زودتر: /pardakht &lt;شماره&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def repay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/pardakht [id]` - settle a loan early, at the same amount it would cost at
    maturity. Paying early costs nothing extra and clears the debt off your book."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این دستور فقط داخل گروه‌ها کار می‌کند!")
        return
    borrowed, _ = db.get_user_loans(chat_id, user.id)
    if not borrowed:
        await update.message.reply_text("بدهی‌ای نداری! 😇")
        return

    parts = update.message.text.split()
    loan_id = None
    if len(parts) > 1 and parts[1].lstrip('#').isdigit():
        loan_id = int(parts[1].lstrip('#'))
    elif len(borrowed) == 1:
        loan_id = borrowed[0][0]
    if loan_id is None:
        await update.message.reply_text(
            "کدوم بدهی رو تسویه کنم؟ با /bedehi شماره‌ها رو ببین، بعد /pardakht <شماره>"
        )
        return
    if loan_id not in [b[0] for b in borrowed]:
        await update.message.reply_text("این شماره جزو بدهی‌های باز تو نیست!")
        return

    result = db.settle_loan(loan_id, forced=False, today_str=tehran_today_str())
    if not result:
        await update.message.reply_text("این بدهی همین الان تسویه شد!")
        return
    who = "بانک" if result['lender_id'] is None else result['lender_name']
    extra = ""
    if result['from_bank'] > 0:
        extra += f"\n🏦 {int(result['from_bank'])} سانتش از سپردهٔ بانکیت برداشته شد."
    if result['shortfall'] > 0:
        extra += f"\n🔻 {int(result['shortfall'])} سانت کم آوردی و سایزت رفت زیر صفر."
    d = result['credit_delta']
    if result['outcome'] == 'token':
        why = " (وام خیلی کوچیک/زودگذر بود — اعتبار نمی‌آره)"
    elif result['outcome'] == 'capped':
        why = " (به سقف اعتبارِ امروزت رسیدی)"
    elif result['was_late']:
        why = " (دیر تسویه کردی!)"
    else:
        why = " (سروقت تسویه کردی 👌)"
    credit_line = f"\n\n📊 اعتبارت {d:+d} شد → <b>{result['credit_score']}</b>/200{why}"
    await update.message.reply_text(
        f"✅ بدهی #{loan_id} تسویه شد.\n\n"
        f"💸 {int(result['due_amount'])} سانت به {_esc(who or '?')} پرداخت شد "
        f"(اصل {int(result['principal'])} + سود {int(result['interest'])}).{extra}{credit_line}",
        parse_mode="HTML"
    )


async def collect_loans_job(context: ContextTypes.DEFAULT_TYPE):
    """Nightly: force-collect everything past its due date, and bin stale offers.

    Runs before the handicap so the day's interest has already been booked as profit and
    loss by the time the dials are recomputed."""
    try:
        db.expire_loan_offers(LOAN_OFFER_TTL_SECONDS)
    except Exception:
        logging.exception("expiring loan offers failed")

    for loan_id in db.get_overdue_loans():
        try:
            r = db.settle_loan(loan_id, forced=True, today_str=tehran_today_str())
            if not r:
                continue
            who = "بانک" if r['lender_id'] is None else r['lender_name']
            bits = [f"🚔 <b>وصول اجباری بدهی #{loan_id}</b>\n",
                    f"{_esc(r['borrower_name'] or '?')} سر موعد {int(r['due_amount'])} سانت "
                    f"بدهی‌شو به {_esc(who or '?')} نداد."]
            if r['from_wallet'] > 0:
                bits.append(f"💼 از جیبش: {int(r['from_wallet'])} سانت")
            if r['from_bank'] > 0:
                bits.append(f"🏦 از سپردهٔ بانکیش: {int(r['from_bank'])} سانت")
            if r['shortfall'] > 0:
                bits.append(f"🔻 بازم کم آورد: {int(r['shortfall'])} سانت — سایزش رفت زیر صفر!")
                bits.append("🏷 از این به بعد <b>بدهکار</b>ه.")
            bits.append(f"📊 اعتبارش {r['credit_delta']:+d} شد → <b>{r['credit_score']}</b>/200 "
                        f"(سقف وام‌های بعدیش کمتر شد)")
            await context.bot.send_message(chat_id=r['chat_id'], text="\n".join(bits),
                                           parse_mode="HTML")
        except Forbidden:
            pass
        except Exception:
            logging.exception(f"collecting loan {loan_id} failed")


def _credit_factor(score):
    """Score -> how much of their size a player may borrow. 100 is par (1.0x)."""
    return max(CREDIT_MIN_FACTOR, min(CREDIT_MAX_FACTOR, (score or 0) / 100.0))


def _credit_cap(borrower_size, score):
    """The most this player may owe on one loan, given their size and their record."""
    return max(LOAN_MIN_PRINCIPAL,
               int(borrower_size * LOAN_MAX_PRINCIPAL_RATIO * _credit_factor(score)))


def _credit_grade(score):
    if score >= 150: return "عالی 🟢"
    if score >= 110: return "خوب 🟢"
    if score >= 80:  return "متوسط 🟡"
    if score >= 50:  return "ضعیف 🟠"
    return "خراب 🔴"


def _credit_report(name, score, repaid, late, defaults, size):
    bar = "█" * max(1, round(score / 20)) + "░" * max(0, 10 - round(score / 20))
    return (f"📊 <b>اعتبارسنجی {_esc(name)}</b>\n\n"
            f"امتیاز: <b>{score}</b>/200 — {_credit_grade(score)}\n"
            f"<code>{bar}</code>\n\n"
            f"✅ تسویهٔ سروقت: {repaid - late}\n"
            f"🐌 تسویهٔ با تأخیر: {late}\n"
            f"🚔 نکول (وصول اجباری): {defaults}\n\n"
            f"💳 سقف وامش الان: <b>{_credit_cap(size, score)}</b> سانت "
            f"(ضریب {_credit_factor(score):.2f}× روی سایز {int(size)})")


async def credit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/etebar [@user]` - pull someone's credit file. Costs CREDIT_CHECK_FEE, which
    goes to the treasury like every other fee. Charging for it is the point: a lender
    deciding whether to trust someone should pay for the diligence."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("اعتبارسنجی فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    db.get_user(user.id, chat_id, user.username, user.first_name)

    target_id, target_name = get_target_user(update, update.message.text, chat_id)
    if not target_id:
        target_id, target_name = user.id, user.first_name

    if not db.charge_credit_check(user.id, chat_id, CREDIT_CHECK_FEE):
        size, _, _ = db.get_user(user.id, chat_id, None, None)
        await update.message.reply_text(
            f"اعتبارسنجی {CREDIT_CHECK_FEE} سانت هزینه داره و تو {int(size)} سانت داری!"
        )
        return

    score, repaid, late, defaults = db.get_credit(target_id, chat_id)
    t_size = (db.get_user_info(target_id, chat_id) or (None, 0))[1] or 0
    await update.message.reply_text(
        _credit_report(target_name, score, repaid, late, defaults, t_size)
        + f"\n\n🧾 هزینهٔ اعتبارسنجی: {CREDIT_CHECK_FEE} سانت → خزانه",
        parse_mode="HTML"
    )


def inflation_of(chat_id):
    return db.get_economy(chat_id)[0]


def priced(base, chat_id, inflation=None):
    """What something costs here and now. Every shop price, ticket and payout goes
    through this rather than using its constant directly."""
    infl = inflation if inflation is not None else inflation_of(chat_id)
    return max(1, int(round(base * max(INFLATION_PRICE_FLOOR, infl))))


def fee_of(chat_id, base_ratio, econ=None):
    """A fee rate after the crown's dial. The king can halve fees or pile them on."""
    e = econ or db.get_economy(chat_id)
    return max(0.0, min(0.90, base_ratio * e[2]))


# ---------------------------------------------------------------- royal decrees

# What the king is offered tonight, per group: {chat_id: (date, [codes], king_id)}.
# Held in memory on purpose - an offer that is lost to a restart simply gets re-rolled
# by the next night's job, and nothing has moved until one is signed.
pending_decrees = {}


def _roll_decrees(chat_id, today_str, king_id):
    """Deal tonight's hand: three honest decrees and three corrupt ones, drawn fresh
    from the two hundred. Kept in that order rather than shuffled so the message can
    group them - the king should see the two columns and feel the choice."""
    good = [d[0] for d in random.sample(decrees.GOOD, DECREE_GOOD_CHOICES)]
    bad = [d[0] for d in random.sample(decrees.BAD, DECREE_BAD_CHOICES)]
    codes = good + bad
    pending_decrees[chat_id] = (today_str, codes, king_id)
    return codes


def _decree_keyboard(chat_id, codes):
    rows = []
    for i, code in enumerate(codes, 1):
        d = decrees.get(code)
        if not d:
            continue
        mark = "😈" if d[4] == 'bad' else "😇"
        rows.append([InlineKeyboardButton(f"{mark} {i}. {d[1]}", callback_data=f"decree_{code}")])
    return InlineKeyboardMarkup(rows)


def _decree_block(codes, start_index):
    """Renders one column of the offer, numbered to match the buttons."""
    out = []
    for i, code in enumerate(codes, start_index):
        d = decrees.get(code)
        if not d:
            continue
        _c, title, desc, eff, _kind = d
        out.append(f"<b>{i}. {_esc(title)}</b>")
        out.append(f"<i>{_esc(desc)}</i>")
        for bit in decrees.summarize(eff):
            out.append(f"   • {bit}")
        out.append("")
    return out


def _render_decree_offer(king_name, codes, econ):
    inflation, unrest, fee_m, int_m, grow_m = econ
    good = [c for c in codes if (decrees.get(c) or (None,)*5)[4] == 'good']
    bad = [c for c in codes if (decrees.get(c) or (None,)*5)[4] == 'bad']
    lines = [
        f"👑 <b>فرمان امشب — {_esc(king_name)}</b>", "",
        f"📈 تورم: <b>{inflation:.2f}×</b>   😡 خشم مردم: <b>{unrest:.0f}</b>/100",
        f"🧾 کارمزد ×{fee_m:.2f}   🏦 سود ×{int_m:.2f}   🌱 رشد ×{grow_m:.2f}", "",
        "━━━━━━━━━━━━━━━", "😇 <b>سازنده</b> — به نفع مردم، به ضرر جیب تو", "",
    ]
    lines += _decree_block(good, 1)
    lines += ["━━━━━━━━━━━━━━━", "😈 <b>فاسد</b> — به نفع جیب تو، به ضرر مردم", ""]
    lines += _decree_block(bad, len(good) + 1)
    lines.append("⚠️ فقط <b>یکی</b> رو می‌تونی امضا کنی. تا فردا شب.")
    return "\n".join(lines)


async def _offer_decrees_to(context, chat_id, today_str):
    """Post tonight's hand to one group. Returns True if an offer actually went out.

    Skips a group that has no king, that already signed today, or that is already
    holding an unsigned offer - so this is safe to call more than once a night."""
    kingdom, _ = refresh_king(chat_id)
    if not kingdom or not kingdom[0]:
        return False
    full = db.get_economy_full(chat_id)
    if full and full[6] == today_str:
        return False  # the king already signed today
    entry = pending_decrees.get(chat_id)
    if entry and entry[0] == today_str and entry[2] == kingdom[0]:
        return False  # already offered tonight, to this same king

    king_id, king_name = kingdom[0], kingdom[1]
    codes = _roll_decrees(chat_id, today_str, king_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=_render_decree_offer(king_name, codes, db.get_economy(chat_id)),
        reply_markup=_decree_keyboard(chat_id, codes),
        parse_mode="HTML"
    )
    return True


async def decree_offer_job(context: ContextTypes.DEFAULT_TYPE):
    """Each night, hand the sitting king his six choices. No king, no decree."""
    today_str = tehran_today_str()
    for chat_id in db.get_all_chats():
        try:
            await _offer_decrees_to(context, chat_id, today_str)
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception:
            logging.exception(f"decree offer failed for {chat_id}")


async def recover_decree_offer(context: ContextTypes.DEFAULT_TYPE):
    """Startup catch-up. run_daily only fires at its appointed minute, so a bot that was
    down - or freshly deployed - past that minute would silently skip the whole night's
    decree. This posts the offer on boot instead, but only once the hour has actually
    come round, and only where tonight's offer is genuinely still missing."""
    now = datetime.datetime.now(IRAN_TZ)
    if (now.hour, now.minute) < (DECREE_OFFER_HOUR, DECREE_OFFER_MINUTE):
        return
    today_str = tehran_today_str()
    for chat_id in db.get_all_chats():
        try:
            if await _offer_decrees_to(context, chat_id, today_str):
                logging.info(f"decree offer recovered on startup for {chat_id}")
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception:
            logging.exception(f"decree recovery failed for {chat_id}")


async def decree_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/farman` - the king pulls up tonight's choices if he missed the announcement."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("فرمان سلطنتی فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    kingdom, _ = refresh_king(chat_id)
    if not kingdom or not kingdom[0]:
        await update.message.reply_text("این گروه هنوز پادشاه نداره! با /king ببین چطور تاج می‌گیرن.")
        return
    if user.id != kingdom[0]:
        await update.message.reply_text(
            f"فقط پادشاه ({_esc(kingdom[1] or '?')}) می‌تونه فرمان بده! 👑", parse_mode="HTML"
        )
        return

    today_str = tehran_today_str()
    entry = pending_decrees.get(chat_id)
    if not entry or entry[0] != today_str or entry[2] != kingdom[0]:
        # No hand dealt yet today (or the crown changed hands) - deal one now.
        codes = _roll_decrees(chat_id, today_str, kingdom[0])
    else:
        codes = entry[1]

    econ = db.get_economy(chat_id)
    full = db.get_economy_full(chat_id)
    if full and full[6] == today_str:
        await update.message.reply_text("امروز فرمانت رو امضا کردی! فردا دوباره. 👑")
        return
    await update.message.reply_text(
        _render_decree_offer(kingdom[1] or '?', codes, econ),
        reply_markup=_decree_keyboard(chat_id, codes), parse_mode="HTML"
    )


async def decree_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن!", show_alert=True)
        return
    code = query.data.split('_', 1)[1] if '_' in query.data else ''
    d = decrees.get(code)
    if not d:
        await query.answer("این فرمان معتبر نیست!", show_alert=True)
        return

    kingdom, _ = refresh_king(chat_id)
    if not kingdom or user.id != kingdom[0]:
        await query.answer("فقط پادشاه می‌تونه فرمان امضا کنه! 👑", show_alert=True)
        return

    today_str = tehran_today_str()
    entry = pending_decrees.get(chat_id)
    # Only a decree that was actually offered tonight can be signed - callback_data is
    # client-supplied, so without this a king could pick his favourite out of all 200.
    if not entry or entry[0] != today_str or code not in entry[1]:
        await query.answer("این فرمان جزو گزینه‌های امشب نیست!", show_alert=True)
        return
    if not db.claim_decree_day(chat_id, today_str):
        await query.answer("امروز فرمانت رو امضا کردی! فردا دوباره.", show_alert=True)
        return

    _c, title, desc, eff, kind = d
    try:
        res = db.apply_decree(chat_id, kingdom[0], kingdom[1] or '?', today_str,
                              code, title, kind, eff)
    except Exception:
        db.release_decree_day(chat_id)
        logging.exception(f"decree {code} failed in {chat_id}")
        await query.answer("اجرای فرمان به مشکل خورد!", show_alert=True)
        return

    pending_decrees.pop(chat_id, None)
    mark = "😈" if kind == 'bad' else "😇"
    lines = [f"👑 <b>فرمان امضا شد</b> {mark}", "",
             f"<b>{_esc(title)}</b>", f"<i>{_esc(desc)}</i>", ""]
    if res['king_delta']:
        verb = "گرفت" if res['king_delta'] > 0 else "داد"
        lines.append(f"👑 پادشاه {abs(int(res['king_delta']))} سانت {verb}")
    if res['minted']:
        lines.append(f"🖨 {int(res['minted'])} سانت از هیچ چاپ شد")
    if res['treasury_delta']:
        lines.append(f"🏛 خزانه {int(res['treasury_delta']):+d}")
    if res['players_delta']:
        lines.append(f"👥 مردم در مجموع {int(res['players_delta']):+d}")
    arrow = "📈" if res['inflation'] > res['inflation_before'] else (
            "📉" if res['inflation'] < res['inflation_before'] else "➖")
    lines.append(f"{arrow} تورم: {res['inflation_before']:.2f} → <b>{res['inflation']:.2f}</b>")
    lines.append(f"😡 خشم مردم: <b>{res['unrest']:.0f}</b>/100")
    if res['fee_mult'] != 1.0 or res['interest_mult'] != 1.0 or res['growth_mult'] != 1.0:
        lines.append(f"🧾 کارمزد ×{res['fee_mult']}  🏦 سود ×{res['interest_mult']}  "
                     f"🌱 رشد ×{res['growth_mult']}")
    if res['unrest'] >= UNREST_REVOLT_THRESHOLD:
        lines.append("\n🔥 <b>مردم دارن شورش می‌کنن!</b> اگه همین‌طور ادامه بدی، تاج رو از سرت برمی‌دارن.")

    await query.answer("فرمان اجرا شد!")
    try:
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")


async def economy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/eghtesad` - the state of the economy, readable by anyone. Deliberately public:
    the whole point of the unrest mechanic is that the group can see what the crown is
    doing to them."""
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این دستور فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    full = db.get_economy_full(chat_id)
    inflation, unrest, fee_m, int_m, grow_m, supply_last, last_decree, good, bad = full
    supply = db.get_money_supply(chat_id)
    treasury, _, _ = db.get_treasury(chat_id)
    deposits, holders = db.get_bank_totals(chat_id)
    kingdom = db.get_kingdom(chat_id)
    king = kingdom[1] if kingdom and kingdom[1] else "—"

    mood = ("😌 آروم" if unrest < 25 else "😐 ناراضی" if unrest < 50
            else "😠 عصبانی" if unrest < UNREST_REVOLT_THRESHOLD else "🔥 در آستانهٔ شورش")
    trend = ("🔥 تورم شدید" if inflation >= 2.0 else "📈 تورم بالا" if inflation >= 1.3
             else "✅ باثبات" if inflation >= 0.85 else "📉 رکود / کاهش قیمت")

    lines = [
        f"📊 <b>اقتصاد گروه</b>", "",
        f"👑 پادشاه: {_esc(king)}",
        f"📈 شاخص قیمت: <b>{inflation:.2f}×</b> — {trend}",
        f"😡 خشم مردم: <b>{unrest:.0f}</b>/100 — {mood}", "",
        f"💵 کل پول در گردش: {int(supply)} سانت",
        f"🏛 خزانه: {int(treasury)} سانت",
        f"🏦 سپرده‌ها: {int(deposits)} سانت ({holders} نفر)", "",
        f"<b>اهرم‌های تاج</b>",
        f"🧾 کارمزدها: ×{fee_m:.2f}",
        f"🏦 سود سپرده: ×{int_m:.2f}  (نرخ مؤثر {BANK_INTEREST_RATE*int_m*100:.1f}٪)",
        f"🌱 رشد روزانه: ×{grow_m:.2f}", "",
        f"📜 فرمان‌ها: {good} سازنده / {bad} فاسد", "",
        f"<b>قیمت‌ها با تورم امروز</b>",
        f"🍆 ویاگرا {priced(60, chat_id, inflation)} • 🎟 بلیت لاتاری "
        f"{priced(LOTTERY_TICKET_PRICE, chat_id, inflation)} • 🐉 پاداش باس "
        f"{priced(BOSS_REWARD_BASE, chat_id, inflation)}",
    ]
    if inflation > 1.15:
        lines.append("\n💡 تورم بالاست: پسِ‌انداز آب می‌ره، بدهکارها برنده‌ان.")
    elif inflation < 0.9:
        lines.append("\n💡 قیمت‌ها پایینه: پس‌انداز می‌ارزه، بدهی سنگین‌تر شده.")
    lines.append("\nتاریخچه: /farmanha")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def decree_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/farmanha` - what past kings actually did, so a record follows them around."""
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این دستور فقط داخل گروه‌ها کار می‌کند!")
        return
    rows = db.get_decree_history(chat_id, 12)
    if not rows:
        await update.message.reply_text("هنوز هیچ فرمانی امضا نشده.")
        return
    lines = ["📜 <b>دفتر فرمان‌ها</b>", ""]
    for date, king_name, title, kind, inf_b, inf_a, king_delta in rows:
        mark = "😈" if kind == 'bad' else "😇"
        arrow = "📈" if (inf_a or 0) > (inf_b or 0) else ("📉" if (inf_a or 0) < (inf_b or 0) else "➖")
        lines.append(f"{mark} <b>{date}</b> — {_esc(king_name or '?')}: {_esc(title)}")
        lines.append(f"    {arrow} تورم {inf_b:.2f}→{inf_a:.2f} | 👑 {int(king_delta or 0):+d} سانت")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def economy_tick_job(context: ContextTypes.DEFAULT_TYPE):
    """Nightly: let the price index chase the money supply, cool the mob a little, and
    see whether a hated king still has a throne."""
    today_str = tehran_today_str()
    for chat_id in db.get_all_chats():
        try:
            tick = db.tick_inflation(chat_id, today_str)
            unrest = db.cool_unrest(chat_id, UNREST_DAILY_COOLDOWN)
            if tick:
                before, after, growth = tick
                if abs(after - before) >= 0.02:
                    arrow = "📈" if after > before else "📉"
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(f"{arrow} <b>شاخص قیمت‌ها</b>\n\n"
                              f"حجم پول {growth*100:+.1f}٪ تغییر کرد.\n"
                              f"تورم: {before:.2f} → <b>{after:.2f}</b>\n\n"
                              + ("قیمت‌ها گرون‌تر شد و ارزش پس‌اندازها کم شد."
                                 if after > before else
                                 "قیمت‌ها پایین اومد و پس‌اندازها باارزش‌تر شد.")),
                        parse_mode="HTML")

            # A furious population eventually removes the problem themselves.
            if unrest >= UNREST_REVOLT_THRESHOLD:
                kingdom = db.get_kingdom(chat_id)
                if kingdom and kingdom[0]:
                    chance = min(0.85, (unrest - UNREST_REVOLT_THRESHOLD) / 40.0 + 0.20)
                    if random.random() < chance:
                        await _revolt(context, chat_id, kingdom)
        except Forbidden:
            db.remove_chat(chat_id)
        except Exception:
            logging.exception(f"economy tick failed for {chat_id}")


async def _revolt(context, chat_id, kingdom):
    """The bill for a corrupt reign. The king's hoard is seized and handed straight back
    to the people he took it from, which is what makes looting a loan against your own
    future rather than free money."""
    king_id, king_name = kingdom[0], kingdom[1]
    size, _, _ = db.get_user(king_id, chat_id, None, None)
    seized = max(0.0, size) * 0.40
    if seized < 1:
        return
    players = [p for p in db.get_all_players(chat_id) if p[0] != king_id and p[0] != BOT_USER_ID]
    if not players:
        return
    if not db.try_deduct_size(king_id, chat_id, seized):
        return
    share = seized / len(players)
    for uid, _n, _s in players:
        db.update_size(uid, chat_id, share)
    db.cool_unrest(chat_id, 60)
    # No need to strip the crown by hand: it is derived from the leaderboard, so losing
    # 40% of his hoard is itself what unseats him - and if he is still the biggest
    # player even after that, he has genuinely earned the right to keep it.
    _kd, new_king = refresh_king(chat_id)
    tail = (f"👑 {_esc(new_king[1])} پادشاه جدیده."
            if new_king else
            f"👑 با این‌همه، {_esc(king_name or '?')} هنوز بزرگ‌ترینه و تاج سرش موند!")
    await context.bot.send_message(
        chat_id=chat_id,
        text=(f"🔥🔥 <b>شورش!</b>\n\n"
              f"مردم ریختن تو قصر و {_esc(king_name or '?')} رو کشیدن پایین.\n"
              f"💰 {int(seized)} سانت از دارایی‌ش مصادره و بین {len(players)} نفر تقسیم شد "
              f"(هر نفر {int(share)} سانت).\n\n" + tail),
        parse_mode="HTML")
    if new_king:
        await announce_achievements(context, chat_id, new_king[1],
                                    award(new_king[0], chat_id, 'king'))


async def martial_law_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/hokm` - the king dissolves an open /ejma and makes its caller his jester.

    Only the sitting king, only on a vote that is genuinely still open, and only once
    every three days per group. Every use raises unrest: the group can see perfectly
    well that a vote was cancelled by decree rather than lost."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("حکومت نظامی فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)

    kingdom, _ = refresh_king(chat_id)
    if not kingdom or not kingdom[0]:
        await update.message.reply_text("این گروه پادشاه نداره!")
        return
    if user.id != kingdom[0]:
        await update.message.reply_text(
            f"فقط پادشاه ({_esc(kingdom[1] or '?')}) می‌تونه حکومت نظامی اعلام کنه! 👑",
            parse_mode="HTML")
        return

    openv = [v for v in db.get_any_open_consensus(chat_id)
             if v[5] < CONSENSUS_VOTE_WINDOW_SECONDS]
    if not openv:
        await update.message.reply_text(
            "الان هیچ اجماع بازی وجود نداره که کنسلش کنی. 🤷\n"
            "(حکومت نظامی فقط روی رأی‌گیری در جریان کار می‌کنه)")
        return

    # Aim at the oldest open vote - the one closest to actually passing.
    vote_id, target_id, target_name, initiator_id, amount, _age = openv[0]
    if initiator_id == user.id:
        await update.message.reply_text("اجماع رو خودت راه انداختی! می‌خوای خودت رو دلقک کنی؟ 🤡")
        return

    ok, remaining = db.try_martial_law(chat_id, MARTIAL_COOLDOWN_SECONDS)
    if not ok:
        days, hours = remaining // 86400, (remaining % 86400) // 3600
        await update.message.reply_text(
            f"⏳ حکومت نظامی هر ۳ روز یک بار!\nتا {days} روز و {hours} ساعت دیگه نمی‌تونی."
        )
        return

    cancelled = db.cancel_consensus(vote_id, chat_id)
    if not cancelled:
        db.release_martial_law(chat_id)
        await update.message.reply_text("اون اجماع همین الان بسته شد!")
        return
    t_id, t_name, init_id, amt = cancelled

    info = db.get_user_info(init_id, chat_id)
    init_name = (info[0] if info else None) or "؟"
    until = db.make_jester(init_id, chat_id, JESTER_HOURS)
    # Cancelling a vote by decree is exactly the sort of thing a population notices.
    econ_note = ""
    try:
        res = db.apply_decree(chat_id, user.id, kingdom[1] or '?', tehran_today_str(),
                              'martial', 'حکومت نظامی', 'bad', {'unrest': MARTIAL_UNREST})
        econ_note = f"\n😡 خشم مردم: <b>{res['unrest']:.0f}</b>/100"
        if res['unrest'] >= UNREST_REVOLT_THRESHOLD:
            econ_note += "\n🔥 مردم دارن شورش می‌کنن!"
    except Exception:
        logging.exception(f"martial law unrest bump failed for {chat_id}")

    await update.message.reply_text(
        f"🪖 <b>حکومت نظامی!</b>\n\n"
        f"پادشاه {_esc(kingdom[1] or '?')} اجماع علیه {_esc(t_name or '?')} رو منحل کرد.\n"
        f"💨 اون {int(amt or 0)} سانت جایی نرفت.\n\n"
        f"🤡 <b>{_esc(init_name)}</b> که این اجماع رو راه انداخته بود، "
        f"تا {JESTER_HOURS} ساعت <b>دلقک دربار</b>ه:\n"
        f"   • نمی‌تونه اجماع راه بندازه یا رأی بده\n"
        f"   • {int(JESTER_TRIBUTE_RATIO*100)}٪ از رشد روزانه‌ش می‌ره تو جیب پادشاه\n"
        f"   • 🤡 کنار اسمش تو لیدربرد می‌مونه"
        f"{econ_note}\n\n"
        f"⏳ حکومت نظامی بعدی: ۳ روز دیگه.",
        parse_mode="HTML")


async def jesters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/dalghak` - who is currently wearing the motley, and for how much longer."""
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("این دستور فقط داخل گروه‌ها کار می‌کند!")
        return
    rows = db.get_jesters(chat_id)
    if not rows:
        await update.message.reply_text("الان هیچ دلقکی تو دربار نیست. 🤷")
        return
    lines = ["🤡 <b>دلقک‌های دربار</b>", ""]
    for _uid, name, secs in rows:
        h, m = int(secs) // 3600, (int(secs) % 3600) // 60
        lines.append(f"• {_esc(name)} — {h} ساعت و {m} دقیقه دیگه آزاد می‌شه")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def transfer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/enteghal <amount>` - move your own size from this group to another one you play
    in, minus a heavy fee. Offers the destination as buttons because nobody knows their
    groups by chat_id."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.message.reply_text("انتقال بین‌گروهی فقط داخل گروه‌ها کار می‌کند!")
        return
    db.track_chat(chat_id)
    size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)

    parts = update.message.text.split()
    amount = None
    if len(parts) > 1:
        try:
            amount = int(float(parts[1]))
        except ValueError:
            amount = None
    if amount is None or amount <= 0:
        await update.message.reply_text(
            f"🔁 انتقال بین‌گروهی\n\n"
            f"سایزت رو به یکی دیگه از گروه‌هایی که توش بازی می‌کنی بفرست.\n"
            f"⚠️ کارمزدش سنگینه: {int(XFER_FEE_RATIO*100)}٪\n"
            f"⏳ هر {XFER_COOLDOWN_SECONDS // 3600} ساعت یک بار\n"
            f"💼 جیبت: {int(size)} سانت\n\n"
            f"/enteghal <مقدار>"
        )
        return
    if amount < XFER_MIN_AMOUNT:
        await update.message.reply_text(f"حداقل مبلغ انتقال {XFER_MIN_AMOUNT} سانته.")
        return
    if size < amount:
        await update.message.reply_text(f"این‌قدر سانت نداری! 💼 {int(size)} سانت داری.")
        return

    groups = db.get_user_groups(user.id, exclude_chat_id=chat_id)
    if not groups:
        await update.message.reply_text(
            "تو هیچ گروه دیگه‌ای بازی نمی‌کنی!\nاول تو یه گروه دیگه /d بزن."
        )
        return

    fee = int(amount * XFER_FEE_RATIO)
    rows = []
    for gid, _gsize in groups[:8]:
        title = f"گروه {gid}"
        try:
            chat = await context.bot.get_chat(gid)
            if chat.title:
                title = chat.title[:40]
        except Exception:
            pass  # bot may have been removed from that group; the id still works
        rows.append([InlineKeyboardButton(f"➡️ {title}", callback_data=f"xfer_{gid}_{amount}")])

    await update.message.reply_text(
        f"🔁 <b>انتقال بین‌گروهی</b>\n\n"
        f"مبلغ: <b>{amount}</b> سانت\n"
        f"🧾 کارمزد ({int(XFER_FEE_RATIO*100)}٪): {fee} سانت → خزانهٔ همین گروه\n"
        f"📦 به مقصد می‌رسه: <b>{amount - fee}</b> سانت\n\n"
        f"کدوم گروه؟",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML"
    )


async def transfer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ اول یه بار تو گروه از /d استفاده کن!", show_alert=True)
        return
    try:
        _, dest_str, amount_str = query.data.split('_', 2)
        dest_chat = int(dest_str); amount = int(amount_str)
    except ValueError:
        return
    if amount < XFER_MIN_AMOUNT or dest_chat >= 0 or dest_chat == chat_id:
        await query.answer("انتقال نامعتبره!", show_alert=True)
        return
    # callback_data is client-supplied, so re-check membership rather than trusting it.
    if dest_chat not in [g[0] for g in db.get_user_groups(user.id, exclude_chat_id=chat_id)]:
        await query.answer("تو اون گروه بازی نمی‌کنی!", show_alert=True)
        return

    ok, remaining = db.try_start_xfer(user.id, chat_id, XFER_COOLDOWN_SECONDS)
    if not ok:
        hours, minutes = remaining // 3600, (remaining % 3600) // 60
        await query.answer(
            f"تازه انتقال زدی! تا {hours} ساعت و {minutes} دقیقهٔ دیگه صبر کن.", show_alert=True
        )
        return

    ok, delivered, fee = db.cross_group_transfer(user.id, chat_id, dest_chat, amount,
                                                 XFER_FEE_RATIO)
    if not ok:
        await query.answer("سایزت کافی نیست!", show_alert=True)
        return

    dest_title = f"گروه {dest_chat}"
    try:
        chat = await context.bot.get_chat(dest_chat)
        if chat.title:
            dest_title = chat.title[:40]
    except Exception:
        pass

    await query.answer(f"{int(delivered)} سانت رسید!")
    try:
        await query.edit_message_text(
            f"🔁 <b>انتقال انجام شد</b>\n\n"
            f"{_esc(user.first_name)} <b>{amount}</b> سانت از این گروه فرستاد به "
            f"<b>{_esc(dest_title)}</b>.\n"
            f"🧾 کارمزد: {int(fee)} سانت رفت تو خزانهٔ همین گروه\n"
            f"📦 رسید: {int(delivered)} سانت",
            parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        await context.bot.send_message(
            chat_id=dest_chat,
            text=(f"🔁 {_esc(user.first_name)} <b>{int(delivered)}</b> سانت از یه گروه دیگه "
                  f"آورد اینجا!"),
            parse_mode="HTML"
        )
    except Exception:
        pass


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: what the auto-handicap last did in a group, and why."""
    if not _owner_only(update):
        return
    parts = update.message.text.split()
    chat_id = int(parts[1]) if len(parts) > 1 and parts[1].lstrip('-').isdigit() else update.effective_chat.id
    rows = db.get_last_rebalance(chat_id, 25)
    if not rows:
        await update.message.reply_text("هنوز هیچ تنظیم خودکاری برای این گروه ثبت نشده.")
        return
    lines = [f"⚖️ آخرین تنظیمات خودکار تعادل (chat {chat_id}):", ""]
    for run_date, name, net, median, gb, ga, lb, la in rows:
        lines.append(
            f"• {run_date} — {_esc(name or '?')}: خالص {net:+.0f} (میانه {median:+.0f})\n"
            f"   رشد {gb:g}→{ga:g} | شانس {lb:g}→{la:g}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_shop_keyboard(user_id):
    rows = []
    items = [(nm, priced(pr, chat_id)) for nm, pr in SHOP_PRICES.items()]
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
    for name, base_price in SHOP_PRICES.items():
        price = priced(base_price, chat_id)
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
    base_price = SHOP_PRICES.get(item_name)
    if base_price is None:
        await query.answer("این آیتم تو فروشگاه نیست!", show_alert=True)
        return
    price = priced(base_price, chat_id)

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
    # The price used to simply vanish. It funds the bank's interest now - that is what
    # lets the bank pay a yield without minting a single centimetre of new size.
    db.treasury_add(chat_id, price, note=f"خرید {item_name}")

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
        reward = priced(BOSS_REWARD_BASE, chat_id) + dmg // 2
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
    pot = sum(p for _, _, _, p in entries)
    mine = next((t for uid, _, t, _p in entries if uid == user_id), 0)
    lines = [
        "🎟️ **لاتاری امشب**",
        f"💰 جایزه: {int(pot * (1 - LOTTERY_BURN_RATIO))} سانتی‌متر",
        f"🎫 قیمت هر بلیت: {priced(LOTTERY_TICKET_PRICE, chat_id)} سانت",
        f"🎫 بلیت‌های تو: {mine}",
        "",
        "قرعه‌کشی نیمه‌شب به وقت تهران. هرچی بلیت بیشتر، شانس بیشتر.",
    ]
    if entries:
        lines.append("\n👥 شرکت‌کننده‌ها:")
        for _uid, fname, t, _p in entries:
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

    cost = count * priced(LOTTERY_TICKET_PRICE, chat_id)
    _, _, buyer_perk = db.get_user(user.id, chat_id, user.username, user.first_name)
    if buyer_perk == "بدبیار":
        await query.answer("امروز پرک بدبیار 🚫 داری و نمی‌تونی بلیت لاتاری بخری!", show_alert=True)
        return
    if not db.try_deduct_size(user.id, chat_id, cost):
        size, _, _ = db.get_user(user.id, chat_id, None, None)
        await query.answer(f"پول کافی نداری! {count} بلیت {cost} سانته، تو {int(size)} داری.", show_alert=True)
        return
    # خرشانس buys odds, not size: the pot only ever grows by what was actually paid in,
    # so the extra entries change who is likely to win without minting any prize money.
    entries = count * 2 if buyer_perk == "خرشانس" else count
    try:
        db.buy_lottery_tickets(chat_id, tehran_today_str(), user.id, user.first_name, entries)
    except Exception:
        db.update_size(user.id, chat_id, cost)
        raise

    if entries != count:
        await query.answer(f"{count} بلیت خریدی و خرشانس 🎰 کردش {entries} تا! 🎟️", show_alert=True)
    else:
        await query.answer(f"{count} بلیت خریدی! 🎟️")
    text, keyboard = build_lottery_view(chat_id, user.id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception:
        pass


async def draw_lottery(context: ContextTypes.DEFAULT_TYPE, draw_date):
    """Midnight draw. The draw itself lives in lottery.draw so the admin panel runs the
    exact same code rather than a second copy of it; this only announces the outcome."""
    for chat_id in db.get_lottery_chats(draw_date):
        try:
            result = lottery.draw(chat_id, draw_date)
            if not result:
                continue
            await context.bot.send_message(chat_id=chat_id, text=lottery.render_result(result))
            await announce_achievements(context, chat_id, result["winner_name"],
                                        award(result["winner_id"], chat_id, 'lottery_winner'))
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
                rubble = 0
                for uid, _n, size in players:
                    cut = max(1, int((size or 0) * 0.08))
                    if (size or 0) > 0 and db.try_deduct_size(uid, chat_id, cut):
                        rubble += cut
                # What the earthquake swallows is added to the vault rather than deleted.
                db.treasury_add(chat_id, rubble, note="زلزله")
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


# ==========================================================================
# Owner-only moderation. Deliberately absent from BOT_COMMANDS: putting these in
# the / menu would advertise their existence to every player in every group.
# They also only answer in the bot's private chat, and to anyone else they are
# silent no-ops rather than "you are not allowed" - a refusal is itself a tell.
# ==========================================================================

OWNER_ID = 812712003

MOD_LIMITS = (0.0, 5.0)


def _esc(value):
    """Escape a user-controlled string for parse_mode=HTML.

    These messages embed names and usernames verbatim. Markdown was unusable here: a
    single '_' in a username (@Reza_Jr) opens an italic entity that never closes and
    Telegram rejects the whole message with "Can't parse entities" - which is exactly
    how /luck was failing. HTML escaping is total (only &, <, >) so no name can break it."""
    return html.escape(str(value), quote=False)


async def _reply_chunks(update, lines, sep="\n"):
    """Send lines as HTML, split across messages so a big group can't blow past
    Telegram's 4096-character limit."""
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + len(sep) > 3500:
            chunks.append(current)
            current = ""
        current += line + sep
    if current:
        chunks.append(current)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="HTML")


def _owner_only(update):
    """True only for the owner in the bot's own DM. Anything else returns False and the
    caller returns without replying at all."""
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == OWNER_ID and chat.id > 0)


async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/groups` - list the chat_ids the other admin commands take."""
    if not _owner_only(update):
        return
    lines = ["👥 گروه‌های ثبت‌شده:\n"]
    for chat_id in db.get_all_chats():
        try:
            chat = await context.bot.get_chat(chat_id)
            title = chat.title or str(chat_id)
        except Exception:
            title = "(دسترسی ندارم)"
        players = len(db.get_group_modifiers(chat_id))
        lines.append(f"<code>{chat_id}</code>\n  {_esc(title)} — {players} بازیکن")
    lines.append("\nبرای دیدن ضریب‌ها: <code>/luck &lt;chat_id&gt;</code>")
    await _reply_chunks(update, lines)


async def luck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/luck <chat_id>` - the whole group's dials in one table."""
    if not _owner_only(update):
        return
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "استفاده: <code>/luck &lt;chat_id&gt;</code>\nبرای دیدن لیست گروه‌ها: /groups",
            parse_mode="HTML")
        return
    try:
        chat_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("chat_id باید عدد باشه. /groups رو بزن.")
        return

    rows = db.get_group_modifiers(chat_id)
    if not rows:
        await update.message.reply_text("این گروه بازیکنی نداره یا chat_id اشتباهه.")
        return

    lines = [f"🎛 ضریب‌های گروه <code>{chat_id}</code>", "(۱.۰ = دست‌نخورده)\n"]
    for uid, name, username, size, luck, growth in rows:
        flag = "" if (luck == 1.0 and growth == 1.0) else "  ⚠️"
        handle = f"@{_esc(username)}" if username else str(uid)
        lines.append(
            f"<code>{uid}</code> {_esc(name)} ({handle}){flag}\n"
            f"    سایز {int(size or 0)} | دزدی ×{luck:g} | رشد ×{growth:g}"
        )
    lines.append(
        "\n<code>/setluck &lt;chat_id&gt; &lt;user_id&gt; &lt;عدد&gt;</code>\n"
        "<code>/setgrowth &lt;chat_id&gt; &lt;user_id&gt; &lt;عدد&gt;</code>\n"
        f"محدوده: {MOD_LIMITS[0]} تا {MOD_LIMITS[1]} — ۱ یعنی عادی، ۰.۳ یعنی شدیداً کم"
    )
    await _reply_chunks(update, lines)


async def _set_modifier_cmd(update, context, column, label):
    if not _owner_only(update):
        return
    parts = update.message.text.split()
    if len(parts) < 4:
        name = 'setluck' if column == 'theft_luck' else 'setgrowth'
        await update.message.reply_text(
            f"استفاده: <code>/{name} &lt;chat_id&gt; &lt;user_id&gt; &lt;عدد&gt;</code>",
            parse_mode="HTML")
        return
    try:
        chat_id = int(parts[1])
        target_id = int(parts[2])
        value = float(parts[3])
    except ValueError:
        await update.message.reply_text("chat_id و user_id باید عدد باشن و ضریب هم یه عدد اعشاری.")
        return
    if not (MOD_LIMITS[0] <= value <= MOD_LIMITS[1]) or value != value:
        await update.message.reply_text(f"ضریب باید بین {MOD_LIMITS[0]} و {MOD_LIMITS[1]} باشه.")
        return

    if not db.set_modifier(target_id, chat_id, column, value):
        await update.message.reply_text("این کاربر تو این گروه پیدا نشد. /luck رو چک کن.")
        return
    # Pin it: a hand-set dial is a decision, and the nightly auto-handicap must not
    # quietly walk it back a few hours later. Setting a dial back to exactly 1.0
    # releases the pin and hands the player back to the automatic system.
    db.set_dials_locked(target_id, chat_id, value != 1.0)

    info = db.get_user_info(target_id, chat_id)
    name = info[0] if info else str(target_id)
    note = "عادی" if value == 1.0 else ("کمتر از عادی 🔻" if value < 1.0 else "بیشتر از عادی 🔺")
    await update.message.reply_text(
        f"✅ {label} برای {_esc(name)} روی ×{value:g} تنظیم شد ({note}).\n"
        f"هیچ اعلانی تو گروه نمی‌ره و خودش خبردار نمی‌شه."
    )


async def setluck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_modifier_cmd(update, context, 'theft_luck', "شانس دزدی")


async def setgrowth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_modifier_cmd(update, context, 'growth_mult', "ضریب رشد روزانه")


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
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).post_init(setup_commands).build()

    app.job_queue.run_daily(midnight_tasks, time=time(hour=0, minute=0, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_daily(spawn_daily_bosses, time=time(hour=BOSS_SPAWN_HOUR, minute=0, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_daily(decree_offer_job, time=time(hour=DECREE_OFFER_HOUR, minute=DECREE_OFFER_MINUTE, second=0, tzinfo=IRAN_TZ))
    # 00:20 Tehran: after midnight_tasks has settled the lottery, expired bosses and
    # collected the crown's tax, so the handicap reads a closed, complete day.
    app.job_queue.run_daily(economy_tick_job, time=time(hour=0, minute=5, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_daily(bank_interest_job, time=time(hour=0, minute=10, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_daily(collect_loans_job, time=time(hour=0, minute=15, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_daily(auto_handicap_job, time=time(hour=0, minute=20, second=0, tzinfo=IRAN_TZ))
    app.job_queue.run_repeating(random_event_job, interval=RANDOM_EVENT_INTERVAL_SECONDS, first=300)
    app.job_queue.run_once(recover_stuck_pvp_matches, when=5)
    app.job_queue.run_once(recover_expired_consensus, when=7)
    app.job_queue.run_once(recover_pending_lotteries, when=9)
    app.job_queue.run_once(recover_decree_offer, when=12)

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
    app.add_handler(MessageHandler(cmd(r'^/(wr|winrate|stats)\b'), winrate_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(king|shah)\b'), king_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(hamsar|malake)\b'), consort_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(khianat|khiyanat)\b'), betray_cmd))
    app.add_handler(MessageHandler(cmd(r'^/talagh\b'), divorce_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(dozdi|steal)\b'), steal_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(shop|forushgah)\b'), shop_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(lottery|lotari)\b'), lottery_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(bank|banak)\b'), bank_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(variz|deposit)\b'), deposit_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(bardasht|withdraw)\b'), withdraw_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(sarghat|heist)\b'), heist_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(nozul|nozool)\b'), nozul_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(vam|loan)\b'), vam_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(bedehi|debts)\b'), debts_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(pardakht|repay)\b'), repay_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(enteghal|transfer)\b'), transfer_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(etebar|credit)\b'), credit_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(farman|decree)\b'), decree_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(farmanha|decrees)\b'), decree_history_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(eghtesad|economy)\b'), economy_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(hokm|nezami|martial)\b'), martial_law_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(dalghak|jester)\b'), jesters_cmd))
    app.add_handler(MessageHandler(cmd(r'^/(ach|achievements|neshan)\b'), achievements_cmd))

    # Owner-only, private-chat-only; intentionally not in BOT_COMMANDS (see there).
    app.add_handler(MessageHandler(cmd(r'^/groups\b'), groups_cmd))
    app.add_handler(MessageHandler(cmd(r'^/luck\b'), luck_cmd))
    app.add_handler(MessageHandler(cmd(r'^/setluck\b'), setluck_cmd))
    app.add_handler(MessageHandler(cmd(r'^/setgrowth\b'), setgrowth_cmd))
    app.add_handler(MessageHandler(cmd(r'^/balance\b'), balance_cmd))

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
    app.add_handler(CallbackQueryHandler(loan_accept_callback, pattern=r'^loanok_'))
    app.add_handler(CallbackQueryHandler(transfer_callback, pattern=r'^xfer_'))
    app.add_handler(CallbackQueryHandler(decree_callback, pattern=r'^decree_'))
    
    app.add_handler(InlineQueryHandler(inline_query))
    
    print("Bot is running...")
    app.run_polling()
