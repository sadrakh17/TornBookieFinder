"""
Torn City Bookie Analyzer Bot — v4
====================================
Claude is the core analyst. Every recommendation is structured JSON logged to
SQLite. A background scanner watches ALL active bookie sports in real-time and
pushes Telegram alerts the moment a high-value opportunity is detected.

New in v4:
  - /watchstart  — start real-time background scanner across ALL sports
  - /watchstop   — stop scanner
  - /watchstatus — see scanner state, interval, events alerted
  - Scanner deduplicates by event_id so you never get the same alert twice
  - Quiet digest sent each cycle when no alerts fire (so you know it's alive)

All commands:
  /start         — Welcome
  /scan          — Browse active sports (manual)
  /recommend     — Claude's top picks right now (manual, auto-logged)
  /analyze       — Analyze a specific sport (manual)
  /resolve       — Fetch scores, resolve pending bets
  /report [N]    — Win rate report for last N days (default 7)
  /history [N]   — Last N logged bets with outcomes
  /watchstart [sec] — Start real-time scanner (default interval from env)
  /watchstop     — Stop scanner
  /watchstatus   — Scanner state
  /info          — Methodology
  /help          — All commands

Env vars:
  TELEGRAM_TOKEN    — from @BotFather
  ANTHROPIC_API_KEY — from console.anthropic.com
  ODDS_API_KEY      — from the-odds-api.com (500 req/month free)
  CHAT_ID           — your Telegram user/chat ID for scanner push alerts
  SCAN_INTERVAL     — scanner cycle in seconds (default 1800 = 30 min)
  ALERT_MIN_CONF    — minimum confidence to alert: HIGH | MEDIUM | LOW (default HIGH)
  WEBHOOK_URL       — Railway public URL (blank = polling mode for local dev)
  PORT              — injected by Railway automatically
  DB_PATH           — SQLite file path (default bets.db)
"""

import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import requests
from aiohttp import web
from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =============================================================================
# BOOTSTRAP
# =============================================================================

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ODDS_API_KEY      = os.getenv("ODDS_API_KEY")
PORT              = int(os.getenv("PORT", "8080"))
WEBHOOK_URL       = os.getenv("WEBHOOK_URL", "").rstrip("/")
DB_PATH           = os.getenv("DB_PATH", "bets.db")
CHAT_ID           = os.getenv("CHAT_ID", "")
SCAN_INTERVAL     = int(os.getenv("SCAN_INTERVAL", "1800"))   # 30 min default
ALERT_MIN_CONF    = os.getenv("ALERT_MIN_CONF", "HIGH")       # HIGH | MEDIUM | LOW

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Scanner runtime state (in-memory; resets on redeploy)
_scanner_task:    asyncio.Task | None = None
_scanner_running: bool = False
_alerted_ids:     set  = set()    # event_ids pushed this session

CONF_RANK  = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
TIER_EMOJI = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}
STATUS_EMOJI = {"WON": "✅", "LOST": "❌", "VOID": "⚪", "PENDING": "⏳"}

# Sports that map to Torn City Bookie offerings
TORN_SPORTS = {
    "americanfootball_nfl":      "NFL (American Football)",
    "baseball_mlb":              "MLB (Baseball)",
    "basketball_nba":            "NBA (Basketball)",
    "icehockey_nhl":             "NHL (Ice Hockey)",
    "soccer_epl":                "EPL (Football)",
    "soccer_uefa_champs_league": "UEFA Champions League",
    "tennis_atp_french_open":    "Tennis ATP",
    "rugbyleague_nrl":           "Rugby League NRL",
    "cricket_test_match":        "Cricket Test",
    "esports_csgo":              "CS2 (Esports)",
    "esports_lol":               "League of Legends",
    "esports_dota2":             "Dota 2",
}

PRIORITY_SPORTS = [
    "soccer_epl",
    "basketball_nba",
    "americanfootball_nfl",
    "icehockey_nhl",
    "baseball_mlb",
    "soccer_uefa_champs_league",
]

SCAN_KEYWORDS = [
    "nfl", "nba", "nhl", "mlb", "premier", "tennis", "rugby",
    "cricket", "dota", "csgo", "league of legends", "esport",
    "darts", "boxing", "mma", "golf",
]


# =============================================================================
# UTILITIES
# =============================================================================

def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _meets_threshold(confidence: str) -> bool:
    """True if this confidence tier meets or exceeds ALERT_MIN_CONF."""
    return CONF_RANK.get(confidence, 0) >= CONF_RANK.get(ALERT_MIN_CONF, 3)


def _chunks(text: str, size: int = 4000) -> list:
    """Split text into Telegram-safe chunks."""
    return [text[i:i+size] for i in range(0, len(text), size)]


