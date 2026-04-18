"""
Filter 3: Holder Quality Check
────────────────────────────────
Cek konsentrasi kepemilikan dan status LP.

Optimasi:
- Terima holders_data dan supply dari screener (shared dengan wallet_analysis)
- LP burn check dihilangkan karena boros RPC dan tidak akurat
  (LP burn info lebih reliable dari DexScreener data)
"""

import logging
from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)


async def check(
    mint: str,
    client: SolanaClient,
    config: type[Config] = Config,
    holders_data: list | None = None,
    supply: int | None = None,
) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → kepemilikan terkonsentrasi.

    holders_data, supply: pass data yang sudah di-fetch (shared).
    """
    try:
        if holders_data is None:
            holders_data = await client.get_token_largest_accounts(mint)
        if supply is None:
            supply = await client.get_token_supply(mint)

        if not holders_data or not supply:
            return True, {"error": "Cannot fetch holder data", "reasons": []}

        supply_f = float(supply)
        holder_pcts = [float(h["amount"]) / supply_f * 100 for h in holders_data]

        top1_pct = holder_pcts[0] if holder_pcts else 0
        top10_pct = sum(holder_pcts[:10])

        reasons = []
        if top1_pct > config.MAX_TOP_HOLDER_PCT:
            reasons.append(f"Top holder: {top1_pct:.1f}% (max {config.MAX_TOP_HOLDER_PCT}%)")
        if top10_pct > config.MAX_TOP10_COMBINED_PCT:
            reasons.append(f"Top 10 combined: {top10_pct:.1f}% (max {config.MAX_TOP10_COMBINED_PCT}%)")

        flag = bool(reasons)

        return flag, {
            "total_holders": len(holders_data),
            "top1_holder_pct": round(top1_pct, 2),
            "top10_combined_pct": round(top10_pct, 2),
            "lp_burned": False,
            "concentration_flag": flag,
            "reasons": reasons,
        }

    except Exception as e:
        logger.warning(f"holder_quality check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}
