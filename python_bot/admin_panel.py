"""Owner-only web panel for DickDickBot.

Runs as its own process (gunicorn on 127.0.0.1) behind nginx, and talks to the same
Supabase database as the bot through db.py — it never imports bot.py, so a panel crash
can never take the game down with it.

Editing is deliberately curated rather than raw-SQL: only the columns in
db.EDITABLE_USER_FIELDS can be written, each through a validator. The panel can move
size, but only via db.admin_adjust_size, so an admin edit lands in the same size_log
ledger as the gameplay around it and stays visible in a player's history.

Auth is a single owner password. It is read from ADMIN_PANEL_PASSWORD_HASH (a
werkzeug hash) so the plaintext is never stored on disk or in the repo.
"""
import functools
import math
import os
import secrets
import time

from flask import (Flask, abort, flash, redirect, render_template_string, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import db

app = Flask(__name__)
# nginx serves this under /dickadmin/ and strips the prefix before proxying, so without
# this Flask builds every url_for()/redirect as "/login" instead of "/dickadmin/login" -
# which lands on the main site's SPA fallback and shows the portfolio page instead of
# the panel. ProxyFix makes Flask honour the X-Forwarded-Prefix/-Proto/-For that the
# nginx block sets. Trusting those headers is safe here precisely because the app only
# listens on 127.0.0.1, so nginx is the only thing that can reach it.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# A missing secret would silently reset every session on restart, so fail loudly.
app.secret_key = os.environ["ADMIN_PANEL_SECRET"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # The panel is only ever served over TLS; without this the session cookie would
    # also be sent over a plain-HTTP request to the same host.
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

PASSWORD_HASH = os.environ["ADMIN_PANEL_PASSWORD_HASH"]

ITEMS = ["ویاگرا", "قرص اورژانسی", "زعفرون", "کاندوم", "شیر موز", "سوزن", "طلسم", "اسپری", "قفل"]
PERKS = ["عادی", "جاکش", "کص‌کش", "حرومزاده", "لاشی", "خایه‌مال", "کون‌گشاد", "زن جنده",
         "جقی", "کیرکلفت", "کص‌شانس", "کیرشکسته", "کون‌سوخته", "حروم‌دست"]

# Login throttling. In-memory is fine: one process, one operator, and a restart
# clearing the counters is not a meaningful bypass when the window is this short.
_failed_logins = {}
MAX_ATTEMPTS, LOCKOUT_SECONDS = 8, 900


def _client_ip():
    # nginx sets X-Real-IP; fall back to the socket for direct/local access.
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def validate(kind, raw):
    """Returns a coerced value, or raises ValueError with a Persian message. This is
    what stops the panel putting the DB into states the game can't handle - notably
    NaN, which used to poison a balance permanently and break the leaderboard."""
    raw = (raw or "").strip()
    if kind in ("number", "int", "mult"):
        try:
            value = float(raw)
        except ValueError:
            raise ValueError("باید یک عدد باشد")
        if not math.isfinite(value):
            raise ValueError("عدد نامعتبر (nan/inf)")
        if kind == "int":
            if value != int(value):
                raise ValueError("باید عدد صحیح باشد")
            return int(value)
        if kind == "mult":
            if not (0.0 <= value <= 5.0):
                raise ValueError("ضریب باید بین ۰ و ۵ باشد")
        return value
    if len(raw) > 64:
        raise ValueError("طولانی‌تر از حد مجاز")
    return raw


BASE = """
<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} — پنل دیک‌بات</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e9ef;--dim:#98a2b3;
--accent:#7c9cff;--good:#4ade80;--bad:#f87171;--warn:#fbbf24}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 system-ui,'Segoe UI',Tahoma,sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 20px;
display:flex;gap:16px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}
header a{color:var(--fg);text-decoration:none;padding:6px 10px;border-radius:8px}
header a:hover{background:#222734}
header .sp{margin-inline-start:auto}
main{padding:20px;max-width:1200px;margin:0 auto}
h1{font-size:20px;margin:0 0 16px}
h2{font-size:16px;margin:24px 0 10px;color:var(--dim);font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.stat{background:#1c212b;border-radius:10px;padding:12px}
.stat b{display:block;font-size:22px}
.stat span{color:var(--dim);font-size:13px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:600px}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:13px}
tr:hover td{background:#1b2029}
a.link{color:var(--accent);text-decoration:none}
a.link:hover{text-decoration:underline}
input,select{background:#0f1319;color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font:inherit;width:100%}
label{display:block;margin:10px 0 4px;color:var(--dim);font-size:13px}
button{background:var(--accent);color:#0b0d11;border:0;border-radius:8px;
padding:9px 16px;font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:#222734;color:var(--fg)}
button.danger{background:var(--bad);color:#160a0a}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:130px}
.pos{color:var(--good)}.neg{color:var(--bad)}.dim{color:var(--dim)}
.flash{background:#1d2a1d;border:1px solid #2f5233;color:#b9f6c4;
padding:10px 14px;border-radius:10px;margin-bottom:14px}
.flash.err{background:#2a1d1d;border-color:#5c2b2b;color:#ffc9c9}
.badge{background:#222734;border-radius:6px;padding:2px 7px;font-size:12px;color:var(--dim)}
form.inline{display:inline}
</style></head><body>
{% if session.get('authed') %}
<header>
  <a href="{{ url_for('index') }}">🏠 خانه</a>
  <a href="{{ url_for('ledger') }}">📜 لاگ تراکنش</a>
  <span class="sp"></span>
  <form class="inline" method="post" action="{{ url_for('logout') }}">
    <button class="ghost">خروج</button></form>
</header>
{% endif %}
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}<div class="flash {{ 'err' if cat=='error' else '' }}">{{ m }}</div>{% endfor %}
{% endwith %}
{{ body }}
</main></body></html>
"""


def page(title, body_html, **ctx):
    """Render a page body inside the shared chrome.

    Both passes go through Flask's own renderer, not a bare jinja2.Template: the bodies
    use url_for/session, which only exist in the app's Jinja environment. The first
    pass autoescapes every value (player names come straight from Telegram), and the
    result is wrapped in Markup so the second pass inserts it rather than escaping the
    HTML it just produced. The wrapped value is a variable, never re-parsed as a
    template, so escaped content can't come back to life as markup."""
    return Markup(_render(BASE, {"title": title, "body": Markup(_render(body_html, ctx))}))


def _render(template_source, ctx):
    """Render through the app's Jinja environment with Flask's own template context
    (url_for, session, get_flashed_messages) merged in.

    The context is passed as a dict rather than **kwargs on purpose:
    render_template_string's first parameter is itself named `source`, so a context
    key called `source` - which the ledger filter uses - collided with it and raised
    "got multiple values for argument 'source'". Passing a dict makes the helper
    immune to whatever the callers happen to name their variables."""
    ctx = dict(ctx)
    app.update_template_context(ctx)
    return app.jinja_env.from_string(template_source).render(ctx)


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = _client_ip()
    fails, until = _failed_logins.get(ip, (0, 0))
    if fails >= MAX_ATTEMPTS and time.time() < until:
        wait = int((until - time.time()) // 60) + 1
        return page("ورود", "<div class='card'><h1>قفل موقت</h1>"
                    f"<p>تلاش‌های ناموفق زیاد. {wait} دقیقهٔ دیگر دوباره امتحان کنید.</p></div>"), 429

    if request.method == "POST":
        if check_password_hash(PASSWORD_HASH, request.form.get("password", "")):
            session.clear()
            session["authed"] = True
            session.permanent = True
            _failed_logins.pop(ip, None)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("index"))
        _failed_logins[ip] = (fails + 1, time.time() + LOCKOUT_SECONDS)
        flash("رمز اشتباه است.", "error")

    return page("ورود", """
<div class="card" style="max-width:380px;margin:60px auto">
  <h1>🍆 پنل مدیریت دیک‌بات</h1>
  <form method="post">
    <label>رمز عبور</label>
    <input type="password" name="password" autofocus autocomplete="current-password">
    <div style="margin-top:14px"><button>ورود</button></div>
  </form>
</div>""")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    groups = []
    for chat_id in db.get_all_chats():
        stats = db.get_group_stats(chat_id)
        kingdom = db.get_kingdom(chat_id)
        groups.append({"chat_id": chat_id, "stats": stats,
                       "king": kingdom[1] if kingdom else None,
                       "consort": kingdom[3] if kingdom else None})
    groups.sort(key=lambda g: -g["stats"]["players"])
    return page("خانه", """
<h1>گروه‌ها</h1>
{% for g in groups %}
<div class="card">
  <div class="row">
    <div style="flex:2">
      <a class="link" style="font-size:17px" href="{{ url_for('group', chat_id=g.chat_id) }}">
        گروه {{ g.chat_id }}</a>
      <div class="dim">
        {% if g.king %}👑 {{ g.king }}{% endif %}
        {% if g.consort %} · 💍 {{ g.consort }}{% endif %}
      </div>
    </div>
  </div>
  <div class="grid" style="margin-top:12px">
    <div class="stat"><b>{{ g.stats.players }}</b><span>بازیکن</span></div>
    <div class="stat"><b>{{ g.stats.active_today }}</b><span>فعال امروز</span></div>
    <div class="stat"><b>{{ g.stats.total_size|int }}</b><span>مجموع سایز</span></div>
    <div class="stat"><b>{{ g.stats.biggest|int }}</b><span>بزرگ‌ترین</span></div>
    <div class="stat"><b>{{ g.stats.log_events }}</b><span>رویداد ثبت‌شده</span></div>
  </div>
</div>
{% endfor %}
{% if not groups %}<div class="card">هنوز گروهی ثبت نشده.</div>{% endif %}
""", groups=groups)


# chat_id is signed: Telegram group ids are negative and the default int
# converter refuses a leading minus, which 404'd every group page.
@app.route("/group/<int(signed=True):chat_id>")
@login_required
def group(chat_id):
    rows = db.get_group_modifiers(chat_id)
    kingdom = db.get_kingdom(chat_id)
    players = []
    for uid, name, username, size, luck, growth in rows:
        detail = db.get_player_detail(uid, chat_id)
        players.append({
            "uid": uid, "name": name, "username": username, "size": size or 0,
            "luck": luck, "growth": growth,
            "streak": detail[11] if detail else 0,
            "wins": detail[9] if detail else 0,
            "losses": detail[10] if detail else 0,
            "perk": detail[6] if detail else "",
            "is_king": bool(kingdom and kingdom[0] == uid),
            "is_consort": bool(kingdom and kingdom[2] == uid),
        })
    return page(f"گروه {chat_id}", """
<h1>گروه {{ chat_id }}</h1>
<div class="card"><div class="tablewrap"><table>
<tr><th>بازیکن</th><th>سایز</th><th>🔥</th><th>برد/باخت</th><th>پرک</th>
    <th>ضریب دزدی</th><th>ضریب رشد</th><th></th></tr>
{% for p in players %}
<tr>
  <td>{{ p.name }}
    {% if p.is_king %}👑{% endif %}{% if p.is_consort %}💍{% endif %}
    <div class="dim" style="font-size:12px">
      {% if p.username %}@{{ p.username }}{% endif %} · {{ p.uid }}</div></td>
  <td><b>{{ p.size|int }}</b></td>
  <td>{{ p.streak }}</td>
  <td>{{ p.wins }}/{{ p.losses }}</td>
  <td class="dim">{{ p.perk }}</td>
  <td {% if p.luck != 1.0 %}class="warn" style="color:var(--warn)"{% endif %}>×{{ p.luck }}</td>
  <td {% if p.growth != 1.0 %}class="warn" style="color:var(--warn)"{% endif %}>×{{ p.growth }}</td>
  <td><a class="link" href="{{ url_for('player', chat_id=chat_id, user_id=p.uid) }}">ویرایش</a></td>
</tr>
{% endfor %}
</table></div></div>

<h2>سپر اجماع فعال</h2>
<div class="card">
{% if protections %}
<div class="tablewrap"><table><tr><th>بازیکن</th><th>تا</th><th>دلیل</th><th></th></tr>
{% for t in protections %}
<tr><td>{{ t[1] }}</td><td class="dim">{{ t[2].strftime('%Y-%m-%d %H:%M') }}</td>
    <td class="dim">{{ t[3] or '' }}</td>
    <td><form class="inline" method="post" action="{{ url_for('clear_protection', chat_id=chat_id, user_id=t[0]) }}">
        <button class="ghost">لغو</button></form></td></tr>
{% endfor %}
</table></div>
{% else %}<span class="dim">هیچ‌کس سپر فعال ندارد.</span>{% endif %}
</div>

<p><a class="link" href="{{ url_for('ledger', chat_id=chat_id) }}">📜 لاگ تراکنش این گروه</a></p>
""", chat_id=chat_id, players=players, protections=db.get_consensus_protections(chat_id))


@app.route("/group/<int(signed=True):chat_id>/player/<int:user_id>")
@login_required
def player(chat_id, user_id):
    detail = db.get_player_detail(user_id, chat_id)
    if not detail:
        abort(404)
    inv = dict(db.get_inventory(user_id, chat_id))
    return page(detail[3] or str(user_id), """
<h1>{{ d[3] }} <span class="badge">{{ d[0] }}</span></h1>
<p class="dim">گروه {{ chat_id }} ·
  {% if d[2] %}@{{ d[2] }}{% endif %} ·
  عضویت از {{ d[8].strftime('%Y-%m-%d') if d[8] else '?' }}</p>

<h2>تنظیم سریع سایز</h2>
<div class="card">
  <form method="post" action="{{ url_for('adjust', chat_id=chat_id, user_id=d[0]) }}">
    <div class="row">
      <div><label>مقدار (منفی = کم کردن)</label>
           <input name="delta" placeholder="مثلاً 50 یا -50"></div>
      <div><label>یادداشت (در لاگ ثبت می‌شود)</label>
           <input name="note" placeholder="دلیل این تغییر"></div>
      <div style="flex:0"><button>اعمال</button></div>
    </div>
  </form>
  <div class="dim" style="margin-top:8px">سایز فعلی: <b>{{ d[4]|int }}</b></div>
</div>

<h2>فیلدها</h2>
<div class="card">
<form method="post" action="{{ url_for('save_fields', chat_id=chat_id, user_id=d[0]) }}">
  <div class="grid">
  {% for col, (kind, label) in fields.items() %}
    <div>
      <label>{{ label }}</label>
      {% if col == 'perk' %}
        <select name="{{ col }}">
          {% for p in perks %}<option {{ 'selected' if p == values[col] else '' }}>{{ p }}</option>{% endfor %}
        </select>
      {% else %}
        <input name="{{ col }}" value="{{ values[col] }}">
      {% endif %}
    </div>
  {% endfor %}
  </div>
  <div style="margin-top:14px"><button>ذخیره</button></div>
</form>
</div>

<h2>آیتم‌ها</h2>
<div class="card">
<form method="post" action="{{ url_for('save_items', chat_id=chat_id, user_id=d[0]) }}">
  <div class="grid">
  {% for item in items %}
    <div><label>{{ item }}</label>
         <input name="item::{{ item }}" value="{{ inv.get(item, 0) }}"></div>
  {% endfor %}
  </div>
  <div style="margin-top:14px"><button>ذخیره آیتم‌ها</button></div>
</form>
</div>

<h2>سانتش از کجا آمده</h2>
<div class="card">
{% if totals %}
<div class="tablewrap"><table><tr><th>منبع</th><th>تعداد</th><th>مجموع</th></tr>
{% for src, n, total in totals %}
<tr><td>{{ src }}</td><td class="dim">{{ n }}</td>
    <td class="{{ 'pos' if total > 0 else 'neg' }}">{{ total|int }}</td></tr>
{% endfor %}
</table></div>
{% else %}<span class="dim">هنوز رویدادی ثبت نشده (لاگ از امروز شروع شده).</span>{% endif %}
</div>

<p><a class="link" href="{{ url_for('ledger', chat_id=chat_id, user_id=d[0]) }}">
  📜 تاریخچهٔ کامل این بازیکن</a></p>
""", d=detail, chat_id=chat_id, fields=db.EDITABLE_USER_FIELDS, perks=PERKS, items=ITEMS,
        inv=inv, totals=db.get_player_totals(chat_id, user_id),
        values={
            'size': int(detail[4] or 0), 'streak': detail[11], 'best_streak': detail[12],
            'wins': detail[9], 'losses': detail[10], 'perk': detail[6] or 'عادی',
            'active_item': detail[7] or '', 'theft_luck': detail[16],
            'growth_mult': detail[17], 'last_grown': detail[5] or '',
        })


@app.route("/group/<int(signed=True):chat_id>/player/<int:user_id>/adjust", methods=["POST"])
@login_required
def adjust(chat_id, user_id):
    try:
        delta = validate('number', request.form.get("delta"))
    except ValueError as e:
        flash(f"مقدار نامعتبر: {e}", "error")
        return redirect(url_for("player", chat_id=chat_id, user_id=user_id))
    note = (request.form.get("note") or "").strip()[:200] or "بدون یادداشت"
    new_size = db.admin_adjust_size(user_id, chat_id, delta, note)
    if new_size is None:
        flash("این بازیکن پیدا نشد.", "error")
    else:
        flash(f"سایز {delta:+g} تغییر کرد. مقدار جدید: {int(new_size)}")
    return redirect(url_for("player", chat_id=chat_id, user_id=user_id))


@app.route("/group/<int(signed=True):chat_id>/player/<int:user_id>/fields", methods=["POST"])
@login_required
def save_fields(chat_id, user_id):
    changed, errors = [], []
    for col, (kind, label) in db.EDITABLE_USER_FIELDS.items():
        if col not in request.form:
            continue
        try:
            value = validate(kind, request.form[col])
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        # Size goes through the ledger path so the change is auditable like any other.
        if col == 'size':
            current = db.get_player_detail(user_id, chat_id)[4] or 0
            if float(value) != float(current):
                db.admin_adjust_size(user_id, chat_id, float(value) - float(current),
                                     "ویرایش مستقیم فیلد سایز از پنل")
                changed.append(label)
            continue
        db.admin_set_user_field(user_id, chat_id, col, value)
        changed.append(label)
    if errors:
        flash("ذخیره نشد — " + " | ".join(errors), "error")
    if changed:
        flash("ذخیره شد: " + "، ".join(changed))
    return redirect(url_for("player", chat_id=chat_id, user_id=user_id))


@app.route("/group/<int(signed=True):chat_id>/player/<int:user_id>/items", methods=["POST"])
@login_required
def save_items(chat_id, user_id):
    changed = []
    current = dict(db.get_inventory(user_id, chat_id))
    for key, raw in request.form.items():
        if not key.startswith("item::"):
            continue
        item = key[6:]
        if item not in ITEMS:
            continue
        try:
            qty = validate('int', raw)
        except ValueError:
            flash(f"{item}: تعداد نامعتبر", "error")
            continue
        qty = max(0, min(qty, 999))
        if qty != current.get(item, 0):
            db.admin_set_inventory(user_id, chat_id, item, qty)
            changed.append(f"{item}={qty}")
    flash("آیتم‌ها ذخیره شد: " + "، ".join(changed) if changed else "تغییری نبود")
    return redirect(url_for("player", chat_id=chat_id, user_id=user_id))


@app.route("/group/<int(signed=True):chat_id>/protection/<int:user_id>/clear", methods=["POST"])
@login_required
def clear_protection(chat_id, user_id):
    flash("سپر اجماع لغو شد." if db.clear_consensus_protection(chat_id, user_id)
          else "سپری برای این بازیکن نبود.")
    return redirect(url_for("group", chat_id=chat_id))


@app.route("/ledger")
@login_required
def ledger():
    chat_id = request.args.get("chat_id", type=int)
    user_id = request.args.get("user_id", type=int)
    source = request.args.get("source") or None
    pagenum = max(0, request.args.get("page", 0, type=int))
    per = 100
    rows = db.get_size_log(chat_id=chat_id, user_id=user_id, source=source,
                           limit=per, offset=pagenum * per)
    return page("لاگ تراکنش", """
<h1>لاگ تراکنش</h1>
<div class="card">
<form method="get" class="row">
  <div><label>گروه</label><input name="chat_id" value="{{ chat_id or '' }}"></div>
  <div><label>کاربر</label><input name="user_id" value="{{ user_id or '' }}"></div>
  <div><label>منبع</label>
    <select name="source">
      <option value="">همه</option>
      {% for s, n in sources %}
      <option value="{{ s }}" {{ 'selected' if s == source else '' }}>{{ s }} ({{ n }})</option>
      {% endfor %}
    </select></div>
  <div style="flex:0"><button>فیلتر</button></div>
</form>
</div>

<div class="card"><div class="tablewrap"><table>
<tr><th>زمان</th><th>بازیکن</th><th>تغییر</th><th>موجودی بعد</th><th>منبع</th><th>یادداشت</th></tr>
{% for r in rows %}
<tr>
  <td class="dim">{{ r[1].strftime('%m-%d %H:%M:%S') }}</td>
  <td><a class="link" href="{{ url_for('player', chat_id=r[2], user_id=r[3]) }}">{{ r[4] }}</a></td>
  <td class="{{ 'pos' if r[5] > 0 else 'neg' }}"><b>{{ '%+d'|format(r[5]|int) }}</b></td>
  <td class="dim">{{ r[6]|int }}</td>
  <td><span class="badge">{{ r[7] }}</span></td>
  <td class="dim">{{ r[8] or '' }}</td>
</tr>
{% endfor %}
</table></div>
{% if not rows %}<span class="dim">رویدادی مطابق این فیلتر نیست.</span>{% endif %}
</div>

<div class="row">
  {% if pagenum > 0 %}
  <a class="link" href="{{ url_for('ledger', chat_id=chat_id, user_id=user_id, source=source, page=pagenum-1) }}">← جدیدتر</a>
  {% endif %}
  {% if rows|length == per %}
  <a class="link" href="{{ url_for('ledger', chat_id=chat_id, user_id=user_id, source=source, page=pagenum+1) }}">قدیمی‌تر →</a>
  {% endif %}
</div>
""", rows=rows, sources=db.get_size_log_sources(chat_id), chat_id=chat_id,
        user_id=user_id, source=source, pagenum=pagenum, per=per)


@app.route("/healthz")
def healthz():
    return "ok", 200
