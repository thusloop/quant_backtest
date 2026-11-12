"""Streamlit Community Cloud entry point. Run from either repository root."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from threading import RLock

os.environ.setdefault("MPLBACKEND", "Agg")
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from market_time import market_now
from webapp.backend.backtest_service import build
from webapp.backend.cloud_service import (
    asset_metadata, export_configuration, import_configuration, initial_configuration,
)
from webapp.backend.data_service import load_asset, sync_asset
from webapp.backend.strategy_service import validate_strategy


@st.cache_resource
def sync_lock() -> RLock:
    return RLock()


@st.cache_data(ttl=60, max_entries=32, show_spinner=False)
def dashboard(meta: dict, definitions: list[dict], realtime: bool, revision: int, minute: str) -> tuple[dict, dict]:
    # revision and minute invalidate data after sync and across market-time changes.
    frame, quote = load_asset(meta["code"], meta=meta, include_realtime=realtime)
    return build(meta["code"], frame, definitions=definitions), quote


def sync(meta: dict) -> dict:
    with sync_lock():
        return sync_asset(meta["code"], meta=meta)


def configuration_panel() -> None:
    state = st.session_state
    with st.expander("添加股票 / 指数"):
        with st.form("new_asset", clear_on_submit=True):
            code = st.text_input("6 位代码", max_chars=6)
            name = st.text_input("显示名称（可选）", max_chars=40)
            submitted = st.form_submit_button("添加并同步")
        if submitted:
            try:
                meta = asset_metadata(code, name)
                if meta["code"] in state.assets:
                    raise ValueError("该标的已存在")
                with st.spinner("正在获取历史行情…"):
                    status = sync(meta)
                if not meta["path"].exists():
                    raise ValueError(status["message"])
                state.assets[meta["code"]] = meta
                state.sync_status[meta["code"]] = status
                state.asset_code = meta["code"]
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    with st.expander("自定义策略"):
        custom = {x["id"]: x for x in state.definitions if not x.get("builtin")}
        editing = st.selectbox("编辑策略", ["new"] + list(custom),
                               format_func=lambda key: "新增策略" if key == "new" else custom[key]["name"])
        current = custom.get(editing, {})
        params = current.get("params", {})
        templates = {"macd_cross": "MACD 金叉 / 死叉", "close": "MACD+ADX 动态仓位", "buy_hold": "一直持有"}
        with st.form(f"strategy_{editing}"):
            name = st.text_input("策略名称", value=current.get("name", ""), max_chars=60)
            description = st.text_input("策略说明", value=current.get("description", ""), max_chars=200)
            template = st.selectbox("策略模板", list(templates),
                                    index=list(templates).index(current.get("base_key", "macd_cross")),
                                    format_func=templates.get)
            fast = st.number_input("MACD 快线", min_value=2, value=params.get("macd_fast", 12), step=1)
            slow = st.number_input("MACD 慢线", min_value=3, value=params.get("macd_slow", 26), step=1)
            signal = st.number_input("MACD 信号线", min_value=2, max_value=50, value=params.get("macd_signal", 9), step=1)
            adx = st.number_input("ADX 周期", min_value=5, max_value=60, value=params.get("adx_period", 14), step=1)
            saved = st.form_submit_button("保存策略")
        if saved:
            try:
                item = validate_strategy({
                    "id": "" if editing == "new" else editing,
                    "name": name, "description": description, "base_key": template,
                    "params": {"macd_fast": fast, "macd_slow": slow, "macd_signal": signal, "adx_period": adx},
                }, state.definitions)
                state.definitions = [x for x in state.definitions if x["id"] != item["id"]] + [item]
                state.selected_strategies = list(dict.fromkeys(state.get("selected_strategies", []) + [item["key"]]))
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if editing != "new" and st.button("删除所选自定义策略"):
            state.definitions = [x for x in state.definitions if x["id"] != editing]
            state.selected_strategies = [x for x in state.get("selected_strategies", []) if x != editing]
            st.rerun()

    with st.expander("配置备份"):
        st.caption("自定义标的和策略保存在当前会话中。关闭会话前可下载配置，下次导入恢复。")
        st.download_button("下载配置 JSON", export_configuration(state.assets, state.definitions),
                           file_name="quant_configuration.json", mime="application/json")
        backup = st.file_uploader("选择配置 JSON", type=["json"])
        if st.button("导入配置", disabled=backup is None):
            try:
                assets, definitions = import_configuration(backup.getvalue(), state.assets, state.definitions)
                state.assets, state.definitions = assets, definitions
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def render_dashboard(data: dict, meta: dict) -> None:
    labels = {key: curve["label"] for key, curve in data["curves"].items()}
    if "selected_strategies" not in st.session_state:
        st.session_state.selected_strategies = [key for key in ("close", "buy_hold") if key in labels]
    else:
        st.session_state.selected_strategies = [key for key in st.session_state.selected_strategies if key in labels]
    selected = st.multiselect("对比策略", list(labels), format_func=labels.get, key="selected_strategies")
    if not selected:
        st.info("请选择至少一种策略。")
        return

    st.subheader("收盘操作参考")
    for column, key in zip(st.columns(len(selected)), selected):
        preview = data["previews"][key]
        with column:
            st.markdown(f"**{labels[key]}**")
            st.write(preview["action"])
            st.caption(preview.get("execution_label", ""))
            st.write(preview.get("reason", ""))
            st.caption(f"{preview.get('signal_date', '')} · {preview.get('position', '')}")
    st.caption("操作参考基于下方标注的数据日期；盘中临时日线会随行情变化。")

    series = {
        key: pd.Series(curve["data"]["equity"], index=pd.to_datetime(curve["data"]["date"]))
        for key, curve in data["curves"].items() if key in selected
    }
    first = min(values.index.min() for values in series.values()).date()
    last = max(values.index.max() for values in series.values()).date()
    range_column, date_column = st.columns([1, 2])
    with range_column:
        period = st.selectbox("快速区间", ["全部", "近1个月", "近3个月", "近6个月", "近1年", "近3年", "自定义"])
    with date_column:
        dates = st.date_input("起止日期", value=(first, last), min_value=first, max_value=last,
                              key=f"dates_{meta['code']}_{first}_{last}", disabled=period != "自定义")
    if period == "自定义":
        if len(dates) != 2:
            st.info("请选择开始和结束日期。")
            return
        start, end = map(pd.Timestamp, dates)
    else:
        end = pd.Timestamp(last)
        months = {"近1个月": 1, "近3个月": 3, "近6个月": 6, "近1年": 12, "近3年": 36}.get(period)
        start = max(pd.Timestamp(first), end - pd.DateOffset(months=months)) if months else pd.Timestamp(first)

    figure = go.Figure()
    stats, exports = [], []
    for key, values in series.items():
        visible = values.loc[start:end]
        if visible.empty:
            continue
        curve = data["curves"][key]
        figure.add_trace(go.Scatter(x=visible.index, y=visible / data["initial"], name=labels[key],
                                    mode="lines", line={"color": curve["color"]}))
        stats.append({"策略": labels[key], "区间收益": visible.iloc[-1] / visible.iloc[0] - 1,
                      "最大回撤": (visible / visible.cummax() - 1).min()})
        exports.append(pd.DataFrame({"日期": visible.index, "策略": labels[key], "权益": visible.values}))
    if not stats:
        st.info("该区间没有交易日数据。")
        return
    for column, metric in zip(st.columns(len(stats)), stats):
        column.metric(metric["策略"], f"{metric['区间收益']:+.2%}")
        column.caption(f"区间最大回撤 {metric['最大回撤']:.2%}")
    figure.update_layout(height=460, margin=dict(l=10, r=10, t=20, b=10),
                         yaxis_title="权益净值", hovermode="x unified", dragmode="pan",
                         xaxis=dict(rangeslider=dict(visible=True)), legend=dict(orientation="h"))
    st.plotly_chart(figure, width="stretch", config={"scrollZoom": True, "displaylogo": False})
    st.caption(f"统计区间：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}。图表可缩放、平移；区间统计由上方日期控件决定。")
    st.download_button("下载区间权益 CSV", pd.concat(exports).to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{meta['code']}_equity.csv", mime="text/csv")

    st.subheader("交易记录")
    query = st.text_input("筛选日期、方向或原因")
    rows = [{"策略": labels[key], **trade} for key in selected for trade in data["trades"][key]
            if start.strftime("%Y-%m-%d") <= str(trade.get("date", "")) <= end.strftime("%Y-%m-%d")]
    if query:
        rows = [row for row in rows if query.casefold() in json.dumps(row, ensure_ascii=False).casefold()]
    if rows:
        trades = pd.DataFrame(rows).sort_values("date", ascending=False)
        for column in ("price", "shares", "fee", "gross", "realized_pnl", "exposure"):
            if column in trades:
                trades[column] = pd.to_numeric(trades[column], errors="coerce")
        st.dataframe(trades, width="stretch", hide_index=True)
        st.download_button("下载交易记录 CSV", trades.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{meta['code']}_trades.csv", mime="text/csv")
    else:
        st.info("所选策略和区间没有匹配的交易记录。")


def main() -> None:
    st.set_page_config(page_title="量化策略监控", page_icon="📈", layout="wide")
    state = st.session_state
    if "assets" not in state:
        state.assets, state.definitions = initial_configuration()
        state.sync_status = {}

    with st.sidebar:
        st.title("量化回测")
        configuration_panel()
        asset_labels = {key: f"{key} · {item['name']}" for key, item in state.assets.items()}
        code = st.selectbox("股票 / 指数", list(state.assets), key="asset_code",
                            format_func=asset_labels.get)
        realtime = st.checkbox("加载盘中实时行情", value=False, help="仅支持 881121，北京时间 15:00 前尝试获取。")
        meta = state.assets[code]
        if st.button("同步行情", type="primary", width="stretch"):
            with st.spinner("正在同步行情…"):
                state.sync_status[code] = sync(meta)
            dashboard.clear()
            st.rerun()
        st.caption("首次打开使用已有日线。点击同步行情获取更新；接口不可用时保留缓存。")
        st.caption(f"北京时间 {market_now():%Y-%m-%d %H:%M:%S}")

    st.title(f"{meta['name']} · 策略监控")
    status = state.sync_status.get(code)
    if status:
        if status.get("status") == "synced":
            st.success(status["message"])
        else:
            st.warning(status["message"])
    if not meta["path"].exists():
        st.info("该标的还没有行情缓存，请点击侧栏的“同步行情”。")
        return
    try:
        with st.spinner("正在计算回测…"):
            data, quote = dashboard(meta, state.definitions, realtime, meta["path"].stat().st_mtime_ns,
                                    market_now().strftime("%Y-%m-%d %H:%M"))
    except Exception as exc:
        st.error(f"数据加载失败：{exc}")
        st.info("可尝试同步行情，或切换到其他标的。")
        return
    st.caption(f"最新数据 {data['latest']} · {quote['message']} · 收盘价执行模拟")
    age = (pd.Timestamp(market_now().date()) - pd.Timestamp(data["latest"])).days
    if age > 4:
        st.warning(f"当前数据截至 {data['latest']}，距今天已超过 4 天。请同步行情后再查看操作参考。")
    render_dashboard(data, meta)
    st.download_button("下载当前历史行情 CSV", meta["path"].read_bytes(),
                       file_name=f"{code}_history.csv", mime="text/csv")


if __name__ == "__main__":
    main()
