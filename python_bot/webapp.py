"""Telegram Mini App for DickDickBot — the browser face of the game.

Runs as its own process (gunicorn on 127.0.0.1:8012, systemd unit `dickbot-web`) behind
nginx, exactly like admin_panel.py, so a crash here can never take the game down.

WHY THIS IMPORTS bot.py, WHEN THE ADMIN PANEL DELIBERATELY DOESN'T
-------------------------------------------------------------------
The panel is a *separate* tool: it reads and writes the database around the game. This
app is the game, in a browser. Every price, cap, fee and rate it shows has to be the
number the bot itself would charge, and CLAUDE.md is emphatic that two copies of a rule
drift and the drifted one charges real players the wrong amount. bot.py imports with no
side effects (its whole runtime lives under `if __name__ == '__main__'`), so importing it
is the drift-safe choice and re-deriving its constants here would be the dangerous one.

WHY THERE IS NO PASSWORD
------------------------
Telegram signs the payload it hands a Mini App. `initData` carries the user plus an
HMAC-SHA256 taken with a key derived from the bot token, so verifying it proves the
caller is a specific Telegram user without a login, a session store, or a password this
codebase would then have to keep out of git. `_verify_init_data` below is the whole auth
system, and nothing downstream ever trusts a user_id from anywhere else - in particular
never from the request body, which is client-supplied.

WHAT THIS APP DELIBERATELY DOES NOT DO
--------------------------------------
Challenges, theft, consensus votes, heists, decrees and the crown's powers are not here.
They are not missing screens: they are group-social mechanics whose entire point is a
message landing in the chat for other people to react to, and a browser tab has nobody
to post to. Those open a deep link back into Telegram instead. What lives here is
everything a player does alone - their balance, the market, the shop, their bag.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request, Response, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import bot

app = Flask(__name__)
# Same reasoning as the admin panel: nginx terminates TLS and may serve this under a
# path prefix, and the app only listens on 127.0.0.1 so nginx is the only thing that
# can set these headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# How old a Telegram signature may be. The signature itself never expires, so without
# this a leaked initData string would be a permanent credential.
INIT_DATA_MAX_AGE_SECONDS = 24 * 3600
# A Login Widget payload is a longer-lived credential than initData - it is what a
# browser keeps between visits - so it gets its own window rather than borrowing one
# that was sized for a signature Telegram re-issues on every launch.
LOGIN_MAX_AGE_SECONDS = 30 * 24 * 3600
# Shown to the page so the widget can be rendered without hardcoding it in two places.
BOT_USERNAME = os.environ.get('BOT_USERNAME', 'dickchallengerbot')

# How long a single announcement may hold a socket. The money has already moved by the
# time any of this runs, so the only thing a slow api.telegram.org can cost is the
# announcement itself - never the transfer, and never the player's HTTP response.
TG_TIMEOUT_SECONDS = 8


def _esc_plain(text):
    """These handlers build PLAIN text carrying player names verbatim, and _tg_send
    posts as HTML. A name with a '<' in it would break the whole message, so escaping
    happens at the boundary - same rule as the nightly report."""
    import html as _html
    return _html.escape(str(text), quote=False)


def _tg_send(chat_id, text):
    """POST one sendMessage. Stdlib only, and every failure is swallowed.

    requirements.txt has no HTTP client and this is not worth adding one for: it is a
    single form-encoded POST. It deliberately returns a bool instead of raising, because
    every caller is in the "already committed" half of a transfer.
    """
    try:
        data = urllib.parse.urlencode({
            'chat_id': chat_id, 'text': text,
            'parse_mode': 'HTML', 'disable_web_page_preview': 'true',
        }).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{bot.TOKEN}/sendMessage', data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=TG_TIMEOUT_SECONDS):
            return True
    except Exception:
        return False


def _guarded_call(fn, args):
    """Call fn and swallow anything it throws.

    Separate from _run_bg so a test can drive the same guard inline instead of racing a
    thread - a stub that only *looked* like this one would be testing itself.
    """
    try:
        return fn(*args)
    except Exception:
        return None


def _run_bg(fn, *args):
    """Run something whose failure must not reach the request.

    The two announcements are two blocking HTTPS calls, and this is a synchronous WSGI
    worker - doing them inline would hand the player's transfer the latency (and the
    worst case, the hang) of a service that has nothing to do with whether it succeeded.
    It is a named seam rather than an inline Thread(...) so tests can run it inline.
    """
    threading.Thread(target=_guarded_call, args=(fn, args), daemon=True).start()


def _announce_transfer(user_name, amount, delivered, fee, src_chat, dest_chat,
                       dest_title):
    """Post a UI transfer to both groups, wording identical to transfer_callback.

    A transfer is the one thing in the app that moves size out of a group other people
    are playing in, so it cannot be a silent, browser-only action - the group that lost
    the size has to see it exactly as it would have seen /enteghal. Nobody should be
    able to tell from the message which surface was used, so the text is copied from the
    handler rather than reworded.
    """
    name = bot._esc(user_name)
    _tg_send(src_chat,
             f"🔁 <b>انتقال انجام شد</b>\n\n"
             f"{name} <b>{int(amount)}</b> سانت از این گروه فرستاد به "
             f"<b>{bot._esc(dest_title)}</b>.\n"
             f"🧾 کارمزد: {int(fee)} سانت رفت تو خزانهٔ بانک مرکزی\n"
             f"📦 رسید: {int(delivered)} سانت")
    _tg_send(dest_chat,
             f"🔁 {name} <b>{int(delivered)}</b> سانت از یه گروه دیگه آورد اینجا!")


def _verify_init_data(raw):
    """Validate Telegram's initData and return the user dict, or None.

    The scheme is Telegram's: every field except `hash` is sorted and joined as
    "k=v\\n", then HMAC-SHA256'd with a key that is itself HMAC-SHA256(b"WebAppData",
    token). Compared with compare_digest, because a timing-variable comparison on an
    authentication tag is exactly the kind of thing that is fine until it isn't.
    """
    if not raw:
        return None
    try:
        pairs = urllib.parse.parse_qsl(raw, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        return None
    data = dict(pairs)
    their_hash = data.pop('hash', None)
    if not their_hash:
        return None

    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    secret = hmac.new(b'WebAppData', bot.TOKEN.encode(), hashlib.sha256).digest()
    ours = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ours, their_hash):
        return None

    # A valid signature over a stale payload is still stale.
    try:
        if time.time() - int(data.get('auth_date', 0)) > INIT_DATA_MAX_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    try:
        user = json.loads(data.get('user', ''))
    except (ValueError, TypeError):
        return None
    return user if isinstance(user, dict) and user.get('id') else None


def _verify_login_widget(raw):
    """Validate a Telegram Login Widget payload and return it, or None.

    This is a DIFFERENT scheme from the Mini App's, and mixing the two up silently
    rejects (or, worse, silently accepts) everything: the widget's key is
    SHA256(token), while initData's is HMAC-SHA256(b"WebAppData", token). Same
    data-check-string shape, same compare_digest, same freshness rule.

    The widget is what lets someone play in an ordinary browser instead of inside
    Telegram. It requires the domain to be registered with BotFather (/setdomain);
    a Mini App does not, which is why this arrived later than the rest of the app.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    data = {k: v for k, v in data.items() if v is not None}
    their_hash = data.pop('hash', None)
    if not their_hash or not data.get('id'):
        return None

    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot.TOKEN.encode()).digest()
    ours = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ours, str(their_hash)):
        return None
    try:
        if time.time() - int(data.get('auth_date', 0)) > LOGIN_MAX_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    return data


