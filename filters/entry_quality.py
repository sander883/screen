"""
Filter 4: Entry Quality Check
───────────────────────────────
Cek market cap tier dan liquidity depth.

Market cap bukan satu-satunya ukuran.
LIQUIDITY lebih penting — mcap $100K dengan likuiditas 2 SOL = slippage gila.

Sweet spot untuk scalp di Solana:
  Market cap : $20K - $500K
  Liquidity  : minimal 10 SOL (~$1,500 USD equivalent)

Token yang baru launch dari Pump.fun masuk ke Raydium saat mcap ~$69K.
Ini fase transisi yang bagus untuk entry.
"""

import logging
from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)

# Estimasi SOL price untuk konversi liquidity (fallback static)
_SOL_PRICE_FALLBACK_USD = 150.0


async def check(
    mint: str,
    client: SolanaClient,
    config: type[Config] = Config,
    dex_data: dict | None = None,
) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → market cap di luar range atau likuiditas terlalu tipis.

    dex_data: pass data DexScreener yang sudah di-fetch sebelumnya
              (untuk hindari double fetch).
    """
    try:
        # Gunakan dex_data yang sudah ada, atau fetch baru
        if not dex_data:
            dex_data = await client.get_dexscreener_data_retry(mint, retries=3, delay=8.0)

        if not dex_data:
            # Tidak ada data market = token baru banget, terlalu risiko
            return True, {
                "error": "No market data yet",
                "reason": "Token mungkin belum terdaftar di DEX",
            }

        price_usd   = float(dex_data.get("priceUsd") or 0)
        market_cap  = float(dex_data.get("marketCap") or 0)
        liq_data    = dex_data.get("liquidity") or {}
        liq_usd     = float(liq_data.get("usd") or 0)
        liq_base    = float(liq_data.get("base") or 0)  # token amount in LP
        vol_5m      = float((dex_data.get("volume") or {}).get("m5") or 0)
        vol_1h      = float((dex_data.get("volume") or {}).get("h1") or 0)

        # Estimasi SOL liquidity dari USD
        liq_sol = liq_usd / _SOL_PRICE_FALLBACK_USD

        mcap_too_low  = market_cap < config.MIN_MARKET_CAP_USD
        mcap_too_high = market_cap > config.MAX_MARKET_CAP_USD
        liq_too_low   = liq_sol < config.MIN_LIQUIDITY_SOL

        flag = mcap_too_low or mcap_too_high or liq_too_low

        reasons = []
        if mcap_too_low:
            reasons.append(f"MCap terlalu rendah: ${market_cap:,.0f} (min ${config.MIN_MARKET_CAP_USD:,.0f})")
        if mcap_too_high:
            reasons.append(f"MCap terlalu tinggi: ${market_cap:,.0f} (max ${config.MAX_MARKET_CAP_USD:,.0f})")
        if liq_too_low:
            reasons.append(f"Liquidity tipis: {liq_sol:.1f} SOL (min {config.MIN_LIQUIDITY_SOL} SOL)")

        # Tentukan mcap tier untuk context
        tier = _get_mcap_tier(market_cap)

        return flag, {
            "price_usd": price_usd,
            "market_cap_usd": round(market_cap, 2),
            "liquidity_usd": round(liq_usd, 2),
            "liquidity_sol_est": round(liq_sol, 2),
            "volume_5m_usd": round(vol_5m, 2),
            "volume_1h_usd": round(vol_1h, 2),
            "mcap_tier": tier,
            "reasons": reasons,
            "dex_pair_url": dex_data.get("url", ""),
        }

    except Exception as e:
        logger.warning(f"entry_quality check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}


def _get_mcap_tier(mcap_usd: float) -> str:
    """Label tier market cap untuk konteks entry."""
    if mcap_usd < 30_000:
        return "micro (<$30K) — bonding curve zone"
    if mcap_usd < 69_000:
        return "pre-grad ($30K-$69K) — pump.fun phase"
    if mcap_usd < 200_000:
        return "sweet spot ($69K-$200K) — post-graduation"
    if mcap_usd < 500_000:
        return "mid ($200K-$500K) — still ok"
    if mcap_usd < 1_000_000:
        return "high ($500K-$1M) — late entry"
    return "very high (>$1M) — skip untuk scalp"
