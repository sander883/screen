"""
Pump.fun Client — On-chain Bonding Curve Reader
─────────────────────────────────────────────────
Baca state bonding curve langsung dari Solana chain (tidak pakai frontend API).

Kenapa on-chain:
- Reliable: data langsung dari blockchain, tidak bisa di-block / rate-limited
- Real-time: update sync dengan transaksi yang sedang terjadi
- Tidak butuh API key tambahan (pakai Helius RPC yang sudah ada)
- Bisa handle token pre-graduation yang belum ada di DexScreener

BondingCurveAccount layout (8-byte discriminator + 5 u64 + bool + Pubkey):
  offset 0-7   : discriminator (u64)
  offset 8-15  : virtualTokenReserves (u64)
  offset 16-23 : virtualSolReserves (u64)
  offset 24-31 : realTokenReserves (u64)
  offset 32-39 : realSolReserves (u64)
  offset 40-47 : tokenTotalSupply (u64)
  offset 48    : complete (bool)
  offset 49-80 : creator (Pubkey, 32 bytes)

Market cap (SOL) = (tokenTotalSupply × virtualSolReserves) / virtualTokenReserves

PDA derivation:
  seeds = ["bonding-curve", mint_bytes]
  program_id = 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
"""

import base64
import logging
import struct
from dataclasses import dataclass

from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_PROGRAM_PUBKEY = Pubkey.from_string(PUMPFUN_PROGRAM_ID)
LAMPORTS_PER_SOL = 1_000_000_000

# Layout offsets
_VIRTUAL_TOKEN_OFF  = 8
_VIRTUAL_SOL_OFF    = 16
_REAL_TOKEN_OFF     = 24
_REAL_SOL_OFF       = 32
_TOTAL_SUPPLY_OFF   = 40
_COMPLETE_OFF       = 48


@dataclass
class BondingCurveState:
    """Parsed pump.fun bonding curve account."""
    mint: str
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool  # True = sudah graduate ke PumpSwap/Raydium

    def market_cap_sol(self) -> float:
        """Market cap dalam SOL."""
        if self.virtual_token_reserves == 0:
            return 0.0
        # (supply × virtual_sol) / virtual_token — semua dalam raw units (lamports, raw tokens)
        mcap_lamports = (self.token_total_supply * self.virtual_sol_reserves) // self.virtual_token_reserves
        return mcap_lamports / LAMPORTS_PER_SOL

    def market_cap_usd(self, sol_price_usd: float) -> float:
        """Market cap dalam USD."""
        return self.market_cap_sol() * sol_price_usd

    def price_per_token_sol(self) -> float:
        """Harga per 1 token dalam SOL."""
        if self.virtual_token_reserves == 0:
            return 0.0
        # price = virtual_sol / virtual_token
        return self.virtual_sol_reserves / self.virtual_token_reserves / LAMPORTS_PER_SOL

    def progress_pct(self) -> float:
        """
        Progress bonding curve (0-100%).
        Graduation terjadi saat real_token_reserves habis (atau mendekati 0).

        Initial real_token_reserves Pump.fun = 793,100,000 tokens (approximately)
        Semakin mendekati 0 = mendekati graduation.
        """
        INITIAL_REAL_TOKEN = 793_100_000 * 10**6  # 6 decimals
        if INITIAL_REAL_TOKEN == 0:
            return 0.0
        tokens_sold = INITIAL_REAL_TOKEN - self.real_token_reserves
        pct = (tokens_sold / INITIAL_REAL_TOKEN) * 100
        return max(0.0, min(100.0, pct))

    def liquidity_sol(self) -> float:
        """SOL yang ada di bonding curve (real liquidity)."""
        return self.real_sol_reserves / LAMPORTS_PER_SOL


def derive_bonding_curve_pda(mint: str) -> str:
    """
    Derive bonding curve PDA address untuk pump.fun.
    seeds = [b"bonding-curve", mint_pubkey_bytes]

    Pakai solders.Pubkey.find_program_address (Rust-backed, teruji).
    """
    mint_pubkey = Pubkey.from_string(mint)
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_pubkey)],
        _PROGRAM_PUBKEY,
    )
    return str(pda)


