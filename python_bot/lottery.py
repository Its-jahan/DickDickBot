"""The lottery draw itself, shared by the bot's midnight job and the admin panel.

This lives outside bot.py so both callers run the *same* draw rather than each keeping
their own copy — two implementations of "pick the winner and pay the pot" would drift,
and the one that drifted would be paying real players the wrong amount.

It imports db and nothing else: no telegram, no flask. Callers do their own announcing.
"""
import random

import db

TICKET_PRICE = 10
# Held back from the prize rather than paid out, so the lottery is a size sink and not
# just a shuffle. The rake is swept into the group's bank treasury, where it funds
# deposit interest instead of being deleted outright.
BURN_RATIO = 0.10

# SystemRandom for the same reason the dice use it: the draw decides real payouts, so
# it should not come from a seedable PRNG.
_rng = random.SystemRandom()


def draw(chat_id, draw_date):
    """Runs one group's draw for one date and pays the winner.

    db.claim_lottery_draw deletes the day's tickets as it reads them, in a single
    statement, so this can never pay a second winner from the same pot no matter how
    many callers race it — the midnight job, the startup recovery sweep and the panel
    button all funnel through here.

    Returns a dict describing what happened, or None if there was nothing to draw."""
    entries = db.claim_lottery_draw(chat_id, draw_date)
    if not entries:
        return None

    pool = []
    for uid, fname, tickets, _paid in entries:
        pool.extend([(uid, fname)] * tickets)
    # The pot is what was actually paid in, not the entry count: bonus entries (a
    # perk, a golden ticket) buy odds, never prize money that nobody funded.
    pot = sum(paid for _, _, _, paid in entries)
    prize = int(pot * (1 - BURN_RATIO))
    if not pool or prize <= 0:
        return None

    winner_id, winner_name = _rng.choice(pool)
    db.update_size(winner_id, chat_id, prize, note=f"لاتاری {draw_date}")
    rake = pot - prize
    if rake > 0:
        db.treasury_add(chat_id, rake, note=f"کارمزد لاتاری {draw_date}")
    winner_tickets = sum(t for uid, _, t, _p in entries if uid == winner_id)

    return {
        "chat_id": chat_id,
        "draw_date": draw_date,
        "winner_id": winner_id,
        "winner_name": winner_name,
        "prize": prize,
        "pot": pot,
        "total_tickets": len(pool),
        "winner_tickets": winner_tickets,
        "odds": winner_tickets / len(pool) * 100,
        "entries": entries,
    }


def render_result(result, manual=False):
    """The message posted to the group. `manual` is stated plainly rather than hidden:
    a draw run early from the panel happened at a different time than players expect,
    and the message should say so instead of implying it fired at midnight."""
    header = "🎟️ قرعه‌کشی لاتاری!" if not manual else "🎟️ قرعه‌کشی لاتاری (زودتر از موعد انجام شد)"
    return (f"{header}\n\n"
            f"🎉 برنده: {result['winner_name']}\n"
            f"💰 جایزه: {result['prize']} سانتی‌متر\n"
            f"🎲 شانسش: {result['odds']:.0f}٪ از {result['total_tickets']} بلیت")


def pending_pot(chat_id, draw_date):
    """(total_tickets, prize_if_drawn_now) for a group's open pot — what the panel shows
    next to the draw button so it isn't a blind action."""
    entries = db.get_lottery_entries(chat_id, draw_date)
    tickets = sum(t for _, _, t, _p in entries)
    pot = sum(p for _, _, _, p in entries)
    return tickets, int(pot * (1 - BURN_RATIO)), entries
