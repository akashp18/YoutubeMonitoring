from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import STATIC_DIR, Settings, get_settings
from app.db import Database
from app.repositories import Repository
from app.services.browser import BrowserManager
from app.services.change_detection import StatusChangeDetector
from app.services.channel_resolver import BrowserFetcher, ChannelResolver, HttpxFetcher
from app.services.history import MonitoringHistoryService
from app.services.notifications import NotificationService
from app.services.runner import MonitoringRunner
from app.services.scheduler import MonitoringScheduler
from app.services.shorts_discovery import ShortsDiscoveryService, ShortsUrlProbe
from app.services.youtube_api import YouTubeDataAPI
from app.settings_store import SettingsStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("youtube_shorts_monitor")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    db = Database(app_settings.database_path)
    applied = db.migrate()
    if applied:
        logger.info("Applied database migrations: %s", ", ".join(applied))

    repo = Repository(db)
    settings_store = SettingsStore(db)

    browser = BrowserManager(settings_store)
    resolver = ChannelResolver(
        fetcher=HttpxFetcher(), fallback_fetcher=BrowserFetcher(browser)
    )
    youtube_api = (
        YouTubeDataAPI(app_settings.youtube_api_key) if app_settings.youtube_api_key else None
    )
    discovery = ShortsDiscoveryService(
        browser_manager=browser,
        api=youtube_api,
        max_scrolls=settings_store.get_int("browser.discovery_max_scrolls", 12),
        scroll_delay_ms=settings_store.get_int("browser.discovery_scroll_delay_ms", 900),
        page_timeout_ms=settings_store.get_int("browser.page_timeout_ms", 25000),
        probe=ShortsUrlProbe(),
    )
    history = MonitoringHistoryService(repo, app_settings.evidence_dir)
    detector = StatusChangeDetector(
        settings_store.get_int("notifications.playback_regression_threshold_ms", 4000)
    )
    notifications = NotificationService(repo, settings_store, app_settings)
    runner = MonitoringRunner(
        repo=repo,
        resolver=resolver,
        discovery=discovery,
        history=history,
        detector=detector,
        notifications=notifications,
        settings_store=settings_store,
        browser_manager=browser,
        evidence_dir=app_settings.evidence_dir,
    )
    scheduler = MonitoringScheduler(repo, runner, settings_store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await scheduler.start()
        logger.info("YouTube Shorts monitor started (database: %s)", app_settings.database_path)
        try:
            yield
        finally:
            await scheduler.stop()
            await browser.close()
            logger.info("YouTube Shorts monitor stopped")

    app = FastAPI(
        title="YouTube Shorts Availability Monitor",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.repo = repo
    app.state.settings_store = settings_store
    app.state.app_settings = app_settings
    app.state.browser = browser
    app.state.resolver = resolver
    app.state.discovery = discovery
    app.state.history = history
    app.state.detector = detector
    app.state.notifications = notifications
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.youtube_api_key = app_settings.youtube_api_key
    app.state.evidence_dir = Path(app_settings.evidence_dir)

    @app.middleware("http")
    async def revalidate_static_assets(request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # The dashboard has no build step: always revalidate so edits to the
            # CSS/JS take effect instead of a stale cached copy being served.
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(router)
    static_dir = Path(STATIC_DIR)
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(static_dir / "favicon.svg")

    return app


app = create_app()
