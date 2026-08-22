# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DickDickBot is a Persian-language Telegram group game bot ("grow your size" comedy game) written in Python using `python-telegram-bot` v20+, with PostgreSQL (Supabase) as the only datastore. There is no build step, no bundler, and no test framework configured in the repo — it's two flat modules under `python_bot/`.

Read `README.md` for the full user-facing feature list (in Persian) — it documents exact game rules (consensus vote thresholds, betting payout formulas, perk effects, crown/consort rules) that are easy to get subtly wrong if you only read the code.

## Running the bot

```bash
cd python_bot
pip install -r requirements.txt
export SUPABASE_DB_URL="postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"
python bot.py
```

- `db.init_db()` runs on every startup and is idempotent (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations) — there is no separate migration tool or migration files. Schema changes are made directly inside `init_db()` in `db.py`.
- The bot's Telegram token is a hardcoded literal (`TOKEN = ...`) near the top of `bot.py`, not an env var.
- No lint/format/build commands exist in this repo (no `Makefile`, no `pyproject.toml`, no CI test job). `deploy.yml` runs `pip install` and restarts the systemd service — it does not run tests.

## Testing

There is no test suite committed to the repo. The established pattern for this codebase (see recent git history) is to spin up a throwaway local Postgres instance and write a standalone script that imports `db`/`bot` directly and drives handlers with hand-rolled fake `Update`/`CallbackQuery`/`Context` objects — there is no pytest config or fixtures.

```bash
# one-time: init a local scratch Postgres cluster on a free port
initdb -D /tmp/pgdataN -A trust
pg_ctl -D /tmp/pgdataN -o "-p 55432 -k /tmp -c listen_addresses=''" -l /tmp/pgN.log start
createdb -p 55432 -h /tmp dicktest

# in the test script, before importing db/bot:
os.environ["SUPABASE_DB_URL"] = "postgresql://postgres@/dicktest?host=/tmp&port=55432"
sys.path.insert(0, "/path/to/python_bot")
import db; db.init_db()
```

