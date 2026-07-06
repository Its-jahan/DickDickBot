import logging
import random
import datetime
from datetime import time
import math
import asyncio
from telegram import Update, Chat, InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, InlineQueryHandler, CallbackQueryHandler
from uuid import uuid4

import db

async def midnight_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = db.get_all_chats()
    msg = "⏰ وقتشه دودولاتون رو بلند کنید!\nروز جدید شروع شده و می‌تونید دوباره سایزتون رو رشد بدید."
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=msg)
        except Exception as e:
            logging.error(f"Failed to send reminder to {cid}: {e}")

def get_dick_name(size):
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

# Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = '8802494355:AAFYiGyKph3R8wLiZoeDsELOPx07Q9ZvuVw'

# Memory store for active challenges
# Format: challenges[target_user_id] = challenger_user_id
challenges = {}

PERK_DESCRIPTIONS = {
    "عادی": "شما امروز پرک خاصی نگرفتید (عادی 👤).",
    "جاکش": "شما پرک **جاکش 🤡** گرفتید! (از بردهای چالش ۵۰٪ کمتر سایز میگیرید).",
    "کص‌کش": "شما پرک **کص‌کش 😈** گرفتید! (از بردهای چالش ۲۰٪ بیشتر سایز میگیرید).",
    "حرومزاده": "شما پرک **حرومزاده 🥶** گرفتید! (دودول شما یخ زد و امروز نمی‌تونید چالش بدید یا بگیرید).",
    "لاشی": "شما پرک **لاشی 🦅** گرفتید! (اگه تو چالش ببازید ۵۰٪ کمتر سایز از دست میدید).",
    "خایه‌مال": "شما پرک **خایه‌مال 🤲** گرفتید! (+۵ سانت هدیه بلافاصله اضافه شد).",
    "کون‌گشاد": "شما پرک **کون‌گشاد 🦥** گرفتید! (تاس‌های شما همیشه ۱ دونه کمتر محاسبه میشه).",
    "زن جنده": "به صورت رندوم یکی از اعضای گروه رو انتخاب می‌کنه و اگه سایزش بیشتر از ۱۰ باشه ۵ سانت از اون کم می‌کنه و به تو اضافه می‌کنه.",
    "جقی": "موقع چالش ممکنه عدد تاس رو رندوم به شدت بالا یا پایین ببره!"
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
        if len(parts) > 1:
            target_username = parts[1]
            row = db.find_user_by_username(target_username, chat_id)
            if row:
                target_user_id, target_first_name, _ = row
            
    return target_user_id, target_first_name

def drop_item(user_id, chat_id):
    # 30% chance to drop an item
    if random.random() > 0.3:
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
    
    today_str = datetime.date.today().isoformat()
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
    
    items = db.get_inventory(user.id, chat_id)
    active_item = db.get_user_active_item(user.id, chat_id)
    
    if not items and not active_item:
        await update.message.reply_text("کیف پول شما در این گروه خالی است!")
        return
        
    msg = "🎒 **آیتم‌های شما در این گروه:**\n\n"
    keyboard = []
    
    for item_name, qty in items:
        msg += f"- {item_name}: {qty} عدد\n"
        keyboard.append([InlineKeyboardButton(f"استفاده از {item_name}", callback_data=f"useitem_{user.id}_{item_name}")])
        
    if active_item:
        msg += f"\n🔥 آیتم فعال برای چالش بعدی: **{active_item}**"
        
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
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
        
    challenge_items = ["کاندوم", "شیر موز", "سوزن", "طلسم", "اسپری"]
    direct_items = ["ویاگرا", "قرص اورژانسی", "زعفرون"]
    
    if item_name in direct_items:
        await query.answer(f"برای استفاده از {item_name} باید تو گروه بنویسی:\n/use {item_name} @username", show_alert=True)
        return
        
    if item_name in challenge_items:
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
        items = db.get_inventory(user.id, chat_id)
        msg = "🎒 **آیتم‌های شما در این گروه:**\n\n"
        keyboard = []
        for i_name, qty in items:
            msg += f"- {i_name}: {qty} عدد\n"
            keyboard.append([InlineKeyboardButton(f"استفاده از {i_name}", callback_data=f"useitem_{user.id}_{i_name}")])
        msg += f"\n🔥 آیتم فعال برای چالش بعدی: **{item_name}**"
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await query.edit_message_text(msg, reply_markup=reply_markup)

async def use_item_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    db.get_user(user.id, chat_id, user.username, user.first_name)
    
    text = update.message.text
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("استفاده: `/use نام_آیتم`\nمثال: `/use کاندوم`")
        return
        
    # item name might be multiple words
    item_name = " ".join([p for p in parts[1:] if not p.startswith('@')])
    
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
        
    challenge_items = ["کاندوم", "شیر موز", "سوزن", "طلسم", "اسپری"]
    direct_items = ["ویاگرا", "قرص اورژانسی", "زعفرون"]
    
    if item_name in challenge_items:
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
            
    elif item_name in direct_items:
        target_user_id, target_first_name = get_target_user(update, text, chat_id)
        if not target_user_id:
            await update.message.reply_text("باید روی یک نفر ریپلای کنید یا یوزرنیمش رو منشن کنید!")
            return
            
        if item_name == "ویاگرا":
            db.update_size(target_user_id, chat_id, 40)
            msg = f"شما با ویاگرا ۴۰ سانت به {target_first_name} اضافه کردید!"
        elif item_name == "قرص اورژانسی":
            db.update_size(target_user_id, chat_id, -40)
            msg = f"شما با قرص اورژانسی ۴۰ سانت از {target_first_name} کم کردید!"
        elif item_name == "زعفرون":
            loss = random.randint(50, 150)
            db.update_size(target_user_id, chat_id, -loss)
            msg = f"شما با زعفرون {loss} سانت از {target_first_name} کم کردید!"
            
        db.use_inventory(user.id, chat_id, item_name)
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("آیتم نامشخص.")

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    rows = db.get_top_users(chat_id, 10)
    
    if not rows:
        await update.message.reply_text("هنوز هیچکس در این گروه در بازی شرکت نکرده است!")
        return
        
    msg = "🏆 برترین‌های این گروه:\n\n"
    for i, (first_name, size) in enumerate(rows, 1):
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
        
    await update.message.reply_text(msg)

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    db.track_chat(chat_id)
    text = update.message.text
    
    db.get_user(user.id, chat_id, user.username, user.first_name)
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
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("لطفا یک مقدار معتبر وارد کنید.")
        return
        
    user_size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    if user_size < amount:
        await update.message.reply_text("شما به اندازه کافی سانتی‌متر برای اهدا در این گروه ندارید!")
        return
        
    db.update_size(user.id, chat_id, -amount)
    db.update_size(target_user_id, chat_id, amount)
    
    new_size, _, _ = db.get_user(user.id, chat_id, user.username, user.first_name)
    await update.message.reply_text(f"شما {int(amount)} سانتی‌متر به {target_first_name} اهدا کردید!\nسایز جدید شما: {int(new_size)} سانتی‌متر.")

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
        
    keyboard = [[InlineKeyboardButton("پذیرش چالش ⚔️", callback_data=f"chal_{user.id}_{bet}")]]
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
    if len(data) != 3 or data[0] != 'chal':
        return
        
    challenger_id = int(data[1])
    bet = int(data[2])
    
    if user.id == challenger_id:
        await query.answer("شما نمی‌توانید چالش خودتان را بپذیرید!", show_alert=True)
        return
        
    challenger_row = db.get_user(challenger_id, chat_id, None, None)
    if not challenger_row or challenger_row[0] < bet:
        await query.answer("شروع‌کننده چالش در حال حاضر سایز کافی ندارد!", show_alert=True)
        return
        
    if challenger_row[2] == "حرومزاده":
        await query.answer("کیر شروع‌کننده امروز فیریز شده (پرک حرومزاده)! نمی‌تواند چالش انجام دهد.", show_alert=True)
        return
        
    user_size, _, user_perk = db.get_user(user.id, chat_id, None, None)
    if user_perk == "حرومزاده":
        await query.answer("شما امروز پرک حرومزاده 🥶 رو دارید و کیرتون فیریز شده! نمی‌تونید چالش رو بپذیرید.", show_alert=True)
        return
        
    if user_size < bet:
        await query.answer(f"شما حداقل {bet} سانتی‌متر برای شرکت در این گروه نیاز دارید!", show_alert=True)
        return
        
    challenger_info = db.get_user_info(challenger_id, chat_id)
    challenger_name = challenger_info[0] if challenger_info else "ناشناس"
        
    await query.answer("چالش پذیرفته شد!")
    await query.edit_message_text(f"⚔️ مسابقه بین {challenger_name} و {user.first_name} بر سر {bet} سانتی‌متر آغاز شد!\nدر حال ریختن تاس...")
    
    val1 = random.randint(1, 6)
    val2 = random.randint(1, 6)
    
    await asyncio.sleep(2)
    
    c_perk = challenger_row[2]
    
    # Active Items
    c_item = db.get_user_active_item(challenger_id, chat_id)
    u_item = db.get_user_active_item(user.id, chat_id)
    
    # Clear active items
    db.clear_user_active_item(challenger_id, chat_id)
    db.clear_user_active_item(user.id, chat_id)
    
    # Process Item Interactions
    # Spray: reduces opponent dice by 1
    if c_item == "اسپری": val2 = max(1, val2 - 1)
    if u_item == "اسپری": val1 = max(1, val1 - 1)
    
    # Apply Dice Perks
    if c_perk == "کون‌گشاد": val1 = max(1, val1 - 1)
    elif c_perk == "زن جنده": val1 = min(6, val1 + 1)
        
    if user_perk == "کون‌گشاد": val2 = max(1, val2 - 1)
    elif user_perk == "زن جنده": val2 = min(6, val2 + 1)
    
    msg_item_log = ""
    if c_item or u_item:
        msg_item_log += "\n🎒 **گزارش آیتم‌ها:**\n"
        
    if c_item == "اسپری": msg_item_log += f"- {challenger_name} اسپری زد و تاس حریف کم شد.\n"
    if u_item == "اسپری": msg_item_log += f"- {user.first_name} اسپری زد و تاس حریف کم شد.\n"
    
    # Needle pierces condom
    c_condom = True if c_item == "کاندوم" else False
    u_condom = True if u_item == "کاندوم" else False
    
    if c_item == "سوزن" and u_condom:
        u_condom = False
        msg_item_log += f"- {challenger_name} سوزن داشت و کاندوم {user.first_name} پاره شد!\n"
    if u_item == "سوزن" and c_condom:
        c_condom = False
        msg_item_log += f"- {user.first_name} سوزن داشت و کاندوم {challenger_name} پاره شد!\n"
        
    # Shield blocks milk
    c_milk = True if c_item == "شیر موز" else False
    u_milk = True if u_item == "شیر موز" else False
    
    if c_item == "طلسم" and u_milk:
        u_milk = False
        msg_item_log += f"- {challenger_name} با طلسم اثر شیر موز {user.first_name} را باطل کرد!\n"
    if u_item == "طلسم" and c_milk:
        c_milk = False
        msg_item_log += f"- {user.first_name} با طلسم اثر شیر موز {challenger_name} را باطل کرد!\n"

    winner_id, loser_id = None, None
    winner_name, loser_name = "", ""
    winner_perk, loser_perk = "", ""
    winner_condom, loser_condom = False, False
    winner_milk, loser_milk = False, False

    if val1 > val2:
        winner_id, loser_id = challenger_id, user.id
        winner_name, loser_name = challenger_name, user.first_name
        winner_perk, loser_perk = c_perk, user_perk
        winner_condom, loser_condom = c_condom, u_condom
        winner_milk, loser_milk = c_milk, u_milk
        msg = f"⚔️ مسابقه بین {challenger_name} و {user.first_name}\n🎲 تاس {challenger_name}: {val1}\n🎲 تاس {user.first_name}: {val2}\n\n🎉 {challenger_name} برنده چالش شد!"
    elif val2 > val1:
        winner_id, loser_id = user.id, challenger_id
        winner_name, loser_name = user.first_name, challenger_name
        winner_perk, loser_perk = user_perk, c_perk
        winner_condom, loser_condom = u_condom, c_condom
        winner_milk, loser_milk = u_milk, c_milk
        msg = f"⚔️ مسابقه بین {challenger_name} و {user.first_name}\n🎲 تاس {challenger_name}: {val1}\n🎲 تاس {user.first_name}: {val2}\n\n🎉 {user.first_name} برنده چالش شد!"
    else:
        msg = f"⚔️ مسابقه بین {challenger_name} و {user.first_name}\n🎲 تاس {challenger_name}: {val1}\n🎲 تاس {user.first_name}: {val2}\n\n🤝 مساوی شد! هیچکس چیزی از دست نداد."
        msg += msg_item_log
        msg += "\n\nبرای ریمچ هر دو طرف باید دکمه زیر رو بزنن:"
        keyboard = [[InlineKeyboardButton("🔄 موافقم با ریمچ!", callback_data=f"rematch_{challenger_id}_{user.id}_{bet}")]]
        await query.edit_message_text(text=msg, reply_markup=InlineKeyboardMarkup(keyboard))
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

    db.update_size(loser_id, chat_id, -loser_loss)
    db.update_size(winner_id, chat_id, winner_gain)
    
    msg += f"\n💰 شرط اصلی: {bet} سانت"
    msg += msg_item_log
    
    if winner_perk in ["کص‌کش", "جاکش"]:
        msg += f"\n({winner_perk} باعث شد برنده {winner_gain} سانت گیرش بیاد)"
    if loser_perk == "لاشی" and loser_loss > 0:
        msg += f"\n(لاشی باعث شد بازنده فقط {loser_loss} سانت از دست بده)"
    
    # Fetch new stats
    winner_size, _, _ = db.get_user(winner_id, chat_id, None, None)
    loser_size, _, _ = db.get_user(loser_id, chat_id, None, None)
    
    w_dname = get_dick_name(winner_size)
    l_dname = get_dick_name(loser_size)
    
    msg += f"\n\n📈 {w_dname} {winner_name} شد {int(winner_size)} سانتی‌متر!"
    msg += f"\n📉 {l_dname} {loser_name} شد {int(loser_size)} سانتی‌متر!"
    
    await query.edit_message_text(text=msg)

# Track rematch agreements: key = "p1_p2_bet", value = set of user_ids who agreed
rematch_agreements = {}

async def rematch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    
    chat_id = resolve_chat_id(query)
    if not chat_id:
        await query.answer("⚠️ خطا!", show_alert=True)
        return
    
    data = query.data.split('_')
    if len(data) != 4 or data[0] != 'rematch':
        return
        
    p1_id = int(data[1])
    p2_id = int(data[2])
    bet = int(data[3])
    
    if user.id != p1_id and user.id != p2_id:
        await query.answer("شما عضو این چالش نیستید!", show_alert=True)
        return
    
    key = f"{p1_id}_{p2_id}_{bet}_{chat_id}"
    
    if key not in rematch_agreements:
        rematch_agreements[key] = set()
    
    rematch_agreements[key].add(user.id)
    
    if len(rematch_agreements[key]) < 2:
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
        keyboard = [[InlineKeyboardButton("🔄 موافقم با ریمچ!", callback_data=f"rematch_{p1_id}_{p2_id}_{bet}")]]
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
    
    await query.answer("هر دو موافقت کردن! تاس‌ها دوباره ریخته میشه...")
    await query.edit_message_text(f"🔄 ریمچ بین {p1_name} و {p2_name}!\nدر حال ریختن تاس...")
    
    await asyncio.sleep(2)
    
    val1 = random.randint(1, 6)
    val2 = random.randint(1, 6)
    
    if val1 > val2:
        winner_id, loser_id = p1_id, p2_id
        winner_name, loser_name = p1_name, p2_name
    elif val2 > val1:
        winner_id, loser_id = p2_id, p1_id
        winner_name, loser_name = p2_name, p1_name
    else:
        # Tie again!
        msg = f"🔄 ریمچ بین {p1_name} و {p2_name}\n🎲 تاس {p1_name}: {val1}\n🎲 تاس {p2_name}: {val2}\n\n🤝 دوباره مساوی شد!\n\nبرای ریمچ هر دو طرف باید دکمه زیر رو بزنن:"
        keyboard = [[InlineKeyboardButton("🔄 موافقم با ریمچ!", callback_data=f"rematch_{p1_id}_{p2_id}_{bet}")]]
        await query.edit_message_text(text=msg, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    db.update_size(loser_id, chat_id, -bet)
    db.update_size(winner_id, chat_id, bet)
    
    winner_size, _, _ = db.get_user(winner_id, chat_id, None, None)
    loser_size, _, _ = db.get_user(loser_id, chat_id, None, None)
    
    w_dname = get_dick_name(winner_size)
    l_dname = get_dick_name(loser_size)
    
    msg = f"🔄 ریمچ بین {p1_name} و {p2_name}\n🎲 تاس {p1_name}: {val1}\n🎲 تاس {p2_name}: {val2}\n\n🎉 {winner_name} برنده ریمچ شد!"
    msg += f"\n💰 شرط: {bet} سانت"
    msg += f"\n\n📈 {w_dname} {winner_name} شد {int(winner_size)} سانتی‌متر!"
    msg += f"\n📉 {l_dname} {loser_name} شد {int(loser_size)} سانتی‌متر!"
    
    await query.edit_message_text(text=msg)

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
    today_str = datetime.date.today().isoformat()
    if last_grown == today_str:
        await query.answer("شما امروز دودول خود را در این گروه رشد داده‌اید! تا فردا صبر کنید.", show_alert=True)
        return
        
    if current_size < 50:
        delta = random.randint(-5, 20)
    elif current_size < 150:
        delta = random.randint(-3, 20)
    else:
        delta = random.randint(-6, 10)
        
    db.update_size(user.id, chat_id, delta, today_str)
    
    current_size = current_size + delta
    
    perk_pool = [
        "عادی", "عادی", "عادی", "عادی",
        "جاکش", "کص‌کش", "حرومزاده", "لاشی", "خایه‌مال", "کون‌گشاد", "زن جنده", "جقی"
    ]
    new_perk = random.choice(perk_pool)
    db.set_user_perk(user.id, chat_id, new_perk)
    if new_perk == "خایه‌مال":
        current_size += 5
        db.update_size(user.id, chat_id, 5)

    dropped_item = drop_item(user.id, chat_id)
    item_msg = f"\n🎁 شما یک آیتم پیدا کردید: **{dropped_item}**\n📝 توضیحات: {ITEM_DESCRIPTIONS.get(dropped_item, '')}" if dropped_item else ""

    verb = "بزرگ شد" if delta >= 0 else "کوچک شد"
    d_name = get_dick_name(current_size)
    msg = f"🍆 {d_name} {user.first_name} {abs(delta)} سانتی‌متر {verb}!\nاندازه فعلی: {int(current_size)} سانتی‌متر.\n\n✨ پرک امروز: {PERK_DESCRIPTIONS.get(new_perk, '')}{item_msg}"
    
    await query.answer(f"{d_name} شما تغییر کرد!")
    try:
        await query.edit_message_text(msg)
    except:
        pass

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.inline_query.from_user
    query = update.inline_query.query.strip()
    
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("پذیرش چالش ⚔️", callback_data=f"chal_{user.id}_{bet}")]])
    )
    
    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="🌱 رشد دادن دودول",
            description="سایز دودولت رو تو این گروه بزرگ کن!",
            input_message_content=InputTextMessageContent(f"🌱 {user.first_name} می‌خواد دودولش رو بماله..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بمالش تا بزرگ شه 💦", callback_data=f"grow_self_{user.id}")]])
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="🏆 برترین‌های گروه",
            description="نمایش لیدربرد این گروه",
            input_message_content=InputTextMessageContent("🏆 در حال بارگذاری لیدربرد گروه..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نمایش برترین‌ها 👁️", callback_data=f"showtop_{user.id}")]])
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="📏 نمایش سایز من",
            description="سایز دودولت رو تو این گروه ببین",
            input_message_content=InputTextMessageContent(f"📏 {user.first_name} می‌خواد سایز دودولش رو ببینه..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نمایش سایز 👁️", callback_data=f"showsize_{user.id}")]])
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="🎒 آیتم‌های من",
            description="آیتم‌هات رو تو این گروه ببین",
            input_message_content=InputTextMessageContent(f"🎒 {user.first_name} می‌خواد کیف پولش رو چک کنه..."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نمایش آیتم‌ها 👁️", callback_data=f"showinv_{user.id}")]])
        ),
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
    rows = db.get_top_users(chat_id, 10)
    
    if not rows:
        await query.edit_message_text("هنوز هیچکس در این گروه در بازی شرکت نکرده است!")
        return
        
    msg = "🏆 برترین‌های این گروه:\n\n"
    for i, (first_name, size) in enumerate(rows, 1):
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
    items = db.get_inventory(user.id, chat_id)
    active_item = db.get_user_active_item(user.id, chat_id)
    
    if not items and not active_item:
        await query.edit_message_text("🎒 کیف پول شما در این گروه خالی است!")
        return
        
    msg = "🎒 **آیتم‌های شما در این گروه:**\n\n"
    keyboard = []
    for item_name, qty in items:
        desc = ITEM_DESCRIPTIONS.get(item_name, '')
        msg += f"- {item_name}: {qty} عدد\n  └ {desc}\n"
        keyboard.append([InlineKeyboardButton(f"استفاده از {item_name}", callback_data=f"useitem_{user.id}_{item_name}")])
        
    if active_item:
        msg += f"\n🔥 آیتم فعال برای چالش بعدی: **{active_item}**"
        
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await query.edit_message_text(msg, reply_markup=reply_markup)

