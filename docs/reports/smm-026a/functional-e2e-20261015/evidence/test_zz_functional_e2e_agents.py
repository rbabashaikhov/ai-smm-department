"""Functional E2E, phase B (local, disposable DB, NOT part of the repo).

Takes the real agent output of phase A (E2E_AGENT_RESULT, a JSON written by
scratchpad/e2e_agents.py) and drives it through the 2.0 product exactly as
packaged: smm-auth (ai_smm_auth) + smm-api with mutations ON (ai_smm_cc_rw)
+ the real worker claim/publish service with FakePublisher. No external
network: every non-loopback connect() raises. Evidence -> E2E_EVIDENCE.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from ai_smm.application.content import RevisionInput, approve_revision, create_content_item
from ai_smm.application.auth import SESSION_COOKIE_NAME
from ai_smm.db.models import AuditLog, MembershipRole, Publication, PublicationStatus, RevisionSource, User
from ai_smm.publishing.routing import build_opener
from ai_smm.publishing.service import publish_claimed_publication
from ai_smm.queue import claim_due_publication
from tests.conftest import TEST_PASSWORD, fake_token
from tests.test_db_auth_contracts import AuthStack, auth_stack  # noqa: F401
from tests.test_e2e_owner_workflow import api_client, auth_client  # noqa: F401
from tests.test_publish_routing import AccountPublishers, LegacyFactory

PROJECT = "ai-catalog-consultant"
ASSETS = f"knowledge/projects/{PROJECT}/assets/"
PILOT_MEDIA = [
    ["telegram-01-recommendation.jpg", "telegram-02-budget.jpg"],
    ["telegram-03-comparison.jpg"],
    ["telegram-04-tradeoff.jpg"],
]

pytestmark = pytest.mark.skipif(
    not os.environ.get("E2E_AGENT_RESULT"), reason="phase-B functional E2E only"
)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise AssertionError(f"external network call blocked: {address!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def owner(make_user, make_membership, project: str) -> User:
    user = make_user()
    make_membership(user=user, project_id=project, role=MembershipRole.OWNER)
    return user


def test_functional_e2e_agents_to_fake_publish(
    auth_client, api_client, owner, session, account_settings, identity_service, make_user, make_membership
):
    agent = json.loads(Path(os.environ["E2E_AGENT_RESULT"]).read_text())
    copy_steps = [s for s in agent["steps"] if s["node"] == "copywriter"]
    edit_steps = [s for s in agent["steps"] if s["node"] == "editor"]
    draft = copy_steps[-1]["output"]["draft"]
    review = edit_steps[-1]["output"]
    plan = agent["steps"][0]["output"]["content_plan"]
    ev: dict = {"agent_run": os.environ["E2E_AGENT_RESULT"], "checks": []}

    def check(name, ok, detail=""):
        ev["checks"].append({"check": name, "pass": bool(ok), "detail": detail})
        assert ok, f"{name}: {detail}"

    def call(method, client, url, status=None, **kw):
        r = getattr(client, method)(url, **kw)
        if status is not None:
            assert r.status_code == status, f"{method} {url}: {r.status_code} {r.text}"
        return r

    # 1. Owner signs in at smm-auth.
    login = call("post", auth_client, "/api/v1/auth/login", 200, json={"email": owner.email, "password": TEST_PASSWORD}).json()
    api_client.cookies.set(SESSION_COOKIE_NAME, auth_client.cookies.get(SESSION_COOKIE_NAME))
    csrf = {"X-CSRF-Token": login["csrf_token"]}

    # 2. Project + two Threads accounts (fake tokens, FakeIdentityService).
    call("post", api_client, "/api/v1/projects", 201, headers=csrf, json={
        "id": PROJECT, "display_name": "AI Catalog Consultant",
        "default_timezone": "Europe/Moscow", "default_language": "ru"})
    accounts = {}
    for name, ext in (("Основной", "3001"), ("Резервный", "3002")):
        acc = call("post", api_client, f"/api/v1/projects/{PROJECT}/social-accounts", 201, headers=csrf,
                   json={"platform": "threads", "display_name": name}).json()
        tok = fake_token(name)
        identity_service.register(tok, ext, f"e2e_{ext}")
        call("post", api_client, f"/api/v1/social-accounts/{acc['id']}/credentials", 201, headers=csrf, json={"access_token": tok})
        accounts[name] = (acc["id"], tok)
    main_id, main_tok = accounts["Основной"]
    ready = call("get", api_client, f"/api/v1/social-accounts/{main_id}", 200).json()
    check("account publishing_ready after verified credential", ready["publishing_ready"], ready.get("external_username"))

    # 3. Bridge: the agent package -> 3 content items, source=copywriter.
    items = []
    for idx, pub in enumerate(draft["publications"]):
        imgs = [{"path": ASSETS + n, "alt_text": "AI Catalog Consultant — " + n[:-4].replace("-", " ")} for n in PILOT_MEDIA[idx]]
        body = "\n\n".join(i["text"].strip() for i in pub["items"])
        created = call("post", api_client, f"/api/v1/projects/{PROJECT}/content-items", 201, headers=csrf, json={
            "title": pub["title"], "content_type": "post",
            "revision": {
                "title": pub["title"], "body": body,
                "format": "carousel" if len(imgs) > 1 else "image", "images": imgs,
                "metadata": {"series_title": plan.get("series_title"), "position": pub["position"],
                             "series_total": len(draft["publications"]),
                             "publishing_strategy": plan.get("publishing_strategy"),
                             "editor_approved": review["editor_approved"]},
                "source": "copywriter", "source_ref": f"e2e:{Path(os.environ['E2E_AGENT_RESULT']).stem}:v{len(copy_steps)}",
                "editor_score": review["editor_score"], "editor_notes": review["editor_feedback"][:5000],
                "social_account_id": main_id}}).json()
        items.append(created)
    listed = call("get", api_client, f"/api/v1/projects/{PROJECT}/content-items", 200).json()
    check("3 drafts stored and listed", listed["total"] == 3 and all(i["status"] == "draft" for i in listed["items"]),
          [i["status"] for i in listed["items"]])
    det = call("get", api_client, f"/api/v1/content-items/{items[0]['id']}", 200).json()
    check("revision keeps agent provenance + editor score/notes",
          det["current_revision"]["source"] == "copywriter" and det["current_revision"]["editor_score"] == review["editor_score"],
          {"source": det["current_revision"]["source"], "editor_score": det["current_revision"]["editor_score"]})
    ev["editor_approved_by_agent"] = review["editor_approved"]

    # 3b. An agent (no user) writing directly through the application layer, as the worker role.
    agent_item, _ = create_content_item(session, project_id=PROJECT, title="agent direct write", content_type="post",
        revision=RevisionInput(body="Черновик агента без пользователя.", format="text", source=RevisionSource.COPYWRITER,
                               social_account_id=uuid.UUID(main_id)), created_by_user=None)
    session.commit()
    check("agent may write a draft with no user (owner role)", agent_item.created_by_user_id is None, str(agent_item.id))
    try:
        approve_revision(session, content_item_id=agent_item.id, expected_project_id=PROJECT,
                         revision_id=agent_item.current_revision_id, actor_user=None)
        session.commit()
        agent_approved = True
    except Exception as exc:
        session.rollback()
        agent_approved = False
        ev["agent_approve_refusal"] = f"{type(exc).__name__}: {exc}"
    check("an agent (no user) cannot approve", not agent_approved, ev.get("agent_approve_refusal", ""))

    # 4. Gates before approval.
    a = items[0]
    rid = a["current_revision_id"]
    r = call("post", api_client, f"/api/v1/content-items/{a['id']}/materialize", headers=csrf, json={"revision_id": rid})
    check("materialize of an unapproved draft refused", r.status_code in (409, 422), f"{r.status_code} {r.json()['error']['code']}")
    r = call("post", api_client, f"/api/v1/content-items/{a['id']}/approve", headers=csrf, json={"revision_id": rid})
    check("approve of a draft (not in review) refused", r.status_code in (403, 409), f"{r.status_code} {r.json()['error']['code']}")
    call("post", api_client, f"/api/v1/content-items/{a['id']}/submit-review", 200, headers=csrf, json={"revision_id": rid})
    r = call("post", api_client, f"/api/v1/content-items/{a['id']}/approve", headers=csrf, json={"revision_id": rid})
    check("approve without step-up password refused", r.status_code == 403 and r.json()["error"]["code"] == "APPROVAL_CONFIRMATION_REQUIRED",
          f"{r.status_code} {r.json()['error']['code']}")
    pubs_before = session.scalar(text("SELECT count(*) FROM publications WHERE project_id = :p"), {"p": PROJECT})
    check("no publication row exists before approval", pubs_before == 0, pubs_before)

    # A viewer cannot approve even with the step-up.
    viewer = make_user()
    make_membership(user=viewer, project_id=PROJECT, role=MembershipRole.VIEWER)

    # 5. Human edit of part 3 (owner shortens it): new revision, back to draft.
    c = items[2]
    cur = call("get", api_client, f"/api/v1/content-items/{c['id']}", 200).json()
    edited_body = cur["current_revision"]["body"].replace("Ограничения задокументированы, строгая", "Строгая")
    rev2 = call("post", api_client, f"/api/v1/content-items/{c['id']}/revisions", 201, headers=csrf, json={
        "expected_item_version": cur["version"], "title": cur["current_revision"]["title"], "body": edited_body,
        "format": cur["current_revision"]["format"], "images": cur["current_revision"]["images"],
        "metadata": cur["current_revision"]["metadata"], "social_account_id": main_id,
        "editor_score": cur["current_revision"]["editor_score"], "editor_notes": "правка владельца"}).json()
    revs = call("get", api_client, f"/api/v1/content-items/{c['id']}/revisions", 200).json()
    check("human edit = new immutable revision, source human", revs["total"] == 2 and rev2["revision"]["source"] == "human",
          [(x["revision_number"], x["source"]) for x in revs["items"]])

    # 6. Approve all three with step-up, materialize.
    publication_ids = []
    for it in items:
        d = call("get", api_client, f"/api/v1/content-items/{it['id']}", 200).json()
        rv = d["current_revision_id"]
        if d["status"] == "draft":
            call("post", api_client, f"/api/v1/content-items/{it['id']}/submit-review", 200, headers=csrf, json={"revision_id": rv})
        prev = call("get", auth_client, f"/api/v1/auth/step-up/approvals/{rv}", 200).json()
        check(f"step-up preview shows exact text+account ({it['title'][:30]})",
              prev["body"] == d["current_revision"]["body"] and prev["social_account_id"] == main_id, prev.get("account_external_username"))
        call("post", auth_client, "/api/v1/auth/step-up/approvals", 201, headers=csrf, json={"revision_id": rv, "password": TEST_PASSWORD})
        ap = call("post", api_client, f"/api/v1/content-items/{it['id']}/approve", 200, headers=csrf,
                  json={"revision_id": rv, "note": "E2E: одобрено владельцем"}).json()
        check("approved by the human owner", ap["approval"]["actor_user_id"] == str(owner.id), ap["item"]["status"])
        m = call("post", api_client, f"/api/v1/content-items/{it['id']}/materialize", 201, headers=csrf, json={"revision_id": rv}).json()
        check("materialized publication = approved, human_reviewed, not scheduled",
              m["publication"]["status"] == "approved" and m["publication"]["human_reviewed"] and m["publication"]["scheduled_at"] is None,
              {k: m["publication"][k] for k in ("id", "status", "format", "human_reviewed")})
        publication_ids.append(m["publication"]["id"])

    # Worker before scheduling: nothing is due.
    check("approved-but-unscheduled is never claimed",
          claim_due_publication(session, worker_id=account_settings.worker_id, lease_seconds=900) is None)
    session.rollback()

    # 7. Scheduling: #1 due now, #2 tomorrow, #3 the day after; then cancel #3.
    now = datetime.now(timezone.utc)
    for pid, when in zip(publication_ids, (now - timedelta(minutes=1), now + timedelta(days=1), now + timedelta(days=2))):
        s = call("post", api_client, f"/api/v1/publications/{pid}/schedule", 200, headers=csrf, json={"scheduled_at": when.isoformat()}).json()
        check(f"publication {pid} scheduled", s["status"] == "scheduled", s["scheduled_at"])
    pv = call("get", api_client, f"/api/v1/publications/{publication_ids[0]}/preview", 200).json()
    ev["preview_1"] = {k: pv.get(k) for k in ("format", "ready", "blocked_reason", "image_count", "series_ready", "series_reason") if k in pv}

    # 8. Worker with FakePublisher, real claim/publish service.
    legacy, fakes = LegacyFactory(), AccountPublishers()
    worker_settings = replace(account_settings, legacy_threads_projects=())

    def work_once():
        claimed = claim_due_publication(session, worker_id=worker_settings.worker_id, lease_seconds=900)
        if claimed is None:
            session.rollback()
            return None
        session.commit()
        return publish_claimed_publication(session, claimed, settings=worker_settings, publisher_factory=legacy,
            media_root=Path.cwd(), account_publisher_factory=fakes, identity_fetcher=identity_service,
            opener=build_opener(account_settings))

    out1 = work_once()
    check("due publication published via FAKE transport", out1 is not None and out1.status == "published",
          out1 and {"id": out1.publication_id, "status": out1.status, "fake_post_id": out1.threads_post_id})
    check("fake transport got the chosen account's token only, no deployment token",
          fakes.tokens == [main_tok] and legacy.calls == 0, {"calls": fakes.publish_calls})
    check("carousel images went through (fake) upload", len(fakes.publishers[0].uploads) == 2, fakes.publishers[0].uploads)
    check("future publications are not claimed", work_once() is None)

    # reschedule #2 to now, cancel #3.
    call("post", api_client, f"/api/v1/publications/{publication_ids[1]}/reschedule", 200, headers=csrf,
         json={"scheduled_at": (now - timedelta(seconds=30)).isoformat()})
    call("post", api_client, f"/api/v1/publications/{publication_ids[2]}/cancel", 200, headers=csrf, json={"note": "E2E cancel"})
    out2 = work_once()
    check("rescheduled publication published (fake)", out2 is not None and out2.status == "published", out2 and out2.publication_id)
    check("cancelled publication never claimed", work_once() is None)

    # 9. Final state + audit.
    session.expire_all()
    final = {p.id: (p.status.value, p.threads_post_id) for p in session.scalars(select(Publication).where(Publication.project_id == PROJECT))}
    ev["final_publications"] = final
    check("final statuses", [final[i][0] for i in publication_ids] == ["published", "published", "cancelled"], final)
    ev["content_items_final"] = [(i["title"], i["status"]) for i in call("get", api_client, f"/api/v1/projects/{PROJECT}/content-items", 200).json()["items"]]
    ev["audit_actions"] = sorted({row.action for row in session.scalars(select(AuditLog))})
    ev["fake_publish_calls"] = fakes.publish_calls
    Path(os.environ.get("E2E_EVIDENCE", "/dev/null")).write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str))
