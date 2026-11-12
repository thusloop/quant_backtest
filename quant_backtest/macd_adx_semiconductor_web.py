# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json
import re
from datetime import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

import macd_adx_backtest as strategy
from market_time import market_now
from history_cache import write_history


ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "a_share" / "data" / "ths_industry_881121_20180101_20260618_none.csv"
OUTPUT_DIR = ROOT / "etf_rotation" / "outputs" / "macd_adx_semiconductor_web"
CODE = "881121"
NAME = "半导体行业指数"
START = pd.Timestamp("2021-07-16")
END = pd.Timestamp("2099-12-31")
BASELINE_END = pd.Timestamp("2026-06-17")
SLIPPAGE = strategy.SLIPPAGE


def normalize_download(raw: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "date", "开盘": "open", "开盘价": "open", "收盘": "close", "收盘价": "close", "最高": "high", "最高价": "high", "最低": "low", "最低价": "low",
        "成交量": "volume", "成交额": "money", "鏃ユ湡": "date", "寮€鐩樹环": "open",
        "鏀剁洏浠?:": "close", "鏈€楂樹环": "high", "鏈€浣庝环": "low",
        "鎴愪氦閲?:": "volume", "鎴愪氦棰?:": "money",
    }
    frame = raw.rename(columns=rename).copy()
    required = ["date", "open", "close", "high", "low", "volume", "money"]
    if any(col not in frame.columns for col in required):
        raise RuntimeError(f"AkShare返回字段不完整: {list(frame.columns)}")
    frame = frame[required]
    frame["date"] = pd.to_datetime(frame["date"])
    for col in required[1:]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date").drop_duplicates("date")


def sync_data() -> dict:
    """Refresh the local cache; fall back to the last cache when offline."""
    today = pd.Timestamp(market_now().date())
    status = {"attempted_at": market_now().isoformat(timespec="seconds"), "status": "cached", "source": "local cache", "error": ""}
    existing = pd.DataFrame()
    if DATA_PATH.exists():
        try:
            existing = strategy.load_history(DATA_PATH).reset_index()
        except Exception as exc:
            status["error"] = f"读取本地缓存失败: {exc}"
    try:
        import akshare as ak

        raw = ak.stock_board_industry_index_ths(
            symbol="半导体", start_date="20180101", end_date=today.strftime("%Y%m%d")
        )
        downloaded = normalize_download(raw)
        merged = pd.concat([existing, downloaded], ignore_index=True).drop_duplicates("date", keep="last").sort_values("date")
        write_history(merged, DATA_PATH)
        status.update({"status": "synced", "source": "AkShare", "downloaded_rows": int(len(downloaded))})
    except Exception as exc:
        status.update({"status": "cached", "error": f"同步失败，使用本地缓存: {exc}"})
    status["cache_path"] = str(DATA_PATH)
    return status


def load_frame() -> pd.DataFrame:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"找不到数据缓存: {DATA_PATH}")
    return strategy.add_indicators(strategy.load_history(DATA_PATH))


def fetch_realtime_snapshot() -> tuple[dict | None, str]:
    """Fetch the current board quote used for the provisional intraday bar."""
    eastmoney_error = ""
    try:
        import akshare as ak

        raw = ak.stock_board_industry_spot_em(symbol="半导体")
        values = dict(zip(raw["item"].astype(str), pd.to_numeric(raw["value"], errors="coerce")))
        latest = float(values.get("最新", np.nan))
        if not np.isfinite(latest) or latest <= 0:
            raise RuntimeError(f"实时行情缺少有效最新价: {values}")
        return {
            "latest": latest,
            "open": float(values.get("开盘", latest)),
            "high": float(values.get("最高", latest)),
            "low": float(values.get("最低", latest)),
            "volume": float(values.get("成交量", 0.0)),
            "money": float(values.get("成交额", 0.0)),
            "quote_time": market_now().isoformat(timespec="seconds"),
            "source": "东方财富",
        }, "实时行情已获取"
    except Exception as exc:
        eastmoney_error = str(exc)

    # AkShare's board quote depends on EastMoney. Use Tonghuashun's public
    # realhead endpoint as a fallback when that request is blocked or times
    # out. Field 10 is latest, 7 is today's open, 8/9 are high/low, and 13/19
    # are volume/amount for a board index.
    try:
        url = "https://d.10jqka.com.cn/v2/realhead/bk_881121/last.js"
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"}, timeout=10)
        response.raise_for_status()
        text = response.text
        payload = json.loads(text[text.find("(") + 1:text.rfind(")")])
        values = payload.get("items", {})
        latest = float(values.get("10", "nan"))
        if not np.isfinite(latest) or latest <= 0:
            raise RuntimeError(f"同花顺实时接口缺少最新价: {values}")
        return {
            "latest": latest,
            "open": float(values.get("7") or latest),
            "high": float(values.get("8") or latest),
            "low": float(values.get("9") or latest),
            "volume": float(values.get("13") or 0.0),
            "money": float(values.get("19") or 0.0),
            "quote_time": payload.get("updateTime") or payload.get("time") or market_now().isoformat(timespec="seconds"),
            "source": "同花顺",
        }, f"同花顺实时行情已获取（东方财富失败后备用）"
    except Exception as exc:
        return None, f"东方财富失败: {eastmoney_error}；同花顺备用也失败: {exc}"


