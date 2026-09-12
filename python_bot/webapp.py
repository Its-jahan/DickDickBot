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

EVERY ACTION LIVES HERE, AND THE GROUP STILL SEES IT
---------------------------------------------------
Nothing is Telegram-only. Growth, theft, donations, challenges, consensus votes, the
heist and the crown's decrees are all reachable from the browser, and each one
still posts to the group exactly as the chat handler would have - a player using the app
is invisible to nobody.

The rule that makes that safe is that the DECISION never lives in a surface. Every one
of them goes through a `bot.perform_*` function that touches no Telegram object and
returns (kind, text); the chat handler and the endpoint here are both thin wrappers over
it. A second copy of the theft odds, the challenge escrow ordering or the consensus
threshold would be a money bug, not a style one.

Three things a browser genuinely cannot do, and how they are handled rather than dodged:

- **It has no job queue.** A challenge accepted or a vote opened here cannot schedule
  its own settlement. The bot sweeps for all of it (recover_stuck_pvp_matches,
  recover_expired_consensus and recover_stuck_heist_attempts now repeat), so the browser
  starts what the bot finishes.
- **It has no clock anyone else can trust.** The heist's three stages were timed by
  scheduled jobs; the timing is now two stored timestamps (alarm_at, vault_at) and
  db.heist_tick advances the run ON READ, so both surfaces compute the same run instead
  of keeping two countdowns that drift.
- **It has nobody to show a button to.** Anything that needs another player's tap is
  announced into the group carrying that keyboard, so a challenge opened in the app is
  accepted from the chat and vice versa - one book, not one per surface.

