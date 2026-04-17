"""
Unit tests untuk Solana screening components.
Menggunakan mocking — tidak butuh koneksi internet atau API key.
"""

import asyncio
import base64
import struct
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from config import Config
from solana_client import SolanaClient, _parse_mint_layout, WSOL_MINT
from solana_screener import SolanaScreener, SolanaTokenResult


# ── Helpers ────────────────────────────────────────────────────────────────

def _build_mint_layout(mint_auth_revoked: bool, freeze_auth_revoked: bool,
                      supply: int = 1_000_000_000, decimals: int = 6) -> str:
    """Build a fake SPL Token MintLayout (82 bytes) as base64."""
    buf = bytearray(82)
    struct.pack_into("<I", buf, 0, 0 if mint_auth_revoked else 1)
    # mint_authority pubkey (32 bytes) - leave zeros
    struct.pack_into("<Q", buf, 36, supply)
    buf[44] = decimals
    buf[45] = 1  # is_initialized
    struct.pack_into("<I", buf, 46, 0 if freeze_auth_revoked else 1)
    return base64.b64encode(bytes(buf)).decode()


def _make_mock_client(**overrides):
    """Buat SolanaClient dengan async methods di-mock."""
    client = MagicMock(spec=SolanaClient)
    defaults = {
        "get_mint_info": AsyncMock(return_value={
            "mint_authority_revoked": True,
            "freeze_authority_revoked": True,
            "supply": 1_000_000_000,
            "decimals": 6,
        }),
        "get_token_largest_accounts": AsyncMock(return_value=[
            {"address": f"acc{i}", "amount": str(100_000 - i*1000), "decimals": 6}
            for i in range(20)
        ]),
        "get_token_supply": AsyncMock(return_value=1_000_000_000),
        "get_multiple_token_account_owners": AsyncMock(return_value=[
            f"owner{i}" for i in range(15)
        ]),
        "get_signatures_for_address": AsyncMock(return_value=[
            {"signature": f"sig{i}"} for i in range(50)  # 50 tx = not fresh
        ]),
        "get_priority_fee": AsyncMock(return_value=50_000),
        "get_dexscreener_data_retry": AsyncMock(return_value={
            "baseToken": {"symbol": "TEST", "name": "Test Token"},
            "priceUsd": "0.0005",
            "marketCap": 100_000,
            "liquidity": {"usd": 15_000},
            "volume": {"m5": 500, "h1": 5000},
            "pairCreatedAt": 1700000000000,
        }),
        "get_sol_price_usd": AsyncMock(return_value=150.0),
        "close": AsyncMock(),
    }
    defaults.update(overrides)
    for name, val in defaults.items():
        setattr(client, name, val)
    return client


# ── Parser Tests ───────────────────────────────────────────────────────────

def test_parse_mint_layout_revoked():
    """Parse mint layout dengan authority revoked."""
    data = _build_mint_layout(mint_auth_revoked=True, freeze_auth_revoked=True)
    result = _parse_mint_layout(data)
    assert result["mint_authority_revoked"] is True
    assert result["freeze_authority_revoked"] is True
    assert result["supply"] == 1_000_000_000
    assert result["decimals"] == 6


def test_parse_mint_layout_active():
    """Parse mint layout dengan authority masih aktif."""
    data = _build_mint_layout(mint_auth_revoked=False, freeze_auth_revoked=False)
    result = _parse_mint_layout(data)
    assert result["mint_authority_revoked"] is False
    assert result["freeze_authority_revoked"] is False


def test_parse_mint_layout_partial():
    """Mint revoked tapi freeze aktif."""
    data = _build_mint_layout(mint_auth_revoked=True, freeze_auth_revoked=False)
    result = _parse_mint_layout(data)
    assert result["mint_authority_revoked"] is True
    assert result["freeze_authority_revoked"] is False


def test_parse_mint_layout_invalid():
    """Data invalid harus return dict kosong."""
    assert _parse_mint_layout("") == {}
    assert _parse_mint_layout("aW52YWxpZA==") == {}  # too short


# ── Filter Tests ───────────────────────────────────────────────────────────