def append_realtime_bar(frame: pd.DataFrame, snapshot: dict | None) -> pd.DataFrame:
    if not snapshot:
        return frame
    today = pd.Timestamp(market_now().date())
    if today <= frame.index[-1]:
        return frame
    previous_close = float(frame["close"].iloc[-1])
    row = pd.DataFrame([{
        "date": today,
        "open": snapshot.get("open", previous_close),
        "close": snapshot["latest"],
        "high": max(snapshot.get("high", snapshot["latest"]), snapshot["latest"]),
        "low": min(snapshot.get("low", snapshot["latest"]), snapshot["latest"]),
        "volume": snapshot.get("volume", 0.0),
        "money": snapshot.get("money", 0.0),
    }]).set_index("date")
    combined = pd.concat([frame[["open", "close", "high", "low", "volume", "money"]], row])
    return strategy.add_indicators(combined)


def close_execution_backtest(frame: pd.DataFrame, return_state: bool = False):
    dates = frame.index[(frame.index >= START) & (frame.index <= END)]
    cash, shares, average_cost = strategy.INITIAL_CASH, 0, 0.0
    entry_price = entry_atr = highest_close = np.nan
    profit_stage = 0
    rows, trades = [], []

    def execute_sell(date, signal_date, signal, reason, price, quantity):
        nonlocal cash, shares, average_cost, entry_price, entry_atr, highest_close, profit_stage
        gross = quantity * price
        fee = strategy.commission(gross)
        realized = gross - fee - average_cost * quantity
        cash += gross - fee
        shares -= quantity
        trades.append({"date": date, "signal_date": signal_date, "side": "SELL", "reason": reason, "price": price, "shares": quantity, "gross": gross, "fee": fee, "realized_pnl": realized, "adx": signal.adx})
        if shares == 0:
            average_cost = 0.0
            entry_price = entry_atr = highest_close = np.nan
            profit_stage = 0

    for date in dates:
        i = frame.index.get_loc(date)
        signal_date = date
        signal = frame.loc[date]
        close_price = float(frame.loc[date, "close"])
        actions = []

        if shares > 0:
            highest_close = max(highest_close, float(signal.close))
            current_atr = float(signal.atr14) if pd.notna(signal.atr14) else entry_atr
            trailing_stop = highest_close - 3.0 * current_atr
            if bool((signal.death_cross and signal.close < signal.ema20) or signal.close <= trailing_stop):
                execute_sell(date, signal_date, signal, "死叉且跌破EMA20/3ATR移动止损", close_price * (1 - SLIPPAGE), shares)
                actions.append("SELL_AT_CLOSE")
            elif shares > 0:
                target_stage = 2 if signal.close >= entry_price * 1.80 else (1 if signal.close >= entry_price * 1.40 else 0)
                if target_stage > profit_stage:
                    quantity = max(1, shares // 2)
                    execute_sell(date, signal_date, signal, f"分批止盈{target_stage * 40}%", close_price * (1 - SLIPPAGE), quantity)
                    profit_stage = target_stage
                    actions.append("TAKE_PROFIT_AT_CLOSE")

        if shares == 0 and bool(signal.golden_cross and pd.notna(signal.adx)):
            exposure = 1.00 if signal.adx >= 40 else (0.80 if signal.adx >= 20 else 0.60)
            price = close_price * (1 + SLIPPAGE)
            quantity = int((cash * exposure) / price)
            gross = quantity * price
            fee = strategy.commission(gross) if quantity else 0.0
            if quantity > 0 and gross + fee <= cash:
                cash -= gross + fee
                shares = quantity
                average_cost = (gross + fee) / quantity
                entry_price = price
                entry_atr = float(signal.atr14) if pd.notna(signal.atr14) else 0.0
                highest_close = float(signal.close)
                profit_stage = 0
                trades.append({"date": date, "signal_date": signal_date, "side": "BUY", "reason": "MACD金叉收盘买入", "price": price, "shares": quantity, "gross": gross, "fee": fee, "realized_pnl": np.nan, "adx": signal.adx, "exposure": exposure})
                actions.append("BUY_AT_CLOSE")

        equity_value = cash + shares * close_price
        rows.append({"date": date, "signal_date": signal_date, "open": frame.loc[date, "open"], "close": close_price, "dif": signal.dif, "dea": signal.dea, "adx": signal.adx, "ema20": signal.ema20, "shares": shares, "cash": cash, "equity": equity_value, "position": "持仓" if shares else "空仓", "action": ",".join(actions) if actions else "HOLD", "action_reason": "；".join([t["reason"] for t in trades[-len(actions):]]) if actions else "无交易信号"})

    equity = pd.DataFrame(rows)
    trade_frame = pd.DataFrame(trades)
    summary = strategy.metrics(equity, trade_frame)
    state = {"cash": cash, "shares": shares, "average_cost": average_cost, "entry_price": entry_price, "entry_atr": entry_atr, "highest_close": highest_close, "profit_stage": profit_stage}
    return (equity, trade_frame, summary, state) if return_state else (equity, trade_frame, summary)


def build_buy_hold_curve(frame: pd.DataFrame) -> pd.DataFrame:
    dates = frame.index[frame.index >= START]
    first_price = float(frame.loc[dates[0], "open"]) * (1 + SLIPPAGE)
    shares = int(strategy.INITIAL_CASH / first_price)
    fee = strategy.commission(shares * first_price)
    while shares > 0 and shares * first_price + fee > strategy.INITIAL_CASH:
        shares -= 1
        fee = strategy.commission(shares * first_price)
    cash = strategy.INITIAL_CASH - shares * first_price - fee
    return pd.DataFrame({
        "date": dates,
        "equity": cash + shares * frame.loc[dates, "close"].to_numpy(),
    })


def build_macd_cross_curve(frame: pd.DataFrame) -> pd.DataFrame:
    """Golden cross buy / death cross sell, using same-day close execution."""
    dates = frame.index[frame.index >= START]
    cash, shares = strategy.INITIAL_CASH, 0
    rows, trades = [], []
    for date in dates:
        signal = frame.loc[date]
        close_price = float(frame.loc[date, "close"])
        if shares > 0 and bool(signal.death_cross):
            sell_price = close_price * (1 - SLIPPAGE)
            gross = shares * sell_price
            fee = strategy.commission(gross)
            cash += gross - fee
            trades.append({"date": date.strftime("%Y-%m-%d"), "signal_date": date.strftime("%Y-%m-%d"), "side": "SELL", "reason": "MACD死叉收盘卖出", "price": sell_price, "shares": shares, "fee": fee})
            shares = 0
        if shares == 0 and bool(signal.golden_cross):
            buy_price = close_price * (1 + SLIPPAGE)
            quantity = int(cash / buy_price)
            gross = quantity * buy_price
            fee = strategy.commission(gross) if quantity else 0.0
            if quantity > 0 and gross + fee <= cash:
                cash -= gross + fee
                shares = quantity
                trades.append({"date": date.strftime("%Y-%m-%d"), "signal_date": date.strftime("%Y-%m-%d"), "side": "BUY", "reason": "MACD金叉收盘买入", "price": buy_price, "shares": quantity, "fee": fee})
        rows.append({"date": date, "equity": cash + shares * close_price})
    return pd.DataFrame(rows), pd.DataFrame(trades)


def preview_action(frame: pd.DataFrame) -> dict:
    now = market_now()
    today = pd.Timestamp(now.date())
    before_close = now.time() < time(15, 0)
    complete = frame[frame.index < today] if before_close else frame
    if complete.empty:
        return {"action": "WAIT", "label": "等待第一条完整日线", "signal_date": "", "execution_label": "暂无", "reason": "数据不足"}
    intraday_bar = frame.loc[today] if before_close and today in frame.index else None
    signal_date = today if intraday_bar is not None else complete.index[-1]
    signal = intraday_bar if intraday_bar is not None else complete.loc[signal_date]
    execution_today = before_close
    execution_label = f"今日收盘（{today:%Y-%m-%d}）" if execution_today else f"下一交易日收盘（今日已过15:00）"
    _, _, _, state = close_execution_backtest(complete, return_state=True)
    reasons = []
    action = "HOLD"
    if state["shares"] > 0:
        high = max(state["highest_close"], float(signal.close))
        atr = float(signal.atr14) if pd.notna(signal.atr14) else state["entry_atr"]
        stop = high - 3.0 * atr
        if bool((signal.death_cross and signal.close < signal.ema20) or signal.close <= stop):
            action, reasons = "SELL_AT_CLOSE", ["MACD死叉且跌破EMA20或触发3×ATR移动止损"]
        elif signal.close >= state["entry_price"] * 1.8:
            action, reasons = "TAKE_PROFIT_AT_CLOSE", ["盈利达到80%，收盘减半"]
        elif signal.close >= state["entry_price"] * 1.4:
            action, reasons = "TAKE_PROFIT_AT_CLOSE", ["盈利达到40%，收盘减半"]
        else:
            reasons = ["持仓继续观察"]
    elif bool(signal.golden_cross and pd.notna(signal.adx)):
        exposure = "100%" if signal.adx >= 40 else ("80%" if signal.adx >= 20 else "60%")
        action, reasons = "BUY_AT_CLOSE", [f"MACD金叉，ADX={signal.adx:.1f}，建议仓位{exposure}"]
    else:
        reasons = ["无MACD金叉，保持空仓"]
    data_mode = "盘中实时临时日线" if intraday_bar is not None else "上一完整日线（实时接口不可用）"
    signal_text = f"{signal_date:%Y-%m-%d}（{data_mode}）"
    return {"action": action, "label": action.replace("_", " "), "signal_date": signal_text, "latest_data_date": signal_date.strftime("%Y-%m-%d"), "execution_label": execution_label, "reason": "；".join(reasons), "close": float(signal.close), "adx": float(signal.adx) if pd.notna(signal.adx) else None, "position": "持仓" if state["shares"] else "空仓", "data_mode": data_mode}


def preview_macd_cross(frame: pd.DataFrame, now=None) -> dict:
    now = market_now() if now is None else now
    today = pd.Timestamp(now.date())
    before_close = now.time() < time(15, 0)
    complete = frame[frame.index < today] if before_close else frame
    signal_date = today if today in frame.index else complete.index[-1]
    if before_close and today not in frame.index:
        signal_date = complete.index[-1]
    signal = frame.loc[signal_date]
    _, trades = build_macd_cross_curve(complete)
    in_position = bool(not trades.empty and trades.iloc[-1]["side"] == "BUY")
    if in_position and bool(signal.death_cross):
        action, reason = "SELL_AT_CLOSE", "MACD死叉，收盘卖出"
    elif not in_position and bool(signal.golden_cross):
        action, reason = "BUY_AT_CLOSE", "MACD金叉，收盘买入"
    else:
        action, reason = "HOLD", "无新的金叉/死叉信号"
    mode = "盘中实时临时日线" if before_close and signal_date == today else "上一完整日线"
    execution = "今日收盘" if before_close else "下一交易日收盘（今日已过15:00）"
    return {"action": action, "signal_date": f"{signal_date:%Y-%m-%d}（{mode}）", "execution_label": execution, "reason": reason, "position": "持仓" if in_position else "空仓"}


def render_html(frame, close_summary, daily, trades, macd_cross_trades, sync_status, previews, benchmark_summary, buy_hold_curve, macd_cross_curve):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_csv(OUTPUT_DIR / "daily_operations.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / "close_execution_trades.csv", index=False, encoding="utf-8-sig")
    buy_hold_curve.to_csv(OUTPUT_DIR / "buy_and_hold_curve.csv", index=False, encoding="utf-8-sig")
    macd_cross_curve.to_csv(OUTPUT_DIR / "macd_cross_curve.csv", index=False, encoding="utf-8-sig")
    macd_cross_trades.to_csv(OUTPUT_DIR / "macd_cross_trades.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "sync_status.json").write_text(json.dumps(sync_status, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    chart_payload = json.dumps({
        "initial": strategy.INITIAL_CASH,
        "close": [{"date": d.strftime("%Y-%m-%d"), "equity": float(v)} for d, v in zip(daily["date"], daily["equity"])],
        "buy_hold": [{"date": d.strftime("%Y-%m-%d"), "equity": float(v)} for d, v in zip(buy_hold_curve["date"], buy_hold_curve["equity"])],
        "macd_cross": [{"date": d.strftime("%Y-%m-%d"), "equity": float(v)} for d, v in zip(macd_cross_curve["date"], macd_cross_curve["equity"])],
    }, ensure_ascii=False, separators=(",", ":"))
    trade_payload = json.dumps({"close": trades.to_dict("records"), "macd_cross": macd_cross_trades.to_dict("records"), "buy_hold": []}, ensure_ascii=False, default=str, separators=(",", ":"))
    preview_payload = json.dumps(previews, ensure_ascii=False, default=str, separators=(",", ":"))
    chart_html = """
<div class='chart-toolbar' style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px'><div id='strategyPicker' style='position:relative'><button id='strategyPickerButton' type='button'>选择策略</button><div id='strategyPickerMenu' hidden style='position:absolute;z-index:5;top:38px;left:0;min-width:220px;padding:10px;background:#fff;border:1px solid #bfc7d2;border-radius:4px;box-shadow:0 5px 16px #0002'><label style='display:block;padding:5px'><input type='checkbox' class='seriesToggle' data-key='close' checked> MACD+ADX动态仓位策略</label><label style='display:block;padding:5px'><input type='checkbox' class='seriesToggle' data-key='buy_hold' checked> 一直持有</label><label style='display:block;padding:5px'><input type='checkbox' class='seriesToggle' data-key='macd_cross'> MACD金叉买入/死叉卖出</label></div></div><button data-months='1' class='recentRange'>近1个月</button><button data-months='3' class='recentRange'>近3个月</button><button data-months='6' class='recentRange'>近6个月</button><button data-months='12' class='recentRange'>近1年</button><button data-months='36' class='recentRange'>近3年</button><label>开始 <input id='chartStart' type='date'></label><label>结束 <input id='chartEnd' type='date'></label><button id='zoomOut'>缩小</button><button id='zoomIn'>放大</button><button id='resetZoom'>重置</button></div>
<div id='chartStats' class='chart-stats' style='display:flex;gap:18px;flex-wrap:wrap;color:#475467;font-size:13px;margin-bottom:8px'></div><svg id='equityChart' viewBox='0 0 1200 560' style='width:100%;height:auto;border:1px solid #e1e5ea;background:#fff' role='img' aria-label='动态权益和回撤图'></svg>
<script>
const chartData = __PAYLOAD__;
const tradeData = __TRADES__;
const previewData = __PREVIEWS__;
const svg = document.getElementById('equityChart');
const startInput = document.getElementById('chartStart');
const endInput = document.getElementById('chartEnd');
const stats = document.getElementById('chartStats');
const seriesMeta = {close:{label:'MACD+ADX动态仓位策略',color:'#d97706'}, buy_hold:{label:'一直持有',color:'#176b87'}, macd_cross:{label:'MACD金叉买入/死叉卖出',color:'#7c3aed'}};
const seriesKeys = Object.keys(seriesMeta);
const allDates = [...new Set(seriesKeys.flatMap(key => chartData[key].map(x => x.date)))].sort();
let rangeStart = 0, rangeEnd = allDates.length - 1;
const NS = 'http://www.w3.org/2000/svg';
function make(tag, attrs={}) { const node=document.createElementNS(NS, tag); for (const [k,v] of Object.entries(attrs)) node.setAttribute(k, v); return node; }
function selectSeries(series, start, end) { return series.filter(x => x.date >= start && x.date <= end); }
function drawLine(points, color, width=2) { if (points.length < 2) return; svg.appendChild(make('polyline', {points: points.map(p => p[0]+','+p[1]).join(' '), fill:'none', stroke:color, 'stroke-width':width, 'stroke-linejoin':'round'})); }
function text(x, y, value, anchor='start') { const n=make('text',{x,y,'text-anchor':anchor,fill:'#667085','font-size':'12'}); n.textContent=value; svg.appendChild(n); }
function metric(series, start, end) { const data=selectSeries(series,start,end); if (!data.length) return null; const first=data[0].equity, last=data[data.length-1].equity; let peak=first, dd=0; for (const x of data) { peak=Math.max(peak,x.equity); dd=Math.min(dd,x.equity/peak-1); } return {ret:last/first-1, dd}; }
function fmtPct(x) { return (x*100).toFixed(2)+'%'; }
function renderLiveActions(selected) { const box=document.getElementById('liveActions'); box.innerHTML=selected.map(key=>{const p=previewData[key]; if(!p) return ''; const cls=p.action.includes('BUY')?'buy':(p.action.includes('SELL')?'sell':'hold'); return `<div class='box'><div class='muted'>${seriesMeta[key].label} · ${p.execution_label}</div><div class='value ${cls}'>${p.action}</div><div class='muted'>${p.reason} · ${p.signal_date} · ${p.position}</div></div>`;}).join('')||'<div class="muted">请至少选择一条策略</div>'; }
function renderTrades(selected) { const body=document.getElementById('tradeBody'); const rows=[]; for (const key of selected) { for (const trade of tradeData[key]) rows.push(`<tr><td>${seriesMeta[key].label}</td><td>${trade.date}</td><td>${trade.signal_date}</td><td>${trade.side}</td><td>${trade.reason}</td><td>${Number(trade.price).toFixed(2)}</td><td>${Number(trade.shares).toFixed(0)}</td></tr>`); } body.innerHTML=rows.length?rows.join(''):`<tr><td colspan='7'>所选策略没有主动买卖记录</td></tr>`; }
function render() {
  const start=allDates[rangeStart], end=allDates[rangeEnd]; startInput.value=start; endInput.value=end; svg.innerHTML='';
  const selected=[...document.querySelectorAll('.seriesToggle:checked')].map(input=>input.dataset.key);
  const series=seriesKeys.filter(key=>selected.includes(key)).map(key=>[key,seriesMeta[key].label]); const equityValues=[];
  renderLiveActions(selected);
  if (!series.length) { stats.innerHTML='<span>请至少选择一条曲线</span>'; return; }
  for (const [key] of series) for (const x of selectSeries(chartData[key],start,end)) equityValues.push(x.equity/chartData.initial);
  const yMin=Math.min(...equityValues), yMax=Math.max(...equityValues), yPad=Math.max((yMax-yMin)*.08,.03);
  const left=78, right=1180, top=30, equityBottom=510;
  const sx=(date)=>left+(allDates.indexOf(date)-rangeStart)/Math.max(rangeEnd-rangeStart,1)*(right-left);
  const sy=(v)=>equityBottom-(v-(yMin-yPad))/Math.max(yMax-yMin+2*yPad,.1)*(equityBottom-top);
  svg.appendChild(make('rect',{x:left,y:top,width:right-left,height:equityBottom-top,fill:'#f8fafc'}));
  text(left,18,'权益净值');
  for (let i=0;i<=5;i++) { const value=yMin-yPad+(yMax-yMin+2*yPad)*i/5; const y=sy(value); svg.appendChild(make('line',{x1:left,y1:y,x2:right,y2:y,stroke:'#e5e7eb','stroke-width':1})); text(left-8,y+4,value.toFixed(2),'end'); }
  for (const [key,label] of series) {
    const data=selectSeries(chartData[key],start,end); const ep=data.map(x=>[sx(x.date),sy(x.equity/chartData.initial)]); drawLine(ep,seriesMeta[key].color,2.2);
    if(data.length) { const last=ep[ep.length-1]; svg.appendChild(make('circle',{cx:last[0],cy:last[1],r:3.5,fill:seriesMeta[key].color})); }
  }
  text(left,545,start); text(right,545,end,'end');
  stats.innerHTML=series.map(([key])=>{const m=metric(chartData[key],start,end);return `<span><i style="display:inline-block;width:9px;height:9px;background:${seriesMeta[key].color};margin-right:5px"></i>${seriesMeta[key].label}：区间收益 <b>${fmtPct(m.ret)}</b> · 区间最大回撤 <b>${fmtPct(m.dd)}</b></span>`;}).join('')+`<em>统计区间：${start} 至 ${end}</em>`;
  renderTrades(selected);
}
function setRangeByDate() { const a=allDates.indexOf(startInput.value), b=allDates.indexOf(endInput.value); if(a>=0&&b>=0&&a<b){rangeStart=a;rangeEnd=b;render();} }
startInput.addEventListener('change',setRangeByDate); endInput.addEventListener('change',setRangeByDate);
document.getElementById('resetZoom').onclick=()=>{rangeStart=0;rangeEnd=allDates.length-1;render();};
document.getElementById('zoomIn').onclick=()=>{const span=Math.max(20,Math.floor((rangeEnd-rangeStart)*.8));const mid=Math.floor((rangeStart+rangeEnd)/2);rangeStart=Math.max(0,mid-Math.floor(span/2));rangeEnd=Math.min(allDates.length-1,rangeStart+span);render();};
document.getElementById('zoomOut').onclick=()=>{const span=Math.min(allDates.length-1,Math.ceil((rangeEnd-rangeStart)*1.25));const mid=Math.floor((rangeStart+rangeEnd)/2);rangeStart=Math.max(0,mid-Math.floor(span/2));rangeEnd=Math.min(allDates.length-1,rangeStart+span);render();};
function setRecentRange(months) { const end=allDates.length-1; const target=new Date(allDates[end]+'T00:00:00'); target.setMonth(target.getMonth()-months); const targetText=target.toISOString().slice(0,10); let start=allDates.findIndex(x=>x>=targetText); rangeStart=start<0?0:start; rangeEnd=end; render(); }
document.querySelectorAll('.recentRange').forEach(button=>button.addEventListener('click',()=>setRecentRange(Number(button.dataset.months))));
const pickerButton=document.getElementById('strategyPickerButton');
const pickerMenu=document.getElementById('strategyPickerMenu');
function updatePickerLabel(){const n=document.querySelectorAll('.seriesToggle:checked').length; pickerButton.textContent=`选择策略（已选${n}条）`;}
pickerButton.addEventListener('click',event=>{event.stopPropagation(); pickerMenu.hidden=!pickerMenu.hidden;});
pickerMenu.addEventListener('click',event=>event.stopPropagation());
document.addEventListener('click',()=>{pickerMenu.hidden=true;});
document.querySelectorAll('.seriesToggle').forEach(input=>input.addEventListener('change',()=>{updatePickerLabel();render();}));
updatePickerLabel();
svg.addEventListener('wheel',e=>{e.preventDefault(); if(e.deltaY<0) document.getElementById('zoomIn').click(); else document.getElementById('zoomOut').click();},{passive:false});
render();
</script>
""".replace("__PAYLOAD__", chart_payload).replace("__TRADES__", trade_payload).replace("__PREVIEWS__", preview_payload)

    recent = daily.tail(180).iloc[::-1]
    rows = "".join(f"<tr><td>{r.date:%Y-%m-%d}</td><td>{r.signal_date:%Y-%m-%d}</td><td>{r.action}</td><td>{r.action_reason}</td><td>{r.close:.2f}</td><td>{r.dif:.2f}</td><td>{r.dea:.2f}</td><td>{r.adx:.2f}</td><td>{r.ema20:.2f}</td><td>{r.position}</td><td>{r.equity:,.2f}</td></tr>" for r in recent.itertuples())
    trade_rows = "".join(f"<tr><td>{r.date}</td><td>{r.signal_date}</td><td>{r.side}</td><td>{r.reason}</td><td>{r.price:.2f}</td><td>{int(r.shares)}</td></tr>" for r in trades.tail(100).iloc[::-1].itertuples()) if not trades.empty else "<tr><td colspan='6'>暂无交易</td></tr>"
    sync_text = "已同步 AkShare" if sync_status["status"] == "synced" else "使用本地缓存"
    error_text = "；".join(x for x in [sync_status.get("error", ""), sync_status.get("realtime", "")] if x)
    # Keep the existing template compact while removing the legacy Baseline
    # presentation from the generated page.
    baseline_summary = close_summary
    preview = previews["close"]
    html = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>半导体MACD+ADX交易面板</title><style>
body{{margin:0;background:#eef1f4;color:#17202a;font-family:'Microsoft YaHei',Arial,sans-serif}}main{{width:min(1500px,calc(100% - 24px));margin:20px auto}}header,section{{background:#fff;border:1px solid #d8dee7;border-radius:6px;padding:18px;margin-top:16px}}header{{margin-top:0;border-top:4px solid #176b87}}h1{{margin:0 0 6px;font-size:24px}}h2{{font-size:17px;margin:0 0 12px}}.muted{{color:#667085;font-size:13px}}.signal{{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr;gap:12px;margin-top:14px}}.box{{padding:14px;background:#f7f9fb;border:1px solid #e1e7ed;border-radius:5px}}.value{{font-size:24px;font-weight:700;margin-top:6px}}.buy{{color:#b42318}}.sell{{color:#087443}}.hold{{color:#667085}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:8px;border-bottom:1px solid #e7ebef;white-space:nowrap;text-align:right}}th:first-child,td:first-child{{text-align:left}}.scroll{{overflow:auto;max-height:600px;border:1px solid #e1e5ea}}input{{width:300px;padding:8px;border:1px solid #bfc7d2;border-radius:4px}}img{{width:100%;display:block}}@media(max-width:900px){{.signal{{grid-template-columns:1fr 1fr}}}}@media(max-width:600px){{.signal{{grid-template-columns:1fr}}}}
</style></head><body><main><header><h1>半导体 MACD+ADX 交易面板</h1><div class='muted'>数据代码 {CODE} · 每次打开脚本自动同步；实时建议按基金收盘价执行</div><div class='muted'>同步状态：{sync_text} · {sync_status.get('attempted_at','')} {error_text}</div><div class='signal'><div class='box'><div class='muted'>{preview.get('execution_label','')}操作建议</div><div class='value {'buy' if 'BUY' in preview['action'] else ('sell' if 'SELL' in preview['action'] else 'hold')}'>{preview['action']}</div><div class='muted'>{preview.get('reason','')}</div></div><div class='box'><div class='muted'>信号日期</div><div class='value'>{preview.get('signal_date','-')}</div><div class='muted'>收盘价 {preview.get('close','-')}</div></div><div class='box'><div class='muted'>ADX / 当前状态</div><div class='value'>{preview.get('adx','-')}</div><div class='muted'>{preview.get('position','-')}</div></div><div class='box'><div class='muted'>历史回测年化</div><div class='value'>{baseline_summary['annual_return']:+.2%}</div><div class='muted'>最大回撤 {baseline_summary['max_drawdown']:.2%} · 夏普 {baseline_summary['sharpe_zero_rf']:.2f}</div></div></div></header>
 <section><h2>动态权益曲线（区间回撤显示为数值）</h2>{chart_html}</section>
<section><h2>绩效对比</h2><table><thead><tr><th>口径</th><th>年化收益</th><th>总收益</th><th>最大回撤</th><th>夏普</th><th>交易次数</th></tr></thead><tbody><tr><td>历史回测（次日开盘）</td><td>{baseline_summary['annual_return']:+.2%}</td><td>{baseline_summary['total_return']:+.2%}</td><td>{baseline_summary['max_drawdown']:.2%}</td><td>{baseline_summary['sharpe_zero_rf']:.2f}</td><td>{baseline_summary['trade_count']}</td></tr><tr><td>基金收盘执行模拟</td><td>{close_summary['annual_return']:+.2%}</td><td>{close_summary['total_return']:+.2%}</td><td>{close_summary['max_drawdown']:.2%}</td><td>{close_summary['sharpe_zero_rf']:.2f}</td><td>{close_summary['trade_count']}</td></tr><tr><td>买入持有</td><td>{benchmark_summary['annual_return']:+.2%}</td><td>{benchmark_summary['total_return']:+.2%}</td><td>{benchmark_summary['max_drawdown']:.2%}</td><td>-</td><td>-</td></tr></tbody></table></section>
 <section><h2>每日交易情况</h2><p class='muted'>当前口径使用当天收盘价（历史数据为收盘代理，盘中运行时为14:58实时价）；信号日与收盘执行日相同。页面显示最近180个交易日。</p><input id='filter' placeholder='筛选日期、操作或原因'><div class='scroll'><table id='daily'><thead><tr><th>交易日</th><th>信号日</th><th>操作</th><th>原因</th><th>收盘</th><th>DIF</th><th>DEA</th><th>ADX</th><th>EMA20</th><th>状态</th><th>权益</th></tr></thead><tbody>{rows}</tbody></table></div></section>
 <section><h2>交易记录（按所选策略显示）</h2><div class='scroll'><table><thead><tr><th>策略</th><th>交易日</th><th>信号日</th><th>方向</th><th>原因</th><th>价格</th><th>数量</th></tr></thead><tbody id='tradeBody'></tbody></table></div></section>
 <section class='muted'><b>执行说明：</b>所有策略统一按当天收盘价结算；历史回测用日线收盘价作为14:58代理，盘中运行时优先使用实时价构造临时日线。基金 T+1 场景下，建议在“操作建议”所示的收盘时点提交申购/赎回。</section></main><script>const q=document.getElementById('filter');const rs=[...document.querySelectorAll('#daily tbody tr')];q.addEventListener('input',()=>{{const x=q.value.toLowerCase();rs.forEach(r=>r.hidden=x&&!r.textContent.toLowerCase().includes(x));}});</script></body></html>"""
    html = re.sub(r"<div class='signal'>.*?</div></header>", "<div id='liveActions' class='signal'></div></header>", html, count=1, flags=re.S)
    legacy_row = f"<tr><td>历史回测（次日开盘）</td><td>{baseline_summary['annual_return']:+.2%}</td><td>{baseline_summary['total_return']:+.2%}</td><td>{baseline_summary['max_drawdown']:.2%}</td><td>{baseline_summary['sharpe_zero_rf']:.2f}</td><td>{baseline_summary['trade_count']}</td></tr>"
    html = html.replace(legacy_row, "").replace("历史回测年化", "基金收盘模拟年化")
    (OUTPUT_DIR / "report.html").write_text(html, encoding="utf-8")
    (OUTPUT_DIR / "summary.json").write_text(json.dumps({"sync": sync_status, "preview": preview, "close_execution": close_summary, "buy_and_hold": benchmark_summary}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> None:
    sync_status = sync_data()
    frame = load_frame()
    realtime_snapshot, realtime_message = fetch_realtime_snapshot()
    preview_frame = append_realtime_bar(frame, realtime_snapshot)
    sync_status["realtime"] = realtime_message
    if realtime_snapshot:
        sync_status["realtime_snapshot"] = realtime_snapshot
    close_equity, close_trades, close_summary = close_execution_backtest(frame)
    benchmark_frame = frame[frame.index <= BASELINE_END]
    benchmark, benchmark_summary = strategy.buy_hold(benchmark_frame)
    buy_hold_curve = build_buy_hold_curve(frame)
    macd_cross_curve, macd_cross_trades = build_macd_cross_curve(frame)
    previews = {"close": preview_action(preview_frame), "macd_cross": preview_macd_cross(preview_frame), "buy_hold": {"action": "HOLD", "execution_label": "无主动操作", "reason": "一直持有，不产生择时买卖", "signal_date": "-", "position": "持有"}}
    render_html(frame, close_summary, close_equity, close_trades, macd_cross_trades, sync_status, previews, benchmark_summary, buy_hold_curve, macd_cross_curve)
    print(json.dumps({"sync": sync_status, "previews": previews, "close_execution": close_summary, "buy_and_hold": benchmark_summary}, ensure_ascii=False, default=str))
    print(f"report: {OUTPUT_DIR / 'report.html'}")


if __name__ == "__main__":
    main()