Item use follows the same rule: perform_item_use returns a `public` line for the items
that reach another player and None for the ones that are nobody else's business, so the
group sees a ویاگرا land on somebody and never sees which item you armed for a challenge.
"""
import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import os
import threading
import time
import types
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request, Response, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import bot
import decrees

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


def _tg_api(method, payload):
    """Small Bot API boundary used for announcements, admin checks and invoices."""
    try:
        encoded = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
            else value
            for key, value in payload.items() if value is not None
        }
        data = urllib.parse.urlencode(encoded).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{bot.TOKEN}/{method}', data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=TG_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode('utf-8'))
        return body.get('result') if body.get('ok') else None
    except Exception:
        return None


def _tg_send(chat_id, text, reply_markup=None):
    """POST one tone-aware sendMessage; a failed announcement never rolls money back.

    `reply_markup` carries the keyboard for the things a browser starts but somebody in
    the chat has to answer - a challenge nobody can accept is not a challenge. _tg_api
    JSON-encodes a dict payload already, so it is passed straight through.
    """
    return _tg_api('sendMessage', {
        'chat_id': chat_id, 'text': bot.tone_text(chat_id, text),
        'parse_mode': 'HTML', 'disable_web_page_preview': 'true',
        'reply_markup': reply_markup,
    }) is not None


def _tg_is_admin(chat_id, user_id):
    member = _tg_api('getChatMember', {'chat_id': chat_id, 'user_id': user_id})
    return bool(member) and member.get('status') in ('creator', 'administrator', 'owner')


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
    tones = db.get_chat_tones([r[0] for r in rows])
    return jsonify({
        'ok': True,
        'name': who[1],
        'groups': [{'chat_id': cid, 'size': float(size or 0),
                    'tone': tones.get(cid, 'adult'),
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
        'me_id': uid,
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
        # The board is already computed above for the rank, so the leaderboard costs
        # nothing extra here - and it belongs on the home screen now that the bell has
        # the sixth tab.
        'board': [{'user_id': r[0], 'name': r[1], 'size': float(r[2] or 0),
                   'streak': int(r[3] or 0), 'king': r[0] == king_id,
                   'consort': r[0] == (crown[2] if crown else None)}
                  for r in board],
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
    item_stars = {p['item']: {'sku': sku, 'stars': p['stars']}
                  for sku, p in bot.STAR_ITEM_PRODUCTS.items()}
    for item in items:
        item.update(item_stars.get(item['name'], {}))
    packages = [{'sku': sku, **product}
                for sku, product in bot.STAR_SIZE_PACKAGES.items()]
    return jsonify({'ok': True, 'items': items, 'star_packages': packages,
                    'wallet': float(wallet or 0),
                    'inflation': float(econ[0])})


@app.post('/api/stars/invoice')
def api_stars_invoice():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    sku = str((request.get_json(silent=True) or {}).get('sku') or '')
    db.get_user(uid, chat_id, username, name)
    order_id, product = bot.create_star_order(uid, chat_id, sku)
    if not order_id:
        return _fail('این محصول پیدا نشد')
    fields = bot.star_invoice_fields(order_id, product)
    invoice_url = _tg_api('createInvoiceLink', {
        'title': fields['title'], 'description': fields['description'],
        'payload': fields['payload'], 'currency': 'XTR',
        'prices': [p.to_dict() for p in fields['prices']],
    })
    if not invoice_url:
        db.fail_star_order(order_id)
        return _fail('ساخت فاکتور تلگرام ممکن نشد؛ دوباره امتحان کن', 502)
    return jsonify({'ok': True, 'order_id': order_id, 'invoice_url': invoice_url})


@app.get('/api/stars/order/<order_id>')
def api_stars_order(order_id):
    sc, err = _need_scope()
    if err:
        return err
    uid, _name, _username, chat_id = sc
    valid_id = bot.parse_star_invoice_payload('stars:' + order_id)
    order = valid_id and db.get_star_order(valid_id, uid)
    if not order or order['chat_id'] != chat_id:
        return _fail('سفارش پیدا نشد', 404)
    return jsonify({'ok': True, 'status': order['status'], 'kind': order['kind'],
                    'quantity': order['quantity'], 'stars': order['stars']})


@app.post('/api/settings/tone')
def api_settings_tone():
    sc, err = _need_scope()
    if err:
        return err
    uid, _name, _username, chat_id = sc
    mode = str((request.get_json(silent=True) or {}).get('mode') or '')
    if mode not in ('adult', 'polite'):
        return _fail('لحن نامعتبره')
    if not _tg_is_admin(chat_id, uid):
        return _fail('فقط ادمین گروه می‌تونه لحن رو عوض کنه', 403)
    db.set_chat_tone(chat_id, mode)
    bot.set_cached_chat_tone(chat_id, mode)
    return jsonify({'ok': True, 'tone': mode,
                    'message': 'لحن محترمانه فعال شد' if mode == 'polite'
                    else 'لحن +۱۸ فعال شد'})


def _item_kind(name):
    """Which bucket an item is in, answered from bot.py's own lists.

    The front end used to carry a hardcoded copy of these names to decide which items
    showed a button. That is the drift bug this repo keeps getting bitten by: an item
    added to a bucket in bot.py would silently stay unusable in the app forever. The
    buckets are the bot's, so the answer comes from the bot.
    """
    for kind, group in (('direct', bot.DIRECT_ITEMS), ('challenge', bot.CHALLENGE_ITEMS),
                        ('theft', bot.THEFT_ITEMS), ('instant', bot.INSTANT_ITEMS)):
        if name in group:
            return kind
    return 'passive'


@app.get('/api/inventory')
def api_inventory():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    items = []
    for it, n in db.get_inventory(uid, chat_id):
        kind = _item_kind(it)
        items.append({
            'name': it, 'count': int(n), 'desc': bot.ITEM_DESCRIPTIONS.get(it, ''),
            'kind': kind,
            # Passive items are worn, not used - there is nothing to press.
            'usable': kind != 'passive',
            'needs_target': kind == 'direct',
        })
    return jsonify({'ok': True, 'items': items})


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
    """Use any item, including the ones that reach another player.

    perform_item_use is the SAME function /use calls, so the dose limit, the
    consume-before-apply ordering and the کون‌سوخته block are not a second copy that
    could drift - they are the only copy.

    Anything that touched somebody else is announced in the group. That is not a
    courtesy: 40 centimetres moving off a player who never opened the app is exactly the
    kind of thing the chat has to see, and the shared function says so by returning a
    `public` line for precisely those items and None for the rest.
    """
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    item = str(body.get('item') or '')

    target_id, target_name = None, None
    if item in bot.DIRECT_ITEMS:
        try:
            target_id = int(body.get('target'))
        except (TypeError, ValueError):
            return _fail('روی کی می‌خوای استفاده کنی؟')
        target = db.get_user_info(target_id, chat_id)
        if not target:
            return _fail('این بازیکن تو این گروه نیست')
        target_name = target[0] or '؟'

    kind, message, public = bot.perform_item_use(
        uid, name, username, chat_id, item, target_id, target_name)
    if kind == bot.ITEM_REFUSED:
        return _fail(message)
    if public:
        _announce(chat_id, _esc_plain(public))
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
    day = bot.tehran_today_str()
    db.log_event(chat_id, day, 'transfer',
                 f"🔁 {name} {int(amount)} سانت از این گروه فرستاد به {dest_title}.",
                 actor_id=uid, actor_name=name, amount=amount)
    db.log_event(dest_chat, day, 'transfer',
                 f"🔁 {name} {int(delivered)} سانت از یه گروه دیگه آورد اینجا!",
                 actor_id=uid, actor_name=name, amount=delivered)

    _run_bg(_announce_transfer, name, amount, delivered, fee, chat_id, dest_chat,
            dest_title)

    return jsonify({
        'ok': True, 'delivered': float(delivered), 'fee': float(fee),
        'message': f'{int(delivered)} سانت رسید به {dest_title} (کارمزد {int(fee)})',
    })


def _announce(chat_id, text, reply_markup=None):
    """Post a group action's outcome to the group it happened in.

    The whole reason theft and a donation live in a chat is that other people see them.
    Doing one from a browser must not make it invisible, so the app posts exactly the
    text the chat handler would have replied with - nobody can tell which surface was
    used. Dispatched off the request like the transfer announcement: the size has
    already moved, so a slow api.telegram.org costs the announcement and nothing else.
    """
    _run_bg(_tg_send, chat_id, text, reply_markup)


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


@app.post('/api/grow')
def api_grow():
    """/d from the app. perform_growth is the same coroutine the chat handler awaits."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    db.get_user(uid, chat_id, username, name)

    # perform_growth reads exactly three attributes off `user` (id, first_name,
    # username) and nothing else Telegram-shaped - it was written to be callable
    # without an Update, which is what makes this a shim rather than a mock.
    user = types.SimpleNamespace(id=uid, first_name=name, username=username)
    # asyncio.run is safe here: this is a synchronous WSGI worker, so there is no
    # running loop to conflict with.
    ok, text = asyncio.run(bot.perform_growth(user, chat_id))
    if not ok:
        return _fail(text)
    _announce(chat_id, _esc_plain(text))
    return jsonify({'ok': True, 'message': text})