Key gotchas when writing this style of test:
- Mock `bot._dice_rng.randint` to rig match outcomes deterministically (it's `random.SystemRandom()`, not the global `random` module).
- Fake `CallbackQuery` objects need both a message-based path (`query.message.chat.id` / `query.message.message_id`) and an inline path (`query.message is None`, only `query.inline_message_id` and `query.chat_instance` available) — see "Inline vs. in-chat callback queries" below. Getting this wrong silently breaks tests that never exercise the inline path, which is the app's most common usage pattern.
- Fake `Context` objects need `.bot` (with async `edit_message_text`/`send_message`) and, for anything PvP-challenge-related, `.job_queue` (with a `run_once(callback, when, data, name)` method) — `accept_challenge_callback` schedules a resolution job through it.
- Run the full existing regression scripts before shipping any change to `bot.py`/`db.py`; there's no single entrypoint, run each script individually.

## Architecture

Two modules, no package structure:

- **`db.py`** — the only place SQL is written. Every table's schema lives inside `init_db()`. Every other function opens a connection via the `get_connection()` context manager (commits on success, rolls back and re-raises on exception, always closes) and returns plain tuples — there is no ORM. `bot.py` never touches `psycopg2` directly.
- **`bot.py`** — everything else: all command/callback handlers, all game logic (perks, items, escrow math, crown/consort, theft, shop, boss, lottery), and the `if __name__ == '__main__':` block that wires up `ApplicationBuilder`, registers job-queue jobs, and registers every handler (regex-based `MessageHandler`s for commands like `/d`, `/c`, `/dozdi`; `CallbackQueryHandler`s keyed on `callback_data` prefixes like `chal_`, `bet_`, `rematch_`, `buy_`, `bosshit_`, `lot_`).

### Data model (all tables live in `db.py`'s `init_db()`)

- `users` (composite PK `user_id, chat_id`) — **every group is a fully independent league**: the same Telegram user has a separate `size`/`perk`/`wins`/`losses` row per `chat_id`. Never assume a user has one global size.
- `chats`, `chat_instances` — the latter maps a Telegram inline "chat_instance" token to a real `chat_id`, since inline queries never reveal which chat they were typed in (see below).
- `kingdom` (one row per group: who wears the crown and who their consort is), `bosses` / `boss_hits`, `lottery_tickets`, `achievements`, `claimed_challenges`
- `inventory`, `consensus_votes` / `consensus_vote_casts` / `consensus_protection` (the `/ejma` group-vote-to-shrink-someone feature), `pvp_matches` / `pvp_match_bets` (1v1 challenges + spectator betting).

### Money/size flow is escrow-based everywhere

Any stake (a PvP challenge bet, a spectator bet, a shop purchase, a lottery ticket) is deducted from `size` **the instant it's placed/accepted**, not at settlement time. Settlement then either pays out `stake × multiplier` to the winner or simply never returns the stake to the loser. This convention exists specifically to prevent double-spend/over-commit exploits (a user accepting two challenges at once using the same not-yet-deducted balance was a real, fixed bug). When adding any new betting/wagering feature, follow this same pattern rather than deducting at settlement.

Payouts must stay zero-sum: a winner can never receive more than what was actually removed from the loser (see the `winner_gain = min(winner_gain, loser_loss)` guard in `resolve_pvp_match` in `bot.py`) — perks/items that shield a loser from losing their stake must shrink the winner's take to match, not mint size out of nothing.

### PvP challenges resolve via a persisted, restartable job, not an in-process sleep

`accept_challenge_callback` escrows both bets, persists the match to `pvp_matches` (dice are rolled later, at resolution time, not at accept time — perks/items are read fresh from the DB at resolution too), and schedules `pvp_resolve_job` via `context.job_queue.run_once(..., when=BET_WINDOW_SECONDS)`. `resolve_pvp_match` is the single settlement function called both by that scheduled job and by `recover_stuck_pvp_matches` (a `run_once(..., when=5)` startup sweep that catches any match whose window closed while the process was down — e.g. a deploy mid-window). `db.claim_pvp_match()` atomically flips `pending -> resolved` so the two callers can never double-settle the same match. Any change to challenge settlement logic must go in `resolve_pvp_match`, not duplicated elsewhere.

### Inline vs. in-chat callback queries — the recurring footgun

This bot is used heavily through Telegram's inline mode (`@dickchallengerbot ...` typed directly in a group chat, tagged "via @dickchallengerbot" in the resulting message). For a callback query on a message that originated that way, **`query.message` is `None`** — Telegram gives the bot no `Message` object, only `query.chat_instance` and `query.inline_message_id`. Code that reads `query.message.chat.id` or `query.message.message_id` unconditionally will crash the handler the instant an inline-originated button is pressed. This exact bug has bitten this codebase more than once (challenges getting silently stuck forever after the escrow already happened but before resolution was scheduled).

The established handling pattern:
- To resolve a chat_id from a callback query: use `resolve_chat_id(query)` in `bot.py` — it reads `query.message.chat.id` if present, else falls back to `db.get_chat_id_from_instance(query.chat_instance)` (a mapping populated the first time that chat_instance was seen with a real message attached).
- To edit a message later (e.g. from a background job that only has IDs, not a live `query` object): store both `message_id`/`chat_id` **and** `inline_message_id` when persisting anything tied to a message, and branch on which is set when calling `context.bot.edit_message_text(...)` (see `deliver_pvp_message` in `bot.py`).

### Multi-group ambiguity in inline mode

Telegram's inline mode never tells the bot which group the query was typed in. For a user active in only one group, `db.get_last_chat(user_id)` resolves it safely; for a user active in **more than one** group, it deliberately returns `None` rather than guessing (guessing wrong once meant leaking one group's leaderboard data into another). Any inline feature must handle the `None` case by falling back to a tap-to-reveal button that resolves the chat from the concretely-sent message, never by picking one of the ambiguous groups.

### Perks expire at Tehran midnight, not at next use

A daily perk is granted alongside a growth roll, so `last_grown` (the growth date) doubles as the perk's expiry stamp. `db.get_user()` — the only place perks are ever read from — lazily returns `'عادی'` (normal) whenever `last_grown` isn't today's date in `Asia/Tehran`, with no scheduled job needed. Any new perk-gated logic should just read the perk via `db.get_user()` as usual; the expiry is transparent.

### Background jobs (all registered in `bot.py`'s `__main__` block)

