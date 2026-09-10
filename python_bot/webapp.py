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
import time
import urllib.parse

from flask import Flask, jsonify, request, Response
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
    size, perk, last_grown = db.get_user(uid, chat_id, username, name)
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
    wallet, _p, _lg = db.get_user(uid, chat_id, username, name)
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
    wallet, _p, _lg = db.get_user(uid, chat_id, username, name)
    held = {s: (float(a), float(c)) for s, a, c in db.crypto_holdings_of(uid, chat_id)}
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
    wallet, _p, _lg = db.get_user(uid, chat_id, username, name)
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
    wallet, _p, _lg = db.get_user(uid, chat_id, username, name)
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
    wallet, _p, _lg = db.get_user(uid, chat_id, username, name)
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
_PAGE = None


@app.get('/')
def index():
    """The whole app is one file. No bundler, no build step - same call the rest of this
    repo makes, and it keeps the deploy a git pull and a restart."""
    global _PAGE
    if _PAGE is None or app.debug:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'templates', 'app.html'), encoding='utf-8') as fh:
            _PAGE = fh.read()
    return Response(_PAGE, mimetype='text/html; charset=utf-8')


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
