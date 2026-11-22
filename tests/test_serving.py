"""Security and behaviour tests for serving/user_app.py and serving/admin_app.py.

Needs a PostgreSQL server. TEST_DATABASE_URL must point at a database the
tests may wipe, e.g.
    TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost/sentinel_test pytest tests/test_serving.py

Ported from the former test_security.py (which targeted serving/main_secure.py).
"""

import io
import os
import sys
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

USER_SECRET = "u" * 40
ADMIN_SECRET = "a" * 40


@pytest.fixture(scope="session")
def migrated():
    from serving.migrate import migrate
    engine = create_engine(DB_URL)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    first = migrate(DB_URL)
    again = migrate(DB_URL)
    assert first == ["001_init_all_features.sql"] and again == []
    return engine


@pytest.fixture
def store(migrated):
    from serving.common import UserStore
    with migrated.begin() as c:
        c.execute(text("TRUNCATE audit_logs, users RESTART IDENTITY CASCADE"))
    return UserStore(DB_URL)


@pytest.fixture
def user_client(store):
    from serving.user_app import create_app
    return TestClient(create_app(store, USER_SECRET))


@pytest.fixture
def admin_client(store):
    from serving.admin_app import create_app
    return TestClient(create_app(store, ADMIN_SECRET))


def make_admin(store, name="admin", password="AdminPass123!"):
    from serving.common import RoleEnum
    store.create(name, password, f"{name}@example.com", RoleEnum.ADMIN)


def make_user(store, name="alice", password="AlicePass123!"):
    from serving.common import RoleEnum
    store.create(name, password, f"{name}@example.com", RoleEnum.USER)


def signup(client, name="alice", password="SecurePass123!"):
    r = client.post("/auth/signup", json={"username": name, "password": password, "email": f"{name}@example.com"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def admin_token(admin_client, name="admin", password="AdminPass123!"):
    r = admin_client.post("/auth/admin-login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def png():
    buf = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 200, 10)).save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("secret", ["", "short", "your-secret-key-change-in-production-xxxxxxxxxx"])
def test_apps_refuse_weak_or_missing_secret(store, monkeypatch, secret):
    from serving import admin_app, user_app
    monkeypatch.setenv("JWT_SECRET", secret)
    for module in (user_app, admin_app):
        with pytest.raises(RuntimeError):
            module.create_app(store)


def test_app_refuses_unmigrated_database(migrated):
    from serving.common import UserStore
    from serving.user_app import create_app
    engine = create_engine(DB_URL)
    with engine.begin() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS empty_schema"))
    url = DB_URL + ("&" if "?" in DB_URL else "?") + "options=-csearch_path%3Dempty_schema"
    with pytest.raises(RuntimeError, match="serving.migrate"):
        create_app(UserStore(url), USER_SECRET)


# --------------------------------------------------------------------------- #
# Public endpoints
# --------------------------------------------------------------------------- #

def test_health_public(user_client, admin_client):
    assert user_client.get("/health").json()["status"] == "healthy"
    assert admin_client.get("/health").json()["status"] == "healthy"


def test_no_admin_routes_on_user_app(user_client):
    for path in ["/users", "/audit-log", "/system-stats", "/metrics", "/admin/users"]:
        assert user_client.get(path).status_code == 404, path


def test_no_user_routes_on_admin_app(admin_client):
    assert admin_client.post("/auth/signup", json={"username": "x", "password": "y"}).status_code == 404
    assert admin_client.post("/generate-video", json={"prompt": "x"}).status_code == 404


# --------------------------------------------------------------------------- #
# Signup / login (user app)
# --------------------------------------------------------------------------- #

def test_signup_returns_bearer_token(user_client):
    r = user_client.post("/auth/signup", json={"username": "alice", "password": "SecurePass123!", "email": "a@x.io"})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"


def test_signup_requires_email(user_client):
    r = user_client.post("/auth/signup", json={"username": "alice", "password": "SecurePass123!"})
    assert r.status_code == 422


def test_signup_requires_min_password_length(user_client):
    r = user_client.post("/auth/signup", json={"username": "alice", "password": "short", "email": "a@x.io"})
    assert r.status_code == 400 and "at least 8 characters" in r.json()["detail"]


def test_signup_rejects_duplicate_username(user_client):
    signup(user_client)
    r = user_client.post("/auth/signup", json={"username": "alice", "password": "OtherPass123!", "email": "b@x.io"})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]


