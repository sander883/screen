"""
GMGN.AI Client — On-chain Analytics & Token Security
──────────────────────────────────────────────────────
Integrasi dengan GMGN API untuk:
1. New pair detection (alternatif WebSocket polling)
2. Token security / rug risk check
3. Trending tokens + smart money data

API Base: https://gmgn.ai/defi/quotation/v1/
Auth: header x-route-key (Cooperation API key)
Rate limit: 1 call per 5 seconds per key
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://gmgn.ai/defi/quotation/v1"

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://gmgn.ai/",
}


class GmgnClient:
    """Async client untuk GMGN.AI API."""

    def __init__(self, api_key: str = "", timeout: float = 10.0):
        self.api_key = api_key
        headers = {**_DEFAULT_HEADERS}
        if api_key:
            headers["x-route-key"] = api_key
        self._http = httpx.AsyncClient(timeout=timeout, headers=headers)
        self._last_call: float = 0.0
        self._rate_limit = 5.0  # 1 call per 5 seconds

    async def close(self):
        await self._http.aclose()

    async def _throttle(self):
        """Rate limit: max 1 call per 5 seconds."""
        now = time.time()
        elapsed = now - self._last_call
        if elapsed < self._rate_limit:
            await asyncio.sleep(self._rate_limit - elapsed)
        self._last_call = time.time()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """GET request dengan rate limiting."""
        await self._throttle()
        url = f"{_BASE_URL}{path}"
        try:
            r = await self._http.get(url, params=params)
            if r.status_code == 429:
                logger.warning("GMGN rate limited (429)")
                return {}
            if r.status_code != 200:
                logger.warning(f"GMGN {path}: HTTP {r.status_code}")
                return {}
            return r.json()
        except Exception as e:
            logger.warning(f"GMGN {path} failed: {e}")
            return {}

    # ── New Pairs ─────────────────────────────────────────────────────────

    async def get_new_pairs(self, limit: int = 20) -> list[dict]:
        """
        Fetch token baru di Solana, sorted by creation time (terbaru dulu).
        Sudah difilter: not honeypot, verified, renounced.

        Returns list of token dicts with: address, symbol, name, usd_market_cap,
        liquidity, holder_count, open_timestamp, dll.
        """
        data = await self._get(
            "/rank/sol/swaps/1h",
            params={
                "orderby": "open_timestamp",
                "direction": "desc",
                "limit": str(limit),
                "filters[]": ["not_honeypot", "renounced"],
            },
        )
        return _extract_token_list(data)

    async def get_pump_tokens(self, limit: int = 20) -> list[dict]:
        """
        Fetch Pump.fun tokens sorted by bonding curve progress.
        Berguna untuk monitor token yang hampir graduate.
        """
        data = await self._get(
            "/rank/sol/pump",
            params={
                "limit": str(limit),
                "orderby": "progress",
                "direction": "desc",
                "pump": "true",
            },
        )
        return _extract_token_list(data)

    # ── Token Security ────────────────────────────────────────────────────

    async def get_token_security(self, mint: str) -> dict:
        """
        Cek keamanan token dari GMGN.
        Returns dict dengan:
          - is_honeypot: bool
          - is_blacklisted: bool
          - owner_renounced: bool
          - lp_burned_pct: float
          - buy_tax: float
          - sell_tax: float
          - smart_money_count: int
          - rug_risk: str ("low" / "medium" / "high")
        """
        data = await self._get(f"/tokens/sol/{mint}")
        if not data or not data.get("data"):
            return {}

        token = data["data"]
        if isinstance(token, dict) and "token" in token:
            token = token["token"]
        elif isinstance(token, list) and token:
            token = token[0]

        return _parse_token_security(token)

    # ── Trending / Smart Money ────────────────────────────────────────────

    async def get_trending(
        self, timeframe: str = "1h", orderby: str = "smartmoney", limit: int = 20
    ) -> list[dict]:
        """
        Fetch trending tokens sorted by smart money activity.

        timeframe: 1m, 5m, 1h, 6h, 24h
        orderby: smartmoney, volume, marketcap, holder_count, swaps
        """
        data = await self._get(
            f"/rank/sol/swaps/{timeframe}",
            params={
                "orderby": orderby,
                "direction": "desc",
                "limit": str(limit),
                "filters[]": ["not_honeypot", "renounced"],
            },
        )
        return _extract_token_list(data)

    async def get_smart_money_for_token(self, mint: str) -> dict:
        """
        Cek apakah smart money / KOL membeli token ini.
        Returns: smart_money_count, smart_money_holding_pct, top_wallets
        """
        data = await self._get(f"/tokens/sol/{mint}")
        if not data or not data.get("data"):
            return {"smart_money_count": 0}

        token = data["data"]
        if isinstance(token, dict) and "token" in token:
            token = token["token"]
        elif isinstance(token, list) and token:
            token = token[0]

        return {
            "smart_money_count": token.get("smart_money") or token.get("smart_degen") or 0,
            "bluechip_owner_pct": token.get("bluechip_owner_percentage", 0),
            "holder_count": token.get("holder_count", 0),
            "is_trending": bool(token.get("is_show_alert")),
        }


# ── Helpers ───────────────────────────────────────────────────────────────

def _extract_token_list(data: dict) -> list[dict]:
    """Extract token list dari GMGN response (various formats)."""
    if not data:
        return []
    inner = data.get("data")
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        # Bisa nested: data.rank, data.tokens, atau langsung list values
        for key in ("rank", "tokens", "pairs"):
            if key in inner and isinstance(inner[key], list):
                return inner[key]
        # Fallback: ambil value pertama yang list
        for v in inner.values():
            if isinstance(v, list):
                return v
    return []


def _parse_token_security(token: dict) -> dict:
    """Parse token security fields dari GMGN response."""
    if not token:
        return {}

    is_honeypot = bool(token.get("is_honeypot"))
    owner_renounced = bool(
        token.get("renounced") or token.get("owner_renounced")
    )
    lp_burned = float(token.get("burn_ratio") or token.get("lp_burned_pct") or 0)

    buy_tax = float(token.get("buy_tax") or 0)
    sell_tax = float(token.get("sell_tax") or 0)

    smart_money = int(token.get("smart_money") or token.get("smart_degen") or 0)

    # Determine rug risk level
    risk = "low"
    if is_honeypot:
        risk = "high"
    elif sell_tax > 10 or buy_tax > 10:
        risk = "high"
    elif not owner_renounced:
        risk = "medium"
    elif sell_tax > 5 or buy_tax > 5:
        risk = "medium"

    return {
        "source": "gmgn",
        "is_honeypot": is_honeypot,
        "owner_renounced": owner_renounced,
        "lp_burned_pct": lp_burned,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "smart_money_count": smart_money,
        "bluechip_owner_pct": float(token.get("bluechip_owner_percentage") or 0),
        "holder_count": int(token.get("holder_count") or 0),
        "rug_risk": risk,
        "address": token.get("address", ""),
        "symbol": token.get("symbol", ""),
    }
