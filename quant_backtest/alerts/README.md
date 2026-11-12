# 交易日 14:30 MACD QQ 邮件提醒

此任务独立于 Streamlit，网页休眠或无人访问时仍可通过 GitHub Actions 检查行情。

默认设置：

- 发件人：`1793951795@qq.com`
- 收件人：`1556444893@qq.com`
- 标的：半导体行业指数 `881121`
- 策略：MACD 金叉买入、死叉卖出，参数固定为 **12 / 26 / 9**。
- 信号口径与网页内置 MACD 策略相同：依据历史回测模拟仓位，空仓时金叉提示买入、持仓时死叉提示卖出。
  它不会读取你的真实账户仓位。

## 配置并启用

1. 将项目上传到 GitHub。工作流文件 `.github/workflows/macd-email-alert.yml` 必须位于
   **GitHub 仓库根目录**的 `.github/workflows/` 下，并合并到默认分支。
   如果只上传 `quant_backtest` 里面的内容，还需要把外层 `.github` 文件夹复制到新仓库根目录。
   工作流会自动识别两种目录布局。
2. 登录发件 QQ 邮箱网页版，在“设置 → 账户”中开启 **SMTP 服务**并生成授权码。
3. 打开 GitHub 仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**：

   | Secret 名称 | 内容 |
   |---|---|
   | `QQ_SMTP_AUTH_CODE` | 发件 QQ 邮箱生成的 SMTP 授权码，**不是 QQ 登录密码** |

   授权码只填写到 Secret，不写入 JSON、代码、日志或工作流正文。
4. 打开仓库 **Actions**，如有启用提示则启用工作流。选择 **MACD 14-30 QQ email alert**，
   点击 **Run workflow**，将 `mode` 选为 **test_email**，手动运行一次，检查接收邮箱是否收到测试信。
   测试邮件可在任意时间发送，不代表买卖信号。
5. 配置成功后，默认分支上的计划任务会在周一至周五北京时间 14:30 触发。
   股票交易日历会进一步排除法定休市日。检查日志和 Summary 可查看 `sent`、`no_signal`、
   `already_sent`、`skipped` 或 `error` 等结果。

## 自己设置监控标的

在仓库 **Settings → Secrets and variables → Actions → Variables** 添加或修改：

| Variable 名称 | 示例 / 作用 |
|---|---|
| `MACD_ALERT_CODES` | `881121`，或多个代码 `881121,884229,600000` |
| `QQ_SMTP_USER` | 可选，覆盖默认发件邮箱；对应授权码也要更换 |
| `QQ_MAIL_TO` | 可选，覆盖默认收件邮箱 |

`MACD_ALERT_CODES` 留空时读取同目录的 `config.json`。
`88xxxx` 自动识别为同花顺行业/板块指数，`0`、`3`、`6` 开头的六位代码按沪深 A 股处理。
当前不支持 ETF、北交所或普通大盘指数；如 `000001` 默认指深圳股票，不是上证指数。
股票历史使用前复权日线，分钟行情使用当天实际价格。每次刷新股票历史全量重算前复权，
以避免除权后拼接不一致。

也可以直接编辑 `config.json` 中的 `symbols` 列表，配置代码、显示名称和类型：

```json
"symbols": [
  {"code": "881121", "name": "半导体行业指数", "type": "ths_board"},
  {"code": "600000", "name": "浦发银行", "type": "stock"}
]
```

手动 Run workflow 时的 `codes` 参数只覆盖本次执行。
网页中的会话标的选择与此配置相互独立，不会自动修改计划任务。
如需暂停邮件，将 `config.json` 的 `enabled` 改为 `false`，或在 GitHub Actions 中停用工作流。

## 时间、数据与重复发送

- Cron 为 UTC `30 6 * * 1-5`，对应北京时间交易日下午 **14:30**。
  GitHub Actions 可能排队延迟，**不保证邮件在 14:30 准时送达**。
- 延迟启动仍读取当天 **14:30 的分钟行情**，不会使用启动时的价格，也不会用当日收盘价代替。
  当天已有的日线、14:30 以后的分钟价格都不会参与当次信号计算。
- 仅在北京时间 **14:30（含）至 15:00（不含）**之间发送正式信号邮件；
  获取数据后再次检查窗口，超过窗口跳过。不会补发前一天的过期信号。
- 周末、非交易日不发送。交易日历未覆盖当天、缺少上一交易日日线、14:30 分钟数据缺失
  或接口出错时，任务失败并在日志中标明，**不会用旧缓存冒充当天信号**。
- 无买卖信号不发送；默认使用 SSL 连接 `smtp.qq.com:465`。
  邮件包含标的、买卖方向、行情时间、价格、DIF、DEA 和信号原因。
- 成功发送后将“日期、代码、方向、收件人”写入记录，通过 GitHub Actions Artifact 跨运行保存；
  同日重复执行会跳过已记录的邮件。工作流串行运行，记录保留 30 天。
  多标的中途失败也会保存此前成功发送的记录。
  不要在当天手动删除记录 Artifact。若发送完成后任务被强制终止或记录上传失败，
  邮件与远程记录无法组成原子事务，重跑仍可能重复发送。
- 手动 `test_email` 不受交易日和时间窗口限制，也不参与信号邮件去重。
  手动 `dry_run` 不发送、不更改记录，可在交易日 14:30 后检查当天信号。
- GitHub 的计划任务仅在默认分支运行。Fork 通常需要手动启用 Actions；
  公共仓库长期没有活动时，计划任务可能被平台自动停用，应留意 Actions 状态。

## 本地验证

仅检查，不发邮件：

```powershell
python -m pip install -r quant_backtest/requirements-alerts.txt
python quant_backtest/macd_email_alert.py --mode dry_run
```

默认模式也是 `dry_run`。14:30 前会跳过，正式发送必须显式指定 `--mode check`，
并在进程环境中设置 `QQ_SMTP_AUTH_CODE`。本地 `.alert-state/` 目录已加入 Git 忽略规则。
自动化测试通过模拟 SMTP 验证邮件逻辑，不会向真实邮箱发信。
