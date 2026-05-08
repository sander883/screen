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
    import time
    client = MagicMock(spec=SolanaClient)
    # Pair created 1 menit lalu — fresh untuk semua mode
    pair_created_ms = int(time.time() * 1000) - 60_000
    # RPC call counter (non-mock attribute)
    client.rpc_call_count = 0
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
        "get_signatures_for_address": AsyncMock(
            side_effect=lambda addr, limit=20: [
                {"signature": f"sig-{addr}-{i}",
                 "slot": hash(addr) % 1_000_000 + i,
                 "blockTime": int(time.time()) - 200 * 86400 + i * 86400}  # ~200 days ago spread
                for i in range(min(limit, 50))
            ]
        ),
        "get_wallet_tx_count_cached": AsyncMock(return_value=50),
        "_fetch_wallet_info": AsyncMock(
            side_effect=lambda w, limit=20: (50, time.time() - (100 + hash(w) % 300) * 86400)
        ),
        "get_wallet_age_days": AsyncMock(
            side_effect=lambda w, limit=20: 100.0 + hash(w) % 300
        ),
        "get_priority_fee": AsyncMock(return_value=50_000),
        "get_dexscreener_data_retry": AsyncMock(return_value={
            "baseToken": {"symbol": "TEST", "name": "Test Token"},
            "priceUsd": "0.0005",
            "marketCap": 50_000,
            "liquidity": {"usd": 30_000},
            "volume": {"m5": 500, "h1": 5000},
            "pairCreatedAt": pair_created_ms,
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
    """Terlalu banyak wallet fresh → flag."""
    import time
    from filters import wallet_analysis
    client = _make_mock_client(
        get_wallet_tx_count_cached=AsyncMock(return_value=2),  # hanya 2 tx = fresh
        _fetch_wallet_info=AsyncMock(return_value=(2, time.time() - 5 * 86400)),  # 5 days old
        get_wallet_age_days=AsyncMock(return_value=5.0),  # 5 days old
    )
    flag, details = asyncio.run(wallet_analysis.check("mint123", client, Config))
    assert flag is True
    assert details["fresh_wallet_ratio"] > 0


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
    """Liquidity < MIN_LIQUIDITY_SOL → flag."""
    import time
    from filters import entry_quality
    dex_data = {
        "baseToken": {"symbol": "TEST"},
        "priceUsd": "0.001",
        "marketCap": 100_000,
        "liquidity": {"usd": 200},  # @ $150 SOL = ~1.3 SOL (di bawah threshold fresh 3 SOL)
        "volume": {"m5": 100, "h1": 1000},
        "pairCreatedAt": int(time.time() * 1000) - 60_000,  # baru 1 menit lalu
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
        "marketCap": 50_000,
        "liquidity": {"usd": 30_000},  # ~200 SOL, ratio 1.67x
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


def test_screener_entry_flag_hard_skip():
    """Entry flag alone → SKIP (mcap terlalu tinggi bukan target scalp)."""
    import time
    async def run():
        screener = SolanaScreener(Config)
        screener.client = _make_mock_client(
            get_dexscreener_data_retry=AsyncMock(return_value={
                "baseToken": {"symbol": "BIGCAP", "name": "Big Cap Token"},
                "priceUsd": "7.0",
                "marketCap": 7_000_000,  # $7M — jauh di atas max $80K
                "liquidity": {"usd": 500_000},
                "volume": {"m5": 1000, "h1": 10_000},
                "pairCreatedAt": int(time.time() * 1000) - 60_000,
            }),
        )
        result = await screener.screen("mint_bigcap")
        await screener.close()
        return result

    result = asyncio.run(run())
    assert result.decision == "SKIP"
    assert result.entry_flag is True
    assert result.skipped_early is True
    # Should NOT run wallet analysis (RPC savings)
    assert result.holder_count == 0


def test_decision_entry_flag_overrides():
    """Even with 0 other flags, entry_flag alone = SKIP."""
    r = SolanaTokenResult(mint="x", entry_flag=True)
    assert r.decision == "SKIP"
    r2 = SolanaTokenResult(mint="x", entry_flag=False)
    assert r2.decision == "GAS IT"


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


# ── Wallet Age Tests ──────────────────────────────────────────────────────

def test_wallet_analysis_young_wallets_flag():
    """Semua wallet umurnya < 30 hari → flag (avg too low)."""
    import time as _time
    from filters import wallet_analysis
    client = _make_mock_client(
        _fetch_wallet_info=AsyncMock(
            side_effect=lambda w, limit=20: (50, _time.time() - 10 * 86400)  # 10 days old
        ),
        get_wallet_age_days=AsyncMock(return_value=10.0),
    )
    flag, details = asyncio.run(wallet_analysis.check("mint", client, Config))
    assert flag is True
    assert details["avg_wallet_age_days"] < 30
    assert any("avg wallet age" in r.lower() for r in details["reasons"])


def test_wallet_analysis_uniform_age_flag():
    """Semua wallet umur persis sama → std dev 0 → flag."""
    import time as _time
    from filters import wallet_analysis
    client = _make_mock_client(
        _fetch_wallet_info=AsyncMock(
            return_value=(50, _time.time() - 120 * 86400)  # exactly 120 days old
        ),
        get_wallet_age_days=AsyncMock(return_value=120.0),  # all same age
    )
    flag, details = asyncio.run(wallet_analysis.check("mint", client, Config))
    assert flag is True
    assert details["wallet_age_std_days"] < 1  # ~0
    assert any("std dev" in r.lower() or "seragam" in r.lower() for r in details["reasons"])


def test_wallet_analysis_diverse_old_wallets_ok():
    """Wallet tua dengan umur bervariasi → tidak flag."""
    from filters import wallet_analysis
    client = _make_mock_client()  # defaults: ages 100-400 days, varied
    flag, details = asyncio.run(wallet_analysis.check("mint", client, Config))
    assert flag is False
    assert details["avg_wallet_age_days"] > 30


# ── MCap:Liq Ratio Tests ──────────────────────────────────────────────────

def test_entry_quality_ratio_flag():
    """MCap:Liq ratio terlalu tinggi → flag."""
    from filters import entry_quality
    dex_data = {
        "baseToken": {"symbol": "INFLATED"},
        "priceUsd": "0.01",
        "marketCap": 100_000,
        "liquidity": {"usd": 5_000},  # ratio = 20x, way above 2.5x
        "volume": {"m5": 100, "h1": 500},
    }
    client = _make_mock_client()
    flag, details = asyncio.run(entry_quality.check("m", client, Config, dex_data))
    assert flag is True
    assert details["mcap_liq_ratio"] == 20.0
    assert any("ratio" in r.lower() or "inflated" in r.lower() for r in details["reasons"])


def test_entry_quality_ratio_ok():
    """MCap:Liq ratio 1.5x → ok."""
    from filters import entry_quality
    dex_data = {
        "baseToken": {"symbol": "HEALTHY"},
        "priceUsd": "0.001",
        "marketCap": 45_000,
        "liquidity": {"usd": 30_000},  # ratio = 1.5x
        "volume": {"m5": 500, "h1": 3000},
    }
    client = _make_mock_client()
    flag, details = asyncio.run(entry_quality.check("m", client, Config, dex_data))
    assert flag is False
    assert details["mcap_liq_ratio"] == 1.5


# ── Bundle Detection Tests ────────────────────────────────────────────────

def test_bundle_detection_clean():
    """Semua holder beli di slot berbeda → no bundle flag."""
    from filters import bundle_detection
    client = _make_mock_client()
    holders = [
        {"address": f"acc{i}", "amount": str(100_000 - i*1000)}
        for i in range(10)
    ]
    flag, details = asyncio.run(bundle_detection.check("mint", client, Config, holders))
    assert flag is False
    assert details.get("bundle_detected") is False


def test_bundle_detection_flagged():
    """Multiple holders di slot yang sama → bundle flag."""
    from filters import bundle_detection
    same_slot = 999_999
    client = _make_mock_client(
        get_signatures_for_address=AsyncMock(return_value=[
            {"signature": "bundled_sig", "slot": same_slot}
        ]),
    )
    holders = [
        {"address": f"acc{i}", "amount": str(100_000 - i*1000)}
        for i in range(5)
    ]
    flag, details = asyncio.run(bundle_detection.check("mint", client, Config, holders))
    assert flag is True
    assert details["max_same_slot"] == 5
    assert details["bundle_detected"] is True
    assert any("bundle" in r.lower() for r in details["reasons"])


def test_bundle_detection_below_threshold():
    """2 holders same slot (threshold=3) → no flag."""
    from filters import bundle_detection
    call_count = [0]
    def _side_effect(addr, limit=20):
        call_count[0] += 1
        # First 2 calls return same slot, rest different
        slot = 100 if call_count[0] <= 2 else call_count[0] * 1000
        return [{"signature": f"sig{call_count[0]}", "slot": slot}]
    client = _make_mock_client(
        get_signatures_for_address=AsyncMock(side_effect=_side_effect),
    )
    holders = [
        {"address": f"acc{i}", "amount": str(100_000 - i*1000)}
        for i in range(5)
    ]
    flag, details = asyncio.run(bundle_detection.check("mint", client, Config, holders))
    assert flag is False
    assert details["max_same_slot"] == 2


def test_screener_bundle_causes_skip():
    """Bundle detected → auto SKIP."""
    r = SolanaTokenResult(mint="x", bundle_flag=True)
    assert r.decision == "SKIP"


# ── Pump.fun Client Tests ──────────────────────────────────────────────────

def _build_bonding_curve_data(
    virtual_token: int = 1_073_000_000 * 10**6,
    virtual_sol: int = 30 * 1_000_000_000,
    real_token: int = 793_100_000 * 10**6,
    real_sol: int = 0,
    total_supply: int = 1_000_000_000 * 10**6,
    complete: bool = False,
) -> str:
    """Build fake bonding curve account data (base64)."""
    buf = bytearray(81)
    # discriminator 0..7
    struct.pack_into("<Q", buf, 8,  virtual_token)
    struct.pack_into("<Q", buf, 16, virtual_sol)
    struct.pack_into("<Q", buf, 24, real_token)
    struct.pack_into("<Q", buf, 32, real_sol)
    struct.pack_into("<Q", buf, 40, total_supply)
    buf[48] = 1 if complete else 0
    return base64.b64encode(bytes(buf)).decode()


def test_pumpfun_pda_deterministic():
    """PDA derivation harus deterministic untuk mint yang sama."""
    from pumpfun_client import derive_bonding_curve_pda
    mint = "Fh7mLxtPAysdvHcMcJ37A3vc6WvBVh7JVDwxmwk6pump"
    pda1 = derive_bonding_curve_pda(mint)
    pda2 = derive_bonding_curve_pda(mint)
    assert pda1 == pda2
    assert len(pda1) >= 32  # base58 pubkey


def test_pumpfun_parse_bonding_curve():
    """Parse bonding curve layout."""
    from pumpfun_client import parse_bonding_curve_account
    data = _build_bonding_curve_data(
        virtual_token=1_073_000_000 * 10**6,
        virtual_sol=30 * 1_000_000_000,
        real_token=500_000_000 * 10**6,
        real_sol=5 * 1_000_000_000,
        total_supply=1_000_000_000 * 10**6,
        complete=False,
    )
    state = parse_bonding_curve_account("mint123", data)
    assert state is not None
    assert state.virtual_token_reserves == 1_073_000_000 * 10**6
    assert state.virtual_sol_reserves == 30 * 1_000_000_000
    assert state.real_sol_reserves == 5 * 1_000_000_000
    assert state.complete is False


def test_pumpfun_parse_complete_flag():
    """Complete=True harus ter-parse benar."""
    from pumpfun_client import parse_bonding_curve_account
    data = _build_bonding_curve_data(complete=True)
    state = parse_bonding_curve_account("mint123", data)
    assert state.complete is True


def test_pumpfun_parse_invalid():
    """Data invalid → None."""
    from pumpfun_client import parse_bonding_curve_account
    assert parse_bonding_curve_account("mint", "") is None
    assert parse_bonding_curve_account("mint", "aW52YWxpZA==") is None


def test_pumpfun_market_cap_sol():
    """Market cap SOL formula: (supply × virtual_sol) / virtual_token / LAMPORTS."""
    from pumpfun_client import BondingCurveState
    state = BondingCurveState(
        mint="x",
        virtual_token_reserves=1_073_000_000 * 10**6,
        virtual_sol_reserves=30 * 1_000_000_000,
        real_token_reserves=793_100_000 * 10**6,
        real_sol_reserves=0,
        token_total_supply=1_000_000_000 * 10**6,
        complete=False,
    )
    mcap_sol = state.market_cap_sol()
    # (1e9 × 30) / 1.073e9 ≈ 27.96 SOL
    assert 27 < mcap_sol < 29


def test_pumpfun_progress_pct():
    """Progress pct naik seiring real_token turun."""
    from pumpfun_client import BondingCurveState
    # Fresh: belum ada yang beli
    fresh = BondingCurveState(
        mint="x", virtual_token_reserves=1, virtual_sol_reserves=1,
        real_token_reserves=793_100_000 * 10**6, real_sol_reserves=0,
        token_total_supply=1, complete=False,
    )
    assert fresh.progress_pct() < 1.0

    # Setengah terjual
    half = BondingCurveState(
        mint="x", virtual_token_reserves=1, virtual_sol_reserves=1,
        real_token_reserves=(793_100_000 // 2) * 10**6, real_sol_reserves=0,
        token_total_supply=1, complete=False,
    )
    assert 49 < half.progress_pct() < 51


def test_pumpfun_liquidity_sol():
    """Liquidity = real_sol_reserves / LAMPORTS."""
    from pumpfun_client import BondingCurveState
    state = BondingCurveState(
        mint="x", virtual_token_reserves=1, virtual_sol_reserves=1,
        real_token_reserves=1, real_sol_reserves=12_500_000_000,  # 12.5 SOL
        token_total_supply=1, complete=False,
    )
    assert abs(state.liquidity_sol() - 12.5) < 0.001


def test_pumpfun_get_market_data_shape():
    """get_market_data output kompatibel DexScreener shape."""
    import pumpfun_client
    from pumpfun_client import PumpFunClient
    bc_data = _build_bonding_curve_data(
        virtual_token=1_073_000_000 * 10**6,
        virtual_sol=30 * 1_000_000_000,
        real_token=500_000_000 * 10**6,
        real_sol=5 * 1_000_000_000,
        total_supply=1_000_000_000 * 10**6,
    )
    solana = MagicMock()
    solana._rpc = AsyncMock(return_value={"value": {"data": [bc_data, "base64"]}})
    pf = PumpFunClient(solana)
    result = asyncio.run(pf.get_market_data(
        "Fh7mLxtPAysdvHcMcJ37A3vc6WvBVh7JVDwxmwk6pump", sol_price_usd=150.0
    ))
    assert "priceUsd" in result
    assert "marketCap" in result
    assert result["liquidity"]["sol"] == 5.0
    assert result["liquidity"]["usd"] == 750.0  # 5 SOL × $150
    assert result["source"] == "pumpfun_bonding_curve"
    assert "bonding_curve" in result
    assert result["bonding_curve"]["complete"] is False


def test_pumpfun_get_market_data_missing_account():
    """Account tidak ada → return empty dict."""
    from pumpfun_client import PumpFunClient
    solana = MagicMock()
    solana._rpc = AsyncMock(return_value={"value": None})
    pf = PumpFunClient(solana)
    result = asyncio.run(pf.get_market_data(
        "Fh7mLxtPAysdvHcMcJ37A3vc6WvBVh7JVDwxmwk6pump", sol_price_usd=150.0
    ))
    assert result == {}


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
