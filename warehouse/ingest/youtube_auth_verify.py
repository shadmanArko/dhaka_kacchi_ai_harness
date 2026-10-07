"""Prove warehouse.ingest.youtube_auth behaves correctly, WITHOUT Google.

Same idiom as the other *_verify.py scripts: plain ok/FAIL output and an exit
code, not pytest. No database is needed and nothing real is touched: the OAuth
flow runs against a local fake of Google's authorization and token endpoints,
and .env handling runs against temporary files.

This script handles credentials and rewrites a file holding every other secret
in the repo, which is why it is checked this hard rather than trusted.

WHAT IT PROVES: that the flow is right about the documented OAuth 2.0 shapes -
PKCE, the state check, refusal of forged callbacks, the granted-scope check,
and that nothing sensitive leaks into an error message. It cannot prove that
the REAL Google behaves identically; the first real `make youtube-auth` is that
test, and its final probe line says whether the token works.

Run with `make verify-youtube-auth`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from warehouse.config import YouTubeOAuthSettings
from warehouse.ingest import youtube_auth as ya
from warehouse.ingest.youtube_auth import AuthError

CLIENT_ID = "fake-client.apps.googleusercontent.com"
CLIENT_SECRET = "FAKE-CLIENT-SECRET-DO-NOT-LEAK"
SETTINGS = YouTubeOAuthSettings(
    client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=None
)

_failures: list[str] = []
_checks_run = 0


def _check(name: str, problems: list[str]) -> None:
    global _checks_run
    _checks_run += 1
    if problems:
        _failures.append(name)
        print(f"  FAIL  {name}")
        for p in problems:
            print(f"          {p}")
    else:
        print(f"  ok    {name}")


# ---------------------------------------------------------------------------
# A fake Google: token endpoint + reports endpoint
# ---------------------------------------------------------------------------


class _Fake:
    codes: dict[str, str] = {}  # code -> the PKCE challenge it was issued against
    exchanges: list[dict[str, str]] = []
    scope = ya.REQUIRED_SCOPE
    give_refresh = True
    leak_on_error = False
    reports_mode = "ok"  # ok | not_enabled | other


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        _Fake.exchanges.append(form)

        if form.get("client_secret") != CLIENT_SECRET:
            return self._send(401, {"error": "invalid_client"})
        challenge = _Fake.codes.get(form.get("code", ""))
        if challenge is None:
            # Deliberately echoes secret + code, like a careless server could,
            # to prove the helper scrubs them before showing the operator.
            detail = (
                f"bad code {form.get('code')} for secret {CLIENT_SECRET}"
                if _Fake.leak_on_error
                else "bad code"
            )
            return self._send(400, {"error": "invalid_grant", "error_description": detail})
        # The real PKCE check: the verifier sent now must hash to the challenge
        # sent at the start. A helper that mangled either would fail here.
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if expected != challenge:
            return self._send(400, {"error": "invalid_grant", "error_description": "PKCE mismatch"})
        body = {"access_token": "ACCESS-123", "scope": _Fake.scope, "token_type": "Bearer"}
        if _Fake.give_refresh:
            body["refresh_token"] = "1//REFRESH-ABC_def-123"
        self._send(200, body)

    def do_GET(self) -> None:  # noqa: N802
        if _Fake.reports_mode == "ok":
            return self._send(200, {"rows": [[4242]]})
        if _Fake.reports_mode == "not_enabled":
            msg = "YouTube Analytics API has not been used in project 1 before or it is disabled."
            return self._send(403, {"error": {"code": 403, "message": msg}})
        self._send(403, {"error": {"code": 403, "message": "insufficient permissions"}})


def _reset(**over: object) -> None:
    _Fake.codes = {}
    _Fake.exchanges = []
    _Fake.scope = ya.REQUIRED_SCOPE
    _Fake.give_refresh = True
    _Fake.leak_on_error = False
    _Fake.reports_mode = "ok"
    for k, v in over.items():
        setattr(_Fake, k, v)


def _http_get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _browser(mode: str, seen: dict) -> object:
    """Stands in for the user's browser. `mode`: approve | forged_first | deny | silent."""

    def browse(url: str) -> None:
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        seen["url"] = url
        redirect, state = q["redirect_uri"], q["state"]
        if mode == "silent":
            return
        if mode == "forged_first":
            seen["forged_status"] = _http_get(f"{redirect}/?code=EVIL&state=not-the-state")
            _Fake.codes["EVIL"] = q["code_challenge"]  # would exchange fine IF it were accepted
        if mode == "deny":
            _http_get(f"{redirect}/?error=access_denied&state={state}")
            return
        code = f"CODE-{len(_Fake.codes)}"
        _Fake.codes[code] = q["code_challenge"]
        _http_get(f"{redirect}/?code={code}&state={state}")

    return browse


