"""
Meteora DLMM Screener Service for Railway
==========================================
Standalone service that:
1. Runs Birdeye screening every 20 hours
2. Sends TOP 4 pool results to Telegram automatically
3. Provides HTTP endpoints for Railway health check

Deploy as separate Railway service.

Environment Variables (set in Railway dashboard):
    TELEGRAM_BOT_TOKEN  - Token dari BotFather (wajib)
    TELEGRAM_CHAT_ID    - Chat ID Telegram Anda (wajib)
    BIRDEYE_API_KEY     - Optional, pakai default bawaan
    SCREENING_INTERVAL  - Interval dalam detik (default: 72000 = 20 jam)
"""

import os
import sys
import json
import time
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# ── Shared state (accessed by both main thread & HTTP server thread) ─────────
last_screening_time = None
next_screening_time = None
screening_status = "Starting..."

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("screener")

# ── Config from environment ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8834794355:AAF1iEdkP45xPkOw8jsRKJInSxMunPKDEaE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7456917534")
SCREENING_INTERVAL = int(os.environ.get("SCREENING_INTERVAL", "72000"))  # 20 jam
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Birdeye API ──────────────────────────────────────────────────────────────
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "acf47eeffcce41c2a0e3a3b0e6fdd92d")
HEADERS = {
    "x-api-key": BIRDEYE_API_KEY,
    "accept": "application/json",
}

# ── Filters ──────────────────────────────────────────────────────────────────
MIN_VOLUME = 100_000
MIN_LIQUIDITY = 10_000
MAX_MARKET_CAP = 50_000_000
MAX_PRICE = 1.0
MIN_HOLDERS = 200
MIN_UNIQUE_WALLETS_24H = 50
MIN_BUY_SELL_RATIO = 0.3
REQUIRE_SOCIAL = True


# ── Helper Functions ─────────────────────────────────────────────────────────

