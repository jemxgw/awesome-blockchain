#!/usr/bin/env python3
"""Monitor TradFi vs Binance bStocks vs Hyperliquid xyz price spreads.

Thresholds:
  >= 5%  -> WATCH
  >= 10% -> ALERT

Usage:
  python3 monitor.py
  python3 monitor.py --json
  python3 monitor.py --loop 60
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

HL_INFO = "https://api.hyperliquid.xyz/info"
BN_PRODUCTS = (
    "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"

# Explicit TradFi feeds for non-US or ambiguous tickers.
YAHOO_OVERRIDE: dict[str, tuple[str, str]] = {
    "MINIMAX": ("0100.HK", "HKD"),
    "ZHIPU": ("2513.HK", "HKD"),
    "SKHX": ("000660.KS", "KRW"),
    "CAMBRICON": ("688256.SS", "CNY"),
    "GOLD": ("GC=F", "USD"),
    "SILVER": ("SI=F", "USD"),
    "COPPER": ("HG=F", "USD"),
    "NATGAS": ("NG=F", "USD"),
    "CL": ("CL=F", "USD"),
    "BRENTOIL": ("BZ=F", "USD"),
    "JPY": ("JPY=X", "USD"),  # Yahoo JPY=X is USD/JPY rate
}

# Binance bStock base -> underlying ticker when auto strip of trailing B fails.
BN_BASE_OVERRIDE: dict[str, str] = {
    "MUB": "MU",
    "SOXLB": "SOXL",
    "SOXSB": "SOXS",
    "TQQQB": "TQQQ",
    "SQQQB": "SQQQ",
    "QQQB": "QQQ",
    "SPYB": "SPY",
    "IWMB": "IWM",
    "EWYB": "EWY",
    "EWJB": "EWJ",
    "EWZB": "EWZ",
    "KORUB": "KORU",
    "DRAMB": "DRAM",
    "SNXXB": "SNXX",
    "ANTB": "ANT",
    "SPCXB": "SPCX",
    "BNCB": "BNC",
}

# Skip Yahoo auto-match for these HL names (ticker collisions / wrong asset).
SKIP_YAHOO_AUTO: set[str] = {
    "CATL",  # Catalyst Acquisition vs CATL
    "CORN",  # Teucrium ETF vs corn
    "SKHX",  # handled via override to KRX
    "GOLD",  # Gold.com stock vs bullion
    "ALUMINIUM",
    "PALLADIUM",
    "PLATINUM",
    "DXY",
    "EUR",
    "GBP",
    "KRW",
    "HO",
    "ICE",
    "NIFTY",
    "JP225",
    "KR200",
    "IBOV",
    "MAGS",
}

WATCH_PCT = 5.0
ALERT_PCT = 10.0
USER_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class SpreadRow:
    ticker: str
    tradefi_usd: float | None
    tradefi_symbol: str | None
    binance_px: float | None
    binance_symbol: str | None
    hl_mark: float | None
    hl_oracle: float | None
    bn_tf_pct: float | None
    hl_tf_pct: float | None
    hl_bn_pct: float | None
    max_abs_pct: float
    level: str  # OK | WATCH | ALERT
    notes: str


def http_get_json(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tradefi-spread-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def http_post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 tradefi-spread-monitor",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def yahoo_price(symbol: str) -> tuple[float, str]:
    payload = http_get_json(YAHOO_CHART.format(symbol=urllib.request.quote(symbol)))
    meta = payload["chart"]["result"][0]["meta"]
    px = meta.get("regularMarketPrice") or meta.get("previousClose")
    if px is None:
        raise ValueError(f"no price for {symbol}")
    return float(px), str(meta.get("currency") or "USD")


def fetch_fx() -> dict[str, float]:
    fx: dict[str, float] = {"USD": 1.0}
    for ccy, sym in (("HKD", "HKD=X"), ("CNY", "CNY=X"), ("KRW", "KRW=X")):
        try:
            px, _ = yahoo_price(sym)
            fx[ccy] = px
        except Exception as exc:  # noqa: BLE001
            print(f"warn: fx {ccy}: {exc}", file=sys.stderr)
    return fx


def to_usd(price: float, ccy: str, fx: dict[str, float]) -> float:
    if ccy == "USD":
        return price
    if ccy not in fx:
        raise KeyError(f"missing FX for {ccy}")
    # Yahoo FX pairs are quoted as XXX=X meaning USD/XXX for HKD/CNY/KRW
    return price / fx[ccy]


def bn_base_to_ticker(base: str) -> str:
    if base in BN_BASE_OVERRIDE:
        return BN_BASE_OVERRIDE[base]
    if base.endswith("B") and len(base) > 2:
        return base[:-1]
    return base


def fetch_binance_bstocks() -> dict[str, dict[str, Any]]:
    raw = http_get_json(BN_PRODUCTS)
    out: dict[str, dict[str, Any]] = {}
    for item in raw.get("data") or []:
        tags = item.get("tags") or []
        if "bStocks" not in tags:
            continue
        if item.get("st") != "TRADING" or item.get("q") != "USDT":
            continue
        ticker = bn_base_to_ticker(str(item["b"]))
        out[ticker] = {
            "symbol": item["s"],
            "base": item["b"],
            "name": item.get("an") or item["b"],
            "price": float(item["c"]),
            "quote_vol": float(item.get("qv") or 0),
        }
    return out


def fetch_hl_xyz() -> dict[str, dict[str, Any]]:
    payload = http_post_json(HL_INFO, {"type": "metaAndAssetCtxs", "dex": "xyz"})
    meta, ctxs = payload[0], payload[1]
    out: dict[str, dict[str, Any]] = {}
    for i, univ in enumerate(meta["universe"]):
        name = univ["name"]
        if not name.startswith("xyz:"):
            continue
        if univ.get("isDelisted"):
            continue
        short = name[4:]
        ctx = ctxs[i]
        mark = float(ctx.get("markPx") or 0)
        oracle = float(ctx.get("oraclePx") or 0)
        if mark <= 0:
            continue
        out[short] = {
            "name": name,
            "mark": mark,
            "oracle": oracle,
            "mid": float(ctx["midPx"]) if ctx.get("midPx") else None,
            "oi": float(ctx.get("openInterest") or 0),
            "vol": float(ctx.get("dayNtlVlm") or 0),
        }
    return out


def resolve_yahoo_symbol(ticker: str) -> tuple[str, str] | None:
    if ticker in YAHOO_OVERRIDE:
        return YAHOO_OVERRIDE[ticker]
    if ticker in SKIP_YAHOO_AUTO:
        return None
    # US-style tickers only for auto match
    if not ticker.replace(".", "").isalnum():
        return None
    if len(ticker) > 5:
        return None
    return ticker, "USD"


def pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0


def classify(max_abs: float) -> str:
    if max_abs >= ALERT_PCT:
        return "ALERT"
    if max_abs >= WATCH_PCT:
        return "WATCH"
    return "OK"


def build_rows() -> tuple[list[SpreadRow], dict[str, Any]]:
    fx = fetch_fx()
    bn = fetch_binance_bstocks()
    hl = fetch_hl_xyz()

    tickers = sorted(set(bn) | set(hl) | set(YAHOO_OVERRIDE))
    yahoo_jobs: dict[str, tuple[str, str]] = {}
    for t in tickers:
        resolved = resolve_yahoo_symbol(t)
        if resolved:
            yahoo_jobs[t] = resolved

    tradefi: dict[str, dict[str, Any]] = {}

    def _one(t: str, sym: str, ccy: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            raw, detected = yahoo_price(sym)
            use_ccy = ccy if ccy != "USD" else detected
            # JPY=X is already a USD quote of JPY; treat as USD price of the HL JPY market
            if sym == "JPY=X":
                usd = raw
                use_ccy = "USD"
            else:
                usd = to_usd(raw, use_ccy if use_ccy in fx or use_ccy == "USD" else ccy, fx)
            return t, {"symbol": sym, "raw": raw, "usd": usd, "ccy": use_ccy}, None
        except Exception as exc:  # noqa: BLE001
            return t, None, str(exc)

    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = [pool.submit(_one, t, sym, ccy) for t, (sym, ccy) in yahoo_jobs.items()]
        for fut in as_completed(futs):
            t, data, err = fut.result()
            if data:
                tradefi[t] = data
            elif err and t in ("MINIMAX", "ZHIPU", "NVDA", "MSTR", "SKHX"):
                print(f"warn: yahoo {t}: {err}", file=sys.stderr)

    rows: list[SpreadRow] = []
    for t in tickers:
        b = bn.get(t)
        h = hl.get(t)
        y = tradefi.get(t)
        if not b and not h:
            continue
        # Need at least one comparable pair
        if not ((y and b) or (y and h) or (b and h)):
            continue

        bn_tf = pct(b["price"], y["usd"]) if b and y else None
        hl_tf = pct(h["mark"], y["usd"]) if h and y else None
        hl_bn = pct(h["mark"], b["price"]) if h and b and b["price"] else None

        notes: list[str] = []
        hl_liquid = bool(h and (h["vol"] > 0 or h["oi"] > 0))
        if h and not hl_liquid:
            notes.append("hl-illiquid")
            # Ignore stale/delisted-residual HL marks for thresholding
            hl_tf = None
            hl_bn = None

        candidates = [x for x in (bn_tf, hl_tf, hl_bn) if x is not None]
        max_abs = max((abs(x) for x in candidates), default=0.0)
        level = classify(max_abs)
        rows.append(
            SpreadRow(
                ticker=t,
                tradefi_usd=y["usd"] if y else None,
                tradefi_symbol=y["symbol"] if y else None,
                binance_px=b["price"] if b else None,
                binance_symbol=b["symbol"] if b else None,
                hl_mark=h["mark"] if h else None,
                hl_oracle=h["oracle"] if h else None,
                bn_tf_pct=bn_tf,
                hl_tf_pct=hl_tf,
                hl_bn_pct=hl_bn,
                max_abs_pct=max_abs,
                level=level,
                notes=",".join(notes),
            )
        )

    rows.sort(key=lambda r: r.max_abs_pct, reverse=True)
    meta = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "as_of_shanghai": datetime.now(USER_TZ).isoformat(),
        "fx": fx,
        "binance_count": len(bn),
        "hl_count": len(hl),
        "watch_pct": WATCH_PCT,
        "alert_pct": ALERT_PCT,
        "focus": ["MINIMAX", "ZHIPU"],
    }
    return rows, meta


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "   -   "
    return f"{v:+7.2f}%"


def fmt_px(v: float | None) -> str:
    if v is None:
        return "      -"
    if v >= 1000:
        return f"{v:7.1f}"
    if v >= 100:
        return f"{v:7.2f}"
    return f"{v:7.3f}"


def print_report(rows: list[SpreadRow], meta: dict[str, Any]) -> None:
    print(f"TradFi ↔ Binance bStocks ↔ Hyperliquid xyz")
    print(f"as of {meta['as_of_shanghai']} (Asia/Shanghai)")
    print(f"thresholds: WATCH>={WATCH_PCT:.0f}%  ALERT>={ALERT_PCT:.0f}%")
    print()

    focus = {t: None for t in meta["focus"]}
    for r in rows:
        if r.ticker in focus:
            focus[r.ticker] = r

    print("=== FOCUS (tweet: MiniMax / Zhipu) ===")
    for t, r in focus.items():
        if not r:
            print(f"  {t}: no data")
            continue
        print(
            f"  {t}: TF({r.tradefi_symbol})={fmt_px(r.tradefi_usd).strip()} "
            f"HL={fmt_px(r.hl_mark).strip()} "
            f"HL-TF={fmt_pct(r.hl_tf_pct).strip()}  [{r.level}]"
        )
    print()

    watch = [r for r in rows if r.level in ("WATCH", "ALERT")]
    alerts = [r for r in rows if r.level == "ALERT"]
    print(f"=== SPREADS >= {WATCH_PCT:.0f}%  (ALERT if >= {ALERT_PCT:.0f}%)  count={len(watch)} alert={len(alerts)} ===")
    print(
        f"{'lv':5} {'tkr':8} {'TF$':>8} {'BN':>8} {'HL':>8} "
        f"{'BN-TF':>8} {'HL-TF':>8} {'HL-BN':>8} notes"
    )
    for r in watch:
        print(
            f"{r.level:5} {r.ticker:8} {fmt_px(r.tradefi_usd)} {fmt_px(r.binance_px)} "
            f"{fmt_px(r.hl_mark)} {fmt_pct(r.bn_tf_pct)} {fmt_pct(r.hl_tf_pct)} "
            f"{fmt_pct(r.hl_bn_pct)} {r.notes}"
        )
    print()
    print("=== Top liquid pairs by |spread| (vol filter via presence on BN or HL) ===")
    shown = 0
    for r in rows:
        if r.max_abs_pct < 2:
            continue
        if r.level == "OK" and shown >= 25:
            continue
        if r.level == "OK":
            shown += 1
        print(
            f"{r.level:5} {r.ticker:8} TF={fmt_px(r.tradefi_usd).strip()} "
            f"BN={fmt_px(r.binance_px).strip()} HL={fmt_px(r.hl_mark).strip()} "
            f"BN-TF={fmt_pct(r.bn_tf_pct).strip()} HL-TF={fmt_pct(r.hl_tf_pct).strip()} "
            f"HL-BN={fmt_pct(r.hl_bn_pct).strip()}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="TradFi / Binance / Hyperliquid spread monitor")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--loop", type=int, default=0, help="repeat every N seconds (0=once)")
    parser.add_argument("--min", type=float, default=WATCH_PCT, help="min abs %% to include in JSON watchlist")
    args = parser.parse_args()

    while True:
        try:
            rows, meta = build_rows()
        except urllib.error.URLError as exc:
            print(f"network error: {exc}", file=sys.stderr)
            if args.loop <= 0:
                return 1
            time.sleep(args.loop)
            continue

        if args.json:
            watch = [asdict(r) for r in rows if r.max_abs_pct >= args.min]
            print(json.dumps({"meta": meta, "watch": watch, "all": [asdict(r) for r in rows]}, indent=2))
        else:
            print_report(rows, meta)

        if args.loop <= 0:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
