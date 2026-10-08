# 闲鱼卡券 AI 客服 V3.5 / V3.6 维护交接

更新日期：2026-10-08

这份文件用于后续在 Codex 中继续修改、测试、打包、安装和发布。先看“当前状态”和“禁止事项”，再动代码。

## 1. 当前状态

| 项目 | V3.5 本地版 | V3.6 云端内测版 |
| --- | --- | --- |
| 本机目录 | `C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent` | `C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent-cloud-preview-v3.6` |
| Git 分支 | `local-gui-v1` | `cloud-preview-v3.6` |
| 最新标记版本 | `0.9.74` | `0.11.50` |
| 应用身份 | `cn.xianyu.cardai.v2` / 闲鱼卡券AI客服 | `cn.yituan123.xianyucardai.cloud` / 闲鱼卡券AI客服云端内测版 |
| 用户数据目录 | `%APPDATA%\xianyu-card-ai-v3` | `%APPDATA%\xianyu-card-ai-cloud-preview` |
| 主数据库 | `data\xianyu-v2.db` | `data\xianyu-v2.db`，但位于独立的 V3.6 用户目录 |
| 安装包输出 | `desktop_v2\release-v3.5` | `desktop_v2\release-cloud-prod` |
| 更新方式 | 手工覆盖安装 | `electron-updater` + R2 通道 |
| 云授权 | 无 | `https://api.yituan123.com`，48 小时离线宽限 |

V3.5 当前源码在 `0.9.74` 之后又加入了“完整地址不得由模型臆断为可用门店”的修复，提交为 `d761d79`。这项修改已经通过完整 Python 回归，但尚未升版本、打安装包或安装，因此已安装的 `0.9.74` 不包含它。

V3.6 的已发布 Git 状态是 `36d04b9`（版本戳）和 `9b99c42`（功能提交）。本机 V3.6 工作树另有 8 个未提交文件，不能当成 `0.11.50` 已发布内容：

- `XianyuAgent.py`
- `app_store.py`
- `desktop_app.py`
- `main.py`
- `tests/test_app_store.py`
- `tests/test_main_workflow.py`
- `tests/test_v2_store.py`
- `v2_store.py`

这些未提交修改涉及时间问法、门店问法、澄清话术和上下文逻辑。接手后应先单独审查、测试，再决定提交、拆分或丢弃；不要在未确认归属前覆盖。

## 2. 禁止事项

1. V3.5 与 V3.6 没有共同 Git merge base，禁止直接 `merge`。需要同步修复时，逐文件人工移植并重新跑各自测试。
2. 不要用 V3.5 安装包覆盖 V3.6 数据目录，也不要互相复制 SQLite 数据库。
3. 不要提交 `.env`、API Key、Cookie、管理员密码、授权数据库或用户数据库。
4. 不要直接改 `build_info.py` 一个地方就发布；版本号、安装包元数据和发布文件必须一致。
5. V3.6 同一版本的安装包禁止覆盖上传。内容变化必须先升版本。
6. V3.6 的 `latest.yml` 必须最后上传；先上传版本化安装包和 `.blockmap`。
7. 线上只开放 22、80、443；禁止把 8787 或数据库端口暴露到公网。

## 3. Git 与仓库关系

两个工作树使用同一远端仓库：

```text
origin   https://github.com/zgjun1992-collab/XianyuAutoAgent.git
upstream https://github.com/shaxiu/XianyuAutoAgent.git
```

日常维护：

```powershell
# V3.5
Set-Location 'C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent'
git status --short
git switch local-gui-v1

# V3.6
Set-Location 'C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent-cloud-preview-v3.6'
git -c safe.directory='C:/Users/ASUS/Documents/Codex/2026-09-01/new-chat/XianyuAutoAgent-cloud-preview-v3.6' status --short
```

V3.6 工作树在当前 Windows 环境可能触发 Git “dubious ownership”。只给单条命令加 `-c safe.directory=...`，不要为了省事写全局 `safe.directory=*`。

## 4. 代码架构

```text
闲鱼消息/订单事件
  -> XianyuAgent.py / XianyuApis.py（连接、收发、令牌和事件）
  -> main.py（消息编排、上下文、风险与回复流程）
  -> v2_store.py（确定性 SKU、门店、价格、数量、规则匹配）
  -> 大模型语义解析/兜底（只能在资料约束内生成）
  -> 审核、静默人工或自动发送

Electron/Vue 桌面端
  -> desktop_v2/electron/main.cjs
  -> 启动 v2_backend.py
  -> app_store.py / v2_store.py -> SQLite
```

关键文件：

- `main.py`：实时工作流、会话上下文、确定性回复优先级、模型 grounding、人工处理策略。
- `v2_store.py`：商品知识、SKU、门店表、价格/张数/人数/金额算法、退款和策略数据。
- `app_store.py`：基础数据库、配置与审计数据访问。
- `v2_backend.py`：桌面端本地 HTTP 后端。
- `desktop_v2/src`：Vue 页面。
- `desktop_v2/electron/main.cjs`：Electron 生命周期、本地后端、数据目录、托盘、V3.6 授权和更新。
- `prompts`：模型提示词；修改后必须同时做确定性测试和模型兜底测试。
- `tests`：Python 回归测试。