def test_token_safety_auto_skip_mint_active():
    """Mint authority aktif → auto skip flag."""
    from filters import token_safety
    client = _make_mock_client(
        get_mint_info=AsyncMock(return_value={
            "mint_authority_revoked": False,
            "freeze_authority_revoked": True,
        })
    )
    flag, details = asyncio.run(token_safety.check("mint123", client))
    assert flag is True
    assert details["auto_skip"] is True
    assert "mint authority masih aktif" in details["reasons"]


def test_token_safety_safe():
    """Kedua authority revoked → tidak flag."""
    from filters import token_safety
    client = _make_mock_client()
    flag, details = asyncio.run(token_safety.check("mint123", client))
    assert flag is False
    assert details["mint_authority_revoked"] is True


def test_network_filter_normal():
    """Priority fee normal → tidak flag."""
    from filters import network
    client = _make_mock_client(get_priority_fee=AsyncMock(return_value=50_000))
    flag, details = asyncio.run(network.check(client, Config))
    assert flag is False
    assert details["network_status"] == "normal"


def test_network_filter_congested():
    """Priority fee tinggi → flag."""
    from filters import network
    client = _make_mock_client(get_priority_fee=AsyncMock(return_value=1_000_000))
    flag, details = asyncio.run(network.check(client, Config))
    assert flag is True
    assert details["network_status"] == "congested"


def test_wallet_analysis_too_few_holders():
    """Holders < MIN_HOLDER_COUNT → flag."""
    from filters import wallet_analysis
    client = _make_mock_client(
        get_token_largest_accounts=AsyncMock(return_value=[
            {"address": f"acc{i}", "amount": "100"} for i in range(5)
        ])
    )
    flag, details = asyncio.run(wallet_analysis.check("mint123", client, Config))
    assert flag is True
    assert details["holder_count"] == 5


def test_wallet_analysis_many_fresh_wallets():
    """Terlalu banyak wallet fresh → bundle flag."""
    from filters import wallet_analysis
    client = _make_mock_client(
        get_signatures_for_address=AsyncMock(return_value=[
            {"signature": "s1"}, {"signature": "s2"}  # hanya 2 tx = fresh
        ])
    )
    flag, details = asyncio.run(wallet_analysis.check("mint123", client, Config))
    assert flag is True
    assert details["bundle_suspected"] is True


def test_wallet_analysis_healthy():
    """Wallet holders punya history panjang → tidak flag."""
    from filters import wallet_analysis
    client = _make_mock_client()  # default = 50 tx per wallet
    flag, details = asyncio.run(wallet_analysis.check("mint123", client, Config))
    assert flag is False


def test_holder_quality_concentration():
    """Top holder menguasai > 15% → flag."""
    from filters import holder_quality
    client = _make_mock_client(
        get_token_largest_accounts=AsyncMock(return_value=[
            {"address": "top", "amount": "300000000"},  # 30%
            *[{"address": f"a{i}", "amount": "10000000"} for i in range(10)],
        ]),
        get_token_supply=AsyncMock(return_value=1_000_000_000),
    )
    flag, details = asyncio.run(holder_quality.check("mint123", client, Config))
    assert flag is True
    assert details["top1_holder_pct"] > Config.MAX_TOP_HOLDER_PCT


def test_holder_quality_distributed():
    """Distribusi sehat → tidak flag."""
    from filters import holder_quality
    client = _make_mock_client(
        get_token_largest_accounts=AsyncMock(return_value=[
            {"address": f"a{i}", "amount": "30000000"}  # 3% each
            for i in range(20)
        ]),
        get_token_supply=AsyncMock(return_value=1_000_000_000),
    )
    flag, details = asyncio.run(holder_quality.check("mint123", client, Config))
    assert flag is False


def test_entry_quality_no_market_data():
    """Tidak ada data DEX → flag."""
    from filters import entry_quality
    client = _make_mock_client(
        get_dexscreener_data_retry=AsyncMock(return_value={}),
    )
    flag, details = asyncio.run(entry_quality.check("mint123", client, Config, None))
    assert flag is True


def test_entry_quality_mcap_too_high():
    """Market cap di atas range → flag."""
    from filters import entry_quality
    dex_data = {
        "baseToken": {"symbol": "TEST", "name": "Test"},
        "priceUsd": "0.01",
        "marketCap": 2_000_000,
        "liquidity": {"usd": 100_000},
        "volume": {"m5": 1000, "h1": 10000},
    }
    client = _make_mock_client()
    flag, details = asyncio.run(entry_quality.check("m", client, Config, dex_data))
    assert flag is True
    assert details["market_cap_usd"] == 2_000_000


