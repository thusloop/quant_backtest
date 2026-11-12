"""Check the MACD strategy at 14:30 Shanghai time, independently of Streamlit."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import json
import math
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import tempfile

from market_time import MARKET_TZ, market_now

ROOT = Path(__file__).resolve().parent
CHECK_TIME = time(14, 30)
DEADLINE = time(15, 0)
QQ_ADDRESS = re.compile(r"[^@\s<>]+@qq\.com", re.IGNORECASE)


class AlertError(Exception):
    pass


@dataclass(frozen=True)
class Symbol:
    code: str
    name: str
    kind: str


@dataclass(frozen=True)
class Config:
    enabled: bool
    sender: str
    recipient: str
    symbols: tuple[Symbol, ...]


@dataclass(frozen=True)
class Snapshot:
    timestamp: datetime
    price: float
    opening: float
    high: float
    low: float
    volume: float
    money: float
    source: str


def execution_window(now: datetime, mode: str) -> tuple[bool, str]:
    now = now.astimezone(MARKET_TZ)
    if mode == "test_email":
        return True, "手动测试邮件"
    if now.weekday() >= 5:
        return False, "周末不检查交易信号"
    if now.time() < CHECK_TIME:
        return False, "尚未到北京时间 14:30"
    if mode == "check" and now.time() >= DEADLINE:
        return False, "已到北京时间 15:00，跳过过期提醒"
    return True, "检查当天 14:30 的分钟行情"


def load_config(path: Path, environ=None) -> Config:
    env = os.environ if environ is None else environ
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AlertError("无法读取邮件提醒配置 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("enabled", True), bool):
        raise AlertError("配置必须是对象，enabled 必须是布尔值")
    sender = str(env.get("QQ_SMTP_USER") or value.get("sender", "")).strip()
    recipient = str(env.get("QQ_MAIL_TO") or value.get("recipient", "")).strip()
    if not QQ_ADDRESS.fullmatch(sender) or not QQ_ADDRESS.fullmatch(recipient):
        raise AlertError("发件人和收件人必须是有效的 QQ 邮箱地址")
    records = value.get("symbols", [])
    override = env.get("MACD_ALERT_CODES", "").strip()
    if override:
        records = [{"code": code} for code in re.split(r"[,，\s]+", override) if code]
    if not isinstance(records, list) or not records or len(records) > 20:
        raise AlertError("请设置 1 至 20 个监控标的")
    symbols = {}
    for item in records:
        if not isinstance(item, dict):
            raise AlertError("每个监控标的必须是包含 code 的对象")
        code = str(item.get("code", "")).strip()
        if not re.fullmatch(r"[0-9]{6}", code):
            raise AlertError("监控代码必须是 6 位数字")
        kind = item.get("type", "ths_board" if code.startswith("88") else "stock")
        if kind not in {"ths_board", "stock"}:
            raise AlertError("当前支持 ths_board 板块指数和 stock 沪深 A 股")
        if kind == "ths_board" and not code.startswith("88"):
            raise AlertError("同花顺板块指数代码应以 88 开头")
        if kind == "stock" and not code.startswith(("0", "3", "6")):
            raise AlertError("股票提醒当前支持 0、3、6 开头的沪深 A 股代码")
        name = str(item.get("name") or ("半导体行业指数" if code == "881121" else code)).strip()[:60]
        symbols[code] = Symbol(code, name, kind)
    return Config(value.get("enabled", True), sender, recipient, tuple(symbols.values()))


class MarketData:
    """Fetch dated 14:30 bars; never substitute an undated spot quote."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    @staticmethod
    def trade_dates() -> list[date]:
        import akshare as ak
        import pandas as pd

        raw = ak.tool_trade_date_hist_sina()
        days = sorted(set(pd.to_datetime(raw["trade_date"]).dt.date))
        if not days:
            raise AlertError("交易日历为空")
        return days

    @staticmethod
    def _jsonp(url: str) -> dict:
        import requests

        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                "Referer": "https://q.10jqka.com.cn/"}, timeout=20)
        response.raise_for_status()
        text = response.text
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise AlertError("行情接口未返回有效 JSON")
        return json.loads(text[start:end + 1])

    def history(self, symbol: Symbol, today: date):
        import pandas as pd
        import macd_adx_backtest as base
        from history_cache import write_history
        from webapp.backend.config import ASSETS
        from webapp.backend.data_service import _normalize

        cache = self.cache_dir / f"{symbol.code}.csv"
        seed = ASSETS.get(symbol.code, {}).get("path")
        existing = pd.DataFrame()
        path = cache if cache.exists() else seed
        if path and path.exists():
            existing = base.load_history(path).reset_index()
            existing = existing[existing["date"] < pd.Timestamp(today)]
        if symbol.kind == "ths_board":
            start_year = int(existing["date"].max().year) if not existing.empty else 2018
            rows = []
            for year in range(start_year, today.year + 1):
                payload = self._jsonp(f"https://d.10jqka.com.cn/v4/line/bk_{symbol.code}/01/{year}.js")
                rows.extend(row.split(",")[:7] for row in payload.get("data", "").split(";") if row)
            if not rows:
                raise AlertError(f"{symbol.code} 历史接口没有返回数据")
            downloaded = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "money"])
        else:
            import akshare as ak

            # Refresh the full QFQ series: corporate actions can rescale old prices.
            downloaded = ak.stock_zh_a_hist(symbol=symbol.code, period="daily", start_date="20180101",
                                            end_date=(today - timedelta(days=1)).strftime("%Y%m%d"), adjust="qfq")
            existing = pd.DataFrame()
        frame = _normalize(pd.concat([existing, _normalize(downloaded)], ignore_index=True))
        frame = frame[frame["date"] < pd.Timestamp(today)]
        write_history(frame, cache)
        return frame.set_index("date")

    def snapshot(self, symbol: Symbol, today: date) -> Snapshot:
        import pandas as pd

        target = datetime.combine(today, CHECK_TIME, MARKET_TZ)
        if symbol.kind == "ths_board":
            payload = self._jsonp(f"https://d.10jqka.com.cn/v6/time/bk_{symbol.code}/last.js")
            board = payload.get(f"bk_{symbol.code}", {})
            if str(board.get("date")) != today.strftime("%Y%m%d"):
                raise AlertError(f"{symbol.code} 分钟行情不是当日数据")
            points = []
            for row in board.get("data", "").split(";"):
                fields = row.split(",")
                if len(fields) >= 2 and re.fullmatch(r"\d{4}", fields[0]) and "0930" <= fields[0] <= "1430":
                    points.append((fields[0], float(fields[1])))
            points.sort()
            if not points or points[-1][0] != "1430":
                raise AlertError(f"{symbol.code} 缺少当天 14:30 分钟行情")
            prices = [price for _, price in points]
            return Snapshot(target, prices[-1], prices[0], max(prices), min(prices), 0.0, 0.0, "同花顺 14:30 分钟行情")

        import akshare as ak

        raw = ak.stock_zh_a_hist_min_em(symbol=symbol.code,
                start_date=f"{today} 09:30:00", end_date=f"{today} 14:30:00", period="1", adjust="")
        if raw.empty or "时间" not in raw or "收盘" not in raw:
            raise AlertError(f"{symbol.code} 没有股票分钟行情")
        frame = raw.copy()
        frame["timestamp"] = pd.to_datetime(frame["时间"])
        frame = frame[(frame["timestamp"].dt.date == today) &
                      (frame["timestamp"] <= pd.Timestamp(target.replace(tzinfo=None)))].sort_values("timestamp")
        if frame.empty or frame["timestamp"].iloc[-1].time() != CHECK_TIME:
            raise AlertError(f"{symbol.code} 缺少当天 14:30 分钟行情（可能停牌）")
        prices = pd.to_numeric(frame["收盘"], errors="coerce")
        return Snapshot(target, float(prices.iloc[-1]), float(prices.iloc[0]), float(prices.max()),
                        float(prices.min()), 0.0, 0.0, "东方财富 14:30 分钟行情")


