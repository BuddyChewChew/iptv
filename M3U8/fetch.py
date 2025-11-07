#!/usr/bin/env python3
import asyncio
import re
from pathlib import Path

from scrapers import (
    fstv,
    lotus,
    pixel,
    ppv,
    roxie,
    streambtw,
    streameast,
    strmd,
    strmfree,
    tvpass,
    watchfooty,
)
from scrapers.utils import get_logger, network

log = get_logger(__name__)

BASE_FILE = Path(__file__).parent / "base.m3u8"

EVENTS_FILE = Path(__file__).parent / "events.m3u8"

COMBINED_FILE = Path(__file__).parent / "TV.m3u8"


def load_base() -> tuple[list[str], int]:
    log.info("Fetching base M3U8")

    data = BASE_FILE.read_text(encoding="utf-8")

    pattern = re.compile(r'tvg-chno="(\d+)"')

    last_chnl_num = max(map(int, pattern.findall(data)), default=0)

    return data.splitlines(), last_chnl_num


async def main() -> None:
    base_m3u8, tvg_chno = load_base()

    tasks = [
        asyncio.create_task(fstv.scrape(network.client)),
        asyncio.create_task(lotus.scrape(network.client)),
        asyncio.create_task(pixel.scrape(network.client)),
        asyncio.create_task(ppv.scrape(network.client)),
        asyncio.create_task(roxie.scrape(network.client)),
        asyncio.create_task(streambtw.scrape(network.client)),
        asyncio.create_task(streameast.scrape(network.client)),
        asyncio.create_task(strmd.scrape(network.client)),
        asyncio.create_task(strmfree.scrape(network.client)),
        asyncio.create_task(tvpass.scrape(network.client)),
        asyncio.create_task(watchfooty.scrape(network.client)),
    ]

    await asyncio.gather(*tasks)

    additions = (
        fstv.urls
        | lotus.urls
        | pixel.urls
        | ppv.urls
        | roxie.urls
        | streambtw.urls
        | streameast.urls
        | strmd.urls
        | strmfree.urls
        | tvpass.urls
        | watchfooty.urls
    )

    live_events: list[str] = []

    combined_channels: list[str] = []

    for i, (event, info) in enumerate(
        sorted(additions.items()),
        start=1,
    ):
        extinf_all = (
            f'#EXTINF:-1 tvg-chno="{tvg_chno + i}" tvg-id="{info["id"]}" '
            f'tvg-name="{event}" tvg-logo="{info["logo"]}" group-title="Live Events",{event}'
        )

        extinf_live = (
            f'#EXTINF:-1 tvg-chno="{i}" tvg-id="{info["id"]}" '
            f'tvg-name="{event}" tvg-logo="{info["logo"]}" group-title="Live Events",{event}'
        )

        vlc_block = [
            f'#EXTVLCOPT:http-referrer={info["base"]}',
            f'#EXTVLCOPT:http-origin={info["base"]}',
            f"#EXTVLCOPT:http-user-agent={network.UA}",
            info["url"],
        ]

        combined_channels.extend(["\n" + extinf_all, *vlc_block])

        live_events.extend(["\n" + extinf_live, *vlc_block])

    COMBINED_FILE.write_text(
        "\n".join(base_m3u8 + combined_channels),
        encoding="utf-8",
    )

    log.info(f"Base + Events saved to {COMBINED_FILE.resolve()}")

    EVENTS_FILE.write_text(
        '#EXTM3U url-tvg="https://github.com/BuddyChewChew/iptv/raw/refs/heads/main/EPG/TV.xml"\n'
        + "\n".join(live_events),
        encoding="utf-8",
    )

    log.info(f"Events saved to {EVENTS_FILE.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())

    try:
        asyncio.run(network.client.aclose())
    except Exception:
        pass
