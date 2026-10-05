# -*- coding: utf-8 -*-
"""
持仓异动监控脚本（美股/港股指数/贵金属）

用法:
    python monitor.py
"""

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import yaml


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8))
UTC_TZ = dt.timezone.utc
GROUP_LABELS = {
    "us": "美股",
    "hk": "港股科技",
    "metal": "黄金",
}
GROUP_ORDER = ("us", "hk", "metal")


def load_dotenv():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()


def beijing_now():
    return dt.datetime.now(BEIJING_TZ)


def utc_now():
    return dt.datetime.now(UTC_TZ)


def state_date():
    # UTC date keeps a US session on one logical monitoring date.
    return utc_now().date().isoformat()


def alert_group_key(cfg, alert):
    group = str(alert.get("group", "")).strip().lower()
    if group in GROUP_LABELS:
        return group

    symbol = str(alert.get("symbol", ""))
    for position in cfg.get("positions", []):
        if str(position.get("symbol", "")) == symbol:
            market = str(position.get("market", "us")).strip().lower()
            return market if market in GROUP_LABELS else "us"
    return "us"


def group_label(group):
    return GROUP_LABELS.get(group, GROUP_LABELS["us"])


def log_label(value):
    salt = (os.environ.get("LOG_SALT") or os.environ.get("STATE_KEY") or "local")
    digest = hmac.new(
        salt.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:8]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg.get("positions"), list) or not cfg["positions"]:
        raise ValueError("config.yaml 中 positions 不能为空")
    for key in (
        "daily_change_pct",
        "cost_change_pct",
        "portfolio_daily_pnl",
        "volume_ratio",
        "price_gap_pct",
    ):
        if key not in cfg.get("alerts", {}):
            raise ValueError(f"config.yaml 中 alerts.{key} 未配置")
    return cfg


def setup_logging(cfg):
    log_path = BASE_DIR / cfg["runtime"]["log_file"]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def is_dst_us(d: dt.date) -> bool:
    """Return whether US daylight saving time is active on a given date."""
    mar1 = dt.date(d.year, 3, 1)
    dst_start = mar1 + dt.timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = dt.date(d.year, 11, 1)
    dst_end = nov1 + dt.timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_start <= d < dst_end


def in_market_hours(now: dt.datetime, cfg) -> bool:
    if cfg["runtime"].get("monitor_24h", False):
        return True
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING_TZ)

    utc_date = now.astimezone(UTC_TZ).date()
    eastern_offset = -4 if is_dst_us(utc_date) else -5
    eastern_now = now.astimezone(
        dt.timezone(dt.timedelta(hours=eastern_offset))
    )
    if eastern_now.weekday() >= 5:
        return False

    rt = cfg["runtime"]
    if is_dst_us(eastern_now.date()):
        open_t = dt.datetime.strptime(rt["market_open_cst"], "%H:%M").time()
        close_t = dt.datetime.strptime(rt["market_close_cst"], "%H:%M").time()
    else:
        open_t = dt.datetime.strptime(rt["winter_open_cst"], "%H:%M").time()
        close_t = dt.datetime.strptime(rt["winter_close_cst"], "%H:%M").time()

    current_t = now.time()
    if open_t > close_t:
        return current_t >= open_t or current_t <= close_t
    return open_t <= current_t <= close_t


def _sina_code(symbol, market):
    if market == "us":
        return f"gb_{symbol.lower()}"
    if market == "hk":
        return f"rt_hk{symbol}"
    if market == "metal":
        return f"hf_{symbol}"
    return f"gb_{symbol.lower()}"


def _parse_quote(fields, market, symbol):
    if market == "us":
        if len(fields) < 11:
            return None
        price = float(fields[1])
        change_pct = float(fields[2])
        change_amt = float(fields[4])
        return {
            "name": fields[0],
            "price": price,
            "change_pct": change_pct,
            "change_amt": change_amt,
            "prev_close": price - change_amt,
            "open": float(fields[5]) if fields[5] else 0,
            "high": float(fields[6]) if fields[6] else 0,
            "low": float(fields[7]) if fields[7] else 0,
            "volume": float(fields[10]) if fields[10] else 0,
        }

    if market == "hk":
        if len(fields) < 9:
            return None
        # HK format: symbol,name,open,prev_close,high,low,price,change,change_pct
        return {
            "name": fields[1],
            "price": float(fields[6]),
            "change_pct": float(fields[8]),
            "change_amt": float(fields[7]),
            "prev_close": float(fields[3]),
            "open": float(fields[2]) if fields[2] else 0,
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": 0,
        }

    if market == "metal":
        if len(fields) < 8:
            return None
        price = float(fields[0])
        prev_close = float(fields[7])
        change_amt = price - prev_close
        return {
            "name": fields[-1] if fields[-1] else symbol,
            "price": price,
            "change_pct": change_amt / prev_close * 100 if prev_close > 0 else 0,
            "change_amt": change_amt,
            "prev_close": prev_close,
            "open": float(fields[3]) if fields[3] else 0,
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": 0,
        }

    return None


