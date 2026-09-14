import argparse
import getpass
import json
import os

from cloud_license.store import LicenseStore


def secret(prompt, confirmation=False):
    value = getpass.getpass(prompt)
    if confirmation and value != getpass.getpass("再次输入密码："):
        raise SystemExit("两次输入的密码不一致")
    return value


def main():
    parser = argparse.ArgumentParser(description="Private-sales subscription administration")
    parser.add_argument("--db", default=os.getenv("LICENSE_DB", "cloud-license.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    create_admin = sub.add_parser("create-admin", help="创建管理后台账号")
    create_admin.add_argument("username")

    create = sub.add_parser("create-user", help="创建客户账号")
    create.add_argument("username")
    create.add_argument("--name", default="")

    grant = sub.add_parser("grant", help="开通或续费")
    grant.add_argument("user_id", type=int)
    grant.add_argument("plan", choices=["trial", "weekly", "monthly", "quarterly", "yearly"])
    grant.add_argument("--days", type=int)
    grant.add_argument("--note", default="")

    sub.add_parser("users", help="列出客户")

    status = sub.add_parser("status", help="冻结或恢复客户")
    status.add_argument("user_id", type=int)
    status.add_argument("value", choices=["active", "frozen"])

    revoke = sub.add_parser("revoke-device", help="解绑设备")
    revoke.add_argument("user_id", type=int)
    revoke.add_argument("device_id")

    sub.add_parser("purge-sessions", help="清理过期会话")

    args = parser.parse_args()
    store = LicenseStore(args.db)
    if args.command == "create-admin":
        result = store.create_admin(args.username, secret("管理员密码：", confirmation=True), actor="admin-cli")
    elif args.command == "create-user":
        result = store.create_user(
            args.username, secret("客户初始密码：", confirmation=True), args.name, actor="admin-cli"
        )
    elif args.command == "grant":
        result = store.grant_subscription(
            args.user_id, args.plan, args.days, note=args.note, actor="admin-cli"
        )
    elif args.command == "users":
        result = store.list_users()
    elif args.command == "status":
        result = store.set_user_status(args.user_id, args.value, actor="admin-cli")
    elif args.command == "revoke-device":
        store.revoke_device(args.user_id, args.device_id, actor="admin-cli")
        result = {"revoked": True}
    else:
        result = store.purge_expired_sessions()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
