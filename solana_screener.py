"""
Solana Screener — Orchestrator
────────────────────────────────
Jalankan 5 filter secara terstruktur untuk setiap new pair yang terdeteksi.

Urutan:
  Filter 0 (token_safety) → AUTO SKIP jika flag, jangan proses lebih lanjut
  Filter 1-4 dijalankan paralel → hitung total flags
  0-1 flag → GAS IT | 2+ flag → SKIP

Kirim hasil ke Telegram jika GAS IT (atau semua hasil jika mau monitor penuh).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from config import Config
from solana_client import SolanaClient
from filters import token_safety, network, wallet_analysis, holder_quality, entry_quality
from notifier.telegram import send_alert

logger = logging.getLogger(__name__)


@dataclass
class SolanaTokenResult:
    """Hasil lengkap screening satu token."""
    mint: str
    symbol: str = "UNKNOWN"
    name: str = "Unknown Token"
    pair_age_seconds: float = 0.0
    screened_at: float = field(default_factory=time.time)

    # Filter 0 — Token Safety (auto-skip)
    safety_flag: bool = False
    mint_authority_revoked: bool = False
    freeze_authority_revoked: bool = False
    safety_details: dict = field(default_factory=dict)

    # Filter 1 — Network
    network_flag: bool = False
    priority_fee_microlamports: int = 0
    network_details: dict = field(default_factory=dict)

    # Filter 2 — Wallet Analysis
    wallet_flag: bool = False
    holder_count: int = 0
    fresh_wallet_count: int = 0
    fresh_wallet_ratio: float = 0.0
    wallet_details: dict = field(default_factory=dict)

    # Filter 3 — Holder Quality
    holder_flag: bool = False
    top1_holder_pct: float = 0.0
    top10_combined_pct: float = 0.0
    lp_burned: bool = False
    holder_details: dict = field(default_factory=dict)

    # Filter 4 — Entry Quality
    entry_flag: bool = False
    market_cap_usd: float = 0.0
    liquidity_usd: float = 0.0
    liquidity_sol_est: float = 0.0
    volume_5m_usd: float = 0.0
    mcap_tier: str = ""
    dex_pair_url: str = ""
    entry_details: dict = field(default_factory=dict)

    # Agregasi
    flag_reasons: list[str] = field(default_factory=list)

    @property
    def total_flags(self) -> int:
        return sum([self.network_flag, self.wallet_flag, self.holder_flag, self.entry_flag])

    @property
    def decision(self) -> str:
        if self.safety_flag:
            return "SKIP"
        return "SKIP" if self.total_flags >= 2 else "GAS IT"

    @property
    def is_gas_it(self) -> bool:
        return self.decision == "GAS IT"

    def summary(self) -> str:
        """Human-readable summary untuk logging."""
        lines = [
            f"{'='*52}",
            f"  {self.decision} — ${self.symbol} ({self.name[:20]})",
            f"{'='*52}",
            f"  CA: {self.mint}",
            f"  MCap: ${self.market_cap_usd:,.0f} | Liq: {self.liquidity_sol_est:.1f} SOL",
            f"  Holders: {self.holder_count} | Fresh: {self.fresh_wallet_count} ({self.fresh_wallet_ratio:.0%})",
            f"  Top1: {self.top1_holder_pct:.1f}% | Top10: {self.top10_combined_pct:.1f}%",
            f"  Mint revoked: {self.mint_authority_revoked} | Freeze revoked: {self.freeze_authority_revoked}",
            f"  Priority fee: {self.priority_fee_microlamports:,} microlamports",
            f"  LP Burned: {self.lp_burned}",
            f"  Flags: {self.total_flags}/4  →  {self.decision}",
        ]
        if self.flag_reasons:
            lines.append("  Reasons:")
            for r in self.flag_reasons:
                lines.append(f"    • {r}")
        return "\n".join(lines)


class SolanaScreener:
    """
    Orchestrator utama.
    Semua filter dijalankan dan hasilnya dikumpulkan ke SolanaTokenResult.
    """

    def __init__(self, config: type[Config] = Config):
        self.config = config
        self.client = SolanaClient(
            rpc_url=config.rpc_url(),
            helius_api_key=config.HELIUS_API_KEY,
        )

    async def close(self):
        await self.client.close()

    async def screen(self, mint: str) -> SolanaTokenResult:
        """
        Jalankan full screening pipeline untuk sebuah token mint address.
        """
        result = SolanaTokenResult(mint=mint)
        start = time.time()
        logger.info(f"Screening: {mint}")

        # ── Filter 0: Token Safety (blocking check) ─────────────────────
        safety_flag, safety_det = await token_safety.check(mint, self.client)
        result.safety_flag = safety_flag
        result.mint_authority_revoked = safety_det.get("mint_authority_revoked", False)
        result.freeze_authority_revoked = safety_det.get("freeze_authority_revoked", False)
        result.safety_details = safety_det

        if safety_flag:
            reasons = safety_det.get("reasons", [])
            result.flag_reasons.extend(reasons or ["Token safety check failed"])
            logger.info(f"AUTO SKIP (safety): {mint} — {reasons}")
            return result

        # ── Fetch DexScreener data sekali, share ke filter lain ─────────
        dex_data = await self.client.get_dexscreener_data_retry(mint, retries=3, delay=8.0)
        _enrich_metadata(result, dex_data)

        # ── Filter 1-4: Jalankan paralel ─────────────────────────────────
        net_task    = network.check(self.client, self.config)
        wallet_task = wallet_analysis.check(mint, self.client, self.config)
        holder_task = holder_quality.check(mint, self.client, self.config)
        entry_task  = entry_quality.check(mint, self.client, self.config, dex_data)

        (
            (net_flag, net_det),
            (wallet_flag, wallet_det),
            (holder_flag, holder_det),
            (entry_flag, entry_det),
        ) = await asyncio.gather(net_task, wallet_task, holder_task, entry_task)

        # ── Populate result ───────────────────────────────────────────────
        result.network_flag                = net_flag
        result.priority_fee_microlamports  = net_det.get("priority_fee_microlamports", 0)
        result.network_details             = net_det

        result.wallet_flag        = wallet_flag
        result.holder_count       = wallet_det.get("holder_count", 0)
        result.fresh_wallet_count = wallet_det.get("fresh_wallet_count", 0)
        result.fresh_wallet_ratio = wallet_det.get("fresh_wallet_ratio", 0.0)
        result.wallet_details     = wallet_det

        result.holder_flag         = holder_flag
        result.top1_holder_pct     = holder_det.get("top1_holder_pct", 0.0)
        result.top10_combined_pct  = holder_det.get("top10_combined_pct", 0.0)
        result.lp_burned           = holder_det.get("lp_burned", False)
        result.holder_details      = holder_det

        result.entry_flag        = entry_flag
        result.market_cap_usd    = entry_det.get("market_cap_usd", 0.0)
        result.liquidity_usd     = entry_det.get("liquidity_usd", 0.0)
        result.liquidity_sol_est = entry_det.get("liquidity_sol_est", 0.0)
        result.volume_5m_usd     = entry_det.get("volume_5m_usd", 0.0)
        result.mcap_tier         = entry_det.get("mcap_tier", "")
        result.dex_pair_url      = entry_det.get("dex_pair_url", "")
        result.entry_details     = entry_det

        # Kumpulkan semua reasons
        for det in [net_det, wallet_det, holder_det, entry_det]:
            result.flag_reasons.extend(det.get("reasons", []))

        elapsed = time.time() - start
        logger.info(f"Screened in {elapsed:.1f}s | {result.decision} ({result.total_flags}/4 flags) | {mint}")

        return result

    async def screen_and_notify(self, mint: str) -> SolanaTokenResult:
        """
        Screen token dan kirim Telegram alert jika GAS IT.
        """
        result = await self.screen(mint)
        print(result.summary())

        if result.is_gas_it:
            sent = await send_alert(
                result,
                self.config.TELEGRAM_BOT_TOKEN,
                self.config.TELEGRAM_CHAT_ID,
            )
            if sent:
                logger.info(f"Telegram alert sent: {result.mint}")

        return result


def _enrich_metadata(result: SolanaTokenResult, dex_data: dict):
    """Isi nama dan simbol dari DexScreener data."""
    if not dex_data:
        return
    base = dex_data.get("baseToken") or {}
    result.symbol = base.get("symbol", "UNKNOWN")
    result.name   = base.get("name", "Unknown Token")
    # Pair age dari pairCreatedAt (unix ms)
    created_at = dex_data.get("pairCreatedAt")
    if created_at:
        result.pair_age_seconds = time.time() - (created_at / 1000)
