"""Tests for meme coin screener."""

from screener import MemeScreener, ScreeningConfig, ScreeningResult


def make_token(**overrides):
    """Helper: buat token data default yang lolos semua filter."""
    defaults = {
        "address": "0xTEST",
        "gas_fee_gwei": 20.0,
        "holder_count": 200,
        "funded_wallet_age_days": 30,
        "top_holder_pct": 5.0,
        "top10_combined_pct": 30.0,
        "market_cap_usd": 100_000,
    }
    defaults.update(overrides)
    return defaults


def test_all_clear_gas_it():
    """Token yang lolos semua filter -> GAS IT."""
    screener = MemeScreener()
    result = screener.screen(make_token())
    assert result.total_flags == 0
    assert result.decision == "GAS IT"


def test_one_flag_still_gas_it():
    """Satu flag masih GAS IT."""
    screener = MemeScreener()
    result = screener.screen(make_token(gas_fee_gwei=100))
    assert result.total_flags == 1
    assert result.decision == "GAS IT"


def test_two_flags_skip():
    """Dua flag -> SKIP."""
    screener = MemeScreener()
    result = screener.screen(make_token(gas_fee_gwei=100, holder_count=10))
    assert result.total_flags == 2
    assert result.decision == "SKIP"


def test_all_flags_skip():
    """Semua flag -> SKIP."""
    screener = MemeScreener()
    result = screener.screen(make_token(
        gas_fee_gwei=100,
        holder_count=10,
        top_holder_pct=80,
        market_cap_usd=5,
    ))
    assert result.total_flags == 4
    assert result.decision == "SKIP"


def test_gas_fee_flag():
    """Step 1: gas fee di atas threshold -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(gas_fee_gwei=51))
    assert result.gas_fee_flag is True

    result = screener.screen(make_token(gas_fee_gwei=50))
    assert result.gas_fee_flag is False


def test_holder_count_flag():
    """Step 2: holder count terlalu rendah -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(holder_count=50))
    assert result.holder_wallet_flag is True

    result = screener.screen(make_token(holder_count=100))
    assert result.holder_wallet_flag is False


def test_funded_wallet_age_flag():
    """Step 2: wallet age terlalu muda -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(funded_wallet_age_days=3))
    assert result.holder_wallet_flag is True

    result = screener.screen(make_token(funded_wallet_age_days=7))
    assert result.holder_wallet_flag is False


def test_top_holder_concentration_flag():
    """Step 3: top holder terlalu terkonsentrasi -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(top_holder_pct=20))
    assert result.top_holder_flag is True

    result = screener.screen(make_token(top_holder_pct=15))
    assert result.top_holder_flag is False


def test_top10_combined_flag():
    """Step 3: top 10 combined terlalu besar -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(top10_combined_pct=60))
    assert result.top_holder_flag is True

    result = screener.screen(make_token(top10_combined_pct=50))
    assert result.top_holder_flag is False


def test_market_cap_too_low_flag():
    """Step 4: market cap terlalu rendah -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(market_cap_usd=5_000))
    assert result.market_cap_flag is True


def test_market_cap_too_high_flag():
    """Step 4: market cap terlalu tinggi -> flag."""
    screener = MemeScreener()
    result = screener.screen(make_token(market_cap_usd=2_000_000))
    assert result.market_cap_flag is True


def test_market_cap_in_range():
    """Step 4: market cap dalam range -> OK."""
    screener = MemeScreener()
    result = screener.screen(make_token(market_cap_usd=500_000))
    assert result.market_cap_flag is False


def test_custom_config():
    """Config custom bisa mengubah threshold."""
    config = ScreeningConfig(
        max_gas_fee_gwei=100,
        min_holder_count=50,
        min_funded_wallet_age_days=3,
        max_top_holder_pct=30,
        max_top10_combined_pct=70,
        min_market_cap_usd=1_000,
        max_market_cap_usd=5_000_000,
    )
    screener = MemeScreener(config)
    # Token yang sebelumnya flag, sekarang OK dengan config longgar
    result = screener.screen(make_token(
        gas_fee_gwei=80,
        holder_count=60,
        funded_wallet_age_days=5,
        top_holder_pct=25,
        top10_combined_pct=60,
        market_cap_usd=3_000_000,
    ))
    assert result.total_flags == 0
    assert result.decision == "GAS IT"


def test_summary_output():
    """Summary menghasilkan string yang readable."""
    screener = MemeScreener()
    result = screener.screen(make_token())
    summary = result.summary()
    assert "Token: 0xTEST" in summary
    assert "GAS IT" in summary


if __name__ == "__main__":
    import sys
    test_functions = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    failed = 0
    for fn in test_functions:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
