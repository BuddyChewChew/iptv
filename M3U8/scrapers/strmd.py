import asyncio
import re
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from playwright.async_api import BrowserContext, async_playwright

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

API_FILE = Cache(Path(__file__).parent / "caches" / "strmd_api.json", exp=28_800)

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "strmd.json", exp=10_800)

MIRRORS = ["https://streamed.pk", "https://streami.su", "https://streamed.st"]


def validate_category(s: str) -> str:
    if "-" in s:
        return " ".join(i.capitalize() for i in s.split("-"))

    elif s == "fight":
        return "Fight (UFC/Boxing)"

    return s.capitalize() if len(s) >= 4 else s.upper()


async def refresh_api_cache(
    client: httpx.AsyncClient, url: str
) -> list[dict[str, Any]]:
    log.info("Refreshing API cache")

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{url}": {e}')
        return {}

    data = r.json()

    data[-1]["timestamp"] = Time.now().timestamp()

    return data


async def process_event(
    url: str,
    url_num: int,
    context: BrowserContext,
    timeout: int | float = 10,
) -> str | None:

    page = await context.new_page()

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=15_000,
        )

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await page.click(".jw-icon-display")

            await asyncio.wait_for(wait_task, timeout=timeout)
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
            return captured[0]

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
        api_data = await refresh_api_cache(
            client,
            urljoin(
                base_url,
                "api/matches/all-today",
            ),
        )

        API_FILE.write(api_data)

    events: list[dict[str, str]] = []

    now = Time.clean(Time.now())
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)
    pattern = re.compile(r"[\n\r]+|\s{2,}")

    for event in api_data:
        category = event["category"]

        if category == "other":
            continue

        if not (ts := event["date"]):
            continue

        start_ts = int(str(ts)[:-3])

        event_dt = Time.from_ts(start_ts)

        if not start_dt <= event_dt <= end_dt:
            continue

        sport = validate_category(category)

        parts = pattern.split(event["title"].strip())
        name = " | ".join(p.strip() for p in parts if p.strip())

        logo = urljoin(base_url, poster) if (poster := event.get("poster")) else None

        key = f"[{sport}] {name} (STRMD)"

        if cached_keys & {key}:
            continue

        sources: list[dict[str, str]] = event["sources"]

        if not sources:
            continue

        source = sources[1] if len(sources) > 1 else sources[0]
        source_type = source.get("source")
        stream_id = source.get("id")

        if not (source_type and stream_id):
            continue

        events.append(
            {
                "sport": sport,
                "event": name,
                "link": f"https://embedsports.top/embed/{source_type}/{stream_id}/1",
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

    if not (base_url := await network.get_base(MIRRORS)):
        log.warning("No working PPV mirrors")
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

                if url:
                    sport, event, logo, ts = (
                        ev["sport"],
                        ev["event"],
                        ev["logo"],
                        ev["timestamp"],
                    )

                    key = f"[{sport}] {event} (STRMD)"

                    tvg_id, pic = leagues.get_tvg_info(sport, event)

                    entry = {
                        "url": url,
                        "logo": logo or pic,
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
