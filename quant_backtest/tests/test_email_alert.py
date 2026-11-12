from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import os
from pathlib import Path
import smtplib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from macd_email_alert import (
    AlertError, Config, MarketData, Snapshot, Symbol, check, evaluate, execution_window,
    load_config, load_state, main, message_for, send_mail,
)
from market_time import MARKET_TZ

NOW = datetime(2026, 11, 12, 14, 35, tzinfo=MARKET_TZ)
SYMBOL = Symbol("881121", "半导体行业指数", "ths_board")
CONFIG = Config(True, "1793951795@qq.com", "1556444893@qq.com", (SYMBOL,))


def history(prices=None):
    values = np.linspace(200, 100, 100) if prices is None else prices
    return pd.DataFrame({"open": values, "close": values, "high": values + 1,
                         "low": values - 1, "volume": 100.0, "money": 10000.0},
                        index=pd.bdate_range(end="2026-11-11", periods=len(values), name="date"))


def snapshot(price=1000.0):
    return Snapshot(datetime(2026, 11, 12, 14, 30, tzinfo=MARKET_TZ), price, price, price, price,
                    100.0, 10000.0, "模拟 14:30 行情")


def provider(frame=None, quote=None):
    result = Mock(spec=MarketData)
    frame = history() if frame is None else frame
    result.history.return_value = frame
    result.snapshot.return_value = snapshot() if quote is None else quote
    result.trade_dates.return_value = sorted(set(frame.index.date) | {NOW.date(), date(2026, 11, 13)})
    return result


