"""One-time helper: mint a READ-ONLY YouTube Analytics refresh token.

Run with `make youtube-auth`. It opens your browser to Google's consent page,
you click Allow, and it saves the resulting refresh token into .env.

WHY THIS EXISTS, AND WHY NOT JUST REUSE THE OLD TOKEN: the YouTube Analytics
API (watch time, impressions, click-through rate) is readable only by the
channel owner, so it needs OAuth rather than an API key. A token already
existed in .env, borrowed from the separate dk-publishing project, but it was
granted `youtube.upload` and `youtube` - permission to upload and DELETE videos
on the channel. A statistics job has no business holding that, and this token
is the one that ends up in the VPS's .env. This helper asks for exactly one
permission, `yt-analytics.readonly`, so a leaked copy can read numbers and do
nothing else.

It replaces only the YOUTUBE_OAUTH_REFRESH_TOKEN line in THIS repo's .env. The
old token keeps working inside the other project's own config; nothing there is
touched or revoked.

HOW THE FLOW WORKS (the standard "installed app" loopback flow with PKCE):
  1. A tiny web server starts on 127.0.0.1 only, on a random free port.
  2. Your browser goes to Google and you approve.
  3. Google redirects to that local server with a one-time code.
  4. The code is exchanged for tokens, and the refresh token is saved.
No password is ever seen by this script, and the local server accepts exactly
one callback, only if it carries the random `state` value generated at the
start - a stray or forged request is answered 400 and ignored.

PRECONDITIONS, because each fails with a different confusing Google error:
  - The OAuth client must be of type "Desktop app" (it is: "Desktop DK Publish").
  - The consent screen must be "In production", not "Testing": Testing revokes
    refresh tokens after 7 days. (Checked 2026-10-07: it is In production.)
  - The YouTube Analytics API must be ENABLED in that Google Cloud project, or
    every report call returns 403 - which is not a token problem. The probe at
    the end of this script tells you which of the two it is.

You will see an "unverified app" warning. Click Advanced -> Go to (unsafe). That
is expected for your own app: verification matters only for apps other people
use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from warehouse.config import (
    ENV_FILE,
    ConfigError,
    YouTubeOAuthSettings,
    load_youtube_oauth_settings,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"

# The ONE permission requested. Not youtube.readonly, and nothing monetary: the
# impression and click-through metrics are not revenue data, so the narrower
# scope covers everything the analytics stage needs.
REQUIRED_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

WAIT_SECONDS = 300
REQUEST_TIMEOUT_S = 30
ENV_KEY = "YOUTUBE_OAUTH_REFRESH_TOKEN"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class AuthError(RuntimeError):
    """The flow failed in a way the operator must act on. Never caught."""


@dataclass(frozen=True, slots=True)
class Grant:
    refresh_token: str
    access_token: str
    scopes: tuple[str, ...]


def _require_https_or_local(url: str, label: str) -> None:
    """The client secret and tokens travel to these URLs, so plain http is only
    acceptable to a loopback address (which is what the verify script uses)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        return
    raise AuthError(f"{label} {url!r} must be https (or http to localhost for testing).")


# ---------------------------------------------------------------------------
# PKCE + authorization URL
# ---------------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). 64 random bytes -> an 86-character verifier,
    inside RFC 7636's 43-128 range."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(
    client_id: str, redirect_uri: str, state: str, challenge: str, *, auth_url: str = AUTH_URL
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": REQUIRED_SCOPE,
        # offline asks for a refresh token; prompt=consent forces Google to
        # issue one even if this client was approved before. Without it a repeat
        # run silently returns NO refresh token and looks like it worked.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# The loopback callback server
# ---------------------------------------------------------------------------


class _Server(HTTPServer):
    expected_state: str
    params: dict[str, str] | None
    done: threading.Event