def parse_bonding_curve_account(mint: str, account_data_b64: str) -> BondingCurveState | None:
    """Parse base64-encoded bonding curve account data."""
    try:
        raw = base64.b64decode(account_data_b64)
        if len(raw) < _COMPLETE_OFF + 1:
            return None

        virtual_token = struct.unpack_from("<Q", raw, _VIRTUAL_TOKEN_OFF)[0]
        virtual_sol   = struct.unpack_from("<Q", raw, _VIRTUAL_SOL_OFF)[0]
        real_token    = struct.unpack_from("<Q", raw, _REAL_TOKEN_OFF)[0]
        real_sol      = struct.unpack_from("<Q", raw, _REAL_SOL_OFF)[0]
        total_supply  = struct.unpack_from("<Q", raw, _TOTAL_SUPPLY_OFF)[0]
        complete      = bool(raw[_COMPLETE_OFF])

        return BondingCurveState(
            mint=mint,
            virtual_token_reserves=virtual_token,
            virtual_sol_reserves=virtual_sol,
            real_token_reserves=real_token,
            real_sol_reserves=real_sol,
            token_total_supply=total_supply,
            complete=complete,
        )
    except Exception as e:
        logger.warning(f"Parse bonding curve failed for {mint}: {e}")
        return None


class PumpFunClient:
    """
    Client untuk query data Pump.fun on-chain.
    Butuh reference ke SolanaClient yang sudah ada (share RPC connection).
    """

    def __init__(self, solana_client):
        self.solana = solana_client

    async def get_bonding_curve(self, mint: str) -> BondingCurveState | None:
        """
        Ambil state bonding curve untuk sebuah token.
        Returns None jika:
          - Mint bukan pump.fun token
          - Token sudah graduate (account dihapus)
          - RPC error
        """
        try:
            pda = derive_bonding_curve_pda(mint)
        except Exception as e:
            logger.debug(f"PDA derivation failed for {mint}: {e}")
            return None

        # Fetch account data via RPC
        result = await self.solana._rpc(
            "getAccountInfo",
            [pda, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not result or not result.get("value"):
            return None

        data_list = result["value"].get("data", [])
        if not data_list:
            return None

        account_data_b64 = data_list[0] if isinstance(data_list, list) else data_list
        return parse_bonding_curve_account(mint, account_data_b64)

    async def get_market_data(self, mint: str, sol_price_usd: float) -> dict:
        """
        Convenience method: ambil data market format standard.
        Return format kompatibel dengan DexScreener (drop-in replacement).

        Returns empty dict jika tidak bisa parse bonding curve.
        """
        state = await self.get_bonding_curve(mint)
        if not state:
            return {}

        mcap_sol = state.market_cap_sol()
        mcap_usd = state.market_cap_usd(sol_price_usd)
        liq_sol = state.liquidity_sol()
        liq_usd = liq_sol * sol_price_usd
        price_sol = state.price_per_token_sol()
        price_usd = price_sol * sol_price_usd

        return {
            "source": "pumpfun_bonding_curve",
            "priceUsd": str(price_usd),
            "marketCap": mcap_usd,
            "liquidity": {"usd": liq_usd, "sol": liq_sol},
            "bonding_curve": {
                "progress_pct": state.progress_pct(),
                "complete": state.complete,
                "virtual_sol_reserves": state.virtual_sol_reserves,
                "virtual_token_reserves": state.virtual_token_reserves,
                "real_sol_reserves": state.real_sol_reserves,
                "real_token_reserves": state.real_token_reserves,
            },
            # Pump.fun sebelum graduate tidak expose volume, set 0
            "volume": {"m5": 0, "h1": 0},
            "baseToken": {"symbol": "UNKNOWN", "name": "Unknown"},
        }
