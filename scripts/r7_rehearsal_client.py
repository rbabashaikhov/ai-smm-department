"""HTTP side of the R7 rehearsal (scripts/r7_mutation_rehearsal.sh).

Drives the Control Center through nginx exactly as a browser would --
sign-in and step-up go to smm-auth, everything else to smm-api -- and
prints one PASS/FAIL line per check. Never prints a response body, a
cookie, a CSRF token or a password. Standard library only.

    python r7_rehearsal_client.py <phase> <base_url> <email> <password_file> <state_file>

Phases:

  readonly       mutations off: sign-in works, every business write is 403
                 MUTATIONS_DISABLED
  smoke          mutations on -- the PRODUCTION smoke (docs/reports/smm-025d/r7/
                 runbook.md, R7.7): refuses to run in a project with legacy
                 publishing; account, unverified credential and its
                 revocation, draft -> revision -> submit -> reject, an approval
                 WITHOUT step-up is 403 APPROVAL_CONFIRMATION_REQUIRED, CSRF;
                 leaves both items rejected and the account disabled.
                 Schedules nothing, creates no publication
  approve-path   REHEARSAL ONLY (refuses any project but "rehearsal"): step-up,
                 approve, materialize and schedule two new items, so the worker
                 part of the rehearsal has something to look at
  after-disable  mutations off again: business writes 403 MUTATIONS_DISABLED,
                 reads still work

The state file (JSON, in a private work directory) carries ids from one
phase to the next. The project is $R7_PROJECT (default "rehearsal").
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT = os.environ.get("R7_PROJECT", "rehearsal")
FORBIDDEN = {
    "ciphertext", "nonce", "ephemeral_public_key", "access_token", "token",
    "private_key", "password", "password_hash", "token_hash", "secret",
}

phase, base, email, password_file, state_file = sys.argv[1:6]
password = Path(password_file).read_text(encoding="utf-8")
state_path = Path(state_file)
state = json.loads(state_path.read_text()) if state_path.exists() else {}
failures: list[str] = []
passed = 0


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None

    def call(self, method: str, path: str, body=None, *, csrf: bool = True):
        headers = {"Content-Type": "application/json"}
        if csrf and self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload

    def login(self) -> None:
        status, payload = self.call("POST", "/api/v1/auth/login",
                                    {"email": email, "password": password}, csrf=False)
        check("sign-in (smm-auth)", status == 200, f"status={status}")
        if status != 200:
            finish()
        self.csrf = payload["csrf_token"]

    def logout(self) -> None:
        status, _ = self.call("POST", "/api/v1/auth/logout", {})
        check("sign-out (smm-auth)", status == 204, f"status={status}")


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}  {detail}")


def code(payload) -> str | None:
    return ((payload or {}).get("error") or {}).get("code")


def secret_keys(value, found: list[str]) -> list[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            if key.lower() in FORBIDDEN:
                found.append(key)
            secret_keys(inner, found)
    elif isinstance(value, list):
        for inner in value:
            secret_keys(inner, found)
    return found


def finish() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(f"\n{phase}: {passed} passed, {len(failures)} failed")
    sys.exit(1 if failures else 0)


def mutations_flag(c: Client):
    status, me = c.call("GET", "/api/v1/auth/me")
    return status, ((me or {}).get("control_plane") or {}).get("mutations_enabled")


def new_item(c: Client, title: str, body: str, account: str | None = None) -> dict:
    """A legacy project takes no destination; any other needs an account of it."""

    revision = {"body": body, "format": "text"}
    if account:
        revision["social_account_id"] = account
    status, item = c.call("POST", f"/api/v1/projects/{PROJECT}/content-items",
                          {"title": title, "content_type": "post", "revision": revision})
    check(f"create content item '{title}' -> 201", status == 201, f"status={status} code={code(item)}")
    return item or {}


def submit(c: Client, item: dict) -> None:
    status, payload = c.call("POST", f"/api/v1/content-items/{item['id']}/submit-review",
                             {"revision_id": item["current_revision_id"]})
    check(f"submit-review '{item['title']}' -> 200", status == 200, f"status={status} code={code(payload)}")


c = Client()

if phase in ("readonly", "after-disable"):
    c.login()
    status, flag = mutations_flag(c)
    check("me: mutations_enabled is false", status == 200 and flag is False, f"status={status} flag={flag}")
    status, payload = c.call("GET", f"/api/v1/projects/{PROJECT}/content-items")
    check("reads work (content-items list)", status == 200, f"status={status}")
    for method, path, body in (
        ("POST", f"/api/v1/projects/{PROJECT}/content-items",
         {"title": "x", "revision": {"body": "x"}}),
        ("POST", f"/api/v1/projects/{PROJECT}/social-accounts", {"display_name": "x"}),
        ("POST", "/api/v1/publications/1/schedule",
         {"scheduled_at": datetime.now(timezone.utc).isoformat()}),
    ):
        status, payload = c.call(method, path, body)
        check(f"{method} {path} -> 403 MUTATIONS_DISABLED",
              status == 403 and code(payload) == "MUTATIONS_DISABLED", f"status={status} code={code(payload)}")
    c.logout()

elif phase == "smoke":
    c.login()
    status, flag = mutations_flag(c)
    check("me: mutations_enabled is true", status == 200 and flag is True, f"status={status} flag={flag}")
    # The smoke's own project: created here by the owner (the creator
    # becomes its owner), never one with live content. Re-running reuses it.
    status, project = c.call("GET", f"/api/v1/projects/{PROJECT}")
    if status == 404:
        status, project = c.call("POST", "/api/v1/projects",
                                 {"id": PROJECT, "display_name": "R7 smoke (no publishing)",
                                  "description": "Created by the R7 smoke; nothing here is ever scheduled."})
        check(f"create project '{PROJECT}' -> 201", status == 201, f"status={status} code={code(project)}")
        status, project = c.call("GET", f"/api/v1/projects/{PROJECT}")
    # Safety first: with a LIVE worker, a legacy-publishing project is one
    # mistaken schedule away from a real post. The smoke never runs there.
    legacy = (project or {}).get("legacy_publishing")
    check(f"project '{PROJECT}' exists and has no legacy publishing", status == 200 and legacy is False,
          f"status={status} legacy_publishing={legacy}")
    if failures:
        c.logout()
        finish()

    # CSRF: a business write with the session cookie but without the header.
    status, payload = c.call("POST", f"/api/v1/projects/{PROJECT}/content-items",
                             {"title": "x", "revision": {"body": "x"}}, csrf=False)
    check("business write without CSRF -> 403", status == 403, f"status={status} code={code(payload)}")

    # Account + an UNVERIFIED credential (no platform call), then revoke it.
    status, account = c.call("POST", f"/api/v1/projects/{PROJECT}/social-accounts",
                             {"platform": "threads", "display_name": "R7 rehearsal account"})
    check("create social account -> 201", status == 201, f"status={status} code={code(account)}")
    status, credential = c.call("POST", f"/api/v1/social-accounts/{account['id']}/credentials",
                                {"access_token": "r7-rehearsal-not-a-real-token", "verify": False})
    check("store credential (verify=false) -> 201, active, unverified",
          status == 201 and credential["status"] == "active"
          and credential["verification_status"] == "unverified",
          f"status={status} code={code(credential)}")
    check("credential response carries no key material", not secret_keys(credential, []),
          f"keys={secret_keys(credential, [])}")
    state["key_id"] = (credential or {}).get("key_id")
    status, revoked = c.call("POST", f"/api/v1/social-account-credentials/{credential['id']}/revoke", {})
    check("revoke credential -> 200 revoked", status == 200 and revoked["status"] == "revoked",
          f"status={status} code={code(revoked)}")
    status, detail = c.call("GET", f"/api/v1/social-accounts/{account['id']}")
    check("account detail carries no key material", status == 200 and not secret_keys(detail, []),
          f"status={status}")
    state["account_id"] = account["id"]

    # Draft -> new revision -> submit -> reject. The destination is the test
    # account: active, but with no verified credential it can never be routed.
    item = new_item(c, "R7 draft to reject", "Rehearsal text, will be rejected.", account["id"])
    status, revision = c.call("POST", f"/api/v1/content-items/{item['id']}/revisions",
                              {"body": "Rehearsal text, second revision.", "format": "text",
                               "social_account_id": account["id"],
                               "expected_item_version": item["version"]})
    check("new revision -> 201", status == 201, f"status={status} code={code(revision)}")
    status, item = c.call("GET", f"/api/v1/content-items/{item['id']}")
    submit(c, item)
    status, payload = c.call("POST", f"/api/v1/content-items/{item['id']}/reject",
                             {"revision_id": item["current_revision_id"]})
    check("reject -> 200 (needs no step-up)", status == 200, f"status={status} code={code(payload)}")

    # Approval WITHOUT step-up: refused by the database guard, nothing written.
    item2 = new_item(c, "R7 approval without step-up", "Rehearsal text that must not be approved.",
                     account["id"])
    submit(c, item2)
    status, payload = c.call("POST", f"/api/v1/content-items/{item2['id']}/approve",
                             {"revision_id": item2["current_revision_id"]})
    check("approve without step-up -> 403 APPROVAL_CONFIRMATION_REQUIRED",
          status == 403 and code(payload) == "APPROVAL_CONFIRMATION_REQUIRED",
          f"status={status} code={code(payload)}")
    status, after = c.call("GET", f"/api/v1/content-items/{item2['id']}")
    check("the item is still under review", status == 200 and after["status"] == "in_review",
          f"status={status} item={after.get('status') if after else None}")
    status, pubs = c.call("GET", f"/api/v1/projects/{PROJECT}/publications")
    check("no publication exists", status == 200 and pubs.get("total", len(pubs.get("items", []))) == 0,
          f"status={status} total={pubs.get('total') if pubs else None}")
    # Leave a clean state: both items rejected, the test account disabled.
    status, payload = c.call("POST", f"/api/v1/content-items/{item2['id']}/reject",
                             {"revision_id": item2["current_revision_id"]})
    check("reject the refused item -> 200", status == 200, f"status={status} code={code(payload)}")
    status, acc = c.call("GET", f"/api/v1/social-accounts/{account['id']}")
    status, payload = c.call("POST", f"/api/v1/social-accounts/{account['id']}/disable",
                             {"expected_version": acc["version"]})
    check("disable the test account -> 200", status == 200, f"status={status} code={code(payload)}")
    c.logout()

elif phase == "approve-path":
    if PROJECT != "rehearsal":
        sys.exit("approve-path is for the disposable rehearsal only")
    c.login()
    when = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    state["publications"] = []
    for title in ("R7 approved and kept", "R7 approved, to tamper"):
        item = new_item(c, title, "Rehearsal text that the owner approves.")
        submit(c, item)
        revision_id = item["current_revision_id"]
        status, preview = c.call("GET", f"/api/v1/auth/step-up/approvals/{revision_id}")
        check(f"step-up preview (smm-auth) for '{item['title']}' -> 200", status == 200, f"status={status}")
        status, payload = c.call("POST", "/api/v1/auth/step-up/approvals",
                                 {"revision_id": revision_id, "password": "not-the-password"})
        check("step-up with a wrong password -> 401 STEP_UP_FAILED",
              status == 401 and code(payload) == "STEP_UP_FAILED", f"status={status} code={code(payload)}")
        status, payload = c.call("POST", "/api/v1/auth/step-up/approvals",
                                 {"revision_id": revision_id, "password": password})
        check("step-up with the password -> 201", status == 201, f"status={status} code={code(payload)}")
        status, approved = c.call("POST", f"/api/v1/content-items/{item['id']}/approve",
                                  {"revision_id": revision_id})
        check(f"approve '{item['title']}' -> 200", status == 200, f"status={status} code={code(approved)}")
        status, payload = c.call("POST", f"/api/v1/content-items/{item['id']}/approve",
                                 {"revision_id": revision_id})
        check("a second approval with the same (consumed) grant is refused", status in (403, 409),
              f"status={status} code={code(payload)}")
        status, materialized = c.call("POST", f"/api/v1/content-items/{item['id']}/materialize",
                                      {"revision_id": revision_id})
        check("materialize -> 201 approved", status == 201
              and materialized["publication"]["status"] == "approved", f"status={status} code={code(materialized)}")
        publication = materialized["publication"]["id"]
        status, scheduled = c.call("POST", f"/api/v1/publications/{publication}/schedule",
                                   {"scheduled_at": when})
        check("schedule (due now) -> scheduled", status == 200 and scheduled["status"] == "scheduled",
              f"status={status} code={code(scheduled)}")
        state["publications"].append(publication)
    c.logout()

else:
    sys.exit(f"unknown phase {phase}")

finish()
