"""Binance API 인증: 키 로드 + HMAC-SHA256 서명.

이 모듈은 인증만 담당한다. 주문/포지션 등 비즈니스 로직을 넣지 말 것.

시크릿 취급 규칙:
 - 키는 환경변수 또는 **저장소 밖** env 파일에서만 읽는다. 코드/설정 파일에 하드코딩 금지.
   기본 위치: ~/.config/autocoin/.env  (AUTOCOIN_ENV_FILE 환경변수로 변경 가능)
 - env 파일이 저장소 안에 있으면 로드를 거부한다 (실수로 커밋되는 경로 차단).
 - repr/로그에 시크릿이 찍히지 않도록 마스킹한다.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = "~/.config/autocoin/.env"
API_KEY_HEADER = "X-MBX-APIKEY"
# Binance 권장 기본값. 서버 수신 시각이 timestamp + recvWindow 를 넘으면 거절된다.
DEFAULT_RECV_WINDOW_MS = 5000


class MissingCredentialsError(RuntimeError):
    """서명 요청에 필요한 API 키가 없음."""


class UnsafeSecretLocationError(RuntimeError):
    """env 파일이 저장소 안에 있음."""


def secrets_env_path() -> Path:
    return Path(os.getenv("AUTOCOIN_ENV_FILE", DEFAULT_ENV_FILE)).expanduser().resolve()


def load_secrets_env(path: str | Path | None = None, repo_root: Path = REPO_ROOT) -> Path:
    """저장소 밖 env 파일을 로드(기존 환경변수는 덮어쓰지 않음). 파일이 없으면 조용히 넘어간다."""
    p = Path(path).expanduser().resolve() if path is not None else secrets_env_path()
    if p.is_relative_to(repo_root.resolve()):
        raise UnsafeSecretLocationError(
            f"시크릿 env 파일은 저장소 밖에 둬야 합니다: {p} (기본 {DEFAULT_ENV_FILE})")
    if p.exists():
        load_dotenv(p, override=False)
    return p


def _mask(value: str) -> str:
    return f"{value[:4]}…({len(value)}자)" if len(value) > 4 else "***"


@dataclass(frozen=True)
class BinanceCredentials:
    api_key: str
    api_secret: str

    def __repr__(self) -> str:  # 시크릿 노출 방지
        return f"BinanceCredentials(api_key={_mask(self.api_key)}, api_secret=***)"

    __str__ = __repr__

    @classmethod
    def from_env(cls, key_var: str = "BINANCE_API_KEY",
                 secret_var: str = "BINANCE_API_SECRET",
                 dotenv_path: str | Path | None = None) -> BinanceCredentials:
        """환경변수에서 로드. 저장소 밖 env 파일(기본 ~/.config/autocoin/.env)을 먼저 읽는다."""
        env_file = load_secrets_env(dotenv_path)
        key = os.getenv(key_var, "").strip()
        secret = os.getenv(secret_var, "").strip()
        if not key or not secret:
            raise MissingCredentialsError(
                f"{key_var}/{secret_var} 가 설정되지 않았습니다 ({env_file} 에 입력).")
        return cls(api_key=key, api_secret=secret)


def sign_query(secret: str, query: str) -> str:
    """쿼리 문자열의 HMAC-SHA256 hex 서명."""
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def build_signed_query(creds: BinanceCredentials, params: dict | None = None,
                       recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
                       timestamp_ms: int | None = None) -> tuple[str, dict[str, str]]:
    """서명된 쿼리 문자열과 인증 헤더를 반환.

    timestamp 는 요청 직전에 생성해야 하므로 재시도마다 다시 호출할 것.
    """
    p = dict(params or {})
    p["recvWindow"] = recv_window_ms
    p["timestamp"] = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    query = urlencode(p)
    query = f"{query}&signature={sign_query(creds.api_secret, query)}"
    return query, {API_KEY_HEADER: creds.api_key}
