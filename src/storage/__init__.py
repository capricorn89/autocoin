"""시장데이터 저장 계층 (PostgreSQL + TimescaleDB).

 - schema.sql       : 테이블/hypertable 정의 (멱등)
 - compression.sql  : 압축 정책 (TimescaleDB 라이선스 빌드에서만 동작, 실패해도 수집에는 영향 없음)
 - db.py            : DSN, DB 생성, 스키마 적용 CLI
 - pg_sink.py       : 수집기 레코드 → DB 배치 적재 싱크
"""
