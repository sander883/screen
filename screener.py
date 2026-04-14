"""
Meme Coin Screening Strategy
=============================
4 Filter Sebelum Scalp New Pair

Strategi filter bertingkat untuk menyaring koin meme baru yang layak di-scalp.
Setiap filter menghasilkan flag (True/False). Jika total flag >= 2, SKIP.
Jika flag <= 1, potensi scalp 60-70% di new pair fresh launch.
"""

from dataclasses import dataclass, field


@dataclass
class ScreeningResult:
    token_address: str
    gas_fee_flag: bool = False
    holder_wallet_flag: bool = False
    top_holder_flag: bool = False
    market_cap_flag: bool = False
    details: dict = field(default_factory=dict)

    @property
    def total_flags(self) -> int:
        return sum([
            self.gas_fee_flag,
            self.holder_wallet_flag,
            self.top_holder_flag,
            self.market_cap_flag,
        ])

    @property
    def decision(self) -> str:
        return "SKIP" if self.total_flags >= 2 else "GAS IT"

    def summary(self) -> str:
        lines = [
            f"Token: {self.token_address}",
            f"Step 1 - Gas Fee:        {'FLAG' if self.gas_fee_flag else 'OK'}",
            f"Step 2 - Holder/Wallet:  {'FLAG' if self.holder_wallet_flag else 'OK'}",
            f"Step 3 - Top Holder:     {'FLAG' if self.top_holder_flag else 'OK'}",
            f"Step 4 - Market Cap:     {'FLAG' if self.market_cap_flag else 'OK'}",
            f"Total Flags: {self.total_flags}/4",
            f"Decision: {self.decision}",
        ]
        if self.details:
            lines.append("Details:")
            for key, value in self.details.items():
                lines.append(f"  {key}: {value}")
        return "\n".join(lines)


@dataclass
class ScreeningConfig:
    # Step 1: Gas fee threshold (in gwei for EVM, lamports for Solana)
    max_gas_fee_gwei: float = 50.0

    # Step 2: Minimum holder count and funded wallet age (in days)
    min_holder_count: int = 100
    min_funded_wallet_age_days: int = 7

    # Step 3: Max percentage a single top holder can own (excluding LP/burn)
    max_top_holder_pct: float = 15.0
    # Max combined percentage of top 10 holders
    max_top10_combined_pct: float = 50.0

    # Step 4: Market cap range for entry (in USD)
    min_market_cap_usd: float = 10_000.0
    max_market_cap_usd: float = 1_000_000.0


class MemeScreener:
    """Screener utama yang menjalankan 4 filter bertingkat."""

    def __init__(self, config: ScreeningConfig | None = None):
        self.config = config or ScreeningConfig()

    def screen(self, token_data: dict) -> ScreeningResult:
        """
        Jalankan 4 filter screening pada token_data.

        token_data harus berisi:
            - address: str
            - gas_fee_gwei: float
            - holder_count: int
            - funded_wallet_age_days: float (usia rata-rata wallet yang mendanai)
            - top_holder_pct: float (persentase holder terbesar)
            - top10_combined_pct: float (kombinasi top 10 holder)
            - market_cap_usd: float
        """
        result = ScreeningResult(token_address=token_data["address"])

        self._check_gas_fee(token_data, result)
        self._check_holder_wallet(token_data, result)
        self._check_top_holder(token_data, result)
        self._check_market_cap(token_data, result)

        return result

    def _check_gas_fee(self, data: dict, result: ScreeningResult):
        """Step 1: Cek Global Gas Fee."""
        gas = data.get("gas_fee_gwei", 0)
        threshold = self.config.max_gas_fee_gwei
        result.gas_fee_flag = gas > threshold
        result.details["gas_fee_gwei"] = gas
        result.details["gas_fee_threshold"] = threshold

    def _check_holder_wallet(self, data: dict, result: ScreeningResult):
        """Step 2: Holder & Funded Wallet Age."""
        holder_count = data.get("holder_count", 0)
        wallet_age = data.get("funded_wallet_age_days", 0)

        low_holders = holder_count < self.config.min_holder_count
        young_wallets = wallet_age < self.config.min_funded_wallet_age_days

        result.holder_wallet_flag = low_holders or young_wallets
        result.details["holder_count"] = holder_count
        result.details["funded_wallet_age_days"] = wallet_age

    def _check_top_holder(self, data: dict, result: ScreeningResult):
        """Step 3: Cek Balance Top Holder."""
        top_pct = data.get("top_holder_pct", 0)
        top10_pct = data.get("top10_combined_pct", 0)

        concentrated = top_pct > self.config.max_top_holder_pct
        top10_heavy = top10_pct > self.config.max_top10_combined_pct

        result.top_holder_flag = concentrated or top10_heavy
        result.details["top_holder_pct"] = top_pct
        result.details["top10_combined_pct"] = top10_pct

    def _check_market_cap(self, data: dict, result: ScreeningResult):
        """Step 4: Cek Entry Market Cap."""
        mcap = data.get("market_cap_usd", 0)
        too_low = mcap < self.config.min_market_cap_usd
        too_high = mcap > self.config.max_market_cap_usd

        result.market_cap_flag = too_low or too_high
        result.details["market_cap_usd"] = mcap
        result.details["market_cap_range"] = (
            f"${self.config.min_market_cap_usd:,.0f} - ${self.config.max_market_cap_usd:,.0f}"
        )


def demo():
    """Demo screening beberapa token."""
    screener = MemeScreener()

    tokens = [
        {
            "address": "0xABC...111",
            "gas_fee_gwei": 25.0,
            "holder_count": 250,
            "funded_wallet_age_days": 30,
            "top_holder_pct": 8.0,
            "top10_combined_pct": 35.0,
            "market_cap_usd": 150_000,
        },
        {
            "address": "0xDEF...222",
            "gas_fee_gwei": 80.0,
            "holder_count": 50,
            "funded_wallet_age_days": 2,
            "top_holder_pct": 40.0,
            "top10_combined_pct": 75.0,
            "market_cap_usd": 5_000,
        },
        {
            "address": "0xGHI...333",
            "gas_fee_gwei": 30.0,
            "holder_count": 180,
            "funded_wallet_age_days": 15,
            "top_holder_pct": 20.0,
            "top10_combined_pct": 55.0,
            "market_cap_usd": 500_000,
        },
    ]

    for token in tokens:
        result = screener.screen(token)
        print("=" * 50)
        print(result.summary())
        print()


if __name__ == "__main__":
    demo()
