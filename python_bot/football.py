import os
import math
import httpx

API_FOOTBALL_KEY = os.environ.get('API_FOOTBALL_KEY')
API_BASE_URL = 'https://v3.football.api-sports.io'

# Common Persian names for national teams/popular clubs mapped to the English
# name API-Football actually uses, so an admin can type "اسپانیا فرانسه" instead
# of "Spain France". Anything not in this table is searched as typed (works fine
# for English team names, may not match obscure Persian-only spellings).
TEAM_NAME_ALIASES = {
    'اسپانیا': 'Spain', 'فرانسه': 'France', 'آلمان': 'Germany', 'ایتالیا': 'Italy',
    'انگلیس': 'England', 'برزیل': 'Brazil', 'آرژانتین': 'Argentina', 'پرتغال': 'Portugal',
    'هلند': 'Netherlands', 'بلژیک': 'Belgium', 'کرواسی': 'Croatia', 'اروگوئه': 'Uruguay',
    'مراکش': 'Morocco', 'ژاپن': 'Japan', 'کره جنوبی': 'South Korea', 'آمریکا': 'USA',
    'ایران': 'Iran', 'عربستان': 'Saudi Arabia', 'قطر': 'Qatar', 'سوئیس': 'Switzerland',
    'دانمارک': 'Denmark', 'صربستان': 'Serbia', 'لهستان': 'Poland', 'سنگال': 'Senegal',
    'رئال مادرید': 'Real Madrid', 'بارسلونا': 'Barcelona', 'بارسا': 'Barcelona',
    'منچستر یونایتد': 'Manchester United', 'منچستر سیتی': 'Manchester City',
    'لیورپول': 'Liverpool', 'چلسی': 'Chelsea', 'آرسنال': 'Arsenal',
    'بایرن مونیخ': 'Bayern Munich', 'بایرن': 'Bayern Munich', 'دورتموند': 'Borussia Dortmund',
    'یوونتوس': 'Juventus', 'میلان': 'AC Milan', 'اینتر': 'Inter', 'پاری سن ژرمن': 'Paris Saint Germain',
    'پی اس جی': 'Paris Saint Germain',
}


def resolve_team_query(text):
    return TEAM_NAME_ALIASES.get(text.strip(), text.strip())


class FootballApiError(Exception):
    pass


def _require_key():
    if not API_FOOTBALL_KEY:
        raise FootballApiError(
            "API_FOOTBALL_KEY تنظیم نشده. برای فعال کردن بت فوتبالی، یک کلید رایگان از "
            "api-football.com بگیرید و آن را به عنوان متغیر محیطی API_FOOTBALL_KEY تنظیم کنید."
        )


async def _get(path, params):
    _require_key()
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f'{API_BASE_URL}{path}', headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def find_fixture(team1_query, team2_query, date_str):
    """Searches a specific day's fixtures for a match between two teams (fuzzy,
    case-insensitive substring match against API-Football's team names). Returns
    a dict {fixture_id, home_team, away_team, kickoff_iso} or None if not found."""
    data = await _get('/fixtures', {'date': date_str})
    q1 = resolve_team_query(team1_query).lower()
    q2 = resolve_team_query(team2_query).lower()
    for item in data.get('response', []):
        home = item['teams']['home']['name']
        away = item['teams']['away']['name']
        names = (home.lower(), away.lower())
        if (q1 in names[0] or q1 in names[1]) and (q2 in names[0] or q2 in names[1]):
            return {
                'fixture_id': item['fixture']['id'],
                'home_team': home,
                'away_team': away,
                'kickoff_iso': item['fixture']['date'],
            }
    return None


# API-Football short status codes that mean "match is currently live".
LIVE_STATUSES = {'1H', 'HT', '2H', 'ET', 'BT', 'P', 'LIVE'}
HALFTIME_STATUSES = {'HT'}
FINISHED_STATUSES = {'FT', 'AET', 'PEN'}


async def get_fixture_status(fixture_id):
    """Returns {status, elapsed, home_score, away_score, home_team, away_team} for
    a single fixture, or None if the API returned nothing for that ID."""
    data = await _get('/fixtures', {'id': fixture_id})
    response = data.get('response', [])
    if not response:
        return None
    item = response[0]
    return {
        'status': item['fixture']['status']['short'],
        'elapsed': item['fixture']['status'].get('elapsed') or 0,
        'home_score': item['goals']['home'] if item['goals']['home'] is not None else 0,
        'away_score': item['goals']['away'] if item['goals']['away'] is not None else 0,
        'home_team': item['teams']['home']['name'],
        'away_team': item['teams']['away']['name'],
    }


MIN_ODDS = 1.03
MAX_ODDS = 50.0
MATCH_LENGTH_MINUTES = 90


async def get_prematch_home_probability(fixture_id):
    """Returns the home team's implied pre-match win probability (0-1), derived from
    API-Football's own bookmaker odds for the 'Match Winner' market, normalized over
    just Home/Away (the Draw price is dropped since our market is 2-way only). Returns
    None if odds aren't available for this fixture (unsupported plan, no bookmaker has
    posted a line yet, etc.) - callers should fall back to a neutral 50/50 prior."""
    try:
        data = await _get('/odds', {'fixture': fixture_id})
    except Exception:
        return None
    response = data.get('response', [])
    if not response:
        return None
    for bookmaker in response[0].get('bookmakers', []):
        for bet in bookmaker.get('bets', []):
            if bet.get('name') == 'Match Winner':
                values = {v['value']: v['odd'] for v in bet.get('values', [])}
                if 'Home' in values and 'Away' in values:
                    try:
                        p_home_raw = 1 / float(values['Home'])
                        p_away_raw = 1 / float(values['Away'])
                    except (ValueError, ZeroDivisionError):
                        continue
                    return p_home_raw / (p_home_raw + p_away_raw)
    return None


def compute_odds(elapsed_minutes, home_score, away_score, prior_home_prob=0.5):
    """Live 'smart' odds for a 2-way (home win / away win) market, Polymarket-style.
    prior_home_prob seeds the match's opening price (from real bookmaker odds when
    available, 0.5 otherwise) - its influence fades out linearly as the match
    progresses, while the current goal difference matters more and more, so by full
    time the actual score fully determines the price regardless of pre-match
    expectations. The same lead matters more with less time left to overturn it, so
    a team's odds keep tightening the closer we get to the final whistle - never a
    fixed, static price like a traditional pre-match line."""
    time_factor = min(max(elapsed_minutes, 0) / MATCH_LENGTH_MINUTES, 1.0)
    goal_diff = home_score - away_score
    prior_home_prob = min(max(prior_home_prob, 0.01), 0.99)
    prior_edge = math.log(prior_home_prob / (1 - prior_home_prob))
    # Amplify how much a goal difference matters as time runs out: x0.6 early on,
    # up to x3.0 by full time, so a 1-goal lead late in the match prices in as
    # much more decisive than the same lead at kickoff.
    goal_edge = goal_diff * (0.6 + 2.4 * time_factor)
    edge = prior_edge * (1 - time_factor) + goal_edge
    p_home = 1 / (1 + math.exp(-edge))
    p_away = 1 - p_home
    odds_home = min(max(1 / p_home, MIN_ODDS), MAX_ODDS)
    odds_away = min(max(1 / p_away, MIN_ODDS), MAX_ODDS)
    return round(odds_home, 2), round(odds_away, 2)