def test_signup_always_creates_plain_user(user_client, store):
    signup(user_client)
    assert store.get("alice").role == "user"


def test_login_with_correct_password(user_client):
    signup(user_client)
    r = user_client.post("/auth/login", json={"username": "alice", "password": "SecurePass123!"})
    assert r.status_code == 200


@pytest.mark.parametrize("name,password", [("alice", "WrongPass123!"), ("nobody", "SomePass123!")])
def test_login_failures(user_client, name, password):
    signup(user_client)
    r = user_client.post("/auth/login", json={"username": name, "password": password})
    assert r.status_code == 401 and "Invalid" in r.json()["detail"]


def test_users_persist_across_app_restarts(store):
    from serving.user_app import create_app
    signup(TestClient(create_app(store, USER_SECRET)))
    r = TestClient(create_app(store, USER_SECRET)).post(
        "/auth/login", json={"username": "alice", "password": "SecurePass123!"})
    assert r.status_code == 200


def test_admin_logging_into_user_app_gets_user_role(user_client, store):
    make_admin(store)
    r = user_client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
    assert jwt.decode(r.json()["access_token"], options={"verify_signature": False})["role"] == "user"


def test_password_stored_as_bcrypt(user_client, store):
    signup(user_client)
    stored = store.get("alice").password_hash
    assert stored != "SecurePass123!" and stored.startswith("$2")


def test_password_verification():
    from serving.common import hash_password, verify_password
    h = hash_password("SecurePass123!")
    assert verify_password("SecurePass123!", h) and not verify_password("WrongPass123!", h)


# --------------------------------------------------------------------------- #
# User endpoints
# --------------------------------------------------------------------------- #

def test_user_endpoints_require_auth(user_client):
    assert user_client.post("/generate-video", json={"prompt": "test"}).status_code == 401
    r = user_client.post("/generate-video", json={"prompt": "t"}, headers=bearer("invalid_token"))
    assert r.status_code == 401


def test_generate_video_returns_playable_video(user_client):
    import cv2
    r = user_client.post("/generate-video", json={"prompt": "test video"}, headers=bearer(signup(user_client)))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] in ("video/webm", "video/mp4")
    suffix = ".webm" if r.headers["content-type"] == "video/webm" else ".mp4"
    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"sentinel_test_download{suffix}"
    path.write_bytes(r.content)
    cap = cv2.VideoCapture(str(path))
    frames = 0
    while cap.read()[0]:
        frames += 1
    path.unlink()
    assert frames == 72  # 3 s at 24 fps