## 5. 问答决策原则

当前实现的正确优先级：

1. 风险、退款、人工和静默规则。
2. 当前商品绑定的人工知识与结构化数据。
3. 确定性 SKU / 门店 / 价格 / 数量 / 人数 / 金额解析。
4. 语义分类模型，只负责理解问法，不得发明业务事实。
5. 普通模型兜底；回复仍要经过数字和门店 grounding 检查。

业务事实来源优先级：实时商品选项/SKU > 人工保存知识 > 页面描述。门店查询只能使用当前商品绑定的门店表。模型不能因为地址相似、路名相似或数字相似就回复“可以用”。

已覆盖的典型问法包括：

- 价格近似：`169` 可匹配 `169.9`，但回复必须带真实规格确认。
- 库存：`169还有吗`、`直接拍72的是吗`、`有四人餐吗`、`今日价格`。
- 数量：`两张`、`2个`、`几张多少钱`、`消费400买四张对吗`。
- 人数/套餐：`单人`、`两个人`、`四人餐`、`套餐`。
- 门店：城市、区县、商圈、商场、门店名、完整地址以及短上下文追问。
- 售后：领取、券码、核销、有效期、退款、转人工。

短消息必须结合最近的商品、SKU、门店和问题类型上下文，例如先问门店后再发“万象汇店”，先问套餐后再发“2个”。不能把短消息脱离当前会话单独分类。

## 6. 2026-10-08 地址误判修复

问题：买家发送 `解放中路276号奉发·古华庭16栋1楼101-102号`，真实门店表没有该地址，但旧流程没有把它识别成门店查询，消息落入模型后被错误回复“该门店在适用范围内”。

修复：

- `v2_store.py` 将带路名、门牌号、栋/楼/室等特征的完整地址强制送入门店表验证。
- 未匹配地址返回不可用/请补充城市和店名，不再落入模型自由回答。
- `main.py` 增加第二道 grounding：没有门店表证据时，拒绝模型生成的“可以用/适用范围内”等正向结论。
- 增加确定性门店和模型 grounding 回归测试。

验证：V3.5 完整测试 `551 tests` 全部通过。

后续：V3.6 也需要移植同一修复，但必须先处理其 8 个未提交文件，因为 `main.py`、`v2_store.py` 和测试文件存在重叠修改。

## 7. V3.5 本地版

### 7.1 能力

- 本地商品知识、首次回复、SKU/价格/张数/人数/金额计算。
- Excel/CSV/TXT 门店导入、当前商品绑定、城市和具体门店搜索。
- 套餐图片与触发词。
- 发货、退款和客服约束；退款申请按 72 小时处理。
- 本地测试中心、审核和审计。
- 卖家登录状态同步；敏感配置本地加密。
- 人工优先解决：直接说“人工”时先追问具体问题；普通问题不应无条件转人工。退款流程仍保留 72 小时处理承诺。

### 7.2 本地数据

默认用户数据目录：

```text
%APPDATA%\xianyu-card-ai-v3
  desktop-settings.json
  data\xianyu-v2.db
  data\product-images\...
```

覆盖安装不会主动清空该目录。备份时先退出程序，再复制整个目录。恢复时也先退出程序，并保留恢复前副本。

### 7.3 开发、测试、打包

```powershell
Set-Location 'C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent'

# 测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 开发启动
powershell -ExecutionPolicy Bypass -File .\start-v2-dev.ps1

# 构建安装包
powershell -ExecutionPolicy Bypass -File .\build-v3.ps1
```

安装包命名：`XianyuCardAI-V3.5-Setup-<version>.exe`。

发布前至少检查：

```powershell
git status --short
git diff --check
Get-FileHash .\desktop_v2\release-v3.5\XianyuCardAI-V3.5-Setup-*.exe -Algorithm SHA256
```

版本发布应依次完成：功能提交 -> 更新 `desktop_v2/package.json` 与 `build_info.py` -> 版本戳提交 -> 全测 -> 打包 -> 校验 ProductVersion 和 SHA-256 -> 关闭旧进程 -> 覆盖安装 -> 打开并做冒烟测试 -> 推送 Git。

## 8. V3.6 云端内测版

### 8.1 与 V3.5 的差异

- 独立 appId、用户数据目录、构建输出和安装名称，可与 V3.5 并存。
- 增加客户账号、套餐、设备绑定、冻结/解绑和在线授权。
- 管理员密码使用 Argon2id；后台会话使用 HttpOnly/SameSite Cookie、CSRF、登录限流、持久锁定和审计。
- 授权失败或到期只停止自动客服，不删除本地商品、知识、门店和历史。
- 增加托盘、单实例、自动更新、正式图标和内置 PDF 文档。
- `0.11.50` 重点修复了 SKU/门店连续上下文、门店实体提取、套餐追问和适用范围排除。

### 8.2 客户端开发与打包

