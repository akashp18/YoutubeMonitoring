from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.domain import INTERVAL_PRESETS, MAX_VERIFICATION_SECONDS, Availability
from app.services.channel_resolver import ChannelResolutionError, ResolvedChannel
from app.services.channel_url import (
    ChannelUrlError,
    looks_like_channel_id,
    parse_channel_url,
)
from app.services.scheduler import AlreadyRunningError
from app.timeutil import add_minutes, iso

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _state(request: Request) -> Any:
    return request.app.state


class ChannelCreate(BaseModel):
    channel_url: str
    channel_id: str | None = None
    monitoring_interval_minutes: int = Field(default=60, ge=0, le=10080)
    max_shorts_per_run: int = Field(default=40, ge=1, le=500)
    playback_verification_seconds: float = Field(default=5.0, ge=2, le=MAX_VERIFICATION_SECONDS)
    incremental_checks: bool = True
    notify_new_shorts: bool = True
    monitoring_enabled: bool = True


class ChannelUpdate(BaseModel):
    channel_url: str | None = None
    monitoring_interval_minutes: int | None = Field(default=None, ge=0, le=10080)
    max_shorts_per_run: int | None = Field(default=None, ge=1, le=500)
    playback_verification_seconds: float | None = Field(
        default=None, ge=2, le=MAX_VERIFICATION_SECONDS
    )
    incremental_checks: bool | None = None
    notify_new_shorts: bool | None = None
    monitoring_enabled: bool | None = None


class ValidateRequest(BaseModel):
    channel_url: str
    channel_id: str | None = None


class SettingsUpdate(BaseModel):
    settings: dict[str, Any]


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {
        "status": "ok",
        "time": iso(),
        "database": bool(state.repo.db.table_exists("youtube_shorts")),
        "browser_version": getattr(state.browser, "browser_version", None),
        "youtube_api": bool(state.youtube_api_key),
    }


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    state = _state(request)
    repo = state.repo
    next_due = repo.db.scalar(
        "SELECT MIN(next_run_at) FROM youtube_channels WHERE monitoring_enabled = 1 "
        "AND monitoring_interval_minutes > 0 AND next_run_at IS NOT NULL"
    )
    last_run = repo.runs.latest()
    alerts = repo.alerts.list(state="open", limit=5)
    return {
        "stats": repo.shorts.counts(),
        "channels": len(repo.channels.list()),
        "alerts_open": repo.alerts.open_count(),
        "latest_alerts": alerts["items"],
        "last_run": last_run,
        "next_run_at": next_due,
        "scheduler": state.scheduler.status(),
        "interval_presets": {str(k): v for k, v in INTERVAL_PRESETS.items()},
        "last_24h": repo.results.stats_since(add_minutes(-24 * 60)),
        "config": {
            "youtube_api_key_configured": bool(state.youtube_api_key),
            "notifications_configured": state.notifications.channel_configured(),
            "browser_version": getattr(state.browser, "browser_version", None),
            "evidence_dir": str(state.evidence_dir),
        },
    }


@router.get("/channels")
async def list_channels(request: Request) -> dict[str, Any]:
    repo = _state(request).repo
    rows = repo.db.query(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM youtube_shorts s WHERE s.channel_id = c.id AND s.is_listed = 1) "
        "AS shorts_count, "
        "(SELECT COUNT(*) FROM youtube_shorts s WHERE s.channel_id = c.id AND s.is_listed = 1 "
        "AND s.last_availability_status IS NOT NULL AND s.last_availability_status <> 'PASS') "
        "AS problem_count, "
        "(SELECT COUNT(*) FROM youtube_shorts s WHERE s.channel_id = c.id AND s.is_listed = 0) "
        "AS delisted_count, "
        "(SELECT MAX(completed_at) FROM monitoring_runs r WHERE r.channel_id = c.id) AS last_run_at "
        "FROM youtube_channels c ORDER BY c.id ASC"
    )
    return {"items": rows, "total": len(rows)}


@router.post("/channels/validate")
async def validate_channel(request: Request, payload: ValidateRequest) -> dict[str, Any]:
    state = _state(request)
    try:
        parsed = parse_channel_url(payload.channel_url)
    except ChannelUrlError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code}
    if payload.channel_id and not looks_like_channel_id(payload.channel_id):
        return {
            "ok": False,
            "error": "A channel ID must start with 'UC' and contain 24 characters.",
            "code": "invalid_channel_id",
        }
    try:
        if payload.channel_id:
            resolved = await state.resolver.resolve_id(payload.channel_id.strip())
        else:
            resolved = await state.resolver.resolve(parsed)
    except ChannelResolutionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "code": exc.code,
            "parsed": parsed.as_dict(),
        }
    return {"ok": True, "parsed": parsed.as_dict(), "resolved": resolved.as_dict()}