def send_telegram(text):
    """Send message to Telegram via Bot API."""
    import httpx
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
            if resp.status_code == 200:
                logger.info("Pesan terkirim ke Telegram")
                return True
            else:
                logger.error(f"Telegram error: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"Gagal kirim Telegram: {e}")
        return False


def fetch_trending():
    """Fetch trending tokens from Birdeye."""
    import httpx
    url = "https://public-api.birdeye.so/defi/token_trending"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data", {}).get("tokens"):
                    return data["data"]["tokens"]
            return None
    except Exception as e:
        logger.error(f"Birdeye trending error: {e}")
        return None


def fetch_overview(address):
    """Fetch token overview from Birdeye."""
    import httpx
    url = f"https://public-api.birdeye.so/defi/token_overview?address={address}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, headers=HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    return data["data"]
            return None
    except:
        return None


def calc_safety_score(overview):
    """Calculate safety score 0-100."""
    if not overview:
        return 0, "No data"

    score = 0
    details = []

    holder = overview.get("holder", 0) or 0
    if holder >= 1000:
        score += 25
    elif holder >= MIN_HOLDERS:
        score += 15
    details.append(f"H:{holder:,}")

    wallets = overview.get("uniqueWallet24h", 0) or 0
    if wallets >= 500:
        score += 20
    elif wallets >= MIN_UNIQUE_WALLETS_24H:
        score += 10
    details.append(f"W:{wallets:,}")

    buy = overview.get("buy24h", 0) or 0
    sell = overview.get("sell24h", 0) or 0
    if buy > 0 and sell > 0:
        ratio = buy / sell
        if ratio >= 1.0:
            score += 20
        elif ratio >= MIN_BUY_SELL_RATIO:
            score += 10
        details.append(f"B/S:{ratio:.2f}")
    else:
        details.append("B/S:N/A")

    ext = overview.get("extensions", {}) or {}
    tw = bool(ext.get("twitter"))
    web = bool(ext.get("website"))
    if tw and web:
        score += 20
        details.append("TW+WEB")
    elif tw:
        score += 15
        details.append("TW")
    elif web:
        score += 10
        details.append("WEB")
    else:
        details.append("NOSOCIAL")

    total = overview.get("totalSupply", 0) or 0
    circ = overview.get("circulatingSupply", 0) or 0
    if total > 0 and circ > 0:
        pct = (circ / total) * 100
        if pct > 50:
            score += 15
        elif pct > 20:
            score += 10
        details.append(f"C:{pct:.0f}%")
    else:
        details.append("S:?")

    return score, " | ".join(details)


def run_screening():
    """Run complete screening pipeline. Returns formatted message."""
    logger.info("=" * 50)
    logger.info("MEMULAI SCREENING...")
    logger.info("=" * 50)

    # Step 1: Get trending
    tokens = fetch_trending()
    if not tokens:
        msg = "❌ *Screening Gagal*\n\nTidak bisa mengambil data dari Birdeye API."
        send_telegram(msg)
        return msg

    logger.info(f"Trending tokens: {len(tokens)}")

    # Step 2: Basic filter
    candidates = []
    for t in tokens:
        vol = t.get("volume24hUSD", 0) or 0
        liq = t.get("liquidity", 0) or 0
        mcap = t.get("marketcap", 0) or 0
        price = t.get("price", 0) or 0

        if vol < MIN_VOLUME or liq < MIN_LIQUIDITY or mcap > MAX_MARKET_CAP or price > MAX_PRICE:
            continue

        candidates.append({
            "address": t.get("address", ""),
            "symbol": t.get("symbol", "???"),
            "volume": vol,
            "liquidity": liq,
            "market_cap": mcap,
            "price": price,
        })

    logger.info(f"Lolos basic filter: {len(candidates)}")

    global last_screening_time, next_screening_time, screening_status

    if not candidates:
        last_screening_time = datetime.now(timezone.utc).isoformat()
        next_screening_time = time.time() + SCREENING_INTERVAL
        screening_status = "Tidak ada koin lolos filter dasar."

        msg = "⚠️ *Screening: Tidak ada pool* 😕\n\nTidak ada koin yang lolos filter dasar."
        send_telegram(msg)
        return msg

    # Step 3: Safety check
    passed = []
    for c in candidates:
        ov = fetch_overview(c["address"])
        if not ov:
            continue

        holder = ov.get("holder", 0) or 0
        wallets = ov.get("uniqueWallet24h", 0) or 0
        buy = ov.get("buy24h", 0) or 0
        sell = ov.get("sell24h", 0) or 0
        ext = ov.get("extensions", {}) or {}
        has_social = bool(ext.get("twitter")) or bool(ext.get("website"))

        if holder < MIN_HOLDERS:
            continue
        if wallets < MIN_UNIQUE_WALLETS_24H:
            continue
        if buy > 0 and sell > 0 and (buy / sell) < MIN_BUY_SELL_RATIO:
            continue
        if REQUIRE_SOCIAL and not has_social:
            continue

        score, details = calc_safety_score(ov)
        c["score"] = score
        c["details"] = details
        c["holder"] = holder
        c["has_twitter"] = bool(ext.get("twitter"))
        c["has_website"] = bool(ext.get("website"))
        passed.append(c)

        time.sleep(0.3)

    logger.info(f"Lolos safety: {len(passed)}")

    if not passed:
        last_screening_time = datetime.now(timezone.utc).isoformat()
        next_screening_time = time.time() + SCREENING_INTERVAL
        screening_status = "Semua koin gagal safety check."

        msg = "⚠️ *Screening: Tidak ada pool aman* 🛡️\n\nSemua koin gagal safety check. Coba lagi 20 jam lagi."
        send_telegram(msg)
        return msg

    # Step 4: Rank top 4
    ranked = sorted(passed, key=lambda x: (x["score"], x["volume"]), reverse=True)
    top = ranked[:4]

    # ── Update shared state ──
    last_screening_time = datetime.now(timezone.utc).isoformat()
    next_screening_time = (
        datetime.now(timezone.utc).timestamp() + SCREENING_INTERVAL
    )
    screening_status = f"Screening selesai. {len(top)} pool ditemukan."

    # Build message
    lines = [
        "🚀 *HASIL SCREENING METEOURA*",
        f"📅 {datetime.now().strftime('%d %B %Y %H:%M')} WIB",
        "",
        f"📊 Dari {len(tokens)} token trending:",
        f"   ✅ Filter dasar: {len(candidates)}",
        f"   ✅ Safety check: {len(passed)}",
        f"   🏆 TOP {len(top)} POOL:",
        "",
    ]

    for i, p in enumerate(top, 1):
        social = ""
        if p.get("has_twitter"):
            social += "🐦"
        if p.get("has_website"):
            social += "🌐"
        if not social:
            social = "⚠️"

        lines.extend([
            f"  *{i}. {p['symbol']}* {social}",
            f"     Score: {p['score']}% | 👥 {p['holder']:,} holders",
            f"     💰 ${p['price']:.8f} | 📊 Vol: ${p['volume']:,.0f}",
            f"     🔗 `{p['address']}`",
            "",
        ])

    lines.extend([
        "💰 *Rekomendasi:*",
        f"   $10/pool x {len(top)} pool = ${10*len(top)}",
        "   TP: +35% | SL: -15% | Durasi: 20 jam",
        "",
        "🔄 Screening berikutnya: 20 jam lagi",
        "━━━━━━━━━━━━━━━━━━━",
    ])

    msg = "\n".join(lines)

    # Send to Telegram
    send_telegram(msg)
    logger.info("Screening selesai, hasil terkirim ke Telegram.")
    return msg


# ── HTTP Server for Railway Health Check ─────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for Railway health checks."""

    def do_GET(self):
        global last_screening_time, next_screening_time, screening_status

        if self.path == "/health":
            now_ts = time.time()
            if next_screening_time:
                hours_left = max(0, (next_screening_time - now_ts) / 3600)
            else:
                hours_left = "?"

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "service": "meteora-screener",
                "last_screening": last_screening_time or "never",
                "next_screening_in_hours": round(hours_left, 1) if isinstance(hours_left, float) else hours_left,
                "screening_status": screening_status,
                "interval_hours": SCREENING_INTERVAL // 3600,
            }, indent=2).encode())
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h1>Meteora DLMM Screener Service</h1>"
                b"<p>Running on Railway. Screening every 20 hours.</p>"
                b"<p><a href='/health'>/health</a></p>"
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(f"HTTP: {args[0]} {args[1]} {args[2]}")