- `midnight_tasks` — daily at Tehran midnight: draws the lottery for the day that just ended, expires unkilled bosses, collects the crown's daily tax, then sends the growth reminder.
- `spawn_daily_bosses` — daily at 20:00 Tehran; one co-op boss per active group.
- `random_event_job` — every 3h, small per-group chance of an earthquake/viagra-rain/treasure event.
- `recover_stuck_pvp_matches` — one-shot, 5s after startup; sweeps `pvp_matches` for anything stale.

## The bank is deliberately outside `users.size`

`bank_accounts.balance` is a second balance per (user, chat) that is **not** part of
`users.size`. Nothing that reads `users.size` — the leaderboard (`get_top_users_full`),
the crown (`refresh_king`), `/dozdi`, `/ejma`, challenge stakes — can see banked size.
That is the whole trade the feature sells: deposits are safe from theft precisely
because they cost you your position on the table. Do not "fix" this by adding banked
size into the leaderboard; it would turn the bank into a strictly-dominant safe box and
kill theft and challenges in one move.

Two invariants hold the economy together, and both have regression coverage:

- **The bank cannot mint size.** Interest is paid *only* out of `bank_treasury`, and
  `pay_interest` scales every depositor down by the same factor when the treasury can't
  cover what's owed. An empty treasury pays exactly zero. The treasury's only inflows
  are real sinks — shop purchases, the lottery rake (`lottery.BURN_RATIO`), `/ejma`,
  shrink items (قرص/زعفرون), and earthquakes — each of which used to simply delete
  size. If you add a new sink, route it through `db.treasury_add` rather than dropping
  the size on the floor.
- **A heist is zero-sum.** `heist_take` moves treasury + a slice of every *other*
  depositor's balance into the thief's wallet in one transaction, and returns the
  per-victim amounts so the group message can name who paid.

### Deposits must never count as ledger losses

A deposit leaves the wallet, so it lands in `size_log` as a large negative delta. The
nightly auto-handicap reads `size_log` to decide who is winning, so an uncounted deposit
would read as "this player is losing badly" and reward them with a growth bonus —
deposit, withdraw, repeat is then the cheapest exploit in the game.
`get_recent_net_by_user` therefore excludes the `bank_deposit` and `bank_withdraw`
sources. Any future feature that shuffles size between two pockets of the same player
must be excluded there too.

The per-day deposit cap counts **gross** deposits (`deposited_today`), so
deposit → withdraw → deposit cannot refill the allowance. This is what keeps size in
circulation and keeps `/dozdi` worth typing.

## Loans split principal from interest in the ledger

`loans` covers both lenders: `lender_id IS NULL` is the treasury-funded `/vam`, anything
else is a player-to-player `/nozul`. Both settle through `settle_loan`, so the two can
never drift apart.

The ledger split is the part that is easy to get wrong. A loan's **principal** is a
transfer between two pockets — not income for the borrower, not a loss for the lender —
so it is logged as `loan_principal`, which `get_recent_net_by_user` ignores exactly the
way it ignores bank transfers. Without that, taking a loan would look to the nightly
handicap like a catastrophic loss and quietly pay the borrower a growth bonus for
borrowing. The **interest** is the only real profit and loss in the arrangement, so it is
logged separately as `loan_interest` and *does* count — a player getting rich from usury
gets throttled like one getting rich from dice.

`_collect` charges interest against the wallet first and books at most what the wallet
actually paid. The size_log rows for a repayment must sum to the real change in
`users.size`, so interest paid out of a seized bank deposit is recorded in `bank_log`
only — the ledger cannot book money the wallet never paid.

Debt collection deliberately reaches into `bank_accounts`. The bank is safe from
*theft*; if it were safe from *debt* as well, then borrowing and immediately hiding the
proceeds in it would be a free money printer. Order is wallet → bank → negative wallet,
and the lender is made whole in every case, so a default is still zero-sum.

`LOAN_MAX_PRINCIPAL_RATIO` caps a loan at the borrower's current size. That bound is what
stops a single default from burying a player past any hope of recovery, and it is the
main lever if usury turns out to be too safe for lenders.

## Fees are transfers, never sinks

Every fee in the game moves size into `bank_treasury` via `db.treasury_add` (or, inside
a bank/transfer transaction, an inline treasury upsert). None of them delete size. This
matters because the treasury is the *only* thing funding deposit interest: a fee that
deleted size would quietly lower everyone's yield instead of raising it.

