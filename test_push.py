# -*- coding: utf-8 -*-
"""测试已启用的推送渠道，运行后会产生一条真实测试消息。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor import (  # noqa: E402
    load_config,
    push_email,
    push_serverchan,
    push_wecom,
)


cfg = load_config()
push = cfg.get("push", {})

title = "【测试】美股异动监控推送通道正常"
content = (
    "如果你收到这条消息，说明推送配置成功。\n\n"
    "监控会根据 config.yaml 的规则检测持仓异动。"
)

ok = False

email_cfg = push.get("email", {})
if email_cfg.get("enabled"):
    print("发送邮件测试消息...")
    ok = push_email(email_cfg, title, content) or ok

if push.get("serverchan_sendkey"):
    print("发送 Server酱测试消息...")
    ok = push_serverchan(push["serverchan_sendkey"], title, content) or ok

if push.get("wecom_webhook"):
    print("发送企业微信测试消息...")
    ok = push_wecom(push["wecom_webhook"], title, content) or ok

print()
if ok:
    print("至少一个推送渠道发送成功。")
else:
    print("所有推送渠道均失败，请检查环境变量和 config.yaml。")
    print("QQ 邮箱授权码获取：QQ邮箱 -> 设置 -> 账户 -> POP3/SMTP 服务")