if __name__ == '__main__':
    db.init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.job_queue.run_daily(midnight_reminder, time=time(hour=0, minute=0, second=0))
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', start))
    
    app.add_handler(MessageHandler(filters.Regex(r'^/(dick|grow|d)\b'), dick))
    app.add_handler(MessageHandler(filters.Regex(r'^/(top|t)\b'), top))
    app.add_handler(MessageHandler(filters.Regex(r'^/(donate|dd)\b'), donate))
    app.add_handler(MessageHandler(filters.Regex(r'^/(challenge|c)\b'), challenge))
    app.add_handler(MessageHandler(filters.Regex(r'^/(inv|inventory|i)\b'), inventory_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^/(use|u)\b'), use_item_cmd))
    
    app.add_handler(CallbackQueryHandler(accept_challenge_callback, pattern=r'^chal_'))
    app.add_handler(CallbackQueryHandler(rematch_callback, pattern=r'^rematch_'))
    app.add_handler(CallbackQueryHandler(grow_callback, pattern=r'^grow_self_'))
    app.add_handler(CallbackQueryHandler(use_item_callback, pattern=r'^useitem_'))
    app.add_handler(CallbackQueryHandler(show_top_callback, pattern=r'^showtop_'))
    app.add_handler(CallbackQueryHandler(show_size_callback, pattern=r'^showsize_'))
    app.add_handler(CallbackQueryHandler(show_inv_callback, pattern=r'^showinv_'))
    
    app.add_handler(InlineQueryHandler(inline_query))
    
    print("Bot is running...")
    app.run_polling()
