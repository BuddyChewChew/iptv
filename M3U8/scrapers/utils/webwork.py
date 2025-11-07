import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TypeVar

import httpx
from playwright.async_api import Browser, BrowserContext, Playwright, Request

from .logger import get_logger

T = TypeVar("T")


class Network:
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
    )

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=5,
            follow_redirects=True,
            headers={"User-Agent": Network.UA},
            http2=True,
        )

        self._logger = get_logger("network")

    async def check_status(self, url: str) -> bool:
        try:
            r = await self.client.get(url)
            r.raise_for_status()
            return r.status_code == 200
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            self._logger.debug(f"Status check failed for {url}: {e}")
            return False

    async def get_base(self, mirrors: list[str]) -> str | None:
        tasks = [self.check_status(link) for link in mirrors]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        working_mirrors = [
            mirror for mirror, success in zip(mirrors, results) if success
        ]

        return working_mirrors[0] if working_mirrors else None

    @staticmethod
    async def safe_process(
        fn: Callable[[], Awaitable[T]],
        url_num: int,
        timeout: int | float = 15,
        log: logging.Logger | None = None,
    ) -> T | None:

        if not log:
            log = logging.getLogger(__name__)

        task = asyncio.create_task(fn())

        try:
            return await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(f"URL {url_num}) Timed out after {timeout}s, skipping event")

            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.debug(f"URL {url_num}) Ignore exception after timeout: {e}")

            return None
        except Exception as e:
            log.error(f"URL {url_num}) Unexpected error: {e}")
            return None

    @staticmethod
    def capture_req(
        req: Request,
        captured: list[str],
        got_one: asyncio.Event,
    ) -> None:

        invalids = ["amazonaws", "knitcdn"]

        escaped = [re.escape(i) for i in invalids]

        pattern = re.compile(
            rf"^(?!.*({'|'.join(escaped)})).*\.m3u8",
            re.IGNORECASE,
        )

        if pattern.search(req.url):
            captured.append(req.url)
            got_one.set()

    async def process_event(
        self,
        url: str,
        url_num: int,
        context: BrowserContext,
        timeout: int | float = 10,
        log: logging.Logger | None = None,
    ) -> str | None:

        page = await context.new_page()

        captured: list[str] = []

        got_one = asyncio.Event()

        handler = partial(
            self.capture_req,
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

    @staticmethod
    async def browser(
        playwright: Playwright,
        browser: str = "firefox",
        ignore_https_errors: bool = False,
    ) -> tuple[Browser, BrowserContext]:

        if browser == "brave":
            brwsr = await playwright.chromium.connect_over_cdp("http://localhost:9222")
            context = brwsr.contexts[0]
        else:
            brwsr = await playwright.firefox.launch(headless=True)

            context = await brwsr.new_context(
                user_agent=Network.UA,
                ignore_https_errors=ignore_https_errors,
                viewport={"width": 1366, "height": 768},
                device_scale_factor=1,
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="dark",
                permissions=["geolocation"],
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                },
            )

            await context.add_init_script(
                """
            Object.defineProperty(navigator, "webdriver", { get: () => undefined });

            Object.defineProperty(navigator, "languages", {
            get: () => ["en-US", "en"],
            });

            Object.defineProperty(navigator, "plugins", {
            get: () => [1, 2, 3, 4],
            });

            const elementDescriptor = Object.getOwnPropertyDescriptor(
            HTMLElement.prototype,
            "offsetHeight"
            );

            Object.defineProperty(HTMLDivElement.prototype, "offsetHeight", {
            ...elementDescriptor,
            get: function () {
                if (this.id === "modernizr") {
                return 24;
                }
                return elementDescriptor.get.apply(this);
            },
            });

            Object.defineProperty(window.screen, "width", { get: () => 1366 });
            Object.defineProperty(window.screen, "height", { get: () => 768 });

            const getParameter = WebGLRenderingContext.prototype.getParameter;

            WebGLRenderingContext.prototype.getParameter = function (param) {
            if (param === 37445) return "Intel Inc."; //  UNMASKED_VENDOR_WEBGL
            if (param === 37446) return "Intel Iris OpenGL    Engine"; // UNMASKED_RENDERER_WEBGL
            return getParameter.apply(this, [param]);
            };

            const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                if (node.tagName === "IFRAME" && node.hasAttribute("sandbox")) {
                    node.removeAttribute("sandbox");
                }
                });
            });
            });

            observer.observe(document.documentElement, { childList: true, subtree: true });

            """
            )

        return brwsr, context


network = Network()

__all__ = ["network"]