@app.get('/api/challenges')
def api_challenges():
    """The group's open challenges - the listing a browser needs and a button cannot be.

    Challenges opened with /c appear here and challenges opened here appear in the chat,
    because both write the same open_challenges row.
    """
    sc, err = _need_scope()
    if err:
        return err
    uid, _name, _username, chat_id = sc
    size, _lg, _perk = db.get_user(uid, chat_id, _username, _name)
    rows = db.list_open_challenges(chat_id, bot.CHALLENGE_OPEN_SECONDS)
    return jsonify({
        'ok': True,
        'size': float(size or 0),
        'me_id': uid,
        'open': [{'nonce': n, 'challenger_id': cid, 'challenger': cname,
                  'bet': float(bet or 0), 'age': int(age or 0)}
                 for n, cid, cname, bet, age in rows],
    })


@app.post('/api/challenge/create')
def api_challenge_create():
    """Open a challenge from the app, and post it to the group WITH its accept button.

    The keyboard is the point: a challenge nobody in the chat can tap is not the same
    feature. build_challenge_data signs the stake exactly as the chat path does, and is
    handed the nonce the row was written under so the button and the row are the same
    challenge rather than two.
    """
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    try:
        bet = int(body.get('bet'))
    except (TypeError, ValueError):
        return _fail('مقدار شرط رو بنویس')

    kind, text, nonce = bot.perform_challenge_create(uid, name, username, chat_id, bet)
    if kind == bot.CHALLENGE_REFUSED:
        return _fail(text)
    _announce(chat_id, _esc_plain(text), reply_markup={'inline_keyboard': [[{
        'text': 'بیا کیرمو بخور ⚔️',
        'callback_data': bot.build_challenge_data(uid, bet, nonce),
    }]]})
    return jsonify({'ok': True, 'message': text, 'nonce': nonce})


