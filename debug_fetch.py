# -*- coding: utf-8 -*-
"""只读调试脚本：获取行情并显示异动，不写入状态文件。"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor import (  # noqa: E402
    detect_alerts,
    fetch_fx_rates,
    fetch_sina_realtime,
    get_avg_volume,
    group_label,
    load_config,
    load_state,
)


cfg = load_config()
positions = cfg["positions"]
symbols = [position["symbol"] for position in positions]
print("获取行情:", symbols)

quotes = fetch_sina_realtime(positions)
print(f"成功获取 {len(quotes)}/{len(symbols)} 只")
print()

state = copy.deepcopy(load_state(cfg))
print("=== 各标的行情快照 ===")
for position in positions:
    symbol = position["symbol"]
    if symbol not in quotes:
        print(f"{symbol:6s} 无数据")
        continue
    quote = quotes[symbol]
    average = get_avg_volume(symbol, state)
    ratio = quote["volume"] / average if average > 0 else 0
    cost = float(position.get("cost_price", 0) or 0)
    cost_change = (
        (quote["price"] - cost) / cost * 100 if cost > 0 else 0
    )
    history_length = len(state.get(f"vol_hist_{symbol}", []))
    print(
        f"{symbol:6s} {quote['name']:14s} "
        f"现价{quote['price']:8.2f} "
        f"日涨跌{quote['change_pct']:+6.2f}% "
        f"较成本{cost_change:+6.2f}% "
        f"量比{ratio:.1f}x(均量{average / 1e6:.1f}M) "
        f"历史{history_length}天"
    )

fx = fetch_fx_rates(cfg)
print()
print(
    f"汇率: 美元/人民币 {fx['usd_cny']:.4f}, "
    f"港币/人民币 {fx['hkd_cny']:.4f}"
)

quotes_complete = all(symbol in quotes for symbol in symbols)
alerts, group_pnl = detect_alerts(
    cfg, quotes, state, quotes_complete=quotes_complete, fx=fx
)
print()
print(f"美股组合当日盈亏: ${group_pnl.get('us', 0.0):+.2f}")
print(f"港股科技当日盈亏: ¥{group_pnl.get('hk', 0.0):+.2f}")
print(f"黄金当日盈亏: ¥{group_pnl.get('metal', 0.0):+.2f}")
if not quotes_complete:
    print("行情不完整，已跳过组合盈亏提醒。")
print(f"触发异动: {len(alerts)} 条")
for alert in alerts:
    print(
        " -",
        group_label(alert.get("group", "us")),
        "|",
        alert["type"],
        "|",
        alert["msg"],
    )
