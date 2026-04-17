"""
Pair Detector — Real-time WebSocket Listener
──────────────────────────────────────────────
Subscribe ke Pump.fun program via Solana WebSocket.
Setiap token baru yang di-create di Pump.fun akan trigger screening.

Alur:
  1. WebSocket logsSubscribe → pump.fun program
  2. Filter log yang mengandung "Instruction: Create"
  3. Ambil tx signature → fetch via Helius enhanced transactions API
  4. Extract mint address dari instruction accounts
  5. Panggil screener.screen_and_notify(mint)

Reconnect otomatis jika koneksi putus.
Rate limiting: max 1 screening per menit untuk mint yang sama.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import httpx
import websockets

logger = logging.getLogger(__name__)

# Header agar tidak diblokir Cloudflare
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Cache mint yang sudah di-screen dalam 10 menit terakhir (anti-duplicate)
_screened_cache: dict[str, float] = {}
_CACHE_TTL_SECONDS = 600  # 10 menit


class PairDetector:
    """
    Real-time detector untuk new pair di Pump.fun dan Raydium.
    Panggil `start()` untuk mulai listening.
    """

    def __init__(
        self,
        ws_url: str,
        pumpfun_program: str,
        helius_api_key: str,
        on_new_pair: Callable[[str], Awaitable[None]],
        max_concurrent: int = 3,
    ):
        self.ws_url = ws_url
        self.pumpfun_program = pumpfun_program
        self.helius_api_key = helius_api_key
        self.on_new_pair = on_new_pair
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running = False

    async def start(self):
        """
        Mulai listening. Loop reconnect otomatis dengan exponential backoff.
        """
        self._running = True
        backoff = 2.0
        attempt = 0

        while self._running:
            try:
                logger.info(f"Connecting to Solana WebSocket (attempt {attempt + 1})")
                await self._listen()
                backoff = 2.0  # Reset backoff jika berhasil connect
                attempt = 0
            except Exception as e:
                attempt += 1
                logger.warning(f"WebSocket disconnected: {e} | Reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)  # Max 60 detik backoff

    async def stop(self):
        self._running = False

    async def _listen(self):
        """Koneksi WebSocket dan handle pesan masuk."""
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.pumpfun_program]},
                {"commitment": "confirmed"},
            ],
        }

        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=30,
            close_timeout=10,
        ) as ws:
            await ws.send(json.dumps(subscribe_msg))
            confirm = json.loads(await ws.recv())
            logger.info(f"Subscribed to pump.fun | sub_id={confirm.get('result')}")

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    await self._handle_message(msg)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.warning(f"Error handling WS message: {e}")

    async def _handle_message(self, msg: dict):
        """Process satu WebSocket message."""
        # Hanya proses notification (bukan subscription confirm)
        if msg.get("method") != "logsNotification":
            return

        value = msg.get("params", {}).get("result", {}).get("value", {})
        logs = value.get("logs", [])
        signature = value.get("signature", "")
        err = value.get("err")

        # Skip jika transaksi error atau bukan Create instruction
        if err or not signature:
            return
        if not any("Instruction: Create" in log for log in logs):
            return

        # Fetch mint dari transaksi
        mint = await self._extract_mint(signature)
        if not mint:
            logger.debug(f"Could not extract mint from tx: {signature[:20]}...")
            return

        # Skip jika sudah di-screen recently
        if _is_recently_screened(mint):
            return

        _mark_screened(mint)
        logger.info(f"New pair detected: {mint} (tx: {signature[:20]}...)")

        # Jalankan screening dengan concurrency limit
        asyncio.create_task(self._screen_with_limit(mint))

    async def _screen_with_limit(self, mint: str):
        """Jalankan screening dengan semaphore untuk batasi concurrency."""
        async with self._semaphore:
            try:
                await self.on_new_pair(mint)
            except Exception as e:
                logger.error(f"Screening error for {mint}: {e}")

    async def _extract_mint(self, signature: str) -> str | None:
        """
        Ambil mint address dari transaksi pump.fun Create.

        Strategi:
        1. Helius enhanced transactions (paling akurat untuk parsed data)
        2. Fallback: raw RPC getTransaction dan ekstrak dari instruction accounts

        Di pump.fun Create instruction, mint ada di account index 1.
        """
        if self.helius_api_key:
            mint = await self._extract_mint_helius(signature)
            if mint:
                return mint
        return await self._extract_mint_rpc(signature)

    async def _extract_mint_helius(self, signature: str) -> str | None:
        """Extract mint dari Helius parsed transaction."""
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

                # Cari instruction pump.fun dan ambil account index 1 (mint)
                for ix in tx.get("instructions", []):
                    if ix.get("programId") == self.pumpfun_program:
                        accounts = ix.get("accounts", [])
                        if len(accounts) >= 2:
                            return accounts[1]

                # Fallback: cek tokenTransfers
                transfers = tx.get("tokenTransfers", [])
                if transfers:
                    mint = transfers[0].get("mint")
                    if mint:
                        return mint

                # Fallback terakhir: ambil dari accountData
                for acc in tx.get("accountData", []):
                    for change in acc.get("tokenBalanceChanges", []):
                        if change.get("mint"):
                            return change["mint"]

        except Exception as e:
            logger.debug(f"Helius mint extract failed: {e}")
        return None

    async def _extract_mint_rpc(self, signature: str) -> str | None:
        """
        Extract mint dari raw Solana RPC transaction.
        Handle baik legacy maupun versioned (v0) transactions.
        """
        rpc_url = (
            f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
            if self.helius_api_key
            else "https://api.mainnet-beta.solana.com"
        )
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
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

                # Handle versioned transaction: loadedAddresses + staticAccountKeys
                tx_msg = data.get("transaction", {}).get("message", {})
                account_keys = tx_msg.get("accountKeys", [])

                # Cari instruction untuk pump.fun
                instructions = tx_msg.get("instructions", [])
                for ix in instructions:
                    program_idx = ix.get("programIdIndex")
                    if program_idx is None or program_idx >= len(account_keys):
                        continue
                    if account_keys[program_idx] == self.pumpfun_program:
                        account_indices = ix.get("accounts", [])
                        # Di pump.fun Create, mint ada di account index 1 dari instruction
                        if len(account_indices) >= 2:
                            mint_idx = account_indices[1]
                            if mint_idx < len(account_keys):
                                return account_keys[mint_idx]

                # Fallback: cek postTokenBalances → ambil mint pertama
                meta = data.get("meta", {}) or {}
                post_balances = meta.get("postTokenBalances", [])
                if post_balances:
                    return post_balances[0].get("mint")

        except Exception as e:
            logger.debug(f"RPC mint extract failed: {e}")
        return None


# ── Cache helpers ──────────────────────────────────────────────────────────

def _is_recently_screened(mint: str) -> bool:
    """Cek apakah mint ini sudah di-screen dalam TTL window."""
    _cleanup_cache()
    return mint in _screened_cache


def _mark_screened(mint: str):
    _screened_cache[mint] = time.time()


def _cleanup_cache():
    """Hapus entri cache yang sudah expired."""
    now = time.time()
    expired = [k for k, v in _screened_cache.items() if now - v > _CACHE_TTL_SECONDS]
    for k in expired:
        del _screened_cache[k]
