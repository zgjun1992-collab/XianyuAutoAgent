# `api.yituan123.com` 部署说明

备案通过前只准备文件，不开放 80/443，不把服务暴露到公网。正式服务由 Caddy 终止 HTTPS，再转发到只监听 `127.0.0.1:8787` 的 FastAPI/Uvicorn。

## 服务器目录

```text
/opt/xianyu-license/app       应用源码
/opt/xianyu-license/venv      Python 虚拟环境
/var/lib/xianyu-license       SQLite 数据库
/var/backups/xianyu-license   本机备份
/etc/xianyu-license.env       非公开运行配置
```

## 首次准备

```bash
sudo apt update
sudo apt install -y python3-venv sqlite3 curl caddy
sudo useradd --system --home /var/lib/xianyu-license --shell /usr/sbin/nologin xianyu-license
sudo install -d -o xianyu-license -g xianyu-license -m 0750 /var/lib/xianyu-license /var/log/xianyu-license
sudo install -d -o root -g root -m 0755 /opt/xianyu-license/app
sudo python3 -m venv /opt/xianyu-license/venv
sudo /opt/xianyu-license/venv/bin/pip install --upgrade pip
sudo /opt/xianyu-license/venv/bin/pip install -r /opt/xianyu-license/app/cloud_license/requirements.txt
sudo install -o root -g xianyu-license -m 0640 deploy/env/license.env.example /etc/xianyu-license.env
sudo install -o root -g root -m 0644 deploy/systemd/xianyu-license.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/xianyu-license-backup.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/xianyu-license-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

正式环境文件 `/etc/xianyu-license.env` 中不得保存管理员密码；创建管理员后，只保留数据库路径、域名、监听地址等非账号配置。

先创建管理员，密码会在终端安全输入，不进入命令历史：

```bash
sudo -u xianyu-license env LICENSE_DB=/var/lib/xianyu-license/cloud-license.db \
  /opt/xianyu-license/venv/bin/python -m cloud_license.admin_cli create-admin admin
```

启动仅本机可访问的授权服务：

```bash
sudo systemctl enable --now xianyu-license
curl --fail http://127.0.0.1:8787/health
sudo systemctl enable --now xianyu-license-backup.timer
```

## 备案通过后开放 HTTPS

1. 将 `api.yituan123.com` 的 A 记录指向 `47.98.226.56`。
2. 阿里云防火墙只开放 TCP 22、80、443；不要开放 8787 和数据库端口。
3. 将 `deploy/caddy/Caddyfile` 安装为 `/etc/caddy/Caddyfile`。
4. 检查并重载 Caddy。

```bash
sudo install -o root -g root -m 0644 deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail https://api.yituan123.com/health
```

管理后台地址为 `https://api.yituan123.com/admin`。不要通过 HTTP 登录管理后台。

## 备份恢复

立即备份：

```bash
sudo systemctl start xianyu-license-backup.service
sudo journalctl -u xianyu-license-backup.service --since today
```

恢复前必须停止业务并指定备份：

```bash
sudo LICENSE_DB=/var/lib/xianyu-license/cloud-license.db \
  bash deploy/scripts/restore-sqlite.sh /var/backups/xianyu-license/cloud-license-TIMESTAMP.sqlite.gz
```

恢复脚本会校验备份、保留当前数据库安全副本、重启服务并检查健康状态。
