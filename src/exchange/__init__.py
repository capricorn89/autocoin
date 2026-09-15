"""거래소 연동 계층 (Binance USDT-M Futures).

 - auth.py      : API 키 로드 + HMAC 서명 (비즈니스 로직 없음)
 - rest.py      : REST 클라이언트 (재시도, rate-limit weight 추적)
 - orderbook.py : 로컬 오더북 + 시퀀스 검증
 - ws.py        : WebSocket 오더북/체결 수집기 (재연결, heartbeat, 시퀀스 갭 재동기화)
 - sinks.py     : 수집 레코드 저장소 (M1: JSONL, M2에서 TimescaleDB로 교체)
"""
