# 🎰 Torn City Bookie Analyzer Bot — v4

A Telegram bot where **Claude AI is the core analyst**. It scans real-time sports odds matching Torn City's Bookie feature, identifies value bets, logs every pick to SQLite, resolves them against real match scores, and reports your actual win rate over time.

**New in v4:** A real-time background scanner that watches ALL active bookie sports continuously and pushes Telegram alerts the moment a high-value opportunity appears.

---

## ⚠️ Read This First

**The Torn City API does not expose bookie data.** Torn's Bookie uses a third-party live odds provider. This bot connects to [The Odds API](https://the-odds-api.com/) which covers the same sports (NFL, NBA, NHL, MLB, EPL, Tennis, Rugby, Cricket, CS2, Dota2, etc.).

**What "win rate" means here:** Computed from real match results fetched after each game ends — not simulated. Claude identifies value bets using implied probability math. No system guarantees wins per event. Value betting works over volume, not individual games.

---

## 🧠 Architecture

```
The Odds API (40+ bookmakers, live odds)
        ↓ preprocess_events()
Claude Sonnet (structured JSON analysis)
        ↓ db_save_picks()
SQLite (every pick logged: event_id, odds, confidence, status)
        ↓ resolve_pending_bets()
Odds API scores → WON / LOST / VOID
        ↓ claude_report()
Claude (performance report with real win rate)
        ↓
Telegram (you)
```

The **real-time scanner** runs as a background asyncio task:
```
/watchstart → _scanner_loop() (asyncio.Task)
  every SCAN_INTERVAL seconds:
    1. discover_torn_sports() — ALL active sports dynamically
    2. fetch_odds() for each sport (up to 50 events)
    3. Filter already-alerted event_ids (dedup)
    4. claude_analyze() — Claude evaluates new events
    5. db_save_picks() — log everything
    6. Push alert for picks ≥ ALERT_MIN_CONF
    7. Quiet digest if zero alerts (confirms alive)
```

---

## 🚀 Setup

### Prerequisites
- Python 3.10+
- A Telegram account
- Three API keys (all free to start)

### Step 1: Install
```bash
git clone <your-repo>
cd torn_bookie_bot
pip install -r requirements.txt
```

### Step 2: Get API Keys

| Key | Where | Cost |
|-----|-------|------|
| `TELEGRAM_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` | Free |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Pay-per-use |
| `ODDS_API_KEY` | [the-odds-api.com](https://the-odds-api.com/) | 500 req/month free |

### Step 3: Get your Telegram Chat ID
Message [@userinfobot](https://t.me/userinfobot) on Telegram. It will reply with your user ID. Put this in `CHAT_ID`.

### Step 4: Configure
```bash
cp .env.example .env
nano .env   # fill in all values
```

### Step 5: Run locally
```bash
python bot.py
```

---

## 🚂 Railway Deployment

```
1. Push all files to a GitHub repo

2. Railway dashboard → New Project → Deploy from GitHub repo

3. Variables tab → add these:
   TELEGRAM_TOKEN     = (from BotFather)
   ANTHROPIC_API_KEY  = (from Anthropic console)
   ODDS_API_KEY       = (from the-odds-api.com)
   CHAT_ID            = (your Telegram user ID from @userinfobot)
   SCAN_INTERVAL      = 1800
   ALERT_MIN_CONF     = HIGH
   WEBHOOK_URL        = (leave BLANK for first deploy)

4. Deploy → wait for green

5. Settings → Domains → Generate Domain → copy the URL

6. Variables tab → set WEBHOOK_URL = https://your-app.up.railway.app

7. Redeploy → bot is live

8. In Telegram: /start  then  /watchstart
```

---

## 🤖 Commands

### Manual Commands
| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/scan` | Browse all active Torn-compatible sports |
| `/recommend` | Claude picks top bets right now (auto-logged) |
| `/analyze soccer_epl` | Analyze one sport specifically |
| `/resolve` | Fetch real scores, mark picks WON/LOST/VOID |
| `/report 7` | Performance report for last 7 days |
| `/history 20` | Last 20 logged picks with outcomes |
| `/info` | Methodology explanation |
| `/help` | Full command reference |

### Real-Time Scanner
| Command | Description |
|---------|-------------|
| `/watchstart` | Start scanning ALL sports (interval from env) |
| `/watchstart 900` | Start with custom 15-min interval |
| `/watchstop` | Stop scanner gracefully |
| `/watchstatus` | Running state, threshold, events alerted |

---

## 📲 Alert Format

When the scanner finds a qualifying pick:

```
🚨 BOOKIE ALERT 🟢 HIGH
━━━━━━━━━━━━━━━━━━━━━━
🏆 NBA (Basketball)
⚔️  Boston Celtics vs Miami Heat
✅ Bet: Boston Celtics
📊 Odds: 1.62 (61.7% implied)
🕐 Starts: 2026-04-11 01:30 UTC
💡 Strong consensus across 7 bookmakers. Low vig market at 3.2%.
━━━━━━━━━━━━━━━━━━━━━━
Auto-logged. Use /resolve after match.
```

---

## 📈 Report Output Structure

`/report 30` asks Claude to write:

1. **Period & Sample Size**
2. **Overall Win Rate** (resolved only, VOIDs excluded)
3. **ROI Estimate** (flat 1-unit stakes)
4. **By Confidence Tier** — HIGH / MEDIUM / LOW win rates separately
5. **By Sport** — best and worst sports
6. **Best Pick** — highest-odds WON bet
7. **Worst Miss** — highest-odds LOST bet
8. **Calibration** — are HIGH picks outperforming LOW?
9. **Verdict** — direct assessment: keep following or not?

---

## 📊 Odds API Usage Budget

| Action | API calls |
|--------|-----------|
| `/scan` | 1 |
| `/analyze [sport]` | 1 |
| `/recommend` | 1–6 |
| `/resolve` | 1 per sport with pending bets |
| `/watchstart` cycle | ~1 per active sport found |

Free tier: **500 req/month**. At `SCAN_INTERVAL=1800` (30 min) with ~10 active sports, expect ~480 calls/day — **upgrade to paid** ($5/month for 10k) if running 24/7.

---

## 💡 Responsible Use

- Never bet more than 1–5% of your bankroll per event
- Focus on HIGH confidence picks only
- Value betting requires volume to see statistical edge
- Never chase losses
- The bot is a filter — not a guarantee

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Entire bot — DB, odds, Claude, scanner, Telegram |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway build + deploy config |
| `Procfile` | Process declaration |
| `.env.example` | Environment variable template |
| `bets.db` | SQLite database (auto-created on first run) |