def _header_value(name):
    """One auth header, decoded.

    HTTP header values are Latin-1 ONLY, and a Telegram display name is routinely
    Persian or has an emoji in it. The Login Widget hands the page that name verbatim
    inside its JSON payload, so putting the payload straight into a header made the
    browser refuse the whole request before it was sent:

        Failed to execute 'fetch' on 'Window': Failed to read the 'headers' property
        from 'RequestInit': String contains non ISO-8859-1 code point

    That is not a failed login, it is fetch() declining to run - so *every* call the
    page made threw, and the app was completely unusable in a browser for anyone whose
    name isn't Latin-1. It never showed up inside Telegram because initData arrives
    percent-encoded, which is why /app kept working the whole time.

    So the page base64-encodes any value that doesn't fit, behind a 'b64:' marker.
    Anything without the marker is passed through untouched, which keeps initData and
    every login stored before this fix working byte-for-byte as they did."""
    raw = request.headers.get(name, '')
    if not raw.startswith('b64:'):
        return raw
    try:
        return base64.b64decode(raw[4:], validate=True).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return ''


def _auth():
    """(user_id, first_name, username) for this request, or None.

    Two ways in, both cryptographic and both header-borne so neither can be confused
    with anything the page itself chose to send: initData when the app is opened inside
    Telegram, and a Login Widget payload when it is opened in an ordinary browser. The
    rest of the app cannot tell which was used, and must not care.
    """
    user = _verify_init_data(_header_value('X-Telegram-Init-Data'))
    if user is None:
        user = _verify_login_widget(_header_value('X-Telegram-Login'))
    if user is None:
        return None
    return (int(user['id']), user.get('first_name') or 'بازیکن', user.get('username'))


