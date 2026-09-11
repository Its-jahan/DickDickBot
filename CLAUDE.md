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

### Run the handlers, not just the SQL

The db-layer suites all passed while `/shop` was dead: `build_shop_keyboard` referenced
a `chat_id` it was never passed, which is a `NameError` at call time and completely
invisible to SQL-level tests. Anything that touches `bot.py` needs a test that actually
*calls* the handler.

`test_commands.py` drives every player-facing command against fake `Update`/`Context`
objects and asserts it neither raises nor stays silent. It needs no Telegram and only a
scratch Postgres, so it is cheap to extend — add a line to its `cases` list whenever you
add a command. Note the shape of the fakes: `reply_text` records instead of sending, and
`FakeUpdate` supplies `effective_user` / `effective_chat` / `message`, which is the
minimum every handler here touches.

There is also a static pass worth keeping in mind for this kind of bug: walking the AST
for names a function loads but never binds (accounting for module globals, imports and
closures) finds exactly this class of defect across the whole file in one go.

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


## One event, one message — the bot edits rather than posts again

Groups are noisy, and almost all of the noise was the bot answering itself. The rule now
is that **a message the bot already owns gets edited; a second message is the exception
that has to justify itself.**

### The nightly report

Every group used to receive six to eight separate messages between 00:00 and 00:20
Tehran — the lottery result, the boss that escaped, the king's tax, the growth reminder,
the price index at 00:05, the bank's interest at 00:10, and one per collected loan at
00:15. They are all the *same* event, the day turning over, so they are now **one message
that each job edits**.

`bot.night_report(context, chat_id, key, rank, body)` is the whole interface, backed by
`night_reports` (the message id for a (chat, day)) and `night_report_sections` (the
sections). Four things about it are load-bearing:

- **`key` is unique per night**, so a job that runs twice — a restart, a recovery sweep —
  overwrites its own section rather than printing it again. The nightly claims already
  make the *money* idempotent; this gives the *text* the same property. Per-loan sections
  use `loan:<id>` so several collections coexist without colliding.
- **`rank` fixes the reading order, not arrival.** The reminder is written at 00:00 but
  reads as the header; loans land at 00:15 and belong at the bottom.
- **The message id is in the database, not in memory.** The jobs are twenty minutes apart
  and a deploy lands between them often enough to matter — same lesson as `pvp_matches`.
- **It degrades to sending, and re-adopts.** If the edit fails (the report was deleted)
  the whole report is re-posted and the new message becomes the report, so a deletion
  costs one repost rather than turning the rest of the night back into a message per job.
  Past `NR_MAX_CHARS` it stops absorbing and the new section goes out alone. Losing the
  running report is survivable; losing the night's news is not, and a refused edit would
  do exactly that.

