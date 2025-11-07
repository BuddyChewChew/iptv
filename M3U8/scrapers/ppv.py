from functools import partial
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.async_api import async_playwright

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

API_FILE = Cache(Path(__file__).parent / "caches" / "ppv_api.json", exp=28_800)

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "ppv.json", exp=10_800)

BASE_URL = "https://ppv.to"


async def refresh_api_cache(
    client: httpx.AsyncClient,
    url: str,
) -> dict[str, dict[str, str]]:
    log.info("Refreshing API cache")

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{url}": {e}')
        return {}

    return r.json()


async def get_events(
    client: httpx.AsyncClient,
    cached_keys: set[str],
) -> list[dict[str, str]]:
    if not (api_data := API_FILE.load(per_entry=False)):
        api_data = await refresh_api_cache(
            client,
            urljoin(
                BASE_URL,
                "api/streams",
            ),
        )

        API_FILE.write(api_data)

    events: list[dict[str, str]] = []

    now = Time.clean(Time.now())
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    for stream_group in api_data["streams"]:
        sport = stream_group["category"]

        if sport == "24/7 Streams":
            continue

        for event in stream_group["streams"]:
            name = event.get("name")
            start_ts = event.get("starts_at")
            logo = event.get("poster")
            iframe = event.get("iframe")

            if not (name and start_ts and iframe):
                continue

            key = f"[{sport}] {name} (PPV)"

            if cached_keys & {key}:
                continue

            event_dt = Time.from_ts(start_ts)

            if not start_dt <= event_dt <= end_dt:
                continue

            events.append(
                {
                    "sport": sport,
                    "event": name,
                    "link": iframe,
                    "logo": logo,
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

    events = await get_events(client, set(cached_urls.keys()))

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
                    sport, event, logo, ts = (
                        ev["sport"],
                        ev["event"],
                        ev["logo"],
                        ev["timestamp"],
                    )

                    key = f"[{sport}] {event} (PPV)"

                    tvg_id, pic = leagues.get_tvg_info(sport, event)

                    entry = {
                        "url": url,
                        "logo": logo or pic,
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
