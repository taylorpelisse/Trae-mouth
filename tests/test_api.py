from __future__ import annotations

import io
import json
import zipfile

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import auth_payload, make_jwt


def make_client(config):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text="upstream disabled in tests")
    )
    app = create_app(config, transport=transport)
    return TestClient(app)


def setup_admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/admin/api/setup",
        json={"username": "admin", "password": "password123"},
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.cookies.get("tm_csrf", "")}


def test_setup_login_csrf(config) -> None:
    client = make_client(config)
    with client:
        page = client.get("/admin")
        assert 'id="root"' in page.text
        assert client.get("/admin/api/status").json()["initialized"] is False
        headers = setup_admin(client)
        assert client.get("/admin/api/status").json()["initialized"] is True
        me = client.get("/admin/api/me")
        assert me.json()["username"] == "admin"
        missing = client.post("/admin/api/accounts/import/code", json={})
        assert missing.status_code == 403
        ok = client.post(
            "/admin/api/accounts/import/code", json={}, headers=headers
        )
        assert ok.status_code == 200
        code = ok.json()["code"]
        assert code.startswith("tmimp_")
        check = client.post(
            "/public/accounts/import/check", json={"import_code": code}
        )
        assert check.status_code == 200
        assert check.json()["expires_at"] > 0
        invalid = client.post(
            "/public/accounts/import/check", json={"import_code": "1"}
        )
        assert invalid.status_code == 403
        oneclick = client.get(f"/public/helper/oneclick.bat?code={code}")
        assert oneclick.status_code == 200
        assert b"certutil -decode" in oneclick.content
        assert code.encode() in oneclick.content
        assert (
            client.get("/public/helper/oneclick.bat?code=bad").status_code == 403
        )


def test_change_password(config) -> None:
    client = make_client(config)
    with client:
        headers = setup_admin(client)
        wrong = client.post(
            "/admin/api/password",
            json={"current_password": "wrong", "new_password": "newpassword123"},
            headers=headers,
        )
        assert wrong.status_code == 400
        changed = client.post(
            "/admin/api/password",
            json={
                "current_password": "password123",
                "new_password": "newpassword123",
            },
            headers=headers,
        )
        assert changed.status_code == 200
        client.post("/admin/api/logout", json={}, headers=headers)
        old_login = client.post(
            "/admin/api/login",
            json={"username": "admin", "password": "password123"},
        )
        assert old_login.status_code == 401
        new_login = client.post(
            "/admin/api/login",
            json={"username": "admin", "password": "newpassword123"},
        )
        assert new_login.status_code == 200


def test_manual_and_public_storage_import(config) -> None:
    client = make_client(config)
    with client:
        headers = setup_admin(client)
        token = make_jwt({"data": {"id": "manual-1"}, "exp": 4102444800})
        response = client.post(
            "/admin/api/accounts/import/manual",
            json={"token": token, "label": "手动账号"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        account = response.json()["account"]
        assert account["user_id"] == "manual-1"
        assert account["label"] == "手动账号"
        assert account["last_refreshed_at"] is None

        code_response = client.post(
            "/admin/api/accounts/import/code", json={}, headers=headers
        )
        code = code_response.json()["code"]
        _, storage = auth_payload(user_id="public-1", username="public-user")
        response = client.post(
            "/public/accounts/import/storage",
            json={
                "import_code": code,
                "encrypted_value": storage["iCubeAuthInfo://icube.cloudide"],
                "server_data": storage["iCubeServerData://icube.cloudide"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["account"]["user_id"] == "public-1"
        assert response.json()["account"]["plan"] == "Pro"
        # Code is single-use.
        again = client.post(
            "/public/accounts/import/storage",
            json={
                "import_code": code,
                "encrypted_value": storage["iCubeAuthInfo://icube.cloudide"],
            },
        )
        assert again.status_code == 403
        accounts = client.get("/admin/api/accounts", headers=headers).json()["accounts"]
        assert {row["user_id"] for row in accounts} == {"manual-1", "public-1"}
        assert all("token" not in row for row in accounts)


def test_public_oauth_import(config) -> None:
    client = make_client(config)
    with client:
        headers = setup_admin(client)
        code = client.post(
            "/admin/api/accounts/import/code", json={}, headers=headers
        ).json()["code"]
        token = make_jwt({"data": {"id": "oauth-1"}, "exp": 4102444800})
        callback = {
            "userJwt": json.dumps(
                {
                    "Token": token,
                    "RefreshToken": "oauth-refresh",
                    "TokenExpireAt": "2099-01-01T00:00:00.000Z",
                    "ClientID": "ono9krqynydwx5",
                }
            ),
            "userInfo": json.dumps(
                {
                    "UserID": "oauth-1",
                    "ScreenName": "OAuth User",
                    "AvatarUrl": "https://example.invalid/a.png",
                }
            ),
            "login_trace_id": "trace-1",
        }
        response = client.post(
            "/public/accounts/import/oauth",
            json={
                "import_code": code,
                "callback": callback,
                "device": {
                    "machine_id": "a" * 32,
                    "device_id": "1234567890123456789",
                    "brand": "83DG",
                    "cpu": "AMD",
                    "device_type": "windows",
                    "os_version": "Windows 11 Pro",
                    "app_version": "0.1.7",
                    "ide_version": "3.4.4",
                    "ide_version_code": "20260212",
                },
                "login_trace_id": "trace-1",
            },
        )
        assert response.status_code == 200, response.text
        account = response.json()["account"]
        assert account["user_id"] == "oauth-1"
        assert account["username"] == "OAuth User"
        assert account["has_refresh_token"] is True
        detail = client.get(f"/admin/api/accounts/{account['id']}", headers=headers)
        assert detail.json()["device"]["machine_id"] == "a" * 32


def test_helper_bundle_and_code_validation(config) -> None:
    client = make_client(config)
    with client:
        headers = setup_admin(client)
        code = client.post(
            "/admin/api/accounts/import/code", json={}, headers=headers
        ).json()["code"]
        response = client.get(f"/public/helper/bundle?code={code}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = set(zf.namelist())
            assert {"start_login.bat", "login_helper.py", "README.txt"} <= names
            bat = zf.read("start_login.bat").decode("utf-8")
            assert code in bat
        assert client.get("/public/helper/bundle?code=bad").status_code == 403


def test_account_actions(config) -> None:
    client = make_client(config)
    with client:
        headers = setup_admin(client)
        token = make_jwt({"data": {"id": "actions-1"}, "exp": 4102444800})
        account = client.post(
            "/admin/api/accounts/import/manual",
            json={"token": token},
            headers=headers,
        ).json()["account"]
        account_id = account["id"]
        assert client.post(
            f"/admin/api/accounts/{account_id}/disable", json={}, headers=headers
        ).json()["account"]["status"] == "disabled"
        assert client.post(
            f"/admin/api/accounts/{account_id}/enable", json={}, headers=headers
        ).json()["account"]["status"] == "active"
        patched = client.patch(
            f"/admin/api/accounts/{account_id}",
            json={"label": "新备注", "concurrency_limit": 5},
            headers=headers,
        )
        assert patched.json()["account"]["label"] == "新备注"
        assert patched.json()["account"]["concurrency_limit"] == 5
        events = client.get(
            f"/admin/api/accounts/{account_id}/events", headers=headers
        ).json()["events"]
        assert any(event["event"] == "account_imported" for event in events)
        assert client.delete(
            f"/admin/api/accounts/{account_id}", headers=headers
        ).status_code == 200