The report is assembled as HTML, so anything not already escaped must be passed with
`html_safe=False` (or `_esc`'d by the caller). An unescaped player name would break the
whole report, not just its own line — that is why the escaping is at the boundary.

`night_report_prune` keeps a week. The report is a display artefact; nothing reads an old
one.

### One tap, not two: `/d` grows on the first message

`/d` used to post *"X is about to grow…"* with a button X then had to press — two
messages and two taps for a thing X had already asked for by typing the command. In a
group the chat is known from the command itself, so the button resolved nothing and
confirmed nothing.

`perform_growth(user, chat_id)` is the roll, and it deliberately **touches no Telegram
object**: it returns `(True, text)` or `(False, why not)`. `dick()` replies with the
text, `grow_callback` edits its message into it. Two copies of the dice is the drift this
repo keeps getting bitten by, so there is a test asserting neither handler calls
`roll_nonzero` itself.

**Inline still needs its button, and that is not an oversight.** Telegram never says
which group an inline query was typed in, so the chat can only be resolved from the
concretely-sent message the button is attached to (`resolve_chat_id`). That is the same
reason the tap-to-reveal fallback exists for ambiguous multi-group users.

Two more things the tests pin, because both were easy to lose in the split:

- **The refusals stay cheap and the success stays permanent.** "You already grew today"
  is chatter and gets `reply_temp`; the roll moved size, so it is the record and gets a
  plain `reply_text`.
- **A refused roll must not burn the day.** `claim_daily_growth_with_streak` is what
  stamps it, and it runs *after* the prison check, so a jailed player still has their `/d`
  tomorrow.

The rest of the buttons in the bot are not this pattern and were left alone: a shop,
inventory, lottery or decree keyboard is a **menu** (the tap chooses something), and a
challenge, heist, `/ejma` or loan button is tapped by **somebody else**. `/vam` is the
one self-confirm that stays: the button is where the origination fee and the repayment
total are first shown, so signing it is real consent to a price rather than a second ask.

### Throwaway chatter is swept; the record is not

The other half of the noise is the chatter around the game: the command somebody typed,
the refusal it got back, and the personal lookup nobody else is reading. None of it is
part of what *happened*, and in a busy group it is most of what is on screen. All three
are now scheduled for deletion:

| | lives for | why |
|---|---|---|
| the typed `/command` | `COMMAND_MESSAGE_SECONDS` (30s) | superseded the moment the bot answers |
| a refusal | `EPHEMERAL_ERROR_SECONDS` (20s) | read once, by one person, never again |
| a personal lookup | `EPHEMERAL_LOOKUP_SECONDS` (120s) | regenerable by typing the command again |

`reply_temp(update, context, text)` and `reply_lookup(...)` are the two helpers;
`sweep_later(context, chat_id, message_id, seconds)` is what they sit on, and the
command message is swept from `log_incoming` — the one handler that sees every message,
which is why it is there rather than in forty command handlers.

Five things are load-bearing:

- **The record must never use them.** A challenge result, a theft, a heist, a trade, a
  deposit, a decree, the nightly report — anything that moved size or carries the buttons
  the game runs on — stays forever. There is a regression test naming those handlers and
  asserting each still has a permanent `reply_text`, in both directions: that the noise
  is swept *and* that the record is not.
- **`OUR_COMMANDS` is why another bot's users keep their messages.** Plenty of groups run
  several bots, and sweeping everything that starts with a slash would delete `/ban` out
  from under whoever typed it. It is filled twice on purpose: `cmd()` harvests every name
  in every handler pattern (so a command added tomorrow is covered without anyone
  remembering), and `_seed_our_commands()` reads `BOT_COMMANDS` at import (because
  `cmd()` only runs when handlers register, which never happens when `webapp.py` imports
  this module or when a test does). Neither alone is enough — the menu has no aliases.
- **Nothing is swept in a DM.** `sweep_later` returns immediately for `chat_id >= 0`:
  there is no group to keep tidy and no delete right to do it with.
- **Every failure is swallowed.** The bot may not be an admin, the message may be gone,
  or it may be past the 48 hours Telegram lets a bot delete. Tidying is cosmetic and must
  never cost somebody their answer — there is a test that a `job_queue` of `None` still
  lets the reply go out.
- **Cleanup is in-memory (`job_queue.run_once`), deliberately.** If the process dies
  first the message simply stays, which is exactly where the bot was before. Persisting it
  would put a cosmetic concern in the same class as the money, and the recovery sweeps
  exist for the money.

When you add a command: a refusal gets `reply_temp`, a read-only answer gets
`reply_lookup`, and anything that moved size keeps `reply_text`.

### Badges and coronations ride along

`badge_lines(who, earned)` returns text to **append** to the message that caused the
badge. There is deliberately no function that posts a badge on its own any more — the old
`announce_achievements` is gone rather than merely unused, because leaving it there is an
invitation to send one more message.

The worst offenders it replaced:

- A settled challenge posted the result, the winner's badges, the loser's badges and a
  coronation — four messages for one event. All four are now the single edit
  `deliver_pvp_message` was already making. `coronation_text` exists for exactly this:
  `announce_coronation` is now the thin wrapper for callers that have no message of their
  own to attach to.
- A boss killed by five players posted five badge messages and then the rewards. One now.
- `/d`, theft, the consort, betrayal and the heist each posted one or two extra.

When you add anything that awards a badge: award it **before** you build the message, and
paste `badge_lines(...)` on the end. `html=False` for a plain-text message
(`deliver_pvp_message` sends without `parse_mode`), the default for an HTML one.

## The bank is deliberately outside `users.size`

`bank_accounts.balance` is a second balance per (user, chat) that is **not** part of
`users.size`. Nothing that reads `users.size` — the leaderboard (`get_top_users_full`),
the crown (`refresh_king`), `/dozdi`, `/ejma`, challenge stakes — can see banked size.
That is the whole trade the feature sells: deposits are safe from theft precisely
because they cost you your position on the table. Do not "fix" this by adding banked
size into the leaderboard; it would turn the bank into a strictly-dominant safe box and
kill theft and challenges in one move.

Two invariants hold the economy together, and both have regression coverage:

- **The bank cannot mint size.** Interest is paid *only* out of `central_bank.reserve`, and
  `pay_interest` scales every depositor down by the same factor when the treasury can't
  cover what's owed. An empty treasury pays exactly zero. **The rate itself now moves
  with the treasury** (see below), so that proportional haircut is the backstop rather
  than the normal case it used to be. The treasury's only inflows
  are real sinks — shop purchases, the lottery rake (`lottery.BURN_RATIO`), `/ejma`,
  shrink items (قرص/زعفرون), and earthquakes — each of which used to simply delete
  size. If you add a new sink, route it through `db.treasury_add` rather than dropping
  the size on the floor.
- **A heist is zero-sum.** `heist_take` moves treasury + a slice of every *other*
  depositor's balance into the thief's wallet in one transaction, and returns the
  per-victim amounts so the group message can name who paid.

### Balances are local. The bank is global.

That one line is the whole design, and the two halves are separate on purpose:

- **`users.size` is per `(user, chat)` and always will be.** 100 in one group and 1000
  in another are two unrelated numbers. Every group is still an independent league with
  its own leaderboard, its own king, its own theft and its own challenges.
- **There is exactly one treasury for the entire bot**, stored as `central_bank.reserve`
  — a single number, not a vault per group and not a pool of member accounts. Every sink
  feeds it and every payout comes out of it, whichever group the player was standing in.
  `bank_treasury` still exists but holds only per-group *bookkeeping*: which day this
  group's interest was last paid, and when its last heist was attempted.

An earlier version kept a `balance` per group and derived the pool as their `SUM`. That
was a safe way to merge the *behaviour* without touching live balances, but it left the
storage saying something the game no longer meant, and every read had to decide whether
it wanted the group's share or the sum. `init_db` now folds those rows into the one
number and **drops the column**, guarded by the `bot_meta` key
`treasury_merged_global`. Dropping rather than zeroing is deliberate: a stale column
that still looks authoritative is the drift bug class this repo keeps hitting, and code
that still reads it should fail loudly instead of quietly seeing `0`.

The same migration also clears `economy.supply_last`, because `get_money_supply` no
longer counts a treasury share — without that reset, the first night after the deploy
would read the *definition change* as a collapse in every group's money supply and
deflate every price in the game.

**One group's claim on the pot is bounded, and that bound is load-bearing.** A heist
takes a fraction of "the treasury", and so does a corrupt decree. Against one global pot
those would let a lucky player in the smallest group on the bot walk off with the vault
backing every other group's savers — the exact failure the old per-group split prevented
by accident. So `db._group_weight` derives a group's weight (its wallets + deposits over
every group's) and `_group_claim` scales the draw by it. It is **derived, never stored**:
there is no per-group treasury for it to be a balance of, and it exists only to bound
withdrawals, never to hold money. Negative wallets are floored at zero — a group carrying
a big debtor is not thereby smaller.

Interest is deliberately **not** weight-capped: that is the bank honouring a debt it owes
to named savers, not a group helping itself to the pot. A group whose players have paid
nothing into the treasury still pays its savers, funded by the groups that have. That is
what a shared bank *is*, and there is a regression test asserting it.

The other thing that stays per group is **deposits remember which group they were made
in.** Withdrawals only work in the group the deposit was made in. If they didn't,
"deposit in the farm group, withdraw in the main group" would be a free, frictionless
cross-group transfer and would reopen the exact farm-group exploit the `/enteghal` gate
exists to stop. There is a regression test asserting it.

### Deposits fund loans, which is what makes it a bank and not a box

`accept_loan`'s treasury branch takes `principal` out of the deposits and books it
as `loans_out`; `settle_loan` retires the principal and books **only the interest** as
reserve. Booking the whole `due_amount` as reserve — which is what it used to do — would
count the principal twice now that it came from deposits rather than from the vault.

There is no default loss for the bank to absorb, and that is deliberate rather than an
oversight: `_collect` drives the borrower's wallet negative for anything they can't
cover, so the lender is made whole every time and the hole stays the borrower's problem.

Two limits bound the loan book, and both are real:

- `CB_RESERVE_RATIO` keeps that share of **deposits** permanently un-lent. The bank's own
  equity (the reserve) *is* lendable on top of that — a real bank lends its capital as
  well as its depositors' money, and without it `/vam` would be dead in a world where
  nobody has banked anything yet.
- The cash check: `reserve + deposits - loans_out` must cover the principal today.

### The bank run is a real failure mode now

`bank_withdraw` checks the bank's cash **before** touching the saver's account, and
returns `(False, 'run', available, 0)` when the money is real but currently sitting
inside somebody's `/vam` loan. The saver's balance is left exactly where it was, and
`/bardasht` says so plainly instead of implying they're broke. `CB_RESERVE_RATIO` is
sized to make this rare — ordinary withdrawals always clear — but it can and should
happen when enough savers head for the door at once.

`/markazi` prints the balance sheet publicly, laid out so that **deposits appear as a
liability** and the loan book as the asset. Players kept assuming their deposit was the
bank's money sitting in a box; the reserve requirement and the run risk only make sense
once you can see that it isn't.

### The deposit rate floats on how well the treasury covers the deposits

The rate is not a constant. `bank_base_rate` interpolates between `BANK_RATE_MIN` and
`BANK_RATE_MAX` across a coverage band (`BANK_COVERAGE_POOR` → `BANK_COVERAGE_RICH`),
where **coverage is `treasury / total_deposits`** — the honest measure, since a
1000-size vault is rich against 500 of deposits and broke against 50,000. The crown's
`interest_mult` still multiplies the result, so the king's lever keeps working.

This replaced a flat advertised rate that the treasury then quietly failed to honour,
scaling everyone down at payout time. Two reasons the floating rate is better, and both
are the point of the feature:

- **A moving rate is a signal; a broken promise is not.** A thin vault visibly pays less
  instead of promising 4% and delivering a haircut.
- **It closes a feedback loop.** The treasury is filled by real sinks — shop purchases,
  fees, the lottery rake — so "spend more and everyone's interest goes up" is now a true
  statement players can act on. The nightly announcement says so explicitly.

`bank_effective_rate(chat_id)` is the single source of the quoted number: the nightly
job, `/bank` and `/eghtesad` all call it, so what a player is shown is exactly what gets
paid. Do not reintroduce a constant for display — a shown number drifting from the real
one is a bug class this codebase has already been bitten by.

### …and on whether the bank actually earns anything

Coverage is a measure of **stock**, and a bank can be rich and still be dying. The
production ledger showed exactly that shape: deposits growing ~19k net a week while the
only recurring income was fees on other people's activity. The interest bill scales with
deposits; the earnings do not. Left alone, the liability outruns the income no matter
how full the vault looks today.

So `bank_effective_rate` takes the **lower** of two caps, and both bind:

- the coverage rate above (`bank_base_rate`), and
- `bank_income_cap(deposits, income)` — what the bank's own earnings over
  `BANK_INCOME_WINDOW_DAYS` could actually fund, paying out only
  `BANK_INCOME_PAYOUT_SHARE` of them so the rest is retained and rebuilds the reserve.

They catch different failures: coverage catches a **drained** vault, income catches a
vault that is **full but unprofitable**. `BANK_RATE_MIN` floors the income cap, so a bad
week throttles the rate rather than switching interest off — absorbing that is what a
reserve is for.

`db.get_treasury_income(days)` counts **only `treasury_in` rows**, which is deliberately
narrower than "the treasury went up". A crypto buyer parking size against a position is
logged as `crypto_in` and must never read as income, because the bank may have to hand
every centimetre of it back on the next sale. If you add a path that moves size into the
treasury, decide explicitly which of the two it is.

### The account-maintenance fee is what makes the rate self-funding

`BANK_MAINTENANCE_FEE_RATIO` is charged nightly on every deposit balance, straight into
the one reserve. It is the structural half of the fix:
the fee is levied on **precisely the number the interest bill is levied on**, so cost and
income scale together by construction rather than by luck. A saver nets
`(rate − fee)` a day.

Three details that are load-bearing:

- It is charged **after** interest lands and **inside the same `claim_interest_run`
  slot**, so a restart can no more double-charge the fee than it can double-pay the
  interest.
- It lands in the one reserve like every other fee. It used to be charged to the
  group's own member account specifically so that this group's savers funded their own
  maintenance; with a single pot there is nowhere else for it to go, and the
  cost-tracks-income argument above is unaffected because both are levied on the same
  deposit base.
- It is logged as `treasury_in`, so it feeds the income window above. That is the
  feedback loop: a bigger deposit base directly funds the rate paid on it.

The fee does **not** by itself make a night profitable — at a 6% rate against a 2% fee
the bank still runs a deficit, and the income cap is what stops that continuing
indefinitely. The regression test asserts the accounting identity (the night's treasury
change is exactly fee collected minus interest paid), not profitability.

### Lending is a business, not a favour

The ledger's clearest finding: players lending to each other with `/nozul` had earned
~14,770 in interest against the official bank's ~417 from `/vam` — **35×**. The loan
market is where the money in this game actually is, and the bank had no share of it.

Two changes, and the first matters more than it looks:

- **`bank_loan_rate` is derived from what the bank pays savers**, never a constant
  sitting next to it: `base_deposit_rate × LOAN_TERM_DAYS × BANK_LOAN_SPREAD`, clamped
  to `[BANK_LOAN_RATE_MIN, BANK_LOAN_RATE_MAX]`. A fixed loan rate against a *floating*
  deposit rate is a spread that can silently go negative. It anchors to the **base**
  rate rather than the crown-adjusted one, so a king who halves his group's interest
  doesn't thereby get cheap loans — the bank's margin is a property of the bank, not of
  local politics. There is a test asserting the spread survives at both ends of the band.
- **`BANK_LOAN_ORIGINATION_RATIO` is withheld from the disbursement**, not billed later:
  the borrower receives `principal − fee` but still owes the full `due_amount`. That
  ordering is the whole point — it is the one part of a loan the bank collects **even
  when the loan later defaults**, and there is no path where the fee fails because a
  wallet was empty. `accept_loan` therefore returns four values now.

A player lender is charged nothing of the sort: `/nozul` is unregulated, which is half
the reason it exists.

### Deposits must never count as ledger losses

A deposit leaves the wallet, so it lands in `size_log` as a large negative delta. The
nightly auto-handicap reads `size_log` to decide who is winning, so an uncounted deposit
would read as "this player is losing badly" and reward them with a growth bonus —
deposit, withdraw, repeat is then the cheapest exploit in the game.
`get_recent_net_by_user` therefore excludes the `bank_deposit` and `bank_withdraw`
sources. Any future feature that shuffles size between two pockets of the same player
must be excluded there too — `crypto_principal` is the newest one, for exactly this
reason.

The per-day deposit cap counts **gross** deposits (`deposited_today`), so
deposit → withdraw → deposit cannot refill the allowance. This is what keeps size in
circulation and keeps `/dozdi` worth typing.

## The heist is a real game, not a hidden dice roll, and losing has consequences

`/sarghat` used to be a single `random.random() < chance` check, then a single memory
game. It is now a **three-stage job that two people have to pull off together**, and it
is deliberately tuned to be *nearly impossible* — the bank is supposed to be safe, and a
heist is the rare exception that proves it. A pure guesser is at about **1 in 21
billion**, and there is a test asserting that number stays past one in a billion.

```
stage 0  the offer      the named accomplice has to actually accept
stage 1  the alarm      the ACCOMPLICE cuts the wire, on an unpredictable cue
stage 2  the vault      the THIEF plays the symbol-memory game
stage 3  the getaway    BOTH have to tap out before the clock runs
```

One wrong tap at any stage is an instant loss — there is no partial credit for getting
most of the way through, and `advance_heist_attempt` / `cut_heist_wire` flip the row to
`'lost'` right there.

### The partner is mandatory by construction, not by rule

Stage 1 is tapped by the accomplice and stage 3 needs both, so **one player physically
cannot finish the job**. That is a far stronger guarantee than a rule saying they may
not, and it is why the whole feature is built around the split rather than bolting a
partner onto the end.

Three things follow from it, and all three are load-bearing:

- **The accomplice accepts; they are never merely named.** Losing jails *both*, so being
  volunteered into four days of prison by somebody else's tap would be the worst button
  in the game. `create_heist_offer` opens at `status='offered'` and nothing runs until
  `accept_heist_offer` flips it.
- **The group's cooldown slot is claimed before the invitation goes out and released if
  it is declined or expires.** Claiming up front is what stops two players both opening a
  heist; releasing is what stops a player burning the group's five days by @-ing someone
  who was asleep. Same claim/release shape as `claim_war_day`/`release_war_day` — copy it,
  don't reinvent it. A declined offer is `'cancelled'`, deliberately **not** `'lost'`:
  nobody tried to rob anything, so nobody goes to prison.
- **Neither conspirator is robbed.** `heist_take` skips the thief *and* the partner when
  taking its slice of every depositor — taking a cut off your own accomplice and then
  handing most of it back to them is not a bug so much as an insult.

The take is split `HEIST_PARTNER_SHARE` to the accomplice, and the **partner's cut is
rounded first with the thief taking the remainder**, so the two payouts sum to the total
exactly. Two independent roundings is how you mint a centimetre out of nowhere, and a
heist has to stay as zero-sum as it was when one person carried the whole bag. On a loss
both are sentenced on their **own** share of the would-be take, so the partner's bail and
labour are scaled to what they stood to gain.

### Each stage has its own clock, and its timeout must claim on the stage

Every stage schedules its own timeout job, which means a pair who cleared the alarm with
a second to spare has a stage-1 timeout still in flight. `db.claim_heist_stage_timeout`
therefore puts the stage **in the `WHERE` clause** — checking it in Python first and then
settling leaves exactly the race it is there to close. There is a regression test that
clears the alarm and then fires the stale stage-1 timeout at it.

The row's `expires_at` stays the *whole-run* deadline the startup sweep reads, so a
process that died between stages is still swept exactly as before.

### The sequence is never shown all at once — that is the anti-cheat

The first version printed the whole order as one line of text. Players simply copied the
message (or screenshotted it) and read it back, which made the "memory game" a formality.

So the sequence is now revealed **one symbol at a time**: `heist_reveal_step_job` edits
the *same* message once per symbol, each frame overwriting the last, then a deliberate
blank frame (`HEIST_BLANK_SECONDS`) wipes the final symbol before `heist_keypad_job`
puts the keypad up. At no instant does any message, copy-paste, or single screenshot
contain more than one symbol of the answer. There is a regression test asserting exactly
that: no frame — the opening message included — may contain two symbols of the pool.

Three more things make guessing or half-remembering useless, and all three matter:

- The sequence is drawn **with replacement** from `HEIST_SYMBOLS` (9 of them), so
  repeats are possible. This kills the old "each symbol appears exactly once, so cross
  them off as you go" shortcut, which had made the last few taps free.
- **Buttons are never removed as they're used.** They can't be — a removed button would
  make a legitimate repeat untappable. The keypad is *reshuffled* after every tap
  instead, so nobody can pre-record positions. `callback_data` carries the symbol index,
  never the position, which is what makes a reshuffle purely cosmetic to correctness
  (and why a failed reshuffle edit is safe to swallow).
- `HEIST_RECALL_SECONDS` is tight enough that reassembling the answer from screenshots
  on a second device loses to the clock.

**`HEIST_MIN_VAULT` is not a balance dial.** The weight cap already decides how much a
group can take; the floor only stops a heist being staged against literally nothing. Set
high (it was 800), it silently locked small leagues out of the feature altogether — and
because the refusal said *"the vault is nearly empty"* while the shared treasury held
22,000, it read to players as a bug in the bank rather than a property of their group.
The refusal now prints the real reserve, this group's share of it, and why the share is
what it is. A displayed number that is true but unexplained is its own kind of drift.

A pure guesser is at `(1/6) × (1/9)^10` ≈ **1 in 21 billion**. The dials to retune if it
ever needs to be easier or harder, in order of effect: `HEIST_SEQUENCE_LENGTH`,
`HEIST_REVEAL_STEP_SECONDS`, then `HEIST_RECALL_SECONDS`.

**Stage 1 is memory too, not just reaction.** The wire colour is named exactly once, when
the accomplice accepts, and never shown again — by the time the six buttons appear it has
to be in their head. The cue lands at a random `HEIST_ALARM_MIN..MAX_SECONDS` so nobody
can pre-aim, and `HEIST_CUT_SECONDS` is short. Tapping *before* the cue is refused rather
than treated as a loss (`cut_heist_wire` returns `'early'`): there is no button to press
yet, so a stray tap is a client glitch, not a decision.

One operational note: the reveal is a chain of ~10 edits to one message in ~13 seconds.
`heist_reveal_step_job` therefore schedules the next step **whether or not its own edit
succeeded** — a dropped frame (flood control, a deleted message) must never stall the
chain and leave the keypad never appearing, which would strand the attempt until the
timeout.

### It's a persisted attempt, not in-memory state — same lesson as `pvp_matches`

`heist_attempts` holds the whole job: both conspirators, the wire, the memorized
`sequence`, `progress`, which `stage` is live, and both getaway flags. **The wire and the
sequence are rolled once, at `create_heist_offer`, not per stage** — a job that survived a
restart would otherwise have to re-roll, and a re-rolled answer is a different game from
the one the player was shown.

`resolve_heist_attempt` is the single settlement function, called from a losing tap, each
stage's timeout job, and the startup sweep `recover_stuck_heist_attempts` (a
`run_once(..., when=14)`, alongside the other recovery jobs). Three atomic claims keep
those from ever settling the same attempt twice — `claim_heist_stage_timeout` (stage
scoped), `claim_expired_heist_attempt` (whole run), and `cancel_heist_offer` — so copy
the `pvp_matches` pattern here, don't reinvent it, if this ever needs another mini-game.