@router.post("/channels", status_code=201)
async def create_channel(request: Request, payload: ChannelCreate) -> dict[str, Any]:
    state = _state(request)
    repo = state.repo
    try:
        parsed = parse_channel_url(payload.channel_url)
    except ChannelUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved = None
    error = None
    try:
        if payload.channel_id:
            if not looks_like_channel_id(payload.channel_id):
                raise HTTPException(status_code=400, detail="Invalid channel ID.")
            resolved = await state.resolver.resolve_id(payload.channel_id.strip())
        else:
            resolved = await state.resolver.resolve(parsed)
    except ChannelResolutionError as exc:
        error = str(exc)
        if payload.channel_id and looks_like_channel_id(payload.channel_id):
            # A supplied channel id is authoritative even if metadata is blocked.
            supplied_id = payload.channel_id.strip()
            resolved = ResolvedChannel(
                channel_id=supplied_id,
                channel_name=None,
                handle=None,
                canonical_url=f"https://www.youtube.com/channel/{supplied_id}",
                shorts_url=f"https://www.youtube.com/channel/{supplied_id}/shorts",
                source="input",
            )
        else:
            raise HTTPException(status_code=502, detail=error) from exc

    existing = repo.channels.find_by_youtube_id(resolved.channel_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"That channel is already monitored (channel #{existing['id']}).",
        )
    channel = repo.channels.create(
        {
            "channel_url": payload.channel_url.strip(),
            "canonical_url": resolved.canonical_url,
            "channel_id": resolved.channel_id,
            "channel_name": resolved.channel_name,
            "handle": resolved.handle,
            "shorts_url": resolved.shorts_url,
            "monitoring_enabled": 1 if payload.monitoring_enabled else 0,
            "monitoring_interval_minutes": payload.monitoring_interval_minutes,
            "max_shorts_per_run": payload.max_shorts_per_run,
            "playback_verification_seconds": payload.playback_verification_seconds,
            "incremental_checks": 1 if payload.incremental_checks else 0,
            "notify_new_shorts": 1 if payload.notify_new_shorts else 0,
            "resolution_status": "resolved",
            "resolution_error": None,
            "next_run_at": (
                add_minutes(payload.monitoring_interval_minutes)
                if payload.monitoring_interval_minutes > 0
                else None
            ),
        }
    )
    if error:
        channel["resolution_warning"] = error
    if resolved.channel_name is None:
        channel["resolution_warning"] = (
            "Channel details could not be read; monitoring will resolve them on the first run."
        )
    return channel


@router.patch("/channels/{channel_id}")
async def update_channel(request: Request, channel_id: int, payload: ChannelUpdate) -> dict[str, Any]:
    state = _state(request)
    repo = state.repo
    channel = repo.channels.get(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    values: dict[str, Any] = {}
    if payload.monitoring_enabled is not None:
        values["monitoring_enabled"] = 1 if payload.monitoring_enabled else 0
    if payload.monitoring_interval_minutes is not None:
        values["monitoring_interval_minutes"] = payload.monitoring_interval_minutes
    if payload.max_shorts_per_run is not None:
        values["max_shorts_per_run"] = payload.max_shorts_per_run
    if payload.playback_verification_seconds is not None:
        values["playback_verification_seconds"] = payload.playback_verification_seconds
    if payload.incremental_checks is not None:
        values["incremental_checks"] = 1 if payload.incremental_checks else 0
    if payload.notify_new_shorts is not None:
        values["notify_new_shorts"] = 1 if payload.notify_new_shorts else 0

    if payload.channel_url and payload.channel_url.strip() != channel["channel_url"]:
        try:
            parsed = parse_channel_url(payload.channel_url)
        except ChannelUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            resolved = await state.resolver.resolve(parsed)
        except ChannelResolutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        duplicate = repo.channels.find_by_youtube_id(resolved.channel_id)
        if duplicate and duplicate["id"] != channel_id:
            raise HTTPException(status_code=409, detail="That channel is already monitored.")
        if channel["channel_id"] and channel["channel_id"] != resolved.channel_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "That URL resolves to a different YouTube channel. Add it as a new channel "
                    "instead so the existing monitoring history stays attached to the right channel."
                ),
            )
        values.update(
            {
                "channel_url": payload.channel_url.strip(),
                "canonical_url": resolved.canonical_url,
                "channel_id": resolved.channel_id,
                "channel_name": resolved.channel_name or channel["channel_name"],
                "handle": resolved.handle,
                "shorts_url": resolved.shorts_url,
                "resolution_status": "resolved",
                "resolution_error": None,
            }
        )

    updated = repo.channels.update(channel_id, values)

    enabled = bool((updated or channel)["monitoring_enabled"])
    interval = int((updated or channel)["monitoring_interval_minutes"] or 0)
    if payload.monitoring_enabled is not None or payload.monitoring_interval_minutes is not None:
        repo.channels.set_next_run(
            channel_id, add_minutes(interval) if enabled and interval > 0 else None
        )
    return repo.channels.get(channel_id) or {}