def _scope():
    """(user_id, first_name, username, chat_id) with the chat VERIFIED against the
    caller's own memberships.

    The chat_id is client-supplied, so it is checked against get_user_groups rather than
    trusted. Without that, changing one number in a request would read - and trade
    against - any group in the bot. Every league is independent here for the same reason
    it is in the bot: a user has a separate size per chat.
    """
    who = _auth()
    if who is None:
        return None
    try:
        chat_id = int(request.args.get('chat_id') or (request.get_json(silent=True) or {}).get('chat_id'))
    except (TypeError, ValueError):
        return None
    if chat_id >= 0:
        return None
    if chat_id not in {row[0] for row in db.get_user_groups(who[0])}:
        return None
    return (who[0], who[1], who[2], chat_id)


def _fail(message, code=400):
    return jsonify({'ok': False, 'error': message}), code


def _need_scope():
    sc = _scope()
    if sc is None:
        return None, _fail('اجازهٔ دسترسی نداری', 403)
    return sc, None


# ---------------------------------------------------------------- read endpoints

@app.get('/api/groups')
def api_groups():
    who = _auth()
    if who is None:
        return _fail('اجازهٔ دسترسی نداری', 403)
    rows = db.get_user_groups(who[0])
    titles = db.get_chat_titles([r[0] for r in rows])
    return jsonify({
        'ok': True,
        'name': who[1],
        'groups': [{'chat_id': cid, 'size': float(size or 0),
                    'title': titles.get(cid) or f'گروه {str(cid)[-6:]}'}
                   for cid, size in rows],
    })


@app.get('/api/home')
def api_home():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    # get_user returns (size, last_grown, perk) - see its SELECT. Reading it as
    # (size, perk, last_grown) compared the PERK against today's date, so grown_today
    # was permanently False and the perk badge showed a date string.
    size, last_grown, perk = db.get_user(uid, chat_id, username, name)
    board = db.get_top_users_full(chat_id)
    rank = next((i + 1 for i, r in enumerate(board) if r[0] == uid), None)
    crown, _changed = bot.refresh_king(chat_id)
    king_id = crown[0] if crown else None
    balance, _dep_date, _dep_today = db.get_bank(uid, chat_id)
    econ = db.get_economy(chat_id)
    rate, _base, _cov = bot.bank_effective_rate(chat_id, econ)
    holdings = db.crypto_holdings_of(uid, chat_id)
    prices = {r[0]: bot.crypto_display_price(r[2], r[4], r[6]) for r in db.crypto_all()}
    portfolio = sum(float(a) * prices.get(s, 0.0) for s, a, _c in holdings)
    return jsonify({
        'ok': True,
        'name': name,
        'size': float(size or 0),
        'perk': perk,
        'grown_today': last_grown == bot.tehran_today_str(),
        'rank': rank,
        'players': len(board),
        'bank': float(balance or 0),
        'bank_rate': rate,
        'maintenance': bot.fee_of(chat_id, bot.BANK_MAINTENANCE_FEE_RATIO, econ),
        'portfolio': portfolio,
        'is_king': king_id == uid,
        'king': crown[1] if crown else None,
        'consort': crown[3] if crown else None,
        'inflation': float(econ[0]),
        'unrest': float(econ[1]),
    })


@app.get('/api/top')
def api_top():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    crown, _changed = bot.refresh_king(chat_id)
    king_id = crown[0] if crown else None
    consort_id = crown[2] if crown else None
    rows = db.get_top_users_full(chat_id)
    return jsonify({'ok': True, 'me': uid, 'rows': [
        {'user_id': r[0], 'name': r[1], 'size': float(r[2] or 0), 'streak': int(r[3] or 0),
         'king': r[0] == king_id, 'consort': r[0] == consort_id}
        for r in rows[:50]
    ]})


@app.get('/api/bank')
def api_bank():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    balance, dep_date, dep_today = db.get_bank(uid, chat_id)
    if dep_date != bot.tehran_today_str():
        dep_today = 0.0
    cap = bot._bank_daily_cap(wallet)
    econ = db.get_economy(chat_id)
    rate, _base, cov = bot.bank_effective_rate(chat_id, econ)
    cb = db.get_central_bank()
    return jsonify({
        'ok': True,
        'wallet': float(wallet or 0),
        'balance': float(balance or 0),
        'cap': cap,
        'remaining': max(0, cap - int(dep_today)),
        'rate': rate,
        'maintenance': bot.fee_of(chat_id, bot.BANK_MAINTENANCE_FEE_RATIO, econ),
        'deposit_fee': bot.fee_of(chat_id, bot.BANK_DEPOSIT_FEE_RATIO, econ),
        'withdraw_fee': bot.fee_of(chat_id, bot.BANK_WITHDRAW_FEE_RATIO, econ),
        'coverage': cov,
        'reserve': cb['reserve'],
        'deposits': cb['deposits'],
        'loans_out': cb['loans_out'],
        'cash': cb['cash'],
        'loan_rate': bot.bank_loan_rate(chat_id),
    })


