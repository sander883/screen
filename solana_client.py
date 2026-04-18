"""
Async client untuk Solana RPC (via Helius) dan DexScreener API.
Semua I/O non-blocking menggunakan httpx.AsyncClient.
"""

import asyncio
import base64
import logging
import struct
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# SPL Token MintLayout offsets (82 bytes total)
_MINT_AUTH_OPT_OFF = 0    # u32: 1=Some, 0=None
_MINT_AUTH_OFF     = 4    # Pubkey (32 bytes)
_SUPPLY_OFF        = 36   # u64
_DECIMALS_OFF      = 44   # u8
_FREEZE_AUTH_OPT   = 46   # u32: 1=Some, 0=None
_FREEZE_AUTH_OFF   = 50   # Pubkey (32 bytes)
_MINT_ACCOUNT_SIZE = 82

# Wrapped SOL mint (dipakai untuk fetch SOL/USD price)
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Header agar tidak diblokir Cloudflare
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


class SolanaClient:
    """Async client untuk Solana RPC dan API eksternal."""

    def __init__(self, rpc_url: str, helius_api_key: str = "", timeout: float = 10.0):
        self.rpc_url = rpc_url
        self.helius_api_key = helius_api_key
        self._http = httpx.AsyncClient(timeout=timeout, headers=_DEFAULT_HEADERS)
        self._sol_price_cache: tuple[float, float] | None = None  # (price, timestamp)
        self.rpc_call_count: int = 0
        self._priority_fee_cache: tuple[int, float] | None = None  # (fee, timestamp)
        self._wallet_age_cache: dict[str, tuple[int, float]] = {}  # addr → (tx_count, timestamp)

    async def close(self):
        await self._http.aclose()

    # ── Solana RPC ─────────────────────────────────────────────────────────

    async def _rpc(self, method: str, params: list) -> dict | list | None:
        self.rpc_call_count += 1
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = await self._http.post(self.rpc_url, json=payload)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                logger.warning(f"RPC {method} error: {data['error']}")
                return None
            return data.get("result")
        except Exception as e:
            logger.warning(f"RPC {method} failed: {e}")
            return None

    async def get_mint_info(self, mint: str) -> dict:
        """
        Parse SPL Token MintLayout: mint authority, freeze authority, supply, decimals.
        Returns {} jika gagal.
        """
        result = await self._rpc("getAccountInfo", [mint, {"encoding": "base64"}])
        if not result or not result.get("value"):
            return {}
        data_list = result["value"].get("data", [])
        if not data_list:
            return {}
        return _parse_mint_layout(data_list[0])

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        """Top 20 token holder accounts dengan balance."""
        result = await self._rpc("getTokenLargestAccounts", [mint])
        return (result or {}).get("value", [])

    async def get_token_supply(self, mint: str) -> int:
        """Total supply token (raw amount)."""
        result = await self._rpc("getTokenSupply", [mint])
        return int((result or {}).get("value", {}).get("amount", 0))

    async def get_signatures_for_address(
        self, address: str, limit: int = 20
    ) -> list[dict]:
        """
        Daftar signature transaksi terbaru untuk sebuah address.
        Diurutkan dari terbaru ke terlama.
        """
        result = await self._rpc(
            "getSignaturesForAddress",
            [address, {"limit": limit, "commitment": "confirmed"}],
        )
        return result or []

    async def get_transaction(self, signature: str) -> dict | None:
        """Fetch full transaction data."""
        return await self._rpc(
            "getTransaction",
            [signature, {"encoding": "json", "commitment": "confirmed",
                         "maxSupportedTransactionVersion": 0}],
        )

    async def get_priority_fee(self) -> int:
        """
        Estimasi priority fee jaringan saat ini (microlamports).
        Cached 30 detik — priority fee global, tidak perlu fetch per token.
        """
        import time as _time
        CACHE_TTL = 30.0

        if self._priority_fee_cache:
            fee, ts = self._priority_fee_cache
            if _time.time() - ts < CACHE_TTL:
                self.rpc_call_count -= 0  # no RPC call needed
                return fee

        result = await self._rpc(
            "getPriorityFeeEstimate",
            [{"accountKeys": [], "options": {"priorityLevel": "High"}}],
        )
        if isinstance(result, dict) and "priorityFeeEstimate" in result:
            fee = int(result["priorityFeeEstimate"])
            self._priority_fee_cache = (fee, _time.time())
            return fee

        fees = await self._rpc("getRecentPrioritizationFees", [[]])
        if fees:
            vals = [f["prioritizationFee"] for f in fees if f.get("prioritizationFee")]
            if vals:
                fee = int(sum(vals) / len(vals))
                self._priority_fee_cache = (fee, _time.time())
                return fee
        return 0

    async def get_token_account_balance(self, token_account: str) -> int:
        """Balance sebuah token account (raw amount)."""
        result = await self._rpc("getTokenAccountBalance", [token_account])
        return int((result or {}).get("value", {}).get("amount", 0))

    async def get_token_account_owner(self, token_account: str) -> str | None:
        """
        Ambil owner address dari token account via jsonParsed encoding.
        Dipakai untuk map token_account → real wallet owner.
        """
        result = await self._rpc(
            "getAccountInfo",
            [token_account, {"encoding": "jsonParsed"}],
        )
        if not result or not result.get("value"):
            return None
        try:
            return (
                result["value"]
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
                .get("owner")
            )
        except Exception:
            return None

    async def get_multiple_token_account_owners(
        self, token_accounts: list[str]
    ) -> list[str | None]:
        """Batch fetch owner addresses (1 RPC call)."""
        if not token_accounts:
            return []
        result = await self._rpc(
            "getMultipleAccounts",
            [token_accounts, {"encoding": "jsonParsed"}],
        )
        if not result:
            return [None] * len(token_accounts)
        owners = []
        for acc in (result.get("value") or []):
            if not acc:
                owners.append(None)
                continue
            try:
                owner = (
                    acc.get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                    .get("owner")
                )
                owners.append(owner)
            except Exception:
                owners.append(None)
        # Pad jika hasil kurang dari input
        while len(owners) < len(token_accounts):
            owners.append(None)
        return owners

    async def get_wallet_tx_count_cached(self, wallet: str, limit: int = 20) -> int:
        """
        Ambil jumlah tx sebuah wallet, cached 10 menit.
        Whales / market makers yang sama sering muncul di banyak token.
        """
        import time as _time
        CACHE_TTL = 600.0  # 10 menit

        if wallet in self._wallet_age_cache:
            count, ts = self._wallet_age_cache[wallet]
            if _time.time() - ts < CACHE_TTL:
                return count

        sigs = await self.get_signatures_for_address(wallet, limit=limit)
        count = len(sigs) if sigs else 0
        self._wallet_age_cache[wallet] = (count, _time.time())

        # Prune cache kalau terlalu besar
        if len(self._wallet_age_cache) > 5000:
            now = _time.time()
            self._wallet_age_cache = {
                k: v for k, v in self._wallet_age_cache.items()
                if now - v[1] < CACHE_TTL
            }

        return count

    # ── Helius Enhanced Transactions ───────────────────────────────────────

    async def get_parsed_transactions(self, signatures: list[str]) -> list[dict]:
        """
        Helius enhanced/parsed transactions untuk bundle detection.
        Mengembalikan [] jika tidak ada API key atau request gagal.
        """
        if not self.helius_api_key or not signatures:
            return []
        url = "https://api.helius.xyz/v0/transactions"
        try:
            r = await self._http.post(
                url,
                params={"api-key": self.helius_api_key},
                json={"transactions": signatures[:20]},
            )
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.warning(f"Helius parsed tx failed: {e}")
        return []

    async def get_asset(self, mint: str) -> dict:
        """
        Helius DAS API - metadata token (nama, simbol, creator).
        Fallback ke {} jika tidak ada Helius key.
        """
        if not self.helius_api_key:
            return {}
        url = f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}}
        try:
            r = await self._http.post(url, json=payload)
            if r.status_code == 200:
                return r.json().get("result", {})
        except Exception as e:
            logger.warning(f"getAsset failed: {e}")
        return {}

    # ── DexScreener ────────────────────────────────────────────────────────

    async def get_dexscreener_data(self, mint: str) -> dict:
        """
        Ambil price, market cap, liquidity, volume dari DexScreener.
        Pilih pair Solana dengan likuiditas tertinggi.
        Mengembalikan {} jika token belum listed atau gagal.
        """
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        try:
            r = await self._http.get(url)
            if r.status_code != 200:
                return {}
            pairs = r.json().get("pairs") or []
            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not sol_pairs:
                return {}
            # Ambil pair dengan liquidity USD tertinggi
            return max(
                sol_pairs,
                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
            )
        except Exception as e:
            logger.warning(f"DexScreener failed for {mint}: {e}")
            return {}

    async def get_dexscreener_data_retry(self, mint: str, retries: int = 3, delay: float = 5.0) -> dict:
        """
        Coba ambil data DexScreener beberapa kali.
        Token baru butuh beberapa detik sebelum muncul.
        """
        for attempt in range(retries):
            data = await self.get_dexscreener_data(mint)
            if data:
                return data
            if attempt < retries - 1:
                await asyncio.sleep(delay)
        return {}

    async def get_sol_price_usd(self) -> float:
        """
        Ambil harga SOL USD dari DexScreener (cached 60 detik).
        Fallback ke $150 jika gagal.
        """
        import time
        FALLBACK = 150.0
        CACHE_TTL = 60.0

        if self._sol_price_cache:
            price, ts = self._sol_price_cache
            if time.time() - ts < CACHE_TTL:
                return price

        data = await self.get_dexscreener_data(WSOL_MINT)
        try:
            price = float(data.get("priceUsd") or 0)
            if price > 0:
                self._sol_price_cache = (price, time.time())
                return price
        except Exception:
            pass

        logger.warning(f"Cannot fetch SOL price, using fallback ${FALLBACK}")
        return FALLBACK


# ── Parser ─────────────────────────────────────────────────────────────────

def _parse_mint_layout(data_b64: str) -> dict:
    """
    Parse SPL Token MintLayout dari base64.
    Mengembalikan mint_authority_revoked, freeze_authority_revoked, supply, decimals.
    """
    try:
        data = base64.b64decode(data_b64)
        if len(data) < _MINT_ACCOUNT_SIZE:
            return {}
        mint_auth_opt   = struct.unpack_from("<I", data, _MINT_AUTH_OPT_OFF)[0]
        supply          = struct.unpack_from("<Q", data, _SUPPLY_OFF)[0]
        decimals        = data[_DECIMALS_OFF]
        freeze_auth_opt = struct.unpack_from("<I", data, _FREEZE_AUTH_OPT)[0]
        return {
            "mint_authority_revoked": mint_auth_opt == 0,
            "freeze_authority_revoked": freeze_auth_opt == 0,
            "supply": supply,
            "decimals": decimals,
        }
    except Exception as e:
        logger.warning(f"Failed to parse mint layout: {e}")
        return {}