def test_entry_quality_liquidity_too_low():
    """Liquidity < 10 SOL → flag."""
    from filters import entry_quality
    dex_data = {
        "baseToken": {"symbol": "TEST"},
        "priceUsd": "0.001",
        "marketCap": 100_000,
        "liquidity": {"usd": 500},  # @ $150 SOL = ~3.3 SOL
        "volume": {"m5": 100, "h1": 1000},
    }
    client = _make_mock_client()
    flag, details = asyncio.run(entry_quality.check("m", client, Config, dex_data))
    assert flag is True
    assert details["liquidity_sol_est"] < Config.MIN_LIQUIDITY_SOL


def test_entry_quality_sweet_spot():
    """Market cap & liquidity dalam range → tidak flag."""
    from filters import entry_quality
    dex_data = {
        "baseToken": {"symbol": "TEST"},
        "priceUsd": "0.001",
        "marketCap": 100_000,
        "liquidity": {"usd": 5_000},  # ~33 SOL
        "volume": {"m5": 1000, "h1": 10000},
    }
    client = _make_mock_client()
    flag, details = asyncio.run(entry_quality.check("m", client, Config, dex_data))
    assert flag is False


# ── End-to-end Screener Tests ──────────────────────────────────────────────

def test_screener_gas_it_full_pipeline():
    """Semua filter OK → decision GAS IT."""
    async def run():
        screener = SolanaScreener(Config)
        screener.client = _make_mock_client()
        result = await screener.screen("mint_gas_it")
        await screener.close()
        return result

    result = asyncio.run(run())
    assert result.decision == "GAS IT"
    assert result.total_flags == 0
    assert result.safety_flag is False


def test_screener_auto_skip_unsafe():
    """Mint authority aktif → decision SKIP (auto)."""
    async def run():
        screener = SolanaScreener(Config)
        screener.client = _make_mock_client(
            get_mint_info=AsyncMock(return_value={
                "mint_authority_revoked": False,
                "freeze_authority_revoked": True,
            })
        )
        result = await screener.screen("mint_unsafe")
        await screener.close()
        return result

    result = asyncio.run(run())
    assert result.decision == "SKIP"
    assert result.safety_flag is True


def test_screener_multi_flag_skip():
    """Multi filter flag → decision SKIP."""
    async def run():
        screener = SolanaScreener(Config)
        screener.client = _make_mock_client(
            get_priority_fee=AsyncMock(return_value=2_000_000),  # congested
            get_token_largest_accounts=AsyncMock(return_value=[
                {"address": "top", "amount": "400000000"},  # 40% top1
                *[{"address": f"a{i}", "amount": "20000000"} for i in range(19)],
            ]),
        )
        result = await screener.screen("mint_multi")
        await screener.close()
        return result

    result = asyncio.run(run())
    assert result.decision == "SKIP"
    assert result.total_flags >= 2


# ── Telegram Format Tests ──────────────────────────────────────────────────

def test_telegram_format_gas_it():
    """Format message GAS IT harus include CA dan semua data."""
    from notifier.telegram import _format_message

    r = SolanaTokenResult(
        mint="Abc123XyZ",
        symbol="PEPE2",
        name="Pepe 2.0",
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        market_cap_usd=100_000,
        liquidity_sol_est=50.0,
        liquidity_usd=7500,
        holder_count=150,
        top1_holder_pct=5.0,
        top10_combined_pct=25.0,
        mcap_tier="sweet spot ($69K-$200K) — post-graduation",
    )
    msg = _format_message(r)
    assert "GAS IT" in msg
    assert "Abc123XyZ" in msg
    assert "PEPE2" in msg


def test_telegram_format_skip():
    """Format message SKIP harus jelas."""
    from notifier.telegram import _format_message

    r = SolanaTokenResult(
        mint="Scm777",
        symbol="RUG",
        safety_flag=True,
        flag_reasons=["mint authority masih aktif"],
    )
    msg = _format_message(r)
    assert "SKIP" in msg
    assert "Scm777" in msg


# ── Runner ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
