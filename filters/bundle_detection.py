"""
Filter 5: Bundle Detection
────────────────────────────
Deteksi apakah top holders membeli token di slot/block yang sama (coordinated buy).

Jito bundles memungkinkan banyak transaksi dieksekusi di slot yang sama.
Kalau banyak top holder account dibuat di slot yang sama → cabal / insider.

Cara kerja:
1. Ambil top N token accounts dari holders_data (sudah ada, shared)
2. Untuk tiap token account, fetch signature terbaru (limit=1) → dapat slot
3. Hitung berapa account yang share slot yang sama
4. Kalau > threshold → flag sebagai bundled

Cost: N RPC calls (parallelized), N = 5 default
"""

import asyncio
import logging
from collections import Counter

from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)

MAX_ACCOUNTS_TO_CHECK = 5


async def check(
    mint: str,
    client: SolanaClient,
    config: type[Config] = Config,
    holders_data: list | None = None,
) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → terdeteksi bundle (banyak holder beli di slot yang sama).
    """
    try:
        if holders_data is None:
            holders_data = await client.get_token_largest_accounts(mint)

        if not holders_data:
            return False, {"error": "No holder data", "reasons": []}

        token_accounts = [
            h.get("address") for h in holders_data[:MAX_ACCOUNTS_TO_CHECK]
            if h.get("address")
        ]

        if len(token_accounts) < 2:
            return False, {"checked": len(token_accounts), "reasons": []}

        slots = await asyncio.gather(
            *[_get_account_first_slot(client, addr) for addr in token_accounts]
        )

        valid_slots = [s for s in slots if s is not None]
        if len(valid_slots) < 2:
            return False, {"checked": len(token_accounts), "resolved_slots": len(valid_slots), "reasons": []}

        slot_counts = Counter(valid_slots)
        max_same_slot = max(slot_counts.values())
        most_common_slot = slot_counts.most_common(1)[0][0]

        threshold = getattr(config, "MAX_BUNDLE_SAME_SLOT", 3)
        flag = max_same_slot >= threshold

        reasons = []
        if flag:
            reasons.append(
                f"Bundle detected: {max_same_slot}/{len(valid_slots)} top holders "
                f"membeli di slot yang sama ({most_common_slot}) — coordinated buy"
            )

        return flag, {
            "checked": len(token_accounts),
            "resolved_slots": len(valid_slots),
            "max_same_slot": max_same_slot,
            "most_common_slot": most_common_slot if flag else None,
            "slot_distribution": dict(slot_counts),
            "bundle_detected": flag,
            "reasons": reasons,
        }

    except Exception as e:
        logger.warning(f"bundle_detection check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}


async def _get_account_first_slot(client: SolanaClient, token_account: str) -> int | None:
    """Get the slot of the most recent tx for a token account."""
    sigs = await client.get_signatures_for_address(token_account, limit=1)
    if sigs:
        return sigs[0].get("slot")
    return None
