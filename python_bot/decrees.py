"""The royal decrees: 100 that help the group and cost the king, 100 the other way round.

The whole system turns on one deliberate asymmetry. A **bad** decree makes the king
personally richer and damages the economy — it prints money, levies the population,
raids the treasury, or throttles everyone else's growth. A **good** decree repairs the
economy and is paid for out of the king's own pocket. So the crown is permanently
tempted, and virtue is permanently expensive.

That is not a balance bug, it is the game: the king only holds the crown while he is the
biggest player, so every honest decree pushes him closer to losing it, and every corrupt
one raises `unrest` until the group throws him out. Ruling well is supposed to be a way
to lose power slowly, and ruling badly a way to lose it suddenly.

Effect keys (all optional, all applied by db.apply_decree in one transaction):

    mint              flat size created from nothing and handed to the king. The ONLY
                      thing in the game that increases the money supply out of thin air,
                      which is exactly why it is reserved for the worst decrees.
    burn_king         fraction of the king's size destroyed outright (removes supply)
    treasury_to_king  fraction of the group treasury moved to the king
    king_to_treasury  fraction of the king's size moved to the treasury
    levy              fraction of EVERY other player's size taken for the king
    handout           flat size paid from the king to every other player
    relief            flat size paid from the king to players below `relief_below`
    inflation         delta on the price index
    unrest            delta on public anger
    fee_mult          multiplier applied to the group's fee dial
    interest_mult     multiplier applied to the deposit-interest dial
    growth_mult       multiplier applied to everyone's daily growth dial
"""