class _Handler(BaseHTTPRequestHandler):
    server: _Server  # type: ignore[assignment]

    def log_message(self, *_args: object) -> None:  # keep the codes out of the terminal
        pass

    def _reply(self, status: int, message: str) -> None:
        body = (
            "<html><body style='font-family:sans-serif;padding:2em'>"
            f"<h3>{message}</h3></body></html>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server's required name)
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/":
            return self._reply(404, "Not found.")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        if "code" not in q and "error" not in q:
            return self._reply(400, "Nothing to do here.")
        # Only a callback carrying OUR state counts. Anything else - a stale tab,
        # a prefetch, another local program - is refused and ignored, and the
        # flow keeps waiting for the real one rather than failing.
        if not secrets.compare_digest(q.get("state", ""), self.server.expected_state):
            return self._reply(400, "This request was not part of the sign-in. Ignored.")
        if self.server.params is None:
            self.server.params = q
            self.server.done.set()
        self._reply(200, "Done. You can close this tab and return to the terminal.")


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def _redact(text: str, *secrets_: str) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "***")
    return text


def exchange_code(
    settings: YouTubeOAuthSettings,
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    token_url: str = TOKEN_URL,
) -> Grant:
    _require_https_or_local(token_url, "token URL")
    data = urllib.parse.urlencode(
        {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(token_url, data=data), timeout=REQUEST_TIMEOUT_S
        ) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
            detail = f"{err.get('error', 'error')}: {err.get('error_description', '')}".strip()
        except (ValueError, AttributeError):
            detail = f"HTTP {exc.code}"
        raise AuthError(
            _redact(f"Google refused the code: {detail}", settings.client_secret, code)
        ) from None
    except urllib.error.URLError as exc:
        raise AuthError(
            _redact(f"Could not reach Google: {exc.reason}", settings.client_secret)
        ) from None

    scopes = tuple(body.get("scope", "").split())
    if REQUIRED_SCOPE not in scopes:
        # Google's consent screen lets people untick individual permissions.
        raise AuthError(
            "The YouTube Analytics permission was not granted (it was probably unticked "
            f"on the consent screen). Granted: {', '.join(scopes) or 'nothing'}. Run it "
            "again and leave the box ticked."
        )
    refresh = body.get("refresh_token")
    if not refresh:
        raise AuthError(
            "Google returned no refresh token. Remove this app at "
            "myaccount.google.com/permissions and run it again."
        )
    return Grant(refresh_token=refresh, access_token=body["access_token"], scopes=scopes)


def authorize(
    settings: YouTubeOAuthSettings,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    auth_url: str = AUTH_URL,
    token_url: str = TOKEN_URL,
    timeout: float = WAIT_SECONDS,
    out: Callable[[str], None] = print,
) -> Grant:
    _require_https_or_local(auth_url, "authorization URL")
    _require_https_or_local(token_url, "token URL")

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)

    server = _Server(("127.0.0.1", 0), _Handler)
    server.expected_state = state
    server.params = None
    server.done = threading.Event()
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = build_auth_url(settings.client_id, redirect_uri, state, challenge, auth_url=auth_url)
    try:
        out("Opening your browser. If nothing opens, paste this into it:")
        out(f"\n  {url}\n")
        out("Google will warn the app is unverified: click Advanced -> Go to (unsafe).")
        open_browser(url)
        if not server.done.wait(timeout):
            raise AuthError(f"Timed out after {int(timeout)}s waiting for you to approve.")
    finally:
        server.shutdown()
        server.server_close()

    params = server.params or {}
    if "error" in params:
        reason = params["error"]
        hint = " You clicked Cancel/Deny." if reason == "access_denied" else ""
        raise AuthError(f"Google reported: {reason}.{hint}")
    return exchange_code(settings, params["code"], verifier, redirect_uri, token_url=token_url)