Current fees: theft loot (`THEFT_FEE_RATIO`), challenge winnings — never the returned
stake (`CHALLENGE_FEE_RATIO`), deposits and withdrawals (`BANK_*_FEE_RATIO`), and the
cross-group transfer (`XFER_FEE_RATIO`). The challenge settlement also sweeps the
`spread` — anything a shielding perk stops the winner collecting while the loser still
pays in full — which used to evaporate.

When adding a new fee, take it in the same transaction as the thing it is charging on,
and never charge it on money that is merely being returned from escrow.

## Cross-group transfer is the one seam between leagues

Groups are otherwise fully independent — the same player has a separate size in each.
`/enteghal` is the single exception and is priced at `XFER_FEE_RATIO` (30%) with a 24h
cooldown, because a player who is rich in one group could otherwise import that lead and
skip the other group's game entirely. The fee stays in the **source** group's treasury:
that is the group losing the wealth, so it keeps a cut.

The destination `users` row must already exist — you can only send to a league you
already play in, which stops this being a way to seed a fresh account somewhere. As with
loans, the principal is logged as `xfer_principal` (ignored by the handicap, since it is
the same player's money) while `xfer_fee` counts as a real cost.

## One-time data migrations go in `init_db`, guarded by `bot_meta`

`deposit_fee_backfilled` is the current example: it charges the deposit fee, once,
against balances that were banked before that fee existed. The guard row is what makes
it safe to leave in place — `init_db()` runs on every startup, so an unguarded data
migration would re-charge on every restart. There is a regression test that runs
`init_db()` three times and asserts the balances only move once.

## The credit score IS the borrowing limit

`users.credit_score` starts at `CREDIT_BASE` (100) and is applied as a multiplier —
`score / 100`, clamped to `[CREDIT_MIN_FACTOR, CREDIT_MAX_FACTOR]` — on top of
`LOAN_MAX_PRINCIPAL_RATIO` when working out how much a player may borrow. It is
deliberately not a cosmetic stat: behaviour feeds straight back into access to money, so
a player who defaults twice genuinely cannot get the loan that would let them do it
again.

The penalty is graded by how far the collector had to reach, which is the part worth
preserving if these numbers get retuned: paying late voluntarily (`CREDIT_LATE`) is a
slip; being force-collected from the wallet (`CREDIT_FORCED`) is a failure; having the
sweep dig into your bank deposit (`CREDIT_BANK_SEIZED`) or leave you in the red
(`CREDIT_SHORTFALL`) is worse. `settle_loan` decides which of these applies and writes
the score in the *same transaction* that moves the money, so a rating can never disagree
with the loan book it describes.

`BANK_LOAN_MIN_SCORE` gates `/vam` only. The official bank refuses bad credit outright;
loan sharks are unregulated and will lend to anyone, which is what `/etebar` is for — a
lender pays `CREDIT_CHECK_FEE` to price the risk themselves before making an offer. That
fee is a transfer to the treasury like every other fee, and unlike loan principal it
*does* count toward the nightly handicap, because it is a real cost.

## Admin panel

`python_bot/admin_panel.py` is a separate Flask/gunicorn service (`dickbot-admin`,
127.0.0.1:8011) served at **https://admin.inddex.app**. It imports `db.py` but never
`bot.py`, and runs as its own systemd unit, so panel and game fail independently.

- Secrets live in `/etc/dickbot-admin.env` (mode 600, outside the repo): the session
  secret, a werkzeug password *hash*, and the DB URL. The plaintext password is not
  stored anywhere in the repo or in git history.
- nginx config is mirrored at `deploy/nginx-admin.inddex.app.conf`. Cloudflare proxies
  the hostname in Full (strict) mode, so the origin needs a real cert for the
  subdomain — a self-signed one gives a 526. The port-80 block must keep serving
  `/.well-known/acme-challenge/` for renewals: Cloudflare only talks HTTPS to the
  origin for HTTPS requests, so plain HTTP on 80 is what carries the ACME challenge
  for a proxied hostname.
- `X-Real-IP` is set from `$http_cf_connecting_ip`, not `$remote_addr` — behind
  Cloudflare the socket peer is always a CF edge IP.
- The panel is also reachable at `https://inddex.app/dickadmin/`, where nginx strips
  the prefix. `ProxyFix(x_prefix=1, ...)` is what makes Flask build URLs under that
  prefix; without it every redirect goes to `/login` and the main site's SPA fallback
  answers with the portfolio page.
- Editing is curated: only `db.EDITABLE_USER_FIELDS` is writable and every value goes
  through `admin_panel.validate` (which rejects nan/inf — the value that once poisoned
  a balance permanently). Size edits route through `db.admin_adjust_size` so they land
  in the ledger like gameplay does.

### The nightly auto-handicap (`auto_handicap_job`)

Runs daily at 00:20 Tehran, after `midnight_tasks` has closed the day out. For each
group with at least `HANDICAP_MIN_PLAYERS` active players it reads
`db.get_recent_net_by_user(chat_id, HANDICAP_WINDOW_DAYS)` — **net gained recently from
the ledger, not current balance** — and nudges each player's `growth_mult` and
`theft_luck` toward a target derived from their distance above/below the group's
*median* net (mean absolute deviation is the spread measure; a single whale can't
inflate it the way a standard deviation would). Dials move only `HANDICAP_SMOOTHING` of
the way each night and are clamped to `HANDICAP_GROWTH_RANGE` / `HANDICAP_LUCK_RANGE`.

Two things to know before touching it:

- **`users.dials_locked` is the contract between this job and the owner commands.**
  `/setgrowth` and `/setluck` pin a player (setting a dial to exactly `1.0` unpins
  them); the job skips pinned players entirely. Without that flag the two systems
  write the same two columns and silently fight over them.
- **The lock backfill in `init_db()` must stay one-shot.** It is guarded by the
  `bot_meta` key `dials_lock_migrated`. `init_db()` runs on *every* startup, so an
  unguarded "lock everything that isn't 1.0" would re-fire on each restart and freeze
  every dial the job had legitimately moved.

Every decision is written to `rebalance_log`; `/balance [chat_id]` (owner-only) prints
the recent ones with before/after values.

### Size must be conserved — the invariants that keep it that way

Three separate leaks were fixed here, and all three are easy to reintroduce:

- **The spectator book is parimutuel, not a fixed 2×.** Winners split exactly what the
  losers staked, pro rata (remainder to the largest stake). A flat double payout mints
  size whenever the book is one-sided, which is the normal case — everyone backs the
  favourite. If *nobody* picks the winner the book is voided and every stake refunded,
  the same rule the tie branch follows.
- **Perks that shield a loser must shrink the winner's take to match** (`لاشی`,
  `کاندوم`) *and* perks that shrink the winner's take must shrink the loser's loss to
  match (`جاکش`) — otherwise the difference is silently destroyed, which is just as
  wrong as minting it.
