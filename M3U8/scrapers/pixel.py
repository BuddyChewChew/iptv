import re
from pathlib import Path

import httpx

from .utils import Cache, Time, get_logger, leagues

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

API_FILE = Cache(Path(__file__).parent / "caches" / "pixel_api.json", exp=28_800)

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "pixel.json", exp=10_800)

BASE_URL = "https://pixelsport.tv/backend/livetv/events"


async def refresh_api_cache(
    client: httpx.AsyncClient,
    url: str,
    ts: float,
) -> dict[str, list[dict, str, str]]:
    log.info("Refreshing API cache")

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{url}": {e}')
        return {}

    data = r.json()

    data["timestamp"] = ts

    return data


async def get_events(
    client: httpx.AsyncClient,
    cached_keys: set[str],
) -> dict[str, str | float]:
    now = Time.clean(Time.now())

    if not (api_data := API_FILE.load(per_entry=False)):
        api_data = await refresh_api_cache(
            client,
            BASE_URL,
            now.timestamp(),
        )

        API_FILE.write(api_data)

    events = {}

    pattern = re.compile(
        r"https?://[^\s'\"]+?\.m3u8(?:\?[^\s'\"]*)?",
        re.IGNORECASE,
    )

    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    for event in api_data["events"]:
        event_dt = Time.from_str(f'{event["date"]} UTC', "%Y-%m-%dT%H:%M:%S.%fZ")

        if now.date() != event_dt.date():
            continue

        if not start_dt <= event_dt <= end_dt:
            continue

        event_name = event["match_name"]
        channel_info: dict[str, str] = event["channel"]
        category: dict[str, str] = channel_info["TVCategory"]

        sport = category["name"]

        stream_urls = [(i, f"server{i}URL") for i in range(1, 4)]

        for z, stream_url in stream_urls:
            if stream_link := channel_info.get(stream_url):
                if pattern.search(stream_link):
                    key = f"[{sport}] {event_name} {z} (PIXL)"

                    if cached_keys & {key}:
                        continue

                    tvg_id, logo = leagues.get_tvg_info(sport, event_name)

                    events[key] = {
                        "url": stream_link,
                        "logo": logo,
                        "base": "https://pixelsport.tv/",
                        "timestamp": event_dt.timestamp(),
                        "id": tvg_id or "Live.Event.us",
                    }

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    cached_urls = CACHE_FILE.load()
    cached_count = len(cached_urls)
    urls.update(cached_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(client, set(cached_urls.keys()))

    if events:
        for d in (urls, cached_urls):
            d |= events

    if new_count := len(cached_urls) - cached_count:
        log.info(f"Collected and cached {new_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
