# V3.6 云端内测版

本工程从 V3.5 基线独立恢复，源码、测试数据库、构建输出和版本号均与 V3.5 隔离，不会覆盖原版程序或真实用户数据。

## 当前能力

- FastAPI + Uvicorn 授权服务，只监听服务器本机地址；
- 客户账号登录、套餐校验、设备绑定、冻结和解绑；
- 正式管理员账号，Argon2id 密码哈希；
- HttpOnly/SameSite 管理会话、CSRF 校验、登录限流和持久锁定；
- 管理操作审计、过期会话清理；
- SQLite 小规模内测运行，以及 SQLite 到 PostgreSQL 的一次性迁移工具；
- systemd、Caddy、每日备份、校验和恢复脚本；
- 客户端正式服务地址固定为 `https://api.yituan123.com`；
- 在线授权检查和 48 小时断网宽限；
- 到期或未授权仅停止自动客服，不删除本地商品、知识、门店或历史数据。

## 本机联调

安装开发依赖后创建管理员。密码由终端安全读取，不会进入命令历史：

```powershell
.\.venv\Scripts\python.exe -m cloud_license.admin_cli --db .\work\cloud-license-test.db create-admin admin
.\start-license-test.ps1
```

打开 `http://127.0.0.1:8787/admin`。本机脚本会关闭 Secure Cookie，仅用于本机 HTTP 联调；正式环境必须保持 Secure Cookie 并使用 HTTPS。

桌面端本机联调运行：

```powershell
.\start-v2-dev.ps1
```

开发脚本会临时使用 `http://127.0.0.1:8787`，打包版本仍固定连接 `https://api.yituan123.com`。

## 正式部署边界

备案审核期间只完成本地构建和服务器离线准备，不创建公网 DNS 解析，不开放 80/443，也不开放 8787 或数据库端口。备案通过后再严格按 [deploy/README.md](deploy/README.md) 执行。

PostgreSQL 当前处于迁移准备阶段。内测人数少时继续使用 SQLite；切换前必须先备份、在空 PostgreSQL 数据库演练迁移并完成 API 回归测试。

## 回退

- V3.5 原项目：`C:\Users\ASUS\Documents\Codex\2026-09-01\new-chat\XianyuAutoAgent`
- 本工程 V3.5 基线提交：`e5b90a00c07d7be64e342981f933b0ff62d495b2`
- V3.6 初始云授权提交：`03b6377cf8f09501da62073bbb4bc92d06ceb29e`

需要回退时先备份测试数据库，再从对应提交创建新的工作副本；不要清理或覆盖 V3.5 目录。
