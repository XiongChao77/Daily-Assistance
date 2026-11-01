# 每日 HTML 资讯与计划提醒

使用 Conda `base` 环境的 Python，每天北京时间 07:30 生成 HTML 简报，并显示 Windows 通知和持续到手动关闭的桌面计划提醒。登录时会检查当日是否漏跑；仅补当天。

## 目录

- `config/`：报告栏目配置、模型连接设置和 API 密钥。`api_config.json` 含密钥，不要公开。
- `resources/prompts/`、`resources/templates/`：AI 提示词和 HTML 页面模板。
- `data/plans/`：计划事项 Markdown。支持 `- [ ] 事项`、`- [x] 已完成事项` 和 `截止: 2026-11-05`。程序会自动写入隐藏稳定 ID；改标题后仍保留状态，请保留行尾 `<!-- plan-id: ... -->`。
- `data/state/`：通知与报告状态、计划进度、服务令牌和进程锁。
- `data/reports/YYYY-MM-DD/`：当天 `Daily.html`、`report.json` 和可续跑的 `report_draft.json`。桌面 `Daily.lnk` 指向最新日报。
- `logs/`：每日任务日志按日期保存在 `daily/`；计划服务日志保存在此目录根部。
- `docs/requirements/`：资讯需求说明。
- `archive/`：历史 QA 截图、浏览器配置和 Python 缓存归档。
- 根目录中的 `daily.py`、`reporting.py`、`plan_store.py`、`plan_service.py`、`reminder_popup.py` 是程序入口和逻辑；`install_task.ps1` 注册计划任务，`requirements.txt` 列出 Python 依赖。

## 安装

在 PowerShell 中，从本目录运行：

```powershell
& 'C:\Users\xc176\anaconda3\python.exe' -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_task.ps1
```

每日任务和计划服务使用当前 Windows 用户的交互式会话。电脑在 07:30 关机或未登录时，会在下次登录后检查并补跑。Windows 的通知开关和“请勿打扰”可能隐藏通知中心消息；桌面计划窗口会持续显示，直到手动关闭。

报告中的每个计划事项有“完成”和“更新进度”按钮；提醒弹窗中的每条计划旁也有“完成”按钮。完成后，该事项仍保留在报告中显示为已完成，但后续不再出现在提醒弹窗；当前打开的弹窗会立即移除该条目，来自 HTML 的更新会在约 3 秒内同步。进度更新可填写 0–100% 和备注。HTML 按钮需要本机计划服务运行，服务停止时页面会显示提示。

每个 News 子栏目和 Crypto News 均保存 10 条，默认展示前 4 条；点击 More 可查看另外 6 条。

## 手动运行

```powershell
& 'C:\Users\xc176\anaconda3\python.exe' .\daily.py --force
& 'C:\Users\xc176\anaconda3\python.exe' .\daily.py --refresh
& 'C:\Users\xc176\anaconda3\python.exe' .\daily.py --render-only
& 'C:\Users\xc176\anaconda3\python.exe' .\daily.py --notify-test
```

模型按栏目使用 Responses API 的 `web_search` 工具检索报道，返回结构化 JSON；程序校验条数并用模板生成 HTML。行情由 Yahoo Finance 获取，包含价格、涨跌额、涨跌幅和近五日小走势图；获取失败的标的显示“报价暂不可用”。

`--refresh` 重新搜索并生成当天内容。只修改 HTML 模板时，用 `--render-only` 根据 `data/reports` 下当天的 `report.json` 重新排版，不调用模型或重新抓取行情。修改 `config/report_config.json` 的栏目或数量后需用 `--refresh` 更新数据。