# ---------------------------------------------------------------- corrupt decrees
# King gets richer, everyone else pays for it one way or another.
BAD = [
    ("b001", "چاپ اسکناس شبانه", "ضرابخانه رو تا صبح سرِ کار گذاشتی.", {"mint": 120, "inflation": 0.22, "unrest": 6}),
    ("b002", "ضرابخانه دو شیفته", "کارگرها خوابشون نمی‌بره، تو خوابت می‌بره.", {"mint": 180, "inflation": 0.30, "unrest": 8}),
    ("b003", "سکهٔ کم‌عیار", "نصف مس، نصف ادعا.", {"mint": 90, "inflation": 0.18, "unrest": 5}),
    ("b004", "چاپ بی‌پشتوانه", "پشتوانه؟ اسم گربه‌مونه.", {"mint": 250, "inflation": 0.40, "unrest": 12}),
    ("b005", "اصلاح ارزی به سبک خودم", "یه صفر اضافه کردم، مشکل حل شد.", {"mint": 160, "inflation": 0.28, "unrest": 7}),
    ("b006", "وام بی‌بازگشت به خزانهٔ شخصی", "قرض گرفتم از خودم، به خودم.", {"mint": 140, "inflation": 0.24, "unrest": 9}),
    ("b007", "ذخیرهٔ استراتژیک شخصی", "برای روز مبادا. مبادای من.", {"mint": 200, "inflation": 0.32, "unrest": 10}),
    ("b008", "پول موازی", "دو تا واحد پول بهتر از یکیه.", {"mint": 110, "inflation": 0.26, "unrest": 8}),
    ("b009", "کاغذ ارزون شد", "فرصت رو از دست ندادم.", {"mint": 300, "inflation": 0.45, "unrest": 15}),
    ("b010", "تورم کنترل‌شده (کنترل با من)", "همه‌چی تحت کنترله. جیب من.", {"mint": 70, "inflation": 0.15, "unrest": 4}),

    ("b011", "مالیات تاج", "هرکی زیر این تاج نفس می‌کشه، اجاره می‌ده.", {"levy": 0.05, "unrest": 8}),
    ("b012", "مالیات هوا", "نفس کشیدن مشمول عوارض شد.", {"levy": 0.07, "unrest": 11}),
    ("b013", "خراج سالانه، امسال دو بار", "تقویم رو عوض کردم.", {"levy": 0.09, "unrest": 14}),
    ("b014", "عوارض عبور از گروه", "پیام می‌فرستی؟ عوارض بده.", {"levy": 0.04, "unrest": 6}),
    ("b015", "مالیات بر لبخند", "شاد بودن لوکسه.", {"levy": 0.03, "unrest": 5}),
    ("b016", "سهم پادشاه از هر معامله", "بی‌سر و صدا، از همه.", {"levy": 0.06, "unrest": 9}),
    ("b017", "مصادرهٔ اموال مشکوک", "مشکوک یعنی هرکی پول داره.", {"levy": 0.12, "unrest": 18}),
    ("b018", "مالیات بر سایهٔ رعیت", "سایه‌ات رو زمین منه.", {"levy": 0.08, "unrest": 12}),
    ("b019", "کمک داوطلبانهٔ اجباری", "داوطلب بشو، وگرنه.", {"levy": 0.10, "unrest": 15}),
    ("b020", "حق‌الحکومه", "چون هستم، پس می‌گیرم.", {"levy": 0.02, "unrest": 3}),

    ("b021", "برداشت از خزانه", "قرضه. قول می‌دم.", {"treasury_to_king": 0.15, "unrest": 6}),
    ("b022", "خالی کردن نصف خزانه", "نصفش که مال منه اصلاً.", {"treasury_to_king": 0.40, "unrest": 14}),
    ("b023", "هزینهٔ تشریفات سلطنتی", "تاج باید برق بزنه.", {"treasury_to_king": 0.10, "unrest": 4}),
    ("b024", "سفر رسمی به هیچ‌جا", "با هیئت همراه. همراهِ من.", {"treasury_to_king": 0.20, "unrest": 8}),
    ("b025", "بازسازی قصر", "سقف چکه می‌کرد. الان طلاست.", {"treasury_to_king": 0.25, "unrest": 10}),
    ("b026", "حقوق معوقهٔ پادشاه", "از روز اول تا حالا.", {"treasury_to_king": 0.30, "unrest": 12}),
    ("b027", "صندوق توسعهٔ شخصی", "توسعهٔ خودم هم توسعه‌ست.", {"treasury_to_king": 0.18, "unrest": 7}),
    ("b028", "پاداش عملکرد", "خودم به خودم دادم.", {"treasury_to_king": 0.12, "unrest": 5}),
    ("b029", "غارت آشکار خزانه", "دیگه حتی بهونه هم نیاوردم.", {"treasury_to_king": 0.50, "unrest": 20}),
    ("b030", "خرید اسب مخصوص", "اسب که این‌قدر گرون نیست... این یکی هست.", {"treasury_to_king": 0.08, "unrest": 3}),

    ("b031", "افزایش کارمزدها", "همه‌چی گرون‌تر شد، سهم من بیشتر.", {"fee_mult": 1.30, "treasury_to_king": 0.12, "unrest": 7}),
    ("b032", "کارمزد مضاعف", "دو برابر، بدون توضیح.", {"fee_mult": 1.50, "treasury_to_king": 0.15, "unrest": 11}),
    ("b033", "عوارض بانکی جدید", "بانک هم باید سهم بده.", {"fee_mult": 1.20, "treasury_to_king": 0.10, "unrest": 5}),
    ("b034", "کارمزد پنهان", "تو صورت‌حساب ننوشتیم.", {"fee_mult": 1.15, "treasury_to_king": 0.08, "unrest": 4}),
    ("b035", "تعرفهٔ سلطنتی", "اسمش قشنگه، جیبت رو خالی می‌کنه.", {"fee_mult": 1.40, "treasury_to_king": 0.14, "unrest": 9}),
    ("b036", "کارمزد روی کارمزد", "خلاقیت مالی.", {"fee_mult": 1.60, "treasury_to_king": 0.18, "unrest": 14}),
    ("b037", "حق ثبت هر تراکنش", "امضای من پول داره.", {"fee_mult": 1.25, "treasury_to_king": 0.09, "unrest": 6}),
    ("b038", "عوارض نگهداری خزانه", "خزانه رو من نگه می‌دارم، پس...", {"fee_mult": 1.18, "treasury_to_king": 0.11, "unrest": 5}),
    ("b039", "تعرفهٔ ورود به فروشگاه", "نگاه کردن هم پول داره.", {"fee_mult": 1.35, "treasury_to_king": 0.13, "unrest": 8}),
    ("b040", "کارمزد شناور (همیشه بالا)", "شناوره ولی فقط بالا می‌ره.", {"fee_mult": 1.45, "treasury_to_king": 0.16, "unrest": 10}),

    ("b041", "کاهش سود سپرده‌گذارها", "سودشون رو کم کردم، تفاوتش مال من.", {"interest_mult": 0.70, "treasury_to_king": 0.15, "unrest": 9}),
    ("b042", "توقف سود بانکی", "امسال سودی در کار نیست.", {"interest_mult": 0.50, "treasury_to_king": 0.20, "unrest": 14}),
    ("b043", "سود منفی برای پس‌انداز", "پول نگه داری، ضرر می‌کنی.", {"interest_mult": 0.55, "treasury_to_king": 0.18, "unrest": 13}),
    ("b044", "بازنگری در نرخ سود", "بازنگری یعنی کم شد.", {"interest_mult": 0.80, "treasury_to_king": 0.10, "unrest": 6}),
    ("b045", "سود فقط برای خواص", "خواص یعنی من.", {"interest_mult": 0.65, "treasury_to_king": 0.16, "unrest": 11}),
    ("b046", "تعلیق موقت سود (دائمی)", "موقت به مدت نامحدود.", {"interest_mult": 0.60, "treasury_to_king": 0.17, "unrest": 12}),
    ("b047", "سقف سود سپرده", "سقف رو گذاشتم رو زمین.", {"interest_mult": 0.75, "treasury_to_king": 0.12, "unrest": 7}),
    ("b048", "مالیات بر سود بانکی", "سود گرفتی؟ مالیاتش رو بده.", {"interest_mult": 0.85, "treasury_to_king": 0.14, "unrest": 8}),
    ("b049", "بستن باجهٔ سود", "باجه تعطیل شد.", {"interest_mult": 0.58, "treasury_to_king": 0.19, "unrest": 13}),
    ("b050", "سود به شرط سکوت", "هرکی حرف بزنه، سودش قطع.", {"interest_mult": 0.68, "treasury_to_king": 0.15, "unrest": 15}),

    ("b051", "کند کردن رشد رعیت", "بزرگ شدنشون به نفعم نیست.", {"growth_mult": 0.88, "treasury_to_king": 0.12, "unrest": 10}),
    ("b052", "جیرهٔ رشد", "روزی این‌قدر، نه بیشتر.", {"growth_mult": 0.85, "treasury_to_king": 0.14, "unrest": 12}),
    ("b053", "محدودیت تغذیه", "کمبود بودجه.", {"growth_mult": 0.90, "treasury_to_king": 0.10, "unrest": 8}),
    ("b054", "تعطیلی مزارع", "زمین‌ها رو فروختم.", {"growth_mult": 0.82, "mint": 80, "inflation": 0.14, "unrest": 14}),
    ("b055", "خشکسالی مصنوعی", "آب رو بستم.", {"growth_mult": 0.86, "treasury_to_king": 0.13, "unrest": 13}),
    ("b056", "سهمیه‌بندی رشد", "با کارت ملی.", {"growth_mult": 0.92, "treasury_to_king": 0.08, "unrest": 6}),
    ("b057", "مالیات بر رشد", "بزرگ شدی؟ پول بده.", {"growth_mult": 0.94, "levy": 0.04, "unrest": 9}),
    ("b058", "توقف پروژه‌های عمرانی", "بودجه‌ش رو لازم داشتم.", {"growth_mult": 0.89, "treasury_to_king": 0.16, "unrest": 11}),
    ("b059", "انحصار رشد برای دربار", "فقط دربار رشد می‌کنه.", {"growth_mult": 0.80, "treasury_to_king": 0.20, "unrest": 17}),
    ("b060", "کاهش نامحسوس رشد", "کسی متوجه نمی‌شه.", {"growth_mult": 0.95, "treasury_to_king": 0.07, "unrest": 4}),

    ("b061", "فروش مقام درباری", "لقب می‌فروشم، نقد.", {"levy": 0.05, "treasury_to_king": 0.10, "unrest": 10}),
    ("b062", "حراج القاب", "خان، بیگ، سردار — همه موجوده.", {"levy": 0.04, "mint": 60, "inflation": 0.10, "unrest": 8}),
    ("b063", "فروش معافیت مالیاتی", "پول بده، مالیات نده.", {"levy": 0.06, "unrest": 12}),
    ("b064", "اجارهٔ تاج برای عکس", "ساعتی حساب می‌کنم.", {"levy": 0.02, "treasury_to_king": 0.06, "unrest": 4}),
    ("b065", "فروش اطلاعات رعیت", "اعتبارسنجی همه رو فروختم.", {"treasury_to_king": 0.14, "unrest": 13}),
    ("b066", "رشوهٔ رسمی", "رسمیش کردم که راحت‌تر باشه.", {"levy": 0.07, "unrest": 14}),
    ("b067", "حق دسترسی به پادشاه", "نوبت گرفتن پول داره.", {"levy": 0.03, "unrest": 6}),
    ("b068", "فروش زمین‌های عمومی", "عمومی بود، شخصی شد.", {"mint": 130, "inflation": 0.20, "unrest": 12}),
    ("b069", "خصوصی‌سازی خزانه", "خصوصی یعنی مالِ من.", {"treasury_to_king": 0.35, "unrest": 18}),
    ("b070", "قرارداد محرمانه", "جزئیاتش محرمانه‌ست. مبلغش هم.", {"treasury_to_king": 0.22, "levy": 0.03, "unrest": 11}),

    ("b071", "جنگ تبلیغاتی", "دشمن خیالی، هزینهٔ واقعی.", {"treasury_to_king": 0.18, "mint": 70, "inflation": 0.12, "unrest": 9}),
    ("b072", "بسیج اجباری", "همه باید کمک کنن.", {"levy": 0.08, "growth_mult": 0.93, "unrest": 15}),
    ("b073", "هزینهٔ دفاعی", "دفاع از تاج، در برابر مردم.", {"levy": 0.06, "treasury_to_king": 0.12, "unrest": 13}),
    ("b074", "استخدام گارد شخصی", "امنیت من امنیت همه‌ست.", {"treasury_to_king": 0.20, "unrest": 10}),
    ("b075", "ساخت مجسمهٔ خودم", "بزرگ. خیلی بزرگ.", {"treasury_to_king": 0.16, "mint": 50, "inflation": 0.08, "unrest": 11}),
    ("b076", "جشن تاج‌گذاری مجدد", "بهونه لازم نیست.", {"treasury_to_king": 0.14, "unrest": 7}),
    ("b077", "سفرهای زیارتی درجه یک", "با کل خانواده.", {"treasury_to_king": 0.19, "unrest": 8}),
    ("b078", "خرید جواهرات تاج", "سرمایه‌گذاریه، نه ولخرجی.", {"treasury_to_king": 0.24, "unrest": 12}),
    ("b079", "تأسیس وزارتخانهٔ بی‌کار", "برای فامیل.", {"treasury_to_king": 0.15, "growth_mult": 0.94, "unrest": 12}),
    ("b080", "بودجهٔ محرمانهٔ دربار", "رقمش رو نمی‌گم.", {"treasury_to_king": 0.28, "unrest": 14}),

    ("b081", "دستکاری در ترازو", "کمی به نفع خودم.", {"mint": 60, "levy": 0.03, "inflation": 0.10, "unrest": 8}),
    ("b082", "تقلب در دفتر خزانه", "یه صفر جابه‌جا شد.", {"mint": 100, "treasury_to_king": 0.10, "inflation": 0.16, "unrest": 12}),
    ("b083", "حذف حسابرسی", "دیگه کسی چک نمی‌کنه.", {"treasury_to_king": 0.26, "unrest": 15}),
    ("b084", "دو دفتره کردن حساب‌ها", "یکی برای مردم، یکی واقعی.", {"mint": 90, "treasury_to_king": 0.14, "inflation": 0.14, "unrest": 13}),
    ("b085", "پاک کردن بدهی‌های خودم", "من که به خودم بدهکار نیستم.", {"mint": 110, "inflation": 0.18, "unrest": 11}),
    ("b086", "سانسور آمار تورم", "تورم صفره چون نگفتیمش.", {"inflation": 0.20, "treasury_to_king": 0.09, "unrest": 10}),
    ("b087", "گزارش دروغین رونق", "همه‌چی عالیه (نیست).", {"inflation": 0.15, "treasury_to_king": 0.12, "unrest": 9}),
    ("b088", "تعطیلی دفتر شکایات", "شکایتی در کار نیست.", {"unrest": 16, "treasury_to_king": 0.10}),
    ("b089", "خرید سکوت بزرگان", "با پول خودشون.", {"levy": 0.05, "treasury_to_king": 0.08, "unrest": 12}),
    ("b090", "انکار همه‌چیز", "چه خزانه‌ای؟", {"treasury_to_king": 0.32, "unrest": 17}),

    ("b091", "تورم افسارگسیخته", "بذار بسوزه.", {"mint": 350, "inflation": 0.50, "unrest": 20}),
    ("b092", "چپاول نهایی", "آخرشه دیگه.", {"treasury_to_king": 0.60, "levy": 0.10, "unrest": 25}),
    ("b093", "همه‌چی مال منه", "قانون جدید، یک ماده.", {"levy": 0.15, "treasury_to_king": 0.30, "unrest": 28}),
    ("b094", "فرار با خزانه", "دارم می‌رم، خداحافظ.", {"treasury_to_king": 0.55, "mint": 150, "inflation": 0.35, "unrest": 30}),
    ("b095", "آخرین چاپ", "ماشین رو خاموش نکنید.", {"mint": 400, "inflation": 0.55, "unrest": 22}),
    ("b096", "مالیات نابودکننده", "تا آخرین سانت.", {"levy": 0.18, "unrest": 26}),
    ("b097", "ورشکستگی اعلام‌نشده", "خزانه خالیه، کسی نپرسه.", {"treasury_to_king": 0.45, "inflation": 0.25, "unrest": 21}),
    ("b098", "تصاحب سپرده‌های بانکی", "بانک هم مال منه.", {"treasury_to_king": 0.50, "levy": 0.08, "unrest": 27}),
    ("b099", "لغو تمام حقوق مردم", "ساده‌سازی قوانین.", {"levy": 0.12, "growth_mult": 0.80, "unrest": 29}),
    ("b100", "سلطنت مطلقه", "دیگه حتی تظاهر هم نمی‌کنم.", {"levy": 0.14, "treasury_to_king": 0.40, "inflation": 0.30, "unrest": 32}),
]

