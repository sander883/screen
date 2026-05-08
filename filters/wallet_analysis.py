"""
Filter 2: Wallet Analysis
──────────────────────────
Menganalisis pola wallet dari actual token holders.

Checks:
- Fresh wallet ratio (tx count < threshold)
- Average wallet age (min 90 days / 3 bulan — dari defined.fi)
- Wallet age std deviation (min 90 days — campuran umur = organic)

Optimasi:
- Terima holders_data dari screener (shared dengan holder_quality)
- Wallet info (tx count + age) cached 10 menit
- Cek top 8 owner saja (bukan 15), sudah cukup akurat
- Age dan tx count dari 1 RPC call yang sama (shared cache)
"""

import asyncio
import logging
import math
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
    flag=True → pola wallet mencurigakan.

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

        # Fetch tx counts + wallet ages in parallel (shared cache, no extra RPC)
        info_results = await asyncio.gather(
            *[client._fetch_wallet_info(w) for w in unique_owners]
        )
        tx_counts = [r[0] for r in info_results]

        # Wallet ages in days
        wallet_ages = await asyncio.gather(
            *[client.get_wallet_age_days(w) for w in unique_owners]
        )
        valid_ages = [a for a in wallet_ages if a > 0]

        fresh_threshold = config.FRESH_WALLET_TX_THRESHOLD
        fresh_count = sum(1 for c in tx_counts if c < fresh_threshold)
        checked = len(tx_counts)
        fresh_ratio = fresh_count / checked if checked else 0.0
        avg_tx = sum(tx_counts) / checked if checked else 0.0

        # Wallet age stats
        avg_age_days = sum(valid_ages) / len(valid_ages) if valid_ages else 0.0
        age_std_days = _stddev(valid_ages) if len(valid_ages) >= 2 else 0.0

        min_avg_age = getattr(config, "MIN_AVG_WALLET_AGE_DAYS", 90.0)
        min_age_std = getattr(config, "MIN_WALLET_AGE_STD_DAYS", 90.0)

        reasons = []
        if fresh_ratio > config.MAX_FRESH_WALLET_RATIO:
            reasons.append(
                f"{fresh_count}/{checked} top holder wallets fresh "
                f"(ratio {fresh_ratio:.0%} > max {config.MAX_FRESH_WALLET_RATIO:.0%})"
            )
        if valid_ages and avg_age_days < min_avg_age:
            reasons.append(
                f"Avg wallet age {avg_age_days:.0f} hari "
                f"(min {min_avg_age:.0f} hari / {min_avg_age/30:.0f} bulan)"
            )
        if len(valid_ages) >= 2 and age_std_days < min_age_std:
            reasons.append(
                f"Wallet age std dev {age_std_days:.0f} hari "
                f"(min {min_age_std:.0f} hari) — umur wallet terlalu seragam"
            )

        flag = bool(reasons)

        return flag, {
            "holder_count": holder_count,
            "unique_owners_checked": checked,
            "fresh_wallet_count": fresh_count,
            "fresh_wallet_ratio": round(fresh_ratio, 3),
            "avg_wallet_tx_count": round(avg_tx, 1),
            "avg_wallet_age_days": round(avg_age_days, 1),
            "wallet_age_std_days": round(age_std_days, 1),
            "reasons": reasons,
        }

    except Exception as e:
        logger.warning(f"wallet_analysis check failed for {mint}: {e}")
        return False, {"error": str(e), "assumed": "ok"}


def _stddev(values: list[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)