def _run(base: str, mode: str, seen: dict, **kw: object) -> ya.Grant:
    return ya.authorize(
        SETTINGS,
        open_browser=_browser(mode, seen),
        auth_url=f"{base}/auth",
        token_url=f"{base}/token",
        out=lambda _s: None,
        **kw,
    )


def _expect_error(fn: object) -> str | None:
    try:
        fn()  # type: ignore[operator]
    except AuthError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------


def run_checks(base: str) -> int:
    # -- PKCE and the authorization URL ----------------------------------------
    verifier, challenge = ya.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    problems = []
    if challenge != expected.decode():
        problems.append("challenge is not base64url(sha256(verifier))")
    if not (43 <= len(verifier) <= 128):
        problems.append(f"verifier length {len(verifier)} outside RFC 7636's 43-128")
    if "=" in challenge:
        problems.append("challenge carries base64 padding")
    if ya.pkce_pair()[0] == verifier:
        problems.append("two verifiers were identical")
    _check("PKCE verifier/challenge follow RFC 7636 and are random", problems)

    url = ya.build_auth_url(CLIENT_ID, "http://127.0.0.1:1", "S", "C")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
    problems = []
    if q.get("scope") != ya.REQUIRED_SCOPE:
        problems.append(f"scope is {q.get('scope')!r}, must be exactly the read-only one")
    for key, want in {
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge_method": "S256",
        "response_type": "code",
    }.items():
        if q.get(key) != want:
            problems.append(f"{key}={q.get(key)!r}, want {want!r}")
    if CLIENT_SECRET in url or "secret" in url.lower():
        problems.append("the client secret appears in the browser URL")
    _check(
        "auth URL asks for ONLY the read-only scope, offline, and never carries the secret",
        problems,
    )

    # -- the happy path ---------------------------------------------------------
    _reset()
    seen: dict = {}
    grant = _run(base, "approve", seen)
    problems = []
    if grant.refresh_token != "1//REFRESH-ABC_def-123":
        problems.append(f"got refresh token {grant.refresh_token!r}")
    if len(_Fake.exchanges) != 1:
        problems.append(f"{len(_Fake.exchanges)} token exchanges, expected exactly 1")
    elif _Fake.exchanges[0].get("grant_type") != "authorization_code":
        problems.append("wrong grant_type")
    _check("full flow succeeds; the token server verified PKCE and the secret itself", problems)

    # -- forged callback ----------------------------------------------------------
    _reset()
    seen = {}
    grant = _run(base, "forged_first", seen)
    problems = []
    if seen.get("forged_status") != 400:
        problems.append(f"forged callback answered {seen.get('forged_status')}, want 400")
    sent = [e.get("code") for e in _Fake.exchanges]
    if sent != ["CODE-1"]:
        problems.append(f"exchanged codes {sent}; the forged EVIL code must never be sent")
    if grant.refresh_token != "1//REFRESH-ABC_def-123":
        problems.append("the real callback after a forged one did not complete the flow")
    _check("a forged callback (wrong state) is refused AND the real one still completes", problems)

    # -- user denies --------------------------------------------------------------
    _reset()
    msg = _expect_error(lambda: _run(base, "deny", {}))
    _check(
        "clicking Deny gives a plain-language error and exchanges nothing",
        []
        if msg and "access_denied" in msg and "Deny" in msg and not _Fake.exchanges
        else [f"msg={msg!r}, exchanges={len(_Fake.exchanges)}"],
    )

    # -- granular consent: permission unticked -------------------------------------
    _reset(scope="https://www.googleapis.com/auth/userinfo.email")
    msg = _expect_error(lambda: _run(base, "approve", {}))
    _check(
        "refuses a grant whose Analytics permission was unticked, and says so",
        [] if msg and "unticked" in msg and "1//REFRESH" not in msg else [f"msg={msg!r}"],
    )

    _reset(give_refresh=False)
    msg = _expect_error(lambda: _run(base, "approve", {}))
    _check(
        "a response with no refresh token is an error, not a silent success",
        [] if msg and "no refresh token" in msg else [f"msg={msg!r}"],
    )

    # -- timeout -------------------------------------------------------------------
    _reset()
    msg = _expect_error(lambda: _run(base, "silent", {}, timeout=0.3))
    _check(
        "gives up with a clear message if nobody approves",
        [] if msg and "Timed out" in msg else [f"msg={msg!r}"],
    )

    # -- nothing sensitive in errors ---------------------------------------------------
    _reset(leak_on_error=True)
    msg = _expect_error(
        lambda: ya.exchange_code(
            SETTINGS, "STOLEN-CODE", "v" * 50, "http://127.0.0.1:1", token_url=f"{base}/token"
        )
    )
    problems = []
    if msg is None:
        problems.append("no AuthError raised")
    else:
        for secret in (CLIENT_SECRET, "STOLEN-CODE"):
            if secret in msg:
                problems.append(f"error text leaked {secret!r}: {msg}")
    _check("an error body that echoes the secret and the code is scrubbed before display", problems)

    msg = _expect_error(
        lambda: ya.authorize(SETTINGS, token_url="http://evil.example/token", out=lambda _s: None)
    )
    _check(
        "refuses to send the client secret over plain http to a non-local host",
        [] if msg and "https" in msg else [f"msg={msg!r}"],
    )

    # -- saving to .env ----------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        original = (
            "# my comment\n"
            "DATABASE_URL=postgresql://x\n"
            "\n"
            f"{ya.ENV_KEY}=OLD-BROAD-TOKEN\n"
            f"{ya.ENV_KEY}_BACKUP=keep-me\n"
            "THREADS_ACCESS_TOKEN=THAA-secret\n"
        )
        path.write_text(original)
        path.chmod(
            0o640
        )  # NOT 0o600: that is also mkstemp's default, so it could not catch a lost mode
        ya.update_env_file(path, {ya.ENV_KEY: "NEW/token-1"})
        after = path.read_text().split("\n")
        problems = []
        want = original.replace(f"{ya.ENV_KEY}=OLD-BROAD-TOKEN", f"{ya.ENV_KEY}=NEW/token-1").split(
            "\n"
        )
        if after != want:
            problems.append(f"unexpected result:\n{path.read_text()}")
        if stat.S_IMODE(path.stat().st_mode) != 0o640:
            problems.append(f"mode became {oct(stat.S_IMODE(path.stat().st_mode))}, want 0o640")
        if any(n.startswith(".env.tmp-") for n in os.listdir(tmp)):
            problems.append("a temp file was left behind")
        _check(
            "replaces ONLY the token line: comments, order, similar keys and file mode untouched",
            problems,
        )

        path.write_text("A=1\nB=2")  # no trailing newline
        ya.update_env_file(path, {ya.ENV_KEY: "tok"})
        _check(
            "appends a missing key cleanly when the file has no trailing newline",
            [] if path.read_text() == f"A=1\nB=2\n{ya.ENV_KEY}=tok\n" else [repr(path.read_text())],
        )

        path.write_text(f"{ya.ENV_KEY}=one\nX=1\n{ya.ENV_KEY}=two\n")
        ya.update_env_file(path, {ya.ENV_KEY: "fresh"})
        _check(
            "a duplicated key is replaced everywhere (dotenv lets the LAST one win)",
            []
            if path.read_text() == f"{ya.ENV_KEY}=fresh\nX=1\n{ya.ENV_KEY}=fresh\n"
            else [repr(path.read_text())],
        )

        path.write_text("A=1\n")
        bad = []
        for value in ("has space", "new\nline", 'q"uote', "hash#tag", ""):
            try:
                ya.update_env_file(path, {ya.ENV_KEY: value})
                bad.append(f"accepted {value!r}")
            except ValueError:
                pass
        if path.read_text() != "A=1\n":
            bad.append("the file was modified despite the refusal")
        _check("refuses values that could corrupt or inject into a .env", bad)

        missing = Path(tmp) / "new.env"
        ya.update_env_file(missing, {ya.ENV_KEY: "t"})
        _check(
            "creating a new .env gives it owner-only permissions",
            []
            if stat.S_IMODE(missing.stat().st_mode) == 0o600
            else [oct(stat.S_IMODE(missing.stat().st_mode))],
        )

    # -- the probe -----------------------------------------------------------------------
    reports = f"{base}/reports"
    _reset()
    ok = ya.probe_analytics("tok", reports_url=reports)
    _reset(reports_mode="not_enabled")
    off = ya.probe_analytics("tok", reports_url=reports)
    _reset(reports_mode="other")
    other = ya.probe_analytics("tok", reports_url=reports)
    unreachable = ya.probe_analytics("tok", reports_url="http://127.0.0.1:9/reports")
    problems = []
    if "OK" not in ok or "4242" not in ok:
        problems.append(f"success message: {ok}")
    if "not enabled" not in off or "Enable" not in off:
        problems.append(f"not-enabled message gives no fix: {off}")
    if "403" not in other or "not enabled" in other:
        problems.append(f"a permission error was mislabelled: {other}")
    if "unreachable" not in unreachable:
        problems.append(f"unreachable message: {unreachable}")
    _check(
        "the probe tells 'API not enabled' apart from a permission error and never raises",
        problems,
    )

    print()
    if _failures:
        print(f"FAILED {len(_failures)}/{_checks_run} checks: {', '.join(_failures)}")
        return 1
    print(f"all {_checks_run} checks passed")
    return 0


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return run_checks(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