# ---------------------------------------------------------------- honest decrees
# The group gets better off; the king pays for it out of his own size.
GOOD = [
    ("g001", "کاهش کارمزدها", "از جیب خودم جبران کردم.", {"fee_mult": 0.85, "king_to_treasury": 0.06, "unrest": -5}),
    ("g002", "حذف کارمزد واریز", "پس‌انداز نباید جریمه داشته باشه.", {"fee_mult": 0.75, "king_to_treasury": 0.09, "unrest": -8}),
    ("g003", "نصف کردن کارمزدها", "نصفش رو خودم می‌دم.", {"fee_mult": 0.60, "king_to_treasury": 0.14, "unrest": -12}),
    ("g004", "معافیت کارمزد برای فقرا", "کسی که نداره، نباید بده.", {"fee_mult": 0.88, "relief": 12, "unrest": -7}),
    ("g005", "شفاف‌سازی کارمزدها", "همه‌چی نوشته شد، بعضیاش حذف.", {"fee_mult": 0.90, "king_to_treasury": 0.05, "unrest": -6}),
    ("g006", "لغو عوارض اضافی", "اون تعرفه‌ها اشتباه بود.", {"fee_mult": 0.80, "king_to_treasury": 0.08, "unrest": -9}),
    ("g007", "کارمزد پلکانی عادلانه", "هرکی بیشتر داره، بیشتر می‌ده.", {"fee_mult": 0.92, "king_to_treasury": 0.06, "unrest": -5}),
    ("g008", "بازگرداندن کارمزدهای ناحق", "از خزانهٔ شخصی پس دادم.", {"fee_mult": 0.82, "handout": 8, "unrest": -11}),
    ("g009", "تعطیلی باجه‌های زائد", "هزینه‌ش رو خودم دادم.", {"fee_mult": 0.86, "king_to_treasury": 0.07, "unrest": -6}),
    ("g010", "کارمزد صفر برای یک روز", "هدیهٔ من به مردم.", {"fee_mult": 0.70, "king_to_treasury": 0.12, "unrest": -10}),

    ("g011", "یارانهٔ نان", "به هرکی که کم داره.", {"relief": 15, "unrest": -8}),
    ("g012", "کمک به تهی‌دستان", "از سهم خودم.", {"relief": 25, "unrest": -12}),
    ("g013", "بستهٔ حمایتی زمستان", "کسی نباید بلرزه.", {"relief": 20, "unrest": -10}),
    ("g014", "صندوق فقرزدایی", "پرش کردم با پول خودم.", {"relief": 30, "king_to_treasury": 0.05, "unrest": -14}),
    ("g015", "وام بدون بهره به نیازمندان", "بدون بهره یعنی بدون بهره.", {"relief": 18, "unrest": -9}),
    ("g016", "جیرهٔ اضطراری", "برای کسایی که زیر خط‌ان.", {"relief": 22, "unrest": -11}),
    ("g017", "بخشش بدهی فقرا", "قلم گرفتم روش.", {"relief": 35, "unrest": -15}),
    ("g018", "کمک‌هزینهٔ درمان", "سلامتی مردم مهم‌تره.", {"relief": 16, "unrest": -8}),
    ("g019", "سبد کالای سلطنتی", "واقعی، نه تبلیغاتی.", {"relief": 28, "unrest": -13}),
    ("g020", "نجات ته‌جدولی‌ها", "هیچ‌کس نباید صفر بمونه.", {"relief": 40, "relief_below": 40, "unrest": -16}),

    ("g021", "سرمایه‌گذاری در خزانه", "از دارایی خودم ریختم توش.", {"king_to_treasury": 0.10, "unrest": -6}),
    ("g022", "تقویت صندوق ملی", "یه سوم دارایی‌م رفت.", {"king_to_treasury": 0.20, "unrest": -12}),
    ("g023", "پشتوانه‌سازی برای پول", "حالا پول واقعاً ارزش داره.", {"king_to_treasury": 0.15, "inflation": -0.10, "unrest": -9}),
    ("g024", "ذخیرهٔ روز مبادا", "مبادای مردم، نه من.", {"king_to_treasury": 0.12, "unrest": -7}),
    ("g025", "بازسازی خزانهٔ غارت‌شده", "قبلی‌ها خراب کردن، من درست می‌کنم.", {"king_to_treasury": 0.25, "unrest": -14}),
    ("g026", "وقف دارایی سلطنتی", "برای همیشه، برای همه.", {"king_to_treasury": 0.18, "unrest": -11}),
    ("g027", "انتقال جواهرات به خزانه", "تاج رو نگه داشتم، بقیه‌ش رفت.", {"king_to_treasury": 0.16, "unrest": -10}),
    ("g028", "حراج اموال شخصی به نفع مردم", "قصر رو کوچیک کردم.", {"king_to_treasury": 0.22, "unrest": -13}),
    ("g029", "بازپرداخت غارت‌های گذشته", "بدهی اخلاقی بود.", {"king_to_treasury": 0.14, "handout": 6, "unrest": -12}),
    ("g030", "کاهش بودجهٔ دربار", "تشریفات رو تعطیل کردم.", {"king_to_treasury": 0.08, "unrest": -5}),

    ("g031", "مهار تورم", "پول اضافه رو از گردش خارج کردم.", {"burn_king": 0.08, "inflation": -0.15, "unrest": -6}),
    ("g032", "سوزاندن پول اضافی", "کمتر پول، باارزش‌تر پول.", {"burn_king": 0.12, "inflation": -0.22, "unrest": -8}),
    ("g033", "جمع‌آوری اسکناس بی‌پشتوانه", "همه‌ش رو سوزوندم.", {"burn_king": 0.15, "inflation": -0.28, "unrest": -10}),
    ("g034", "انقباض پولی", "دردناکه ولی لازمه.", {"burn_king": 0.10, "inflation": -0.18, "unrest": -4}),
    ("g035", "اصلاح ارزی واقعی", "این بار واقعاً اصلاح.", {"burn_king": 0.18, "inflation": -0.32, "unrest": -12}),
    ("g036", "بازگرداندن ارزش پول", "یه سانت دوباره یه سانت شد.", {"burn_king": 0.14, "inflation": -0.25, "unrest": -9}),
    ("g037", "توقف کامل چاپ پول", "ماشین رو خاموش کردم.", {"burn_king": 0.06, "inflation": -0.12, "unrest": -5}),
    ("g038", "سیاست پول سفت", "سخت‌گیرانه، ولی جواب می‌ده.", {"burn_king": 0.16, "inflation": -0.30, "unrest": -7}),
    ("g039", "نابودی ذخایر تورم‌زا", "از انبار خودم شروع کردم.", {"burn_king": 0.20, "inflation": -0.35, "unrest": -13}),
    ("g040", "ریاضت سلطنتی", "اول از خودم.", {"burn_king": 0.09, "inflation": -0.16, "unrest": -8}),

    ("g041", "افزایش سود سپرده", "پس‌انداز باید بصرفه.", {"interest_mult": 1.20, "king_to_treasury": 0.08, "unrest": -7}),
    ("g042", "سود ویژهٔ سپرده‌گذاران", "از جیب خودم تأمینش کردم.", {"interest_mult": 1.35, "king_to_treasury": 0.14, "unrest": -11}),
    ("g043", "تضمین سود بانکی", "تضمین با دارایی شخصی.", {"interest_mult": 1.25, "king_to_treasury": 0.12, "unrest": -9}),
    ("g044", "سود مضاعف برای خردپس‌اندازها", "کوچیک‌ها بیشتر بگیرن.", {"interest_mult": 1.30, "relief": 10, "unrest": -10}),
    ("g045", "بازگرداندن سودهای قطع‌شده", "حق مردم بود.", {"interest_mult": 1.40, "king_to_treasury": 0.16, "unrest": -13}),
    ("g046", "نرخ سود منصفانه", "نه کم، نه فریبنده.", {"interest_mult": 1.15, "king_to_treasury": 0.06, "unrest": -5}),
    ("g047", "پاداش وفاداری بانکی", "هرکی موند، بیشتر گرفت.", {"interest_mult": 1.22, "king_to_treasury": 0.09, "unrest": -8}),
    ("g048", "حذف مالیات بر سود", "سود مردم دست‌نخورده بمونه.", {"interest_mult": 1.18, "king_to_treasury": 0.07, "unrest": -6}),
    ("g049", "سود تضمینی حتی در بحران", "خزانه رو خودم پر می‌کنم.", {"interest_mult": 1.45, "king_to_treasury": 0.18, "unrest": -14}),
    ("g050", "بازگشایی باجه‌های سود", "همه‌جا، برای همه.", {"interest_mult": 1.28, "king_to_treasury": 0.11, "unrest": -9}),

    ("g051", "سرمایه‌گذاری در رشد مردم", "همه بهتر رشد می‌کنن.", {"growth_mult": 1.10, "king_to_treasury": 0.10, "unrest": -8}),
    ("g052", "آبیاری مزارع", "از بودجهٔ خودم.", {"growth_mult": 1.12, "king_to_treasury": 0.11, "unrest": -9}),
    ("g053", "بذر رایگان", "برای همه، بدون استثنا.", {"growth_mult": 1.08, "handout": 5, "unrest": -7}),
    ("g054", "احیای زمین‌های بایر", "کار سختی بود.", {"growth_mult": 1.15, "king_to_treasury": 0.14, "unrest": -11}),
    ("g055", "کود و ابزار برای همه", "تجهیزات از خزانهٔ شخصی.", {"growth_mult": 1.09, "king_to_treasury": 0.09, "unrest": -7}),
    ("g056", "مدرسهٔ کشاورزی", "یاد بگیرن، خودشون بسازن.", {"growth_mult": 1.18, "king_to_treasury": 0.16, "unrest": -12}),
    ("g057", "لغو محدودیت رشد", "هرچی می‌تونید بزرگ شید.", {"growth_mult": 1.20, "king_to_treasury": 0.15, "unrest": -13}),
    ("g058", "پروژهٔ عمرانی بزرگ", "جاده، پل، آب.", {"growth_mult": 1.25, "king_to_treasury": 0.20, "inflation": -0.08, "unrest": -15}),
    ("g059", "حمایت از تولید داخلی", "پول رو ریختم تو کار مردم.", {"growth_mult": 1.14, "king_to_treasury": 0.13, "unrest": -10}),
    ("g060", "انقلاب صنعتی کوچک", "گرون بود، ارزشش رو داشت.", {"growth_mult": 1.30, "king_to_treasury": 0.24, "inflation": -0.10, "unrest": -16}),

    ("g061", "جشن عمومی", "همه یه چیزی گرفتن.", {"handout": 8, "unrest": -8}),
    ("g062", "عیدی سلطنتی", "از حساب خودم.", {"handout": 12, "unrest": -10}),
    ("g063", "تقسیم غنایم", "سهم همه رو دادم.", {"handout": 15, "unrest": -12}),
    ("g064", "پاداش همبستگی", "چون کنار هم موندیم.", {"handout": 10, "unrest": -9}),
    ("g065", "هدیهٔ تاج‌گذاری", "به جای گرفتن، دادم.", {"handout": 18, "unrest": -13}),
    ("g066", "سفرهٔ عمومی", "هفت روز، برای همه.", {"handout": 14, "unrest": -11}),
    ("g067", "بخشش عمومی", "همه از نو شروع کنن.", {"handout": 6, "unrest": -14}),
    ("g068", "توزیع ذخایر سلطنتی", "انبار رو باز کردم.", {"handout": 20, "unrest": -15}),
    ("g069", "پاداش به همهٔ رعیت", "بی‌قید و شرط.", {"handout": 22, "unrest": -16}),
    ("g070", "آخرین سکه‌هام", "دیگه چیزی برام نموند.", {"handout": 25, "unrest": -18}),

    ("g071", "شنیدن صدای مردم", "دفتر شکایات باز شد.", {"unrest": -10, "king_to_treasury": 0.04}),
    ("g072", "عفو عمومی", "همه بخشیده شدن.", {"unrest": -14, "king_to_treasury": 0.06}),
    ("g073", "پایان سانسور آمار", "تورم واقعی رو اعلام کردم.", {"unrest": -8, "inflation": -0.06, "king_to_treasury": 0.05}),
    ("g074", "حسابرسی مستقل", "بذارید همه ببینن.", {"unrest": -12, "king_to_treasury": 0.08}),
    ("g075", "انتشار دفتر خزانه", "هیچی پنهون نموند.", {"unrest": -11, "king_to_treasury": 0.05}),
    ("g076", "پوزش رسمی", "اشتباه کردم، جبران می‌کنم.", {"unrest": -13, "handout": 5}),
    ("g077", "محاکمهٔ فاسدها", "حتی نزدیکان خودم.", {"unrest": -16, "king_to_treasury": 0.10}),
    ("g078", "بازگرداندن اموال مصادره‌ای", "پس دادم، همه‌ش رو.", {"unrest": -17, "handout": 12}),
    ("g079", "تضمین آزادی معامله", "کسی جلوی کسی رو نمی‌گیره.", {"unrest": -9, "fee_mult": 0.92, "king_to_treasury": 0.06}),
    ("g080", "قانون یکسان برای همه", "حتی برای تاج.", {"unrest": -15, "king_to_treasury": 0.12}),

    ("g081", "ثبات قیمت‌ها", "قیمت‌ها قفل شد.", {"inflation": -0.12, "king_to_treasury": 0.08, "unrest": -7}),
    ("g082", "کنترل بازار", "احتکار ممنوع.", {"inflation": -0.14, "fee_mult": 0.90, "unrest": -8, "king_to_treasury": 0.07}),
    ("g083", "تعادل عرضه و تقاضا", "بالاخره فهمیدم چیه.", {"inflation": -0.10, "growth_mult": 1.06, "unrest": -6, "king_to_treasury": 0.05}),
    ("g084", "سیاست پولی هوشمند", "با مشورت اهل فن.", {"inflation": -0.18, "king_to_treasury": 0.10, "unrest": -9}),
    ("g085", "استقلال بانک مرکزی", "دیگه دستم بهش نمی‌رسه.", {"inflation": -0.20, "king_to_treasury": 0.14, "unrest": -12}),
    ("g086", "پشتوانهٔ طلا", "هر سانت پشتوانه داره.", {"inflation": -0.24, "king_to_treasury": 0.18, "unrest": -11}),
    ("g087", "توقف استقراض از خزانه", "دست از سرش برداشتم.", {"inflation": -0.08, "king_to_treasury": 0.06, "unrest": -6}),
    ("g088", "انضباط مالی", "خرج به اندازهٔ دخل.", {"inflation": -0.16, "burn_king": 0.06, "unrest": -8}),
    ("g089", "بودجهٔ متوازن", "برای اولین بار.", {"inflation": -0.13, "king_to_treasury": 0.09, "unrest": -7}),
    ("g090", "پایان دخالت در بازار", "بازار خودش می‌دونه.", {"inflation": -0.11, "fee_mult": 0.88, "unrest": -8, "king_to_treasury": 0.06}),

    ("g091", "همه‌چی رو پس دادم", "هرچی گرفته بودم.", {"king_to_treasury": 0.30, "handout": 10, "unrest": -20}),
    ("g092", "رنسانس اقتصادی", "همه‌چی رو درست کردم.", {"growth_mult": 1.22, "inflation": -0.20, "king_to_treasury": 0.22, "unrest": -18}),
    ("g093", "عصر طلایی", "گرون‌ترین تصمیم عمرم.", {"growth_mult": 1.25, "interest_mult": 1.25, "king_to_treasury": 0.28, "unrest": -22}),
    ("g094", "پادشاه فقیر، مردم غنی", "تاج موند، ثروت رفت.", {"handout": 30, "unrest": -24}),
    ("g095", "اصلاحات بنیادین", "از پایه، از خودم.", {"fee_mult": 0.70, "growth_mult": 1.18, "king_to_treasury": 0.25, "unrest": -19}),
    ("g096", "بخشش کل بدهی‌ها", "دفتر رو بستم.", {"relief": 45, "relief_below": 80, "unrest": -21}),
    ("g097", "توزیع کامل خزانهٔ شخصی", "چیزی نگه نداشتم.", {"handout": 35, "unrest": -25}),
    ("g098", "تثبیت کامل اقتصاد", "دیگه نوسان نداریم.", {"inflation": -0.40, "burn_king": 0.22, "unrest": -20}),
    ("g099", "میراث ماندگار", "بعد از من هم می‌مونه.", {"growth_mult": 1.28, "interest_mult": 1.20, "king_to_treasury": 0.26, "inflation": -0.15, "unrest": -23}),
    ("g100", "کناره‌گیری شرافتمندانه", "تاج ارزش این‌همه رو نداشت.", {"king_to_treasury": 0.35, "handout": 20, "unrest": -30}),
]

