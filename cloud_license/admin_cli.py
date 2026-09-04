import argparse
import json
import os

from cloud_license.store import LicenseStore


def main():
    parser = argparse.ArgumentParser(description="Private-sales subscription admin")
    parser.add_argument("--db", default=os.getenv("LICENSE_DB", "cloud-license.db"))
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("password")
    create.add_argument("--name", default="")
    grant = sub.add_parser("grant")
    grant.add_argument("user_id", type=int)
    grant.add_argument("plan", choices=["trial", "monthly", "yearly"])
    grant.add_argument("--days", type=int)
    users = sub.add_parser("users")
    status = sub.add_parser("status")
    status.add_argument("user_id", type=int)
    status.add_argument("value", choices=["active", "frozen"])
    revoke = sub.add_parser("revoke-device")
    revoke.add_argument("user_id", type=int)
    revoke.add_argument("device_id")
    args = parser.parse_args()
    store = LicenseStore(args.db)
    if args.command == "create-user":
        result = store.create_user(args.username, args.password, args.name, actor="admin-cli")
    elif args.command == "grant":
        result = store.grant_subscription(args.user_id, args.plan, args.days, actor="admin-cli")
    elif args.command == "users":
        result = store.list_users()
    elif args.command == "status":
        result = store.set_user_status(args.user_id, args.value, actor="admin-cli")
    else:
        store.revoke_device(args.user_id, args.device_id, actor="admin-cli")
        result = {"revoked": True}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
