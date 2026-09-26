"""텔레그램 알림.

토큰은 저장소 밖 ~/.config/autocoin/.env 에만 둔다 (auth.load_secrets_env 와 같은 규칙).
    TELEGRAM_BOT_TOKEN=...   (krx-hft 변수명 TELEGRAM_TOKEN 도 허용)
    TELEGRAM_CHAT_ID=...

토큰이 들어간 URL·예외 문자열은 절대 로그에 남기지 않는다.

  python -m src.notify --test      # 설정 확인용 테스트 메시지 전송
"""
from __future__ import annotations

import argparse
import logging
import os

import requests

from .exchange.auth import load_secrets_env

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None,
                 timeout: float = 10.0, prefix: str = "[autocoin]"):
        self._token = token
        self.chat_id = chat_id
        self._session = session or requests.Session()
        self.timeout = timeout
        self.prefix = prefix

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self.chat_id}, token=***)"

    @classmethod
    def from_env(cls, env_file=None) -> TelegramNotifier | None:
        load_secrets_env(env_file)
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
        chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        return cls(token, chat_id) if token and chat_id else None

    def send(self, text: str) -> bool:
        """전송 성공(또는 재시도해도 소용없는 거절)이면 True, 일시 실패면 False(재시도 대상)."""
        body = f"{self.prefix} {text}"[:MAX_LEN]
        try:
            r = self._session.post(API.format(token=self._token),
                                   data={"chat_id": self.chat_id, "text": body,
                                         "disable_web_page_preview": "true"},
                                   timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("텔레그램 전송 실패(%s) — 나중에 재시도", type(e).__name__)
            return False
        if r.status_code == 200:
            return True
        if r.status_code == 429 or r.status_code >= 500:
            log.warning("텔레그램 전송 실패 HTTP %s — 나중에 재시도", r.status_code)
            return False
        # 400/401/403 등: 토큰·chat_id 설정 오류. 재시도해도 같으므로 버린다.
        log.error("텔레그램 전송 거절 HTTP %s — 토큰/chat_id 확인 필요 (메시지 폐기)", r.status_code)
        return True


def list_chats(token: str, session: requests.Session | None = None) -> list[dict]:
    """봇에게 최근 메시지를 보낸 대화방 목록 (getUpdates). chat_id 확인용."""
    s = session or requests.Session()
    try:
        r = s.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    except requests.RequestException as e:
        raise SystemExit(f"getUpdates 실패({type(e).__name__})") from None
    if r.status_code != 200:
        raise SystemExit(f"getUpdates 실패 HTTP {r.status_code} — 토큰을 확인하세요.")
    chats: dict[int, dict] = {}
    for u in r.json().get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = {"chat_id": c["id"], "type": c.get("type"),
                              "name": c.get("title") or " ".join(
                                  x for x in (c.get("first_name"), c.get("last_name")) if x)
                              or c.get("username")}
    return list(chats.values())


def main() -> None:
    ap = argparse.ArgumentParser(description="텔레그램 알림 설정 확인")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--chats", action="store_true",
                    help="봇에게 말을 건 대화방의 chat_id 출력 (TELEGRAM_BOT_TOKEN 만 있으면 됨)")
    args = ap.parse_args()
    logging.basicConfig(level="INFO")
    if args.chats:
        load_secrets_env()
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN 이 ~/.config/autocoin/.env 에 없습니다.")
        chats = list_chats(token)
        if not chats:
            print("대화 기록이 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        for c in chats:
            print(f"chat_id={c['chat_id']}  type={c['type']}  name={c['name']}")
        return
    n = TelegramNotifier.from_env()
    if n is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 ~/.config/autocoin/.env 에 없습니다.")
    if args.test:
        ok = n.send("테스트 메시지예요. 수집 끊김 알림이 이 대화방으로 와요.")
        raise SystemExit(0 if ok else 1)
    print(n)


if __name__ == "__main__":
    main()
