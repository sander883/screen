"""
Filter 2: Wallet Analysis
──────────────────────────
Filter PALING PENTING untuk kualitas screening.
Menganalisis pola wallet dari actual token holders (bukan sekedar fee payers).

Logika:
1. Ambil top 15 token holders via getTokenLargestAccounts
2. Map token account → owner wallet address (batch RPC call)
3. Untuk setiap owner, cek jumlah tx history (proxy untuk wallet age)
4. Wallet dengan < N tx = "fresh wallet"
5. Jika ratio fresh wallets terlalu tinggi → bundle/cabal flag

Pendekatan ini jauh lebih akurat dari membaca fee payer karena:
- Fee payer di pump.fun create = dev, bukan buyer
- Token largest accounts = actual holders saat ini
- Bundle/cabal langsung ketahuan dari pattern usia wallet holder
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
    flag=True → pola wallet mencurigakan (bundle/cabal).
    """
    try:
        # 1. Ambil top token holders (token accounts)
        holders = await client.get_token_largest_accounts(mint)
        holder_count = len(holders)

        if holder_count < config.MIN_HOLDER_COUNT:
            return True, {
                "holder_count": holder_count,
                "min_required": config.MIN_HOLDER_COUNT,
                "reasons": [f"Hanya {holder_count} holders (min {config.MIN_HOLDER_COUNT})"],
            }

        # 2. Ambil owner addresses dari top 15 token accounts (batch)
        top_token_accounts = [h.get("address") for h in holders[:15] if h.get("address")]
        owners = await client.get_multiple_token_account_owners(top_token_accounts)
        unique_owners = list({o for o in owners if o})

        if not unique_owners:
            return True, {
                "error": "Cannot resolve token account owners",
                "holder_count": holder_count,
            }

        # 3. Untuk tiap owner, cek tx count (proxy wallet age)
        tx_counts = await _get_wallet_tx_counts(unique_owners, client)

        # 4. Hitung fresh wallets
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


async def _get_wallet_tx_counts(wallets: list[str], client: SolanaClient) -> list[int]:
    """
    Ambil tx count untuk tiap wallet (paralel).
    Tx count < threshold = wallet fresh (baru dibuat).
    """
    tasks = [client.get_signatures_for_address(w, limit=20) for w in wallets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    counts = []
    for r in results:
        if isinstance(r, Exception) or r is None:
            counts.append(100)  # unknown = assume not fresh
        else:
            counts.append(len(r))
    return counts