def parse_sina_response(text, positions):
    result = {}
    for pos in positions:
        symbol = pos["symbol"]
        market = pos.get("market", "us")
        code = _sina_code(symbol, market)
        match = re.search(f'var hq_str_{re.escape(code)}="([^"]*)"', text)
        if not match or not match.group(1):
            logging.warning("%s(%s): 新浪无数据", log_label(symbol), market)
            continue
        try:
            quote = _parse_quote(match.group(1).split(","), market, symbol)
        except (ValueError, IndexError) as exc:
            logging.warning("%s: 数据解析失败 %s", log_label(symbol), exc)
            continue
        if quote:
            result[symbol] = quote
    return result


def fetch_sina_realtime(positions):
    codes = [_sina_code(p["symbol"], p.get("market", "us")) for p in positions]
    url = f"https://hq.sinajs.cn/list={','.join(codes)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://finance.sina.com.cn",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        response.encoding = "gbk"
    except Exception as exc:
        logging.error("新浪行情请求失败: %s", exc)
        return {}
    return parse_sina_response(response.text, positions)


def update_volume_history(symbol, volume, state):
    today = state_date()
    hist_key = f"vol_hist_{symbol}"
    lastdate_key = f"vol_lastdate_{symbol}"
    today_key = f"vol_today_{symbol}"
    state.setdefault(hist_key, [])

    last_date = state.get(lastdate_key)
    if last_date and last_date != today:
        previous_volume = state.get(today_key, 0)
        history = state[hist_key]
        if previous_volume > 0 and (
            not history or history[-1][0] != last_date
        ):
            history.append([last_date, previous_volume])
            del history[:-25]

    state[lastdate_key] = today
    state[today_key] = volume


def get_avg_volume(symbol, state):
    history = state.get(f"vol_hist_{symbol}", [])
    if len(history) >= 5:
        volumes = [item[1] for item in history[-20:]]
        return sum(volumes) / len(volumes)

    avg_key = f"vol_em_avg_{symbol}"
    tried_key = f"vol_em_tried_{symbol}_{state_date()}"
    average = float(state.get(avg_key, 0) or 0)
    if average <= 0 and tried_key not in state:
        state[tried_key] = True
        average = _try_fetch_em_avg_volume(symbol)
        if average > 0:
            state[avg_key] = average
    return average


def _try_fetch_em_avg_volume(symbol):
    import time

    for attempt in range(2):
        for market in (105, 106, 107):
            url = (
                "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
                f"secid={market}.{symbol.upper()}&"
                "fields1=f1,f2,f3,f4,f5,f6&"
                "fields2=f51,f52,f53,f54,f55,f56,f57&"
                "klt=101&fqt=1&end=20500101&lmt=25"
            )
            try:
                response = requests.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
                klines = (data.get("data") or {}).get("klines", [])
                if len(klines) < 2:
                    continue
                previous_klines = klines[:-1]
                volumes = [
                    float(line.split(",")[5])
                    for line in previous_klines[-20:]
                ]
                average = sum(volumes) / len(volumes) if volumes else 0
                if average > 0:
                    return average
            except Exception:
                continue
        if attempt == 0:
            time.sleep(1)
    return 0