def evaluate(symbol: Symbol, history, quote: Snapshot, now: datetime, previous_day: date) -> dict:
    import pandas as pd
    import macd_adx_backtest as base
    import macd_adx_semiconductor_web as strategy

    today = now.astimezone(MARKET_TZ).date()
    target = datetime.combine(today, CHECK_TIME, MARKET_TZ)
    if quote.timestamp != target or quote.timestamp > now:
        raise AlertError("仅允许使用当天 14:30 且不晚于当前时间的行情")
    if not all(math.isfinite(value) and value > 0 for value in (quote.price, quote.opening, quote.high, quote.low)):
        raise AlertError("14:30 行情价格无效")
    if not (quote.low <= quote.price <= quote.high):
        raise AlertError("14:30 行情高低价不一致")
    complete = history[history.index < pd.Timestamp(today)].copy().sort_index()
    if len(complete) < 35 or complete.index[-1].date() != previous_day:
        raise AlertError("历史日线不足或缺少上一交易日数据，跳过信号计算")
    if complete.index.duplicated().any() or not (complete["close"] > 0).all() or not all(
            math.isfinite(float(value)) for value in complete["close"]):
        raise AlertError("历史收盘数据无效或重复")
    if not (complete.index >= strategy.START).any():
        raise AlertError("缺少策略起始日之后的历史数据")
    bar = pd.DataFrame([{"open": quote.opening, "close": quote.price, "high": quote.high,
                         "low": quote.low, "volume": quote.volume, "money": quote.money}],
                       index=pd.DatetimeIndex([pd.Timestamp(today)], name="date"))
    frame = base.add_indicators(pd.concat([complete, bar]))
    preview = strategy.preview_macd_cross(frame, now=target)
    latest = frame.iloc[-1]
    return {"code": symbol.code, "name": symbol.name, "action": preview["action"],
            "reason": preview["reason"], "position": preview["position"], "signal_date": today.isoformat(),
            "quote_time": quote.timestamp.isoformat(), "price": quote.price, "source": quote.source,
            "dif": float(latest.dif), "dea": float(latest.dea), "histogram": float(2 * (latest.dif - latest.dea))}


