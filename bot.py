"""
╔══════════════════════════════════════════════════════════╗
║   🕌 Egypt Sharia Investment Monitor Bot                 ║
║   Telegram Bot — Full Monitoring Suite                   ║
║   Funds: NMF · CMS · ASO · AZG · MTF                    ║
╚══════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import json
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)

# ─────────────────────────────────────────────
#  CONFIG  (edit config.json — not this file)
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
CAIRO_TZ = ZoneInfo("Africa/Cairo")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log")),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("ShariaBot")

def load_config() -> dict:
    # Fall back to config.example.json if config.json is absent (e.g. in production containers)
    cfg_path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else os.path.join(BASE_DIR, "config.example.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    # Secrets injected via env vars (Coolify) override whatever is in the file
    if os.environ.get("BOT_TOKEN"):
        cfg["telegram"]["bot_token"] = os.environ["BOT_TOKEN"]
    if os.environ.get("CHAT_ID"):
        cfg["telegram"]["chat_id"] = os.environ["CHAT_ID"]
    return cfg

def load_portfolio() -> dict:
    if not os.path.exists(PORTFOLIO_FILE):
        return {"investments": [], "start_date": datetime.now().strftime("%Y-%m-%d")}
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)

def save_portfolio(data: dict):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────
#  DATA FETCHERS
# ─────────────────────────────────────────────

def fetch_gold_price_egp() -> dict:
    """Fetch gold price via multiple fallback sources."""
    try:
        resp = requests.get(
            "https://api.gold-api.com/price/XAU",
            timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            usd_per_oz = float(data.get("price", 0))
            if usd_per_oz > 0:
                fx = fetch_usd_egp_rate()
                egp_per_oz = usd_per_oz * fx
                egp_per_gram_24k = egp_per_oz / 31.1035
                return {
                    "usd_per_oz": usd_per_oz,
                    "egp_per_oz": egp_per_oz,
                    "egp_per_gram": egp_per_gram_24k,
                    "usd_egp_rate": fx,
                    "source": "gold-api.com"
                }
    except Exception as e:
        log.warning(f"Gold API error: {e}")

    try:
        resp = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            egp_rate = data["rates"].get("EGP", 50.5)
            return {
                "usd_per_oz": None,
                "egp_per_oz": None,
                "egp_per_gram": None,
                "usd_egp_rate": egp_rate,
                "source": "fx-only (gold API unavailable)"
            }
    except Exception as e:
        log.warning(f"FX API error: {e}")

    return {"usd_per_oz": None, "egp_per_oz": None, "egp_per_gram": None,
            "usd_egp_rate": None, "source": "unavailable"}

def fetch_usd_egp_rate() -> float:
    """Fetch USD/EGP exchange rate."""
    try:
        resp = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=8
        )
        if resp.status_code == 200:
            return float(resp.json()["rates"].get("EGP", 50.5))
    except Exception:
        pass
    return 50.5

def fetch_egx_data() -> dict:
    """
    Fetch EGX30/EGX33 index data.
    Uses Yahoo Finance for EGX30 (^CASE30) as a proxy since EGX33 Sharia
    doesn't have a direct Yahoo ticker. EGX30 correlates closely.
    """
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ECASE30"
        headers = {"User-Agent": "Mozilla/5.0"}
        params = {"interval": "1d", "range": "5d"}
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            result = data["chart"]["result"][0]
            meta = result["meta"]
            closes = result["indicators"]["quote"][0].get("close", [])
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                current = closes[-1]
                prev = closes[-2]
                change_pct = ((current - prev) / prev) * 100
                return {
                    "index": "EGX30",
                    "current": current,
                    "prev_close": prev,
                    "change_pct": change_pct,
                    "currency": meta.get("currency", "EGP"),
                    "source": "Yahoo Finance"
                }
    except Exception as e:
        log.warning(f"EGX data error: {e}")
    return {"index": "EGX30", "current": None, "prev_close": None,
            "change_pct": None, "currency": "EGP", "source": "unavailable"}

def fetch_fund_nav_estimates() -> dict:
    """
    Egyptian mutual fund NAVs are NOT available via public API.
    We use proxy logic:
    - Equity funds (NMF, CMS, ASO): correlate with EGX30/33
    - Gold fund (AZG): correlate with EGP gold price
    - MTF: correlate with CBE overnight rate (stable, ~daily accrual)

    For real NAVs, users should check Thndr app directly.
    This function provides market context, not official NAVs.
    """
    egx = fetch_egx_data()
    gold = fetch_gold_price_egp()
    config = load_config()
    nav_data = {}

    funds = config.get("funds", {})
    for ticker, info in funds.items():
        fund_type = info.get("type", "equity")
        if fund_type == "equity":
            nav_data[ticker] = {
                "name": info["name"],
                "type": "Equity",
                "proxy": egx.get("change_pct"),
                "proxy_source": "EGX30 index",
                "note": "Check Thndr app for official NAV",
                "direction": "up" if (egx.get("change_pct") or 0) > 0 else "down"
            }
        elif fund_type == "gold":
            nav_data[ticker] = {
                "name": info["name"],
                "type": "Gold",
                "proxy": gold.get("egp_per_gram"),
                "proxy_source": "EGP gold price/gram",
                "note": "Check Thndr app for official NAV",
                "direction": "up"
            }
        elif fund_type == "money_market":
            nav_data[ticker] = {
                "name": info["name"],
                "type": "Money Market",
                "proxy": info.get("estimated_annual_yield", 25.0),
                "proxy_source": "CBE rate proxy",
                "note": "Stable daily accrual — check Thndr app",
                "direction": "up"
            }

    return {"funds": nav_data, "egx": egx, "gold": gold,
            "timestamp": datetime.now(CAIRO_TZ).isoformat()}

# ─────────────────────────────────────────────
#  MESSAGE FORMATTERS  (HTML parse mode)
# ─────────────────────────────────────────────

def format_daily_update() -> str:
    data = fetch_fund_nav_estimates()
    egx = data["egx"]
    gold = data["gold"]
    now = datetime.now(CAIRO_TZ)

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🕌 <b>SHARIA PORTFOLIO DAILY BRIEF</b>")
    lines.append(f"📅 {now.strftime('%A, %d %b %Y — %H:%M')} Cairo")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    lines.append("\n📈 <b>INDEX UPDATE</b>")
    if egx["current"]:
        arrow = "🟢 ▲" if egx["change_pct"] > 0 else "🔴 ▼"
        lines.append(
            f"<code>EGX30:</code> {egx['current']:,.0f} pts  "
            f"{arrow} <code>{egx['change_pct']:+.2f}%</code>"
        )
        lines.append("<i>EGX33 Sharia closely mirrors EGX30</i>")
    else:
        lines.append("<i>EGX data unavailable — market may be closed</i>")

    lines.append("\n🥇 <b>GOLD PRICE (EGP)</b>")
    if gold["egp_per_gram"]:
        lines.append(f"<code>24K per gram:</code> EGP {gold['egp_per_gram']:,.1f}")
        lines.append(f"<code>USD/EGP rate:</code> {gold['usd_egp_rate']:.2f}")
        if gold["usd_per_oz"]:
            lines.append(f"<code>Gold (USD/oz):</code> ${gold['usd_per_oz']:,.1f}")
    elif gold["usd_egp_rate"]:
        lines.append(f"<code>USD/EGP rate:</code> {gold['usd_egp_rate']:.2f}")
        lines.append("<i>Gold price per gram unavailable today</i>")
    else:
        lines.append("<i>Gold data unavailable</i>")

    lines.append("\n📊 <b>FUND CONTEXT</b>")
    lines.append("<i>Official NAVs: check Thndr app daily</i>")
    for ticker, info in data["funds"].items():
        if info["type"] == "Equity" and info["proxy"] is not None:
            arrow = "🟢" if info["proxy"] > 0 else "🔴"
            lines.append(f"{arrow} <code>{ticker}</code> — EGX30 moved <code>{info['proxy']:+.2f}%</code> today")
        elif info["type"] == "Gold" and info["proxy"]:
            lines.append(f"🥇 <code>{ticker}</code> — Gold @ EGP {info['proxy']:,.1f}/g")
        elif info["type"] == "Money Market":
            lines.append(f"🛡️ <code>{ticker}</code> — Stable accrual ~{info['proxy']:.1f}% annual yield")

    lines.append("\n💡 <b>TODAY'S SIGNAL</b>")
    if egx["change_pct"] is not None:
        if egx["change_pct"] < -3:
            lines.append("🔵 <b>DCA OPPORTUNITY</b> — Market down &gt;3%. Consider investing this month's equity allocation now rather than waiting.")
        elif egx["change_pct"] > 5:
            lines.append("⚡ <b>STRONG UP DAY</b> — Hold positions. Do not chase. Stick to your monthly DCA schedule.")
        elif -1 < egx["change_pct"] < 1:
            lines.append("😴 <b>QUIET DAY</b> — Normal. Stick to your plan.")
        else:
            lines.append("✅ <b>NORMAL MOVEMENT</b> — No action needed. Stay the course.")
    else:
        lines.append("📌 <b>Market closed or data unavailable.</b> Check Thndr app directly.")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Data: Yahoo Finance · gold-api · exchangerate-api</i>")
    lines.append("<i>⚠️ Not financial advice. Verify on Thndr app.</i>")

    return "\n".join(lines)

def format_portfolio_summary(portfolio: dict) -> str:
    config = load_config()
    now = datetime.now(CAIRO_TZ)
    investments = portfolio.get("investments", [])
    start_date = portfolio.get("start_date", "N/A")

    if not investments:
        return (
            "📊 <b>PORTFOLIO TRACKER</b>\n\n"
            "No investments recorded yet!\n\n"
            "Use /invest to log your monthly investments.\n"
            "Example: <code>/invest NMF 6750</code>"
        )

    fund_totals = {}
    for inv in investments:
        ticker = inv["ticker"]
        amount = inv["amount"]
        fund_totals[ticker] = fund_totals.get(ticker, 0) + amount

    total_invested = sum(fund_totals.values())

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        months_active = max(1, (now.year - start.year) * 12 + (now.month - start.month))
    except Exception:
        months_active = 1

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 <b>MY PORTFOLIO TRACKER</b>")
    lines.append(f"🗓️ Since: {start_date}  ({months_active} months)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━\n")

    lines.append("💰 <b>INVESTED BY FUND</b>")
    fund_info = config.get("funds", {})
    for ticker, amount in sorted(fund_totals.items(), key=lambda x: -x[1]):
        pct = (amount / total_invested * 100) if total_invested else 0
        name = fund_info.get(ticker, {}).get("name", ticker)
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        lines.append(f"<code>{ticker}</code> {name[:20]}")
        lines.append(f"  EGP {amount:,.0f}  ({pct:.1f}%)")
        lines.append(f"  <code>{bar}</code>")

    lines.append(f"\n💵 <b>TOTAL INVESTED:</b> <code>EGP {total_invested:,.0f}</code>")
    lines.append(f"📅 <b>Monthly avg:</b> <code>EGP {total_invested/months_active:,.0f}</code>")

    lines.append("\n🎯 <b>ALLOCATION CHECK</b>")
    targets = config.get("target_allocation", {})
    if targets and total_invested > 0:
        for ticker, target_pct in targets.items():
            actual = fund_totals.get(ticker, 0)
            actual_pct = (actual / total_invested * 100) if total_invested else 0
            diff = actual_pct - target_pct
            if abs(diff) > 5:
                status = "⬆️ OVER" if diff > 0 else "⬇️ UNDER"
                lines.append(f"<code>{ticker}</code>: {actual_pct:.1f}% vs target {target_pct}%  {status} by {abs(diff):.1f}%")
            else:
                lines.append(f"<code>{ticker}</code>: {actual_pct:.1f}% vs target {target_pct}%  ✅")

    lines.append("\n📈 <b>ESTIMATED GROWTH</b> (illustrative)")
    lines.append("<i>Based on avg 30% annual return assumption</i>")
    for yr in [1, 3, 5, 10]:
        projected = total_invested * ((1.30) ** yr)
        lines.append(f"  Year {yr}: <code>EGP {projected:,.0f}</code>")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Use /invest &lt;TICKER&gt; &lt;AMOUNT&gt; to log investments</i>")
    lines.append("<i>⚠️ Projections are illustrative only</i>")

    return "\n".join(lines)

def format_macro_alert() -> str:
    gold = fetch_gold_price_egp()
    egx = fetch_egx_data()
    now = datetime.now(CAIRO_TZ)

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ <b>MACRO SNAPSHOT</b>")
    lines.append(f"🕐 {now.strftime('%d %b %Y %H:%M')} Cairo")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━\n")

    lines.append("🏦 <b>CBE POLICY RATE</b>")
    lines.append("<code>Deposit rate:</code> 27.25%  (as of Apr 2025)")
    lines.append("<code>Lending rate:</code> 28.25%")
    lines.append("<i>⚡ CBE has signaled rate cuts in 2025</i>")
    lines.append("<i>→ Impact: MTF yields will decline; equity funds become more attractive</i>")

    lines.append("\n💱 <b>EGP EXCHANGE RATE</b>")
    if gold["usd_egp_rate"]:
        lines.append(f"<code>USD/EGP:</code> {gold['usd_egp_rate']:.2f}")
        lines.append(f"<code>EUR/EGP:</code> ~{gold['usd_egp_rate'] * 1.08:.2f} (est)")
    else:
        lines.append("<i>FX data unavailable</i>")

    lines.append("\n🥇 <b>GOLD</b>")
    if gold["egp_per_gram"]:
        lines.append(f"<code>24K gold/gram EGP:</code> {gold['egp_per_gram']:,.1f}")
        if gold["usd_per_oz"]:
            lines.append(f"<code>Gold USD/oz:</code> ${gold['usd_per_oz']:,.1f}")
    else:
        lines.append("<i>Gold price data unavailable</i>")

    lines.append("\n📈 <b>EGX30 INDEX</b>")
    if egx["current"]:
        arrow = "▲" if (egx["change_pct"] or 0) > 0 else "▼"
        lines.append(f"<code>Level:</code> {egx['current']:,.0f}")
        lines.append(f"<code>Today:</code> {arrow} {egx['change_pct']:+.2f}%")
    else:
        lines.append("<i>Market data unavailable</i>")

    lines.append("\n📌 <b>KEY MACRO WATCH ITEMS</b>")
    lines.append("• 🔴 EGP devaluation risk → hedge via AZG (gold)")
    lines.append("• 🔵 CBE rate cuts → reduce MTF, increase equity")
    lines.append("• 🟡 Global gold rally → AZG outperforms")
    lines.append("• 🟢 EGX bull run → NMF/CMS leading funds")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Sources: Yahoo Finance · gold-api · exchangerate-api</i>")

    return "\n".join(lines)

def format_rebalance_check(portfolio: dict) -> str:
    config = load_config()
    investments = portfolio.get("investments", [])
    fund_totals = {}
    for inv in investments:
        fund_totals[inv["ticker"]] = fund_totals.get(inv["ticker"], 0) + inv["amount"]
    total = sum(fund_totals.values())
    targets = config.get("target_allocation", {})

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔔 <b>6-MONTH REBALANCING CHECK</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if not investments or not total:
        lines.append("No portfolio data yet. Log investments with /invest first.")
        return "\n".join(lines)

    needs_rebalance = False
    lines.append("<b>Current vs Target Allocation:</b>\n")

    for ticker, target in targets.items():
        actual = fund_totals.get(ticker, 0)
        actual_pct = (actual / total * 100) if total else 0
        diff = actual_pct - target

        if abs(diff) > 5:
            needs_rebalance = True
            if diff > 0:
                action = f"⬆️ TRIM — reduce by ~EGP {abs(diff)/100 * total:,.0f}"
            else:
                action = f"⬇️ ADD — increase by ~EGP {abs(diff)/100 * total:,.0f}"
            lines.append(f"<b>{ticker}:</b> {actual_pct:.1f}% (target {target}%)")
            lines.append(f"  → {action}\n")
        else:
            lines.append(f"<b>{ticker}:</b> {actual_pct:.1f}% (target {target}%) ✅\n")

    if needs_rebalance:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("⚡ <b>ACTION NEEDED</b>")
        lines.append("Rebalance by adjusting your <b>next 1-2 monthly investments</b>")
        lines.append("Do NOT sell existing positions unless drift &gt;10%")
    else:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("✅ <b>PORTFOLIO IS BALANCED</b>")
        lines.append("No action needed. Next check: 6 months.")

    lines.append("\n<i>Rebalance strategy: redirect new monthly cash</i>")
    lines.append("<i>Only sell if drift exceeds 10% from target</i>")

    return "\n".join(lines)

# ─────────────────────────────────────────────
#  TELEGRAM COMMAND HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Investor"
    kb = [
        [InlineKeyboardButton("📊 Daily Update", callback_data="daily"),
         InlineKeyboardButton("💰 My Portfolio", callback_data="portfolio")],
        [InlineKeyboardButton("⚠️ Macro Snapshot", callback_data="macro"),
         InlineKeyboardButton("🔔 Rebalance Check", callback_data="rebalance")],
        [InlineKeyboardButton("📖 How to Log Investment", callback_data="help_invest"),
         InlineKeyboardButton("ℹ️ About This Bot", callback_data="about")],
    ]
    markup = InlineKeyboardMarkup(kb)
    msg = (
        f"🕌 <b>السلام عليكم {name}!</b>\n\n"
        f"Welcome to your <b>Egypt Sharia Investment Monitor</b>.\n\n"
        f"<b>Monitoring these funds:</b>\n"
        f"📈 <code>NMF</code> — Naeem Sharia Equity\n"
        f"📊 <code>CMS</code> — Misr Shariah Equity\n"
        f"⚡ <code>ASO</code> — AZ Sharia Opportunities\n"
        f"🥇 <code>AZG</code> — AZ Gold Fund\n"
        f"🛡️ <code>MTF</code> — Misr Takaful Fund\n\n"
        f"<b>Automated alerts:</b>\n"
        f"• ☀️ Daily market brief (9 AM Cairo)\n"
        f"• 🔔 Rebalancing reminder (every 6 months)\n"
        f"• ⚡ Macro alerts when EGP or gold moves sharply\n\n"
        f"Choose an option below or type a command:"
    )
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=markup)

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching market data...")
    text = await asyncio.to_thread(format_daily_update)
    await msg.edit_text(text, parse_mode="HTML")

async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfolio = load_portfolio()
    text = await asyncio.to_thread(format_portfolio_summary, portfolio)
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_macro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching macro data...")
    text = await asyncio.to_thread(format_macro_alert)
    await msg.edit_text(text, parse_mode="HTML")

async def cmd_rebalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfolio = load_portfolio()
    text = await asyncio.to_thread(format_rebalance_check, portfolio)
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_invest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Log an investment: /invest NMF 6750"""
    config = load_config()
    valid_tickers = list(config.get("funds", {}).keys())

    args = ctx.args
    if len(args) < 2:
        tickers_str = " | ".join(f"<code>{t}</code>" for t in valid_tickers)
        await update.message.reply_text(
            f"📝 <b>Log a monthly investment</b>\n\n"
            f"Usage: <code>/invest TICKER AMOUNT</code>\n\n"
            f"Valid tickers: {tickers_str}\n\n"
            f"Example: <code>/invest NMF 6750</code>\n"
            f"Example: <code>/invest AZG 3000</code>",
            parse_mode="HTML"
        )
        return

    ticker = args[0].upper()
    if ticker not in valid_tickers:
        await update.message.reply_text(
            f"❌ Unknown ticker <code>{ticker}</code>. Valid: {', '.join(valid_tickers)}",
            parse_mode="HTML"
        )
        return

    try:
        amount = float(args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Example: <code>/invest NMF 6750</code>",
            parse_mode="HTML"
        )
        return

    portfolio = load_portfolio()
    portfolio["investments"].append({
        "ticker": ticker,
        "amount": amount,
        "date": datetime.now(CAIRO_TZ).strftime("%Y-%m-%d"),
        "note": " ".join(args[2:]) if len(args) > 2 else ""
    })
    save_portfolio(portfolio)

    fund_name = config["funds"][ticker]["name"]
    total = sum(i["amount"] for i in portfolio["investments"] if i["ticker"] == ticker)

    await update.message.reply_text(
        f"✅ <b>Investment logged!</b>\n\n"
        f"Fund: <code>{ticker}</code> — {fund_name}\n"
        f"Amount: <code>EGP {amount:,.0f}</code>\n"
        f"Date: {datetime.now(CAIRO_TZ).strftime('%d %b %Y')}\n\n"
        f"Total invested in <code>{ticker}</code>: <code>EGP {total:,.0f}</code>\n\n"
        f"View full portfolio: /portfolio",
        parse_mode="HTML"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>COMMAND REFERENCE</b>\n\n"
        "/start — Main menu\n"
        "/daily — Today's market brief + fund signals\n"
        "/portfolio — Your investment tracker\n"
        "/macro — EGP, gold, CBE rate snapshot\n"
        "/rebalance — Check if allocation needs adjusting\n"
        "/invest TICKER AMOUNT — Log a monthly investment\n"
        "  <i>Example: /invest NMF 6750</i>\n"
        "/history — Show all logged investments\n"
        "/help — This message\n\n"
        "<b>Valid fund tickers:</b>\n"
        "<code>NMF</code> <code>CMS</code> <code>ASO</code> <code>AZG</code> <code>MTF</code>\n\n"
        "<b>Scheduled alerts:</b>\n"
        "• ☀️ 9:00 AM Cairo — Daily market brief\n"
        "• 🔔 Every 6 months — Rebalancing reminder\n"
        "• ⚡ Triggered — Sharp macro moves\n\n"
        "<i>Data sources: Yahoo Finance, gold-api, exchangerate-api</i>\n"
        "<i>⚠️ Not financial advice. Always verify on Thndr app.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfolio = load_portfolio()
    investments = portfolio.get("investments", [])
    if not investments:
        await update.message.reply_text(
            "No investments logged yet.\nUse <code>/invest NMF 6750</code> to start tracking.",
            parse_mode="HTML"
        )
        return

    recent = investments[-20:]
    lines = ["📋 <b>INVESTMENT HISTORY</b> (last 20)\n"]
    for inv in reversed(recent):
        lines.append(
            f"<code>{inv['date']}</code> — <code>{inv['ticker']}</code> — EGP {inv['amount']:,.0f}"
            + (f" <i>{inv['note']}</i>" if inv.get("note") else "")
        )

    total = sum(i["amount"] for i in investments)
    lines.append(f"\n💰 <b>Total all-time invested:</b> <code>EGP {total:,.0f}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ─────────────────────────────────────────────
#  CALLBACK QUERY HANDLER (Inline Buttons)
# ─────────────────────────────────────────────

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "daily":
        await query.message.reply_text("⏳ Fetching...")
        text = await asyncio.to_thread(format_daily_update)
        await query.message.reply_text(text, parse_mode="HTML")

    elif query.data == "portfolio":
        portfolio = load_portfolio()
        text = await asyncio.to_thread(format_portfolio_summary, portfolio)
        await query.message.reply_text(text, parse_mode="HTML")

    elif query.data == "macro":
        await query.message.reply_text("⏳ Fetching...")
        text = await asyncio.to_thread(format_macro_alert)
        await query.message.reply_text(text, parse_mode="HTML")

    elif query.data == "rebalance":
        portfolio = load_portfolio()
        text = await asyncio.to_thread(format_rebalance_check, portfolio)
        await query.message.reply_text(text, parse_mode="HTML")

    elif query.data == "help_invest":
        await query.message.reply_text(
            "📝 <b>Logging your investments</b>\n\n"
            "After each monthly investment on Thndr, log it here:\n\n"
            "<code>/invest NMF 6750</code>\n"
            "<code>/invest CMS 3000</code>\n"
            "<code>/invest AZG 3000</code>\n"
            "<code>/invest MTF 2250</code>\n\n"
            "This builds your portfolio tracker over time.",
            parse_mode="HTML"
        )

    elif query.data == "about":
        await query.message.reply_text(
            "ℹ️ <b>About This Bot</b>\n\n"
            "Built for Sharia-compliant Egyptian market investing.\n\n"
            "<b>What it does:</b>\n"
            "• Tracks EGX30/33 daily performance\n"
            "• Monitors EGP exchange rate &amp; gold price\n"
            "• Sends daily 9 AM Cairo market briefs\n"
            "• Logs your monthly fund investments\n"
            "• Checks allocation vs. targets\n"
            "• Reminds you to rebalance every 6 months\n\n"
            "<b>What it does NOT do:</b>\n"
            "• Cannot access real-time Thndr NAVs (no public API)\n"
            "• Cannot execute trades\n"
            "• Does not constitute financial advice\n\n"
            "<i>Always verify fund NAVs on the Thndr app directly.</i>",
            parse_mode="HTML"
        )

# ─────────────────────────────────────────────
#  SCHEDULED JOBS
# ─────────────────────────────────────────────

async def job_daily_brief(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs daily at 9:00 AM Cairo time."""
    config = load_config()
    chat_id = config["telegram"]["chat_id"]
    if not chat_id or chat_id == "NADER_CHAT_ID_HERE":
        log.warning("CHAT_ID not configured — skipping daily brief.")
        return
    try:
        text = await asyncio.to_thread(format_daily_update)
        await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        log.info("Daily brief sent.")
    except Exception as e:
        log.error(f"Failed to send daily brief: {e}")

async def job_macro_spike_check(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs every 4 hours — alerts on significant moves."""
    config = load_config()
    chat_id = config["telegram"]["chat_id"]
    if not chat_id or chat_id == "NADER_CHAT_ID_HERE":
        return
    try:
        egx = await asyncio.to_thread(fetch_egx_data)
        gold = await asyncio.to_thread(fetch_gold_price_egp)
        alerts = []

        if egx["change_pct"] is not None:
            if egx["change_pct"] <= -3:
                alerts.append(
                    f"🔵 <b>EGX MARKET DROP ALERT</b>\n"
                    f"EGX30 fell <code>{egx['change_pct']:.2f}%</code> today.\n"
                    f"→ <b>Potential DCA opportunity</b> for NMF/CMS.\n"
                    f"Consider investing this month's equity allocation now."
                )
            elif egx["change_pct"] >= 5:
                alerts.append(
                    f"🟢 <b>EGX STRONG RALLY</b>\n"
                    f"EGX30 up <code>{egx['change_pct']:.2f}%</code> today.\n"
                    f"→ Hold positions. Do not chase the rally."
                )

        if gold["usd_egp_rate"] and gold["usd_egp_rate"] > 53:
            alerts.append(
                f"⚠️ <b>EGP WEAKNESS ALERT</b>\n"
                f"USD/EGP at <code>{gold['usd_egp_rate']:.2f}</code>\n"
                f"→ AZG (gold fund) is your best hedge right now."
            )

        for alert in alerts:
            await ctx.bot.send_message(chat_id=chat_id, text=alert, parse_mode="HTML")
            log.info(f"Macro alert sent: {alert[:50]}")

    except Exception as e:
        log.error(f"Macro spike check failed: {e}")

async def job_rebalance_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs every 180 days."""
    config = load_config()
    chat_id = config["telegram"]["chat_id"]
    if not chat_id or chat_id == "NADER_CHAT_ID_HERE":
        return
    try:
        portfolio = load_portfolio()
        rebalance_text = await asyncio.to_thread(format_rebalance_check, portfolio)
        text = (
            "🔔 <b>6-MONTH REBALANCING REMINDER</b>\n\n"
            "It's time to review your portfolio allocation!\n\n"
        ) + rebalance_text
        await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        log.info("Rebalance reminder sent.")
    except Exception as e:
        log.error(f"Rebalance reminder failed: {e}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    config = load_config()
    token = config["telegram"]["bot_token"]

    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("\n❌ ERROR: Please set your bot token in config.json or BOT_TOKEN env var\n")
        print("Steps:")
        print("1. Open Telegram → search @BotFather")
        print("2. Send /newbot and follow instructions")
        print('3. Copy the token into config.json → telegram.bot_token\n')
        return

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(CommandHandler("rebalance", cmd_rebalance))
    app.add_handler(CommandHandler("invest", cmd_invest))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Scheduled jobs
    jq: JobQueue = app.job_queue

    # Daily brief at 9:00 AM Cairo (UTC+2 = 07:00 UTC)
    jq.run_daily(
        job_daily_brief,
        time=datetime.strptime("09:00", "%H:%M").replace(tzinfo=CAIRO_TZ).timetz(),
        name="daily_brief"
    )

    # Macro spike check every 4 hours
    jq.run_repeating(job_macro_spike_check, interval=14400, first=60, name="macro_check")

    # Rebalancing reminder every 180 days
    jq.run_repeating(job_rebalance_reminder, interval=180*86400, first=180*86400, name="rebalance")

    log.info("🕌 Sharia Investment Bot starting...")
    print("\n✅ Bot is running! Press Ctrl+C to stop.\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
