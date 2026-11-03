from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, booking_body


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


def test_login_is_rate_limited(client, admin_user):
    for _ in range(10):
        client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "guess"})
    # Even the right password is refused until the window passes
    res = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert res.status_code == 429


def test_login_is_rate_limited_per_account_across_ips(client, admin_user):
    for i in range(20):
        client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "guess"},
                    headers={"x-forwarded-for": f"198.51.100.{i}"})
    res = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      headers={"x-forwarded-for": "198.51.100.200"})
    assert res.status_code == 429


def test_edit_contact(client, auth, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    client.post("/v1/bookings", json=booking_body(email="other@example.com"))
    lead = client.get("/v1/admin/leads", headers=auth, params={"q": "jane@"}).json()["items"][0]
    url = f"/v1/admin/contacts/{lead['contact']['id']}"

    res = client.patch(url, headers=auth, json={"name": "Jane Doe", "timezone": "Asia/Singapore"})
    assert res.status_code == 200 and res.json()["name"] == "Jane Doe" and res.json()["timezone"] == "Asia/Singapore"
    assert client.patch(url, headers=auth, json={"timezone": "Mars/Base"}).status_code == 422
    assert client.patch(url, headers=auth, json={"name": None}).status_code == 422
    assert client.patch(url, headers=auth, json={"email": "other@example.com"}).status_code == 409
    assert client.patch(url, headers=auth, json={"email": None, "phone_e164": None}).status_code == 422


def test_logout(client, admin_user):
    tokens = login(client)
    other = login(client)
    assert client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    # A logged-out token being sent again isn't theft: other devices stay signed in
    assert client.post("/v1/auth/refresh", json={"refresh_token": other["refresh_token"]}).status_code == 200


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_sessions_list_and_log_out_a_device(client, admin_user):
    phone = login(client)
    tablet = client.post("/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                                                 "device_label": "Tablet"}).json()
    # Refreshing keeps the session's sign-in time and marks it active now
    phone = client.post("/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]}).json()

    sessions = client.get("/v1/auth/sessions", headers=bearer(phone)).json()
    assert sorted(s["device_label"] for s in sessions) == ["Pixel 8", "Tablet"]
    current = [s for s in sessions if s["is_current"]]
    assert len(current) == 1 and current[0]["device_label"] == "Pixel 8"
    assert current[0]["signed_in_at"] <= current[0]["last_active_at"]

    tablet_id = next(s["id"] for s in sessions if s["device_label"] == "Tablet")
    assert client.delete(f"/v1/auth/sessions/{tablet_id}", headers=bearer(phone)).status_code == 204
    assert client.delete(f"/v1/auth/sessions/{tablet_id}", headers=bearer(phone)).status_code == 404

    # The tablet can't refresh any more, and that doesn't end the phone's session
    assert client.post("/v1/auth/refresh", json={"refresh_token": tablet["refresh_token"]}).status_code == 401
    assert [s["device_label"] for s in client.get("/v1/auth/sessions", headers=bearer(phone)).json()] == ["Pixel 8"]
    assert client.post("/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]}).status_code == 200


def test_sessions_need_a_token(client, admin_user):
    assert client.get("/v1/auth/sessions").status_code == 401