class ConfigAndWindowTests(unittest.TestCase):
    def test_config_defaults_and_custom_codes(self):
        config = load_config(ROOT / "alerts/config.json", environ={})
        self.assertEqual(config, CONFIG)
        override = load_config(ROOT / "alerts/config.json", environ={"MACD_ALERT_CODES": "881121, 600000，884229"})
        self.assertEqual([item.code for item in override.symbols], ["881121", "600000", "884229"])
        self.assertEqual(override.symbols[1].kind, "stock")
        for code in ("../bad", "881121;echo hi", "830001"):
            with self.subTest(code=code), self.assertRaises(AlertError):
                load_config(ROOT / "alerts/config.json", environ={"MACD_ALERT_CODES": code})

    def test_window_uses_beijing_time_and_rejects_late_sends(self):
        for hour, minute, expected in ((14, 29, False), (14, 30, True), (14, 59, True), (15, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(execution_window(NOW.replace(hour=hour, minute=minute), "check")[0], expected)
        self.assertTrue(execution_window(datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc), "check")[0])
        self.assertFalse(execution_window(NOW.replace(day=14), "check")[0])
        self.assertTrue(execution_window(NOW.replace(hour=20), "dry_run")[0])
        self.assertTrue(execution_window(NOW.replace(day=14), "test_email")[0])


class StrategyTests(unittest.TestCase):
    def test_buy_sell_hold_match_existing_strategy(self):
        cases = [
            (history(), snapshot(1000), "BUY_AT_CLOSE"),
            (history(np.concatenate([np.linspace(200, 100, 50), np.linspace(100, 200, 50)])), snapshot(1), "SELL_AT_CLOSE"),
            (history(), snapshot(99), "HOLD"),
        ]
        for frame, quote, expected in cases:
            with self.subTest(expected=expected):
                signal = evaluate(SYMBOL, frame, quote, NOW, date(2026, 11, 11))
                self.assertEqual(signal["action"], expected)
                self.assertAlmostEqual(signal["histogram"], 2 * (signal["dif"] - signal["dea"]))
                self.assertEqual(evaluate(SYMBOL, frame, quote, NOW.replace(hour=16), date(2026, 11, 11)), signal)

    def test_stale_quote_missing_history_and_future_daily_rows(self):
        frame = history()
        stale = Snapshot(datetime(2026, 11, 11, 14, 30, tzinfo=MARKET_TZ), 1000, 1000, 1000, 1000, 0, 0, "旧行情")
        with self.assertRaises(AlertError):
            evaluate(SYMBOL, frame, stale, NOW, date(2026, 11, 11))
        with self.assertRaises(AlertError):
            evaluate(SYMBOL, frame.iloc[:-1], snapshot(), NOW, date(2026, 11, 11))
        contaminated = pd.concat([frame, pd.DataFrame({"open": [1], "close": [1], "high": [1], "low": [1],
                    "volume": [0], "money": [0]}, index=pd.to_datetime(["2026-11-12"]))])
        self.assertEqual(evaluate(SYMBOL, contaminated, snapshot(), NOW, date(2026, 11, 11)),
                         evaluate(SYMBOL, frame, snapshot(), NOW, date(2026, 11, 11)))

    def test_ths_minute_parser_uses_exact_1430_without_future_prices(self):
        market = MarketData(Path("unused"))
        payload = {"bk_881121": {"date": "20261112", "data": "0930,90;1429,95;1430,100;1431,9000;1500,1"}}
        with patch.object(market, "_jsonp", return_value=payload):
            quote = market.snapshot(SYMBOL, NOW.date())
        self.assertEqual((quote.price, quote.high, quote.low), (100, 100, 90))
        for board in ({"date": "20261111", "data": "1430,100"},
                      {"date": "20261112", "data": "1429,100;1431,500"}):
            with patch.object(market, "_jsonp", return_value={"bk_881121": board}), self.assertRaises(AlertError):
                market.snapshot(SYMBOL, NOW.date())

    def test_stock_minute_parser_rejects_missing_bar_and_ignores_later_data(self):
        market = MarketData(Path("unused"))
        stock = Symbol("600000", "测试股票", "stock")
        minutes = pd.DataFrame({"时间": ["2026-11-12 09:30:00", "2026-11-12 14:30:00", "2026-11-12 14:31:00"],
                                "收盘": [10, 11, 500]})
        with patch("akshare.stock_zh_a_hist_min_em", return_value=minutes):
            quote = market.snapshot(stock, NOW.date())
            self.assertEqual((quote.price, quote.high), (11, 11))
        with patch("akshare.stock_zh_a_hist_min_em", return_value=minutes.iloc[[0, 2]]), self.assertRaises(AlertError):
            market.snapshot(stock, NOW.date())


class DeliveryTests(unittest.TestCase):
    def test_sends_once_per_date_code_direction_recipient_across_runs(self):
        with tempfile.TemporaryDirectory() as temporary, patch("macd_email_alert.market_now", return_value=NOW):
            state = Path(temporary) / "state.json"
            mailer = Mock()
            first = check(CONFIG, provider(), state, NOW, mailer=mailer)
            second = check(CONFIG, provider(), state, NOW, mailer=mailer)
            self.assertEqual([first[0]["status"], second[0]["status"]], ["sent", "already_sent"])
            self.assertEqual(mailer.call_count, 1)
            self.assertEqual(len(load_state(state)["sent"]), 1)
            message = mailer.call_args.args[1]
            self.assertIn("买入", message["Subject"])
            self.assertEqual(message["To"], CONFIG.recipient)
            self.assertIn("14:30", message.get_content())

    def test_no_signal_dry_run_holidays_and_unavailable_calendar_never_send(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            mailer = Mock()
            result = check(CONFIG, provider(quote=snapshot(99)), state, NOW, mailer=mailer)
            self.assertEqual(result[0]["status"], "no_signal")
            result = check(CONFIG, provider(), state, NOW, dry_run=True, mailer=mailer)
            self.assertEqual(result[0]["status"], "dry_run")
            market = provider()
            market.trade_dates.return_value = [date(2026, 11, 11), date(2026, 11, 13)]
            self.assertEqual(check(CONFIG, market, state, NOW, mailer=mailer)[0]["status"], "skipped")
            market.history.assert_not_called()
            market.trade_dates.return_value = [date(2025, 12, 31)]
            with self.assertRaises(AlertError):
                check(CONFIG, market, state, NOW, mailer=mailer)
            mailer.assert_not_called()
            self.assertFalse(state.exists())

    def test_failed_send_or_slow_fetch_does_not_mark_delivered(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            failed_mailer = Mock(side_effect=AlertError("模拟发送失败"))
            with patch("macd_email_alert.market_now", return_value=NOW):
                result = check(CONFIG, provider(), state, NOW, mailer=failed_mailer)
            self.assertEqual(result[0]["status"], "error")
            self.assertFalse(state.exists())
            mailer = Mock()
            with patch("macd_email_alert.market_now", return_value=NOW.replace(hour=15)):
                result = check(CONFIG, provider(), state, NOW, mailer=mailer)
            self.assertEqual(result[0]["status"], "error")
            mailer.assert_not_called()

    def test_malformed_delivery_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            state.write_text("broken", encoding="utf-8")
            mailer = Mock()
            with self.assertRaises(AlertError):
                check(CONFIG, provider(), state, NOW, mailer=mailer)
            mailer.assert_not_called()

    def test_smtp_uses_ssl_auth_code_and_redacts_failures(self):
        password = "test-only-authorization-code"
        smtp = Mock()
        smtp.send_message.return_value = {}
        smtp.quit.side_effect = smtplib.SMTPResponseException(-1, b"Disconnected on QUIT")
        with patch.dict(os.environ, {"QQ_SMTP_AUTH_CODE": password}), patch(
                "smtplib.SMTP_SSL", return_value=smtp) as factory:
            send_mail(CONFIG, message_for(CONFIG))
            self.assertEqual(factory.call_args.args, ("smtp.qq.com", 465))
            smtp.login.assert_called_once_with(CONFIG.sender, password)
            smtp.send_message.assert_called_once()
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, password.encode())
        with patch.dict(os.environ, {"QQ_SMTP_AUTH_CODE": password}), patch(
                "smtplib.SMTP_SSL", return_value=smtp), self.assertRaises(AlertError) as caught:
            send_mail(CONFIG, message_for(CONFIG))
        self.assertNotIn(password, str(caught.exception))
        with patch.dict(os.environ, {"QQ_SMTP_AUTH_CODE": ""}), self.assertRaisesRegex(AlertError, "QQ_SMTP_AUTH_CODE"):
            send_mail(CONFIG, message_for(CONFIG))


if __name__ == "__main__":
    unittest.main()
