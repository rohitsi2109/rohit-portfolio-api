from datetime import date, timedelta

from sqlalchemy import select

from app.models import Contact, Lead
from tests.conftest import booking_body


def make_lead(client, auth, name, email, **extra):
    res = client.post("/v1/admin/leads", headers=auth,
                      json={"name": name, "email": email, "source_channel": "linkedin", **extra})
    assert res.status_code == 201, res.text
    return res.json()["lead"]


def test_lead_list_is_open_leads_hot_first(client, auth):
    cold = make_lead(client, auth, "Cold", "cold@example.com", temperature="cold")
    hot = make_lead(client, auth, "Hot", "hot@example.com", temperature="hot")
    unrated = make_lead(client, auth, "Unrated", "u@example.com")
    spam = make_lead(client, auth, "Spam", "spam@example.com", temperature="hot")
    client.patch(f"/v1/admin/leads/{spam['id']}", headers=auth, json={"status": "spam"})

    ids = [i["id"] for i in client.get("/v1/admin/leads", headers=auth).json()["items"]]
    assert ids == [hot["id"], cold["id"], unrated["id"]]

    page1 = client.get("/v1/admin/leads", headers=auth, params={"limit": 2}).json()
    page2 = client.get("/v1/admin/leads", headers=auth, params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [i["id"] for i in page1["items"] + page2["items"]] == ids
    assert page2["next_cursor"] is None

    only_spam = client.get("/v1/admin/leads", headers=auth, params={"status": "spam"}).json()["items"]
    assert [i["id"] for i in only_spam] == [spam["id"]]
    found = client.get("/v1/admin/leads", headers=auth, params={"q": "hot@"}).json()["items"]
    assert [i["id"] for i in found] == [hot["id"]]
    no_rating = client.get("/v1/admin/leads", headers=auth, params={"temperature": "unrated"}).json()["items"]
    assert [i["id"] for i in no_rating] == [unrated["id"]]
    mixed = client.get("/v1/admin/leads", headers=auth, params={"temperature": ["cold", "unrated"]}).json()["items"]
    assert [i["id"] for i in mixed] == [cold["id"], unrated["id"]]


def test_manual_lead_warns_about_existing_contacts(client, auth, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    res = client.post("/v1/admin/leads", headers=auth, json={
        "name": "Jane again", "email": "jane@example.com", "phone_e164": "+61412345678", "source_channel": "whatsapp"})
    body = res.json()
    assert len(body["warnings"]) == 1 and "already exists" in body["warnings"][0]
    assert body["lead"]["type"] == "manual" and body["lead"]["consent_at"] is None


def test_patch_lead(client, auth, db):
    lead = make_lead(client, auth, "Pat", "pat@example.com")
    res = client.patch(f"/v1/admin/leads/{lead['id']}", headers=auth,
                       json={"status": "contacted", "temperature": "warm", "tags": ["ecommerce", " urgent "]})
    body = res.json()
    assert body["status"] == "contacted" and body["first_contacted_at"] is not None
    assert body["tags"] == ["ecommerce", "urgent"]
    assert {e["type"] for e in body["events"]} >= {"status_changed", "temperature_changed", "tags_changed"}

    assert client.patch(f"/v1/admin/leads/{lead['id']}", headers=auth, json={"status": "won"}).status_code == 409
    assert client.patch(f"/v1/admin/leads/{lead['id']}", headers=auth,
                        json={"lost_reason": "price"}).status_code == 422
    res = client.patch(f"/v1/admin/leads/{lead['id']}", headers=auth,
                       json={"status": "lost", "lost_reason": "price", "lost_comment": "Too expensive"})
    assert res.json()["lost_reason"] == "price"
    res = client.patch(f"/v1/admin/leads/{lead['id']}", headers=auth, json={"status": "qualified"})
    assert res.json()["lost_reason"] is None


def test_proposals(client, auth):
    lead = make_lead(client, auth, "Quote", "q@example.com")
    p = client.post(f"/v1/admin/leads/{lead['id']}/proposals", headers=auth,
                    json={"title": "Website", "amount": "4500", "currency": "AUD", "status": "sent"}).json()
    assert p["status"] == "sent" and p["sent_at"] is not None
    p = client.patch(f"/v1/admin/proposals/{p['id']}", headers=auth, json={"status": "accepted"}).json()
    assert p["status"] == "accepted" and p["responded_at"] is not None
    events = client.get(f"/v1/admin/leads/{lead['id']}", headers=auth).json()["events"]
    assert {"proposal_sent", "proposal_accepted"} <= {e["type"] for e in events}


def test_convert_payments_and_client_summary(client, auth):
    lead = make_lead(client, auth, "Priya", "priya@example.com", phone_country="SG")
    res = client.post(f"/v1/admin/leads/{lead['id']}/convert", headers=auth,
                      json={"title": "Business website", "value": "8500", "currency": "AUD", "company": "Royal Touchup"})
    assert res.status_code == 201, res.text
    conv = res.json()
    assert conv["client_created"] is True
    assert client.post(f"/v1/admin/leads/{lead['id']}/convert", headers=auth,
                       json={"title": "Again"}).status_code == 409

    # A second lead from the same person reuses the client
    lead2 = client.post("/v1/admin/leads", headers=auth, json={
        "name": "Priya", "email": "priya@example.com", "source_channel": "referral"}).json()["lead"]
    conv2 = client.post(f"/v1/admin/leads/{lead2['id']}/convert", headers=auth,
                        json={"title": "Mobile app", "value": "3000", "currency": "SGD"}).json()
    assert conv2["client_id"] == conv["client_id"] and conv2["client_created"] is False

    project_id = conv["project_id"]
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    deposit = client.post(f"/v1/admin/projects/{project_id}/payments", headers=auth,
                          json={"description": "50% deposit", "amount": "4250", "currency": "AUD"}).json()
    client.post(f"/v1/admin/projects/{project_id}/payments", headers=auth,
                json={"description": "Final", "amount": "4250", "currency": "AUD", "due_on": yesterday})
    paid = client.patch(f"/v1/admin/payments/{deposit['id']}", headers=auth,
                        json={"status": "paid", "method": "Wise"}).json()
    assert paid["paid_on"] == date.today().isoformat()

    overdue = client.get("/v1/admin/payments", headers=auth, params={"state": "overdue"}).json()
    assert [p["description"] for p in overdue] == ["Final"] and overdue[0]["client_name"] == "Priya"
    received = client.get("/v1/admin/payments", headers=auth, params={"state": "received"}).json()
    assert [p["description"] for p in received] == ["50% deposit"] and received[0]["is_overdue"] is False

    [summary] = client.get("/v1/admin/clients", headers=auth).json()
    assert summary["company"] == "Royal Touchup" and summary["country"] == "SG"
    assert summary["project_count"] == 2
    assert float(summary["contracted_value"]["AUD"]) == 8500 and float(summary["contracted_value"]["SGD"]) == 3000
    assert float(summary["total_paid"]["AUD"]) == 4250 and float(summary["overdue"]["AUD"]) == 4250

    detail = client.get(f"/v1/admin/clients/{conv['client_id']}", headers=auth).json()
    assert len(detail["projects"]) == 2 and len(detail["leads"]) == 2

    # Leaving 'won' is allowed; the client and project stay
    res = client.patch(f"/v1/admin/leads/{lead['id']}", headers=auth, json={"status": "qualified"})
    assert res.json()["converted_at"] is None
    assert client.get(f"/v1/admin/clients/{conv['client_id']}", headers=auth).status_code == 200


def test_erasure(client, auth, db):
    keep = make_lead(client, auth, "Client", "client@example.com")
    client.post(f"/v1/admin/leads/{keep['id']}/convert", headers=auth, json={"title": "Site"})
    gone = make_lead(client, auth, "Gone", "gone@example.com")
    client.post(f"/v1/admin/leads/{gone['id']}/notes", headers=auth, json={"body": "Called, not interested"})

    res = client.delete(f"/v1/admin/contacts/{keep['contact']['id']}", headers=auth)
    assert res.status_code == 409
    assert client.delete(f"/v1/admin/contacts/{gone['contact']['id']}", headers=auth).status_code == 204
    assert db.scalar(select(Contact).where(Contact.email == "gone@example.com")) is None
    assert db.get(Lead, gone["id"]) is None


def test_contacted_and_stats(client, auth, fake_cal):
    client.post("/v1/bookings", json=booking_body())
    lead = client.get("/v1/admin/leads", headers=auth).json()["items"][0]
    res = client.post(f"/v1/admin/leads/{lead['id']}/contacted", headers=auth, json={"channel": "whatsapp"})
    assert res.json()["first_contacted_at"] is not None

    stats = client.get("/v1/admin/stats", headers=auth).json()
    assert stats["total_leads"] == 1
    assert stats["leads_by_channel"] == {"website": 1}
    assert stats["leads_by_source"] == {"linkedin": 1}
    assert stats["avg_response_seconds"] is not None
    assert stats["conversion_rate"] == 0


def test_register_device(client, auth):
    for _ in range(2):
        assert client.post("/v1/admin/devices", headers=auth, json={"fcm_token": "tok-1"}).status_code == 204
