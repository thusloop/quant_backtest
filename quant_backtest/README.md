# 量化回测与策略监控

支持 Streamlit Community Cloud 免费部署，也可以使用原有 Flask 网页在本地运行。当前支持：

- 半导体行业指数 `881121`
- `884229` 板块指数
- MACD+ADX 动态仓位策略
- MACD 金叉买入、死叉卖出策略
- 一直持有基准

网页支持策略多选、交易记录筛选、快速区间、区间收益、最大回撤、权益曲线缩放和平移。
网络不可用时可使用已有行情缓存。北京时间 15:00 前，881121
可以尝试使用实时行情生成临时日线，用于当天收盘操作建议。

## Streamlit 启动

推荐 Python **3.12**。在整个仓库根目录执行：

```powershell
python -m pip install -r quant_backtest/requirements.txt
python -m streamlit run quant_backtest/streamlit_app.py
```

浏览器打开终端提示的地址，通常是 <http://localhost:8501>。
若当前目录已经是 `quant_backtest`，使用 `python -m streamlit run streamlit_app.py`。

云端版提供标的切换、策略多选、参数编辑、交互曲线、快速/自定义日期区间、收益/回撤统计、
交易记录筛选，以及行情、权益和交易 CSV 下载。

- 首次打开先读取仓库自带的日线 CSV，不会自动等待历史行情下载；点击侧栏“同步行情”更新。
- 盘中实时行情默认关闭，可在侧栏开启；仅 881121 在北京时间 15:00 前尝试获取。
- 时间判断明确使用 `Asia/Shanghai`，不依赖云服务器时区。
- 自定义标的与策略属于当前浏览器会话，互不覆盖。修改后使用“配置备份”下载 JSON，
  下次访问时导入恢复。配置文件不包含行情 CSV；导入新标的后需要同步行情。
- 行情 CSV 是实例内共享的公共行情缓存。仓库内文件是启动种子，云端同步结果不回写 GitHub。
  休眠、重启或重新部署后，运行中产生的文件可能丢失；重要行情可通过页面下载，
  再人工更新仓库中的 CSV。不要依赖云实例永久保存文件。

## 部署到 Streamlit Community Cloud

1. 将整个仓库上传到自己的 GitHub 仓库。至少保留 `quant_backtest` 下的 Python 文件、
   `webapp/backend`、`webapp/strategies/*.json`、`a_share/data/*.csv`、
   `a_share/data/custom_assets.json` 和 `requirements.txt`。不要上传 `.venv`、缓存或 secrets。
2. 登录 <https://share.streamlit.io/>，选择 **Create app / Deploy an app**，连接该 GitHub 仓库。
3. 选择实际上传的分支，将 **Main file path** 设置为：
   `quant_backtest/streamlit_app.py`。
4. 在 **Advanced settings** 选择 **Python 3.12**，然后部署。
   平台会发现入口文件同目录的 `quant_backtest/requirements.txt` 并安装依赖。
5. 若只把 `quant_backtest` 文件夹的内容作为 GitHub 仓库根目录上传，入口应填写
   `streamlit_app.py`，此时依赖文件就在仓库根目录。

仅网页部署无需额外 Flask 服务、数据库、API 密钥或自定义启动命令。
`.streamlit/config.toml` 位于整个仓库根目录，用于无界面启动和页面主题；
只上传子目录时可一并复制该配置，缺失它也能运行。

免费实例可能休眠，也有资源限制。行情接口在海外云环境可能超时或被拒绝，
同步失败时页面仍使用已有缓存，并显示同步状态和最新数据日期。
可以先在本地更新 CSV 再提交到 GitHub。该部署不提供无人访问时持续运行或定时交易。

## 交易日 14:30 邮件提醒

已提供独立的 GitHub Actions 任务，默认监控 `881121`，使用 MACD **12 / 26 / 9**。
当天 14:30 有买入/卖出信号时，从 `1793951795@qq.com` 发送到 `1556444893@qq.com`。
可通过仓库变量 `MACD_ALERT_CODES` 自行设置监控代码。

在 GitHub 添加 `QQ_SMTP_AUTH_CODE` Secret（发件邮箱的 SMTP 授权码）后，
手动运行一次 `test_email` 检查配置。工作流位于整个仓库根目录的
`.github/workflows/macd-email-alert.yml`，只上传子目录时需要同时复制该工作流。

GitHub Actions 可能延迟启动；程序仍使用当天 14:30 分钟价格，超过北京时间 15:00 则跳过发送。
缺少当日分钟行情或上一交易日日线时不会发送旧信号。
详细配置、支持的代码类型、去重和测试说明见 [邮件提醒说明](alerts/README.md)。

官方部署说明：<https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app>

## 原有 Flask 启动

```powershell
cd E:\001_some_AI_code\量化\quant_backtest
python -m webapp.backend.app --host 127.0.0.1 --port 8765
```

浏览器打开：<http://127.0.0.1:8765/>

## 目录

```text
quant_backtest/
  streamlit_app.py  Streamlit Community Cloud 入口
  market_time.py    北京时间
  history_cache.py  行情缓存原子写入
  macd_email_alert.py  独立邮件任务
  requirements-alerts.txt  邮件任务依赖
  alerts/          邮件配置与 GitHub Actions 使用说明
  webapp/
    backend/       Flask API、数据同步、回测服务、配置
    frontend/      页面、样式、交互脚本
    strategies/    本地策略 JSON（内置模板和自定义策略）
  a_share/data/    881121 与 884229 本地缓存；新增代码后生成 custom_assets.json
  macd_adx_backtest.py
  macd_adx_semiconductor_web.py
  requirements.txt
```

## API

```text
GET  /api/assets
POST /api/assets              添加股票/指数代码并同步历史数据
GET  /api/strategies          查看策略模板和本地自定义策略
POST /api/strategies          保存一个策略副本
DELETE /api/strategies/<id>   删除自定义策略
GET  /api/dashboard?code=881121
GET  /api/dashboard?code=884229
POST /api/sync?code=881121
POST /api/sync?code=884229
```

884229 以及新增的 `88xxxx` 行业/板块代码使用同花顺接口；普通股票代码使用 AkShare
股票日线接口。88xxxx 板块本地已有缓存时刷新只请求当年数据，并与历史缓存合并。881121 的实时
行情优先使用东方财富，失败时自动切换到同花顺，并在网页状态中显示实际来源。

## 验证

在仓库根目录执行：

```powershell
python -m unittest discover -s quant_backtest/tests -v
```

测试使用仓库 CSV 和模拟行情接口，不请求真实行情服务，也不更改现有配置。
