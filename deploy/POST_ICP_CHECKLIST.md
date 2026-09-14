# 备案通过后的上线清单

更新时间：2026-09-14

## 当前状态

- [x] `api.yituan123.com` 备案已完成（以运营方提供的备案后台结果为准）。
- [x] V3.5 业务修复已迁入 V3.6，云授权、设备限制与 48 小时离线宽限保留。
- [x] 授权服务、Caddy、systemd、备份及恢复模板已在 `deploy/` 中准备。
- [ ] 在不经过本机代理/TUN 的公网 DNS 上确认 A 记录最终值。
- [ ] 在服务器上完成授权服务部署、管理员创建和本机健康检查。
- [ ] 开放 80/443、签发证书并完成公网健康检查。
- [ ] 完成客户端登录、设备限制、续费、冻结、离线宽限与恢复联网测试。

> 本机当前网络会把域名解析到 `198.18.0.0/15` 的代理测试地址，不能用该结果判断公网 DNS 是否已生效。必须从阿里云 DNS 控制台、服务器或不经过代理的公网网络复核。

## 1. 发布前锁定

1. 记录 V3.6 发布提交、安装包 SHA-256 和部署源码 SHA-256。
2. 确认发布包不包含 `.env`、数据库、日志、管理员密码、会话 Cookie 或授权 Token。
3. 备份服务器当前 `/opt/xianyu-license/app`、`/etc/xianyu-license.env` 和数据库。
4. 保留上一版安装包及服务器源码，发生故障时按同一数据库版本回退。

## 2. 服务器本机部署

按 `deploy/README.md` 完成用户、目录、venv、依赖、环境文件及 systemd 安装，然后执行：

```bash
sudo systemctl enable --now xianyu-license
curl --fail http://127.0.0.1:8787/health
sudo systemctl enable --now xianyu-license-backup.timer
sudo systemctl start xianyu-license-backup.service
sudo journalctl -u xianyu-license -n 100 --no-pager
sudo journalctl -u xianyu-license-backup.service -n 100 --no-pager
```

验收条件：服务只监听 `127.0.0.1:8787`，数据库和 8787 端口不对公网开放，备份文件可通过恢复脚本校验。

## 3. DNS、HTTPS 与防火墙

1. 在 DNS 控制台把 `api.yituan123.com` 的 A 记录指向已复核的授权服务器公网 IP。
2. 安全组和主机防火墙只开放 TCP 22、80、443；SSH 最好再限制来源 IP。
3. 安装 `deploy/caddy/Caddyfile`，校验并重载 Caddy：

```bash
sudo install -o root -g root -m 0644 deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail https://api.yituan123.com/health
```

验收条件：证书链有效，HTTP 自动跳转 HTTPS，响应包含安全头，公网无法访问 8787。

## 4. 授权与客户端端到端测试

- 新建月卡账号，V3.6 `0.11.1` 能登录并绑定当前设备。
- 第二设备登录符合设备上限；超过上限时明确拒绝。
- 续费后到期时间正确延长；冻结账号后现有会话失效。
- 断网后进入 48 小时宽限；超过宽限停止自动客服但不删除本地数据。
- 恢复网络后重新校验并回到在线授权状态。
- V3.5 与 V3.6 可并存，安装目录、数据目录、进程名和安装包标识不互相覆盖。
- 业务回归覆盖首次回复、门店、SKU、日期/餐段、购买时间、退款政策和人工接管。

## 5. 小流量发布与回退

1. 先给一个内部账号和一台设备发布，观察授权接口、登录失败率和客户端日志。
2. 通过后再扩大账号范围，不直接全量替换。
3. 若授权服务异常，先回退服务器源码并恢复健康检查；若客户端异常，回发上一版 V3.6 安装包。
4. 数据库恢复必须先停止服务，并使用 `deploy/scripts/restore-sqlite.sh`；恢复后重新执行健康检查和登录验证。
