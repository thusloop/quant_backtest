from __future__ import annotations

import base64
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "etf_rotation" / "outputs" / "macd_adx_backtest"
INITIAL_CASH = 100_000.0
START = pd.Timestamp("20210716")
END = pd.Timestamp("20260716")
COMMISSION_RATE = 0.0001
MIN_COMMISSION = 5.0
SLIPPAGE = 0.0001
ASSETS = {
    "半导体": {"code": "881121", "path": ROOT / "a_share" / "data" / "ths_industry_881121_20180101_20260618_none.csv", "name": "半导体行业指数"},
    "PCB": {"code": "885959", "path": ROOT / "etf_rotation" / "data" / "ths_concept" / "885959.csv", "name": "PCB概念指数"},
    "CPO": {"code": "886033", "path": ROOT / "etf_rotation" / "data" / "ths_concept" / "886033.csv", "name": "共封装光学(CPO)概念指数"},
}


def load_history(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame = frame.rename(columns={
        "日期": "date", "开盘价": "open", "收盘价": "close",
        "最高价": "high", "最低价": "low", "成交量": "volume", "成交额": "money",
    })
    required = ["date", "open", "close", "high", "low", "volume", "money"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"{path}缺少字段: {missing}")
    frame = frame[required].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    for column in required[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "close", "high", "low"]).sort_values("date").set_index("date")