@app.get('/api/crypto')
def api_crypto():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    held = {s: (float(a), float(c)) for s, a, c in db.crypto_holdings_of(uid, chat_id)}
    # What the real market says, so the board can show that these prices are not invented.
    feed = db.crypto_feed_status()
    coins = []
    for sym, cname, mid, prev_mid, base, vol, net in db.crypto_all():
        price = bot.crypto_display_price(mid, base, net)
        prev = bot.crypto_display_price(prev_mid, base, net)
        mine = held.get(sym)
        coins.append({
            'symbol': sym, 'name': cname, 'price': price,
            'change': ((price - prev) / prev * 100.0) if prev else 0.0,
            'vs_base': (price - float(base)) / float(base) * 100.0,
            'demand': db._crypto_impact(float(net) * float(base),
                                        bot.CRYPTO_IMPACT_DEPTH, bot.CRYPTO_IMPACT_CAP),
            'units': mine[0] if mine else 0.0,
            'avg_cost': mine[1] if mine else 0.0,
            'value': (mine[0] * price) if mine else 0.0,
        })
        fid, usd, age = feed.get(sym, (None, None, None))
        coins[-1]['feed'] = fid
        coins[-1]['feed_usd'] = usd
        # "Live" means the feed answered recently. When it hasn't, the coin is back on
        # the random walk and the board says so rather than implying a real quote.
        coins[-1]['live'] = bool(usd) and age is not None and age < bot.CRYPTO_FEED_STALE_SECONDS
    return jsonify({
        'ok': True, 'coins': coins, 'wallet': float(wallet or 0),
        'fee': bot.CRYPTO_FEE_RATIO,
        'cap': bot._crypto_daily_cap(wallet),
        'liquidity': db.get_central_bank()['reserve'],
    })


@app.get('/api/shop')
def api_shop():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    econ = db.get_economy(chat_id)
    day, week = bot.tehran_today_str(), bot.tehran_week_str()
    items = []
    for item in bot.SHOP_PRICES:
        d, w = db.get_shop_item_counts(chat_id, item, day, week)
        price = bot.shop_item_price(chat_id, item, d, w)
        items.append({
            'name': item, 'price': price,
            'day_left': max(0, bot.SHOP_DAILY_LIMIT - d),
            'week_left': max(0, bot.SHOP_WEEKLY_LIMIT - w),
            'desc': bot.ITEM_DESCRIPTIONS.get(item, ''),
        })
    return jsonify({'ok': True, 'items': items, 'wallet': float(wallet or 0),
                    'inflation': float(econ[0])})


@app.get('/api/inventory')
def api_inventory():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    return jsonify({'ok': True, 'items': [
        {'name': it, 'count': int(n), 'desc': bot.ITEM_DESCRIPTIONS.get(it, '')}
        for it, n in db.get_inventory(uid, chat_id)
    ]})


@app.get('/api/economy')
def api_economy():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    e = db.get_economy_full(chat_id)
    crown, _changed = bot.refresh_king(chat_id)
    rate, _b, cov = bot.bank_effective_rate(chat_id)
    income = db.get_treasury_income(bot.BANK_INCOME_WINDOW_DAYS)
    # One treasury for the whole bot; `group_claim` is the most of it this group could
    # ever draw (a heist, a corrupt decree) - see db._group_weight.
    treasury, _t2, _t3 = db.get_treasury(chat_id)
    group_claim = db.group_reserve_claim(chat_id)
    return jsonify({
        'ok': True,
        'inflation': float(e[0]), 'unrest': float(e[1]),
        'fee_mult': float(e[2]), 'interest_mult': float(e[3]), 'growth_mult': float(e[4]),
        'king': crown[1] if crown else None,
        'consort': crown[3] if crown else None,
        'is_king': bool(crown) and crown[0] == uid,
        'bank_rate': rate, 'coverage': cov,
        'bank_income': income, 'treasury': float(treasury or 0),
        'group_claim': float(group_claim or 0),
    })


