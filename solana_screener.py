"""
Solana Screener — Orchestrator
────────────────────────────────
Jalankan 5 filter secara terstruktur untuk setiap new pair yang terdeteksi.

Pipeline HEMAT RPC:
  1. Filter 0 (token_safety)  → 1 RPC call, AUTO SKIP jika flag
  2. Fetch DexScreener        → 0 RPC calls (gratis)
  3. Filter 4 (entry_quality) → 0 RPC calls (pakai data DexScreener)
     → EARLY EXIT jika mcap/pair age di luar range
  4. Filter 1 (network)       → 1 RPC call (cached 30s)
  5. Fetch token holders      → 2 RPC calls (shared antara filter 2 & 3)
  6. Filter 2 (wallet_analysis) → 8 RPC calls (top 8 owner wallet age)
  7. Filter 3 (holder_quality)  → 0 extra (reuse data step 5)

Total WORST case: ~12 calls per token (turun dari ~21)
Token yang di-skip awal: 1-2 calls saja

Kirim hasil ke Telegram jika GAS IT.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from config import Config
from solana_client import SolanaClient
from pumpfun_client import PumpFunClient
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
    rpc_calls_used: int = 0
    skipped_early: bool = False

    @property
    def total_flags(self) -> int:
        return sum([self.network_flag, self.wallet_flag, self.holder_flag, self.entry_flag])

    @property
    def decision(self) -> str:
        if self.safety_flag:
            return "SKIP"
        if self.entry_flag:
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
            f"  RPC calls: {self.rpc_calls_used}",
        ]
        if self.flag_reasons:
            lines.append("  Reasons:")
            for r in self.flag_reasons:
                lines.append(f"    • {r}")
        return "\n".join(lines)


class SolanaScreener:
    """
    Orchestrator utama dengan pipeline hemat RPC.
    Filter diurutkan dari murah ke mahal — early exit jika sudah jelas SKIP.
    """

    def __init__(self, config: type[Config] = Config):
        self.config = config
        self.client = SolanaClient(
            rpc_url=config.rpc_url(),
            helius_api_key=config.HELIUS_API_KEY,
        )
        self.pumpfun = PumpFunClient(self.client)
        self._stats = {"total": 0, "gas_it": 0, "skip": 0, "rpc_total": 0}

    @property
    def stats(self):
        return self._stats.copy()

    async def close(self):
        await self.client.close()

    async def screen(self, mint: str) -> SolanaTokenResult:
        """
        Pipeline screening hemat RPC:
        1. Safety check (1 call) → auto skip
        2. DexScreener (FREE) + entry quality → early exit
        3. Network + wallet + holder (parallel, only if needed)
        """
        result = SolanaTokenResult(mint=mint)
        start = time.time()
        rpc_before = self.client.rpc_call_count
        logger.info(f"Screening: {mint}")

        # ─── PHASE 1: Token Safety (1 RPC call) ──────────────────────
        safety_flag, safety_det = await token_safety.check(mint, self.client)
        result.safety_flag = safety_flag
        result.mint_authority_revoked = safety_det.get("mint_authority_revoked", False)
        result.freeze_authority_revoked = safety_det.get("freeze_authority_revoked", False)
        result.safety_details = safety_det

        if safety_flag:
            reasons = safety_det.get("reasons", [])
            result.flag_reasons.extend(reasons or ["Token safety check failed"])
            result.rpc_calls_used = self.client.rpc_call_count - rpc_before
            self._record(result)
            logger.info(f"AUTO SKIP (safety): {mint} [{result.rpc_calls_used} RPC calls]")
            return result

        # ─── PHASE 2: DexScreener + Entry Quality (0 RPC calls) ──────
        dex_data = await self.client.get_dexscreener_data_retry(mint, retries=3, delay=8.0)

        # Fallback ke Pump.fun on-chain bonding curve kalau DexScreener kosong
        # (token pre-graduation belum terindeks di DexScreener)
        if not dex_data:
            sol_price = await self.client.get_sol_price_usd()
            dex_data = await self.pumpfun.get_market_data(mint, sol_price)
            if dex_data:
                logger.info(f"Using pump.fun on-chain data for {mint}")

        _enrich_metadata(result, dex_data)

        entry_flag, entry_det = await entry_quality.check(
            mint, self.client, self.config, dex_data
        )
        result.entry_flag = entry_flag
        result.market_cap_usd = entry_det.get("market_cap_usd", 0.0)
        result.liquidity_usd = entry_det.get("liquidity_usd", 0.0)
        result.liquidity_sol_est = entry_det.get("liquidity_sol_est", 0.0)
        result.volume_5m_usd = entry_det.get("volume_5m_usd", 0.0)
        result.mcap_tier = entry_det.get("mcap_tier", "")
        result.dex_pair_url = entry_det.get("dex_pair_url", "")
        result.entry_details = entry_det
        if entry_det.get("reasons"):
            result.flag_reasons.extend(entry_det["reasons"])

        # Entry flag = hard skip (mcap/age/liquidity di luar range = bukan target scalp)
        if entry_flag:
            result.skipped_early = True
            result.rpc_calls_used = self.client.rpc_call_count - rpc_before
            self._record(result)
            logger.info(
                f"EARLY SKIP (entry): {mint} mcap=${result.market_cap_usd:,.0f} "
                f"[{result.rpc_calls_used} RPC calls saved]"
            )
            return result

        # ─── PHASE 3: Full Analysis (parallel, shared data) ──────────
        # Fetch holders SEKALI, share ke wallet_analysis & holder_quality
        holders_data = await self.client.get_token_largest_accounts(mint)
        supply = await self.client.get_token_supply(mint)

        # Run filter 1 + 2 + 3 in parallel
        tasks = [
            network.check(self.client, self.config),
            wallet_analysis.check(mint, self.client, self.config,
                                  holders_data=holders_data),
            holder_quality.check(mint, self.client, self.config,
                                 holders_data=holders_data, supply=supply),
        ]

        results = await asyncio.gather(*tasks)

        net_flag, net_det = results[0]
        result.network_flag = net_flag
        result.priority_fee_microlamports = net_det.get("priority_fee_microlamports", 0)
        result.network_details = net_det
        if net_det.get("reasons"):
            result.flag_reasons.extend(net_det["reasons"])

        wallet_flag, wallet_det = results[1]
        result.wallet_flag = wallet_flag
        result.holder_count = wallet_det.get("holder_count", 0)
        result.fresh_wallet_count = wallet_det.get("fresh_wallet_count", 0)
        result.fresh_wallet_ratio = wallet_det.get("fresh_wallet_ratio", 0.0)
        result.wallet_details = wallet_det
        if wallet_det.get("reasons"):
            result.flag_reasons.extend(wallet_det["reasons"])

        holder_flag, holder_det = results[2]
        result.holder_flag = holder_flag
        result.top1_holder_pct = holder_det.get("top1_holder_pct", 0.0)
        result.top10_combined_pct = holder_det.get("top10_combined_pct", 0.0)
        result.lp_burned = holder_det.get("lp_burned", False)
        result.holder_details = holder_det
        if holder_det.get("reasons"):
            result.flag_reasons.extend(holder_det["reasons"])

        # ─── Done ─────────────────────────────────────────────────────
        result.rpc_calls_used = self.client.rpc_call_count - rpc_before
        elapsed = time.time() - start
        self._record(result)
        logger.info(
            f"Screened in {elapsed:.1f}s | {result.decision} "
            f"({result.total_flags}/4 flags) | {mint} "
            f"[{result.rpc_calls_used} RPC calls]"
        )
        return result

    async def screen_and_notify(self, mint: str) -> SolanaTokenResult:
        """Screen token dan kirim Telegram alert jika GAS IT."""
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

    def _record(self, result: SolanaTokenResult):
        self._stats["total"] += 1
        self._stats["rpc_total"] += result.rpc_calls_used
        if result.is_gas_it:
            self._stats["gas_it"] += 1
        else:
            self._stats["skip"] += 1
        if self._stats["total"] % 50 == 0:
            logger.info(
                f"Stats: {self._stats['total']} screened, "
                f"{self._stats['gas_it']} GAS IT, "
                f"{self._stats['skip']} SKIP, "
                f"{self._stats['rpc_total']} total RPC calls "
                f"(avg {self._stats['rpc_total'] / self._stats['total']:.1f}/token)"
            )


def _enrich_metadata(result: SolanaTokenResult, dex_data: dict):
    """Isi nama dan simbol dari DexScreener data."""
    if not dex_data:
        return
    base = dex_data.get("baseToken") or {}
    result.symbol = base.get("symbol", "UNKNOWN")
    result.name = base.get("name", "Unknown Token")
    created_at = dex_data.get("pairCreatedAt")
    if created_at:
        result.pair_age_seconds = time.time() - (created_at / 1000)