@app.post('/api/challenge/accept')
def api_challenge_accept():
    """Accept one. Settlement is the bot's repeating sweep, not a job scheduled here."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    nonce = str(body.get('nonce') or '')

    row = db.get_open_challenge(nonce)
    # The nonce is client-supplied, so the challenge is re-read from the database rather
    # than trusting a stake or a challenger id sent alongside it - the same reason the
    # chat path verifies the signature on its callback_data.
    if not row or row[1] != chat_id or row[5] != 'open':
        return _fail('این چالش دیگه باز نیست')
    _n, _c, challenger_id, _cn, bet, _st = row

    kind, text, match_id = bot.perform_challenge_accept(
        uid, name, username, chat_id, nonce, challenger_id, int(bet))
    if kind == bot.CHALLENGE_REFUSED:
        return _fail(text)
    _announce(chat_id, _esc_plain(text))
    return jsonify({'ok': True, 'message': text, 'match_id': match_id,
                    'resolves_in': bot.BET_WINDOW_SECONDS})


@app.get('/api/ejma')
def api_ejma():
    """Open consensus votes in this group, with what this player may still do."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    _sz, last_grown, _p = db.get_user(uid, chat_id, username, name)
    today = bot.tehran_today_str()
    rows = db.get_open_consensus_list(chat_id, bot.CONSENSUS_VOTE_WINDOW_SECONDS)
    return jsonify({
        'ok': True,
        'eligible': last_grown == today and not db.is_jester(uid, chat_id),
        'min_players': bot.MIN_CONSENSUS_PLAYERS,
        'active_today': db.get_active_today_count(chat_id, today),
        'me_id': uid,
        'open': rows,
    })


@app.post('/api/ejma/start')
def api_ejma_start():
    """Start a vote from the app; the group gets the message and the vote buttons."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    try:
        target_id = int(body.get('target'))
    except (TypeError, ValueError):
        return _fail('اجماع علیه کی؟')

    target = db.get_user_info(target_id, chat_id)
    if not target:
        return _fail('این بازیکن تو این گروه نیست')

    kind, text, vote_id = bot.perform_ejma_start(
        uid, name, username, chat_id, target_id, target[0] or '؟')
    if kind == bot.EJMA_REFUSED:
        return _fail(text)
    _announce(chat_id, _esc_plain(text), reply_markup={'inline_keyboard': [[
        {'text': '✅ موافق (1)', 'callback_data': f'ejmavote_{vote_id}_yes'},
        {'text': '❌ مخالف (0)', 'callback_data': f'ejmavote_{vote_id}_no'},
    ]]})
    return jsonify({'ok': True, 'message': text, 'vote_id': vote_id})


@app.post('/api/ejma/vote')
def api_ejma_vote():
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    try:
        vote_id = int(body.get('vote_id'))
    except (TypeError, ValueError):
        return _fail('کدوم رای‌گیری؟')
    choice = 'yes' if str(body.get('choice')) == 'yes' else 'no'

    kind, text, state = bot.perform_ejma_vote(uid, name, username, chat_id, vote_id, choice)
    if kind == bot.EJMA_REFUSED:
        return _fail(text)
    # An ordinary vote is not news; a settled one is. Announcing every tap would put the
    # running tally in the group once per voter, which is the noise the whole edit-don't-
    # post rule exists to stop.
    if state != 'open':
        _announce(chat_id, _esc_plain(text))
    return jsonify({'ok': True, 'message': text, 'state': state})


@app.get('/api/decree')
def api_decree():
    """Tonight's hand, for the king. Readable now that it lives in the database."""
    sc, err = _need_scope()
    if err:
        return err
    uid, _name, _username, chat_id = sc
    kingdom, _ = bot.refresh_king(chat_id)
    king_id = kingdom[0] if kingdom else None
    today = bot.tehran_today_str()
    full = db.get_economy_full(chat_id)
    signed = bool(full and full[6] == today)

    codes = []
    if king_id and uid == king_id and not signed:
        entry = db.get_pending_decrees(chat_id)
        if not entry or entry[0] != today or entry[2] != king_id:
            codes = bot._roll_decrees(chat_id, today, king_id)
        else:
            codes = entry[1]

    out = []
    for code in codes:
        d = decrees.get(code)
        if d:
            out.append({'code': d[0], 'title': d[1], 'desc': d[2], 'kind': d[4]})
    econ = db.get_economy(chat_id)
    return jsonify({
        'ok': True,
        'is_king': bool(king_id and uid == king_id),
        'king': (kingdom[1] if kingdom else None),
        'signed_today': signed,
        'inflation': float(econ[0]) if econ else 1.0,
        'unrest': float(econ[1]) if econ else 0.0,
        'choices': out,
    })


