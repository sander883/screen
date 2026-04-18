"""
Filter 2: Wallet Analysis
──────────────────────────
Menganalisis pola wallet dari actual token holders.

Optimasi:
- Terima holders_data dari screener (shared dengan holder_quality)
- Pakai cached wallet tx count (whales muncul di banyak token)
- Cek top 8 owner saja (bukan 15), sudah cukup akurat
"""

import asyncio
import logging
from solana_client import SolanaClient
from config import Config

logger = logging.getLogger(__name__)

MAX_OWNERS_TO_CHECK = 8


async def check(
    mint: str,
    client: SolanaClient,
    config: type[Config] = Config,
    holders_data: list | None = None,
) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → pola wallet mencurigakan (bundle/cabal).

    holders_data: pass data dari getTokenLargestAccounts (shared).
    """
    try:
        if holders_data is None:
            holders_data = await client.get_token_largest_accounts(mint)

        holder_count = len(holders_data)

        if holder_count < config.MIN_HOLDER_COUNT:
            return True, {
                "holder_count": holder_count,
                "min_required": config.MIN_HOLDER_COUNT,
                "reasons": [f"Hanya {holder_count} holders (min {config.MIN_HOLDER_COUNT})"],
            }

        top_token_accounts = [
            h.get("address") for h in holders_data[:MAX_OWNERS_TO_CHECK]
            if h.get("address")
        ]
        owners = await client.get_multiple_token_account_owners(top_token_accounts)
        unique_owners = list({o for o in owners if o})

        if not unique_owners:
            return True, {
                "error": "Cannot resolve token account owners",
                "holder_count": holder_count,
            }

        # Pakai cached wallet tx count
        tx_counts = await asyncio.gather(
            *[client.get_wallet_tx_count_cached(w) for w in unique_owners]
        )

        fresh_threshold = config.FRESH_WALLET_TX_THRESHOLD
        fresh_count = sum(1 for c in tx_counts if c < fresh_threshold)
        checked = len(tx_counts)
        fresh_ratio = fresh_count / checked if checked else 0.0
        avg_tx = sum(tx_counts) / checked if checked else 0.0

        flag = fresh_ratio > config.MAX_FRESH_WALLET_RATIO

        reasons = []
        if flag:
            reasons.append(
                f"{fresh_count}/{checked} top holder wallets fresh "
                f"(ratio {fresh_ratio:.0%} > max {config.MAX_FRESH_WALLET_RATIO:.0%})"
            )

        return flag, {
            "holder_count": holder_count,
            "unique_owners_checked": checked,
            "fresh_wallet_count": fresh_count,
            "fresh_wallet_ratio": round(fresh_ratio, 3),
            "avg_wallet_tx_count": round(avg_tx, 1),
            "bundle_suspected": flag,
            "reasons": reasons,
        }

    except Exception as e:
        logger.warning(f"wallet_analysis check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}
