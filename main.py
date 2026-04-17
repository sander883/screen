"""
Main Entry Point
─────────────────
Jalankan bot screening meme coin Solana secara real-time.

Mode:
  python main.py              → Live mode: listen new pairs dari pump.fun
  python main.py --scan CA    → Manual: screen satu token spesifik
  python main.py --test       → Test koneksi API (Helius + Telegram)

Setup:
  1. cp .env.example .env
  2. Isi HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID di .env
  3. pip install -r requirements.txt
  4. python main.py
"""

import asyncio
import argparse
import logging
import signal
import sys
import time

from config import Config
from solana_screener import SolanaScreener
from pair_detector import PairDetector

# ── Logging setup ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ── Modes ──────────────────────────────────────────────────────────────────

async def live_mode():
    """
    Mode utama: listen new pairs dari Pump.fun secara real-time.
    Setiap token baru yang create → jalankan 5 filter → Telegram alert jika GAS IT.
    """
    _check_config()

    screener = SolanaScreener(Config)
    detector = PairDetector(
        ws_url=Config.ws_url(),
        pumpfun_program=Config.PUMPFUN_PROGRAM,
        helius_api_key=Config.HELIUS_API_KEY,
        on_new_pair=screener.screen_and_notify,
        max_concurrent=3,
    )

    logger.info("=" * 55)
    logger.info("  Meme Coin Screener — LIVE MODE")
    logger.info(f"  Network: Solana Mainnet")
    logger.info(f"  Source : Pump.fun ({Config.PUMPFUN_PROGRAM[:20]}...)")
    logger.info(f"  Alert  : Telegram (chat_id={Config.TELEGRAM_CHAT_ID})")
    logger.info("=" * 55)
    logger.info("Listening untuk new pairs... (Ctrl+C untuk stop)")

    # Graceful shutdown via asyncio signal handlers
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows tidak support add_signal_handler
            signal.signal(sig, lambda *_: stop_event.set())

    detect_task = asyncio.create_task(detector.start())
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        await detector.stop()
        detect_task.cancel()
        try:
            await detect_task
        except asyncio.CancelledError:
            pass
        await screener.close()
        logger.info("Bot stopped.")


async def scan_mode(mint: str):
    """
    Mode manual: screen satu token spesifik.
    Berguna untuk backtest atau cek token yang kamu temukan manual.
    """
    _check_config(require_telegram=False)

    screener = SolanaScreener(Config)
    try:
        logger.info(f"Scanning: {mint}")
        result = await screener.screen_and_notify(mint)
        if not result.is_gas_it:
            print(f"\nDecision: {result.decision} ({result.total_flags}/4 flags)")
    finally:
        await screener.close()


async def test_mode():
    """
    Test koneksi ke Helius RPC dan Telegram Bot.
    Jalankan ini setelah setup .env untuk verifikasi konfigurasi.
    """
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    print("\n── Test Konfigurasi ────────────────────────────")

    # Test Helius
    print(f"\n[1] Helius API Key: {'SET' if Config.HELIUS_API_KEY else 'MISSING'}")
    if Config.HELIUS_API_KEY:
        url = Config.rpc_url()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []}
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=headers) as http:
                r = await http.post(url, json=payload)
                result = r.json().get("result", "unknown")
                status = "OK" if result == "ok" else f"Response: {result}"
                print(f"    Helius RPC: {status}")
        except Exception as e:
            print(f"    Helius RPC: FAILED — {e}")
    else:
        print("    Set HELIUS_API_KEY di .env untuk menggunakan Helius")
        print("    Fallback ke public RPC (lebih lambat, rate limited)")

    # Test DexScreener
    print("\n[2] DexScreener API (no key required)")
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as http:
            r = await http.get(
                "https://api.dexscreener.com/latest/dex/tokens/"
                "So11111111111111111111111111111111111111112"
            )
            if r.status_code == 200:
                data = r.json()
                pairs = data.get("pairs") or []
                print(f"    DexScreener: OK ({len(pairs)} pairs found for WSOL)")
            else:
                print(f"    DexScreener: {r.status_code}")
    except Exception as e:
        print(f"    DexScreener: FAILED — {e}")

    # Test Telegram
    print(f"\n[3] Telegram Bot Token: {'SET' if Config.TELEGRAM_BOT_TOKEN else 'MISSING'}")
    print(f"    Telegram Chat ID  : {'SET' if Config.TELEGRAM_CHAT_ID else 'MISSING'}")
    if Config.TELEGRAM_BOT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=headers) as http:
                r = await http.get(
                    f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/getMe"
                )
                if r.status_code == 200:
                    bot_name = r.json().get("result", {}).get("username", "?")
                    print(f"    Telegram Bot: OK (@{bot_name})")
                else:
                    print(f"    Telegram Bot: FAILED ({r.status_code})")
        except Exception as e:
            print(f"    Telegram Bot: FAILED — {e}")

    print("\n── Filter Thresholds ───────────────────────────")
    print(f"  Max priority fee : {Config.MAX_PRIORITY_FEE_MICROLAMPORTS:,} microlamports")
    print(f"  Min holder count : {Config.MIN_HOLDER_COUNT}")
    print(f"  Max fresh wallets: {Config.MAX_FRESH_WALLET_RATIO:.0%}")
    print(f"  Max top1 holder  : {Config.MAX_TOP_HOLDER_PCT}%")
    print(f"  Max top10 holder : {Config.MAX_TOP10_COMBINED_PCT}%")
    print(f"  MCap range       : ${Config.MIN_MARKET_CAP_USD:,.0f} - ${Config.MAX_MARKET_CAP_USD:,.0f}")
    print(f"  Min liquidity    : {Config.MIN_LIQUIDITY_SOL} SOL")
    print("────────────────────────────────────────────────\n")


# ── Helpers ────────────────────────────────────────────────────────────────

def _check_config(require_telegram: bool = True):
    """Validasi konfigurasi penting sebelum mulai."""
    errors = []
    if not Config.HELIUS_API_KEY:
        logger.warning("HELIUS_API_KEY tidak di-set. Menggunakan public RPC (rate limited).")
    if require_telegram:
        if not Config.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN belum di-set di .env")
        if not Config.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID belum di-set di .env")
    if errors:
        for e in errors:
            logger.error(e)
        logger.error("Copy .env.example ke .env dan isi API keys.")
        sys.exit(1)


# ── Entry Point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Meme Coin Screener — 5 Filter untuk New Pair Solana"
    )
    parser.add_argument(
        "--scan",
        metavar="MINT_ADDRESS",
        help="Screen satu token secara manual (masukkan mint address / CA)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test koneksi API dan tampilkan konfigurasi",
    )
    args = parser.parse_args()

    if args.test:
        asyncio.run(test_mode())
    elif args.scan:
        asyncio.run(scan_mode(args.scan))
    else:
        asyncio.run(live_mode())


if __name__ == "__main__":
    main()
