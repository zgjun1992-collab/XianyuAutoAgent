import argparse
import sqlite3
from pathlib import Path

import psycopg
from psycopg import sql


COPY_TABLES = ("plans", "users", "admins", "subscriptions", "devices", "audit_logs")
SEQUENCED_TABLES = ("users", "admins", "subscriptions", "devices", "audit_logs")


def source_rows(connection, table):
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    return columns, [tuple(row[column] for column in columns) for row in rows]


def ensure_empty(connection):
    occupied = []
    for table in ("users", "admins", "subscriptions", "devices"):
        count = connection.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
        if count:
            occupied.append(f"{table}={count}")
    if occupied:
        raise SystemExit("目标PostgreSQL不是空库，已停止迁移：" + ", ".join(occupied))


def migrate(sqlite_path, database_url, schema_path):
    source_uri = Path(sqlite_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, psycopg.connect(database_url) as target:
        source.row_factory = sqlite3.Row
        with target.cursor() as cursor:
            cursor.execute(Path(schema_path).read_text(encoding="utf-8"))
        target.commit()
        ensure_empty(target)
        copied = {}
        for table in COPY_TABLES:
            columns, rows = source_rows(source, table)
            if table == "plans":
                enabled_index = columns.index("enabled")
                rows = [
                    tuple(bool(value) if index == enabled_index else value for index, value in enumerate(row))
                    for row in rows
                ]
                with target.cursor() as cursor:
                    cursor.execute("DELETE FROM plans")
            if rows:
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns),
                )
                with target.cursor() as cursor:
                    cursor.executemany(statement, rows)
            copied[table] = len(rows)
        with target.cursor() as cursor:
            for table in SEQUENCED_TABLES:
                cursor.execute(
                    sql.SQL(
                        "SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE((SELECT MAX(id) FROM {}),1), "
                        "EXISTS(SELECT 1 FROM {}))"
                    ).format(sql.Identifier(table), sql.Identifier(table)),
                    (table,),
                )
        target.commit()
    return copied


def main():
    parser = argparse.ArgumentParser(description="Migrate XianyuCardAI license data from SQLite to PostgreSQL")
    parser.add_argument("sqlite_path")
    parser.add_argument("database_url", help="postgresql://user:password@host/database")
    parser.add_argument(
        "--schema",
        default=str(Path(__file__).with_name("schema.sql")),
        help="PostgreSQL schema file",
    )
    parser.add_argument("--confirm", action="store_true", help="required before writing the PostgreSQL target")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("这是写入操作；确认目标为空库后追加 --confirm")
    copied = migrate(args.sqlite_path, args.database_url, args.schema)
    for table, count in copied.items():
        print(f"{table}: {count}")
    print("迁移完成。客户端会话和管理员会话未迁移，切换后所有用户需要重新登录。")


if __name__ == "__main__":
    main()
