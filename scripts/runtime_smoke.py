"""Smoke test of the packaged runtime, through the web origin only.

    uv run python scripts/runtime_smoke.py --base-url http://127.0.0.1:8088
    uv run python scripts/runtime_smoke.py --base-url http://127.0.0.1:8088 \\
        --email owner@local.test --password-file /path/to/file --project demo

Standard library only, read-only except for one login/logout pair. It never
publishes, never schedules, and never talks to anything but --base-url.
With a session it also probes the read-only gate with a cancel of
publication 0, an id that never exists: refused with MUTATIONS_DISABLED
when the API is read-only (the default), 404 when it is not -- nothing is
written either way.
The password is read from a file so it does not appear in a process
listing or the shell history.

Exit code 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


SPA_MARKER = '<div id="root">'
SESSION_COOKIE = "smm_session"


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    set_cookies: list[str]
    body: str

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> object:
        return json.loads(self.body)

    @property
    def is_spa(self) -> bool:
        return "text/html" in self.content_type and SPA_MARKER in self.body

    @property
    def error_code(self) -> str | None:
        if "application/json" not in self.content_type:
            return None

        try:
            return self.json()["error"]["code"]  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass
class Smoke:
    base_url: str
    failures: list[str] = field(default_factory=list)
    passed: int = 0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        data = None
        all_headers = dict(headers or {})

        if body is not None:
            data = json.dumps(body).encode()
            all_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=all_headers
        )

        try:
            response = self._opener.open(req, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc

        with response:
            raw = response.read().decode("utf-8", errors="replace")

            return Reply(
                status=response.status,
                headers={k.lower(): v for k, v in response.headers.items()},
                set_cookies=response.headers.get_all("Set-Cookie") or [],
                body=raw,
            )

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"PASS  {name}")
        else:
            self.failures.append(name)
            print(f"FAIL  {name}  {detail}")


def _cookie_attributes(header: str) -> tuple[str, set[str], dict[str, str]]:
    first, *rest = (part.strip() for part in header.split(";"))
    flags: set[str] = set()
    attrs: dict[str, str] = {}

    for part in rest:
        if "=" in part:
            key, value = part.split("=", 1)
            attrs[key.lower()] = value
        else:
            flags.add(part.lower())

    return first, flags, attrs


def check_static(s: Smoke) -> None:
    root = s.request("GET", "/")
    s.check("GET / serves the SPA", root.status == 200 and root.is_spa,
            f"status={root.status} type={root.content_type}")
    csp = root.headers.get("content-security-policy", "")
    s.check("GET / sends a same-origin CSP", "default-src 'self'" in csp, csp)
    s.check("GET / is not cached",
            root.headers.get("cache-control") == "no-cache",
            root.headers.get("cache-control", ""))

    for path in (
        "/projects/demo/content",
        "/projects",
        "/login",
        "/content/00000000-0000-0000-0000-000000000000",
    ):
        reply = s.request("GET", path)
        s.check(f"GET {path} falls back to index.html",
                reply.status == 200 and reply.is_spa, f"status={reply.status}")

    missing = s.request("GET", "/assets/does-not-exist.js")
    s.check("missing asset is 404, not index.html",
            missing.status == 404 and not missing.is_spa,
            f"status={missing.status}")

    marker = 'src="/assets/'
    if marker in root.body:
        asset = "/assets/" + root.body.split(marker, 1)[1].split('"', 1)[0]
        js = s.request("GET", asset)
        s.check("bundle is served as JavaScript, cached immutable",
                js.status == 200
                and "javascript" in js.content_type
                and "immutable" in js.headers.get("cache-control", ""),
                f"status={js.status} type={js.content_type}")


def check_proxy(s: Smoke) -> None:
    live = s.request("GET", "/health/live")
    s.check("GET /health/live through the proxy",
            live.status == 200 and live.json() == {"status": "live"},
            f"status={live.status} body={live.body[:120]}")

    ready = s.request("GET", "/health/ready")
    s.check("GET /health/ready through the proxy",
            ready.status == 200 and ready.json().get("status") == "ready",  # type: ignore[union-attr]
            f"status={ready.status} body={ready.body[:120]}")

    for path in (
        "/api/v1/not-existing",
        "/api/unknown",
        "/api",
        "/health/not-existing",
        "/health",
    ):
        reply = s.request("GET", path)
        s.check(f"GET {path} is the API's 404 JSON, not the SPA",
                reply.status == 404 and reply.error_code == "NOT_FOUND"
                and not reply.is_spa,
                f"status={reply.status} type={reply.content_type}")

    echoed = s.request("GET", "/health/live",
                       headers={"X-Request-ID": "smoke-request-0001"})
    s.check("X-Request-ID passes through to the API and back",
            echoed.headers.get("x-request-id") == "smoke-request-0001",
            echoed.headers.get("x-request-id", ""))

    generated = s.request("GET", "/health/live")
    s.check("a request id is assigned when the client sends none",
            bool(generated.headers.get("x-request-id")))

    s.check("API responses carry nosniff",
            live.headers.get("x-content-type-options") == "nosniff")
    s.check("no server version is disclosed",
            "/" not in live.headers.get("server", "") and
            "uvicorn" not in live.headers.get("server", "").lower(),
            live.headers.get("server", ""))

    anonymous = s.request("GET", "/api/v1/auth/me")
    s.check("GET /api/v1/auth/me without a session is 401 JSON",
            anonymous.status == 401 and anonymous.error_code == "AUTH_REQUIRED",
            f"status={anonymous.status}")


def check_docs(s: Smoke, expect: str) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        reply = s.request("GET", path)

        if expect == "disabled":
            s.check(f"GET {path} is 404 JSON (docs disabled)",
                    reply.status == 404 and reply.error_code == "NOT_FOUND",
                    f"status={reply.status} type={reply.content_type}")
        else:
            s.check(f"GET {path} is served (docs enabled)",
                    reply.status == 200 and not reply.is_spa,
                    f"status={reply.status}")


def check_session(
    s: Smoke,
    *,
    email: str,
    password: str,
    project: str,
    expect_secure_cookie: bool,
    expect_mutations: str,
) -> None:
    login = s.request("POST", "/api/v1/auth/login",
                      body={"email": email, "password": password})
    s.check("POST /api/v1/auth/login", login.status == 200,
            f"status={login.status} code={login.error_code}")

    if login.status != 200:
        return

    session_headers = [c for c in login.set_cookies
                       if c.startswith(f"{SESSION_COOKIE}=")]
    s.check("login sets the session cookie", len(session_headers) == 1)

    if not session_headers:
        return

    pair, flags, attrs = _cookie_attributes(session_headers[0])
    s.check("session cookie is HttpOnly", "httponly" in flags)
    s.check("session cookie is SameSite=Lax",
            attrs.get("samesite", "").lower() == "lax")
    s.check("session cookie has Path=/", attrs.get("path") == "/")
    s.check(f"session cookie Secure={expect_secure_cookie}",
            ("secure" in flags) is expect_secure_cookie, str(flags))

    csrf = login.json()["csrf_token"]  # type: ignore[index]
    cookie = {"Cookie": pair}

    me = s.request("GET", "/api/v1/auth/me", headers=cookie)
    s.check("GET /api/v1/auth/me with the session", me.status == 200,
            f"status={me.status}")

    projects = s.request("GET", "/api/v1/projects", headers=cookie)
    ids = [p.get("id") for p in projects.json()] if projects.status == 200 else []  # type: ignore[union-attr]
    s.check(f"GET /api/v1/projects lists {project!r}",
            projects.status == 200 and project in ids,
            f"status={projects.status} ids={ids}")

    check_mutation_gate(s, me=me, cookie=cookie, csrf=csrf,
                        expect=expect_mutations)

    no_token = s.request("POST", "/api/v1/auth/logout", headers=cookie)
    s.check("logout without X-CSRF-Token is refused (CSRF intact)",
            no_token.status == 403 and no_token.error_code == "CSRF_REQUIRED",
            f"status={no_token.status} code={no_token.error_code}")

    logout = s.request("POST", "/api/v1/auth/logout",
                       headers={**cookie, "X-CSRF-Token": csrf})
    s.check("POST /api/v1/auth/logout", logout.status == 204,
            f"status={logout.status} code={logout.error_code}")

    after = s.request("GET", "/api/v1/auth/me", headers=cookie)
    s.check("the revoked session no longer authenticates",
            after.status == 401, f"status={after.status}")


def check_mutation_gate(
    s: Smoke,
    *,
    me: Reply,
    cookie: dict[str, str],
    csrf: str,
    expect: str,
) -> None:
    enabled = expect == "enabled"
    reported = None

    if me.status == 200:
        reported = me.json().get("control_plane", {}).get(  # type: ignore[union-attr]
            "mutations_enabled"
        )

    s.check(f"/auth/me reports mutations_enabled={enabled}",
            reported is enabled, f"reported={reported!r}")

    # Publication 0 never exists, so the probe cannot change anything:
    # the gate refuses it first, or -- with mutations on -- it is a 404.
    probe = s.request("POST", "/api/v1/publications/0/cancel",
                      headers={**cookie, "X-CSRF-Token": csrf}, body={})

    if enabled:
        s.check("with mutations on, a command reaches the API (404 for id 0)",
                probe.status == 404 and probe.error_code == "NOT_FOUND",
                f"status={probe.status} code={probe.error_code}")
    else:
        s.check("read-only: a queue command is refused (MUTATIONS_DISABLED)",
                probe.status == 403
                and probe.error_code == "MUTATIONS_DISABLED",
                f"status={probe.status} code={probe.error_code}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--expect-docs", choices=["disabled", "enabled"],
                        default="disabled")
    parser.add_argument("--email")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--project", default="demo")
    parser.add_argument("--expect-secure-cookie", action="store_true",
                        help="Expect Secure on the session cookie (HTTPS).")
    parser.add_argument("--expect-mutations", choices=["disabled", "enabled"],
                        default="disabled",
                        help="AI_SMM_API_MUTATIONS_ENABLED of the target; "
                             "the packaged default is disabled.")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base_url)

    check_static(smoke)
    check_proxy(smoke)
    check_docs(smoke, args.expect_docs)

    if args.email and args.password_file:
        check_session(
            smoke,
            email=args.email,
            password=args.password_file.read_text(encoding="utf-8").strip(),
            project=args.project,
            expect_secure_cookie=args.expect_secure_cookie,
            expect_mutations=args.expect_mutations,
        )
    else:
        print("SKIP  login/logout (no --email/--password-file)")

    print(f"\n{smoke.passed} passed, {len(smoke.failures)} failed")

    return 1 if smoke.failures else 0


if __name__ == "__main__":
    sys.exit(main())