def run_health_server(port):
    """Run health check HTTP server."""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server running on port {port}")
    server.serve_forever()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    """Main entry point."""
    logger.info("=" * 50)
    logger.info("METEOURA SCREENER SERVICE STARTING...")
    logger.info(f"Interval: {SCREENING_INTERVAL} detik ({SCREENING_INTERVAL//3600} jam)")
    logger.info("=" * 50)

    # Validate config
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN tidak diset!")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID tidak diset!")
        sys.exit(1)

    # Start health check server in background
    port = int(os.environ.get("PORT", "8080"))
    health_thread = threading.Thread(target=run_health_server, args=(port,), daemon=True)
    health_thread.start()
    logger.info(f"Health endpoint: http://0.0.0.0:{port}/health")

    # Send startup notification
    send_telegram(
        f"🟢 *Meteora Screener Service Started*\n\n"
        f"📅 {datetime.now().strftime('%d %B %Y %H:%M')} WIB\n"
        f"⏰ Screening otomatis setiap {SCREENING_INTERVAL//3600} jam\n"
        f"🔍 Filter: Volume>${MIN_VOLUME:,} | MCap<${MAX_MARKET_CAP:,} | Safety 6-layer"
    )

    # Main loop
    while True:
        try:
            run_screening()
            logger.info(f"Menunggu {SCREENING_INTERVAL} detik ({SCREENING_INTERVAL//3600} jam)...")
            time.sleep(SCREENING_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Service dihentikan.")
            send_telegram("🔴 *Meteora Screener Service Stopped*")
            break
        except Exception as e:
            logger.exception(f"Error screening: {e}")
            send_telegram(f"⚠️ *Error screening:* {str(e)}")
            logger.info("Coba lagi dalam 5 menit...")
            time.sleep(300)


if __name__ == "__main__":
    main()