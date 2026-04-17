"""
Filter 0: Token Safety Check
─────────────────────────────
Cek mint authority dan freeze authority.
Ini adalah HARD filter — jika flag, token di-skip otomatis tanpa cek filter lain.

Mint authority aktif  = dev bisa cetak supply baru kapan saja → dump
Freeze authority aktif = dev bisa freeze wallet holder → gabisa jual
"""

import logging
from solana_client import SolanaClient

logger = logging.getLogger(__name__)


async def check(mint: str, client: SolanaClient) -> tuple[bool, dict]:
    """
    Returns (flag, details).
    flag=True → AUTO SKIP, token tidak aman.
    """
    try:
        info = await client.get_mint_info(mint)
        if not info:
            # Gagal fetch = asumsikan tidak aman
            return True, {"error": "Cannot fetch mint account", "auto_skip": True}

        mint_revoked   = info.get("mint_authority_revoked", False)
        freeze_revoked = info.get("freeze_authority_revoked", False)

        flag = not mint_revoked or not freeze_revoked
        reasons = []
        if not mint_revoked:
            reasons.append("mint authority masih aktif")
        if not freeze_revoked:
            reasons.append("freeze authority masih aktif")

        return flag, {
            "mint_authority_revoked": mint_revoked,
            "freeze_authority_revoked": freeze_revoked,
            "supply": info.get("supply", 0),
            "decimals": info.get("decimals", 0),
            "reasons": reasons,
            "auto_skip": flag,
        }
    except Exception as e:
        logger.warning(f"token_safety check failed for {mint}: {e}")
        return True, {"error": str(e), "auto_skip": True}
