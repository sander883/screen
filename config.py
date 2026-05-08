import os
from dotenv import load_dotenv

load_dotenv()


# ══════════════════════════════════════════════════════════════════════════
# PRESET MODES
# ══════════════════════════════════════════════════════════════════════════
# Pilih mode via env var SCREENING_MODE (default: fresh)
#
# FRESH    — fokus token baru launch, mcap kecil (<$150K), lebih toleran
# STANDARD — sweet spot scalp, mcap $20K-$500K (setting sebelumnya)
# SAFE     — hanya token lebih established, stricter filter
# ══════════════════════════════════════════════════════════════════════════

PRESETS = {
    "fresh": {
        # Target: token baru banget launch, bonding curve / post-graduation awal
        "MIN_HOLDER_COUNT":          10,
        "MAX_FRESH_WALLET_RATIO":    0.75,  # tolerate up to 75% fresh (wajar di launch)
        "FRESH_WALLET_TX_THRESHOLD": 5,
        "MAX_TOP_HOLDER_PCT":        12.0,
        "MAX_TOP10_COMBINED_PCT":    55.0,
        "MIN_LIQUIDITY_SOL":         3.0,   # bonding curve start sekitar 3-5 SOL
        "MIN_MARKET_CAP_USD":        3_000.0,    # $3K minimum
        "MAX_MARKET_CAP_USD":        150_000.0,  # $150K (fokus fresh)
        "MAX_PAIR_AGE_SECONDS":      1800,  # max 30 menit umur pair
        "MAX_PRIORITY_FEE_MICROLAMPORTS": 500_000,
        "MAX_MCAP_LIQ_RATIO":       2.5,   # mcap/liq max 2.5x (fresh lebih toleran)
        "MAX_BUNDLE_SAME_SLOT":      3,     # max 3 top holder beli di slot yg sama
    },
    "standard": {
        # Sweet spot scalp — setting default sebelumnya
        "MIN_HOLDER_COUNT":          20,
        "MAX_FRESH_WALLET_RATIO":    0.6,
        "FRESH_WALLET_TX_THRESHOLD": 10,
        "MAX_TOP_HOLDER_PCT":        15.0,
        "MAX_TOP10_COMBINED_PCT":    50.0,
        "MIN_LIQUIDITY_SOL":         10.0,
        "MIN_MARKET_CAP_USD":        20_000.0,
        "MAX_MARKET_CAP_USD":        500_000.0,
        "MAX_PAIR_AGE_SECONDS":      7200,  # max 2 jam
        "MAX_PRIORITY_FEE_MICROLAMPORTS": 500_000,
        "MAX_MCAP_LIQ_RATIO":       2.0,   # mcap/liq max 2x
        "MAX_BUNDLE_SAME_SLOT":      3,
    },
    "safe": {
        # Stricter — token yang sudah lebih matang
        "MIN_HOLDER_COUNT":          100,
        "MAX_FRESH_WALLET_RATIO":    0.4,
        "FRESH_WALLET_TX_THRESHOLD": 20,
        "MAX_TOP_HOLDER_PCT":        10.0,
        "MAX_TOP10_COMBINED_PCT":    40.0,
        "MIN_LIQUIDITY_SOL":         30.0,
        "MIN_MARKET_CAP_USD":        50_000.0,
        "MAX_MARKET_CAP_USD":        1_000_000.0,
        "MAX_PAIR_AGE_SECONDS":      86400,  # max 24 jam
        "MAX_PRIORITY_FEE_MICROLAMPORTS": 500_000,
        "MAX_MCAP_LIQ_RATIO":       1.5,   # mcap/liq max 1.5x (stricter)
        "MAX_BUNDLE_SAME_SLOT":      2,
    },
}

_MODE = os.getenv("SCREENING_MODE", "fresh").lower()
if _MODE not in PRESETS:
    _MODE = "fresh"
_PRESET = PRESETS[_MODE]


def _env_float(key: str, default: float) -> float:
    """Env override with float type."""
    val = os.getenv(key)
    return float(val) if val else default


def _env_int(key: str, default: int) -> int:
    """Env override with int type."""
    val = os.getenv(key)
    return int(val) if val else default


