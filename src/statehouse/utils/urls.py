"""URL handling.

Deduplication lives or dies on URL canonicalisation: the same bill page is
reachable with a session id, a tracking parameter, a different host casing and
a trailing slash, and each variant would otherwise become its own row.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

__all__ = [
    "canonical_url",
    "absolutise",
    "same_site",
    "registrable_host",
    "strip_params",
    "TRACKING_PARAMS",
]

#: Parameters that never change what a page contains.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "sessionid",
        "session_id",
        "jsessionid",
        "phpsessid",
        "aspxauth",
        "cachebuster",
        "_",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def registrable_host(url: str) -> str:
    """Lowercased host with any port and userinfo removed."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return host.lower()


def canonical_url(url: str, *, drop: Iterable[str] = ()) -> str:
    """Return a canonical form of ``url``.

    Scheme and host are lowercased, a default port removed, tracking
    parameters dropped, remaining query parameters sorted, the fragment
    discarded, and a bare trailing slash removed from a non-root path.
    Anything that is not parseable as an absolute URL comes back stripped but
    otherwise untouched — better a slightly duplicated key than a crash inside
    a spider.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    unwanted = TRACKING_PARAMS | {p.lower() for p in drop}
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in unwanted
    ]
    query = urlencode(sorted(kept))

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def absolutise(base: str, href: str) -> str:
    """Resolve ``href`` against ``base`` and canonicalise the result."""
    if not href:
        return ""
    return canonical_url(urljoin(base, href.strip()))


def same_site(left: str, right: str) -> bool:
    """True when both URLs share a registrable host.

    Used by the crawl frontier to keep a spider from wandering off a portal
    onto a linked news site.
    """
    left_host = registrable_host(left)
    right_host = registrable_host(right)
    if not left_host or not right_host:
        return False
    if left_host == right_host:
        return True
    left_parts = left_host.split(".")
    right_parts = right_host.split(".")
    return left_parts[-2:] == right_parts[-2:]


def strip_params(url: str, params: Iterable[str]) -> str:
    """Drop specific query parameters, leaving the rest of the URL as given."""
    return canonical_url(url, drop=params)
