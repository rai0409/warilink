"""実 PostgreSQL(warilink_test)を使う API テスト。DB が無い環境ではスキップではなく失敗させる。"""

import os
import re
import uuid

os.environ.setdefault(
    "WARILINK_DATABASE_URL", "postgresql+psycopg://warilink:warilink-dev-only@localhost:55432/warilink_test"
)
os.environ["WARILINK_ENVIRONMENT"] = "test"

# このテストは開始時に全テーブルを downgrade し、テストごとに TRUNCATE する。
# 共有・開発・本番の DB に向いていたら、何もせずに中止する(fail-closed)。
DISPOSABLE_DB = re.compile(r"warilink_test(_[a-z0-9_]+)?")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _assert_disposable_database(url: str) -> None:
    from sqlalchemy.engine import make_url  # noqa: PLC0415

    u = make_url(url)
    if u.host not in LOCAL_HOSTS or not DISPOSABLE_DB.fullmatch(u.database or ""):
        raise SystemExit(
            "API テストは使い捨て DB(ローカルの warilink_test / warilink_test_*)でだけ実行できます。"
            f" 指定された接続先: host={u.host} database={u.database}(downgrade と TRUNCATE を行うため中止しました)"
        )


_assert_disposable_database(os.environ["WARILINK_DATABASE_URL"])

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import Base, get_engine  # noqa: E402
from app.main import app  # noqa: E402

API = "/api/v1"
HERE = os.path.dirname(__file__)


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    with get_engine().connect() as conn:  # 接続後にも実際の DB 名を確かめる(URL の書き換え・別名対策)
        actual = conn.execute(text("SELECT current_database()")).scalar_one()
    if not DISPOSABLE_DB.fullmatch(actual):
        raise SystemExit(f"接続先 DB が使い捨て DB ではありません: {actual}")
    cfg = Config(os.path.join(HERE, "..", "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "..", "..", "alembic"))
    cfg.set_main_option("sqlalchemy.url", os.environ["WARILINK_DATABASE_URL"])
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


# V0 で既定 OFF になった任意機能。API の既存テストは、これらを有効にした状態で従来どおり検証する。
# import 時に os.environ を書き換えると、同じプロセスで後から動く設定テスト(tests/test_production_config.py)に
# 漏れて既定 OFF の検証が崩れるため、テストごとに有効化し、終了時に元の値へ戻してから設定キャッシュを捨てる。
# 停止状態の確認は、各テストの中で環境変数を上書きして行う(test_destinations / test_meta)。
OPTIONAL_FEATURE_ENV = ("WARILINK_FEATURE_PASSKEYS", "WARILINK_FEATURE_PAYMENT_DESTINATIONS", "WARILINK_FEATURE_OCR")


@pytest.fixture(autouse=True)
def _enable_optional_features():
    saved = {k: os.environ.get(k) for k in OPTIONAL_FEATURE_ENV}
    os.environ.update({k: "true" for k in OPTIONAL_FEATURE_ENV})
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    yield
    names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} CASCADE"))


class User:
    """1人の利用者(独立した Cookie を持つブラウザ)を表す。"""

    def __init__(self) -> None:
        self.c = TestClient(app, headers={"X-WariLink-CSRF": "1"})
        r = self.c.post(f"{API}/identity/guest")
        assert r.status_code == 201, r.text

    def req(self, method: str, path: str, *, key: str | None = None, **kw):
        headers = kw.pop("headers", {})
        if method in ("POST",) and "idempotency-key" not in {k.lower() for k in headers}:
            headers["Idempotency-Key"] = key or str(uuid.uuid4())
        return self.c.request(method, f"{API}{path}", headers=headers, **kw)

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, **kw):
        return self.req("POST", path, **kw)

    def put(self, path, **kw):
        return self.req("PUT", path, **kw)

    def patch(self, path, **kw):
        return self.req("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self.req("DELETE", path, **kw)


@pytest.fixture
def user_factory():
    return User


def make_group(owner: User, names=("Bさん", "Cさん", "Dさん"), title="旅行"):
    r = owner.post("/groups", json={"title": title, "my_name": "Aさん", "other_member_names": list(names)})
    assert r.status_code == 201, r.text
    g = r.json()
    return g, {m["display_name"]: m["id"] for m in g["members"]}


def join_as(user: User, owner: User, public_id: str, claim_name: str):
    inv = owner.post(f"/groups/{public_id}/invites", json={}).json()
    preview = user.get(f"/invites/{inv['token']}").json()
    mid = next(m["id"] for m in preview["unclaimed_members"] if m["display_name"] == claim_name)
    r = user.post(f"/invites/{inv['token']}/join", json={"claim_member_id": mid})
    assert r.status_code == 200, r.text
    return r.json()


def equal_expense(payer_id: str, amount: int, member_ids, title="ランチ"):
    return {
        "title": title,
        "amount_minor": amount,
        "payers": [{"member_id": payer_id, "amount_minor": amount}],
        "split": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in member_ids]},
    }