def atomic_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".json", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "sent": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AlertError("发送记录无法读取，停止发送以免重复") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("sent"), list) or not all(
            isinstance(item, str) for item in value["sent"]):
        raise AlertError("发送记录格式错误，停止发送以免重复")
    return value


def message_for(config: Config, signal: dict | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = config.recipient
    message["Date"] = format_datetime(market_now())
    message["Message-ID"] = make_msgid(domain="qq.com")
    if signal is None:
        message["Subject"] = "量化策略邮件通知：QQ SMTP 测试"
        message.set_content("这是一封手动触发的测试邮件，用于检查 QQ 邮箱 SMTP 配置。\n此邮件不代表任何交易信号。")
        return message
    direction = "买入（金叉）" if signal["action"] == "BUY_AT_CLOSE" else "卖出（死叉）"
    message["Subject"] = f"[MACD {direction}] {signal['code']} {signal['name']} · {signal['signal_date']} 14:30"
    message.set_content(
        f"标的：{signal['code']} · {signal['name']}\n信号：{direction}\n"
        f"策略：MACD 金叉买入 / 死叉卖出（12 / 26 / 9）\n"
        f"行情时间：{signal['quote_time']}（北京时间）\n价格：{signal['price']:.4f}\n"
        f"DIF：{signal['dif']:.6f}\nDEA：{signal['dea']:.6f}\nMACD 柱：{signal['histogram']:.6f}\n"
        f"原因：{signal['reason']}\n模拟策略仓位：{signal['position']}\n来源：{signal['source']}\n\n"
        "使用当天 14:30 分钟价格构造临时日线，信号在收盘前可能变化。\n"
        "邮件只提供策略信号，不会执行交易。实际送达时间可能因 GitHub Actions 排队而延迟。\n"
    )
    return message


def send_mail(config: Config, message: EmailMessage) -> None:
    password = os.environ.get("QQ_SMTP_AUTH_CODE", "").strip()
    if not password:
        raise AlertError("缺少 GitHub Secret：QQ_SMTP_AUTH_CODE（QQ 邮箱 SMTP 授权码）")
    smtp = None
    try:
        smtp = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30, context=ssl.create_default_context())
        smtp.login(config.sender, password)
        refused = smtp.send_message(message)
        if refused:
            raise AlertError("QQ SMTP 拒绝接收邮件")
    except (smtplib.SMTPException, OSError) as exc:
        # Never log authentication values or the raw SMTP exchange.
        raise AlertError(f"QQ SMTP 发送失败（{type(exc).__name__}），请检查授权码和 SMTP 服务") from None
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                # A failed QUIT must not turn an acknowledged delivery into a retry.
                smtp.close()


