import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── API Keys (dari .env) ───────────────────────────────────────────────
    HELIUS_API_KEY: str = os.getenv("HELIUS_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Solana Programs ────────────────────────────────────────────────────
    PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    LP_BURN_ADDRESS = "1nc1nerator11111111111111111111111111111111"

    # ── Filter 0: Token Safety (auto-skip jika flag) ───────────────────────
    # Mint authority HARUS revoked, freeze authority HARUS revoked

    # ── Filter 1: Network ─────────────────────────────────────────────────
    # Jika priority fee di atas threshold = jaringan congested
    MAX_PRIORITY_FEE_MICROLAMPORTS: int = 500_000  # ~0.0005 SOL per tx

    # ── Filter 2: Wallet Analysis ──────────────────────────────────────────
    MIN_HOLDER_COUNT: int = 20
    MAX_FRESH_WALLET_RATIO: float = 0.6   # max 60% buyer adalah wallet baru
    MIN_AVG_BUYER_WALLET_AGE_DAYS: float = 3.0
    FRESH_WALLET_TX_THRESHOLD: int = 10   # wallet dengan <10 total tx = "fresh"

    # ── Filter 3: Holder Quality ───────────────────────────────────────────
    MAX_TOP_HOLDER_PCT: float = 15.0      # max % untuk 1 wallet terbesar
    MAX_TOP10_COMBINED_PCT: float = 50.0  # max % gabungan top 10 wallet

    # ── Filter 4: Entry Quality ────────────────────────────────────────────
    MIN_LIQUIDITY_SOL: float = 10.0       # minimum SOL di liquidity pool
    MIN_MARKET_CAP_USD: float = 20_000.0  # $20K
    MAX_MARKET_CAP_USD: float = 500_000.0 # $500K (sweet spot scalp)

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
