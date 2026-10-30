"""pastofi 코인판 로직1 — 5분 급등 뒤 1분 OFI 역방향이면 매도(숏), 익절 2%·손절 1%·최대 5분.

스펙: spec_v3.yaml (pastofi-crypto-1.2, 동결. v2 는 5분 청산이 시장가만, v1 은 수량 규칙도 달랐다). 페이퍼·라이브는 같은 코드 경로를 타고 브로커만 다르다.

engine.py · params.py · tick.py 는 krx-hft 의 pastofi 패키지를 그대로 복사한 것이다 (krx-hft 를 import 하지 않는다).
    출처: ~/Desktop/krx-hft  커밋 a6d3d9b (pastofi-1.5) — engine.py 는 1.3(ce9652f)부터 같다.
    바꾼 곳: engine.py 의 import 두 줄(`from pastofi.params` → `from .params`, `from pastofi.tick` → `from .tick`)뿐.
    tests/test_pastofi_crypto_spec.py 가 sha256 으로 고정한다. params.py 기본값은 KRX용이라 쓰지 않는다 —
    spec.py 가 스펙의 값을 전부 명시해 dataclasses.replace 로 만든다.

모듈
    spec.py        스펙 로드·sha256·엔진 파라미터·수량 규칙
    feed.py        WS 레코드(book_top·aggTrade) → 엔진. 실시간과 DB 재생이 같이 쓴다
    replay.py      DB 하루 재생 (일치 검증·매일 대조)
    brokers.py     페이퍼(PaperExchange + 실시간 호가 뷰) / 라이브(BinanceFuturesBroker + closePosition 비상 손절)
    trader.py      신호 → 주문, 장부·위험 한도, 기록
    runner.py      실행 프로세스 (WS 연결·시계·상태 파일·알림)
    notify.py      텔레그램 문구
    store.py       거래 기록 DB(exec.pastofi_*)
    reconcile.py   매일 일치 대조
    report.py      7일 리포트
    web.py         Tailscale 모니터링 웹
"""