@app.get('/api/debts')
def api_debts():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    score, repaid, late, defaults = db.get_credit(uid, chat_id)
    owed, lent = db.get_user_loans(chat_id, uid)
    return jsonify({
        'ok': True, 'score': score, 'repaid': repaid, 'late': late,
        'defaults': defaults, 'grade': bot._credit_grade(score),
        'owed': [{'id': l[0], 'other': l[1], 'due_amount': float(l[3]),
                  'due_at': l[4].isoformat() if l[4] else None,
                  'principal': float(l[5])} for l in owed],
        'lent': [{'id': l[0], 'other': l[1], 'due_amount': float(l[2]),
                  'due_at': l[3].isoformat() if l[3] else None,
                  'principal': float(l[4])} for l in lent],
    })


# ---------------------------------------------------------------- write endpoints
#
# Every one of these routes through the SAME db function the Telegram handler calls,
# with the SAME constants out of bot.py. Nothing here re-implements a cap, a fee or a
# price - a second copy would drift, and the drifted one charges real players the wrong
# amount. What these do own is turning the db layer's (False, reason, ...) tuples into
# something a browser can render.

def _amount(payload, key='amount'):
    try:
        value = int(float(payload.get(key)))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@app.post('/api/bank/deposit')
def api_deposit():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    amount = _amount(request.get_json(silent=True) or {})
    if amount is None or amount < bot.BANK_MIN_DEPOSIT:
        return _fail(f'حداقل واریز {bot.BANK_MIN_DEPOSIT} سانته')
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    ok, a, b, fee = db.bank_deposit(uid, chat_id, amount, bot.tehran_today_str(),
                                    bot._bank_daily_cap(wallet),
                                    bot.fee_of(chat_id, bot.BANK_DEPOSIT_FEE_RATIO))
    if not ok:
        return _fail('سقف واریز امروزت پر شده' if a == 'cap' else 'سانت کافی نداری')
    return jsonify({'ok': True, 'balance': a, 'fee': fee,
                    'message': f'{amount} سانت واریز شد (کارمزد {int(fee)})'})


@app.post('/api/bank/withdraw')
def api_withdraw():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    amount = _amount(request.get_json(silent=True) or {})
    if amount is None:
        return _fail('مقدار نامعتبره')
    ok, a, b, fee = db.bank_withdraw(uid, chat_id, amount,
                                     bot.fee_of(chat_id, bot.BANK_WITHDRAW_FEE_RATIO))
    if not ok:
        # 'run' is the bank-run case: the money is real, it is just inside somebody's
        # loan right now. Saying "you're broke" here would be a lie.
        if a == 'run':
            return _fail(f'بانک الان فقط {int(b)} سانت نقد داره — پولت سرجاشه، '
                         f'دست وام‌گیرنده‌هاست. کمتر برداشت کن.')
        return _fail('این‌قدر تو بانک نداری')
    return jsonify({'ok': True, 'balance': a, 'fee': fee,
                    'message': f'{amount} سانت برداشت شد (کارمزد {int(fee)})'})


@app.post('/api/crypto/buy')
def api_crypto_buy():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get('symbol') or '')
    spend = _amount(payload, 'spend')
    if spend is None or spend < bot.CRYPTO_MIN_TRADE:
        return _fail(f'حداقل خرید {bot.CRYPTO_MIN_TRADE} سانته')
    if symbol not in {r[0] for r in db.crypto_all()}:
        return _fail('همچین کوینی نداریم')
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    res = db.crypto_buy(uid, chat_id, symbol, spend, bot.CRYPTO_FEE_RATIO,
                        bot.tehran_today_str(), bot._crypto_daily_cap(wallet),
                        bot.CRYPTO_IMPACT_DEPTH, bot.CRYPTO_IMPACT_CAP)
    if not res[0]:
        reason = res[1]
        if reason == 'cap':
            return _fail(f'سقف خرید امروزت پر شده — {int(res[2])} سانت مونده')
        if reason == 'funds':
            return _fail('سانت کافی نداری')
        return _fail('مقدار نامعتبره')
    _o, units, price, total, fee, amount, avg = res
    return jsonify({'ok': True, 'units': units, 'price': price, 'total': total, 'fee': fee,
                    'message': f'{units:.4g} خریدی — قیمت {price:,.2f}، پرداختی {int(total)}'})