V3.6 目录当前没有自己的 `.venv`。正式维护应在该目录创建独立虚拟环境，不要长期复用 V3.5 环境。

```powershell
Set-Location 'C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent-cloud-preview-v3.6'

py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r cloud_license\requirements.txt

.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell -ExecutionPolicy Bypass -File .\start-v2-dev.ps1
powershell -ExecutionPolicy Bypass -File .\build-v3.ps1
```

安装包命名：`XianyuCardAI-V3.6-CloudPreview-<version>.exe`。构建还会生成对应 `.blockmap` 和 `latest.yml`。

本次交接只读验证使用了 V3.5 环境：共发现 548 项，546 项执行通过；`test_cloud_license` 和 `test_cloud_license_api` 因该环境缺少 `argon2`、`fastapi` 在导入阶段中止。这不是已确认的代码失败，也不能写成 V3.6 全绿。创建 V3.6 独立环境并安装两份 requirements 后必须重跑。

### 8.3 授权服务

本机联调：

```powershell
.\.venv\Scripts\python.exe -m cloud_license.admin_cli --db .\work\cloud-license-test.db create-admin admin
.\start-license-test.ps1
```

生产结构：Caddy 终止 HTTPS，反向代理到仅监听 `127.0.0.1:8787` 的 FastAPI/Uvicorn。SQLite 适合当前小规模内测；已经有 SQLite -> PostgreSQL 一次性迁移工具，但 PostgreSQL Store 尚未实现，不能直接切换生产数据库。

迁移工具复制客户、套餐、订阅、设备、管理员和审计，不复制客户/管理员会话；迁移后全部用户需要重新登录。迁移前必须停服务、做 SQLite 备份、在空 PostgreSQL 数据库演练并跑 API 回归。

### 8.4 自动更新发布

```powershell
.\deploy\deploy-r2-release.ps1 -DryRun
.\deploy\deploy-r2-release.ps1
```

更新源：`https://download.yituan123.com/v3.6`。

上传顺序：版本化安装包 -> `.blockmap` -> `latest.yml`。前两项长期缓存，`latest.yml` 禁用缓存。上传后从旧客户端验证发现更新、下载、安装和重启。

### 8.5 运维与备份

生产建议目录：

```text
/opt/xianyu-license/app
/opt/xianyu-license/venv
/var/lib/xianyu-license
/var/backups/xianyu-license
/etc/xianyu-license.env
```

必须验证：

- `curl --fail http://127.0.0.1:8787/health`
- `curl --fail https://api.yituan123.com/health`
- systemd 服务和每日备份 timer 正常。
- Caddy 证书链有效、HTTP 自动跳 HTTPS、安全响应头存在。
- 公网无法连接 8787 和数据库端口。
- 恢复脚本可校验备份并完成一次演练恢复。

## 9. 文档状态

V3.6 安装包内置：

- `desktop_v2\docs\闲鱼卡券AI客服-V3.6-客户使用说明书-0.11.11.pdf`
- `desktop_v2\docs\阿里云百炼APIKey获取图文教程.pdf`

客户手册仍写 `0.11.11`，而当前发布版已经是 `0.11.50`。下一次正式发布前必须更新客户手册，至少补充 0.11.12-0.11.50 的登录/托盘/更新、SKU 连续上下文、门店实体匹配、退款与人工处理规则，并同步安装包内的 PDF 文件名或文档版本页。

百炼配置使用北京地域控制台创建 API Key，模型接口为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，默认模型建议 `qwen-plus`。API Key 只填入客户端加密配置，不写进仓库、截图、日志或交接文档。

## 10. 已知风险与待办

按优先级处理：

1. 审查 V3.6 的 8 个未提交文件，拆分成可解释的提交并跑独立环境全测。
2. 将 `d761d79` 的完整地址门店验证和模型正向门店结论拦截人工移植到 V3.6。
3. 升 V3.5 版本并重新打包后，才让已安装版包含地址修复。
4. 更新 V3.6 客户手册到当前版本。
5. 为 V3.6 PostgreSQL Store 编写实现和与 SQLite 相同的回归测试后，才讨论切库。
6. 增加包含真实对话链的回归：城市 -> 门店名 -> SKU -> 张数/人数/价格 -> 使用时间。
7. 继续限制兜底：可由 SKU、门店、规则或上下文回答的问题不得转人工，也不得使用泛化兜底。
8. 每次发布保留版本号、功能提交、版本戳提交、测试结果、安装包路径、SHA-256 和回滚版本。

## 11. 新维护会话的首条提示词

在 Codex 打开对应目录后，可以直接发送：

```text
先完整阅读 HANDOFF_V3_5_V3_6.md，再检查当前分支、git status、版本号和测试状态。保留现有未提交改动，不要直接合并 V3.5/V3.6。完成修改后跑对应完整测试，再提交并推送当前分支；只有我明确要求时才升版本、打包、安装或发布。
```

如果维护 V3.6，再追加：

```text
V3.6 工作树曾有 8 个未提交文件。先核对这些改动是否仍存在和归属，再决定如何拆分；不要覆盖或混入无关修复。
```