@app.post('/api/decree/sign')
def api_decree_sign():
    sc, err = _need_scope()
    if err:
        return err
    uid, _name, _username, chat_id = sc
    body = request.get_json(silent=True) or {}
    kind, text = bot.perform_decree_sign(uid, chat_id, str(body.get('code') or ''))
    if kind == bot.DECREE_REFUSED:
        return _fail(text)
    # Already HTML from perform_decree_sign - do not escape it again.
    _announce(chat_id, text)
    return jsonify({'ok': True, 'message': text})


def _heist_view(row, uid):
    """What this player may see of a live heist, and nothing more.

    The sequence is the answer, so it is never sent whole. During the reveal the server
    works out which single symbol is on screen right now from vault_at - the same
    anti-cheat the chat gets from editing one message per frame, except here the client
    could otherwise just read the payload. At no instant does a response carry two
    symbols of the answer.
    """
    now = time.time()
    stage = row['stage']
    is_thief = uid == row['thief_id']
    view = {
        'attempt_id': row['id'], 'status': row['status'], 'stage': stage,
        'thief': row['thief_name'], 'partner': row['partner_name'],
        'is_thief': is_thief, 'is_partner': uid == row['partner_id'],
        'would_be': float(row['would_be'] or 0),
        'symbols': bot.HEIST_SYMBOLS, 'wires': bot.HEIST_WIRES,
        'length': bot.HEIST_SEQUENCE_LENGTH,
        'progress': row['progress'] or 0,
        'escape_thief': bool(row['escape_thief']),
        'escape_partner': bool(row['escape_partner']),
    }
    deadline = row.get('stage_deadline')
    view['seconds_left'] = max(0.0, deadline.timestamp() - now) if deadline else None

    if row['status'] == 'offered':
        # The wire is named exactly once, when the accomplice accepts - never here.
        return view

    if stage == 1:
        view['armed'] = bool(row['alarm_armed'])
        # The colour is shown once at accept and never again; by the time the buttons
        # are up it has to be in the accomplice's head.
        view['wire_hint'] = None
    elif stage == 2 and is_thief:
        vault_at = row.get('vault_at')
        if vault_at:
            elapsed = now - vault_at.timestamp()
            step = int(elapsed // bot.HEIST_REVEAL_STEP_SECONDS)
            reveal_end = bot.HEIST_SEQUENCE_LENGTH * bot.HEIST_REVEAL_STEP_SECONDS
            seq = [int(x) for x in (row['sequence'] or '').split(',') if x != '']
            if elapsed < reveal_end and 0 <= step < len(seq):
                view['phase'] = 'reveal'
                view['show'] = seq[step]          # exactly ONE symbol, ever
                view['step'] = step
            elif elapsed < reveal_end + bot.HEIST_BLANK_SECONDS:
                view['phase'] = 'blank'           # the deliberate wipe frame
            else:
                view['phase'] = 'recall'
    return view


def _heist_bust(attempt_id, reason, message):
    """A losing tap from the app. settle_heist is the SAME function the chat's losing
    tap calls, so the sentence is identical whichever surface blew it - and the group is
    told, because a bust is the record."""
    settled, text = bot.settle_heist(attempt_id, outcome='lost', reason=reason)
    if settled:
        row = db.get_heist_attempt(attempt_id)
        if row:
            _announce(row['chat_id'], _esc_plain(text))
    return jsonify({'ok': True, 'result': 'lost', 'message': message})


@app.get('/api/heist')
def api_heist():
    """The player's live run, if any, plus whether they could start one."""
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    attempt_id = db.get_live_heist(chat_id, uid)
    row = None
    if attempt_id:
        row = db.heist_tick(attempt_id, bot.HEIST_CUT_SECONDS, bot.HEIST_VAULT_SECONDS)
    prison_until, labor_until, bail = db.get_heist_status(uid, chat_id)
    now = time.time()
    return jsonify({
        'ok': True,
        'run': _heist_view(row, uid) if row and row['status'] in ('offered', 'pending') else None,
        'jailed': bool(prison_until and prison_until.timestamp() > now),
        'bail': float(bail or 0),
        'min_vault': bot.HEIST_MIN_VAULT,
        'vault': float(db.group_reserve_claim(chat_id)),
    })


@app.post('/api/heist/start')
def api_heist_start():
    """Open a bank job from the app.

    The invitation is sent SYNCHRONOUSLY, unlike every other announcement here, because
    the message id is part of the record rather than a courtesy: the bot's stage jobs
    edit that message, and a row pointing at no message would leave the chat side of the
    run blind. If the send fails the slot is handed straight back, so a dead
    api.telegram.org costs nobody the group's cooldown.
    """
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    body = request.get_json(silent=True) or {}
    try:
        partner_id = int(body.get('partner'))
    except (TypeError, ValueError):
        return _fail('شریکت کیه؟')
    partner = db.get_user_info(partner_id, chat_id)
    if not partner:
        return _fail('این بازیکن تو این گروه نیست')

    kind, text, info = bot.perform_heist_offer(uid, name, username, chat_id,
                                               partner_id, partner[0] or '؟')
    if kind == bot.HEIST_REFUSED:
        return _fail(text)

    sent = _tg_api('sendMessage', {
        'chat_id': chat_id, 'text': bot.tone_text(chat_id, text), 'parse_mode': 'HTML',
        'reply_markup': {'inline_keyboard': [[
            {'text': '🤝 هستم', 'callback_data': f"heistjoin_{info['attempt_id']}_y"},
            {'text': '🙅 نه بابا', 'callback_data': f"heistjoin_{info['attempt_id']}_n"},
        ]]},
    })
    if not sent:
        db.release_heist_slot(chat_id)
        return _fail('نشد پیام رو تو گروه بفرستم — دوباره امتحان کن')
    bot.store_heist_offer(info, chat_id, sent.get('message_id'))
    return jsonify({'ok': True, 'message': 'پیشنهاد رفت تو گروه 🥷',
                    'attempt_id': info['attempt_id']})


@app.post('/api/heist/accept')
def api_heist_accept():
    """The accomplice signs up. This is where the wire colour is named, once."""
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    body = request.get_json(silent=True) or {}
    attempt_id = str(body.get('attempt_id') or '')
    row = db.get_heist_attempt(attempt_id)
    if not row or row['chat_id'] != chat_id or row['status'] != 'offered':
        return _fail('این پیشنهاد دیگه معتبر نیست')
    if uid != row['partner_id']:
        return _fail('این پیشنهاد مال تو نیست')

    import random as _r
    wait = _r.uniform(bot.HEIST_ALARM_MIN_SECONDS, bot.HEIST_ALARM_MAX_SECONDS)
    deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=wait + bot.HEIST_CUT_SECONDS + 5)
    if not db.accept_heist_offer(attempt_id, uid, deadline, wait):
        return _fail('این پیشنهاد قبلاً تموم شده')
    return jsonify({'ok': True, 'wire': row['wire'],
                    'message': f"سیم {bot.HEIST_WIRES[row['wire']]} — خوب نگاش کن!"})


