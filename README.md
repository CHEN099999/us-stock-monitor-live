# 美股持仓异动监控

定时获取持仓行情，按阈值检测异动，并通过邮件、Server酱或企业微信推送。

> 安全提醒：任何曾公开过的邮箱授权码都必须立即作废并重新生成，不能继续复用。

## 检测规则

以下阈值均可在 `config.yaml` 中修改：

- 单票日内涨跌幅 `>= 2%`
- 单票较成本价涨跌幅 `>= 10%`
- 美股、港股科技、黄金分别计算当日盈亏，各自绝对值达到阈值时单独推送
- 美股成交量 `>= 20 日均量 2 倍`
- 两个相邻运行周期之间跳价 `>= 2%`

美股、港股科技、黄金分组推送，不会混在同一封邮件中。同一分组内的同一提醒在同一个监控日内只发送一次。推送失败的提醒会进入待发送队列，后续运行继续重试；只有至少一个渠道发送成功后才会写入已发送状态。

## 组合盈亏的币种口径

三个分组各自用自己最直观的币种显示：

| 分组 | 显示币种 | 计算方式 |
|---|---|---|
| 美股 | 美元 `$` | `Σ qty × (现价 − 昨收)` |
| 港股科技 | 人民币 `¥` | 持仓金额 × 当日涨跌幅 |
| 黄金 | 人民币 `¥` | `Σ qty × (现价 − 昨收)`（美元）× 美元/人民币汇率 |

恒生科技是**指数**，本身没有货币价格，所以按"持仓金额 × 当日涨跌幅"折算：

- 持仓金额默认取 `qty × cost_price`，也可以在持仓里用 `position_value` 直接指定（单位：元）。
- 如果持有的是有港币报价的标的（港股股票/ETF），给该持仓加 `pnl_mode: price`，就改回"按价格变动 × 港币汇率"计算。

汇率默认自动从新浪获取（美元/人民币、港币/人民币），也可以在 `config.yaml` 里写死：

```yaml
display:
  usd_cny: 6.705
  hkd_cny: 0.8544
```

> 注意：`alerts.portfolio_daily_pnl` 是拿**显示币种**的数值去比阈值的。也就是说港股科技和黄金的阈值单位是人民币，美股是美元。

## 数据源

- 实时行情：新浪美股、港股和贵金属接口，美股行情通常有约 15 分钟延迟。
- 汇率：新浪美元/人民币、港币/人民币接口。
- 均量：优先使用滚动积累的数据；首次运行时可从东方财富获取历史 K 线。
- 行情接口和 GitHub Actions 定时器均为尽力而为，不能保证严格每 5 分钟执行。

## 本地运行

1. 安装 Python 3.11 或更高版本。
2. 安装依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 将 `config.example.yaml` 复制为 `config.yaml`，填写持仓和阈值。`config.yaml` 已被 Git 忽略。
4. 将 `.env.example` 复制为 `.env`，填写邮箱配置：

   ```text
   EMAIL_USERNAME=your_name@qq.com
   EMAIL_PASSWORD=your_smtp_authorization_code
   EMAIL_TO=your_name@qq.com
   ```

5. 测试推送：

   ```powershell
   python test_push.py
   ```

6. 运行一次完整检测：

   ```powershell
   python monitor.py
   ```

7. 只查看行情和当前触发结果，不修改状态文件：

   ```powershell
   python debug_fetch.py
   ```

8. 执行单元测试：

   ```powershell
   python -m unittest discover -s tests -v
   ```

## 推送配置

邮件在 `config.yaml` 中保持 `email.enabled: true`。用户名、授权码和收件人建议通过环境变量提供：

- `EMAIL_USERNAME`
- `EMAIL_PASSWORD`
- `EMAIL_TO`

也可以只使用以下任一渠道：

```yaml
push:
  email:
    enabled: false
  serverchan_sendkey: "SCT..."
  wecom_webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
```

## Windows 定时任务

右键 `install_task.bat`，选择“以管理员身份运行”。脚本会创建每 5 分钟运行一次的 `USStockMonitor` 任务。

删除任务：

```powershell
schtasks /delete /tn "USStockMonitor" /f
```

## GitHub Actions

推荐使用 **不含敏感历史的 Public 仓库**。公开仓库的标准 GitHub Actions 运行器免费，同时通过 Secrets 隐藏持仓和邮箱信息。

在 GitHub 仓库中添加以下 Actions Secrets：

| Secret | 内容 |
|---|---|
| `MONITOR_CONFIG` | 完整的 `config.yaml` 内容，使用多行 YAML |
| `STATE_KEY` | 用于加密 `alerted_state.json`，使用下面命令生成 |
| `EMAIL_USERNAME` | 发件邮箱 |
| `EMAIL_PASSWORD` | 新生成的邮箱 SMTP 授权码 |
| `EMAIL_TO` | 收件邮箱，多个地址用逗号分隔 |

生成 `STATE_KEY`：

```powershell
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

工作流会在运行时把 `MONITOR_CONFIG` 写进 `config.yaml`，并加密状态文件。`config.yaml`、`.env` 和邮箱授权码不会提交到仓库。

`concurrency` 已配置为串行执行，避免多个任务同时提交 `alerted_state.json` 造成冲突。GitHub 的 `*/5` 定时任务不保证精确间隔，负载高时可能延后或跳过。

## 运行时段

`config.yaml` 默认设置 `monitor_24h: true`，会全天执行。若要只在美国常规交易时段运行，可改为：

```yaml
runtime:
  monitor_24h: false
```

脚本会先把北京时间转换为美国东部时间，再判断是否为美股工作日，因此能正确处理北京时间凌晨仍在运行的美国周五交易日。

## 文件说明

| 文件 | 作用 |
|---|---|
| `config.example.yaml` | 可公开的配置模板 |
| `config.yaml` | 本地真实配置，已被 Git 忽略 |
| `monitor.py` | 主监控程序 |
| `test_push.py` | 推送渠道测试 |
| `debug_fetch.py` | 只读行情和异动调试 |
| `requirements.txt` | Python 依赖 |
| `alerted_state.json` | 加密后的去重状态、待发送队列和均量历史 |
| `.github/workflows/monitor.yml` | GitHub Actions 定时任务 |

## 安全注意事项

- 不要提交 `.env`、邮箱授权码或企业微信 Webhook。
- 公开仓库不得提交真实持仓、邮箱地址、授权码或未加密状态文件。
- 曾公开过密钥的旧仓库应改为 Private 或归档，并立即作废旧密钥。
- 新浪、东方财富接口均为非官方免费接口，可能变更、限流或暂停服务。
- 本工具不是交易系统，不应作为下单或风控的唯一依据。
