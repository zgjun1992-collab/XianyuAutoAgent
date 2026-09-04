# PostgreSQL 迁移准备

小规模内测继续使用 SQLite。迁移前先停止授权服务并完成 SQLite 备份，然后在空的 PostgreSQL 数据库执行：

```bash
python deploy/postgres/migrate_sqlite_to_postgres.py \
  /var/lib/xianyu-license/cloud-license.db \
  'postgresql://USER:PASSWORD@HOST:5432/xianyu_license?sslmode=require' \
  --confirm
```

迁移脚本会复制客户、套餐、订阅、设备、管理员和审计记录，不复制客户会话或管理员会话。迁移后所有人必须重新登录，这可以避免旧令牌在切换期间继续生效。

脚本拒绝写入已有客户数据的 PostgreSQL，防止误覆盖。切换前还必须为服务端实现 PostgreSQL Store 并跑完相同测试；当前生产内测配置仍使用 SQLite。