`get_heist_attempt` returns a **dict**, not a tuple. There are eighteen columns now and a
positional unpack of that many is a bug waiting to happen every time one is added.

### Losing sentences both of them; winning doesn't touch prison at all

A bust used to just cost a fine, and used to fall on one person. Both conspirators now
serve it — that shared risk is exactly what the accomplice agreed to, and it is what stops
a heist from being a costless favour you do for a friend. `db.send_to_heist_prison` sets two separate
timestamps: `heist_prison_until` (a hard `HEIST_PRISON_DAYS`-day lockout — `db.is_in_heist_prison`
gates `/d`, `/c` on both the challenger and acceptor side, `/dozdi`, and `/sarghat`
itself) and `heist_labor_until`, which runs `HEIST_PRISON_DAYS` + a scaled
`_heist_labor_days(would_be)` (bigger attempted heist → longer labor, capped at
`HEIST_LABOR_MAX_DAYS`) past prison. Once prison ends but labor hasn't, the player can
act normally except that `grow_callback` skims `HEIST_LABOR_TRIBUTE_RATIO` of any
*positive* growth roll to the current king — the exact same shape as the jester tribute,
deliberately: two unrelated punishments that both dock a cut of growth to whoever holds
the crown, so a future one should follow the same pattern rather than inventing a third
mechanism. Neither tribute is excluded from the nightly handicap's ledger read, matching
existing precedent for the jester.

**The king can never be the thief.** `heist_cmd` checks `refresh_king(chat_id)[0] ==
user.id` before anything else moves — a king who could rob his own treasury and then
tax the resulting jester/laborer pool would be self-dealing in a way nothing else in the
game allows.

