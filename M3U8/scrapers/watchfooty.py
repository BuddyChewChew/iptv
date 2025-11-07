import asyncio
import re
from functools import partial
from itertools import chain
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from playwright.async_api import BrowserContext, async_playwright

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

API_FILE = Cache(Path(__file__).parent / "caches" / "watchfty_api.json", exp=28_800)

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "watchfty.json", exp=10_800)

MIRRORS = [
    "https://www.watchfooty.cc",
    "https://www.watchfooty.vip",
    "https://www.watchfooty.live",
]

SPORT_ENDPOINTS = [
    "football",
    "american-football",
    "hockey",
    "basketball",
    "baseball",
    "racing",
    "fighting",
    "golf",
]


async def get_api_data(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{url}": {e}')
        return []

    return r.json()


async def refresh_api_cache(
    client: httpx.AsyncClient,
    url: str,
) -> list[dict[str, Any]]:
    log.info("Refreshing API cache")

    tasks = [
        get_api_data(
            client,
            urljoin(url, f"api/v1/matches/{sport}"),
        )
        for sport in SPORT_ENDPOINTS
    ]

    results = await asyncio.gather(*tasks)

    data = list(chain(*results))

    for ev in data:
        ev["ts"] = ev.pop("timestamp")

    data[-1]["timestamp"] = Time.now().timestamp()

    return data


async def process_event(
    url: str,
    url_num: int,
    context: BrowserContext,
) -> str | None:
    page = await context.new_page()

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(network.capture_req, captured=captured, got_one=got_one)

    page.on("request", handler)

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=15_000,
        )

        try:
            header = await page.wait_for_selector(
                "text=/Stream Links/i",
                timeout=5_000,
            )

            text = await header.inner_text()
        except TimeoutError:
            log.warning(f"URL {url_num}) Can't find stream links header.")
            return

        match = re.search(r"\((\d+)\)", text)

        if not match or int(match[1]) == 0:
            log.warning(f"URL {url_num}) No available stream links.")
            return

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=6)
        except asyncio.TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return

        finally:
            if not wait_task.done():
                wait_task.cancel()

                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            log.info(f"URL {url_num}) Captured M3U8")
            return captured[-1]

        log.warning(f"URL {url_num}) No M3U8 captured after waiting.")
        return

    except Exception as e:
        log.warning(f"URL {url_num}) Exception while processing: {e}")
        return

    finally:
        page.remove_listener("request", handler)
        await page.close()


async def get_events(
    client: httpx.AsyncClient,
    base_url: str,
    cached_keys: set[str],
) -> list[dict[str, str]]:

    if not (api_data := API_FILE.load(per_entry=False, index=-1)):
        api_data = await refresh_api_cache(client, base_url)

        API_FILE.write(api_data)

    events: list[dict[str, str]] = []

    now = Time.clean(Time.now())
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)
    pattern = re.compile(r"\-+|\(")

    for event in api_data:
        match_id = event["matchId"]
        name = event["title"]
        league = event["league"]

        if not (ts := event.get("ts")):
            continue

        start_ts = int(str(ts)[:-3])

        event_dt = Time.from_ts(start_ts)

        if not start_dt <= event_dt <= end_dt:
            continue

        sport = pattern.split(league, 1)[0].strip()

        logo = urljoin(base_url, poster) if (poster := event.get("poster")) else None

        key = f"[{sport}] {name} (WFTY)"

        if cached_keys & {key}:
            continue

        events.append(
            {
                "sport": sport,
                "event": name,
                "link": urljoin(base_url, f"stream/{match_id}"),
                "logo": logo,
                "timestamp": event_dt.timestamp(),
            }
        )

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    cached_urls = CACHE_FILE.load()
    valid_urls = {k: v for k, v in cached_urls.items() if v["url"]}
    valid_count = cached_count = len(valid_urls)
    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    if not (base_url := await network.get_base(MIRRORS)):
        log.warning("No working WatchFooty mirrors")
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
            browser, context = await network.browser(p)

            for i, ev in enumerate(events, start=1):
                handler = partial(
                    process_event,
                    url=ev["link"],
                    url_num=i,
                    context=context,
                )

                url = await network.safe_process(
                    handler,
                    url_num=i,
                    log=log,
                )

                sport, event, logo, ts = (
                    ev["sport"],
                    ev["event"],
                    ev["logo"],
                    ev["timestamp"],
                )

                key = f"[{sport}] {event} (WFTY)"

                tvg_id, pic = leagues.get_tvg_info(sport, event)

                entry = {
                    "url": url,
                    "logo": logo or pic,
                    "base": base_url,
                    "timestamp": ts,
                    "id": tvg_id or "Live.Event.us",
                }

                cached_urls[key] = entry

                if url:
                    valid_count += 1
                    urls[key] = entry

            await browser.close()

    if new_count := valid_count - cached_count:
        log.info(f"Collected and cached {new_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