@app.post('/api/crypto/sell')
def api_crypto_sell():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get('symbol') or '')
    row = next((r for r in db.crypto_all() if r[0] == symbol), None)
    if row is None:
        return _fail('همچین کوینی نداریم')
    holding = dict((s, a) for s, a, _c in db.crypto_holdings_of(uid, chat_id)).get(symbol)
    if not holding:
        return _fail('این کوین رو نداری')
    if payload.get('all'):
        units = float(holding)
    else:
        want = _amount(payload, 'amount')
        if want is None or want < bot.CRYPTO_MIN_TRADE:
            return _fail(f'حداقل فروش {bot.CRYPTO_MIN_TRADE} سانته')
        units = min(float(holding), want / bot.crypto_display_price(row[2], row[4], row[6]))
    res = db.crypto_sell(uid, chat_id, symbol, units, bot.CRYPTO_FEE_RATIO,
                         bot.CRYPTO_IMPACT_DEPTH, bot.CRYPTO_IMPACT_CAP)
    if not res[0]:
        if res[1] == 'liquidity':
            return _fail('بازار نقدینگی نداره — کمتر بفروش')
        return _fail('این کوین رو نداری')
    _o, sold, price, net, fee, pnl, left = res
    partial = sold < units - 1e-6
    return jsonify({'ok': True, 'sold': sold, 'price': price, 'net': net, 'pnl': pnl,
                    'left': left, 'partial': partial,
                    'message': (f'{sold:.4g} فروختی — گرفتی {int(net)}، '
                                f'{"سود" if pnl >= 0 else "ضرر"} {int(abs(pnl))}'
                                + ('  (بازار فقط همین‌قدر رو کشید)' if partial else ''))})


@app.post('/api/shop/buy')
def api_shop_buy():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    item = str((request.get_json(silent=True) or {}).get('item') or '')
    if item not in bot.SHOP_PRICES:
        return _fail('همچین آیتمی نداریم')

    # Claim the scarcity slot first, price off the counts the claim returns, then pay -
    # the same order buy_callback uses, and for the same two reasons: two players racing
    # for the last unit can't both win it, and the price quoted is the price charged
    # because both come from the same pre-purchase counts.
    claimed, day_before, week_before = db.claim_shop_purchase(
        chat_id, item, bot.tehran_today_str(), bot.tehran_week_str(),
        bot.SHOP_DAILY_LIMIT, bot.SHOP_WEEKLY_LIMIT)
    if not claimed:
        return _fail('سهمیهٔ امروز این آیتم تموم شده' if day_before == 'day'
                     else 'سهمیهٔ این هفته تموم شده')
    today_str, week_str = bot.tehran_today_str(), bot.tehran_week_str()
    price = bot.shop_item_price(chat_id, item, day_before, week_before)

    db.get_user(uid, chat_id, username, name)
    if not db.try_deduct_size(uid, chat_id, price):
        db.release_shop_purchase(chat_id, item, today_str, week_str)
        return _fail(f'{price} سانت لازمه و نداری')
    try:
        db.add_inventory(uid, chat_id, item)
    except Exception:
        db.update_size(uid, chat_id, price)
        db.release_shop_purchase(chat_id, item, today_str, week_str)
        raise
    db.treasury_add(chat_id, price, note=f'خرید {item}')
    # Selling through a cap is real evidence of scarcity and bumps inflation - only on
    # the purchase that actually crosses it, never on a refused one. Same rule and same
    # constants as buy_callback; if that ordering ever changes, change it in both.
    if day_before + 1 >= bot.SHOP_DAILY_LIMIT:
        db.bump_inflation(chat_id, bot.SHOP_SOLDOUT_INFLATION_BUMP)
    if week_before + 1 >= bot.SHOP_WEEKLY_LIMIT:
        db.bump_inflation(chat_id, bot.SHOP_WEEKLY_SOLDOUT_INFLATION_BUMP)
    return jsonify({'ok': True, 'price': price,
                    'message': f'{item} خریدی — {price} سانت'})


# ---------------------------------------------------------------- item use
@app.post('/api/inventory/use')
def api_use_item():
    """Only PASSIVE/self-targeted items can be armed from here.

    Anything that needs a target (ویاگرا, قرص, زعفرون) is refused on purpose: those are
    an interaction with another player and belong in the chat where that player can see
    it happen, not in a private browser tab.
    """
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    item = str((request.get_json(silent=True) or {}).get('item') or '')
    # Exactly the set activate_special_item knows how to handle on its own. Challenge
    # items are armed by the challenge flow and the direct ones need a target, so
    # neither belongs in a solo browser tab.
    if item not in bot.THEFT_ITEMS and item not in bot.INSTANT_ITEMS:
        return _fail('این آیتم رو باید توی گروه استفاده کنی')
    db.get_user(uid, chat_id, username, name)
    ok, message = bot.activate_special_item(uid, chat_id, item, name)
    if not ok:
        return _fail(message)
    return jsonify({'ok': True, 'message': message})


