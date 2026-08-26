#!/usr/bin/env python3
"""
fetch_feeds.py — pull recent items from RSS/Atom feeds into a local cache

The point of this file: it needs no credentials at all. Substack, Bandcamp
Daily and most music publications expose open feeds. Search indexes them
with weeks of lag, which is why the Substack roundups were unusable — but
the feeds themselves are current to the minute.

    python3 fetch_feeds.py                # all feeds in feeds.yaml
    python3 fetch_feeds.py --days 14      # window (default 14)
    python3 fetch_feeds.py --add https://example.substack.com/feed

Output: cache/feeds-YYYY-MM-DD.json

Stdlib only, including the parser and the YAML reading -- feeds.yaml is
deliberately kept to a flat "- url  # comment" shape so no dependency is
needed to read it.
"""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
import datetime as dt
import email.utils
import html
import json
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent
CACHE = ROOT / "cache"
FEEDS = ROOT / "feeds.yaml"

UA = f"python:curator-kit-feeds:{__version__} (personal digest tool)"

NS = {"atom": "http://www.w3.org/2005/Atom",
      "dc": "http://purl.org/dc/elements/1.1/"}


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


CTX = ssl_context()


def read_feeds() -> list[str]:
    """Read the feed list. Flat enough that no YAML library is required."""
    if not FEEDS.exists():
        sys.exit(f"! {FEEDS.name} not found")
    urls = []
    for line in FEEDS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("- "):
            urls.append(line[2:].strip().strip("'\""))
    return urls


def parse_date(raw: str | None) -> dt.datetime | None:
    """Feeds are inconsistent: RFC 2822 in RSS, ISO 8601 in Atom, and plenty
    of publications get their own format subtly wrong. Try both, give up
    quietly rather than dropping the item."""
    if not raw:
        return None
    try:
        d = email.utils.parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    try:
        d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def clean(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)          # strip markup
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse(xml_text: str, source: str, cutoff: dt.datetime) -> list[dict]:
    """Handle RSS 2.0 and Atom from one function; the shapes differ but the
    fields we want are the same three."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  ! {source}: malformed XML ({e})", file=sys.stderr)
        return []

    nodes = root.findall(".//item") or root.findall(".//atom:entry", NS)
    out = []
    for n in nodes:
        title = n.findtext("title") or n.findtext("atom:title", None, NS) or ""
        link = n.findtext("link") or ""
        if not link:
            le = n.find("atom:link", NS)
            link = le.get("href", "") if le is not None else ""
        raw_date = (n.findtext("pubDate") or n.findtext("atom:published", None, NS)
                    or n.findtext("atom:updated", None, NS) or n.findtext("dc:date", None, NS))
        when = parse_date(raw_date)
        if when and when < cutoff:
            continue
        body = (n.findtext("description") or n.findtext("atom:summary", None, NS)
                or n.findtext("atom:content", None, NS) or "")
        out.append({
            "source": source,
            "title": clean(title, 200),
            "url": link.strip(),
            "date": when.date().isoformat() if when else None,
            "summary": clean(body),
        })
    return out


def fetch(url: str, days: int) -> list[dict]:
    source = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"  ! {source}: HTTP {e.code}", file=sys.stderr)
        return []
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            print(f"  ! {source}: TLS verification failed "
                  "(see fetch_reddit.py --doctor)", file=sys.stderr)
        else:
            print(f"  ! {source}: {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  ! {source}: {e}", file=sys.stderr)
        return []
    return parse(body, source, cutoff)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=f"Cache recent RSS/Atom items for curator-kit. (v{__version__})")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--add", metavar="URL", help="append a feed to feeds.yaml and exit")
    ap.add_argument("--version", action="version", version=f"fetch_feeds.py {__version__}")
    args = ap.parse_args()

    if args.add:
        with FEEDS.open("a") as f:
            f.write(f"- {args.add}\n")
        print(f"added {args.add} to {FEEDS.name}")
        return

    urls = read_feeds()
    print(f"fetching {len(urls)} feeds, {args.days}d window")

    items: list[dict] = []
    ok = 0
    for u in urls:
        got = fetch(u, args.days)
        if got:
            ok += 1
        items.extend(got)
        print(f"  {re.sub(r'^https?://(www\\.)?', '', u).split('/')[0]:<34} {len(got)}")
        time.sleep(0.4)

    if not items:
        sys.exit("\n! nothing fetched — check the URLs in feeds.yaml")

    items.sort(key=lambda i: i["date"] or "", reverse=True)
    CACHE.mkdir(exist_ok=True)
    out = CACHE / f"feeds-{dt.date.today().isoformat()}.json"
    out.write_text(json.dumps({
        "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": args.days,
        "feeds_ok": ok,
        "feeds_total": len(urls),
        "count": len(items),
        "items": items,
    }, indent=2, ensure_ascii=False) + "\n")

    print(f"\nwrote {out}  ({len(items)} items from {ok}/{len(urls)} feeds)")
    for i in items[:10]:
        print(f"  {i['date'] or '????-??-??'}  {i['source']:<26} {i['title'][:52]}")


if __name__ == "__main__":
    main()
