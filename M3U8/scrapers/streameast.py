from functools import partial
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.async_api import async_playwright
from selectolax.parser import HTMLParser

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "streameast.json", exp=10_800)

MIRRORS = [
    "https://streameast.ch",
    "https://streameast.sg",
    "https://streameast.tw",
    "https://streameast.ph",
    "https://streameast.ec",
    "https://streameast.fi",
    "https://streameast.ms",
    "https://streameast.ps",
    "https://streameast.cf",
    "https://streameast.sk",
    "https://thestreameast.co",
    "https://thestreameast.fun",
    "https://thestreameast.ru",
    "https://thestreameast.su",
]


async def get_events(
    client: httpx.AsyncClient,
    url: str,
    cached_keys: set[str],
) -> list[dict[str, str]]:
    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{url}": {e}')

        return []

    soup = HTMLParser(r.text)
    events = []

    now = Time.clean(Time.now())
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    for section in soup.css("div.se-sport-section"):
        if not (sport := section.attributes.get("data-sport-name", "").strip()):
            continue

        for a in section.css("a.uefa-card"):
            if not (href := a.attributes.get("href")):
                continue

            link = urljoin(url, href)

            team_spans = [t.text(strip=True) for t in a.css("span.uefa-name")]

            if len(team_spans) == 2:
                name = f"{team_spans[0]} vs {team_spans[1]}"

            elif len(team_spans) == 1:
                name = team_spans[0]

            else:
                continue

            if not (time_span := a.css_first(".uefa-time")):
                continue

            time_text = time_span.text(strip=True)

            timestamp = int(a.attributes.get("data-time", Time.default_8()))

            key = f"[{sport}] {name} (SEAST)"

            if cached_keys & {key}:
                continue

            event_dt = Time.from_ts(timestamp)

            if time_text == "LIVE" or (start_dt <= event_dt <= end_dt):
                events.append(
                    {
                        "sport": sport,
                        "event": name,
                        "link": link,
                        "timestamp": timestamp,
                    }
                )

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    cached_urls = CACHE_FILE.load()
    cached_count = len(cached_urls)
    urls.update(cached_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    if not (base_url := await network.get_base(MIRRORS)):
        log.warning("No working StreamEast mirrors")
        CACHE_FILE.write(cached_urls)
        return

    log.info(f'Scraping from "{base_url}"')

    events = await get_events(
        client,
        base_url,
        set(cached_urls.keys()),
    )

    log.info(f"Processing {len(events)} new URL(s)")

    if events:
        async with async_playwright() as p:
            browser, context = await network.browser(p, browser="brave")

            for i, ev in enumerate(events, start=1):
                handler = partial(
                    network.process_event,
                    url=ev["link"],
                    url_num=i,
                    context=context,
                    log=log,
                )

                url = await network.safe_process(
                    handler,
                    url_num=i,
                    log=log,
                )

                if url:
                    sport, event, ts = ev["sport"], ev["event"], ev["timestamp"]

                    tvg_id, logo = leagues.get_tvg_info(sport, event)

                    key = f"[{sport}] {event} (SEAST)"

                    entry = {
                        "url": url,
                        "logo": logo,
                        "base": "https://embedsports.top/",
                        "timestamp": ts,
                        "id": tvg_id or "Live.Event.us",
                    }

                    urls[key] = cached_urls[key] = entry

            await browser.close()

    if new_count := len(cached_urls) - cached_count:
        log.info(f"Collected and cached {new_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
