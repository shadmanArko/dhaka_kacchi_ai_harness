"""Signs in to Google as a SERVICE ACCOUNT, with no human and no browser.

A service account is a robot identity with its own key file. Search Console
(and other Google APIs) accept it as a user. Signing in is the "JWT bearer"
flow: build a small signed token naming who you are and what you want to read,
send it to Google, and get back a short-lived access token.

The only non-trivial step is the RSA signature, done with the `cryptography`
library rather than by hand: hand-rolled crypto is the wrong place to be clever.
Nothing else here is Google-specific, so it works for any Google API whose
scope is passed in.

The private key and the signed assertion are never put in an error message.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

REQUEST_TIMEOUT_S = 30

# Google's own limit is one hour; asking for less means a leaked assertion dies sooner.
ASSERTION_LIFETIME_S = 300

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ServiceAccountError(RuntimeError):
    """Signing in failed in a way the operator must act on. Never caught."""


@dataclass(frozen=True, slots=True)
class ServiceAccountKey:
    client_email: str
    private_key: str
    token_uri: str

    def __repr__(self) -> str:  # never let the key reach a log via a stray print(key)
        return f"ServiceAccountKey(client_email={self.client_email!r}, private_key=<hidden>)"


def parse_key(raw: str, *, source: str) -> ServiceAccountKey:
    """A Google service-account key file's JSON, validated. `source` names where it
    came from, for the error message."""
    try:
        info = json.loads(raw)
    except ValueError:
        raise ServiceAccountError(f"{source} is not valid JSON.") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ServiceAccountError(
            f'{source} is not a service-account key (its "type" must be "service_account"). '
            "An OAuth client file will not work here."
        )
    missing = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
    if missing:
        raise ServiceAccountError(f"{source} is missing: {', '.join(missing)}.")
    key = ServiceAccountKey(info["client_email"], info["private_key"], info["token_uri"])
    parts = urllib.parse.urlsplit(key.token_uri)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        # The signed assertion is sent to this address, so it must not be plain http.
        raise ServiceAccountError(f"{source}: token_uri {key.token_uri!r} must be https.")
    return key


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def build_assertion(key: ServiceAccountKey, scope: str, *, now: int | None = None) -> str:
    """The signed JWT: header.claims.signature, RS256."""
    issued = int(time.time()) if now is None else now
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps(
            {
                "iss": key.client_email,
                "scope": scope,
                "aud": key.token_uri,
                "iat": issued,
                "exp": issued + ASSERTION_LIFETIME_S,
            }
        ).encode()
    )
    try:
        private = serialization.load_pem_private_key(key.private_key.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ServiceAccountError(
            "the private_key in the key file could not be read (it may have been pasted with "
            "its line breaks mangled). Download a fresh key from Google Cloud Console."
        ) from None
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ServiceAccountError("the private_key is not an RSA key, which Google requires.")
    signature = private.sign(header + b"." + claims, padding.PKCS1v15(), hashes.SHA256())
    return (header + b"." + claims + b"." + _b64(signature)).decode()


def access_token(key: ServiceAccountKey, scope: str) -> str:
    """Trades a signed assertion for a short-lived access token."""
    assertion = build_assertion(key, scope)
    body = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(key.token_uri, data=body), timeout=REQUEST_TIMEOUT_S
        ) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
            detail = f"{err.get('error', '')}: {err.get('error_description', '')}".strip(": ")
        except (ValueError, AttributeError):
            detail = f"HTTP {exc.code}"
        hint = ""
        if "signature" in detail.lower() or "invalid_grant" in detail:
            hint = (
                " The key was probably revoked or deleted: create a new one in Google Cloud "
                "Console -> IAM -> Service accounts -> Keys. A wrong clock on this machine "
                "also causes it."
            )
        raise ServiceAccountError(
            f"Google refused the sign-in for {key.client_email}: {detail}.{hint}"
        ) from None
    except urllib.error.URLError as exc:
        raise ServiceAccountError(f"Could not reach Google to sign in: {exc.reason}") from None