# ---------------------------------------------------------------------------
# Saving the token
# ---------------------------------------------------------------------------

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~/+=-]+$")


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a dotenv file, changing nothing else.

    Every other line, comment, blank line and the file's permission bits are
    preserved, and the write is atomic (temp file + rename) so a crash cannot
    leave a half-written .env holding every other secret. A key that already
    appears is replaced EVERYWHERE it appears, because python-dotenv lets a later
    duplicate win and replacing only the first would silently change nothing.
    """
    for key, value in updates.items():
        if not _SAFE_VALUE.match(value):
            raise ValueError(f"refusing to write {key}: value has characters unsafe for a .env")

    existed = path.exists()
    text = path.read_text() if existed else ""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # re-added below; avoids growing a blank line on every save

    remaining = dict(updates)
    for i, line in enumerate(lines):
        for key in list(updates):
            if re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", line):
                lines[i] = f"{key}={updates[key]}"
                remaining.pop(key, None)
    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    mode = path.stat().st_mode & 0o777 if existed else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Does the token actually work against the Analytics API?
# ---------------------------------------------------------------------------


def probe_analytics(access_token: str, *, reports_url: str = REPORTS_URL) -> str:
    """One tiny read-only query, returning a sentence for the operator. Never
    raises: a failed probe must not make a successful token mint look failed -
    but it distinguishes the two causes people confuse (API not enabled vs a
    permission problem)."""
    _require_https_or_local(reports_url, "reports URL")
    end = date.today() - timedelta(days=3)  # Analytics data lags 2-3 days
    query = urllib.parse.urlencode(
        {
            "ids": "channel==MINE",
            "startDate": (end - timedelta(days=7)).isoformat(),
            "endDate": end.isoformat(),
            "metrics": "views",
        }
    )
    req = urllib.request.Request(
        f"{reports_url}?{query}", headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            rows = json.loads(resp.read()).get("rows") or [[0]]
        return f"OK - the Analytics API answered (channel views, last 7 days: {rows[0][0]})."
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        if exc.code == 403 and ("has not been used" in message or "disabled" in message):
            return (
                "TOKEN SAVED, but the YouTube Analytics API is not enabled in your Google Cloud "
                "project yet. Enable it (APIs & Services -> Library -> YouTube Analytics API -> "
                "Enable), wait a minute, then run `make youtube-auth-check`."
            )
        return f"TOKEN SAVED, but the Analytics API said HTTP {exc.code}: {message[:200]}"
    except urllib.error.URLError as exc:
        return f"TOKEN SAVED, but the Analytics API was unreachable: {exc.reason}"


def refresh_access_token(settings: YouTubeOAuthSettings, *, token_url: str = TOKEN_URL) -> str:
    """Exchange the saved refresh token for a short-lived access token."""
    _require_https_or_local(token_url, "token URL")
    assert settings.refresh_token  # the caller loaded settings with the token required
    data = urllib.parse.urlencode(
        {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "refresh_token": settings.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(token_url, data=data), timeout=REQUEST_TIMEOUT_S
        ) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
            detail = f"{err.get('error', '')}: {err.get('error_description', '')}"
        except (ValueError, AttributeError):
            detail = f"HTTP {exc.code}"
        raise AuthError(
            _redact(
                f"Google refused the saved token ({detail}). Run `make youtube-auth`.",
                settings.client_secret,
                settings.refresh_token,
            )
        ) from None
    except urllib.error.URLError as exc:
        raise AuthError(
            _redact(f"Could not reach Google: {exc.reason}", settings.client_secret)
        ) from None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not mint a token; just test the one already saved against the Analytics API",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="print the new refresh token instead of saving it to .env",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_youtube_oauth_settings(require_refresh_token=args.check)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.check:
            print(probe_analytics(refresh_access_token(settings)).replace("TOKEN SAVED, but ", ""))
            return 0

        grant = authorize(settings)
        if args.no_write:
            print(f"\n{ENV_KEY}={grant.refresh_token}")
        else:
            update_env_file(ENV_FILE, {ENV_KEY: grant.refresh_token})
            print(f"\nSaved a read-only token to {ENV_FILE} as {ENV_KEY}.")
        print("Permissions granted:", ", ".join(grant.scopes))
        print(probe_analytics(grant.access_token))
    except AuthError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
