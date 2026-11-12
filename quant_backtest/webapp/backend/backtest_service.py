from __future__ import annotations

import numpy as np
import pandas as pd

import macd_adx_semiconductor_web as strategy
import macd_adx_backtest as base

from .strategy_service import list_strategies


def _series(frame: pd.DataFrame) -> dict:
    return {"date": [x.strftime("%Y-%m-%d") for x in frame["date"]], "equity": [float(x) for x in frame["equity"]]}


def _metrics(curve: pd.DataFrame) -> dict:
    values = curve.set_index("date")["equity"]
    returns = values.pct_change().fillna(0.0)
    total = values.iloc[-1] / base.INITIAL_CASH - 1
    years = max((values.index[-1] - values.index[0]).days / 365.2425, 1 / 365.2425)
    vol = returns.std(ddof=1) * np.sqrt(250)
    return {
        "period_start": values.index[0].strftime("%Y-%m-%d"), "period_end": values.index[-1].strftime("%Y-%m-%d"),
        "total_return": float(total), "annual_return": float((1 + total) ** (1 / years) - 1),
        "max_drawdown": float((values / values.cummax() - 1).min()),
        "sharpe": float(returns.mean() / returns.std(ddof=1) * np.sqrt(250)) if vol else 0.0,
    }


def _trade_records(trades: pd.DataFrame) -> list[dict]:
    if trades.empty:
        return []
    output = trades.copy()
    for column in ("date", "signal_date"):
        if column in output.columns:
            parsed = pd.to_datetime(output[column], errors="coerce")
            output[column] = parsed.dt.strftime("%Y-%m-%d").fillna(output[column].astype(str))
    return output.fillna("").to_dict("records")


def _strategy_frame(frame: pd.DataFrame, definition: dict) -> pd.DataFrame:
    params = definition.get("params") or {}
    if definition.get("base_key") == "buy_hold" or not params:
        return frame
    return base.add_indicators(
        frame[["open", "close", "high", "low", "volume", "money"]].copy(),
        macd_fast=params.get("macd_fast", 12),
        macd_slow=params.get("macd_slow", 26),
        macd_signal=params.get("macd_signal", 9),
        adx_period=params.get("adx_period", 14),
    )


def build(code: str, frame: pd.DataFrame, definitions: list[dict] | None = None) -> dict:
    if frame.empty or not (frame.index >= strategy.START).any():
        raise ValueError("没有可回测的日线，请提供 2021-07-16 之后的数据")
    hold_curve = strategy.build_buy_hold_curve(frame)
    built = {}
    for definition in list_strategies() if definitions is None else definitions:
        if not definition.get("enabled", True):
            continue
        key = definition["key"]
        base_key = definition.get("base_key", key)
        strategy_frame = _strategy_frame(frame, definition)
        if base_key == "close":
            curve, trade_frame, summary = strategy.close_execution_backtest(strategy_frame)
            preview = strategy.preview_action(strategy_frame)
            metrics = summary
        elif base_key == "macd_cross":
            curve, trade_frame = strategy.build_macd_cross_curve(strategy_frame)
            preview = strategy.preview_macd_cross(strategy_frame)
            metrics = _metrics(curve)
        elif base_key == "buy_hold":
            curve, trade_frame, preview = hold_curve, pd.DataFrame(), {"action": "HOLD", "execution_label": "无主动操作", "reason": "一直持有，不产生择时买卖", "signal_date": "-", "position": "持有"}
            metrics = _metrics(curve)
        else:
            continue
        built[key] = {"definition": definition, "curve": curve, "trades": trade_frame, "metrics": metrics, "preview": preview}

    curves = {}
    trades = {}
    previews = {}
    for key, item in built.items():
        definition = item["definition"]
        curves[key] = {"label": definition["name"], "color": definition.get("color", "#0f766e"), "data": _series(item["curve"]), "metrics": item["metrics"]}
        trades[key] = _trade_records(item["trades"])
        previews[key] = item["preview"]
    return {"code": code, "curves": curves, "trades": trades, "previews": previews, "strategies": [item["definition"] for item in built.values()], "initial": float(base.INITIAL_CASH), "latest": frame.index[-1].strftime("%Y-%m-%d")}
