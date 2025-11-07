from functools import partial
from pathlib import Path
from urllib.parse import unquote, urljoin

import httpx
from selectolax.parser import HTMLParser

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str]] = {}

MIRRORS = [
    "https://fstv.online",
    "https://fstv.zip",
    "https://fstv.space",
    "https://fstv.us",
]

CACHE_FILE = Cache(Path(__file__).parent / "caches" / "fstv.json", exp=10_800)


async def process_event(
    client: httpx.AsyncClient,
    url: str,
    url_num: int,
) -> tuple[str, str]:

    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'URL {url_num}) Failed to fetch "{url}": {e}')

        return "", ""

    soup = HTMLParser(r.text)

    if category_links := soup.css(".common-list-category .category-item a"):
        match_name = category_links[-1].text(strip=True)
    else:
        match_name = None

    if not match_name or match_name.lower() == "vs":
        if og_title := soup.css_first("meta[property='og:title']"):
            match_name = (
                og_title.attributes.get("content", "").split(" start on")[0].strip()
            )

    if not (ifr := soup.css_first("iframe")):
        log.info(f"URL {url_num}) No M3U8 found")
        return "", ""

    if src := ifr.attributes.get("src"):
        log.info(f"URL {url_num}) Captured M3U8")
        return match_name or "", unquote(src).split("link=")[-1]


async def get_events(
    client: httpx.AsyncClient,
    base_url: str,
    cached_hrefs: set[str],
) -> list[dict[str, str]]:

    try:
        r = await client.get(base_url)
        r.raise_for_status()
    except Exception as e:
        log.error(f'Failed to fetch "{base_url}": {e}')

        return []

    soup = HTMLParser(r.text)

    events = []

    for wrpr in soup.css("div.fixtures-live-wrapper"):
        for league_block in wrpr.css(".match-table-item > .league-info-wrapper"):
            if not (
                league_name_el := league_block.css_first(".league-info a.league-name")
            ):
                continue

            full_text = league_name_el.text(strip=True)

            if "]" in full_text:
                event_name = full_text.split("]", 1)[1].strip()
            else:
                event_name = full_text

            parent_item = league_block.parent

            for game in parent_item.css(".common-table-row a[href*='/match/']"):
                if not (href := game.attributes.get("href")):
                    continue

                if cached_hrefs & {href}:
                    continue

                cached_hrefs.add(href)

                events.append(
                    {
                        "sport": event_name,
                        "link": urljoin(base_url, href),
                        "href": href,
                    }
                )

    return events


async def scrape(client: httpx.AsyncClient) -> None:
    cached_urls = CACHE_FILE.load()
    cached_hrefs = {entry["href"] for entry in cached_urls.values()}
    cached_count = len(cached_urls)
    urls.update(cached_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    if not (base_url := await network.get_base(MIRRORS)):
        log.warning("No working FSTV mirrors")
        CACHE_FILE.write(cached_urls)
        return

    log.info(f'Scraping from "{base_url}"')

    events = await get_events(
        client,
        base_url,
        cached_hrefs,
    )

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

            match_name, url = await network.safe_process(
                handler,
                url_num=i,
                log=log,
            )

            if url:
                sport = ev["sport"]

                key = (
                    f"[{sport}] {match_name} (FSTV)"
                    if match_name
                    else f"[{sport}] (FSTV)"
                )

                tvg_id, logo = leagues.info(sport)

                entry = {
                    "url": url,
                    "logo": logo,
                    "base": base_url,
                    "timestamp": now,
                    "id": tvg_id or "Live.Event.us",
                    "href": ev["href"],
                }

                urls[key] = cached_urls[key] = entry

    if new_count := len(cached_urls) - cached_count:
        log.info(f"Collected and cached {new_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