# =============================================================================
# DATABASE
# =============================================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id       TEXT,
                sport_key      TEXT NOT NULL,
                sport_name     TEXT NOT NULL,
                match          TEXT NOT NULL,
                home_team      TEXT,
                away_team      TEXT,
                pick           TEXT NOT NULL,
                decimal_odds   REAL NOT NULL,
                implied_prob   REAL NOT NULL,
                confidence     TEXT NOT NULL,
                rationale      TEXT NOT NULL,
                commence_time  TEXT,
                recommended_at TEXT NOT NULL,
                status         TEXT DEFAULT 'PENDING',
                resolved_at    TEXT,
                actual_winner  TEXT
            );

            CREATE TABLE IF NOT EXISTS reports (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                period     TEXT NOT NULL,
                created_at TEXT NOT NULL,
                summary    TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_status ON recommendations(status);
            CREATE INDEX IF NOT EXISTS idx_rec_at ON recommendations(recommended_at);
            CREATE INDEX IF NOT EXISTS idx_sport  ON recommendations(sport_key);
        """)
    logger.info(f"DB ready: {DB_PATH}")


def db_save_picks(picks: list) -> int:
    saved = 0
    with db_connect() as conn:
        for p in picks:
            conn.execute(
                """INSERT INTO recommendations
                   (event_id, sport_key, sport_name, match, home_team, away_team,
                    pick, decimal_odds, implied_prob, confidence, rationale,
                    commence_time, recommended_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.get("event_id", ""),
                    p.get("sport_key", ""),
                    p.get("sport_name", ""),
                    p.get("match", ""),
                    p.get("home_team", ""),
                    p.get("away_team", ""),
                    p.get("pick", ""),
                    float(p.get("decimal_odds", 0)),
                    float(p.get("implied_prob", 0)),
                    p.get("confidence", "LOW"),
                    p.get("rationale", ""),
                    p.get("commence_time", ""),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            saved += 1
    return saved


def db_get_pending() -> list:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM recommendations WHERE status='PENDING' "
            "ORDER BY recommended_at DESC"
        ).fetchall()


def db_resolve_bet(rec_id: int, status: str, actual_winner: str):
    with db_connect() as conn:
        conn.execute(
            "UPDATE recommendations SET status=?, resolved_at=?, actual_winner=? WHERE id=?",
            (status, datetime.now(timezone.utc).isoformat(), actual_winner, rec_id),
        )


def db_get_period(days: int) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM recommendations WHERE recommended_at >= ? "
            "ORDER BY recommended_at DESC",
            (since,),
        ).fetchall()


def db_get_recent(limit: int = 20) -> list:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM recommendations ORDER BY recommended_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def db_save_report(period: str, summary: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO reports (period, created_at, summary) VALUES (?,?,?)",
            (period, datetime.now(timezone.utc).isoformat(), summary),
        )


# =============================================================================
# ODDS API
# =============================================================================

def fetch_sports() -> list:
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": ODDS_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"fetch_sports: {e}")
        return []


def fetch_odds(sport_key: str) -> list:
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "regions":    "us,uk,eu",
                "markets":    "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"fetch_odds({sport_key}): {e}")
        return []


def fetch_scores(sport_key: str, days_from: int = 7) -> list:
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/scores",
            params={"apiKey": ODDS_API_KEY, "daysFrom": days_from},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"fetch_scores({sport_key}): {e}")
        return []


def preprocess_events(events: list, limit: int = 50) -> list:
    """Condense raw Odds API events into compact dicts for Claude."""
    out = []
    for ev in events[:limit]:
        home      = ev.get("home_team", "?")
        away      = ev.get("away_team", "?")
        event_id  = ev.get("id", "")
        commence  = ev.get("commence_time", "")
        bookmakers = ev.get("bookmakers", [])
        if not bookmakers:
            continue

        outcomes: dict[str, list] = {}
        for bk in bookmakers[:8]:
            for mkt in bk.get("markets", []):
                if mkt["key"] == "h2h":
                    for oc in mkt["outcomes"]:
                        outcomes.setdefault(oc["name"], []).append(oc["price"])

        if not outcomes:
            continue

        odds_summary = {}
        for team, prices in outcomes.items():
            avg = round(sum(prices) / len(prices), 3)
            odds_summary[team] = {
                "avg_decimal":      avg,
                "best_decimal":     round(max(prices), 3),
                "implied_prob_pct": round(100 / avg, 2) if avg > 1 else 0,
                "bookmaker_count":  len(prices),
            }

        total_impl = sum(v["implied_prob_pct"] for v in odds_summary.values())
        out.append({
            "event_id":  event_id,
            "match":     f"{home} vs {away}",
            "home_team": home,
            "away_team": away,
            "commence":  commence,
            "vig_pct":   round(total_impl - 100, 2),
            "odds":      odds_summary,
        })
    return out


def discover_torn_sports() -> list[tuple[str, str]]:
    """
    Dynamically fetch ALL active sports from the Odds API and filter to
    those matching Torn City Bookie's offerings. Returns [(key, title), ...].
    """
    raw = fetch_sports()
    found, seen = [], set()
    for s in raw:
        if not s.get("active"):
            continue
        key     = s.get("key", "")
        title   = s.get("title", key)
        title_l = title.lower()
        if key in seen:
            continue
        if key in TORN_SPORTS or any(kw in title_l for kw in SCAN_KEYWORDS):
            seen.add(key)
            found.append((key, title))
    return found


# =============================================================================
# CLAUDE CORE
# =============================================================================

