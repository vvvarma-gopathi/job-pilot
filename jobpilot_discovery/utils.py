from __future__ import annotations

import hashlib
import html
import ipaddress
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    # Greenhouse content is often HTML-entity escaped; decode before parsing.
    decoded = html.unescape(value)
    soup = BeautifulSoup(decoded, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    # Remove fragments and common tracking parameters, retain meaningful query strings.
    query_items = [item for item in parts.query.split("&") if item and not item.lower().startswith(("utm_", "gh_src="))]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "&".join(query_items), ""))


def is_public_https_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme.casefold() != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def fingerprint(title: str, company: str, location: str | None) -> str:
    raw = "|".join((title.casefold().strip(), company.casefold().strip(), (location or "").casefold().strip()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Lever uses milliseconds; Unix seconds are much smaller.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return None
    return None