def detect_alerts(cfg, quotes, state, quotes_complete=True):
    alerts = []
    group_pnl = {group: 0.0 for group in GROUP_ORDER}
    alerts_cfg = cfg["alerts"]

    for pos in cfg["positions"]:
        symbol = pos["symbol"]
        if symbol not in quotes:
            continue

        quote = quotes[symbol]
        quantity = float(pos.get("qty", 1) or 0)
        cost = float(pos.get("cost_price", 0) or 0)
        market = pos.get("market", "us")
        current = quote["price"]
        prev_close = quote["prev_close"]
        daily_change_pct = quote["change_pct"]

        if market == "us":
            today_volume = quote["volume"]
            update_volume_history(symbol, today_volume, state)
            avg_volume = get_avg_volume(symbol, state)
            volume_ratio = (
                today_volume / avg_volume if avg_volume > 0 else 0
            )
        else:
            today_volume = 0
            avg_volume = 0
            volume_ratio = 0

        cost_change_pct = (
            (current - cost) / cost * 100 if cost > 0 else 0
        )
        group_pnl.setdefault(market, 0.0)
        group_pnl[market] += quantity * (current - prev_close)

        last_price_key = f"lastprice_{symbol}"
        previous_price = float(state.get(last_price_key, 0) or 0)
        gap_pct = (
            (current - previous_price) / previous_price * 100
            if previous_price > 0
            else 0
        )
        state[last_price_key] = current

        name = pos.get("name", symbol)
        logical_date = state_date()
        if abs(daily_change_pct) >= alerts_cfg["daily_change_pct"]:
            alerts.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "type": "日内异动",
                    "msg": (
                        f"{name}({symbol}) 日内 {daily_change_pct:+.2f}%，"
                        f"现价 {current:.2f}"
                    ),
                    "group": market,
                    "key": f"{symbol}_{logical_date}_daily",
                }
            )
        if cost > 0 and abs(cost_change_pct) >= alerts_cfg["cost_change_pct"]:
            alerts.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "type": "成本线突破",
                    "msg": (
                        f"{name}({symbol}) 较成本 {cost_change_pct:+.2f}%"
                        f"（成本{cost:.2f}，现价{current:.2f}）"
                    ),
                    "group": market,
                    "key": f"{symbol}_{logical_date}_cost",
                }
            )
        if (
            market == "us"
            and volume_ratio >= alerts_cfg["volume_ratio"]
            and today_volume > 0
        ):
            alerts.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "type": "放量异动",
                    "msg": (
                        f"{name}({symbol}) 放量 {volume_ratio:.1f}倍"
                        f"（今日{today_volume / 1e6:.1f}M / "
                        f"均量{avg_volume / 1e6:.1f}M）"
                    ),
                    "group": market,
                    "key": f"{symbol}_{logical_date}_volume",
                }
            )
        if (
            abs(gap_pct) >= alerts_cfg["price_gap_pct"]
            and gap_pct != 0
        ):
            bucket = int(utc_now().timestamp() // 300)
            alerts.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "type": "短时跳价",
                    "msg": (
                        f"{name}({symbol}) 近5分钟 {gap_pct:+.2f}%，"
                        f"现价 {current:.2f}"
                    ),
                    "group": market,
                    "key": f"{symbol}_{logical_date}_gap_{bucket}",
                }
            )

    for group in GROUP_ORDER:
        pnl = group_pnl.get(group, 0.0)
        if (
            not quotes_complete
            or abs(pnl) < alerts_cfg["portfolio_daily_pnl"]
        ):
            continue

        if group == "us":
            msg = f"美股组合当日合计盈亏 ${pnl:+.2f}"
        elif group == "hk":
            msg = f"港股科技当日变动 {pnl:+.2f} 点"
        else:
            msg = f"黄金当日变动 ${pnl:+.2f}"

        alerts.append(
            {
                "symbol": f"PORTFOLIO_{group.upper()}",
                "name": group_label(group),
                "type": "组合盈亏",
                "msg": msg,
                "group": group,
                "key": f"PORTFOLIO_{group.upper()}_{state_date()}_pnl",
            }
        )

    return alerts, group_pnl