ANALYST_SYSTEM = """You are Claude — the core analyst of the Torn City Bookie Analyzer bot.

Your task: analyze real-time sports odds and identify VALUE BETS for Torn City's Bookie.

FRAMEWORK:
- Implied probability = 100 / decimal_odds
- Vig (overround) = sum of all implied probs - 100 (lower = fairer market)
- Value bet = bookmaker's implied prob < true win probability
- Sharp signal = high bookmaker_count consensus + clear odds gap between sides
- Avoid: implied probs within 3% of each other (coin-flip), vig > 10%

CONFIDENCE (apply strictly):
- HIGH:   bookmaker_count >= 5, vig < 5%, implied_prob > 60%, odds >= 1.40
- MEDIUM: bookmaker_count >= 3, vig 5-8%, implied_prob 45-60%
- LOW:    thin coverage, vig > 8%, very close matchup

RULES:
- Max 5 picks per call
- Never pick implied_prob_pct < 30%
- If no clear value, skip and explain in skipped_matches

CRITICAL: Respond ONLY with valid JSON. No markdown fences. No text outside JSON.

Required schema:
{
  "analysis_summary": "2-3 sentence market overview",
  "picks": [
    {
      "event_id":     "<from input>",
      "sport_key":    "<from input>",
      "sport_name":   "<from input>",
      "match":        "<Team A vs Team B>",
      "home_team":    "<home team>",
      "away_team":    "<away team>",
      "pick":         "<exact team name>",
      "decimal_odds": <number>,
      "implied_prob": <number e.g. 54.3>,
      "confidence":   "HIGH" | "MEDIUM" | "LOW",
      "rationale":    "<1-2 sentences>",
      "commence_time":"<ISO8601 or empty>"
    }
  ],
  "skipped_matches": ["<match>: <reason>"],
  "disclaimer": "<one-line risk disclaimer>"
}"""

REPORT_SYSTEM = """You are Claude — writing a quantitative performance report for a sports betting bot.

Input: structured stats + sample resolved bets.
Be honest. Do not sugarcoat poor results.

Use Telegram Markdown (*bold* _italic_). Follow this exact structure:

1. *Period & Sample Size*
2. *Overall Win Rate* (resolved only, VOIDs excluded)
3. *ROI Estimate* (flat 1-unit stakes: sum of odds-1 for wins, minus count of losses)
4. *By Confidence Tier* — HIGH / MEDIUM / LOW win rates
5. *By Sport* — best and worst performing sports
6. *Best Pick* — highest-odds WON bet
7. *Worst Miss* — highest-odds LOST bet
8. *Calibration* — do HIGH picks outperform LOW? Is the model well-calibrated?
9. *Verdict* — direct answer: should the user keep following this bot?"""


def claude_analyze(sport_key: str, sport_name: str, events: list) -> dict:
    """Core Claude analysis call. Returns parsed dict with picks."""
    if not events:
        return {
            "picks": [],
            "analysis_summary": "No events provided.",
            "skipped_matches": [],
            "disclaimer": "",
        }

    prompt = (
        f"Analyze these {sport_name} events (sport_key: {sport_key}).\n\n"
        f"{json.dumps(events, indent=2)}\n\n"
        "Return ONLY the JSON schema from your system prompt. No other text."
    )
    raw = ""
    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=ANALYST_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip accidental markdown fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        result = json.loads(raw)
        for pick in result.get("picks", []):
            pick.setdefault("sport_key", sport_key)
            pick.setdefault("sport_name", sport_name)
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Claude JSON fail: {e} | raw[:200]={raw[:200]}")
        return {"error": str(e), "picks": [], "raw": raw[:300]}
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return {"error": str(e), "picks": []}


def claude_report(rows: list, period_label: str) -> str:
    """Generate a performance report via Claude."""
    resolved = [r for r in rows if r["status"] in ("WON", "LOST")]
    wins     = [r for r in resolved if r["status"] == "WON"]
    losses   = [r for r in resolved if r["status"] == "LOST"]
    voided   = [r for r in rows if r["status"] == "VOID"]
    pending  = [r for r in rows if r["status"] == "PENDING"]

    roi = round(
        sum(r["decimal_odds"] - 1 for r in wins) - len(losses), 3
    ) if resolved else None

    by_conf = {}
    for tier in ("HIGH", "MEDIUM", "LOW"):
        tr  = [r for r in resolved if r["confidence"] == tier]
        tw  = [r for r in tr if r["status"] == "WON"]
        by_conf[tier] = {
            "resolved": len(tr),
            "wins":     len(tw),
            "win_rate": round(len(tw) / len(tr) * 100, 1) if tr else None,
        }

    by_sport: dict = {}
    for r in resolved:
        sn = r["sport_name"]
        by_sport.setdefault(sn, {"resolved": 0, "wins": 0})
        by_sport[sn]["resolved"] += 1
        if r["status"] == "WON":
            by_sport[sn]["wins"] += 1

    stats = {
        "period":        period_label,
        "total_logged":  len(rows),
        "resolved":      len(resolved),
        "wins":          len(wins),
        "losses":        len(losses),
        "voided":        len(voided),
        "pending":       len(pending),
        "win_rate_pct":  round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        "roi_units":     roi,
        "by_confidence": by_conf,
        "by_sport":      by_sport,
    }

    sample = [
        {
            "match":         r["match"],
            "pick":          r["pick"],
            "odds":          r["decimal_odds"],
            "confidence":    r["confidence"],
            "status":        r["status"],
            "actual_winner": r["actual_winner"],
            "sport":         r["sport_name"],
            "date":          r["recommended_at"][:10],
        }
        for r in sorted(resolved, key=lambda x: x["decimal_odds"], reverse=True)[:10]
    ]

    prompt = (
        f"Write the performance report.\n\n"
        f"STATS:\n{json.dumps(stats, indent=2)}\n\n"
        f"TOP RESOLVED BETS:\n{json.dumps(sample, indent=2)}"
    )
    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2500,
            system=REPORT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude report error: {e}")
        return f"Report generation failed: {e}"


# =============================================================================
# BET RESOLUTION
# =============================================================================