- **`lottery_tickets.tickets` is entries (odds); `lottery_tickets.paid` is the size
  actually spent.** The pot is `SUM(paid)`. Bonus entries (the `خرشانس` perk, the
  `بلیت طلایی` item) pass `paid=0` — they buy odds, never prize money nobody funded.
  Before these were split, any bonus entry inflated the prize as well as the odds.

### Perks and items span three subsystems, not just challenges

Most groups spend their day on `/dozdi` and `/lottery` (theft outnumbered challenges
roughly 3:1 in the ledger), so perks reach into all three:

- Challenge dice/payout: `جاکش`, `کص‌کش`, `لاشی`, `کون‌گشاد`, `حروم‌دست`, `زن جنده`, `جقی`.
- Theft: `THEFT_CHANCE_PERKS`, `THEFT_LOOT_PERKS` (the thief's own perk) and
  `VICTIM_SOFT_PERKS` (the *victim's* perk — `سوراخ‌جیب` makes them easier to rob).
  `شب‌رو` halves the theft cooldown.
- Lottery: `خرشانس` doubles entries bought, `بدبیار` blocks buying entirely.

Items live in four buckets — `CHALLENGE_ITEMS`, `DIRECT_ITEMS`, `PASSIVE_ITEMS`,
`THEFT_ITEMS`, plus `INSTANT_ITEMS` applied immediately. **Theft items arm their own
slot** (`users.active_theft_item`), separate from the challenge slot: one shared slot
meant arming a glove silently disarmed the condom someone was holding. `activate_special_item`
is the single shared implementation behind both the inventory button and `/use`.

A perk's numbers and the text players are shown must not drift — that is a real bug
class here, not a hypothetical. `جقی` promised a wild dice swing in its description
while the code was actually randomising the *stake* at challenge-creation time (and
skewing it 25% upward); `زن جنده` had an undocumented +1 dice bonus. Both are fixed,
and there is a test asserting every perk in the roll pool has a description.

### The lottery draw is shared code

`python_bot/lottery.py` holds the draw itself (`draw`, `render_result`, `pending_pot`)
and imports only `db` — no telegram, no flask. Both `bot.draw_lottery` (midnight job +
startup recovery sweep) and the panel's draw button call it, so there is exactly one
implementation of "pick the winner and pay the pot". Do not inline a second copy: two
versions of a payout drift, and the drifted one pays real players the wrong amount.

`db.claim_lottery_draw` deletes the day's tickets as it reads them in a single
statement, which is what makes all three callers safe to race — the pot can only ever
be paid once.

### The size ledger

Every change to `size` is written to `size_log` from *inside* `db.update_size` and
`db.try_deduct_size`, not at the ~50 call sites, so coverage can't drift. The `source`
column is resolved by `db._caller_name()`, which walks the stack to the first frame
outside `db.py` — a fixed depth lands on `_retry_transient`'s wrapper and stamps the
same useless name on every row. This is the thing that makes "where did this player's
size come from" answerable; check it before adding any new size-moving path.

## Deployment

`.github/workflows/deploy.yml` runs on every push to `main` (and via manual `workflow_dispatch`): SSHes into the production server, `git reset --hard origin/main`, `pip install -r requirements.txt`, `systemctl restart dickbot`, then dumps the last 200 lines of `journalctl -u dickbot` into the workflow log — this is the primary way to check for a clean startup or a crash after shipping a change (grep for `Traceback`/`ERROR`). There is no staging environment; every merge to `main` is live in production immediately.

### The crown is the game's balancing mechanism

The player with the largest size in a group is its king (`refresh_king` recomputes this
from the leaderboard; `db.crown_king` empties the consort seat whenever the crown
changes hands, because the consort belongs to the throne rather than to the person).
The crown deliberately cuts both ways: it collects `KING_TAX_RATIO` of every player's
size each Tehran midnight, and in exchange its wearer loses double in a challenge. This
exists because a runaway leader had made the top of the leaderboard uncontestable —
treat "being #1 must stay dangerous" as the invariant when touching any of it.

Consensus protection is *not* part of that trade: it applies to the king exactly as it
does to everyone else. An earlier version exempted the king, which meant the group
could run back-to-back consensus votes on one person — the precise thing the cooldown
exists to prevent.

The consort (`/hamsar`, king-only, once per Tehran day) takes `CONSORT_TAX_SHARE` of the
tax and can't be robbed — but can defect at any moment with `/khianat @user`, taking
`KHIANAT_STEAL_RATIO` of the king's size and splitting it with whoever they left for.
Betrayal is deducted from the king with `try_deduct_size` first and only then paid out,
so it can never mint size when the treasury is short.

### Per-target rate limits

Two limits are enforced on the *receiving* end rather than the acting end, because
gating the actor leaves the obvious hole open (four people each spending one item on
the same target, or a fresh account being fed by an established one):

- `db.try_claim_dose` — a player can only have a ویاگرا / قرص اورژانسی applied to them
  once per `DOSE_COOLDOWN_HOURS`. Claim the slot *before* consuming the giver's item
  and `db.release_dose` if that consume then fails, or a blocked dose costs someone
  their item. زعفرون is deliberately exempt.
- `db.get_donation_wait_remaining` is checked for both the donor and the recipient, so
  `DONATION_MIN_DAYS` gates receiving a donation as well as making one.

### Size sources and sinks

Growth, boss rewards and the viagra-rain/treasure events *create* size; the shop, the
lottery burn (`LOTTERY_BURN_RATIO`) and the earthquake event *destroy* it. Everything
else (tax, theft, betrayal, challenges, donations) only moves it between players and
must stay exactly zero-sum. When adding a feature, be explicit about which of the three
it is — the economy inflated badly once because every mechanic was a source.