ALL = {code: (code, title, desc, eff, kind)
       for kind, group in (('bad', BAD), ('good', GOOD))
       for code, title, desc, eff in group}


def get(code):
    return ALL.get(code)


def summarize(eff):
    """Plain-Persian preview of what a decree will do, so the king is never asked to
    sign something whose consequences are hidden from him."""
    bits = []
    if eff.get('mint'):             bits.append(f"🖨 چاپ {int(eff['mint'])} سانت برای خودت")
    if eff.get('treasury_to_king'): bits.append(f"🏛→👑 {int(eff['treasury_to_king']*100)}٪ خزانه به تو")
    if eff.get('levy'):             bits.append(f"💰 {int(eff['levy']*100)}٪ سایز هر بازیکن به تو")
    if eff.get('king_to_treasury'): bits.append(f"👑→🏛 {int(eff['king_to_treasury']*100)}٪ سایز تو به خزانه")
    if eff.get('handout'):          bits.append(f"🎁 {int(eff['handout'])} سانت به هر نفر، از جیب تو")
    if eff.get('relief'):           bits.append(f"🍞 {int(eff['relief'])} سانت به فقرا، از جیب تو")
    if eff.get('burn_king'):        bits.append(f"🔥 {int(eff['burn_king']*100)}٪ سایز تو سوزانده می‌شه")
    if eff.get('inflation'):
        d = eff['inflation']
        bits.append(("📈 تورم +" if d > 0 else "📉 تورم ") + f"{d:+.2f}")
    if eff.get('fee_mult'):         bits.append(f"🧾 کارمزدها ×{eff['fee_mult']}")
    if eff.get('interest_mult'):    bits.append(f"🏦 سود سپرده ×{eff['interest_mult']}")
    if eff.get('growth_mult'):      bits.append(f"🌱 رشد روزانه ×{eff['growth_mult']}")
    if eff.get('unrest'):
        u = eff['unrest']
        bits.append(f"😡 خشم مردم {u:+.0f}" if u > 0 else f"😌 خشم مردم {u:+.0f}")
    return bits
