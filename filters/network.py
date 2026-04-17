"""
Filter 1: Network Condition Check
───────────────────────────────────
Cek priority fee jaringan Solana saat ini.
Fee tinggi = congested = biaya masuk mahal = profit scalp berkurang.

Solana normal: < 100K microlamports
Solana congested: > 500K microlamports (= ~0.0005 SOL overhead per tx)
"""

import logging
from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)


async def check(client: SolanaClient, config: type[Config] = Config) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → jaringan sedang congested, biaya tinggi.
    """
    try:
        fee = await client.get_priority_fee()
        threshold = config.MAX_PRIORITY_FEE_MICROLAMPORTS
        flag = fee > threshold

        return flag, {
            "priority_fee_microlamports": fee,
            "threshold": threshold,
            "sol_cost_estimate": fee / 1_000_000_000,  # rough SOL cost
            "network_status": "congested" if flag else "normal",
        }
    except Exception as e:
        logger.warning(f"network check failed: {e}")
        # Gagal fetch fee = asumsikan OK (jangan block karena ini)
        return False, {"error": str(e), "assumed": "normal"}