def add_indicators(frame: pd.DataFrame, macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9, adx_period: int = 14) -> pd.DataFrame:
    macd_fast, macd_slow, macd_signal, adx_period = map(int, (macd_fast, macd_slow, macd_signal, adx_period))
    if not (2 <= macd_fast < macd_slow and 2 <= macd_signal <= 50 and 5 <= adx_period <= 60):
        raise ValueError("MACD/ADX参数不合法")
    result = frame.copy()
    close, high, low = result["close"], result["high"], result["low"]
    result["ema5"] = close.ewm(span=5, adjust=False, min_periods=5).mean()
    result["ema10"] = close.ewm(span=10, adjust=False, min_periods=10).mean()
    result["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    result["ema60"] = close.ewm(span=60, adjust=False, min_periods=60).mean()

    dif = close.ewm(span=macd_fast, adjust=False, min_periods=macd_slow).mean() - close.ewm(span=macd_slow, adjust=False, min_periods=macd_slow).mean()
    dea = dif.ewm(span=macd_signal, adjust=False, min_periods=macd_signal).mean()
    result["dif"], result["dea"] = dif, dea
    result["golden_cross"] = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    result["death_cross"] = (dif < dea) & (dif.shift(1) >= dea.shift(1))

    previous_close = close.shift(1)
    true_range = pd.concat([high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
    result["atr14"] = true_range.rolling(adx_period, min_periods=adx_period).mean()
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr_sum = true_range.rolling(adx_period, min_periods=adx_period).sum()
    plus_di = 100.0 * plus_dm.rolling(adx_period, min_periods=adx_period).sum() / tr_sum.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.rolling(adx_period, min_periods=adx_period).sum() / tr_sum.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    result["plus_di"], result["minus_di"] = plus_di, minus_di
    result["adx"] = dx.rolling(adx_period, min_periods=adx_period).mean()
    result["adx_high"] = result["adx"].cummax()
    result["long_alignment"] = (result["ema5"] > result["ema10"]) & (result["ema10"] > result["ema60"])
    result["market_trend"] = (result["adx"] >= 40) & result["long_alignment"]
    result["long_direction"] = (dif > 0) & (close > result["ema20"]) & (plus_di > minus_di)
    return result


def commission(value: float) -> float:
    return max(abs(value) * COMMISSION_RATE, MIN_COMMISSION)


def metrics(equity: pd.DataFrame, trades: pd.DataFrame) -> dict:
    values = equity.set_index("date")["equity"]
    returns = values.pct_change(fill_method=None)
    returns.iloc[0] = values.iloc[0] / INITIAL_CASH - 1.0
    years = max((values.index[-1] - values.index[0]).days / 365.2425, 1 / 365.2425)
    volatility = returns.std(ddof=1)
    realized = trades.loc[trades["side"] == "SELL", "realized_pnl"] if not trades.empty else pd.Series(dtype=float)
    return {
        "period_start": values.index[0].strftime("%Y-%m-%d"),
        "period_end": values.index[-1].strftime("%Y-%m-%d"),
        "final_equity": float(values.iloc[-1]),
        "total_return": float(values.iloc[-1] / INITIAL_CASH - 1.0),
        "annual_return": float((values.iloc[-1] / INITIAL_CASH) ** (1 / years) - 1.0),
        "max_drawdown": float((values / values.cummax() - 1.0).min()),
        "annual_volatility": float(volatility * math.sqrt(250)),
        "sharpe_zero_rf": float(returns.mean() / volatility * math.sqrt(250)) if volatility else 0.0,
        "trade_count": int(len(trades)),
        "buy_count": int((trades["side"] == "BUY").sum()) if not trades.empty else 0,
        "sell_count": int((trades["side"] == "SELL").sum()) if not trades.empty else 0,
        "commission_paid": float(trades["fee"].sum()) if not trades.empty else 0.0,
        "average_exposure": float((1.0 - equity["cash"] / equity["equity"]).mean()),
        "positive_sell_rate": float((realized > 0).mean()) if not realized.empty else 0.0,
        "partial_take_profit_count": int((trades.get("reason", pd.Series(dtype=str)) == "take_profit").sum()) if not trades.empty else 0,
    }


def run_long_only(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    dates = frame.index[(frame.index >= START) & (frame.index <= END)]
    cash, shares, average_cost = INITIAL_CASH, 0, 0.0
    entry_price, entry_atr, highest_close = np.nan, np.nan, np.nan
    profit_stage = 0
    rows, trades = [], []
    for date in dates:
        i = frame.index.get_loc(date)
        if i == 0:
            continue
        signal_date = frame.index[i - 1]
        signal = frame.loc[signal_date]
        open_price, close_price = float(frame.loc[date, "open"]), float(frame.loc[date, "close"])
        if shares > 0:
            # Use only information known at signal_date. The ATR trail adapts to
            # the current volatility while the high watermark prevents profits
            # from turning into an unbounded loss.
            highest_close = max(highest_close, float(signal.close)) if pd.notna(signal.close) else highest_close
            current_atr = float(signal.atr14) if pd.notna(signal.atr14) else entry_atr
            trailing_stop = highest_close - 3.0 * current_atr if pd.notna(current_atr) else -np.inf
            full_exit = bool(
                (signal.death_cross and signal.close < signal.ema20)
                or signal.close <= trailing_stop
            )
            if full_exit:
                quantity = shares
                execution_price = max(open_price - SLIPPAGE, 0.0001)
                gross = quantity * execution_price
                trade_fee = commission(gross)
                cash += gross - trade_fee
                realized = gross - trade_fee - average_cost * quantity
                shares = 0
                trades.append({"date": date, "signal_date": signal_date, "side": "SELL", "reason": "exit", "price": execution_price, "shares": quantity, "gross": gross, "fee": trade_fee, "realized_pnl": realized, "adx": signal.adx})
                average_cost, entry_price, entry_atr, highest_close, profit_stage = 0.0, np.nan, np.nan, np.nan, 0
            else:
                target_stage = 0
                if signal.close >= entry_price * 1.40:
                    target_stage = 1
                if signal.close >= entry_price * 1.80:
                    target_stage = 2
                if target_stage > profit_stage:
                    quantity = max(1, shares // 2)
                    execution_price = max(open_price - SLIPPAGE, 0.0001)
                    gross = quantity * execution_price
                    trade_fee = commission(gross)
                    cash += gross - trade_fee
                    realized = gross - trade_fee - average_cost * quantity
                    shares -= quantity
                    profit_stage = target_stage
                    trades.append({"date": date, "signal_date": signal_date, "side": "SELL", "reason": "take_profit", "price": execution_price, "shares": quantity, "gross": gross, "fee": trade_fee, "realized_pnl": realized, "adx": signal.adx})
                    if shares == 0:
                        average_cost, entry_price, entry_atr, highest_close = 0.0, np.nan, np.nan, np.nan

        # A golden cross is the trigger. ADX now grades position size instead
        # of blocking entries until the trend is already mature.
        if shares == 0 and bool(signal.golden_cross and pd.notna(signal.adx)):
            exposure = 1.00 if signal.adx >= 40 else (0.80 if signal.adx >= 20 else 0.60)
            quantity = int((cash * exposure) / (open_price * (1 + SLIPPAGE)))
            if quantity > 0:
                execution_price = open_price * (1 + SLIPPAGE)
                gross = quantity * execution_price
                trade_fee = commission(gross)
                if gross + trade_fee <= cash:
                    cash -= gross + trade_fee
                    shares = quantity
                    average_cost = (gross + trade_fee) / quantity
                    entry_price = execution_price
                    entry_atr = float(signal.atr14) if pd.notna(signal.atr14) else 0.0
                    highest_close = float(signal.close)
                    profit_stage = 0
                    trades.append({"date": date, "signal_date": signal_date, "side": "BUY", "reason": "macd_golden_cross", "price": execution_price, "shares": quantity, "gross": gross, "fee": trade_fee, "realized_pnl": np.nan, "adx": signal.adx, "exposure": exposure})
        equity_value = cash + shares * close_price
        rows.append({"date": date, "cash": cash, "shares": shares, "equity": equity_value, "adx": signal.adx, "dif": signal.dif, "dea": signal.dea, "ema20": signal.ema20, "target_exposure": 0.0 if equity_value <= 0 else shares * close_price / equity_value})
    equity = pd.DataFrame(rows)
    trades_frame = pd.DataFrame(trades)
    return equity, trades_frame, metrics(equity, trades_frame)


def buy_hold(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    dates = frame.index[(frame.index >= START) & (frame.index <= END)]
    first = dates[0]
    price = float(frame.loc[first, "open"]) * (1 + SLIPPAGE)
    shares = int(INITIAL_CASH / price)
    trade_fee = commission(shares * price)
    while shares > 0 and shares * price + trade_fee > INITIAL_CASH:
        shares -= 1
        trade_fee = commission(shares * price)
    cash = INITIAL_CASH - shares * price - trade_fee
    equity = pd.DataFrame({"date": dates, "equity": cash + shares * frame.loc[dates, "close"].to_numpy()})
    values = equity.set_index("date")["equity"]
    years = max((values.index[-1] - values.index[0]).days / 365.2425, 1 / 365.2425)
    return equity, {"annual_return": float((values.iloc[-1] / INITIAL_CASH) ** (1 / years) - 1.0), "total_return": float(values.iloc[-1] / INITIAL_CASH - 1.0), "max_drawdown": float((values / values.cummax() - 1.0).min())}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for asset, metadata in ASSETS.items():
        frame = add_indicators(load_history(metadata["path"]))
        equity, trades, strategy_summary = run_long_only(frame)
        benchmark, benchmark_summary = buy_hold(frame)
        asset_dir = OUTPUT_DIR / f"{metadata['code']}_{asset}"
        asset_dir.mkdir(parents=True, exist_ok=True)
        equity.to_csv(asset_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        trades.to_csv(asset_dir / "trades.csv", index=False, encoding="utf-8-sig")
        benchmark.to_csv(asset_dir / "buy_and_hold.csv", index=False, encoding="utf-8-sig")
        (asset_dir / "summary.json").write_text(json.dumps({"metadata": metadata, "strategy": strategy_summary, "buy_and_hold": benchmark_summary}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        results[asset] = {"metadata": metadata, "equity": equity, "trades": trades, "strategy": strategy_summary, "benchmark": benchmark, "benchmark_summary": benchmark_summary}

    summary = {asset: {"metadata": item["metadata"], "strategy": item["strategy"], "buy_and_hold": item["benchmark_summary"]} for asset, item in results.items()}
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    render_report(results)
    print(json.dumps(summary, ensure_ascii=False, default=str))
    print(f"results: {OUTPUT_DIR.resolve()}")


def render_report(results: dict) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.figure(figsize=(13, 7))
    for asset, item in results.items():
        plt.plot(item["equity"]["date"], item["equity"]["equity"] / INITIAL_CASH, linewidth=1.5, label=f"{asset}策略")
        plt.plot(item["benchmark"]["date"], item["benchmark"]["equity"] / INITIAL_CASH, linestyle="--", alpha=0.6, label=f"{asset}买入持有")
    plt.axhline(1.0, color="#9ca3af", linewidth=0.8)
    plt.grid(alpha=0.25)
    plt.legend(fontsize=8, ncol=2)
    plt.title("MACD+ADX趋势策略：半导体、PCB、CPO")
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "equity_comparison.png", dpi=160)
    plt.close()
    chart = base64.b64encode((OUTPUT_DIR / "equity_comparison.png").read_bytes()).decode("ascii")
    rows = "".join(
        f"<tr><td>{asset}</td><td>{item['metadata']['code']}</td><td>{item['strategy']['annual_return']:+.2%}</td><td>{item['strategy']['max_drawdown']:.2%}</td><td>{item['strategy']['sharpe_zero_rf']:.2f}</td><td>{item['strategy']['trade_count']}</td><td>{item['strategy']['partial_take_profit_count']}</td><td>{item['benchmark_summary']['annual_return']:+.2%}</td></tr>"
        for asset, item in results.items()
    )
    html = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>MACD增强策略回测</title><style>body{{margin:0;background:#eef1f4;color:#17202a;font-family:'Microsoft YaHei',Arial,sans-serif}}main{{width:min(1300px,calc(100% - 24px));margin:20px auto}}section{{background:#fff;border:1px solid #d8dee7;border-radius:6px;padding:16px;margin-top:16px}}img{{width:100%;display:block}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:9px;border-bottom:1px solid #e5e8ed;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}.note{{line-height:1.75;color:#475467}}@media(max-width:800px){{section{{overflow:auto}}table{{min-width:950px}}}}</style></head>
<body><main><h1>MACD增强策略回测</h1><p>信号使用前一交易日收盘，次日开盘执行；仅做多，不使用未来数据。</p>
<section><img src='data:image/png;base64,{chart}' alt='策略权益曲线'></section><section><h2>回测结果</h2><table><thead><tr><th>标的</th><th>代码</th><th>策略年化</th><th>策略回撤</th><th>策略夏普</th><th>交易次数</th><th>分批止盈次数</th><th>买入持有年化</th></tr></thead><tbody>{rows}</tbody></table></section>
<section class='note'><h2>策略规则</h2><p>入场：MACD DIF上穿DEA（金叉）；ADX不再作为硬性过滤，而是用于仓位分级：ADX&lt;20使用60%，20-40使用80%，ADX≥40使用100%。</p><p>出场：MACD死叉且收盘价跌破EMA20，或收盘价跌破持仓期间最高收盘价下方3×ATR14的移动止损。盈利达到40%和80%时各减半。</p><p>标的为881121半导体、885959 PCB、886033 CPO行业/概念指数，回测结果不代表实际ETF的跟踪误差和流动性。</p></section></main></body></html>"""
    (OUTPUT_DIR / "report.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
