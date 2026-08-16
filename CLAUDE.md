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