def check(config: Config, provider: MarketData, state_path: Path, now: datetime,
          dry_run: bool = False, mailer=send_mail) -> list[dict]:
    now = now.astimezone(MARKET_TZ)
    allowed, reason = execution_window(now, "dry_run" if dry_run else "check")
    if not config.enabled or not allowed:
        return [{"status": "skipped", "reason": reason if config.enabled else "邮件提醒已关闭"}]
    days = provider.trade_dates()
    if not days or days[-1] < now.date():
        raise AlertError("交易日历未覆盖今天，停止检查")
    if now.date() not in days:
        return [{"status": "skipped", "reason": "今天不是 A 股交易日"}]
    previous = [day for day in days if day < now.date()]
    if not previous:
        raise AlertError("交易日历缺少上一交易日")
    state = load_state(state_path)
    results = []
    for symbol in config.symbols:
        try:
            history = provider.history(symbol, now.date())
            quote = provider.snapshot(symbol, now.date())
            signal = evaluate(symbol, history, quote, now, previous[-1])
            record = {**signal, "status": "no_signal"}
            if signal["action"] in {"BUY_AT_CLOSE", "SELL_AT_CLOSE"}:
                key = f"{signal['signal_date']}|{symbol.code}|{signal['action']}|{config.recipient.lower()}"
                if key in state["sent"]:
                    record["status"] = "already_sent"
                elif dry_run:
                    record["status"] = "dry_run"
                else:
                    # Check again after network calls so a slow request cannot send after close.
                    if not execution_window(market_now(), "check")[0] or market_now().date() != now.date():
                        raise AlertError("行情获取结束时已超出当天发送窗口")
                    mailer(config, message_for(config, signal))
                    state["sent"].append(key)
                    atomic_json(state_path, state)
                    record["status"] = "sent"
            results.append(record)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, AlertError) else f"行情处理失败（{type(exc).__name__}）"
            results.append({"code": symbol.code, "status": "error", "reason": reason})
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["check", "dry_run", "test_email"], default="dry_run")
    parser.add_argument("--gate", action="store_true", help="Output time gate values for GitHub Actions, without loading dependencies")
    parser.add_argument("--config", type=Path, default=ROOT / "alerts" / "config.json")
    parser.add_argument("--state-dir", type=Path, default=Path(".alert-state"))
    args = parser.parse_args(argv)
    now = market_now()
    if args.gate:
        allowed, reason = execution_window(now, args.mode)
        values = f"run={'true' if allowed else 'false'}\nartifact_name=macd-mail-state-{now:%Y-%m-%d}\n"
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(values)
        print(values + reason)
        return 0
    try:
        config = load_config(args.config)
        if args.mode == "test_email":
            send_mail(config, message_for(config))
            results = [{"status": "test_email_sent"}]
        else:
            results = check(config, MarketData(args.state_dir / "history"), args.state_dir / "state.json",
                            now, dry_run=args.mode == "dry_run")
    except Exception as exc:
        results = [{"status": "error", "reason": str(exc) if isinstance(exc, AlertError) else
                    f"提醒任务失败（{type(exc).__name__}）"}]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(f"### MACD 邮件提醒 · 北京时间 {now:%Y-%m-%d %H:%M}\n\n")
            for result in results:
                output.write(f"- {result.get('code', '任务')}: {result['status']} · {result.get('action', '')} "
                             f"{result.get('reason', '')}\n")
    return 1 if any(result["status"] == "error" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
