"""
Filter 2: Wallet Analysis
──────────────────────────
Ini filter PALING PENTING untuk kualitas screening.
Mendeteksi bundled buy dan pola wallet koordinasi (cabal).

Bundled buy: banyak wallet baru membeli di transaksi awal secara koordinasi.
Signal rug pull paling kuat di Solana meme coins.

Logika:
1. Ambil 10 transaksi pertama token mint
2. Ekstrak unique buyer wallets
3. Untuk tiap buyer, cek berapa total tx riwayat wallet
4. Jika banyak buyer adalah "fresh wallets" (< 10 tx total) → bundle flag
5. Jika ratio fresh wallets terlalu tinggi → flag
"""

import asyncio
import logging
import time
from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)

# Waktu "dianggap fresh" dalam hari
_FRESH_WALLET_DAYS = 7
_SOL_GENESIS_TIMESTAMP = 1584332400  # March 2020 approx


async def check(
    mint: str,
    client: SolanaClient,
    config: type[Config] = Config,
) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → pola wallet mencurigakan (potensi bundle/rug).
    """
    try:
        # Ambil transaksi awal token ini
        sigs = await client.get_signatures_for_address(mint, limit=20)
        if not sigs:
            return True, {"error": "No transactions found", "holder_count": 0}

        # Ekstrak buyer wallets dari transaksi
        buyers = await _extract_buyers(mint, sigs[:10], client)
        holder_count = len(buyers)

        if holder_count < config.MIN_HOLDER_COUNT:
            return True, {
                "holder_count": holder_count,
                "min_required": config.MIN_HOLDER_COUNT,
                "reason": "Too few holders",
            }

        # Cek berapa wallet yang "fresh" (baru dibuat)
        wallet_ages = await _check_wallet_ages(buyers[:15], client)
        fresh_count = sum(1 for age in wallet_ages if age < config.FRESH_WALLET_TX_THRESHOLD)
        fresh_ratio = fresh_count / len(wallet_ages) if wallet_ages else 0

        # Hitung rata-rata age (dalam jumlah tx sebagai proxy)
        avg_tx_count = sum(wallet_ages) / len(wallet_ages) if wallet_ages else 0

        flag = fresh_ratio > config.MAX_FRESH_WALLET_RATIO
        reasons = []
        if flag:
            reasons.append(
                f"{fresh_count}/{len(wallet_ages)} buyer wallets fresh "
                f"(ratio {fresh_ratio:.0%} > max {config.MAX_FRESH_WALLET_RATIO:.0%})"
            )

        return flag, {
            "holder_count": holder_count,
            "wallets_checked": len(wallet_ages),
            "fresh_wallet_count": fresh_count,
            "fresh_wallet_ratio": round(fresh_ratio, 3),
            "avg_wallet_tx_count": round(avg_tx_count, 1),
            "bundle_suspected": flag,
            "reasons": reasons,
        }

    except Exception as e:
        logger.warning(f"wallet_analysis check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}


async def _extract_buyers(mint: str, sigs: list[dict], client: SolanaClient) -> list[str]:
    """
    Ambil unique wallet addresses yang berinteraksi dengan token di awal.
    Simplified: ambil dari fee payers transaksi awal.
    """
    buyers = set()
    tasks = [client.get_transaction(s["signature"]) for s in sigs[:8]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception) or not result:
            continue
        try:
            account_keys = (
                result.get("transaction", {})
                .get("message", {})
                .get("accountKeys", [])
            )
            # Fee payer (index 0) adalah buyer/interactor utama
            if account_keys:
                buyers.add(account_keys[0])
        except Exception:
            continue

    return list(buyers)


async def _check_wallet_ages(wallets: list[str], client: SolanaClient) -> list[int]:
    """
    Proxy untuk wallet age: jumlah transaksi total wallet.
    Wallet fresh = sedikit tx.
    """
    tasks = [client.get_signatures_for_address(w, limit=20) for w in wallets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ages = []
    for result in results:
        if isinstance(result, Exception) or result is None:
            ages.append(100)  # unknown = assume ok
            continue
        ages.append(len(result))

    return ages
