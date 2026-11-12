from __future__ import annotations

import json
from datetime import time

import pandas as pd

import macd_adx_backtest as indicators
import macd_adx_semiconductor_web as web_strategy
from market_time import market_now
from history_cache import write_history

from .config import ASSETS


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "date", "开盘": "open", "开盘价": "open", "收盘": "close", "收盘价": "close",
        "最高": "high", "最高价": "high", "最低": "low", "最低价": "low", "成交量": "volume", "成交额": "money",
        "鏃ユ湡": "date", "寮€鐩樹环": "open", "鏀剁洏浠?:": "close", "鏈€楂樹环": "high", "鏈€浣庝环": "low",
        "鎴愪氦閲?:": "volume", "鎴愪氦棰?:": "money",
    }
    frame = raw.rename(columns=rename)
    required = ["date", "open", "close", "high", "low", "volume", "money"]
    missing = [x for x in required if x not in frame.columns]
    if missing:
        raise RuntimeError(f"字段缺失: {missing}")
    frame = frame[required].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    for col in required[1:]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date", kind="stable").drop_duplicates("date", keep="last")


def sync_asset(code: str, meta: dict | None = None) -> dict:
    meta = ASSETS[code] if meta is None else meta
    status = {"code": code, "status": "cached", "message": "使用本地缓存", "checked_at": market_now().isoformat(timespec="seconds")}
    if code == "881121":
        try:
            result = web_strategy.sync_data()
            status.update({"status": result.get("status", "cached"), "message": result.get("error") or result.get("source", "AkShare")})
        except Exception as exc:
            status["message"] = f"同步失败，使用缓存: {exc}"
        return status

    try:
        now = market_now()
        today = now.strftime("%Y%m%d")
        errors = []
        frame = None
        # Tonghuashun's 88xxxx codes are industry/concept indices rather than
        # A-share stocks. Use the same board-history endpoint as 884229 for
        # newly added 88xxxx assets, avoiding the EastMoney stock endpoint.
        if code == "884229" or (meta.get("source") == "custom" and code.startswith("88")):
            import requests
            parts = []
            # A cached file already contains the long history.  Only the
            # current year is needed for a normal refresh; fall back to the
            # full range when the cache does not exist yet.
            start_year = now.year if meta["path"].exists() else 2018
            for year in range(start_year, now.year + 1):
                url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js"
                response = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "http://q.10jqka.com.cn"}, timeout=20)
                if response.status_code != 200 or "{" not in response.text:
                    continue
                payload = json.loads(response.text[response.text.find("{"):response.text.rfind("}") + 1])
                parts.extend(row.split(",") for row in payload.get("data", "").split(";") if row)
            if parts:
                raw = pd.DataFrame(parts).iloc[:, :7]
                raw.columns = ["date", "open", "high", "low", "close", "volume", "money"]
                frame = _normalize(raw)
                if meta["path"].exists() and start_year != 2018:
                    cached = pd.read_csv(meta["path"])
                    frame = _normalize(pd.concat([cached, frame], ignore_index=True))
            else:
                raise RuntimeError(f"同花顺 {code} 历史接口没有返回数据")
        else:
            import akshare as ak
            raw = None
            for name, kwargs in [
                ("stock_zh_a_hist", {"symbol": code, "period": "daily", "start_date": "20180101", "end_date": today, "adjust": "qfq"}),
                ("stock_zh_index_daily", {"symbol": code}),
                ("stock_zh_index_daily_tx", {"symbol": code, "start_date": "20180101", "end_date": today}),
                ("stock_zh_index_daily_em", {"symbol": code, "start_date": "20180101", "end_date": today}),
            ]:
                try:
                    candidate = getattr(ak, name)(**kwargs)
                    if candidate is not None and not candidate.empty:
                        raw = candidate
                        break
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        if frame is None:
            if raw is None:
                raise RuntimeError("；".join(errors) or "没有返回数据")
            frame = _normalize(raw)
        write_history(frame, meta["path"])
        source_name = "同花顺" if code == "884229" or (meta.get("source") == "custom" and code.startswith("88")) else "AkShare"
        status.update({"status": "synced", "message": f"{source_name} 更新 {len(frame)} 条"})
    except Exception as exc:
        status["message"] = f"{code} 数据不可用: {exc}"
    return status


def load_asset(code: str, meta: dict | None = None, include_realtime: bool = True) -> tuple[pd.DataFrame, dict]:
    meta = ASSETS[code] if meta is None else meta
    if not meta["path"].exists():
        raise FileNotFoundError(f"{code} 尚无本地数据文件: {meta['path'].name}")
    frame = indicators.add_indicators(indicators.load_history(meta["path"]))
    realtime = {"status": "not_supported", "message": "该标的当前使用日线缓存", "data_mode": "daily_cache", "quote_time": None, "price": None}
    # Before 15:00, append the latest board quote as a provisional daily bar
    # so the close-execution recommendation reflects the price visible to the
    # user. It is never written into the historical cache.
    if code == "881121" and not include_realtime:
        realtime = {"status": "disabled", "message": "使用日线缓存，可在侧栏开启盘中实时行情", "data_mode": "daily_cache", "quote_time": None, "price": None}
    elif code == "881121" and market_now().time() < time(15, 0):
        try:
            snapshot, message = web_strategy.fetch_realtime_snapshot()
            if snapshot:
                realtime = {"status": "loaded", "message": f"{snapshot.get('source', '实时行情')}实时价已加载：{snapshot['latest']:.2f}", "data_mode": "intraday_provisional", "quote_time": snapshot.get("quote_time"), "price": float(snapshot["latest"]), "source": snapshot.get("source")}
            else:
                realtime = {"status": "failed", "message": f"实时价加载失败：{message}，使用最近收盘价", "data_mode": "daily_cache", "quote_time": None, "price": None}
            frame = web_strategy.append_realtime_bar(frame, snapshot)
        except Exception:
            realtime = {"status": "failed", "message": "实时价加载异常，使用最近收盘价", "data_mode": "daily_cache", "quote_time": None, "price": None}
    elif code == "881121":
        realtime = {"status": "after_close", "message": "当前已过15:00，使用最近完整收盘价", "data_mode": "daily_close", "quote_time": None, "price": float(frame["close"].iloc[-1])}
    return frame, realtime
