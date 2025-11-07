from functools import partial
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.async_api import async_playwright

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

API_FILE = Cache(Path(__file__).parent / "caches" / "strmfree_api.json", exp=28_800)

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "strmfree.json", exp=10_800)

BASE_URL = "https://streamfree.to"


async def refresh_api_cache(
    client: httpx.AsyncClient,
    url: str,
) -> dict[str, dict[str, list]]:
    log.info("Refreshing API cache")

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{url}": {e}')
        return {}

    data = r.json()

    data["timestamp"] = Time.now().timestamp()

    return data


async def get_events(
    client: httpx.AsyncClient,
    url: str,
    cached_keys: set[str],
) -> list[dict[str, str]]:

    if not (api_data := API_FILE.load(per_entry=False)):
        api_data = await refresh_api_cache(
            client,
            urljoin(url, "streams"),
        )

        API_FILE.write(api_data)

    events: list[dict[str, str]] = []

    now = Time.clean(Time.now())
    start_dt = now.delta(hours=-1)
    end_dt = now.delta(minutes=15)

    for category, streams in api_data["streams"].items():
        if not streams:
            continue

        for stream in streams:
            event_dt = Time.from_ts(stream["match_timestamp"])

            if not start_dt <= event_dt <= end_dt:
                continue

            sport, name = stream["league"], stream["name"]

            key = f"[{sport}] {name} (STRMFR)"

            if cached_keys & {key}:
                continue

            stream_url = stream["stream_key"]

            events.append(
                {
                    "sport": sport,
                    "event": name,
                    "link": urljoin(url, f"player/{category}/{stream_url}"),
                    "timestamp": event_dt.timestamp(),
                }
            )

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    cached_urls = CACHE_FILE.load()
    cached_count = len(cached_urls)
    urls.update(cached_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(
        client,
        BASE_URL,
        set(cached_urls.keys()),
    )

    log.info(f"Processing {len(events)} new URL(s)")

    if events:
        async with async_playwright() as p:
            browser, context = await network.browser(p)

            for i, ev in enumerate(events, start=1):
                handler = partial(
                    network.process_event,
                    url=ev["link"],
                    url_num=i,
                    context=context,
                    timeout=6,
                    log=log,
                )

                url = await network.safe_process(
                    handler,
                    url_num=i,
                    log=log,
                )

                if url:
                    sport, event, ts = (
                        ev["sport"],
                        ev["event"],
                        ev["timestamp"],
                    )

                    key = f"[{sport}] {event} (STRMFR)"

                    tvg_id, logo = leagues.get_tvg_info(sport, event)

                    entry = {
                        "url": url.replace("540p", "720p"),
                        "logo": logo,
                        "base": BASE_URL,
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