# ---------------------------------------------------------------- the page itself
# The front end is a React/Vite/shadcn app under web/, and its BUILD OUTPUT IS COMMITTED
# (web/dist). That is the trade that keeps the deploy a git pull and a restart: the
# production host needs no Node at all, exactly as before the app grew a build step. CI
# rebuilds from source and fails if the committed dist has drifted, so the thing served
# is always the thing in web/src.
_HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(_HERE, 'web', 'dist')
_PAGE = None


@app.get('/')
def index():
    global _PAGE
    if _PAGE is None or app.debug:
        with open(os.path.join(DIST, 'index.html'), encoding='utf-8') as fh:
            _PAGE = fh.read()
    return Response(_PAGE, mimetype='text/html; charset=utf-8')


@app.get('/assets/<path:filename>')
def assets(filename):
    """The built JS and CSS. send_from_directory refuses to escape DIST, so a crafted
    filename cannot read the rest of the disk."""
    resp = send_from_directory(os.path.join(DIST, 'assets'), filename)
    # Rebuilt files keep the same names (see vite.config.ts), so they must revalidate -
    # a long cache here would serve yesterday's app after a deploy.
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.get('/api/crypto/history')
def api_crypto_history():
    """Price points for one coin's chart.

    Scoped like everything else even though prices are global: the endpoint is only
    reachable by a signed player, and _need_scope is what enforces that uniformly.
    """
    sc, err = _need_scope()
    if err:
        return err
    symbol = (request.args.get('symbol') or '').strip().upper()[:16]
    if not symbol:
        return _fail('کدام ارز؟')
    try:
        hours = max(1, min(168, int(request.args.get('hours', 24))))
    except (TypeError, ValueError):
        hours = 24
    rows = db.crypto_history(symbol, hours)
    return jsonify({'ok': True, 'symbol': symbol, 'hours': hours,
                    'points': [{'t': t, 'p': p} for t, p in rows]})


@app.get('/api/transfer')
def api_transfer_info():
    """Everything the transfer screen needs, judged exactly the way the bot judges it.

    Whether it is open, what it charges, whether THIS group is allowed to export, and
    how long the cooldown has left are all read from the same functions /enteghal calls.
    Re-deriving any of them here is the drift this app exists to avoid."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    enabled = db.is_xfer_enabled()
    source_ok, source_reason = bot.check_xfer_source(chat_id, uid)

    rows = db.get_user_groups(uid, exclude_chat_id=chat_id)
    titles = db.get_chat_titles([r[0] for r in rows])
    return jsonify({
        'ok': True,
        'enabled': enabled,
        'wallet': float(wallet or 0),
        'fee_ratio': db.get_xfer_fee_ratio(),
        'min_amount': bot.XFER_MIN_AMOUNT,
        'cooldown_hours': bot.XFER_COOLDOWN_SECONDS // 3600,
        'wait_seconds': db.get_xfer_wait_remaining(uid, chat_id,
                                                   bot.XFER_COOLDOWN_SECONDS),
        'source_ok': source_ok,
        'source_reason': None if source_ok else source_reason,
        'groups': [{'chat_id': cid, 'size': float(size or 0),
                    'title': titles.get(cid) or f'گروه {str(cid)[-6:]}'}
                   for cid, size in rows],
    })


@app.post('/api/transfer')
def api_transfer():
    """Mirrors transfer_callback step for step, in the same order, with the same
    functions. Every one of these checks exists for a reason spelled out in CLAUDE.md,
    and skipping any of them here would make the web the soft way round the gate that
    /enteghal enforces in the chat."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}

    if not db.is_xfer_enabled():
        return _fail('انتقال سایز بین گروه‌ها الان بسته‌ست')

    amount = _amount(body)
    if amount is None or amount < bot.XFER_MIN_AMOUNT:
        return _fail(f'حداقل مبلغ انتقال {bot.XFER_MIN_AMOUNT} سانته')
    try:
        dest_chat = int(body.get('to_chat'))
    except (TypeError, ValueError):
        return _fail('گروه مقصد نامعتبره')
    if dest_chat >= 0 or dest_chat == chat_id:
        return _fail('گروه مقصد نامعتبره')

    # The destination is client-supplied, exactly like the button's callback_data, so
    # membership is re-checked rather than trusted.
    if dest_chat not in [g[0] for g in db.get_user_groups(uid, exclude_chat_id=chat_id)]:
        return _fail('تو اون گروه بازی نمی‌کنی')

    source_ok, source_reason = bot.check_xfer_source(chat_id, uid)
    if not source_ok:
        return _fail(source_reason)

    # Checked BEFORE the cooldown is claimed: a transfer refused for being larger than
    # the wallet must not cost the player their 24 hours. transfer_callback does the
    # same, and the two have to stay in step.
    wallet, _lg, _p = db.get_user(uid, chat_id, username, name)
    if wallet < amount:
        return _fail(f'این‌قدر سانت نداری! {int(wallet)} سانت داری.')

    ok, remaining = db.try_start_xfer(uid, chat_id, bot.XFER_COOLDOWN_SECONDS)
    if not ok:
        hours, minutes = remaining // 3600, (remaining % 3600) // 60
        return _fail(f'تازه انتقال زدی! تا {hours} ساعت و {minutes} دقیقهٔ دیگه صبر کن.')

    ok, delivered, fee = db.cross_group_transfer(uid, chat_id, dest_chat, amount,
                                                 db.get_xfer_fee_ratio())
    if not ok:
        return _fail('سایزت کافی نیست')

    titles = db.get_chat_titles([dest_chat])
    dest_title = titles.get(dest_chat) or f'گروه {str(dest_chat)[-6:]}'

    # Both groups hear about it, exactly as they would have from /enteghal. Dispatched
    # after the transfer has committed and off the request, so a failed announcement
    # can neither undo it nor change what the player is told.
    _run_bg(_announce_transfer, name, amount, delivered, fee, chat_id, dest_chat,
            dest_title)

    return jsonify({
        'ok': True, 'delivered': float(delivered), 'fee': float(fee),
        'message': f'{int(delivered)} سانت رسید به {dest_title} (کارمزد {int(fee)})',
    })