class Config:
    # ── API Keys ───────────────────────────────────────────────────────────
    HELIUS_API_KEY: str = os.getenv("HELIUS_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Screening Mode ─────────────────────────────────────────────────────
    MODE: str = _MODE

    # ── Solana Programs ────────────────────────────────────────────────────
    PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    LP_BURN_ADDRESS = "1nc1nerator11111111111111111111111111111111"

    # ── Filter 0: Token Safety (auto-skip) ─────────────────────────────────
    # Mint authority HARUS revoked, freeze authority HARUS revoked

    # ── Filter 1: Network ──────────────────────────────────────────────────
    MAX_PRIORITY_FEE_MICROLAMPORTS: int = _env_int(
        "MAX_PRIORITY_FEE", _PRESET["MAX_PRIORITY_FEE_MICROLAMPORTS"]
    )

    # ── Filter 2: Wallet Analysis ──────────────────────────────────────────
    MIN_HOLDER_COUNT: int = _env_int(
        "MIN_HOLDER_COUNT", _PRESET["MIN_HOLDER_COUNT"]
    )
    MAX_FRESH_WALLET_RATIO: float = _env_float(
        "MAX_FRESH_WALLET_RATIO", _PRESET["MAX_FRESH_WALLET_RATIO"]
    )
    MIN_AVG_BUYER_WALLET_AGE_DAYS: float = 3.0
    FRESH_WALLET_TX_THRESHOLD: int = _env_int(
        "FRESH_WALLET_TX_THRESHOLD", _PRESET["FRESH_WALLET_TX_THRESHOLD"]
    )

    # ── Filter 3: Holder Quality ───────────────────────────────────────────
    MAX_TOP_HOLDER_PCT: float = _env_float(
        "MAX_TOP_HOLDER_PCT", _PRESET["MAX_TOP_HOLDER_PCT"]
    )
    MAX_TOP10_COMBINED_PCT: float = _env_float(
        "MAX_TOP10_COMBINED_PCT", _PRESET["MAX_TOP10_COMBINED_PCT"]
    )

    # ── Filter 4: Entry Quality ────────────────────────────────────────────
    MIN_LIQUIDITY_SOL: float = _env_float(
        "MIN_LIQUIDITY_SOL", _PRESET["MIN_LIQUIDITY_SOL"]
    )
    MIN_MARKET_CAP_USD: float = _env_float(
        "MIN_MARKET_CAP_USD", _PRESET["MIN_MARKET_CAP_USD"]
    )
    MAX_MARKET_CAP_USD: float = _env_float(
        "MAX_MARKET_CAP_USD", _PRESET["MAX_MARKET_CAP_USD"]
    )
    MAX_PAIR_AGE_SECONDS: int = _env_int(
        "MAX_PAIR_AGE_SECONDS", _PRESET["MAX_PAIR_AGE_SECONDS"]
    )
    MAX_MCAP_LIQ_RATIO: float = _env_float(
        "MAX_MCAP_LIQ_RATIO", _PRESET["MAX_MCAP_LIQ_RATIO"]
    )

    # ── Filter 5: Bundle Detection ─────────────────────────────────────────
    MAX_BUNDLE_SAME_SLOT: int = _env_int(
        "MAX_BUNDLE_SAME_SLOT", _PRESET["MAX_BUNDLE_SAME_SLOT"]
    )

    # ── RPC / API URLs ─────────────────────────────────────────────────────
    @classmethod
    def rpc_url(cls) -> str:
        if cls.HELIUS_API_KEY:
            return f"https://mainnet.helius-rpc.com/?api-key={cls.HELIUS_API_KEY}"
        return "https://api.mainnet-beta.solana.com"

    @classmethod
    def ws_url(cls) -> str:
        if cls.HELIUS_API_KEY:
            return f"wss://mainnet.helius-rpc.com/?api-key={cls.HELIUS_API_KEY}"
        return "wss://api.mainnet-beta.solana.com"

    @classmethod
    def helius_api_url(cls) -> str:
        return "https://api.helius.xyz/v0"
