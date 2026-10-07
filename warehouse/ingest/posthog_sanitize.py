"""Reduces a PostHog event to what the warehouse is allowed to keep.

This is the privacy boundary of the PostHog ingester. Nothing the site's
visitors did is stored except through this module, and an event is scrubbed
BEFORE it is written - the unscrubbed event never reaches the database.

THE RULE: AN ALLOWLIST, NOT A DENYLIST. Only the property names in ALLOWED are
kept. Everything else is dropped, including fields nobody has looked at yet. A
denylist ("remove $ip") fails the first day PostHog or a developer adds a new
property that happens to carry personal data; an allowlist fails the other way,
by losing a harmless field until someone reviews it and adds it here. Dropped
fields that are NOT known noise or known-sensitive are counted and reported on
every run (see Sanitized.unreviewed), so a new field is noticed, not lost.

Derived from the real PostHog project on 2026-10-07: 175 distinct property
names across 3,026 events. What that review found, and what is done about it:

  IDENTIFIERS  distinct_id, $user_id, $anon_distinct_id hold a real customer id
               ("cust_...", the same id the ordering database uses) for logged-in
               visitors. Each is replaced by a KEYED HASH (pseudonym()). The same
               person always maps to the same pseudonym, so behaviour can still be
               followed, but the id cannot be turned back into a customer without
               the salt. $device_id and $window_id are dropped outright - a
               persistent device fingerprint buys nothing once visitors are hashed.
  LOCATION     city, postal code, latitude/longitude and accuracy radius were on
               57% of events. Only country, continent and region are kept.
  URLS         $current_url, $referrer, $session_entry_url and friends carried a
               live password-reset token (?token=), order ids (?order=) and ad
               click ids (?fbclid=). URLs keep only the five utm_* parameters.
  CLICK IDS    PostHog also lifts those ids into properties named after them
               ($fbc even embeds the click id in its VALUE). Not on the allowlist,
               so dropped.
  PERSON BLOBS $set / $set_once copy the full URL (token included) into a person
               profile. Dropped.
  PAGE TEXT    $el_text (the visible text of a clicked element, on 913 events) is
               dropped.
  USER AGENT   $raw_user_agent is dropped; the parsed $browser/$os/$device_type
               are kept.
  ADMIN        Events from /admin are the owner's own use of the back office, not
               customers, and are excluded entirely.

Pseudonyms are not anonymity. Whoever holds the salt can recompute the pseudonym
of a known customer id, which is deliberate (it is how a future analysis could
join browsing to orders), and also means the salt must be guarded like a
password. Without it the stored ids are not reversible.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PSEUDONYM_PREFIX = "ph_"
PSEUDONYM_HEX_CHARS = 32  # 128 bits: far beyond any collision risk at this scale

# Properties whose VALUE is an identifier, replaced by pseudonym().
HASHED = frozenset({"distinct_id", "$user_id", "$anon_distinct_id"})

# The ONLY property names that survive. Kept to what the four summary tables and
# a future analyst plausibly need; add to it deliberately, after reviewing what
# the field can contain.
ALLOWED = frozenset(
    {
        # --- identifiers (hashed above) ---
        "distinct_id",
        "$user_id",
        "$anon_distinct_id",
        # --- session and page ---
        "$session_id",
        "$pageview_id",
        "$current_url",
        "$pathname",
        "$host",
        "$referrer",
        "$referring_domain",
        "$search_engine",
        "$external_click_url",
        "$event_type",
        "$is_identified",
        "$cookieless_mode",
        "title",
        "navigation_type",
        # --- how the visit began ---
        "$session_entry_url",
        "$session_entry_host",
        "$session_entry_pathname",
        "$session_entry_referrer",
        "$session_entry_referring_domain",
        "$session_entry_search_engine",
        "$session_entry_utm_source",
        "$session_entry_utm_medium",
        "$session_entry_utm_campaign",
        "$session_entry_utm_content",
        "$session_entry_utm_term",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        # --- engagement on the previous page ---
        "$prev_pageview_id",
        "$prev_pageview_pathname",
        "$prev_pageview_duration",
        "$prev_pageview_max_scroll_percentage",
        "$prev_pageview_max_content_percentage",
        "$prev_pageview_last_scroll_percentage",
        "$prev_pageview_last_content_percentage",
        # Raw scroll/content depth in pixels, alongside the percentages above:
        # numbers about the page, not about the person.
        "$prev_pageview_last_content",
        "$prev_pageview_last_scroll",
        "$prev_pageview_max_content",
        "$prev_pageview_max_scroll",
        # --- site speed ---
        "$web_vitals_FCP_value",
        "$web_vitals_LCP_value",
        "$web_vitals_INP_value",
        "$web_vitals_CLS_value",
        # --- device and browser (parsed, never the raw user agent) ---
        "$browser",
        "$browser_version",
        "$browser_language",
        "$browser_language_prefix",
        "$os",
        "$os_version",
        "$device_type",
        "$device",
        "$device_model",
        "$webview_app",
        "$webview_app_version",
        "$screen_width",
        "$screen_height",
        "$viewport_width",
        "$viewport_height",
        "$timezone",
        "$lib",
        "$lib_version",
        # --- coarse location only ---
        "$geoip_country_code",
        "$geoip_country_name",
        "$geoip_continent_code",
        "$geoip_continent_name",
        "$geoip_subdivision_1_code",
        "$geoip_subdivision_1_name",
        "$geoip_time_zone",
        # --- the site's own events (src/lib/analytics.ts trackEvent calls) ---
        "kind",
        "status",
        "reason",
        "date",
        "fulfillmentType",
        "itemCount",
        "totalCents",
        "feeCents",
        "distanceKm",
    }
)

# Dropped on purpose and never worth reporting - either known-sensitive or known
# SDK noise. Anything dropped that matches NEITHER is "unreviewed" and reported.
_KNOWN_DROPPED_EXACT = frozenset(
    {
        "$ip", "$device_id", "$window_id", "$raw_user_agent", "$set", "$set_once",
        "$el_text", "$elements", "$elements_chain", "token", "$insert_id", "$sent_at",
        "$time", "$timezone_offset", "$config_defaults", "$initialization_time",
        "$last_posthog_reset", "$recording_status", "$session_recording_start_reason",
        "$process_person_profile", "$had_persisted_distinct_id", "$ce_version",
        "$sdk_dist_channel", "$web_vitals_allowed_metrics", "$active_feature_flags",
        "$feature_flag_payloads", "$feature_flag_request_id", "$configured_session_timeout_ms",
        "$snapshot_max_depth_exceeded", "$geoip_latitude", "$geoip_longitude",
        "$geoip_postal_code", "$geoip_city_name", "$geoip_accuracy_radius",
        "$geoip_subdivision_2_code", "$geoip_subdivision_2_name", "$fbc", "$fbp",
    }
)  # fmt: skip
_KNOWN_DROPPED_PATTERNS = (
    re.compile(r"^\$sdk_debug_"),
    re.compile(r"^\$debug_"),
    re.compile(r"^\$lib_rate_limit"),
    re.compile(r"^\$web_vitals_\w+_event$"),
    re.compile(r"^\$\w+_enabled_server_side$"),
    re.compile(r"^\$\w+_disabled_server_side$"),
    # Click ids by the END of the name, so $session_entry_fbclid and any variant
    # not yet invented are recognised as known-sensitive (they are dropped by the
    # allowlist either way; this only keeps them out of the "unreviewed" report).
    re.compile(
        r"(^|[$_])(gclid|gad_source|gclsrc|dclid|gbraid|wbraid|fbclid|msclkid|twclid"
        r"|li_fat_id|mc_cid|igshid|ttclid|rdt_cid|epik|qclid|sccid|kx|irclid|fbc|fbp)$",
        re.IGNORECASE,
    ),
)

_URL_KEY = re.compile(r"(url|referrer|href)$", re.IGNORECASE)

# The only query parameters allowed to survive in a URL.
KEEP_QUERY_PARAMS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}
)

MAX_STRING_LENGTH = 500
MAX_EVENT_NAME_LENGTH = 200


def pseudonym(salt: str, value: str) -> str:
    """A keyed hash of an identifier: deterministic (same person, same pseudonym,
    so behaviour can be followed) and irreversible without the salt. HMAC rather
    than a bare hash of salt+value, which is the textbook construction for this
    and immune to length-extension tricks."""
    digest = hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()
    return PSEUDONYM_PREFIX + digest[:PSEUDONYM_HEX_CHARS]


def scrub_url(raw: str) -> str:
    """Keeps scheme, host and path plus the utm_* parameters. Removes every other
    query parameter, the #fragment and any user:password@. Not a parseable
    absolute URL -> cut at the first ? or #, the only safe guess."""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return re.split(r"[?#]", raw, maxsplit=1)[0]
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() in KEEP_QUERY_PARAMS
    ]
    host = parts.netloc.rpartition("@")[2]  # everything after the LAST @ - drops credentials
    return urlunsplit((parts.scheme, host, parts.path, urlencode(kept), ""))