def test_preprocess(user_client):
    r = user_client.post("/preprocess", headers=bearer(signup(user_client)),
                         files={"file": ("x.png", png(), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"


def test_preprocess_bad_image_is_400_not_500(user_client):
    r = user_client.post("/preprocess", headers=bearer(signup(user_client)),
                         files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Admin access control
# --------------------------------------------------------------------------- #

ADMIN_GETS = ["/users", "/audit-log", "/system-stats", "/metrics"]


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_unauthenticated_rejected_on_admin(admin_client, path):
    assert admin_client.get(path).status_code == 401


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_user_app_token_rejected_on_admin(user_client, admin_client, path):
    """Tokens are signed per app, so even a valid user token fails signature checks here."""
    assert admin_client.get(path, headers=bearer(signup(user_client))).status_code == 401


def test_admin_signed_token_with_user_role_is_forbidden(admin_client, store):
    from serving.common import RoleEnum, TokenService
    token = TokenService(ADMIN_SECRET, 5).issue("alice", RoleEnum.USER).access_token
    r = admin_client.get("/users", headers=bearer(token))
    assert r.status_code == 403 and "Admin role required" in r.json()["detail"]


def test_non_admin_cannot_log_into_admin_app(admin_client, store):
    make_user(store)
    r = admin_client.post("/auth/admin-login", json={"username": "alice", "password": "AlicePass123!"})
    assert r.status_code == 401


def test_admin_lists_users(admin_client, store):
    make_admin(store)
    make_user(store)
    r = admin_client.get("/users", headers=bearer(admin_token(admin_client)))
    assert r.status_code == 200 and r.json()["count"] == 2


def test_users_from_user_app_visible_to_admin(user_client, admin_client, store):
    make_admin(store)
    signup(user_client, "bob")
    names = [u["username"] for u in admin_client.get("/users", headers=bearer(admin_token(admin_client))).json()["users"]]
    assert "bob" in names


def test_admin_promotes_user(admin_client, store):
    make_admin(store)
    make_user(store)
    r = admin_client.post("/promote", params={"username": "alice"}, headers=bearer(admin_token(admin_client)))
    assert r.status_code == 200 and "promoted" in r.json()["message"]
    assert store.get("alice").role == "admin"


def test_admin_deletes_user(admin_client, store):
    make_admin(store)
    make_user(store)
    r = admin_client.delete("/users/alice", headers=bearer(admin_token(admin_client)))
    assert r.status_code == 200 and store.get("alice") is None


def test_admin_cannot_delete_self(admin_client, store):
    make_admin(store)
    r = admin_client.delete("/users/admin", headers=bearer(admin_token(admin_client)))
    assert r.status_code == 400 and "Cannot delete your own account" in r.json()["detail"]


def test_deleting_admin_with_audit_history_is_409(admin_client, store):
    make_admin(store)
    make_admin(store, "admin2")
    admin_client.get("/users", headers=bearer(admin_token(admin_client, "admin2")))  # admin2 now has history
    r = admin_client.delete("/users/admin2", headers=bearer(admin_token(admin_client)))
    assert r.status_code == 409


def test_forged_signature_rejected(admin_client, store):
    make_admin(store)
    forged = admin_token(admin_client)[:-10] + "fakesignat"
    assert admin_client.get("/users", headers=bearer(forged)).status_code == 401


def test_admin_token_role_claim(admin_client, store):
    make_admin(store)
    payload = jwt.decode(admin_token(admin_client), options={"verify_signature": False})
    assert payload["role"] == "admin" and payload["sub"] == "admin"


# --------------------------------------------------------------------------- #
# Audit log (persisted in Postgres)
# --------------------------------------------------------------------------- #

def test_admin_actions_audited(admin_client, store):
    make_admin(store)
    make_user(store)
    token = admin_token(admin_client)
    admin_client.get("/users", headers=bearer(token))
    admin_client.post("/promote", params={"username": "alice"}, headers=bearer(token))
    log = admin_client.get("/audit-log", headers=bearer(token)).json()["audit_log"]
    actions = [(e["username"], e["action"]) for e in log]
    assert ("admin", "LIST_USERS") in actions and ("admin", "PROMOTE_TO_ADMIN") in actions


def test_audit_log_survives_restart(admin_client, store):
    from serving.admin_app import create_app
    make_admin(store)
    admin_client.get("/users", headers=bearer(admin_token(admin_client)))
    fresh = TestClient(create_app(store, ADMIN_SECRET))
    log = fresh.get("/audit-log", headers=bearer(admin_token(fresh))).json()["audit_log"]
    assert any(e["action"] == "LIST_USERS" for e in log)


# --------------------------------------------------------------------------- #
# create_admin CLI
# --------------------------------------------------------------------------- #

def test_create_admin_cli(store, monkeypatch, admin_client):
    from serving import create_admin
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("ADMIN_PASSWORD", "BootstrapPass123!")
    assert create_admin.main(["root", "root@example.com"]) == 0
    admin_token(admin_client, "root", "BootstrapPass123!")
    make_user(store)
    assert create_admin.main(["alice"]) == 0
    assert store.get("alice").role == "admin"
