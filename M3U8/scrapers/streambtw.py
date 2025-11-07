import re
from functools import partial
from pathlib import Path
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str]] = {}

BASE_URL = "https://streambtw.com"

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "streambtw.json", exp=3_600)


async def process_event(
    client: httpx.AsyncClient,
    url: str,
    url_num: int,
) -> str | None:

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'URL {url_num}) Failed to fetch "{url}": {e}')
        return

    valid_m3u8 = re.compile(
        r'var\s+(\w+)\s*=\s*["\']?(https?:\/\/[^"\'\s>]+\.m3u8(?:\?[^"\'\s>]*)?)["\']?',
        re.IGNORECASE,
    )

    if match := valid_m3u8.search(r.text):
        log.info(f"URL {url_num}) Captured M3U8")
        return match[2]

    log.info(f"URL {url_num}) No M3U8 found")


async def get_events(client: httpx.AsyncClient) -> list[dict[str, str]]:
    try:
        r = await client.get(BASE_URL)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{BASE_URL}": {e}')

        return []

    soup = HTMLParser(r.text)

    events = []

    for card in soup.css("div.container div.card"):
        link = card.css_first("a.btn.btn-primary")

        if not (href := link.attrs.get("href")):
            continue

        sport = card.css_first("h5.card-title").text(strip=True)

        name = card.css_first("p.card-text").text(strip=True)

        events.append(
            {
                "sport": sport,
                "event": name,
                "link": urljoin(BASE_URL, href),
            }
        )

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    if cached := CACHE_FILE.load():
        urls.update(cached)
        log.info(f"Loaded {len(urls)} event(s) from cache")
        return

    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(client)

    log.info(f"Processing {len(events)} new URL(s)")

    if events:
        now = Time.now().timestamp()

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                client=client,
                url=ev["link"],
                url_num=i,
            )

            url = await network.safe_process(
                handler,
                url_num=i,
                log=log,
                timeout=10,
            )

            if url:
                sport, event = ev["sport"], ev["event"]

                key = f"[{sport}] {event} (SBTW)"

                tvg_id, logo = leagues.get_tvg_info(sport, event)

                entry = {
                    "url": url,
                    "logo": logo,
                    "base": BASE_URL,
                    "timestamp": now,
                    "id": tvg_id or "Live.Event.us",
                }

                urls[key] = entry

    log.info(f"Collected {len(urls)} event(s)")

    CACHE_FILE.write(urls)
