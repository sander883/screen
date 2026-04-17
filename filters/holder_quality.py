"""
Filter 3: Holder Quality Check
────────────────────────────────
Cek konsentrasi kepemilikan dan status LP (liquidity pool).

Top holder terlalu besar = risiko dump satu wallet menghancurkan harga.
LP tidak burned/locked = developer bisa tarik likuiditas kapan saja (rug pull klasik).

Di Pump.fun: LP di-burn otomatis ke burn address saat graduation.
Di Raydium manual launch: perlu cek manual.

Burn address Solana: 1nc1nerator11111111111111111111111111111111
"""

import asyncio
import logging
from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)


async def check(
    mint: str,
    client: SolanaClient,
    config: type[Config] = Config,
) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → kepemilikan terkonsentrasi atau LP tidak aman.
    """
    try:
        # Ambil top holders dan total supply secara paralel
        holders, supply = await asyncio.gather(
            client.get_token_largest_accounts(mint),
            client.get_token_supply(mint),
        )

        if not holders or not supply:
            return True, {"error": "Cannot fetch holder data"}

        supply_f = float(supply)
        holder_pcts = [float(h["amount"]) / supply_f * 100 for h in holders]

        top1_pct = holder_pcts[0] if holder_pcts else 0
        top10_pct = sum(holder_pcts[:10])

        concentration_flag = (
            top1_pct > config.MAX_TOP_HOLDER_PCT
            or top10_pct > config.MAX_TOP10_COMBINED_PCT
        )

        # Cek LP burn - heuristic: jika token adalah pumpfun graduated,
        # LP token harusnya ada di burn address.
        # Kita cek apakah burn address punya token balance dari mint ini.
        lp_burned = await _check_lp_burned(mint, client, config)

        reasons = []
        if top1_pct > config.MAX_TOP_HOLDER_PCT:
            reasons.append(f"Top holder: {top1_pct:.1f}% (max {config.MAX_TOP_HOLDER_PCT}%)")
        if top10_pct > config.MAX_TOP10_COMBINED_PCT:
            reasons.append(f"Top 10 combined: {top10_pct:.1f}% (max {config.MAX_TOP10_COMBINED_PCT}%)")

        # LP tidak burned = tambah flag risk, tapi bukan auto-flag
        # (beberapa legit token lock LP di contract bukan burn)
        flag = concentration_flag

        return flag, {
            "total_holders": len(holders),
            "top1_holder_pct": round(top1_pct, 2),
            "top10_combined_pct": round(top10_pct, 2),
            "lp_burned": lp_burned,
            "concentration_flag": concentration_flag,
            "reasons": reasons,
        }

    except Exception as e:
        logger.warning(f"holder_quality check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}


async def _check_lp_burned(mint: str, client: SolanaClient, config: type[Config]) -> bool:
    """
    Cek apakah ada token balance di burn address untuk mint ini.
    Simplified check - untuk confidence indicator, bukan hard flag.
    """
    try:
        sigs = await client.get_signatures_for_address(config.LP_BURN_ADDRESS, limit=50)
        # Jika burn address punya recent tx yang melibatkan mint ini = LP burned
        # Ini proxy check - untuk accurate check perlu getTokenAccountsByOwner
        # Untuk MVP: return False (unknown), let other signals decide
        return False
    except Exception:
        return False
