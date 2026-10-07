import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://brighton.co.uk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ImJustBrightonEventsBot/1.0; +https://www.imjustbrighton.co.uk/)"
}

session = requests.Session()
session.headers.update(HEADERS)

def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()

def fetch(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def month_urls():
    today = datetime.utcnow().date()
    urls = []
    for i in range(0, 7):
        d = today.replace(day=1)
        month = d.month - 1 + i
        year = d.year + month // 12
        month = month % 12 + 1
        name = datetime(year, month, 1).strftime("%B").lower()
        urls.append(f"{BASE}/whats-on/{name}/")
    return urls

def event_links(html):
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0]
        absolute = urljoin(BASE, href)
        if not absolute.startswith(BASE + "/whats-on/"):
            continue
        path = absolute.replace(BASE, "")
        if any(x in path for x in ["/category/", "/list/", "/tag/"]):
            continue
        if path.rstrip("/") in ["/whats-on", "/whats-on/"]:
            continue
        # Event pages contain a slug and usually either a date segment or /events/.
        if re.search(r"/whats-on/[^/]+/(?:\d{4}-\d{2}-\d{2}/)?(?:\d+/)?$", path):
            if a.get_text(" ", strip=True):
                found.add(absolute)
        elif "/whats-on/events/" in path:
            found.add(absolute)
    return found

def first_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                if item.get("@type") == "Event":
                    return item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for g in graph:
                        if isinstance(g, dict) and g.get("@type") == "Event":
                            return g
    return {}

def parse_event(url):
    try:
        html = fetch(url)
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    data = first_jsonld(soup)

    title = clean(data.get("name")) if data else ""
    if not title:
        h1 = soup.find("h1")
        title = clean(h1.get_text(" ", strip=True) if h1 else "")

    description = clean(data.get("description")) if data else ""
    if not description:
        meta = soup.find("meta", attrs={"name": "description"})
        description = clean(meta.get("content") if meta else "")

    image = ""
    if data:
        image_value = data.get("image")
        if isinstance(image_value, list):
            image = image_value[0] if image_value else ""
        elif isinstance(image_value, dict):
            image = image_value.get("url", "")
        else:
            image = image_value or ""
    if not image:
        meta = soup.find("meta", property="og:image")
        image = meta.get("content", "") if meta else ""
    image = urljoin(url, image) if image else ""

    location = data.get("location") if data else None
    venue = ""
    if isinstance(location, dict):
        venue = clean(location.get("name"))
    elif isinstance(location, list) and location:
        if isinstance(location[0], dict):
            venue = clean(location[0].get("name"))

    start = data.get("startDate") if data else None
    end = data.get("endDate") if data else None

    text = clean(soup.get_text(" ", strip=True))
    if not start:
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text)
        if m:
            day = datetime.strptime(m.group(1), "%d/%m/%Y")
            tm = re.search(r"(\d{1,2}:\d{2})\s*(am|pm)?", text, re.I)
            if tm:
                raw_time = tm.group(1)
                suffix = tm.group(2)
                hh, mm = map(int, raw_time.split(":"))
                if suffix:
                    if suffix.lower() == "pm" and hh != 12:
                        hh += 12
                    if suffix.lower() == "am" and hh == 12:
                        hh = 0
                start = day.strftime("%Y-%m-%d") + f"T{hh:02d}:{mm:02d}:00"
            else:
                start = day.strftime("%Y-%m-%dT00:00:00")

    if start and len(start) == 10:
        start += "T00:00:00"

    if end and len(end) == 10:
        end += "T23:59:59"

    if not venue:
        # Brighton.co.uk event pages put the venue immediately after the time.
        for heading in soup.find_all(["h2", "h3"]):
            if "event specifics" in clean(heading.get_text(" ", strip=True)).lower():
                prev = heading.find_previous(["a", "h2", "h3"])
                if prev:
                    candidate = clean(prev.get_text(" ", strip=True))
                    if 1 < len(candidate) < 100:
                        venue = candidate
                break

    if not title or not start:
        return None

    return {
        "id": url.rstrip("/").split("/")[-1] or url,
        "title": title,
        "start": start,
        "end": end or "",
        "venue": venue,
        "description": description[:1000],
        "image": image,
        "url": url,
        "source": "Brighton.co.uk"
    }

def main():
    links = set()
    for url in month_urls():
        try:
            html = fetch(url)
            links.update(event_links(html))
        except Exception as exc:
            print("MONTH ERROR", url, exc)

    print("Found event URLs:", len(links))
    events = []
    seen = set()

    for index, url in enumerate(sorted(links)):
        if url in seen:
            continue
        seen.add(url)
        event = parse_event(url)
        if event:
            events.append(event)
        if index % 20 == 0:
            print("Parsed", index, "of", len(links))
        time.sleep(0.08)

    # Remove duplicates and past events, keeping multi-day events alive.
    unique = {}
    now = datetime.utcnow()
    for e in events:
        key = (e["title"].lower(), e["start"][:10], e["url"])
        unique[key] = e

    def sort_key(e):
        return e.get("start") or "9999"

    final = sorted(unique.values(), key=sort_key)

    payload = {
        "updated": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "https://brighton.co.uk/whats-on/",
        "events": final
    }

    with open("events/events.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("Wrote", len(final), "events")

if __name__ == "__main__":
    main()