@router.delete("/channels/{channel_id}")
async def delete_channel(request: Request, channel_id: int) -> dict[str, Any]:
    repo = _state(request).repo
    if not repo.channels.get(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found.")
    screenshots = repo.db.query(
        "SELECT e.screenshot_path FROM monitoring_evidence e "
        "JOIN youtube_shorts s ON s.id = e.short_id "
        "WHERE s.channel_id = ? AND e.screenshot_path IS NOT NULL",
        (channel_id,),
    )
    repo.channels.delete(channel_id)
    for row in screenshots:
        try:
            Path(row["screenshot_path"]).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove evidence file %s", row["screenshot_path"])
    return {"deleted": True}


@router.post("/channels/{channel_id}/resolve")
async def resolve_channel(request: Request, channel_id: int) -> dict[str, Any]:
    state = _state(request)
    channel = state.repo.channels.get(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    try:
        parsed = parse_channel_url(channel["channel_url"])
        resolved = await state.resolver.resolve(parsed)
    except (ChannelUrlError, ChannelResolutionError) as exc:
        state.repo.channels.set_resolution(channel_id, None, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    state.repo.channels.set_resolution(channel_id, resolved)
    return state.repo.channels.get(channel_id) or {}


@router.post("/channels/{channel_id}/run")
async def run_channel(request: Request, channel_id: int) -> dict[str, Any]:
    state = _state(request)
    if not state.repo.channels.get(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found.")
    try:
        run = await state.scheduler.request_run(channel_id)
    except AlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.get("/shorts")
async def list_shorts(
    request: Request,
    filter: str = Query(default="all"),
    q: str | None = None,
    channel_id: int | None = None,
    sort: str = Query(default="last_checked_desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    allowed = {
        "all",
        "accessible",
        "restricted",
        "failed",
        "inconclusive",
        "warning",
        "new",
        "changed",
        "unchecked",
        "delisted",
    }
    if filter not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown filter '{filter}'.")
    return _state(request).repo.shorts.list(
        filter_name=filter,
        search=q,
        channel_id=channel_id,
        limit=limit,
        offset=offset,
        sort=sort,
    )


@router.get("/shorts/{short_id}")
async def get_short(request: Request, short_id: int) -> dict[str, Any]:
    repo = _state(request).repo
    short = repo.shorts.get(short_id)
    if not short:
        raise HTTPException(status_code=404, detail="Short not found.")
    latest = repo.results.latest_for_short(short_id)
    previous = repo.results.previous_for_short(short_id, latest["id"]) if latest else None
    history = repo.results.history(short_id, limit=100)
    evidence = repo.evidence.for_result(latest["id"]) if latest else None
    alerts = repo.db.query(
        "SELECT * FROM monitoring_alerts WHERE short_id = ? ORDER BY id DESC LIMIT 50",
        (short_id,),
    )
    return {
        "short": short,
        "latest": latest,
        "previous": previous,
        "history": history,
        "evidence": evidence,
        "alerts": alerts,
    }


@router.post("/shorts/{short_id}/check")
async def check_short(request: Request, short_id: int) -> dict[str, Any]:
    state = _state(request)
    short = state.repo.shorts.get(short_id)
    if not short:
        raise HTTPException(status_code=404, detail="Short not found.")
    if not short["is_listed"]:
        raise HTTPException(
            status_code=400,
            detail="This Short is no longer listed on the channel; a re-check cannot be queued.",
        )
    try:
        run = await state.scheduler.request_run(
            short["channel_id"], scope="video", target_short_id=short_id, trigger="recheck"
        )
    except AlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return _state(request).repo.runs.list(limit=limit, offset=offset)


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> dict[str, Any]:
    repo = _state(request).repo
    run = repo.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {
        "run": run,
        "results": repo.results.for_run(run_id),
        "logs": repo.run_logs.list(run_id, limit=500),
        "evidence": repo.evidence.for_run(run_id),
        "channel": repo.channels.get(run["channel_id"]) if run["channel_id"] else None,
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: int) -> dict[str, Any]:
    state = _state(request)
    if not state.repo.runs.get(run_id):
        raise HTTPException(status_code=404, detail="Run not found.")
    accepted = await state.scheduler.cancel(run_id)
    if not accepted:
        raise HTTPException(
            status_code=409, detail="That run has already finished and cannot be cancelled."
        )
    return {"cancelled": True, "run": state.repo.runs.get(run_id)}


@router.get("/alerts")
async def list_alerts(
    request: Request,
    state_filter: str = Query(default="open", alias="state"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if state_filter not in {"open", "resolved", "all"}:
        raise HTTPException(status_code=400, detail="state must be open, resolved or all")
    return _state(request).repo.alerts.list(state=state_filter, limit=limit, offset=offset)


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(request: Request, alert_id: int) -> dict[str, Any]:
    repo = _state(request).repo
    if not repo.alerts.acknowledge(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found.")
    return repo.alerts.get(alert_id) or {}


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(request: Request, alert_id: int) -> dict[str, Any]:
    repo = _state(request).repo
    if not repo.alerts.resolve(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found.")
    return repo.alerts.get(alert_id) or {}


@router.get("/evidence/{evidence_id}")
async def get_evidence(request: Request, evidence_id: int) -> dict[str, Any]:
    evidence = _state(request).repo.evidence.get(evidence_id)
    if not evidence:
        raise HTTPException(status_code=404, detail="Evidence not found.")
    screenshot = evidence.get("screenshot_path")
    evidence["screenshot_url"] = (
        f"/api/evidence/{evidence_id}/screenshot" if screenshot and Path(screenshot).exists() else None
    )
    return evidence


@router.get("/evidence/{evidence_id}/screenshot")
async def get_screenshot(request: Request, evidence_id: int) -> FileResponse:
    evidence = _state(request).repo.evidence.get(evidence_id)
    if not evidence or not evidence.get("screenshot_path"):
        raise HTTPException(status_code=404, detail="No screenshot stored for this check.")
    path = Path(evidence["screenshot_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="The screenshot has expired and was removed.")
    return FileResponse(path, media_type="image/png", filename=path.name)


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {
        "settings": state.settings_store.public_view(),
        "environment": {
            "youtube_api_key_configured": bool(state.youtube_api_key),
            "smtp_configured": bool(state.app_settings.smtp_host and state.app_settings.smtp_from),
            "database_path": str(state.app_settings.database_path),
            "evidence_dir": str(state.app_settings.evidence_dir),
            "browser_version": getattr(state.browser, "browser_version", None),
            "app_base_url": str(request.base_url).rstrip("/"),
        },
    }


@router.put("/settings")
async def update_settings(request: Request, payload: SettingsUpdate) -> dict[str, Any]:
    state = _state(request)
    values = dict(payload.settings)
    current = state.settings_store.public_view()
    if "notifications.webhook_url" in values:
        supplied = str(values["notifications.webhook_url"] or "")
        if supplied == current.get("notifications.webhook_url") or not supplied:
            values.pop("notifications.webhook_url")
    try:
        updated = state.settings_store.set_many(values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"updated": updated, "settings": state.settings_store.public_view()}


@router.post("/settings/test-notification")
async def test_notification(request: Request) -> dict[str, Any]:
    ok, message = await _state(request).notifications.send_test()
    return {"ok": ok, "message": message}


@router.post("/settings/cleanup-evidence")
async def cleanup_evidence(request: Request) -> dict[str, Any]:
    removed = _state(request).history.purge_expired_evidence()
    return {"removed": removed}


@router.get("/scheduler")
async def scheduler_status(request: Request) -> dict[str, Any]:
    return _state(request).scheduler.status()


@router.post("/scheduler/pause")
async def pause_scheduler(request: Request) -> dict[str, Any]:
    return _state(request).scheduler.set_paused(True)


@router.post("/scheduler/resume")
async def resume_scheduler(request: Request) -> dict[str, Any]:
    status = _state(request).scheduler.set_paused(False)
    await _state(request).scheduler.kick()
    return status


@router.post("/scheduler/run-due")
async def run_due(request: Request) -> dict[str, Any]:
    created = await _state(request).scheduler.run_due_now()
    return {"created": created, "count": len(created)}
