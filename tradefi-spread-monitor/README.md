# TradFi Spread Monitor

Compare **traditional finance** spot prices (Yahoo) with:

- **Binance bStocks** (tokenized equities / ETFs)
- **Hyperliquid `xyz:` HIP-3** equity & commodity perps (trade.xyz)

Inspired by weekend TradFi dislocations (e.g. HK `MINIMAX` / `ZHIPU` moving on crypto liquidations while the cash market is closed).

## Thresholds

| Level | Abs spread |
|-------|------------|
| WATCH | ≥ 5% |
| ALERT | ≥ 10% |

Spreads reported:

- `BN-TF` — Binance vs TradFi
- `HL-TF` — Hyperliquid mark vs TradFi
- `HL-BN` — Hyperliquid vs Binance

## Run

```bash
python3 tradefi-spread-monitor/monitor.py
python3 tradefi-spread-monitor/monitor.py --json
python3 tradefi-spread-monitor/monitor.py --loop 60
```

No third-party packages required (Python 3.10+).

## Focus tickers

| Crypto venue | TradFi |
|--------------|--------|
| `xyz:MINIMAX` | HKEX `0100.HK` (HKD→USD) |
| `xyz:ZHIPU` | HKEX `2513.HK` (HKD→USD) |
| `xyz:SKHX` | KRX `000660.KS` (KRW→USD) |

Binance currently lists many US bStocks but not MiniMax / Zhipu.
