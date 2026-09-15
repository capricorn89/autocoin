"""DB 연결 설정과 스키마 관리.

DSN: 환경변수 AUTOCOIN_PG_DSN, 기본 postgresql:///autocoin (로컬 유닉스 소켓, 현재 OS 사용자, 비밀번호 없음).
비밀이 아니므로 저장소 밖 시크릿 파일로 관리하지 않는다.

  python -m src.storage.db init                 # DB 생성(없으면) + 스키마 적용
  python -m src.storage.db replay-spill <파일>  # 적재 실패로 떨어진 spill JSONL 재적재
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_DSN = "postgresql:///autocoin"
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
COMPRESSION_SQL = Path(__file__).with_name("compression.sql")


def get_dsn(dsn: str | None = None) -> str:
    return dsn or os.getenv("AUTOCOIN_PG_DSN", DEFAULT_DSN)


def ensure_database(dsn: str) -> bool:
    """DB 가 없으면 생성. 생성했으면 True."""
    try:
        psycopg.connect(dsn).close()
        return False
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
    name = conninfo_to_dict(dsn)["dbname"]
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return True


def apply_schema(dsn: str) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        try:
            conn.execute(COMPRESSION_SQL.read_text(encoding="utf-8"))
            compression, error = True, None
        except psycopg.Error as e:
            compression, error = False, str(e).strip().splitlines()[0]
        version = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()[0]
    return {"timescaledb": version, "compression": compression, "compression_error": error}


def main() -> None:
    ap = argparse.ArgumentParser(description="autocoin DB 관리")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    rp = sub.add_parser("replay-spill")
    rp.add_argument("path")
    args = ap.parse_args()
    dsn = get_dsn(args.dsn)

    if args.cmd == "init":
        created = ensure_database(dsn)
        info = apply_schema(dsn)
        print(json.dumps({"dsn": dsn, "created": created, **info}, ensure_ascii=False))
    elif args.cmd == "replay-spill":
        from .pg_sink import replay_spill
        n = replay_spill(args.path, dsn)
        print(f"{n}행 재적재 완료: {args.path}")


if __name__ == "__main__":
    main()
