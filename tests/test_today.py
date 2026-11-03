from datetime import UTC, datetime, timedelta

from app.services.leads import today
from tests.conftest import booking_body
from tests.test_admin import make_lead


def iso_in(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).replace(microsecond=0).isoformat()


def test_today_screen(client, auth, fake_cal):
    # New website lead with a call in 2 hours (always within today or tomorrow, IST)
    client.post("/v1/bookings", json=booking_body(start=iso_in(hours=2)))
    # A call next week isn't on Today
    client.post("/v1/bookings", json=booking_body(email="later@example.com", start=iso_in(days=7)))

    due = make_lead(client, auth, "Due", "due@example.com", temperature="hot", next_follow_up_at=iso_in(hours=-1))
    client.post(f"/v1/admin/leads/{due['id']}/contacted", headers=auth, json={"channel": "call"})
    make_lead(client, auth, "Not yet", "notyet@example.com", next_follow_up_at=iso_in(days=3))

    lead = make_lead(client, auth, "Quote", "quote@example.com")
    d = today()
    for title, valid_until in (("Lapsed", d - timedelta(days=1)), ("Soon", d + timedelta(days=2)),
                               ("Later", d + timedelta(days=30))):
        client.post(f"/v1/admin/leads/{lead['id']}/proposals", headers=auth, json={
            "title": title, "amount": "1000", "currency": "AUD", "status": "sent",
            "valid_until": valid_until.isoformat()})

    conv = client.post(f"/v1/admin/leads/{lead['id']}/convert", headers=auth, json={"title": "Site"}).json()
    for desc, amount, due_on in (("Late", "500", d - timedelta(days=2)), ("Late 2", "250", d - timedelta(days=1)),
                                 ("Soon", "300", d + timedelta(days=3)), ("Far", "900", d + timedelta(days=30))):
        client.post(f"/v1/admin/projects/{conv['project_id']}/payments", headers=auth, json={
            "description": desc, "amount": amount, "currency": "AUD", "due_on": due_on.isoformat()})

    res = client.get("/v1/admin/today", headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["date"] == d.isoformat() and body["timezone"] == "Asia/Kolkata"
    # New and never contacted: the two website bookings and "Not yet". "Due" was contacted; "Quote" is won.
    assert sorted(item["contact"]["email"] for item in body["new_leads"]) == [
        "jane@example.com", "later@example.com", "notyet@example.com"]
    assert [item["contact"]["name"] for item in body["follow_ups_due"]] == ["Due"]
    assert [c["contact"]["email"] for c in body["calls"]] == ["jane@example.com"]
    assert body["calls"][0]["lead_id"] and body["calls"][0]["booking"]["status"] == "confirmed"
    assert [p["description"] for p in body["payments_overdue"]] == ["Late", "Late 2"]
    assert [p["description"] for p in body["payments_due_soon"]] == ["Soon"]
    assert [(p["title"], p["is_expired"]) for p in body["proposals_expiring"]] == [("Lapsed", True), ("Soon", False)]
    assert body["proposals_expiring"][0]["contact"]["name"] == "Quote"

    counts = body["counts"]
    assert counts["new_leads"] == len(body["new_leads"]) == 3
    assert counts["hot_leads"] == 1 and counts["follow_ups_due"] == 1
    assert counts["overdue"] == {"AUD": "750.00"}
    assert counts["calls_today"] in (0, 1)  # the 2-hour call may fall after midnight IST


def test_today_is_empty_on_a_fresh_database(client, auth):
    body = client.get("/v1/admin/today", headers=auth).json()
    assert body["counts"] == {"new_leads": 0, "hot_leads": 0, "follow_ups_due": 0, "calls_today": 0, "overdue": {}}
    assert body["new_leads"] == body["calls"] == body["payments_overdue"] == body["proposals_expiring"] == []


def test_lead_list_temperature_counts(client, auth):
    make_lead(client, auth, "H1", "h1@example.com", temperature="hot", tags=["shop"])
    make_lead(client, auth, "H2", "h2@example.com", temperature="hot")
    make_lead(client, auth, "C", "c@example.com", temperature="cold", tags=["shop"])
    make_lead(client, auth, "U", "u@example.com", tags=["shop"])

    page = client.get("/v1/admin/leads", headers=auth, params={"temperature": "hot", "limit": 1}).json()
    # Counts ignore the temperature filter itself, so every pill shows its number
    assert page["counts"] == {"hot": 2, "warm": 0, "cold": 1, "unrated": 1}
    assert len(page["items"]) == 1
    # ...but respect the other filters
    shop = client.get("/v1/admin/leads", headers=auth, params={"tag": "shop"}).json()
    assert shop["counts"] == {"hot": 1, "warm": 0, "cold": 1, "unrated": 1}
    # Later pages don't recount
    page2 = client.get("/v1/admin/leads", headers=auth,
                       params={"temperature": "hot", "limit": 1, "cursor": page["next_cursor"]}).json()
    assert page2["counts"] is None


def test_find_contacts(client, auth):
    lead = make_lead(client, auth, "Jane", "Jane@Example.com", phone_e164="+61412345678")
    client.post(f"/v1/admin/leads/{lead['id']}/convert", headers=auth, json={"title": "Site"})
    make_lead(client, auth, "Other", "other@example.com", phone_e164="+61412345678")

    by_email = client.get("/v1/admin/contacts", headers=auth, params={"email": "jane@example.com"}).json()
    assert len(by_email) == 1 and by_email[0]["matched_on"] == ["email"]
    assert by_email[0]["client_id"] is not None and by_email[0]["open_leads"] == 0

    by_phone = client.get("/v1/admin/contacts", headers=auth, params={"phone": "+61 412 345 678"}).json()
    assert [m["contact"]["name"] for m in by_phone] == ["Jane", "Other"]
    assert by_phone[1]["open_leads"] == 1 and by_phone[1]["client_id"] is None

    both = client.get("/v1/admin/contacts", headers=auth,
                      params={"email": "jane@example.com", "phone": "+61412345678"}).json()
    assert both[0]["matched_on"] == ["email", "phone"]
    assert client.get("/v1/admin/contacts", headers=auth, params={"email": "nobody@example.com"}).json() == []
    assert client.get("/v1/admin/contacts", headers=auth).status_code == 422


def test_clients_tag_filter(client, auth):
    for name, tags in (("Gym", ["fitness"]), ("Salon", ["beauty"])):
        lead = make_lead(client, auth, name, f"{name.lower()}@example.com", tags=tags)
        client.post(f"/v1/admin/leads/{lead['id']}/convert", headers=auth, json={"title": "Site"})
    res = client.get("/v1/admin/clients", headers=auth, params={"tag": "fitness"}).json()
    assert [c["name"] for c in res] == ["Gym"]
