import asyncio
import io
import ssl
import xml.etree.ElementTree as ET
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx
from playwright.async_api import BrowserContext, async_playwright

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

BASE_URL = "https://cdn.livetv861.me/rss/upcoming_en.xml"

CERT_BUNDLE_URLS = [
    "https://curl.se/ca/cacert.pem",
    "https://ssl.com/repo/certs/Cloudflare-TLS-I-E1.pem",
    "https://ssl.com/repo/certs/SSL.com-TLS-T-ECC-R2.pem",
    "https://ssl.com/repo/certs/Sectigo-AAA-Root.pem",
]

CERT_FILE = Path(__file__).parent / "caches" / "cached-cert.pem"

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "livetvsx.json", exp=10_800)


async def write_to_cert(
    client: httpx.AsyncClient,
    url: str,
    cert: Path,
) -> None:

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception:
        log.error(f"Failed to write fetch: {url} returned {r.status_code}")

    with cert.open("a", encoding="utf-8") as f:
        f.write(f"{r.text}\n")


async def refresh_cert_cache(client: httpx.AsyncClient) -> ssl.SSLContext:
    CERT_FILE.unlink(missing_ok=True)

    tasks = [write_to_cert(client, url, CERT_FILE) for url in CERT_BUNDLE_URLS]

    await asyncio.gather(*tasks)


async def get_cert(client: httpx.AsyncClient) -> ssl.SSLContext:
    if CERT_FILE.is_file():
        mtime = Time.from_ts(CERT_FILE.stat().st_mtime)

        if Time.now() - mtime < timedelta(days=30):
            return ssl.create_default_context(cafile=CERT_FILE)

    log.info("Refreshing cached certificate")

    await refresh_cert_cache(client)

    return ssl.create_default_context(cafile=CERT_FILE)


async def fetch_xml_stream(url: str, ssl_ctx: ssl.SSLContext) -> io.BytesIO | None:
    buffer = io.BytesIO()

    try:
        async with httpx.AsyncClient(
            timeout=10,
            verify=ssl_ctx,
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()

                async for chunk in r.aiter_bytes(8192):
                    buffer.write(chunk)

        buffer.seek(0)

        return buffer
    except Exception as e:
        log.error(f"Failed to fetch {url}: {e}")
        return


async def process_event(
    url: str,
    url_num: int,
    context: BrowserContext,
) -> str | None:

    page = await context.new_page()

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(network.capture_req, captured=captured, got_one=got_one)

    popup = None

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=10_000,
        )

        btn = await page.query_selector(".lnkhdr > tbody > tr > td:nth-child(2)")

        if btn:
            try:
                await btn.click()

                await page.wait_for_timeout(500)
            except Exception as e:
                log.debug(f"URL {url_num}) Failed to click Browser Links tab: {e}")
                return
        else:
            log.warning(f"URL {url_num}) Browser Links tab not found")
            return

        link_img = await page.query_selector(
            "tr:nth-child(2) > td:nth-child(1) td:nth-child(6) img"
        )

        if not link_img:
            log.warning(f"URL {url_num}) No browser link to click.")
            return

        page.on("request", handler)

        try:
            async with page.expect_popup(timeout=5_000) as popup_info:
                try:
                    await link_img.click()
                except Exception as e:
                    log.debug(f"URL {url_num}) Click failed: {e}")

            popup = await popup_info.value

            popup.on("request", handler)
        except Exception:

            try:
                await link_img.click()
            except Exception as e:
                log.debug(f"URL {url_num}) Fallback click failed: {e}")

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=15)
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

        page.remove_listener("request", handler)

        if popup:
            popup.remove_listener("request", handler)

            await popup.close()

        await page.close()

        if captured:
            log.info(f"URL {url_num}) Captured M3U8")
            return captured[-1]

        log.warning(f"URL {url_num}) No M3U8 captured")
        return

    except Exception:
        try:
            page.remove_listener("request", handler)

            if popup:
                popup.remove_listener("request", handler)

                await popup.close()

            await page.close()
        except Exception:
            pass


async def get_events(
    url: str,
    ssl_ctx: ssl.SSLContext,
    cached_keys: set[str],
) -> list[dict[str, str]]:

    events: list[dict[str, str]] = []

    now = Time.clean(Time.now())
    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    if not (buffer := await fetch_xml_stream(url, ssl_ctx)):
        return events

    for _, elem in ET.iterparse(buffer, events=("end",)):
        if elem.tag == "item":
            title = elem.findtext("title") or ""
            desc = elem.findtext("description") or ""
            pub_date = elem.findtext("pubDate") or ""
            link = elem.findtext("link") or ""

            if not all([title, pub_date, link]):
                elem.clear()
                continue

            try:
                event_dt = Time.from_str(pub_date)
            except Exception:
                elem.clear()
                continue

            if not start_dt <= event_dt <= end_dt:
                elem.clear()
                continue

            if desc:
                parts = desc.split(".")
                sport = parts[0].strip() if parts else ""
                event = parts[1].strip() if parts else ""
            else:
                sport, event = "", ""

            key = f"[{sport}: {event}] {title} (LTVSX)"

            if cached_keys & {key}:
                elem.clear()
                continue

            events.append(
                {
                    "sport": sport,
                    "event": event,
                    "title": title,
                    "link": link,
                    "timestamp": event_dt.timestamp(),
                }
            )

            elem.clear()

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    cached_urls = CACHE_FILE.load()
    cached_count = len(cached_urls)
    urls.update({k: v for k, v in cached_urls.items() if v["url"]})

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    ssl_ctx = await get_cert(client)

    if not ssl_ctx:
        log.error("Failed to create SSL context, aborting")
        CACHE_FILE.write(cached_urls)
        return

    events = await get_events(
        BASE_URL,
        ssl_ctx,
        set(cached_urls.keys()),
    )

    log.info(f"Processing {len(events)} new URL(s)")

    async with async_playwright() as p:
        browser, context = await network.browser(p, ignore_https_errors=True)

        for i, ev in enumerate(events, start=1):
            link = ev["link"]

            url = await network.safe_process(
                lambda: process_event(
                    link,
                    url_num=i,
                    context=context,
                ),
                url_num=i,
                log=log,
            )

            sport, event, title, ts = (
                ev["sport"],
                ev["event"],
                ev["title"],
                ev["timestamp"],
            )

            key = f"[{sport}: {event}] {title} (LTVSX)"

            tvg_id, logo = leagues.info(event)

            if not tvg_id:
                tvg_id, logo = leagues.info(sport)

            entry = {
                "url": url,
                "logo": logo,
                "id": tvg_id or "Live.Event.us",
                "base": "https://livetv.sx/enx/",
                "timestamp": ts,
            }

            cached_urls[key] = entry

            if url:
                urls[key] = entry

        await browser.close()

    if new_count := len(cached_urls) - cached_count:
        log.info(f"Collected and cached {new_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