@app.post('/api/heist/cut')
def api_heist_cut():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    body = request.get_json(silent=True) or {}
    attempt_id = str(body.get('attempt_id') or '')
    row = db.get_heist_attempt(attempt_id)
    if not row or row['chat_id'] != chat_id:
        return _fail('این بازی مال این گروه نیست')
    try:
        wire = int(body.get('wire'))
    except (TypeError, ValueError):
        return _fail('کدوم سیم؟')

    db.heist_tick(attempt_id, bot.HEIST_CUT_SECONDS, bot.HEIST_VAULT_SECONDS)
    result = db.cut_heist_wire(attempt_id, uid, wire)
    if result is None:
        return _fail('این بازی مال تو نیست یا دیگه معتبر نیست')
    if result == 'early':
        return _fail('هنوز علامت ندادم!')
    if result in ('late', 'wrong'):
        return _heist_bust(attempt_id, 'alarm',
                           '⏰ دیر شد!' if result == 'late' else '💥 سیم اشتباهی!')
    db.heist_tick(attempt_id, bot.HEIST_CUT_SECONDS, bot.HEIST_VAULT_SECONDS)
    return jsonify({'ok': True, 'result': 'done', 'message': '🔌 دزدگیر خوابید!'})


@app.post('/api/heist/tap')
def api_heist_tap():
    """One symbol at stage 2. advance_heist_attempt is the same call the chat makes."""
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    body = request.get_json(silent=True) or {}
    attempt_id = str(body.get('attempt_id') or '')
    row = db.get_heist_attempt(attempt_id)
    if not row or row['chat_id'] != chat_id:
        return _fail('این بازی مال این گروه نیست')
    if uid != row['thief_id']:
        return _fail('گاوصندوق کار خود دزده!')
    try:
        idx = int(body.get('symbol'))
    except (TypeError, ValueError):
        return _fail('کدوم نماد؟')

    result = db.advance_heist_attempt(attempt_id, idx)
    if result is None:
        return _fail('این بازی دیگه معتبر نیست')
    if result == 'wrong':
        return _heist_bust(attempt_id, 'vault', '💥 نماد اشتباه!')
    if result == 'done':
        # The vault is open, but the job is not over: the getaway is a real stage and a
        # pair who cracked the safe and then failed to run still go to prison.
        deadline = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=bot.HEIST_ESCAPE_SECONDS))
        db.start_heist_escape(attempt_id, deadline)
        return jsonify({'ok': True, 'result': 'done', 'message': '🔓 بازه! حالا فرار کنید!'})
    return jsonify({'ok': True, 'result': 'correct'})