def _state_cipher():
    key = os.environ.get("STATE_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode("ascii"))


def _ledger_path(cfg):
    return BASE_DIR / cfg["runtime"].get(
        "ledger_file", "alerted_ledger.json"
    )


def _ledger_key():
    secret = (
        os.environ.get("EMAIL_PASSWORD", "").strip()
        or os.environ.get("STATE_KEY", "").strip()
    )
    if not secret:
        return ""
    return hashlib.sha256(f"monitor-ledger:{secret}".encode("utf-8")).hexdigest()


def alert_fingerprint(alert_key):
    key = _ledger_key()
    material = str(alert_key).encode("utf-8")
    if not key:
        return hashlib.sha256(material).hexdigest()
    return hmac.new(key.encode("utf-8"), material, hashlib.sha256).hexdigest()


def is_alert_key(key):
    return (
        str(key).startswith("PORTFOLIO_")
        or str(key).endswith(("_daily", "_cost", "_volume"))
        or "_gap_" in str(key)
    )


def load_ledger(cfg):
    path = _ledger_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.error("去重账本读取失败: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}

    cutoff = utc_now() - dt.timedelta(days=30)
    cleaned = {}
    for fingerprint, sent_at in data.items():
        try:
            parsed = dt.datetime.fromisoformat(str(sent_at))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC_TZ)
            if parsed >= cutoff:
                cleaned[str(fingerprint)] = parsed.isoformat()
        except ValueError:
            continue
    return cleaned


def save_ledger(cfg, ledger):
    path = _ledger_path(cfg)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def load_state(cfg):
    path = BASE_DIR / cfg["runtime"]["state_file"]
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        cipher = _state_cipher()
        if cipher is not None:
            raw = cipher.decrypt(raw.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
    except Exception as exc:
        logging.error("状态文件读取失败: %s", exc)
        return {}

    today = state_date()
    cleaned = {}
    for key, value in data.items():
        if key == "pending_alerts" and isinstance(value, list):
            cleaned[key] = [
                item
                for item in value[-100:]
                if isinstance(item, dict) and item.get("key")
            ]
        elif key.startswith(("vol_", "lastprice_", "em_tried_")):
            cleaned[key] = value
        elif today in key:
            cleaned[key] = value
    return cleaned


def save_state(cfg, state):
    path = BASE_DIR / cfg["runtime"]["state_file"]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(state, ensure_ascii=False, indent=2)
    cipher = _state_cipher()
    payload = cipher.encrypt(raw.encode("utf-8")).decode("ascii") if cipher else raw
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def push_email(email_cfg, title, content):
    if not email_cfg.get("enabled"):
        return False
    username = email_cfg.get("username") or os.environ.get(
        "EMAIL_USERNAME", ""
    )
    password = os.environ.get("EMAIL_PASSWORD") or email_cfg.get(
        "password", ""
    )
    recipients_text = email_cfg.get("to") or os.environ.get("EMAIL_TO", "")
    if not username or not password or not recipients_text:
        logging.warning("邮件配置不完整，跳过")
        return False

    recipients = [
        item.strip() for item in recipients_text.split(",") if item.strip()
    ]
    if not recipients:
        logging.warning("邮件收件人为空，跳过")
        return False

    server = None
    try:
        msg = MIMEText(content, "plain", "utf-8")
        msg["From"] = formataddr(
            (str(Header("持仓异动监控", "utf-8")), username)
        )
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = Header(title, "utf-8")
        server = smtplib.SMTP_SSL(
            email_cfg["smtp_server"],
            email_cfg["smtp_port"],
            timeout=15,
        )
        server.login(username, password)
        server.sendmail(username, recipients, msg.as_string())
        logging.info("邮件推送成功")
        return True
    except Exception as exc:
        logging.error("邮件推送异常: %s", exc)
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def push_serverchan(sendkey, title, content):
    if not sendkey:
        return False
    url = f"https://sctapi.ftqq.com/{quote(sendkey, safe='')}.send"
    try:
        response = requests.post(
            url,
            data={"title": title, "desp": content},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0:
            logging.info("Server酱推送成功")
            return True
        logging.error("Server酱推送失败: %s", payload)
    except Exception as exc:
        logging.error("Server酱推送异常: %s", exc)
    return False


def push_wecom(webhook, title, content):
    if not webhook:
        return False
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com":
        logging.error("企业微信 webhook 地址不合法")
        return False
    try:
        response = requests.post(
            webhook,
            json={
                "msgtype": "text",
                "text": {"content": f"{title}\n\n{content}"},
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") == 0:
            logging.info("企业微信推送成功")
            return True
        logging.error("企业微信推送失败: %s", payload)
    except Exception as exc:
        logging.error("企业微信推送异常: %s", exc)
    return False


def send_alerts(cfg, alerts, group=None):
    if not alerts:
        return True
    group = group or alert_group_key(cfg, alerts[0])
    title = (
        f"【{group_label(group)}异动】"
        f"{len(alerts)}条提醒 {beijing_now().strftime('%H:%M')}"
    )
    content = "\n\n".join(
        f"⚠️ {alert['type']}\n{alert['msg']}" for alert in alerts
    )
    push_cfg = cfg.get("push", {})
    sent = False

    email_cfg = push_cfg.get("email", {})
    if email_cfg.get("enabled"):
        sent = push_email(email_cfg, title, content) or sent
    if push_cfg.get("serverchan_sendkey"):
        sent = (
            push_serverchan(
                push_cfg["serverchan_sendkey"], title, content
            )
            or sent
        )
    if push_cfg.get("wecom_webhook"):
        sent = push_wecom(push_cfg["wecom_webhook"], title, content) or sent

    if not sent:
        logging.warning("未配置任何推送渠道，或全部推送失败")
    return sent


def main():
    cfg = load_config()
    setup_logging(cfg)

    if not in_market_hours(beijing_now(), cfg):
        logging.info(
            "非交易时段 (%s)，退出", beijing_now().strftime("%Y-%m-%d %H:%M")
        )
        return

    positions = cfg["positions"]
    symbols = [position["symbol"] for position in positions]
    logging.info("开始检测 %s 只标的", len(symbols))

    state = load_state(cfg)
    ledger = load_ledger(cfg)
    for key, value in state.items():
        if is_alert_key(key) and value != "pending":
            ledger.setdefault(alert_fingerprint(key), str(value))
    logging.info(
        "状态项 %s 个，去重账本 %s 条",
        len(state),
        len(ledger),
    )
    quotes = fetch_sina_realtime(positions)
    if not quotes:
        logging.error("未获取到任何行情数据，退出")
        return

    quotes_complete = all(symbol in quotes for symbol in symbols)
    logging.info(
        "成功获取 %s/%s 只标的行情",
        len(quotes),
        len(symbols),
    )

    alerts, group_pnl = detect_alerts(
        cfg, quotes, state, quotes_complete=quotes_complete
    )
    if quotes_complete:
        logging.info("行情检测完成，触发异动 %s 条", len(alerts))
    else:
        logging.warning(
            "行情不完整，触发异动 %s 条，已跳过组合盈亏提醒",
            len(alerts),
        )
    logging.info(
        "分组当日盈亏: 美股 $%+.2f, 港股科技 %+.2f 点, 黄金 $%+.2f",
        group_pnl.get("us", 0.0),
        group_pnl.get("hk", 0.0),
        group_pnl.get("metal", 0.0),
    )

    pending = state.setdefault("pending_alerts", [])
    pending_keys = {alert["key"] for alert in pending}
    for alert in alerts:
        if alert_fingerprint(alert["key"]) in ledger:
            continue
        alert["group"] = alert_group_key(cfg, alert)
        if alert["key"] not in state:
            state[alert["key"]] = "pending"
        if alert["key"] not in pending_keys:
            pending.append(alert)
            pending_keys.add(alert["key"])
    state["pending_alerts"] = pending[-100:]

    if pending:
        sent_at = beijing_now().isoformat()
        pending_by_group = {group: [] for group in GROUP_ORDER}
        for alert in pending:
            group = alert_group_key(cfg, alert)
            pending_by_group.setdefault(group, []).append(alert)

        remaining = []
        sent_count = 0
        failed_groups = []
        other_groups = [
            group for group in pending_by_group
            if group not in GROUP_ORDER
        ]
        for group in list(GROUP_ORDER) + other_groups:
            group_pending = pending_by_group.get(group, [])
            if not group_pending:
                continue

            if send_alerts(cfg, group_pending, group):
                for alert in group_pending:
                    state[alert["key"]] = sent_at
                    ledger[alert_fingerprint(alert["key"])] = sent_at
                sent_count += len(group_pending)
            else:
                remaining.extend(group_pending)
                failed_groups.append(group_label(group))

        state["pending_alerts"] = remaining[-100:]
        if sent_count:
            logging.info("已成功推送 %s 条异动", sent_count)
        if failed_groups:
            logging.warning(
                "以下分组推送失败，保留待重试: %s",
                ", ".join(failed_groups),
            )

    save_state(cfg, state)
    save_ledger(cfg, ledger)


if __name__ == "__main__":
    main()