### Bail buys out prison, never the labor debt

`heist_bail_amount` is frozen once, at sentencing (`priced(would_be * HEIST_BAIL_RATIO,
chat_id)`), so it can't drift with inflation while the thief is stuck inside. `/vasighe`
→ `db.pay_heist_bail` is one transaction: check the bail is still owed and affordable,
deduct it, clear `heist_prison_until`, credit the treasury (bail is a fee like any
other, never destroyed). It deliberately does **not** touch `heist_labor_until` — paying
your way out buys freedom of movement, not freedom from the king's cut, which is what
stops bail from being a strictly-dominant way to skip the whole punishment.

### The king's pardon is wider than bail, and that asymmetry is the point

`/afv` → `db.pardon_heist_prisoner` clears `heist_prison_until`, `heist_labor_until`
**and** `heist_bail_amount` in one statement. It goes further than bail on purpose: the
labor tribute would have flowed into the king's own pocket, so he is the only one
entitled to forgive it, and forgiving it costs him real size he'd otherwise collect.
That price is also why the power needs no cooldown — it is self-limiting in a way
`/hokm` (which spends unrest instead) is not.

Two guards, both load-bearing: only the sitting king may call it (`refresh_king`), and
**he may never pardon himself**. A jailed player can still hold the crown — prison stops
them growing, not being biggest — so without the self-check a jailed king would simply
walk himself out. `db.pardon_heist_prisoner` returns whether a live sentence actually
existed, so the command can tell "pardoned" apart from "this player wasn't serving
anything" instead of silently claiming success.

## The spectator book has a house, and the treasury is its bankroll

A correct spectator guess pays `BET_PAYOUT_MULT` × the stake, **whether or not anybody
backed the other side**. The parimutuel version that came before was arithmetically
tidy and unplayable: since spectators overwhelmingly pile onto the same player, the
usual outcome was "you called it right, here's your own size back".

The payout is funded in a strict order, and the order is the whole design:

1. **The losing side's forfeited stakes.** A balanced book never touches anything else.
2. **The treasury** (`db.treasury_take_up_to`, which draws at most what's there and
   reports how much it actually got).
3. **A mint**, for whatever is still short.

Money flows the other way too, which is what makes the house solvent rather than a
permanent drain: surplus (losers staked more than the winners are owed) goes into the
treasury, and when *nobody* picks the winner the entire losing pool does. So the house
wins rounds as well as losing them.

**What this does and doesn't cost.** While the treasury can cover the winnings, a
one-sided book changes the money supply by exactly zero — it pays out of the house on a
win and refills the house on a loss, and there is a regression test asserting precisely
that (`test_spectator_book.py`). Minting only happens when the house is momentarily
broke, so the drift is the "reflecting barrier at zero" of a roughly symmetric walk —
sub-linear, not the `+stake/2` per bet a naive flat payout would cost. Two real costs
remain, and both are deliberate: an informed bettor backing a perk-advantaged player has
a genuine edge, and a long unlucky streak against an empty treasury mints. `BET_PAYOUT_MULT`
is the dial — drop it below 2.0 to give the house a rake.

A tie still voids the book and refunds every stake, unchanged: nothing was decided.

## Inter-group war is the only thing that moves size between leagues

Once a day (`WAR_HOUR`, with `recover_group_war` as the startup catch-up) two eligible
groups are drawn at random and one raids the other: `WAR_LOOT_RATIO` of each recently
active defender's **wallet**, shared equally among the raiding group's active players.

**It is zero-sum globally, but deliberately not per group.** Every centimetre taken off
a defender lands on an attacker inside the same `db.execute_group_war` transaction — the
function recomputes both sides and refuses to commit if they don't balance, because a
raid that silently minted into one league and burned another would be the worst bug this
feature could have. But the raided group's money supply really does shrink and the
raider's really does grow, so `tick_inflation` gives the loser cheaper prices and the
winner dearer ones that same night. That is the intended consequence, not a leak.

Taken proportionally, shared equally: the biggest wallets in the losing group pay the
most, and the spoils are split per head rather than weighted toward whoever was already
winning. **Deposits are untouched** — the bank stays safe from a raid exactly as it is
from `/dozdi`, which is the trade that feature sells; only a heist ever reaches deposits.

`war_eligible_groups()` keeps fake groups out of it: `WAR_MIN_PLAYERS` recently-active
players and `WAR_MIN_GROUP_AGE_DAYS`, with `chats.xfer_policy` honoured in both
directions ('blocked' opts a group out, 'trusted' opts it in). The bar is deliberately
lower than the cross-group transfer gate's because nobody *chooses* the pairing here, so
the farm-group exploit that gate exists to stop doesn't apply — and the two features
guard each other anyway: size raided into a fake group is stuck there, because the
transfer gate won't let it out.

Two ordering details worth keeping:

- The day is claimed (`db.claim_war_day`, one war per day for the whole bot, atomic via
  `bot_meta`) **before** anything moves, and `db.release_war_day` hands it back when the
  raid can't actually be staged — no eligible pair, an empty defender, or an exception.
  Without that, one bad draw silently burns the whole day.
- Both groups' crowns are re-checked afterwards, since a raid moves enough size to
  change who is biggest on either side.

War gains and losses are logged as `war_loot` / `war_loss` and are deliberately **not**
excluded from `get_recent_net_by_user`: unlike a bank or loan transfer, they are real
income and real loss, so the nightly handicap should see them.

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

### Collection reaches every league, because the money came from every league

`/vam` is funded out of the central bank's **pooled** deposits — size that every group's
savers paid in. A debt to it is therefore a debt to the whole bot, not to one league, and
`_collect` is built to match:

```
1. the home group's wallet      (where the loan was taken)
2. the home group's deposit
3. EVERY OTHER GROUP the borrower plays in, richest first: wallet, then deposit
4. only then, the home wallet goes negative for whatever is still short
```

**Stopping at the home group left an unstoppable dodge**: borrow in a group you keep
empty, let that one wallet go negative, and keep everything you own in every other
league. No fee or cap closes that — the lender's money is global, so the collector has to
be too.

Three details are load-bearing:

- **Home first is not arbitrary.** The loan was taken against that group's standing and
  its wallet is the one that agreed to it; other leagues are only reached for what the
  home group genuinely could not cover.
- **Only the home group is ever pushed below zero.** Other leagues are drained to exactly
  what they had and no further — the hole belongs to the group that borrowed.
- **Richest first**, so the debt clears in the fewest groups touched and lands on the
  hoard the borrower actually moved the money to, rather than nibbling every league they
  ever said hello in. Ties break on `chat_id` so it is deterministic.