@app.post('/api/heist/escape')
def api_heist_escape():
    sc, err = _need_scope()
    if err:
        return err
    uid, _n, _u, chat_id = sc
    body = request.get_json(silent=True) or {}
    attempt_id = str(body.get('attempt_id') or '')
    row = db.get_heist_attempt(attempt_id)
    if not row or row['chat_id'] != chat_id:
        return _fail('این بازی مال این گروه نیست')
    result = db.tap_heist_escape(attempt_id, uid)
    if result is None:
        return _fail('این سرقت مال تو نیست یا دیگه معتبر نیست')
    if result == 'again':
        return jsonify({'ok': True, 'result': 'again', 'message': 'تو که زدی بیرون! منتظر شریکت بمون.'})
    if result == 'done':
        settled, text = bot.settle_heist(attempt_id, outcome='won')
        if settled:
            _announce(chat_id, _esc_plain(text))
        return jsonify({'ok': True, 'result': 'done', 'message': '🏃 در رفتین!'})
    return jsonify({'ok': True, 'result': 'waiting', 'message': '🏃 تو در رفتی — منتظر شریکت!'})


@app.get('/api/feed')
def api_feed():
    """Today's log for this group, from the EVENT TABLE - not from Telegram.

    The whole point of the inversion: a player who never opens Telegram still sees
    everything that happened, and a Telegram outage costs the chat copy and nothing
    else. `since` powers the unread badge without re-sending the list.
    """
    sc, err = _need_scope()
    if err:
        return err
    uid, name, username, chat_id = sc
    db.get_user(uid, chat_id, username, name)
    day = bot.tehran_today_str()
    rows = db.get_events(chat_id, day, uid)
    try:
        since = int(request.args.get('since') or 0)
    except (TypeError, ValueError):
        since = 0
    return jsonify({
        'ok': True,
        'day': day,
        'unread': db.count_events_since(chat_id, day, uid, since) if since else 0,
        'latest': rows[0][0] if rows else 0,
        'events': [{
            'id': r[0], 't': r[1], 'kind': r[2], 'private': r[3] != 'group',
            'actor_id': r[4], 'actor': r[5], 'target_id': r[6], 'target': r[7],
            'amount': r[8], 'text': r[9],
        } for r in rows],
    })


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
