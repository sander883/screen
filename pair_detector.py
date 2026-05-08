"""
Pair Detector — Real-time New Pair Listener
──────────────────────────────────────────────
Deteksi token baru dari Pump.fun via 2 strategi:

1. WebSocket (preferred) — logsSubscribe ke pump.fun program
   + Real-time, <1s latency
   - Helius free tier sering kena 429

2. HTTP Polling (fallback) — getSignaturesForAddress setiap 10 detik
   + Reliable, pakai RPC biasa yang sudah jalan
   + ~6 RPC calls/menit (hemat)
   - Latency 5-10 detik

Auto-switch: mulai dari WebSocket, kalau 429 3x berturut → pindah ke polling.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import httpx
import websockets

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

_screened_cache: dict[str, float] = {}
_CACHE_TTL_SECONDS = 600

_MAX_WS_429_BEFORE_FALLBACK = 3


class PairDetector:
    """
    Real-time detector untuk new pair di Pump.fun.
    Auto-fallback dari WebSocket ke HTTP polling jika kena rate limit.
    """

    def __init__(
        self,
        ws_url: str,
        pumpfun_program: str,
        helius_api_key: str,
        on_new_pair: Callable[[str], Awaitable[None]],
        max_concurrent: int = 3,
        rpc_url: str = "",
        gmgn_api_key: str = "",
    ):
        self.ws_url = ws_url
        self.rpc_url = rpc_url or (
            f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
            if helius_api_key else "https://api.mainnet-beta.solana.com"
        )
        self.pumpfun_program = pumpfun_program
        self.helius_api_key = helius_api_key
        self.gmgn_api_key = gmgn_api_key
        self.on_new_pair = on_new_pair
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running = False
        self._ws_429_count = 0
        self._mode = "websocket"  # "websocket", "polling", or "gmgn"

    async def start(self):
        """
        Mulai listening. Fallback chain:
        1. WebSocket (real-time, <1s)
        2. GMGN API polling (jika ada key, 0 RPC, setiap 12s)
        3. HTTP RPC polling (fallback terakhir, setiap 10s)
        """
        self._running = True
        # Kalau ada GMGN key tapi tidak ada Helius WS, langsung GMGN
        if self.gmgn_api_key and not self.helius_api_key:
            self._mode = "gmgn"

        while self._running:
            try:
                if self._mode == "websocket":
                    await self._start_websocket()
                elif self._mode == "gmgn":
                    await self._start_gmgn_polling()
                else:
                    await self._start_polling()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Detector error ({self._mode}): {e}")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False

    # ── WebSocket Mode ────────────────────────────────────────────────────

    async def _start_websocket(self):
        backoff = 2.0
        attempt = 0

        while self._running and self._mode == "websocket":
            try:
                attempt += 1
                logger.info(f"Connecting WebSocket (attempt {attempt})")
                await self._ws_listen()
                backoff = 2.0
                attempt = 0
                self._ws_429_count = 0
            except websockets.exceptions.InvalidStatusCode as e:
                if e.status_code == 429:
                    self._ws_429_count += 1
                    logger.warning(
                        f"WebSocket 429 ({self._ws_429_count}/{_MAX_WS_429_BEFORE_FALLBACK})"
                    )
                    if self._ws_429_count >= _MAX_WS_429_BEFORE_FALLBACK:
                        if self.gmgn_api_key:
                            logger.info("Switching to GMGN polling (WebSocket rate limited)")
                            self._mode = "gmgn"
                        else:
                            logger.info("Switching to HTTP polling (WebSocket rate limited)")
                            self._mode = "polling"
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    raise
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    self._ws_429_count += 1
                    logger.warning(
                        f"WebSocket 429 ({self._ws_429_count}/{_MAX_WS_429_BEFORE_FALLBACK})"
                    )
                    if self._ws_429_count >= _MAX_WS_429_BEFORE_FALLBACK:
                        if self.gmgn_api_key:
                            logger.info("Switching to GMGN polling (WebSocket rate limited)")
                            self._mode = "gmgn"
                        else:
                            logger.info("Switching to HTTP polling (WebSocket rate limited)")
                            self._mode = "polling"
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    logger.warning(f"WebSocket error: {e} | Reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _ws_listen(self):
        subscribe_msg = {
            "jsonrpc": "2.0", "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.pumpfun_program]},
                {"commitment": "confirmed"},
            ],
        }

        async with websockets.connect(
            self.ws_url, ping_interval=20, ping_timeout=30, close_timeout=10,
        ) as ws:
            await ws.send(json.dumps(subscribe_msg))
            confirm = json.loads(await ws.recv())
            logger.info(f"WebSocket connected | sub_id={confirm.get('result')}")
            self._ws_429_count = 0

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    await self._handle_ws_message(msg)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.warning(f"WS message error: {e}")

    async def _handle_ws_message(self, msg: dict):
        if msg.get("method") != "logsNotification":
            return

        value = msg.get("params", {}).get("result", {}).get("value", {})
        logs = value.get("logs", [])
        signature = value.get("signature", "")
        err = value.get("err")

        if err or not signature:
            return
        if not any("Instruction: Create" in log for log in logs):
            return

        mint = await self._extract_mint(signature)
        if not mint:
            return

        if _is_recently_screened(mint):
            return

        _mark_screened(mint)
        logger.info(f"New pair (ws): {mint}")
        asyncio.create_task(self._screen_with_limit(mint))

    # ── GMGN Polling Mode ─────────────────────────────────────────────────

    async def _start_gmgn_polling(self):
        """
        Poll GMGN API untuk new pairs setiap 12 detik (rate limit: 1 per 5s).
        Keuntungan: 0 RPC calls, data sudah difilter (not honeypot, renounced).
        """
        from gmgn_client import GmgnClient

        POLL_INTERVAL = 12  # GMGN rate limit = 5s, kita beri margin
        gmgn = GmgnClient(api_key=self.gmgn_api_key)

        logger.info(f"GMGN polling mode started | interval={POLL_INTERVAL}s | 0 RPC calls")

        seen_mints: set[str] = set()
        try:
            while self._running:
                try:
                    tokens = await gmgn.get_new_pairs(limit=20)
                    for token in tokens:
                        mint = token.get("address") or token.get("mint") or ""
                        if not mint or mint in seen_mints:
                            continue
                        if _is_recently_screened(mint):
                            seen_mints.add(mint)
                            continue

                        seen_mints.add(mint)
                        _mark_screened(mint)
                        logger.info(f"New pair (gmgn): {mint} ({token.get('symbol', '?')})")
                        asyncio.create_task(self._screen_with_limit(mint))

                    # Cap seen set size
                    if len(seen_mints) > 5000:
                        seen_mints = set(list(seen_mints)[-2000:])

                except Exception as e:
                    logger.warning(f"GMGN polling error: {e}")

                await asyncio.sleep(POLL_INTERVAL)
        finally:
            await gmgn.close()

    # ── HTTP Polling Mode ─────────────────────────────────────────────────

    async def _start_polling(self):
        """
        Poll getSignaturesForAddress pada pump.fun program setiap POLL_INTERVAL detik.
        Detect signature baru → extract mint → screen.
        """
        POLL_INTERVAL = 10  # detik
        last_signature: str | None = None
        http = httpx.AsyncClient(timeout=10.0, headers=_DEFAULT_HEADERS)

        logger.info(
            f"HTTP polling mode started | interval={POLL_INTERVAL}s | "
            f"program={self.pumpfun_program[:20]}..."
        )

        try:
            while self._running:
                try:
                    new_sigs = await self._poll_new_signatures(http, last_signature)
                    if new_sigs:
                        last_signature = new_sigs[0].get("signature")
                        for sig_info in reversed(new_sigs):
                            sig = sig_info.get("signature", "")
                            if not sig or sig_info.get("err"):
                                continue
                            await self._process_signature(http, sig)
                except Exception as e:
                    logger.warning(f"Polling error: {e}")

                await asyncio.sleep(POLL_INTERVAL)
        finally:
            await http.aclose()

    async def _poll_new_signatures(
        self, http: httpx.AsyncClient, last_sig: str | None
    ) -> list[dict]:
        """Fetch recent signatures for pump.fun program."""
        params: list = [
            self.pumpfun_program,
            {"limit": 10, "commitment": "confirmed"},
        ]
        if last_sig:
            params[1]["until"] = last_sig

        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": params,
        }

        r = await http.post(self.rpc_url, json=payload)
        r.raise_for_status()
        data = r.json()
        return data.get("result") or []

    async def _process_signature(self, http: httpx.AsyncClient, signature: str):
        """Verifikasi apakah signature ini adalah Create instruction, lalu screen."""
        mint = await self._extract_mint(signature)
        if not mint:
            return
        if _is_recently_screened(mint):
            return

        _mark_screened(mint)
        logger.info(f"New pair (poll): {mint}")
        asyncio.create_task(self._screen_with_limit(mint))

    # ── Shared ────────────────────────────────────────────────────────────

    async def _screen_with_limit(self, mint: str):
        async with self._semaphore:
            try:
                await self.on_new_pair(mint)
            except Exception as e:
                logger.error(f"Screening error for {mint}: {e}")

    async def _extract_mint(self, signature: str) -> str | None:
        if self.helius_api_key:
            mint = await self._extract_mint_helius(signature)
            if mint:
                return mint
        return await self._extract_mint_rpc(signature)

    async def _extract_mint_helius(self, signature: str) -> str | None:
        url = "https://api.helius.xyz/v0/transactions"
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=_DEFAULT_HEADERS) as http:
                r = await http.post(
                    url,
                    params={"api-key": self.helius_api_key},
                    json={"transactions": [signature]},
                )
                if r.status_code != 200:
                    return None
                txs = r.json()
                if not txs:
                    return None
                tx = txs[0]

                for ix in tx.get("instructions", []):
                    if ix.get("programId") == self.pumpfun_program:
                        accounts = ix.get("accounts", [])
                        if len(accounts) >= 2:
                            return accounts[1]

                transfers = tx.get("tokenTransfers", [])
                if transfers:
                    mint = transfers[0].get("mint")
                    if mint:
                        return mint

                for acc in tx.get("accountData", []):
                    for change in acc.get("tokenBalanceChanges", []):
                        if change.get("mint"):
                            return change["mint"]

        except Exception as e:
            logger.debug(f"Helius mint extract failed: {e}")
        return None

    async def _extract_mint_rpc(self, signature: str) -> str | None:
        rpc_url = (
            f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
            if self.helius_api_key
            else "https://api.mainnet-beta.solana.com"
        )
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "json", "commitment": "confirmed",
                 "maxSupportedTransactionVersion": 0},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=_DEFAULT_HEADERS) as http:
                r = await http.post(rpc_url, json=payload)
                if r.status_code != 200:
                    return None
                data = r.json().get("result")
                if not data:
                    return None

                tx_msg = data.get("transaction", {}).get("message", {})
                account_keys = tx_msg.get("accountKeys", [])

                instructions = tx_msg.get("instructions", [])
                for ix in instructions:
                    program_idx = ix.get("programIdIndex")
                    if program_idx is None or program_idx >= len(account_keys):
                        continue
                    if account_keys[program_idx] == self.pumpfun_program:
                        account_indices = ix.get("accounts", [])
                        if len(account_indices) >= 2:
                            mint_idx = account_indices[1]
                            if mint_idx < len(account_keys):
                                return account_keys[mint_idx]

                meta = data.get("meta", {}) or {}
                post_balances = meta.get("postTokenBalances", [])
                if post_balances:
                    return post_balances[0].get("mint")

        except Exception as e:
            logger.debug(f"RPC mint extract failed: {e}")
        return None


# ── Cache helpers ──────────────────────────────────────────────────────────

def _is_recently_screened(mint: str) -> bool:
    _cleanup_cache()
    return mint in _screened_cache


def _mark_screened(mint: str):
    _screened_cache[mint] = time.time()


def _cleanup_cache():
    now = time.time()
    expired = [k for k, v in _screened_cache.items() if now - v > _CACHE_TTL_SECONDS]
    for k in expired:
        del _screened_cache[k]
