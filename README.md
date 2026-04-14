# Meme Coin Screener

Strategi **4 Filter Sebelum Scalp New Pair** untuk menyaring koin meme baru yang layak di-scalp.

## 4 Filter

| Step | Filter | Deskripsi |
|------|--------|-----------|
| 1 | Cek Global Gas Fee | Gas fee terlalu tinggi = profit habis dimakan fee |
| 2 | Holder & Funded Wallet Age | Holder sedikit atau wallet baru = potensi rug pull |
| 3 | Cek Balance Top Holder | Kepemilikan terkonsentrasi = risiko dump |
| 4 | Cek Entry Market Cap | Market cap di luar range optimal = risk/reward buruk |

## Keputusan

- **2+ flags = SKIP** - Koin terlalu berisiko, selalu ada koin baru tiap menit
- **0-1 flag = GAS IT** - Potensi scalp 60-70% di new pair fresh launch

## Usage

```python
from screener import MemeScreener

screener = MemeScreener()
result = screener.screen({
    "address": "0xABC...123",
    "gas_fee_gwei": 25.0,
    "holder_count": 250,
    "funded_wallet_age_days": 30,
    "top_holder_pct": 8.0,
    "top10_combined_pct": 35.0,
    "market_cap_usd": 150_000,
})

print(result.decision)  # "GAS IT" atau "SKIP"
print(result.summary())
```

## Run Demo

```bash
python3 screener.py
```

## Run Tests

```bash
python3 test_screener.py
```
