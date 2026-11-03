from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


def login(client):
    return client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                                               "device_label": "Pixel 8"}).json()


def test_wrong_password(client, admin_user):
    res = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "nope"})
    assert res.status_code == 401
    res = client.post("/v1/auth/login", json={"email": "who@example.com", "password": "nope"})
    assert res.status_code == 401


def test_admin_routes_need_a_token(client, admin_user):
    assert client.get("/v1/admin/leads").status_code == 401
    assert client.get("/v1/admin/leads", headers={"Authorization": "Bearer junk"}).status_code == 401
    tokens = login(client)
    res = client.get("/v1/admin/leads", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert res.status_code == 200


def test_refresh_rotates_and_detects_reuse(client, admin_user):
    first = login(client)
    second = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"]

    # Re-using the rotated-out token revokes every session, including the new one
    assert client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert client.post("/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 401


def test_logout(client, admin_user):
    tokens = login(client)
    assert client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
