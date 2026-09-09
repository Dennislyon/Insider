import os
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    filters,
)
import uuid

# ====================== LOAD ENVIRONMENT ======================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # Optional – for auto posting

# ====================== LOGGING ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== IN-MEMORY STORAGE ======================
# For production, replace with SQLite or Redis
user_alerts = {}          # {chat_id: [{"token": str, "target": float, "direction": str}]}
seen_tokens = set()       # To avoid posting the same new token multiple times
ADMINS = set()            # Add your Telegram user IDs here, e.g. {123456789}

# ====================== HELPER FUNCTIONS ======================
def get_dexscreener_token(address: str):
    """Fetch token data from DexScreener"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None
    except Exception as e:
        logger.error(f"DexScreener error: {e}")
        return None


def get_rugcheck(address: str):
    """Basic safety check via RugCheck (public endpoint)"""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{address}/report"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        logger.error(f"RugCheck error: {e}")
        return None


# ====================== COMMAND HANDLERS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚀 *Meme Radar Bot*\n\n"
        "Available commands:\n"
        "/trending – Trending / boosted tokens\n"
        "/scan <address> – Token info + safety check\n"
        "/alert <token> <price> – Set price alert\n"
        "/myalerts – See your active alerts\n"
        "/help – Show this message\n\n"
        "You can also use me in *inline mode*:\n"
        "Type `@YourBotUsername tokenname` in any chat"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        data = r.json()[:10]

        if not data:
            await update.message.reply_text("No trending data right now.")
            return

        msg = "🔥 *Latest Boosted / Trending Tokens*\n\n"
        for item in data:
            chain = item.get("chainId", "?")
            token = item.get("tokenAddress", "")
            desc = (item.get("description") or "")[:70]
            msg += f"• `{token}` ({chain})\n{desc}\n\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error fetching trending: {e}")


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage:\n`/scan <token_address>`", parse_mode="Markdown")
        return

    mint = context.args[0].strip()

    pair = get_dexscreener_token(mint)
    if not pair:
        await update.message.reply_text("❌ Token not found on DexScreener.")
        return

    name = pair["baseToken"]["name"]
    symbol = pair["baseToken"]["symbol"]
    price = pair.get("priceUsd", "N/A")
    liq = pair.get("liquidity", {}).get("usd", 0) or 0
    vol = pair.get("volume", {}).get("h24", 0) or 0
    mcap = pair.get("fdv") or pair.get("marketCap") or "N/A"
    url = pair.get("url", "")

    # Safety checks
    risks = []
    rug = get_rugcheck(mint)
    if rug:
        if rug.get("mintAuthority"):
            risks.append("🔴 Mint authority still active")
        if rug.get("freezeAuthority"):
            risks.append("🔴 Freeze authority still active")
        score = rug.get("score", "N/A")
    else:
        score = "N/A"

    if liq < 3000:
        risks.append("⚠️ Very low liquidity")
    if isinstance(vol, (int, float)) and vol < 1000:
        risks.append("⚠️ Very low volume")

    risk_text = "\n".join(risks) if risks else "✅ No major red flags detected"

    msg = (
        f"🔍 *{name} ({symbol})*\n"
        f"`{mint}`\n\n"
        f"💰 Price: `${price}`\n"
        f"💧 Liquidity: `${liq:,.0f}`\n"
        f"📊 24h Volume: `${vol:,.0f}`\n"
        f"🏦 MCAP/FDV: `${mcap}`\n"
        f"🛡️ RugCheck Score: `{score}`\n\n"
        f"{risk_text}\n\n"
        f"[Open on DexScreener]({url})"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n`/alert <token_or_symbol> <price>`\n\n"
            "Examples:\n`/alert SOL 180`\n`/alert So111... 0.00045`",
            parse_mode="Markdown"
        )
        return

    token = context.args[0]
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid price. Use a number.")
        return

    chat_id = update.effective_chat.id
    user_alerts.setdefault(chat_id, []).append({
        "token": token,
        "target": target,
        "direction": "above",
        "created": datetime.now().isoformat()
    })

    await update.message.reply_text(
        f"✅ Alert set!\nWhen *{token}* goes above `${target}` I will notify you.",
        parse_mode="Markdown"
    )


async def myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    alerts = user_alerts.get(chat_id, [])

    if not alerts:
        await update.message.reply_text("You have no active alerts.")
        return

    msg = "📋 *Your active alerts:*\n\n"
    for i, a in enumerate(alerts, 1):
        msg += f"{i}. {a['token']} → above `${a['target']}`\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ====================== INLINE MODE ======================
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if len(query) < 3:
        return

    results = []
    # Simple search using DexScreener
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={query}", timeout=8)
        pairs = r.json().get("pairs", [])[:5]

        for p in pairs:
            name = p["baseToken"]["name"]
            symbol = p["baseToken"]["symbol"]
            price = p.get("priceUsd", "N/A")
            liq = p.get("liquidity", {}).get("usd", 0) or 0
            mint = p["baseToken"]["address"]

            results.append(
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title=f"{symbol} — ${price}",
                    description=f"{name} | Liq ${liq:,.0f}",
                    input_message_content=InputTextMessageContent(
                        f"*{name} ({symbol})*\n"
                        f"`{mint}`\n"
                        f"Price: ${price}\n"
                        f"Liquidity: ${liq:,.0f}\n"
                        f"[Chart]({p.get('url')})",
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                )
            )
    except Exception as e:
        logger.error(f"Inline error: {e}")

    await update.inline_query.answer(results, cache_time=20)


# ====================== BACKGROUND JOBS ======================
async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """Check all price alerts every 45 seconds"""
    for chat_id, alerts in list(user_alerts.items()):
        remaining = []
        for a in alerts:
            # Very basic price fetch – improve later
            pair = get_dexscreener_token(a["token"]) if len(a["token"]) > 30 else None
            if not pair:
                remaining.append(a)
                continue

            try:
                current = float(pair.get("priceUsd", 0))
                if current >= a["target"]:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🚨 *PRICE ALERT*\n\n*{a['token']}* is now `\( {current}`\n(Your target: ` \){a['target']}`)",
                        parse_mode="Markdown"
                    )
                else:
                    remaining.append(a)
            except:
                remaining.append(a)

        user_alerts[chat_id] = remaining


async def new_tokens_job(context: ContextTypes.DEFAULT_TYPE):
    """Simple new token detector (boosted / latest profiles)"""
    if not CHANNEL_ID:
        return

    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        data = r.json()[:8]

        for item in data:
            addr = item.get("tokenAddress")
            if not addr or addr in seen_tokens:
                continue

            seen_tokens.add(addr)
            desc = (item.get("description") or "New token")[:90]

            msg = (
                f"🆕 *New Token Detected*\n\n"
                f"`{addr}`\n"
                f"{desc}\n\n"
                f"Chain: {item.get('chainId', '?')}"
            )
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=msg,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"New tokens job error: {e}")


# ====================== MAIN ======================
def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN is missing in .env")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("trending", trending))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("alert", alert))
    app.add_handler(CommandHandler("myalerts", myalerts))

    # Inline mode
    app.add_handler(InlineQueryHandler(inline_query))

    # Background jobs
    job_queue = app.job_queue
    job_queue.run_repeating(check_alerts_job, interval=45, first=15)
    job_queue.run_repeating(new_tokens_job, interval=90, first=20)

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