def resolve_pending_bets() -> dict:
    """Fetch scores for all sports with PENDING bets and resolve them."""
    pending = db_get_pending()
    if not pending:
        return {"resolved": 0, "still_pending": 0}

    by_sport: dict[str, list] = {}
    for row in pending:
        by_sport.setdefault(row["sport_key"], []).append(row)

    resolved_count = 0
    still_pending  = 0

    for sport_key, sport_rows in by_sport.items():
        scores    = fetch_scores(sport_key, days_from=7)
        completed = {g["id"]: g for g in scores if g.get("completed")}

        for row in sport_rows:
            game = completed.get(row["event_id"])

            # Fallback: match by team names if event_id missing
            if not game and not row["event_id"]:
                for g in scores:
                    if not g.get("completed"):
                        continue
                    g_teams = {g.get("home_team", ""), g.get("away_team", "")}
                    r_teams = {row["home_team"] or "", row["away_team"] or ""}
                    if r_teams and r_teams <= g_teams:
                        game = g
                        break

            if not game:
                still_pending += 1
                continue

            score_data = game.get("scores") or []
            if not score_data:
                db_resolve_bet(row["id"], "VOID", "No score data")
                resolved_count += 1
                continue

            score_map: dict[str, float] = {}
            for s in score_data:
                try:
                    score_map[s["name"]] = float(s["score"])
                except (KeyError, ValueError):
                    pass

            if len(score_map) < 2:
                db_resolve_bet(row["id"], "VOID", "Incomplete scores")
                resolved_count += 1
                continue

            ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
            if ranked[0][1] == ranked[1][1]:
                actual_winner = "DRAW"
            else:
                actual_winner = ranked[0][0]

            outcome = "WON" if row["pick"] == actual_winner else "LOST"
            db_resolve_bet(row["id"], outcome, actual_winner)
            resolved_count += 1

    return {"resolved": resolved_count, "still_pending": still_pending}


# =============================================================================
# TELEGRAM HANDLERS — MANUAL COMMANDS
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "\n".join([
        "🎰 *Torn City Bookie Analyzer — v4*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Claude AI is the core analyst\\. Every pick is logged, resolved against",
        "real match results, and tracked over time\\.",
        "",
        "*Manual commands:*",
        "🔍 /scan — Browse active sports",
        "🏆 /recommend — Claude's top picks now",
        "📊 /analyze — Analyze a specific sport",
        "✅ /resolve — Resolve pending bets vs real scores",
        "📈 /report — Performance report with real win rate",
        "📋 /history — Recent logged picks with outcomes",
        "",
        "*Real\\-time scanner:*",
        "📡 /watchstart — Start scanning ALL sports automatically",
        "🔴 /watchstop — Stop scanner",
        "📊 /watchstatus — Scanner state",
        "",
        "ℹ️ /info \\| ❓ /help",
        "",
        "⚠️ _Value betting is a long\\-run strategy\\. No guarantees per event\\._",
    ])
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "\n".join([
        "📋 *All Commands*",
        "",
        "/start — Welcome",
        "/scan — Browse active sports",
        "/recommend — Claude top picks now \\(auto\\-logged\\)",
        "/analyze `sport_key` — Analyze one sport",
        "/resolve — Fetch results, mark WON/LOST/VOID",
        "/report `days` — Win rate report \\(default: 7\\)",
        "/history `n` — Last N picks \\(default: 10\\)",
        "",
        "/watchstart `sec` — Start real\\-time scanner",
        "/watchstop — Stop scanner",
        "/watchstatus — Scanner state panel",
        "",
        "/info — Methodology",
        "/help — This message",
        "",
        "*Sport keys:*",
        "`soccer_epl` `basketball_nba` `americanfootball_nfl`",
        "`icehockey_nhl` `baseball_mlb` `soccer_uefa_champs_league`",
        "`esports_csgo` `esports_lol` `tennis_atp_french_open`",
    ])
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "\n".join([
        "📖 *How This Bot Works*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "*AI Core:* Claude \\(Anthropic\\) — structured JSON analysis",
        "*Odds Data:* The Odds API — 40\\+ bookmakers, live pricing",
        "*Logging:* Every pick saved with event ID, odds, confidence tier",
        "*Resolution:* /resolve fetches real scores → WON/LOST/VOID",
        "*Reporting:* /report = real win\\-rate analysis by Claude",
        "*Scanner:* /watchstart scans ALL bookie sports continuously",
        "",
        "*Value Bet Logic:*",
        "Implied prob = 100 ÷ decimal odds",
        "Value = bookmaker underprices true win probability",
        "Vig = overround \\(lower = fairer\\)",
        "",
        "*Confidence Tiers:*",
        "🟢 HIGH — Consensus, vig < 5%, clear favorite",
        "🟡 MEDIUM — Moderate signal",
        "🔴 LOW — Volatile or thin coverage",
        "",
        "_Win rate computed from real match results\\._",
    ])
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Scanning active sports markets...")
    sports = discover_torn_sports()
    if not sports:
        await msg.edit_text(
            "❌ No active Torn\\-compatible sports right now\\.",
            parse_mode="MarkdownV2",
        )
        return

    keyboard = [
        [InlineKeyboardButton(
            f"📊 {title}",
            callback_data=f"analyze:{key}:{title[:28]}"
        )]
        for key, title in sports[:12]
    ]
    keyboard.append([InlineKeyboardButton(
        "🏆 Top Recommendations Now", callback_data="recommend:all"
    )])

    await msg.edit_text(
        f"✅ *{len(sports)} active Torn\\-compatible sports found\\.* Tap to analyze:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2",
    )