def _announce(chat_id, text):
    """Post a group action's outcome to the group it happened in.

    The whole reason theft and a donation live in a chat is that other people see them.
    Doing one from a browser must not make it invisible, so the app posts exactly the
    text the chat handler would have replied with - nobody can tell which surface was
    used. Dispatched off the request like the transfer announcement: the size has
    already moved, so a slow api.telegram.org costs the announcement and nothing else.
    """
    _run_bg(_tg_send, chat_id, text)


@app.get('/api/players')
def api_players():
    """Everyone in this group, for the target pickers. Same rows the leaderboard uses."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    db.get_user(uid, chat_id, username, name)
    crown, _changed = bot.refresh_king(chat_id)
    king_id = crown[0] if crown else None
    consort_id = crown[2] if crown else None
    return jsonify({'ok': True, 'me': uid, 'players': [
        {'user_id': r[0], 'name': r[1], 'size': float(r[2] or 0),
         'king': r[0] == king_id, 'consort': r[0] == consort_id}
        for r in db.get_top_users_full(chat_id)
    ]})


@app.post('/api/steal')
def api_steal():
    """/dozdi from the app. perform_theft is the SAME function the chat handler calls -
    the odds here are not a second implementation of the odds there."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    try:
        target_id = int(body.get('target'))
    except (TypeError, ValueError):
        return _fail('از کی می‌خوای بدزدی؟')

    target = db.get_user_info(target_id, chat_id)
    if not target:
        return _fail('این بازیکن تو این گروه نیست')
    target_name = target[0] or '؟'

    kind, text = bot.perform_theft(uid, name, username, chat_id, target_id, target_name)
    if kind == bot.THEFT_REFUSED:
        return _fail(text)
    _announce(chat_id, _esc_plain(text))
    return jsonify({'ok': True, 'message': text})


@app.post('/api/donate')
def api_donate():
    """/dd from the app, through the same perform_donation the chat handler uses."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    try:
        target_id = int(body.get('target'))
    except (TypeError, ValueError):
        return _fail('به کی می‌خوای اهدا کنی؟')
    amount = _amount(body)
    if amount is None:
        return _fail('مقدار نامعتبره')

    target = db.get_user_info(target_id, chat_id)
    if not target:
        return _fail('این بازیکن تو این گروه نیست')
    target_name = target[0] or '؟'

    kind, text = bot.perform_donation(uid, name, username, chat_id, target_id,
                                      target_name, amount)
    if kind == bot.DONATE_REFUSED:
        return _fail(text)
    # The giver's own message is written in the second person; the group gets the fact.
    _announce(chat_id, _esc_plain(
        f"🎁 {name} {int(amount)} سانت به {target_name} اهدا کرد."))
    return jsonify({'ok': True, 'message': text})


@app.get('/api/config')
def api_config():
    """Public: just the bot's username, so the Login Widget can be rendered without
    the name being hardcoded in the template as well as here."""
    return jsonify({'ok': True, 'bot': BOT_USERNAME})


@app.get('/healthz')
def healthz():
    return jsonify({'ok': True})


@app.after_request
def _headers(resp):
    # A Mini App is framed by Telegram, so X-Frame-Options must NOT be DENY. Everything
    # else is tightened instead.
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


if __name__ == '__main__':
    db.init_db()
    app.run(host='127.0.0.1', port=8012, debug=True)