Each seizure is logged **in the group it came from** — that is where the size left, so
that is where `size_log` has to show it. Interest is charged first and against the home
group where possible (it is the home loan's cost), which preserves the invariant that
each group's rows sum to exactly the change that group's wallet saw. Deposits stay
`bank_log`-only, because the wallet never paid them.

Reaching another league is graded as `CREDIT_BANK_SEIZED`, folded into the same tier as a
seized deposit rather than given a fifth constant: in both cases the home wallet could
not cover what was borrowed against it, and the difference isn't one a player would feel.

Debt collection deliberately reaches into `bank_accounts` as well. The bank is safe from
*theft*; if it were safe from *debt* as well, then borrowing and immediately hiding the
proceeds in it would be a free money printer. The lender is made whole in every case, so
a default is still zero-sum.

One trap for anyone writing a test here: `accept_loan` pays the principal **into the home
wallet**, so a fixture that sets balances *before* the loan silently leaves the proceeds
sitting in the home group and the collector never has to look anywhere else. Set the
balances you want at settlement time *after* acceptance.

`LOAN_MAX_PRINCIPAL_RATIO` caps a loan at the borrower's current size. That bound is what
stops a single default from burying a player past any hope of recovery, and it is the
main lever if usury turns out to be too safe for lenders.

## The crypto market has a house, and it cannot mint

`/crypto` is one market for the whole bot — a coin costs the same in every group, like
the central bank. Only holdings are per `(user, chat)`, because size is. Prices move
every minute via `crypto_tick_job`, a `run_repeating` at `CRYPTO_TICK_SECONDS`.

**The one treasury is the counterparty on both legs, and that is the entire economic
design.** A buy moves size into it; a sell moves it back out.
Nothing is created and nothing is destroyed — the same shape as the spectator book's
house, except that here the house **cannot mint at all**:

> A sale the reserve cannot cover is **partially filled** (`db.crypto_sell` scales the
> units down to what the bank can actually pay) and the remainder of the position stays
> in the player's hands. A coin that has tripled is a claim on the bank, not a claim on
> the universe.

Liquidity is the whole bot's, not the trading group's — a market whose depth depended on
which group you happened to be in would be arbitrary.

This used to be the delicate part. When the vault was a pool of per-group accounts, both
legs had to be spread proportionally or neither: crediting one group's account while
debiting everyone's was farmable — a player in a group holding 0.2% of the pool could
round-trip at a flat price and quietly move the other groups' shares into their own, and
a group's own share was exactly what a heist reached. **With a single stored number there
are no shares left to move between, so that attack is structurally gone rather than
merely closed.** The regression test now asserts the only thing a flat round trip can do
is pay the bank its two fees, and that no wallet in any other group moves at all.

That one rule is what keeps this from being a money printer, and there are regression
tests asserting it: a 50× moonshot against a thin vault stays exactly zero-sum, the
treasury never goes negative, and a completely empty vault moves nothing at all and says
so rather than paying.

### Where the bank's daily income actually comes from

`CRYPTO_FEE_RATIO` is charged on **both** legs, so a round trip costs a trader ~6%
whatever the price does. It lands in the treasury as `treasury_in` and therefore raises
everyone's deposit rate: trading against your friends pays the savers. This is the
third income source, and unlike the other two it scales with how much fun people are
having rather than with how much they deposit.

Note the split in `bank_log`, which is not cosmetic: the **stake** is logged as
`crypto_in` and the **fee** as `treasury_in`. Only the latter counts in
`get_treasury_income`, because the stake may have to be handed straight back.

### Player demand moves the price, and pumping is closed by the pricing

The random walk is only half the price. `crypto_prices.net_units` is the market's net
long position across every player in every group; `crypto_display_price` shifts the mid
by `_crypto_impact(net_notional, CRYPTO_IMPACT_DEPTH, CRYPTO_IMPACT_CAP)`. Buying pushes
a coin up, selling pushes it down, and a coin nobody holds trades at its mid.

**`db._crypto_exec_price` is the anti-pump, and it is the one thing here not to
"simplify".** Price is a function of inventory alone, so a trade's cost is the integral
of that function along the path it walks. Charging the *average* — which for a linear
impact is just the price at the **midpoint** inventory — gives:

```
buy  n units at inventory q    -> pay      mid * n * (1 + imp(q + n/2))
sell n units at inventory q+n  -> receive  mid * n * (1 + imp(q + n/2))
```

Identical. **A player can never profit from the price move their own order caused**, at
any size: an immediate round trip returns exactly what it cost, and the two trading fees
are pure loss. Quoting the pre- or post-trade price instead breaks that equality and
hands a big wallet free size. There is a regression suite that runs pump-and-dumps from
100 up to 500,000 (including one that pins the impact cap) and asserts each loses
precisely its two fees, plus one that slices the pump into eight orders.

`CRYPTO_IMPACT_CAP` is **not** what stops the pump — the pricing is. The cap only keeps
the board off absurd numbers and bounds what the bank is on the hook for.

Two consequences worth knowing before retuning any of it:

- **Impact is measured in notional at `base_price`, not at the live mid.** Using the mid
  would feed the price back into its own impact — up begets up. The base keeps the curve
  fixed per coin.
- **Selling into someone else's pump is legitimate and stays zero-sum.** A player who was
  already holding when a whale buys does profit, and the whale pays for it. That is
  trading, not an exploit, and it cannot be closed without deleting impact entirely; the
  daily buy cap is what bounds it. There is a test asserting the whole episode balances
  across both players and the bank.

A partial fill has to be **solved, not divided**. Fewer units means less impact means a
higher price per unit, so `available / price` overshoots — and `_reserve_take` caps what
it hands over while the wallet is credited in full, which mints. Proceeds
are monotonically increasing in units inside the cap, so `crypto_sell` bisects for the
largest fill the bank can honour. This was a real bug, caught by the conservation test.

### The price walk

`crypto_next_price` is a **mean-reverting geometric** random walk: `pull` toward
`base_price` at `CRYPTO_MEAN_REVERT`, plus a lognormal shock scaled by the coin's own
volatility, clamped to `[base × CRYPTO_MIN_MULT, base × CRYPTO_MAX_MULT]`.

- **Geometric, not additive**, so a 2-size coin and a 500-size coin move by comparable
  *percentages*. An additive shock would leave the cheap coins flat and the expensive
  ones berserk.
- **Mean-reverting**, so one lucky coin can't wander off and become the only thing worth
  holding. Without it the market stops being a market.
- `crypto_set_prices` writes the whole board in **one** statement. This runs every minute
  forever; a per-coin loop would be ten Supabase round trips a minute for the life of the
  bot.

`db.crypto_seed` is called next to `init_db()` on every startup and **never resets a live
price** — it only inserts genuinely new symbols. A deploy mid-rally must not hand
everyone's position back at the base price, and there is a test for it.

### Crypto splits the ledger the same way loans do

`crypto_principal` (the stake going in, and the cost basis coming back out) is
**excluded** from `get_recent_net_by_user`; `crypto_pnl` and `crypto_fee` are **counted**.
Without that split, dumping a wallet into a coin before the nightly handicap reads the
ledger would look like a catastrophic loss and pay a growth bonus for it — the deposit
exploit wearing a different hat. This is why `crypto_holdings.avg_cost` exists: a sale is
not separable into "my money coming back" and "what I actually made" without a cost basis.

`CRYPTO_DAILY_BUY_RATIO` caps a day's buying against the wallet and counts **gross**
spend, so buy → sell → buy cannot refill the allowance. Same reasoning as the deposit
cap: without it the whole wallet leaves circulation and `/dozdi` stops being worth
typing. Portfolio value is deliberately **not** part of `users.size`, so it doesn't count
toward the leaderboard or the crown — until you sell, it isn't size.

## Fees are transfers, never sinks

Every fee in the game moves size into the one reserve via `db.treasury_add` (or, inside
a bank/transfer transaction, `db._reserve_credit`). None of them delete size. This
matters because the treasury is the *only* thing funding deposit interest: a fee that
deleted size would quietly lower everyone's yield instead of raising it.

Current fees: theft loot (`THEFT_FEE_RATIO`), challenge winnings — never the returned
stake (`CHALLENGE_FEE_RATIO`), deposits and withdrawals (`BANK_*_FEE_RATIO`), the
cross-group transfer (`XFER_FEE_RATIO`), nightly account maintenance
(`BANK_MAINTENANCE_FEE_RATIO`), loan origination (`BANK_LOAN_ORIGINATION_RATIO`), and
both legs of a crypto trade (`CRYPTO_FEE_RATIO`). The challenge settlement also sweeps the
`spread` — anything a shielding perk stops the winner collecting while the loser still
pays in full — which used to evaporate.

When adding a new fee, take it in the same transaction as the thing it is charging on,
and never charge it on money that is merely being returned from escrow.

## The shop is real supply and demand, and its limits are global

`/shop` used to sell every item at a fixed, inflation-scaled price with no cap. It now
behaves like an actual market: every item shares one global daily cap
(`SHOP_DAILY_LIMIT`, 5) and one global weekly cap (`SHOP_WEEKLY_LIMIT`, 40) across the
*whole group*, not per player — if one player buys 4 of the day's 5, only 1 is left for
everyone else combined, not 5 more each. `db.claim_shop_purchase` enforces this
atomically with a `SELECT ... FOR UPDATE` on a per-(chat, item) row, so two players
racing for the last unit can never both win it.

Price climbs with each sale toward the cap (`shop_scarcity_mult` in `bot.py`): the first
unit sold today/this week costs the plain inflation-scaled price, and the unit right
before a cap is hit costs up to `SHOP_SCARCITY_MAX_BONUS` (100%) more. The price shown
on the shop's button and the price actually charged always agree, because both are
computed from the *same* pre-purchase counts `claim_shop_purchase` returns — the caller
never re-reads the row a second time between quoting and charging.

Selling an item's stock all the way out is itself real evidence of scarcity, so it
nudges the group's `economy.inflation` up immediately via `db.bump_inflation`
(`SHOP_SOLDOUT_INFLATION_BUMP` for the day, a larger `SHOP_WEEKLY_SOLDOUT_INFLATION_BUMP`
for the week) — on top of, not instead of, whatever the nightly supply-driven
`tick_inflation` would have done anyway. A refused purchase (the cap was already hit
before this call) must never bump inflation again for the same sellout event; only the
purchase that actually crosses the cap does.

As before, every centimetre paid lands directly in the shared treasury via
`db.treasury_add` — the shop was already one of the treasury's few real sinks, and that
did not change.

Day/week are lazily reset the same way perks expire at Tehran midnight — `day`/`week`
key columns are compared against the current stamp on read, with no scheduled reset
job. If the claimed slot then fails to be paid for (insufficient funds, or the inventory
insert somehow throws), `db.release_shop_purchase` hands it back, mirroring the
escrow-claim/release pattern already used for `DOSE_LIMITED_ITEMS`.

## Cross-group transfer: a fee can't stop farming, so the source group is gated

`/enteghal` lets a player move their own size between groups they played in, minus a fee.
It was shut down once already: the 30% fee wasn't enough friction, and players were
leaving to build a size in a low-friction side group they controlled (no real PvP, no
theft, no consensus votes shrinking them) and importing most of it back, which let them
skip this group's economy rather than just discount it.

**Raising the fee cannot fix this, and it is worth understanding why before touching any
of it.** A farm group's cost of production is essentially zero — the farmer is
automatically king there, nobody steals from them, nobody challenges them, no `/ejma`
shrinks them — so it prints size at whatever rate they can be bothered to type `/d`. Any
fee below 100% therefore still leaves farming profitable, and every increase falls
hardest on the honest players who earned their size in a real league. Price was never the
lever.

So the gate is structural: **size may only leave a group that has proved it is a real
league.** `bot.check_xfer_source` judges the SOURCE group against
`db.get_xfer_source_stats`, and a farm fails several of the tests at once, deliberately —
beating one is not enough:

- **Age** (`XFER_MIN_SOURCE_AGE_DAYS`) — from the group's oldest `users.joined_at`.
- **Active players** (`XFER_MIN_SOURCE_PLAYERS`) — distinct players who grew inside
  `XFER_SOURCE_WINDOW_DAYS`.
- **Real competition** (`XFER_MIN_SOURCE_MATCHES` resolved `pvp_matches` among at least
  `XFER_MIN_SOURCE_MATCH_PLAYERS` distinct people) — five challenges between the same two
  alt accounts is not a league, which is why the distinct-participant count is separate
  from the match count.
- **Not owned outright** (`XFER_MAX_SOURCE_SHARE`) — the exporter's share of every
  centimetre in the group, **wallets plus bank deposits**. Counting only wallets would let
  someone park the hoard in the bank and look like a modest member of a group they own.
- **Tenure** (`XFER_MIN_TENURE_DAYS`) — so nobody parachutes into a real group to carry
  money out of it.

These thresholds live in `db.py`, not with the rest of the game balance in `bot.py`, for
one specific reason: `admin_panel.py` shows the owner the same numbers the bot enforces
and deliberately never imports `bot.py`. Two copies of a threshold is the drift bug this
codebase has been bitten by before — keep exactly one.

**The owner's verdict overrides the numbers in both directions.** `chats.xfer_policy` is
`'auto'` (judge on the stats), `'trusted'` (always allowed) or `'blocked'` (never),
settable per group from the panel's group page, which also displays the stats so the
decision isn't taken on faith. This exists because the automatic test is a heuristic:
someone patient enough, with enough alt accounts, could eventually dress a farm up to
pass it, and a farm spotted by eye should be killable in one click rather than in a
deploy.

`check_xfer_source` is re-checked in `transfer_callback` as well as `transfer_cmd`, for
the same reason `is_xfer_enabled` is: a button can be sitting in an old message from
before the owner blocked the group, or from before the group's own numbers fell below the
bar.

Rather than a code change, whether it's open at all and what it charges are now both
runtime state in `bot_meta` (`db.is_xfer_enabled()` / `db.get_xfer_fee_ratio()`,
default 40% when never configured), settable from the admin panel's home page. Both
`transfer_cmd` and `transfer_callback` re-check `is_xfer_enabled()` independently -
the callback re-checks separately because a button can still be sitting in an old
message from before the owner flipped it off. Cooldown (24h) and the minimum amount
(50) stay fixed constants in `bot.py`; only on/off and the fee are panel-controlled.

The underlying `get_user_groups`/`try_start_xfer`/`cross_group_transfer` functions in
`db.py`, and the `users.last_xfer_at` column and `xfer_principal`/`xfer_fee` `size_log`
source tags, are unconditional - the toggle only gates the two `bot.py` handlers.

The feature was reopened by a one-shot `init_db` migration guarded by the `bot_meta` key
`xfer_reopened_with_source_gate`, following the usual pattern. The guard is what matters:
`init_db()` runs on every startup, so an unguarded write would reopen transfer behind the
owner's back every time the bot restarted, no matter how many times they closed it from
the panel.

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

### `/etebar` shows the debt, and the debt is bot-wide

A lender reading only this group's loan book was seeing a fraction of the claim that
already outranks theirs. `/vam` is funded out of pooled deposits and `_collect` sweeps
**every league the borrower plays in**, so a borrower quietly carrying 5,000 of debt in
another group is a far worse risk than an empty local loan book makes them look.

`db.get_debt_exposure(user_id, home_chat_id)` returns the whole picture in one call:
debt here, debt away, how many other groups that is spread over, how much of it is owed
to the bank (the claim that outranks a player lender), the nearest due date, and
`assets` — wallets plus deposits everywhere, which is precisely what the collector can
reach. Negative wallets are floored at zero per group, the same rule `_group_weight`
uses: a group carrying a debtor is not thereby a liability somewhere else.

**It deliberately never returns which groups**, and there is a test asserting no chat id
reaches the message. The number is what a lending decision turns on; the list would
publish the borrower's group membership into a chat, which is the cross-group leak the
inline-mode rules are careful about. A count of groups carries the risk without the
identities.

Two deliberate splits between free and paid:

- **`/etebar` still costs `CREDIT_CHECK_FEE`** and still charges it before reading
  anything. The diligence is what the fee buys: the coverage ratio, the here/away split,
  the due dates and the repayment history.
- **A `/nozul` offer flags the debt TOTAL for free.** The offer already showed the credit
  score for nothing, and the live debt is the more important of the two — a lender about
  to hand over real size should not walk into "he already owes 5,000 elsewhere" merely
  because the breakdown is behind a fee. The offer prints the total and points at
  `/etebar` for the rest.

Only `status = 'active'` counts, so a repaid loan and one the panel quietly forgave both
stop existing here exactly as they do for `get_overdue_loans` and the per-player cap.

`BANK_LOAN_MIN_SCORE` gates `/vam` only. The official bank refuses bad credit outright;
loan sharks are unregulated and will lend to anyone, which is what `/etebar` is for — a
lender pays `CREDIT_CHECK_FEE` to price the risk themselves before making an offer. That
fee is a transfer to the treasury like every other fee, and unlike loan principal it
*does* count toward the nightly handicap, because it is a real cost.

## The economy is political, and that is the design

`economy` holds one row per group: an `inflation` price index, an `unrest` level, and
three multipliers the crown controls (`fee_mult`, `interest_mult`, `growth_mult`).

**Inflation is a real price level, not a stat.** Everything the game charges and
everything it pays out goes through `bot.priced()`, so shop items, lottery tickets, boss
rewards and the daily growth roll all scale with it. Flows keep pace; **stocks do not**.
A banked fortune buys less every day the index rises, while a debt — fixed in nominal
size — quietly shrinks. That single asymmetry is why savers and debtors want opposite
kings, and it is the whole reason the crown's choices matter to anyone but the king.

It moves two ways. `tick_inflation` runs nightly and chases the actual money supply
(**wallets + deposits — the group's own**, not the shared treasury), so a group that
prints finds its shop expensive without anyone deciding that. The treasury is
deliberately excluded now that it is one pot for the whole bot: counting it would count
the same size once per group and make every group's index move together for reasons that
have nothing to do with that group. Size paid into a sink genuinely has left this
league's circulation, and the index should say so. Decrees push the same number deliberately.

### The decrees are asymmetric on purpose

`decrees.py` holds 100 corrupt and 100 honest decrees, and the king is dealt three
of each every night — a fixed 3-and-3 rather than a mixed handful, so a night can never
happen to be all virtue or all corruption and the king can never blame the draw. **Every** corrupt one pays the
king (`mint`, `treasury_to_king`, or `levy`) and raises `unrest`; **every** honest one
costs him (`king_to_treasury`, `handout`, `relief`, or `burn_king`) and lowers it. There
are regression tests asserting exactly that, 100/100 in both directions — if you add a
decree that breaks the pattern, those tests fail, and they should.

The tension this creates is the game. The crown belongs to whoever is biggest, so every
honest decree pushes the king toward losing it, and every corrupt one raises unrest
toward a revolt that seizes 40% of his size and hands it to everyone else. Ruling well
is a slow way to lose power; ruling badly is a sudden one.

`mint` is the only thing in the entire codebase that deliberately creates size from
nothing. (The spectator book can mint too, but only as a last-resort fallback when the
treasury cannot cover a payout it already owes — see "The spectator book has a house".)
It is reserved for the worst decrees deliberately — that is what makes debasement
genuinely corrosive rather than merely unfair, and it is why the bank's "cannot mint"
rule is written the way it is.

### Forcing a revolt: `/enghelab <chat_id>`

The nightly tick only rolls for a revolt above `UNREST_REVOLT_THRESHOLD` and even then
only at a chance, which is right for the game and useless when the owner wants one to
happen. `/enghelab` is the override, and it is **owner-only in the bot's DM**
(`_owner_only`: `user.id == OWNER_ID` *and* `chat.id > 0`). It is deliberately absent
from `BOT_COMMANDS`, so it never appears in anyone's `/` menu, and in a group it returns
without replying at all — even for the owner. There is a suite asserting each of those
negatives before it asserts the command works.

Two details worth keeping:

- **It previews before it acts.** `revolt_preview` shares `REVOLT_SEIZE_RATIO` with
  `_revolt` rather than recomputing, so the figure the owner is shown is the figure
  taken — the shown-vs-charged drift this repo keeps getting bitten by. The confirm
  button carries a uuid claimed through `db.claim_challenge`, the same one-shot nonce
  table challenge buttons use: a DM stays scrollable forever, and claiming in the
  database rather than in memory is what makes single-use survive a restart.
- **The group message is identical either way.** Players cannot tell a forced revolt
  from a nightly one, which is the point — `_revolt` is still the single settlement
  path, it just returns its outcome now so the command can report a no-op instead of
  leaving the owner guessing.

`recover_decree_offer` is a startup catch-up, following the same pattern as the other
recovery sweeps. `run_daily` only fires at its appointed minute, so a bot deployed or
restarted past 21:30 would silently skip that night's decree entirely. The sweep posts
it on boot instead — gated on the hour having actually come round, and on tonight's
offer being genuinely missing, so it can never double-post or re-offer a signed day.

### Two things to be careful of here

`apply_decree` must `INSERT ... ON CONFLICT DO NOTHING` the economy row before it reads
and updates it. Without that, a group whose economy nobody has read yet silently absorbs
every decree's inflation and unrest into a row that does not exist — the decree appears
to work and changes nothing. That was a real bug, caught by test 9.

Only a decree that was actually offered tonight can be signed. `callback_data` is
client-supplied, so `decree_callback` checks the code against `pending_decrees` as well
as checking the signer is the king — otherwise a king could pick his favourite out of
all 200.

## Martial law: the crown's veto over mob rule

`/hokm` lets the sitting king dissolve the open `/ejma` **aimed at him** and put whoever
called it in the motley for `JESTER_HOURS`. It reaches that vote and no other: martial
law is self-defence, not patronage. Selecting the oldest open vote instead would let a
king spend his one declaration shielding a friend while the group's case against *him*
ran on, and with several votes open at once the one that got dissolved would come down
to whichever happened to be filed first. Rationed to once per three days **per group** — the slot lives
on `economy.last_martial_at`, not on the king, so abdicating and being re-crowned cannot
refresh it.

A cancelled vote is `status='cancelled'`, and deliberately does **not** call
`set_consensus_protection` the way `fail_open_consensus` does. A vote that was dissolved
by decree was never actually decided, so the group is free to open another one tomorrow:
the king bought himself a night, not immunity. Getting this backwards would make a
single `/hokm` worth three days of protection and turn the power from strong into
unanswerable.

The price is `MARTIAL_UNREST`, applied through `apply_decree` as a `bad` decree so it
lands in `decree_log` alongside everything else the crown does. That wires the power
straight into the revolt clock: a king who dissolves every vote against his friends is
buying each one with a slice of his own reign.

Being a jester is not cosmetic. It blocks starting `/ejma` **and** voting in one (the
player who reached for mob rule loses access to it), skims `JESTER_TRIBUTE_RATIO` off
each positive daily growth roll into the king's pocket, and shows as 🤡 on the
leaderboard. The tribute is only ever taken from a positive roll — a bad day is
punishment enough — and never when the jester is somehow the king himself.

## Credit gains are scaled; credit losses are not

The obvious exploit is to borrow the minimum, repay it immediately, and repeat. Three
independent brakes, because any one alone is dodgeable: the gain scales with the loan's
size **relative to the borrower** (`size_at_accept`, snapshotted before the principal
lands), a loan repaid inside `CREDIT_MIN_HOLD_RATIO` of its term earns nothing at all,
and `CREDIT_DAILY_GAIN_CAP` bounds a day's total. Penalties are deliberately unscaled:
credit should be slow to build and quick to lose, and a cheap practice default should
still hurt.

## Connections are pooled, and that is why the game feels fast

`get_connection()` used to open a brand-new connection per call. Against Supabase that
is a TCP handshake plus a TLS handshake every time, and measured against production it
cost **~0.8–1.0s per call** — on a database whose largest table is under 2 MB, so
essentially none of it was query time. It compounded everywhere at once, which is why
the bot and the website felt slow together:

| | db calls | old cost |
|---|---|---|
| Mini App home screen | ~12 | ~10s |
| `crypto_tick_job` | 2 | ~7s, every minute, blocking the event loop |
| any bot command | several | seconds |

The measurement that settled it: `/healthz` (0 db calls) answered in ~0.3–1.0s while
`/api/groups` (2 db calls) took 2.0–3.0s. The difference is per-call and constant, not
proportional to any query.

`db.py` now keeps a `ThreadedConnectionPool` per process, created lazily and keyed on the
PID (gunicorn forks its workers, and a pool created before the fork would hand one socket
to two processes). Two rules make it safe, and both have tests:

- **A broken connection is never handed back.** `OperationalError`/`InterfaceError` mean
  the socket is gone, so the connection is closed rather than returned; a plain SQL error
  (a constraint violation, say) rolls back and the connection is reused, because it is
  perfectly good. `_retry_transient` already re-runs every public function once on
  exactly those two errors, so a connection that died while idle in the pool costs one
  silent retry instead of a failed command.
- **Saturation degrades, it never fails.** `getconn()` *raises* `PoolError` the instant
  all `DB_POOL_MAX` are checked out — it does not queue — and `PoolError` is neither of
  the two errors `_retry_transient` catches, so a burst of concurrent handlers would have
  surfaced as the generic "temporary problem". `_acquire` waits `DB_POOL_WAIT_SECONDS`
  for one to come back and then opens a **private** connection it closes afterwards. The
  worst case is exactly the old behaviour, never an error.

Sizing is per process, so the total against Postgres is roughly `DB_POOL_MAX` × (bot +
panel workers + Mini App workers). Keep that product well under `max_connections`.

### The other half: never block the event loop

`psycopg2` is synchronous, so calling it straight from an async job freezes the whole
asyncio loop — no Telegram updates processed at all for the duration. Handlers get away
with it because each one delays only its own user, but `crypto_tick_job` runs on a timer
forever, for everyone: production showed it holding the loop for **seven seconds every
minute**, which was enough for apscheduler to log missed runs and for ordinary commands
to time out against `api.telegram.org`. Its database work goes through
`asyncio.to_thread` now, and there is a test that slows the tick artificially and
measures the longest gap a 10 ms heartbeat coroutine sees.

The Telegram-side timeouts in the `ApplicationBuilder` call are widened for the same
reason: this host's link to `api.telegram.org` is not fast, and the library's defaults
turned a slow request into `telegram.error.TimedOut`, which `on_error` shows as the
generic "temporary problem".

## The Mini App is a third service, and it imports `bot.py` on purpose

`python_bot/webapp.py` is the browser face of the game: a Flask/gunicorn service
(`dickbot-web`, 127.0.0.1:8012) at **https://app.inddex.app**, launched from `/app` in
Telegram. The front end is a **React + Vite + shadcn/ui** app under `python_bot/web/`.

### The build step, and the one thing that keeps it honest

This is the only part of the repo with a build, and it earns it: shadcn/ui is Radix plus
Tailwind plus real components, and approximating that by hand in one HTML file is how
you end up maintaining a worse copy of it.

**`web/dist` is committed.** That is the trade, and it is deliberate: the production host
needs no Node, and the deploy stays `git reset --hard` + `pip install` + restart, exactly
as it was before. Flask serves `dist/index.html` at `/` and `dist/assets/*` at
`/assets/*` (`send_from_directory`, so a crafted filename cannot escape the directory —
there is a test firing traversal at it).

The obvious failure of committing an artifact is shipping a stale one, so
`.github/workflows/web-build.yml` runs `npm ci && tsc --noEmit && npm run build` on every
push and **fails if the committed `dist` differs**. `test_webapp_build.py` asserts the
same thing locally. After changing anything in `web/src`:

```bash
cd python_bot/web && npm ci && npm run build   # then commit dist/
```

Filenames are stable across builds (`assets/app.js`, `assets/app.css`), so the asset
route sends `Cache-Control: no-cache` — hashed names would only make the Flask route
harder, but a long cache with stable names would serve yesterday's app after a deploy.

**recharts is lazy-loaded.** It is two thirds of the weight and only the trade sheet ever
needs it, so it is a separate chunk: the main bundle is ~84 KB gzipped and the five
screens that draw no chart never download the chart library. There is a test asserting
the split survives.

### Light and dark, decided before first paint

The tokens are shadcn's: light on bare `:root`, dark under an explicit `.dark` class, so
no colour has its only definition inside a media query and the toggle wins in both
directions. **The decision is made in `index.html`, before React runs** — an explicit
saved choice, else Telegram's `colorScheme`, else the OS. Doing it in React would paint
the light shell and then flip, which reads as a flash of the wrong colour on every single
launch. There is a test asserting the pre-paint script is still there.

### The market chart

`crypto_history` stores one point per coin per `CRYPTO_HISTORY_EVERY_TICKS` (5 minutes at
a one-minute tick), written by the same `_crypto_tick_sync` that moves the prices, in one
statement like `crypto_set_prices`. `GET /api/crypto/history` serves it, capped at 240
points because a phone cannot show more.

It records the **mid**, not the impact-shifted display price: impact is a function of
inventory, so folding it in would draw somebody's open position rather than the market.
The chart is a display artefact and nothing settles against it — `crypto_prune_history`
keeps a week.

**It imports `bot.py`, and the admin panel deliberately does not. That difference is the
point.** The panel is a tool that reaches *around* the game; this is the game, in a
browser. Every price, cap, fee and rate it shows has to be the number the bot itself
would charge, and two copies of a rule drift — the bug class this codebase keeps getting
bitten by. `bot.py` imports with no side effects (its whole runtime lives under
`if __name__ == '__main__'`), so importing it is the drift-safe choice and re-deriving
its constants inside `webapp.py` would be the dangerous one. There is a regression test
asserting the rate, the deposit cap, the shop price and the coin price the API returns
are *identical* to `bank_effective_rate` / `_bank_daily_cap` / `shop_item_price` /
`crypto_display_price`.

Every write endpoint calls the same `db.py` function the Telegram handler calls, with the
same constants. `api_shop_buy` in particular mirrors `buy_callback` step for step —
claim the slot, price off the counts the claim returned, charge, hand over, bump
inflation only on the purchase that actually crosses a cap. If that ordering changes in
one, change it in both.

### Auth is Telegram's signature, and nothing else

There is no password and no session store. Telegram hands a Mini App an `initData`
string carrying the user plus an HMAC-SHA256 taken with a key derived from the bot token;
`_verify_init_data` checks it with `compare_digest` and rejects anything older than
`INIT_DATA_MAX_AGE_SECONDS` — a valid signature over a stale payload is still stale, and
without that check a leaked `initData` would be a permanent credential.

**Two rules that must not be relaxed:**

- The user id comes *only* from the verified payload, never from the request body.
- `_scope()` checks the client-supplied `chat_id` against `db.get_user_groups(user_id)`
  before anything reads or writes. Without it, changing one number in a request would
  read — and trade against — any group in the bot. Every league is independent here for
  exactly the reason it is in the bot. There are tests asserting a player cannot read or
  write a group they are not in, that an unsigned caller gets 403, that a payload signed
  with a different token is refused, and that swapping the user id while keeping the
  signature is refused.

There are **two** auth schemes, and they are not interchangeable — confusing them
silently breaks one of them:

| | key | used by |
|---|---|---|
| `_verify_init_data` | `HMAC-SHA256(b"WebAppData", token)` | Mini App, opened from `/app` |
| `_verify_login_widget` | `SHA256(token)` | Login Widget, an ordinary browser |

**Both headers are Latin-1 or nothing.** HTTP header values may only contain Latin-1,
and the Login Widget's payload carries the player's Telegram display name *verbatim* —
which for this bot's players is Persian. `JSON.stringify` does not escape it (unlike
Python's `json.dumps`, worth knowing when writing a fixture for this), so the raw
payload in a header made the browser throw `String contains non ISO-8859-1 code point`
and refuse to send **any** request. The symptom is therefore the whole app dying, not a
failed login, and it never appeared inside Telegram because initData arrives
percent-encoded. `headerSafe()` in `app.html` base64-encodes anything that doesn't fit
behind a `b64:` marker and `_header_value()` decodes it; values that already fit are
passed through byte-for-byte, which matters because the widget's HMAC is taken over
exactly those bytes.

`_auth()` tries initData first and falls back to the widget, so nothing downstream can
tell which was used and none of it cares. Both are header-borne, both use
`compare_digest`, and both expire (`INIT_DATA_MAX_AGE_SECONDS` / `LOGIN_MAX_AGE_SECONDS`
— the widget's window is longer because it is what a browser keeps between visits, while
initData is reissued on every launch). There is a test asserting a widget payload is
*not* accepted as initData.

Only the Login Widget needs the domain registered with BotFather (`/setdomain`). A Mini
App opened from a `web_app` keyboard button works on any HTTPS URL with no registration —
worth knowing before debugging the wrong thing.

`X-Frame-Options` is deliberately **not** set: Telegram has to be able to frame a Mini
App. That is asserted too, so nobody "hardens" it into a blank screen.

### Bringing it up on a server

`deploy/setup-web.sh` does the whole thing and is idempotent: installs the unit, installs
the nginx vhost, gets the certificate, starts the service, and proves the chain by
fetching `/healthz` *through nginx* rather than trusting `systemctl is-active`.

The one subtlety worth keeping: nginx refuses to start when an `ssl_certificate` file is
missing, and certbot's HTTP-01 needs nginx already serving port 80 — a deadlock on a
first run. The script breaks it by installing only the first `server {}` block (the
HTTP/ACME half) until the cert exists, then swapping in the full vhost.

It deliberately does **not** do the Cloudflare DNS record (that needs an API token this
script has no business holding — it checks the name resolves and stops with instructions)
or BotFather `/setdomain` (there is no API for it).

### `/enteghal` is in the app, and every gate came with it

Transfer belongs here for the same reason the bank does: it only touches the player's own
state across their own groups, so there is nobody in a chat who needs to see it happen.

`api_transfer` mirrors `transfer_callback` **step for step, in the same order, with the
same functions** — `is_xfer_enabled`, the minimum, a `< 0` destination that isn't the
current group, membership re-checked against `get_user_groups` (the client supplies the
destination here exactly as `callback_data` does there), `check_xfer_source`, the wallet,
then `try_start_xfer`. Skipping any one of them would make the browser the soft way round
the farm-group gate the chat enforces, which is the one thing this feature cannot be.
There is a suite asserting each refusal individually, plus that a blocked source group and
the owner's global switch both still hold from the web.

Two details worth keeping:

- **`db.get_xfer_wait_remaining` exists because `try_start_xfer` is a claim, not a
  question.** The screen has to show the countdown before the player commits, and calling
  the claim to find out would consume the slot for anyone who merely opened the sheet.
- **The wallet is checked *before* the cooldown is claimed**, in both the callback and the
  endpoint. It used to be checked only inside `cross_group_transfer`, which meant a
  transfer refused for being bigger than your wallet still cost you 24 hours for a typo.

The UI is a sheet off the home screen rather than a seventh nav tab — six is already a lot
at phone width, and this is something you do occasionally rather than a screen you live on.

#### A transfer is announced in both groups, and that is not optional

Every other thing the app does touches only the player's own state. A transfer moves size
**out of a group other people are playing in**, so it cannot be a silent browser-only
action — the group that lost the size has to see it exactly as it would have seen
`/enteghal`. `_announce_transfer` in `webapp.py` therefore posts to the source group and
the destination group, copying `transfer_callback`'s wording rather than rewording it:
nobody should be able to tell from the message which surface was used.

Three implementation details, in order of how easy they are to get wrong:

- **`webapp.py` has no `context.bot`**, and `requirements.txt` has no HTTP client. `_tg_send`
  is a single form-encoded `POST` to `api.telegram.org` through stdlib `urllib.request`,
  returning a bool instead of raising — adding `requests` for one POST would be a new
  production dependency for nothing.
- **It runs after the transfer has committed, and off the request.** `_run_bg` hands it to
  a daemon thread, so a slow or dead `api.telegram.org` costs the announcement and nothing
  else: not the money (already moved), not the player's response, not the worker. The
  guard lives in `_guarded_call` rather than inline in `_run_bg` specifically so a test can
  drive the *shipped* guard inline instead of racing a thread.
- **The source message is a fresh message here and an edit in the chat.** `transfer_callback`
  edits the message the button was on; there is no such message from the browser, so the
  same text is sent as a new one. If that wording ever changes, change it in both — there
  is a regression test asserting the distinctive phrases appear in `transfer_callback`'s
  source as well as in what the endpoint sends.

### What is deliberately not in it

Challenges, theft, `/ejma`, heists, decrees and the crown's powers are absent by design,
not by omission. Their entire point is a message landing in the chat for other people to
react to, and a browser tab has nobody to post to. The home screen links back to the chat
for those instead. Item use follows the same line: theft items and the golden ticket can
be armed from the web because they only touch the player's own state, while anything
needing a target (`DIRECT_ITEMS`) is pushed back to the group.

`chats.title` is recorded opportunistically in `log_incoming` — every delivered message
carries the group name and that handler already sees all of them — purely so the group
picker can say a name instead of a chat id.

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

### Debt management is the one corner of the panel that must stay silent

The group page lists every active loan (`db.admin_list_active_loans`) with two actions:
`forgive_loan` and `adjust_loan`. Both are deliberately built to never touch Telegram —
no group announcement, no DM to the borrower or lender, nothing in `size_log` beyond
the loan row's own state. That silence is the whole point of the feature, not an
oversight: it exists for the owner to fix a dispute or a mistake without it looking like
gameplay to anyone involved.

`db.admin_forgive_loan` closes an active loan as `'forgiven'` — a status distinct from
`'repaid'`/`'defaulted'` — without collecting anything from the borrower, paying the
lender or treasury, or touching `credit_score`. That one status flip is also what makes
it invisible everywhere else: `get_overdue_loans` (the collection sweep), `get_user_loans`
(`/بدهی`), and `count_active_loans` (the per-player loan cap) all filter on
`status = 'active'`, so a forgiven loan silently stops existing for every one of them.

`db.admin_set_loan_due_amount` only overrides the number a loan will collect *later* —
it doesn't move size now, and it doesn't bypass `settle_loan`. When the loan does
eventually settle (on time, late, or forced), the normal flow still runs in full:
interest is still `due_amount - principal`, the lender/treasury still gets paid, and
`credit_score` still moves as usual — only the amount differs from what was originally
agreed. So raising or lowering a debt from the panel is silent at the moment you do it,
but not retroactively silent about the loan's eventual, very normal-looking outcome.

`credit_score` is in `EDITABLE_USER_FIELDS` (kind `'credit'`, clamped to
`[db.CREDIT_MIN, db.CREDIT_MAX]`) for the same reason — a direct, silent override of the
one stat that gates borrowing, going through the same generic per-player fields form as
`theft_luck`/`growth_mult`, with no code path that notifies anyone.

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

- **The spectator book pays a flat `BET_PAYOUT_MULT`, with the house as counterparty.**
  It used to be parimutuel — winners split only what the losers staked — which meant the
  normal case (everyone piling onto the same player) paid a correct guess nothing but
  their own stake back. Guessing right and winning nothing is not a bet, so the group is
  now the counterparty of last resort. See "The spectator book has a house" below for
  the funding order and what it does and doesn't cost.
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
else (tax, theft, betrayal, challenges, donations, inter-group war) only moves it between
players and must stay exactly zero-sum — note that war is the one case where "between
players" spans two groups, so it conserves globally rather than per group. When adding a feature, be explicit about which of the three
it is — the economy inflated badly once because every mechanic was a source.

The spectator book is the one mechanic that can be *either*, depending on how the round
goes: it pays winners out of the treasury (and mints only if the treasury is dry) and
banks the losers' stakes back into it. That is neutral by construction while the house is
solvent — see "The spectator book has a house".

The crypto market looks similar but is strictly tighter: it is **always** zero-sum,
because a payout the treasury cannot cover is partially filled rather than minted. It is
therefore a pure transfer mechanic plus a fee sink — see "The crypto market has a house,
and it cannot mint".
