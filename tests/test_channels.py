from .conftest import ADMIN, H, mk_tenant


def add(client, tid, **kw):
    body = {"channel_type": "whatsapp", "display_name": "Main", **kw}
    return client.post(f"/admin/tenants/{tid}/channels", headers=H(ADMIN), json=body)


def test_messaging_channel_limit_enforced_but_web_chat_is_free(client, cw):
    tid = mk_tenant(client, cw, plan="starter")             # 1 messaging channel
    assert add(client, tid, external_id="pn-1").status_code == 200
    r = add(client, tid, channel_type="instagram", external_id="ig-1")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "entitlement_exceeded"
    assert add(client, tid, channel_type="web_chat", display_name="Widget").status_code == 200
    assert client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()["channels"] == {"used": 1, "limit": 1}


def test_removing_a_channel_frees_the_slot_and_the_identifier(client, cw):
    tid = mk_tenant(client, cw, plan="starter")
    cid = add(client, tid, external_id="pn-1").json()["id"]
    assert client.delete(f"/admin/tenants/{tid}/channels/{cid}", headers=H(ADMIN)).status_code == 200
    assert add(client, tid, external_id="pn-1").status_code == 200


def test_same_phone_number_cannot_belong_to_two_tenants(client, cw):
    a = mk_tenant(client, cw, service="sa", name="A")
    b = mk_tenant(client, cw, service="sb", name="B")
    assert add(client, a, external_id="pn-9", phone_number="+2348000000000").status_code == 200
    r = add(client, b, external_id="pn-9")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "channel_conflict"


def test_same_inbox_not_registered_twice_for_one_tenant(client, cw):
    tid = mk_tenant(client, cw)
    assert add(client, tid, chatwoot_inbox_id=5).status_code == 200
    assert add(client, tid, channel_type="instagram", chatwoot_inbox_id=5).status_code == 409


def test_closed_tenant_cannot_add_channels(client, cw):
    tid = mk_tenant(client, cw)
    client.post(f"/admin/tenants/{tid}/cancel", headers=H(ADMIN))
    from .conftest import run_jobs; run_jobs(cw)
    assert add(client, tid, external_id="pn-3").status_code == 409


def test_extra_channel_addon_raises_the_limit(client, cw):
    tid = mk_tenant(client, cw, plan="starter")
    add(client, tid, external_id="a")
    assert add(client, tid, channel_type="messenger", external_id="b").status_code == 409
    client.patch(f"/admin/tenants/{tid}", headers=H(ADMIN), json={"extra_channels": 1})
    assert add(client, tid, channel_type="messenger", external_id="b").status_code == 200
