import pytest

from src.exchange import auth
from src.exchange.auth import (API_KEY_HEADER, BinanceCredentials, MissingCredentialsError,
                               UnsafeSecretLocationError, build_signed_query, load_secrets_env,
                               sign_query)

# 저장소에 실제/문서 예제 키를 두지 않는다. 아래는 테스트 전용 더미 값과
# 그 값으로 미리 계산해 고정한 HMAC-SHA256 결과(알고리즘 회귀 검증용).
DUMMY_SIGNING_VALUE = "unit-test-not-a-real-secret"
TEST_QUERY = "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.001&recvWindow=5000&timestamp=1789478631342"
TEST_SIGNATURE = "4264e617e762daaf422d3a1c9aab3dfd9386963719eb38f2300788196b19802d"


def test_sign_query_known_vector():
    assert sign_query(DUMMY_SIGNING_VALUE, TEST_QUERY) == TEST_SIGNATURE


def test_build_signed_query_appends_signature_and_header():
    creds = BinanceCredentials("dummy-key", DUMMY_SIGNING_VALUE)
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.001}
    query, headers = build_signed_query(creds, params, recv_window_ms=5000,
                                        timestamp_ms=1789478631342)
    assert query == f"{TEST_QUERY}&signature={TEST_SIGNATURE}"
    assert headers == {API_KEY_HEADER: "dummy-key"}


def test_repr_masks_secret():
    creds = BinanceCredentials("abcdefghijkl", "supersecretvalue")
    text = repr(creds) + str(creds)
    assert "supersecretvalue" not in text
    assert "abcdefghijkl" not in text


def test_from_env_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(MissingCredentialsError):
        BinanceCredentials.from_env(dotenv_path=tmp_path / "nope.env")


def test_from_env_reads_external_file_without_overriding(monkeypatch, tmp_path):
    env = tmp_path / "autocoin.env"
    env.write_text("BINANCE_API_KEY=fromfile\nBINANCE_API_SECRET=filesecret\n")
    monkeypatch.setenv("BINANCE_API_KEY", "fromenv")
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    creds = BinanceCredentials.from_env(dotenv_path=env)
    assert creds.api_key == "fromenv"
    assert creds.api_secret == "filesecret"


def test_default_env_file_is_outside_repo(monkeypatch):
    monkeypatch.delenv("AUTOCOIN_ENV_FILE", raising=False)
    p = auth.secrets_env_path()
    assert str(p).endswith(".config/autocoin/.env")
    assert not p.is_relative_to(auth.REPO_ROOT)


def test_env_file_inside_repo_is_rejected(monkeypatch):
    with pytest.raises(UnsafeSecretLocationError):
        load_secrets_env(auth.REPO_ROOT / ".env")
    monkeypatch.setenv("AUTOCOIN_ENV_FILE", str(auth.REPO_ROOT / "config" / "keys.env"))
    with pytest.raises(UnsafeSecretLocationError):
        load_secrets_env()