async def cmd_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "🧠 *Claude scanning priority markets\\.\\.\\.*\n_30\\-45 seconds_",
        parse_mode="MarkdownV2",
    )
    await _do_recommend(msg)


async def _do_recommend(msg):
    """Shared logic for /recommend and the inline recommend button."""
    all_picks     = []
    all_summaries = []
    events_scanned = 0

    for sport_key in PRIORITY_SPORTS:
        raw = fetch_odds(sport_key)
        if not raw:
            continue
        events = preprocess_events(raw, limit=8)
        if not events:
            continue

        sport_name = TORN_SPORTS.get(sport_key, sport_key)
        events_scanned += len(events)

        await msg.edit_text(
            f"🧠 *Analyzing {_escape_md(sport_name)} \\({len(events)} events\\)\\.\\.\\.*",
            parse_mode="MarkdownV2",
        )

        result  = claude_analyze(sport_key, sport_name, events)
        picks   = result.get("picks", [])
        summary = result.get("analysis_summary", "")

        if picks:
            all_picks.extend(picks)
            if summary:
                all_summaries.append(
                    f"*{_escape_md(sport_name)}:* {_escape_md(summary)}"
                )

        if len(all_picks) >= 5:
            break

    if not all_picks:
        await msg.edit_text(
            "❌ No value bets found right now\\. Markets may be closed or vig too high\\.\n"
            "Try /scan \\| Use /watchstart for continuous monitoring\\.",
            parse_mode="MarkdownV2",
        )
        return

    saved = db_save_picks(all_picks)
    now   = _escape_md(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    lines = [
        "🏆 *Claude's Top Picks*",
        f"🕐 {now} \\| {events_scanned} events scanned",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    lines.extend(all_summaries)
    if all_summaries:
        lines.append("")

    for i, p in enumerate(all_picks[:5], 1):
        te   = TIER_EMOJI.get(p.get("confidence", "LOW"), "⚪")
        conf = _escape_md(p.get("confidence", ""))
        mtch = _escape_md(p.get("match", ""))
        pick = _escape_md(p.get("pick", ""))
        odds = p.get("decimal_odds", 0)
        prob = p.get("implied_prob", 0)
        rat  = _escape_md(p.get("rationale", ""))
        lines += [
            f"{i}\\. {te} *{conf}* — {mtch}",
            f"   Bet: *{pick}* @ {odds} \\({prob:.1f}% implied\\)",
            f"   _{rat}_",
            "",
        ]

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💾 *{saved} picks logged\\.* Use /resolve after matches\\.",
        "📈 /report for win rate \\| /history for all picks",
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n_\\.\\.\\. /history for full list_"
    await msg.edit_text(text, parse_mode="MarkdownV2")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        sport_key  = context.args[0].lower()
        sport_name = TORN_SPORTS.get(sport_key, sport_key.replace("_", " ").title())
        await _run_analysis(update.message, sport_key, sport_name)
    else:
        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"analyze:{key}:{label[:28]}")]
            for key, label in list(TORN_SPORTS.items())[:8]
        ]
        await update.message.reply_text(
            "Select a sport:", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def _run_analysis(target, sport_key: str, sport_name: str):
    """Fetch + analyze one sport. target is a Message object."""
    msg = await target.reply_text(
        f"📡 Fetching *{_escape_md(sport_name)}* odds\\.\\.\\.",
        parse_mode="MarkdownV2",
    )
    raw = fetch_odds(sport_key)
    if not raw:
        await msg.edit_text(
            f"❌ No active events for *{_escape_md(sport_name)}*\\.",
            parse_mode="MarkdownV2",
        )
        return

    events = preprocess_events(raw)
    await msg.edit_text(
        f"🧠 *Claude analyzing {len(events)} {_escape_md(sport_name)} events\\.\\.\\.*",
        parse_mode="MarkdownV2",
    )

    result     = claude_analyze(sport_key, sport_name, events)
    picks      = result.get("picks", [])
    skipped    = result.get("skipped_matches", [])
    summary    = result.get("analysis_summary", "")
    disclaimer = result.get("disclaimer", "")

    if "error" in result and not picks:
        await msg.edit_text(
            f"❌ Claude error: {_escape_md(result['error'])}",
            parse_mode="MarkdownV2",
        )
        return

    saved = db_save_picks(picks) if picks else 0
    now   = _escape_md(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    lines = [
        f"📊 *{_escape_md(sport_name)} Analysis*",
        f"🕐 {now} \\| {len(events)} events",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if summary:
        lines += [f"_{_escape_md(summary)}_", ""]

    if picks:
        for i, p in enumerate(picks, 1):
            te   = TIER_EMOJI.get(p.get("confidence", "LOW"), "⚪")
            conf = _escape_md(p.get("confidence", ""))
            mtch = _escape_md(p.get("match", ""))
            pick = _escape_md(p.get("pick", ""))
            odds = p.get("decimal_odds", 0)
            prob = p.get("implied_prob", 0)
            rat  = _escape_md(p.get("rationale", ""))
            lines += [
                f"{i}\\. {te} *{conf}* — {mtch}",
                f"   Bet: *{pick}* @ {odds} \\({prob:.1f}%\\)",
                f"   _{rat}_",
                "",
            ]
    else:
        lines.append("_No strong value bets found in this market\\._")

    if skipped:
        sk = ", ".join(_escape_md(s) for s in skipped[:3])
        lines.append(f"_Skipped: {sk}_")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💾 *{saved} picks logged\\.* _{_escape_md(disclaimer)}_",
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n_\\[truncated\\]_"
    await msg.edit_text(text, parse_mode="MarkdownV2")


async def cmd_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ *Fetching scores and resolving pending bets\\.\\.\\.*",
        parse_mode="MarkdownV2",
    )
    result = resolve_pending_bets()
    r = result["resolved"]
    p = result["still_pending"]
    await msg.edit_text(
        "\n".join([
            "✅ *Resolution Complete*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"Resolved: *{r} bets*",
            f"Still pending: *{p}* \\(match not finished\\)",
            "",
            "📈 /report to see updated win rate",
            "📋 /history to see individual outcomes",
        ]),
        parse_mode="MarkdownV2",
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = 7
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 365))
        except ValueError:
            pass

    msg = await update.message.reply_text(
        f"📈 *Generating {days}\\-day report with Claude\\.\\.\\.*\n_30\\-45 seconds_",
        parse_mode="MarkdownV2",
    )
    rows = db_get_period(days)
    if not rows:
        await msg.edit_text(
            f"📭 No bets logged in the last {days} days\\.\n"
            "Use /recommend or /watchstart to begin tracking\\.",
            parse_mode="MarkdownV2",
        )
        return

    period_label = f"Last {days} days"
    report_text  = claude_report(rows, period_label)
    db_save_report(period_label, report_text)

    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"📈 *Performance Report — Last {days} Days*\n"
        f"🕐 Generated: {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    full = header + report_text
    pieces = _chunks(full)
    await msg.edit_text(pieces[0], parse_mode="Markdown")
    for piece in pieces[1:]:
        await update.message.reply_text(piece, parse_mode="Markdown")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    rows = db_get_recent(limit)
    if not rows:
        await update.message.reply_text(
            "📭 No bets logged yet\\. Use /recommend or /watchstart\\.",
            parse_mode="MarkdownV2",
        )
        return

    lines = [f"📋 *Last {len(rows)} Picks*", "━━━━━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        se   = STATUS_EMOJI.get(r["status"], "❓")
        te   = TIER_EMOJI.get(r["confidence"], "⚪")
        date = r["recommended_at"][:10]
        mtch = _escape_md(r["match"])
        pick = _escape_md(r["pick"])
        conf = _escape_md(r["confidence"])
        wl   = f"\n   Result: {_escape_md(r['actual_winner'])}" if r["actual_winner"] else ""
        lines.append(
            f"{se} {te} `{date}` *{mtch}*\n"
            f"   {pick} @ {r['decimal_odds']} \\[{conf}\\]{wl}"
        )

    resolved = [r for r in rows if r["status"] in ("WON", "LOST")]
    wins     = sum(1 for r in resolved if r["status"] == "WON")
    if resolved:
        wr = round(wins / len(resolved) * 100, 1)
        pending_n = sum(1 for r in rows if r["status"] == "PENDING")
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"*{wins}W* / *{len(resolved)-wins}L* / *{pending_n}P* "
            f"\\({wr}% win rate on resolved\\)",
        ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n_\\[truncated\\]_"
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("analyze:"):
        parts      = data.split(":", 2)
        sport_key  = parts[1]
        sport_name = parts[2] if len(parts) > 2 else sport_key

        await query.edit_message_text(
            f"📡 Fetching *{_escape_md(sport_name)}* odds\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        raw = fetch_odds(sport_key)
        if not raw:
            await query.edit_message_text(
                f"❌ No active events for *{_escape_md(sport_name)}*\\.",
                parse_mode="MarkdownV2",
            )
            return

        events = preprocess_events(raw)
        await query.edit_message_text(
            f"🧠 *Claude analyzing {len(events)} events\\.\\.\\.*",
            parse_mode="MarkdownV2",
        )
        result  = claude_analyze(sport_key, sport_name, events)
        picks   = result.get("picks", [])
        saved   = db_save_picks(picks) if picks else 0
        summary = result.get("analysis_summary", "")

        lines = [
            f"📊 *{_escape_md(sport_name)}* \\| {len(events)} events \\| {saved} logged",
            "",
        ]
        if summary:
            lines += [f"_{_escape_md(summary)}_", ""]
        for i, p in enumerate(picks, 1):
            te   = TIER_EMOJI.get(p.get("confidence", "LOW"), "⚪")
            conf = _escape_md(p.get("confidence", ""))
            mtch = _escape_md(p.get("match", ""))
            pick = _escape_md(p.get("pick", ""))
            rat  = _escape_md(p.get("rationale", ""))
            lines += [
                f"{i}\\. {te} *{conf}* — {mtch}",
                f"   Bet *{pick}* @ {p.get('decimal_odds',0)} "
                f"\\({p.get('implied_prob',0):.1f}%\\)",
                f"   _{rat}_",
                "",
            ]
        if not picks:
            lines.append("_No strong value bets found\\._")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3950] + "\n_\\[truncated\\]_"
        await query.edit_message_text(text, parse_mode="MarkdownV2")

    elif data == "recommend:all":
        await query.edit_message_text(
            "🧠 *Claude scanning priority markets\\.\\.\\.*",
            parse_mode="MarkdownV2",
        )
        await _do_recommend(query)


# =============================================================================
# REAL-TIME SCANNER ENGINE
# =============================================================================

def _format_alert(pick: dict) -> str:
    """Build a MarkdownV2-safe push-alert message for one high-value pick."""
    te    = TIER_EMOJI.get(pick.get("confidence", "LOW"), "⚪")
    conf  = _escape_md(pick.get("confidence", ""))
    sport = _escape_md(pick.get("sport_name", ""))
    mtch  = _escape_md(pick.get("match", ""))
    pick_ = _escape_md(pick.get("pick", ""))
    odds  = pick.get("decimal_odds", 0)
    prob  = pick.get("implied_prob", 0.0)
    rat   = _escape_md(pick.get("rationale", ""))
    ct    = pick.get("commence_time", "")
    start = _escape_md((ct[:16].replace("T", " ") + " UTC") if ct else "TBD")

    return "\n".join([
        f"🚨 *BOOKIE ALERT* {te} {conf}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 *{sport}*",
        f"⚔️  {mtch}",
        f"✅ Bet: *{pick_}*",
        f"📊 Odds: *{odds}* \\({prob:.1f}% implied\\)",
        f"🕐 Starts: {start}",
        f"💡 _{rat}_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "_Auto\\-logged\\. Use /resolve after match\\._",
    ])


async def _scanner_loop(bot, chat_id: str, interval: int):
    """
    Background coroutine: scans ALL active Torn-compatible sports every
    `interval` seconds. Pushes Telegram alerts for picks meeting the
    confidence threshold. Deduplicates by event_id so alerts fire once.

    Cycle steps:
      1. Discover all active sports dynamically (not just TORN_SPORTS dict)
      2. Fetch live odds for every sport, up to 50 events each
      3. Skip events already alerted this session
      4. Send new events to Claude for analysis
      5. Log all picks to DB
      6. Push Telegram alert for threshold-meeting picks
      7. Sleep until next cycle
    """
    global _scanner_running, _alerted_ids

    logger.info(
        f"Scanner started | interval={interval}s | "
        f"min_conf={ALERT_MIN_CONF} | chat={chat_id}"
    )

    # Opening notification
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="\n".join([
                "🟢 *Bookie Scanner is LIVE*",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"📡 Scanning ALL sports every *{interval // 60} min*",
                f"🔔 Alert threshold: *{_escape_md(ALERT_MIN_CONF)}* confidence and above",
                "_I will ping you the moment I spot a high\\-value opportunity\\._",
                "",
                "Use /watchstop to stop \\| /watchstatus for state",
            ]),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Scanner open notify failed: {e}")

    while _scanner_running:
        scan_start    = datetime.now(timezone.utc)
        cycle_sports  = 0
        cycle_events  = 0
        cycle_picks   = 0
        cycle_alerts  = 0

        # Step 1: discover sports dynamically — catches new sports Torn adds
        active_sports = discover_torn_sports()
        cycle_sports  = len(active_sports)
        logger.info(f"Scanner cycle: {cycle_sports} active sports")

        for sport_key, sport_title in active_sports:
            if not _scanner_running:
                break

            sport_name = TORN_SPORTS.get(sport_key, sport_title)
            raw_events = fetch_odds(sport_key)
            if not raw_events:
                await asyncio.sleep(1)
                continue

            # Full scan — up to 50 events per sport
            events = preprocess_events(raw_events, limit=50)
            if not events:
                continue

            cycle_events += len(events)

            # Step 3: filter out already-alerted events
            new_events = [
                e for e in events
                if e.get("event_id") not in _alerted_ids
            ]
            if not new_events:
                await asyncio.sleep(1)
                continue

            # Step 4: Claude analyzes new events
            result = claude_analyze(sport_key, sport_name, new_events)
            picks  = result.get("picks", [])
            if not picks:
                await asyncio.sleep(1)
                continue

            # Step 5: log all picks (regardless of confidence)
            db_save_picks(picks)
            cycle_picks += len(picks)

            # Step 6: alert on threshold-meeting picks
            for pick in picks:
                eid = pick.get("event_id", "")

                if not _meets_threshold(pick.get("confidence", "LOW")):
                    continue
                if eid and eid in _alerted_ids:
                    continue

                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=_format_alert(pick),
                        parse_mode="MarkdownV2",
                    )
                    if eid:
                        _alerted_ids.add(eid)
                    cycle_alerts += 1
                    logger.info(
                        f"Alert sent: {pick.get('match')} → "
                        f"{pick.get('pick')} [{pick.get('confidence')}]"
                    )
                except Exception as e:
                    logger.error(f"Alert send failed: {e}")

            await asyncio.sleep(2)   # rate-limit between sports

        elapsed = (datetime.now(timezone.utc) - scan_start).seconds
        logger.info(
            f"Cycle done | sports={cycle_sports} events={cycle_events} "
            f"picks={cycle_picks} alerts={cycle_alerts} elapsed={elapsed}s"
        )

        if not _scanner_running:
            break

        # Quiet digest when no alerts fired (confirms scanner is alive)
        if cycle_alerts == 0:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="\n".join([
                        "🔍 *Scan complete* — no new opportunities at threshold",
                        f"Checked *{cycle_sports}* sports, *{cycle_events}* events",
                        f"⏳ Next scan in *{interval // 60} min*",
                    ]),
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass

        await asyncio.sleep(interval)

    logger.info("Scanner loop exited cleanly.")


# =============================================================================
# SCANNER COMMANDS
# =============================================================================

async def cmd_watchstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the real-time background scanner."""
    global _scanner_task, _scanner_running, _alerted_ids

    if _scanner_running:
        await update.message.reply_text(
            "⚠️ Scanner is already running\\. Use /watchstop to stop it first\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Target chat: env var > chat that sent the command
    target_chat = CHAT_ID or str(update.effective_chat.id)

    # Optional custom interval: /watchstart 900
    interval = SCAN_INTERVAL
    if context.args:
        try:
            interval = max(300, min(int(context.args[0]), 86400))
        except ValueError:
            pass

    _scanner_running = True
    _alerted_ids     = set()   # reset dedup on each fresh start

    _scanner_task = asyncio.create_task(
        _scanner_loop(context.bot, target_chat, interval)
    )

    await update.message.reply_text(
        "\n".join([
            "✅ *Scanner started\\!*",
            f"📡 Scanning ALL Torn sports every *{interval // 60} min*",
            f"🔔 Alerting: *{_escape_md(ALERT_MIN_CONF)}* confidence and above",
            f"📬 Push target: `{_escape_md(target_chat)}`",
            "",
            "Use /watchstop to stop \\| /watchstatus for state",
        ]),
        parse_mode="MarkdownV2",
    )


async def cmd_watchstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the background scanner."""
    global _scanner_task, _scanner_running

    if not _scanner_running:
        await update.message.reply_text(
            "ℹ️ Scanner is not running\\. Use /watchstart to start it\\.",
            parse_mode="MarkdownV2",
        )
        return

    _scanner_running = False
    if _scanner_task and not _scanner_task.done():
        _scanner_task.cancel()
        try:
            await _scanner_task
        except asyncio.CancelledError:
            pass
    _scanner_task = None

    await update.message.reply_text(
        "\n".join([
            "🔴 *Scanner stopped\\.*",
            f"Session alerted *{len(_alerted_ids)}* unique events\\.",
            "Use /watchstart to resume\\.",
        ]),
        parse_mode="MarkdownV2",
    )


async def cmd_watchstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current scanner state."""
    status = "🟢 *RUNNING*" if _scanner_running else "🔴 *STOPPED*"
    target = _escape_md(CHAT_ID or str(update.effective_chat.id))
    conf   = _escape_md(ALERT_MIN_CONF)

    await update.message.reply_text(
        "\n".join([
            "📡 *Scanner Status*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"Status: {status}",
            f"Alert threshold: *{conf}* confidence",
            f"Scan interval: *{SCAN_INTERVAL // 60} min*",
            f"Push target: `{target}`",
            f"Events alerted this session: *{len(_alerted_ids)}*",
            "",
            "/watchstart \\[interval\\_sec\\] — Start scanner",
            "/watchstop — Stop scanner",
            "/watchstatus — This panel",
        ]),
        parse_mode="MarkdownV2",
    )


# =============================================================================
# RAILWAY HEALTH SERVER
# =============================================================================

async def health_handler(request: web.Request) -> web.Response:
    pending = len(db_get_pending())
    return web.json_response({
        "status":          "ok",
        "service":         "Torn Bookie Analyzer Bot v4",
        "scanner_running": _scanner_running,
        "alerted_events":  len(_alerted_ids),
        "pending_bets":    pending,
        "mode":            "webhook" if WEBHOOK_URL else "polling",
        "time":            datetime.now(timezone.utc).isoformat(),
    })


async def run_health_server(port: int):
    app_web = web.Application()
    app_web.router.add_get("/",       health_handler)
    app_web.router.add_get("/health", health_handler)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"Health server on :{port}")
    while True:
        await asyncio.sleep(3600)


# =============================================================================
# APP WIRING & MAIN
# =============================================================================

def build_app() -> Application:
    for val, name in [
        (TELEGRAM_TOKEN,    "TELEGRAM_TOKEN"),
        (ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY"),
        (ODDS_API_KEY,      "ODDS_API_KEY"),
    ]:
        if not val:
            raise ValueError(f"{name} not set in environment")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("info",        cmd_info))
    app.add_handler(CommandHandler("scan",        cmd_scan))
    app.add_handler(CommandHandler("recommend",   cmd_recommend))
    app.add_handler(CommandHandler("analyze",     cmd_analyze))
    app.add_handler(CommandHandler("resolve",     cmd_resolve))
    app.add_handler(CommandHandler("report",      cmd_report))
    app.add_handler(CommandHandler("history",     cmd_history))
    app.add_handler(CommandHandler("watchstart",  cmd_watchstart))
    app.add_handler(CommandHandler("watchstop",   cmd_watchstop))
    app.add_handler(CommandHandler("watchstatus", cmd_watchstatus))
    app.add_handler(CallbackQueryHandler(cmd_callback))
    return app


def main():
    db_init()
    app = build_app()

    if WEBHOOK_URL:
        WEBHOOK_PORT = PORT + 1
        webhook_path = f"/webhook/{TELEGRAM_TOKEN}"
        full_url     = f"{WEBHOOK_URL}{webhook_path}"

        logger.info(f"Webhook mode | health:{PORT} | ptb:{WEBHOOK_PORT}")
        logger.info(f"Webhook URL: {full_url}")

        async def run_all():
            health_task = asyncio.create_task(run_health_server(PORT))
            await app.initialize()
            await app.bot.set_webhook(url=full_url, allowed_updates=Update.ALL_TYPES)
            await app.start()
            app.run_webhook(
                listen="0.0.0.0",
                port=WEBHOOK_PORT,
                url_path=webhook_path,
                webhook_url=full_url,
                allowed_updates=Update.ALL_TYPES,
                close_loop=False,
            )
            try:
                await health_task
            finally:
                await app.stop()
                await app.shutdown()

        asyncio.run(run_all())
    else:
        logger.info("Polling mode (local dev)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
