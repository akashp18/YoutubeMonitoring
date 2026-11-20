"""Playwright browser lifecycle, page adapter and discovery session."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from app.settings_store import SettingsStore
from app.services.page_scripts import DOM_SCAN, INIT_COLLECTOR, SCROLL_DOWN
from app.services.youtube_parse import iter_shorts_from_data

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}


class BrowserUnavailableError(RuntimeError):
    """Raised when the Playwright browser cannot be started."""


class PageAdapter(Protocol):
    """Minimal page surface used by the monitor (easy to fake in tests)."""

    async def goto(self, url: str, timeout_ms: int) -> int | None: ...

    async def evaluate(self, expression: str, arg: Any | None = None) -> Any: ...

    async def screenshot(self, path: str) -> None: ...

    async def mouse_click(self, x: float, y: float) -> None: ...

    async def close(self) -> None: ...

    def extra_evidence(self) -> dict[str, Any]: ...


class PlaywrightPageAdapter:
    def __init__(self, page: Any, browser_version: str | None = None) -> None:
        self.page = page
        self.browser_version = browser_version
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[dict[str, Any]] = []
        self._attach()

    def _attach(self) -> None:
        page = self.page

        def on_console(message: Any) -> None:
            try:
                if message.type in {"error", "warning"} and len(self.console_errors) < 30:
                    text = (message.text or "").strip()
                    if text:
                        self.console_errors.append(text[:500])
            except Exception:  # noqa: BLE001 - diagnostics must never break a check
                pass

        def on_page_error(error: Any) -> None:
            try:
                if len(self.page_errors) < 30:
                    self.page_errors.append(str(error)[:500])
            except Exception:  # noqa: BLE001
                pass

        def on_request_failed(request: Any) -> None:
            try:
                if len(self.failed_requests) < 30:
                    failure = getattr(request, "failure", None)
                    self.failed_requests.append(
                        {
                            "url": (request.url or "")[:300],
                            "resource_type": getattr(request, "resource_type", None),
                            "error": (failure or "request failed")[:200]
                            if isinstance(failure, str)
                            else str(failure)[:200],
                        }
                    )
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("console", on_console)
            page.on("pageerror", on_page_error)
            page.on("requestfailed", on_request_failed)
        except Exception:  # noqa: BLE001
            logger.debug("Could not attach diagnostics listeners", exc_info=True)

    async def goto(self, url: str, timeout_ms: int) -> int | None:
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return response.status if response is not None else None

    async def evaluate(self, expression: str, arg: Any | None = None) -> Any:
        if arg is None:
            return await self.page.evaluate(expression)
        return await self.page.evaluate(expression, arg)

    async def screenshot(self, path: str) -> None:
        await self.page.screenshot(path=path, full_page=False, timeout=15000)

    async def mouse_click(self, x: float, y: float) -> None:
        await self.page.mouse.click(x, y)

    async def close(self) -> None:
        try:
            await self.page.close()
        except Exception:  # noqa: BLE001 - page may already be closed
            pass

    def extra_evidence(self) -> dict[str, Any]:
        return {
            "console_errors": list(self.console_errors),
            "page_errors": list(self.page_errors),
            "failed_requests": list(self.failed_requests),
            "browser_version": self.browser_version,
        }


class DiscoverySession(Protocol):
    async def open(self, url: str, timeout_ms: int) -> None: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def scroll(self, dy: int) -> None: ...

    async def wait(self, ms: int) -> None: ...

    async def close(self) -> None: ...


class PlaywrightDiscoverySession:
    """Scrolls a channel Shorts tab and harvests items as they load."""

    def __init__(self, page: Any, adapter: PlaywrightPageAdapter | None = None) -> None:
        self.page = page
        self._payloads: list[Any] = []
        self._initial: Any = None
        self._html: str | None = None
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        try:
            page.on("response", self._on_response)
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, response: Any) -> None:
        try:
            url = response.url or ""
        except Exception:  # noqa: BLE001
            return
        if "/youtubei/v1/" not in url and "/youtube.com/@" not in url and "/youtube.com/channel/" not in url:
            return
        if "browse" not in url and "reel" not in url and "search" not in url:
            return
        try:
            task = asyncio.create_task(self._capture(response))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, response: Any) -> None:
        try:
            if len(self._payloads) >= 40:
                return
            content_type = (response.headers or {}).get("content-type", "")
            if "json" not in content_type:
                return
            data = await response.json()
            if isinstance(data, dict):
                self._payloads.append(data)
        except Exception:  # noqa: BLE001 - payload sniffing is best effort
            return

    async def open(self, url: str, timeout_ms: int) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await self.page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._initial = await self.page.evaluate("() => window.ytInitialData || null")
        except Exception:  # noqa: BLE001
            self._initial = None

    async def snapshot(self) -> dict[str, Any]:
        try:
            dom = await self.page.evaluate(DOM_SCAN)
        except Exception:  # noqa: BLE001
            dom = {"items": [], "scrollHeight": 0, "scrollY": 0, "viewport": 0}
        items: dict[str, dict[str, Any]] = {}
        for payload in [self._initial] + list(self._payloads):
            for item in iter_shorts_from_data(payload):
                items.setdefault(item["video_id"], item)
        for item in dom.get("items") or []:
            existing = items.get(item.get("video_id"))
            if existing:
                existing["title"] = existing.get("title") or item.get("title")
                existing["thumbnail_url"] = existing.get("thumbnail_url") or item.get("thumbnail_url")
            else:
                items[item["video_id"]] = item
        return {
            "items": list(items.values()),
            "scrollHeight": dom.get("scrollHeight") or 0,
            "scrollY": dom.get("scrollY") or 0,
            "viewport": dom.get("viewport") or 0,
        }

    async def scroll(self, dy: int) -> None:
        try:
            await self.page.evaluate(SCROLL_DOWN, dy)
        except Exception:  # noqa: BLE001
            try:
                await self.page.mouse.wheel(0, dy)
            except Exception:  # noqa: BLE001
                pass

    async def wait(self, ms: int) -> None:
        try:
            await self.page.wait_for_timeout(ms)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(ms / 1000)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        try:
            await self.page.close()
        except Exception:  # noqa: BLE001
            pass


class BrowserManager:
    """Owns one Chromium instance and one reusable, unauthenticated context."""

    def __init__(self, settings_store: SettingsStore) -> None:
        self.settings = settings_store
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()
        self._version: str | None = None

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.get_bool("browser.headless", True),
                args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
            )
            try:
                self._version = self._browser.version
            except Exception:  # noqa: BLE001
                self._version = None
        except Exception as exc:  # noqa: BLE001
            await self._teardown()
            raise BrowserUnavailableError(
                "Playwright Chromium could not be started. Run `playwright install chromium`. "
                f"({exc})"
            ) from exc

    async def _new_context(self) -> Any:
        locale = self.settings.get_str("browser.locale", "en-US") or "en-US"
        user_agent = self.settings.get_str("browser.user_agent", "")
        options: dict[str, Any] = {
            "locale": locale,
            "viewport": DEFAULT_VIEWPORT,
            "timezone_id": "UTC",
            "accept_downloads": False,
            "java_script_enabled": True,
        }
        if user_agent:
            options["user_agent"] = user_agent
        context = await self._browser.new_context(**options)
        context.set_default_timeout(self.settings.get_int("browser.page_timeout_ms", 25000))
        await context.add_init_script(script=INIT_COLLECTOR)
        return context

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._browser is not None and self._context is not None:
                try:
                    if self._browser.is_connected() and not self._context_is_closed():
                        return
                except Exception:  # noqa: BLE001
                    pass
            if self._browser is None:
                await self._launch()
            if self._context is None or self._context_is_closed():
                self._context = await self._new_context()

    def _context_is_closed(self) -> bool:
        try:
            return bool(self._context and getattr(self._context, "_closed", False))
        except Exception:  # noqa: BLE001
            return False

    @property
    def browser_version(self) -> str | None:
        return self._version

    async def new_page(self) -> PlaywrightPageAdapter:
        await self.ensure_started()
        assert self._context is not None
        page = await self._context.new_page()
        return PlaywrightPageAdapter(page, self._version)

    async def open_discovery_session(self) -> DiscoverySession:
        await self.ensure_started()
        assert self._context is not None
        page = await self._context.new_page()
        return PlaywrightDiscoverySession(page)

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        for target in ("_context", "_browser"):
            obj = getattr(self, target)
            if obj is None:
                continue
            try:
                await obj.close()
            except Exception:  # noqa: BLE001
                pass
            setattr(self, target, None)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