# Shapes that must never appear in anything stored. This is NOT the scrubbing
# (the allowlist does that); it is a second, independent tripwire over the
# FINISHED payload, so that a future bug or a careless allowlist addition makes
# the run stop instead of quietly storing a secret. It fails closed.
_LEAK_PATTERNS = (
    ("a customer id", re.compile(r"\bcus?t?_\w", re.IGNORECASE)),
    (
        "a reset or access token in a URL",
        re.compile(r"[?&#](token|code|access_token)=", re.IGNORECASE),
    ),
    ("an order id in a URL", re.compile(r"[?&]order=", re.IGNORECASE)),
    ("an ad click id", re.compile(r"[?&](fbclid|gclid|msclkid|ttclid|twclid)=", re.IGNORECASE)),
    ("an email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("a PostHog key", re.compile(r"\bph[xc]_[A-Za-z0-9]{16,}")),
)


def leak_check(payload: dict) -> str | None:
    """Returns what kind of sensitive-looking value the finished payload still
    contains, or None if it is clean. Names the KIND, never the value."""
    text = json.dumps(payload, ensure_ascii=False)
    for kind, pattern in _LEAK_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _is_known_dropped(key: str) -> bool:
    return key in _KNOWN_DROPPED_EXACT or any(p.search(key) for p in _KNOWN_DROPPED_PATTERNS)


@dataclass(frozen=True, slots=True)
class Sanitized:
    event_uuid: str
    occurred_at: datetime
    source_created_at: datetime
    payload: dict
    # Property names dropped that are neither known-sensitive nor known noise: the
    # list the owner should look at, since each is either harmless (add it to
    # ALLOWED) or a leak that was caught just in time.
    unreviewed: Counter = field(default_factory=Counter)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_admin(properties: dict) -> bool:
    path = properties.get("$pathname")
    if not isinstance(path, str) or not path:
        url = properties.get("$current_url")
        path = urlsplit(url).path if isinstance(url, str) else ""
    return path == "/admin" or path.startswith("/admin/")


def sanitize_event(
    *,
    salt: str,
    uuid: str,
    event: str,
    timestamp: str,
    created_at: str,
    distinct_id: str,
    properties: dict,
) -> Sanitized | None:
    """Returns the scrubbed event, or None for an event that must not be stored at
    all (the owner's own /admin traffic)."""
    if _is_admin(properties):
        return None

    unreviewed: Counter = Counter()
    clean: dict = {}
    for key, value in properties.items():
        if key == "distinct_id":
            continue  # the column below is authoritative; the property is a copy
        if key not in ALLOWED:
            if not _is_known_dropped(key):
                unreviewed[key] += 1
            continue
        if isinstance(value, (dict, list)):
            # A nested structure can hide anything, so it is not stored even under
            # an allowed name. Reported, since it means a field changed shape.
            unreviewed[f"{key} (not a plain value)"] += 1
            continue
        if key in HASHED:
            if isinstance(value, str) and value:
                clean[key] = pseudonym(salt, value)
            continue
        if isinstance(value, str):
            value = scrub_url(value) if _URL_KEY.search(key) else value
            if len(value) > MAX_STRING_LENGTH:
                value = value[:MAX_STRING_LENGTH]
        clean[key] = value

    payload = {
        "uuid": uuid,
        "event": event[:MAX_EVENT_NAME_LENGTH],
        "timestamp": timestamp,
        "distinct_id": pseudonym(salt, distinct_id) if distinct_id else None,
        "properties": clean,
    }
    return Sanitized(
        event_uuid=uuid,
        occurred_at=_parse_time(timestamp),
        source_created_at=_parse_time(created_at),
        payload=payload,
        unreviewed=unreviewed,
    )
