/* ShortsWatch dashboard. Vanilla JS, no build step. */
(() => {
  "use strict";

  const state = {
    view: "overview",
    viewToken: 0,
    overview: null,
    channels: null,
    shorts: {
      filter: "all",
      q: "",
      channel_id: "",
      sort: "last_checked_desc",
      limit: 25,
      offset: 0,
      request: 0,
    },
    runs: { limit: 25, offset: 0 },
    alerts: { state: "open" },
    settings: null,
    pollTimer: null,
  };

  const VIEW_META = {
    overview: {
      title: "Overview",
      subtitle: "Availability and playback health across your monitored channels.",
    },
    shorts: {
      title: "Shorts monitoring",
      subtitle: "Every discovered Short, its latest availability and playback result.",
    },
    runs: {
      title: "Monitoring runs",
      subtitle: "Scheduled and manual jobs with their per-video outcomes.",
    },
    channels: {
      title: "Channels",
      subtitle: "Configure which channels are monitored and how often.",
    },
    alerts: {
      title: "Alerts",
      subtitle: "Meaningful status changes, deduplicated until resolved.",
    },
    settings: {
      title: "Settings",
      subtitle: "Browser, monitoring, notification and evidence preferences.",
    },
  };

  const STATUS_CLASS = {
    PASS: "ok",
    WARNING: "warn",
    RESTRICTED: "danger",
    FAILED: "danger",
    INCONCLUSIVE: "info",
    RUNNING: "info",
    QUEUED: "neutral",
    COMPLETED: "ok",
    CANCELLED: "warn",
    INTERRUPTED: "warn",
  };

  const PLAYBACK_CLASS = {
    VERIFIED: "ok",
    FAILED: "danger",
    NOT_VERIFIED: "warn",
    SKIPPED: "neutral",
  };

  const INTERVALS = {
    15: "Every 15 minutes",
    30: "Every 30 minutes",
    60: "Every hour",
    360: "Every 6 hours",
    720: "Every 12 hours",
    1440: "Daily",
    0: "Manual only",
  };

  /* ------------------------------- utilities ------------------------------ */

  const $ = (id) => document.getElementById(id);

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function apiUrl(path) {
    if (/^https?:/i.test(path)) return path;
    if (path === "/api" || path.startsWith("/api/")) return path;
    return `/api${path.startsWith("/") ? "" : "/"}${path}`;
  }

  async function api(path, options = {}) {
    const config = { headers: { "Content-Type": "application/json" }, ...options };
    const response = await fetch(apiUrl(path), config);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (body && body.detail) {
          detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch (error) { /* keep status text */ }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function fmtDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  function timeAgo(value) {
    if (!value) return "never";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    const future = seconds < 0;
    const abs = Math.abs(seconds);
    let text;
    if (abs < 45) text = `${abs}s`;
    else if (abs < 3600) text = `${Math.round(abs / 60)}m`;
    else if (abs < 86400) text = `${Math.round(abs / 3600)}h`;
    else text = `${Math.round(abs / 86400)}d`;
    return future ? `in ${text}` : `${text} ago`;
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "—";
    const value = Number(ms);
    if (Number.isNaN(value)) return "—";
    if (value < 1000) return `${Math.round(value)} ms`;
    return `${(value / 1000).toFixed(1)} s`;
  }

  function fmtDuration(ms) {
    if (ms === null || ms === undefined) return "—";
    const seconds = Math.round(Number(ms) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  function statusPill(status) {
    if (!status) return '<span class="pill neutral">Not checked</span>';
    return `<span class="pill ${STATUS_CLASS[status] || "neutral"}">${esc(status)}</span>`;
  }

  function playbackPill(status) {
    if (!status) return '<span class="pill neutral">—</span>';
    return `<span class="pill ${PLAYBACK_CLASS[status] || "neutral"}">${esc(
      status.replace("_", " ")
    )}</span>`;
  }

  function intervalLabel(minutes) {
    if (minutes === null || minutes === undefined) return "—";
    return INTERVALS[minutes] || `Every ${minutes} minutes`;
  }

  function toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("toasts").appendChild(node);
    setTimeout(() => node.remove(), 5200);
  }

  function showBanner(message) {
    const banner = $("banner");
    banner.textContent = message;
    banner.classList.remove("hidden");
  }

  function hideBanner() {
    $("banner").classList.add("hidden");
  }

  function loadingBlock(label = "Loading…") {
    return `<div class="loading"><span class="spinner"></span> ${esc(label)}</div>`;
  }

  function emptyBlock(title, body, actionLabel, action) {
    return `<div class="table-wrap"><div class="empty">
      <h3>${esc(title)}</h3>
      <p>${esc(body)}</p>
      ${actionLabel ? `<button class="btn primary" type="button" data-action="${esc(action)}">${esc(actionLabel)}</button>` : ""}
    </div></div>`;
  }

  /* -------------------------------- router -------------------------------- */

  async function showView(view) {
    const token = ++state.viewToken;
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    $("page-title").textContent = VIEW_META[view].title;
    $("page-subtitle").textContent = VIEW_META[view].subtitle;
    document.title = `${VIEW_META[view].title} · ShortsWatch`;
    window.scrollTo(0, 0);
    $("view").innerHTML = loadingBlock();
    try {
      await loadCurrent(token);
    } catch (error) {
      if (token !== state.viewToken) return;
      $("view").innerHTML = `<div class="card"><div class="empty">
        <h3>Could not load this view</h3><p>${esc(error.message)}</p>
        <button class="btn" data-action="reload-view" type="button">Try again</button>
      </div></div>`;
    }
  }

  function loadCurrent(token) {
    switch (state.view) {
      case "shorts": return loadShorts();
      case "runs": return loadRuns(token);
      case "channels": return loadChannelsView(token);
      case "alerts": return loadAlerts(token);
      case "settings": return loadSettings(token);
      default: return loadOverview(token);
    }
  }

  /* ------------------------------- overview ------------------------------- */

  async function loadOverview(token) {
    const data = await api("/overview");
    if (token !== undefined && token !== state.viewToken) return;
    state.overview = data;
    renderOverview(data);
    renderSchedulerPill(data.scheduler);
    updateAlertBadge(data.alerts_open);
  }

  function renderOverview(data) {
    const stats = data.stats;
    const cards = [
      { label: "Shorts discovered", value: stats.total, sub: `${stats.unchecked} never checked`, tone: "" },
      { label: "Accessible", value: stats.accessible, sub: "Playback verified", tone: "ok" },
      { label: "Restricted", value: stats.restricted, sub: "Explicit YouTube message", tone: "danger" },
      { label: "Playback failures", value: stats.failed, sub: "Load or playback failed", tone: "danger" },
      { label: "Inconclusive", value: stats.inconclusive, sub: "Could not be determined", tone: "info" },
      { label: "New (7 days)", value: stats.new_shorts, sub: `${stats.recently_changed} changed recently`, tone: "warn" },
      { label: "Warnings", value: stats.warning, sub: "Accessible, non-critical issue", tone: "warn" },
      { label: "Delisted", value: stats.delisted, sub: "No longer on the Shorts tab", tone: "" },
    ];
    const cardsHtml = cards.map((card) => `
      <div class="card">
        <div class="card-title">${esc(card.label)}</div>
        <div class="card-value ${card.tone}">${card.value}</div>
        <div class="card-sub">${esc(card.sub)}</div>
      </div>`).join("");

    const lastRun = data.last_run;
    const scheduler = data.scheduler || {};
    const runSummary = lastRun ? `
      <div class="kv">
        <dt>Last run</dt><dd>#${lastRun.id} · ${statusPill(lastRun.status)}</dd>
        <dt>Channel</dt><dd>${esc(lastRun.channel_name || "—")}</dd>
        <dt>Started</dt><dd title="${esc(lastRun.started_at || "")}">${fmtDate(lastRun.started_at)} (${timeAgo(lastRun.started_at)})</dd>
        <dt>Duration</dt><dd>${fmtDuration(lastRun.duration_ms)}</dd>
        <dt>Checked</dt><dd>${lastRun.total_checked} videos · ${lastRun.passed_count} pass · ${lastRun.restricted_count} restricted · ${lastRun.failed_count} failed · ${lastRun.inconclusive_count} inconclusive</dd>
        <dt>Discovery</dt><dd>${lastRun.discovery_new_count} new · ${lastRun.discovery_removed_count} delisted · source ${esc(lastRun.discovery_source || "—")}</dd>
        ${lastRun.error_message ? `<dt>Error</dt><dd style="color:var(--danger)">${esc(lastRun.error_message)}</dd>` : ""}
      </div>` : `<div class="notice">No monitoring run has completed yet. Add a channel and start a run.</div>`;

    const alertsHtml = (data.latest_alerts || []).length
      ? data.latest_alerts.map((alert) => alertRow(alert, false)).join("")
      : `<div class="notice">No open alerts. Problems will appear here after a status change.</div>`;

    const nextText = data.next_run_at
      ? `${fmtDate(data.next_run_at)} <span class="muted">(${timeAgo(data.next_run_at)})</span>`
      : scheduler.paused
        ? "Paused"
        : "No channel is scheduled";

    const healthHtml = `
      <div class="kv">
        <dt>Next scheduled run</dt><dd>${nextText}</dd>
        <dt>Scheduler</dt><dd>${scheduler.paused ? '<span class="pill warn">Paused</span>' : '<span class="pill ok">Active</span>'}</dd>
        <dt>Checks (24h)</dt><dd>${data.last_24h.checks}${data.last_24h.avg_playback_start_ms ? ` · avg start ${fmtMs(data.last_24h.avg_playback_start_ms)}` : ""}</dd>
        <dt>YouTube Data API</dt><dd>${data.config.youtube_api_key_configured ? '<span class="pill ok">Configured</span>' : '<span class="pill neutral">Not configured</span>'}</dd>
        <dt>Notifications</dt><dd>${data.config.notifications_configured ? '<span class="pill ok">Configured</span>' : '<span class="pill neutral">In-app only</span>'}</dd>
        <dt>Browser</dt><dd>${esc(data.config.browser_version || "Chromium (starts on first run)")}</dd>
        <dt>Monitored channels</dt><dd>${data.channels}</dd>
      </div>`;

    $("view").innerHTML = `
      ${data.channels === 0 ? `<div class="banner">No channels are configured yet. Add your channel to start monitoring.</div>` : ""}
      <div class="grid cards">${cardsHtml}</div>
      <div class="split">
        <div style="display:flex;flex-direction:column;gap:14px">
          <div class="card">
            <div class="card-head"><div class="card-title">Last monitoring run</div>
              ${lastRun ? `<button class="btn small ghost" data-action="open-run" data-id="${lastRun.id}" type="button">Open run</button>` : ""}
            </div>
            ${runSummary}
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">Monitoring configuration</div>
              <button class="btn small ghost" data-action="manage-channels" type="button">Channels</button>
            </div>
            ${healthHtml}
          </div>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">Recent alerts</div>
            <button class="btn small ghost" data-action="open-alerts" type="button">All alerts</button>
          </div>
          ${alertsHtml}
        </div>
      </div>`;
  }

  function updateAlertBadge(count) {
    const badge = $("alert-badge");
    badge.textContent = String(count || 0);
    badge.classList.toggle("hidden", !count);
  }

  /* -------------------------------- shorts -------------------------------- */

  async function ensureChannels() {
    if (state.channels) return state.channels;
    const data = await api("/channels");
    state.channels = data.items || [];
    return state.channels;
  }

  async function loadShorts() {
    const requestId = ++state.shorts.request;
    const channels = await ensureChannels();
    if (requestId !== state.shorts.request) return;
    const params = new URLSearchParams({
      filter: state.shorts.filter,
      sort: state.shorts.sort,
      limit: String(state.shorts.limit),
      offset: String(state.shorts.offset),
    });
    if (state.shorts.q) params.set("q", state.shorts.q);
    if (state.shorts.channel_id) params.set("channel_id", state.shorts.channel_id);
    const data = await api(`/shorts?${params.toString()}`);
    if (requestId !== state.shorts.request) return;
    renderShorts(data, channels);
  }

  function renderShorts(data, channels) {
    const focusedId = document.activeElement ? document.activeElement.id : "";
    const caret = focusedId === "short-search" ? $("short-search")?.selectionStart : null;
    const filters = [
      ["all", "All Shorts"],
      ["accessible", "Accessible"],
      ["restricted", "Restricted"],
      ["failed", "Failed"],
      ["inconclusive", "Inconclusive"],
      ["new", "Newly published"],
      ["changed", "Recently changed"],
      ["unchecked", "Not checked"],
      ["delisted", "Delisted"],
    ];
    const chips = filters.map(([key, label]) => `
      <button class="chip ${state.shorts.filter === key ? "active" : ""}" type="button"
        data-filter="${key}">${esc(label)}</button>`).join("");

    const channelOptions = ['<option value="">All channels</option>']
      .concat(channels.map((channel) => `<option value="${channel.id}" ${String(state.shorts.channel_id) === String(channel.id) ? "selected" : ""}>${esc(channel.channel_name || channel.channel_id || `Channel #${channel.id}`)}</option>`))
      .join("");

    const sortOptions = [
      ["last_checked_desc", "Recently checked"],
      ["last_checked_asc", "Oldest check"],
      ["discovered_desc", "Recently discovered"],
      ["title_asc", "Title A–Z"],
      ["status", "Status"],
    ].map(([value, label]) => `<option value="${value}" ${state.shorts.sort === value ? "selected" : ""}>${label}</option>`).join("");

    const rows = data.items.map((item) => shortRow(item)).join("");
    const table = data.items.length
      ? `<div class="table-wrap">
          <table>
            <thead><tr>
              <th>Short</th><th>Availability</th><th>Playback</th><th class="num" title="Time until playback started">Start</th>
              <th class="num">Last checked</th><th class="num">Changed</th><th style="text-align:right">Actions</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`
      : emptyBlock(
          channels.length ? "No Shorts match these filters" : "No channels configured",
          channels.length
            ? "Discovery has not stored a Short for this filter yet. Run a monitoring job to refresh the listing."
            : "Add a YouTube channel first; the monitor discovers its Shorts automatically.",
          channels.length ? null : "Add channel",
          "add-channel"
        );

    const from = data.total === 0 ? 0 : data.offset + 1;
    const to = Math.min(data.offset + data.limit, data.total);
    const pagination = `<div class="pagination">
        <span>Showing ${from}–${to} of ${data.total}</span>
        <span style="display:flex;gap:6px">
          <button class="btn small" type="button" data-action="page-prev" ${data.offset <= 0 ? "disabled" : ""}>Previous</button>
          <button class="btn small" type="button" data-action="page-next" ${to >= data.total ? "disabled" : ""}>Next</button>
        </span>
      </div>`;

    $("view").innerHTML = `
      <div class="toolbar">
        <div class="chips">${chips}</div>
        <div class="spacer"></div>
        <input class="input search" id="short-search" type="search" placeholder="Search title or video ID…" value="${esc(state.shorts.q)}" />
        <select class="select" id="short-channel">${channelOptions}</select>
        <select class="select" id="short-sort">${sortOptions}</select>
      </div>
      ${table}
      ${data.total ? pagination : ""}`;

    if (focusedId === "short-search") {
      const input = $("short-search");
      if (input) {
        input.focus();
        const position = caret == null ? input.value.length : caret;
        try { input.setSelectionRange(position, position); } catch (error) { /* search inputs support it */ }
      }
    }
  }

  function shortRow(item) {
    const thumb = item.thumbnail_url
      ? `<img class="thumb" src="${esc(item.thumbnail_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.outerHTML='<div class=&quot;thumb-fallback&quot;>▶</div>'" />`
      : `<div class="thumb-fallback">▶</div>`;
    const title = esc(item.title || "(untitled Short)");
    const url = item.video_url || `https://www.youtube.com/shorts/${item.video_id}`;
    return `<tr>
      <td>
        <div class="video-cell">
          ${thumb}
          <div style="min-width:0">
            <div class="video-title"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title}</a></div>
            <div class="video-meta">
              <span class="mono">${esc(item.video_id)}</span>
              ${item.is_listed ? "" : '<span class="pill neutral">delisted</span>'}
              ${item.channel_name ? `<span>· ${esc(item.channel_name)}</span>` : ""}
            </div>
          </div>
        </div>
      </td>
      <td>${statusPill(item.last_availability_status)}</td>
      <td>${playbackPill(item.last_playback_status)}</td>
      <td class="num mono">${fmtMs(item.last_playback_start_ms)}</td>
      <td class="num" title="${esc(item.last_checked_at || "")}">${timeAgo(item.last_checked_at)}</td>
      <td class="num" title="${esc(item.last_status_change_at || "")}">${item.last_status_change_at ? timeAgo(item.last_status_change_at) : "—"}</td>
      <td class="actions">
        <button class="btn small ghost" type="button" data-action="open-short-link" data-url="${esc(url)}" title="Open on YouTube">Open</button>
        <button class="btn small ghost" type="button" data-action="recheck-short" data-id="${item.id}" title="Run a fresh check">Check</button>
        <button class="btn small ghost" type="button" data-action="short-details" data-id="${item.id}" title="History, diagnostics and alerts">Details</button>
      </td>
    </tr>`;
  }

  function onShortsFilter(filter) {
    state.shorts.filter = filter;
    state.shorts.offset = 0;
    loadShorts().catch((error) => toast(error.message, "error"));
  }

  /* --------------------------------- runs --------------------------------- */

  async function loadRuns(token) {
    const params = new URLSearchParams({
      limit: String(state.runs.limit),
      offset: String(state.runs.offset),
    });
    const data = await api(`/runs?${params.toString()}`);
    if (token !== undefined && token !== state.viewToken) return;
    renderRuns(data);
  }

  function resultLine(run) {
    const parts = [
      [run.passed_count, "ok", "passed"],
      [run.warning_count, "warn", "warnings"],
      [run.restricted_count, "danger", "restricted"],
      [run.failed_count, "danger", "failed"],
      [run.inconclusive_count, "info", "inconclusive"],
    ].filter(([count]) => Number(count) > 0);
    if (!parts.length) return '<span class="muted">—</span>';
    return `<div class="result-line">${parts
      .map(([count, tone, label]) => `<span class="res ${tone}" title="${count} ${label}">${Number(count)} <span class="res-label">${label}</span></span>`)
      .join("")}</div>`;
  }

  function renderRuns(data) {
    const rows = data.items.map((run) => {
      const active = run.status === "RUNNING" || run.status === "QUEUED";
      return `<tr>
        <td class="mono">#${run.id}</td>
        <td>${esc(run.channel_name || "—")}</td>
        <td><span class="pill neutral">${esc(run.trigger_type)}${run.scope === "video" ? " · single" : ""}</span></td>
        <td>${statusPill(run.status)}</td>
        <td>${run.total_checked}<span class="muted"> / ${run.discovery_total_count || 0} discovered</span></td>
        <td>${resultLine(run)}</td>
        <td title="${esc(run.started_at || run.queued_at || "")}">${fmtDate(run.started_at || run.queued_at)}</td>
        <td>${fmtDuration(run.duration_ms)}</td>
        <td class="actions">
          <button class="btn small ghost" type="button" data-action="open-run" data-id="${run.id}">View</button>
          ${active ? `<button class="btn small danger" type="button" data-action="cancel-run" data-id="${run.id}">Cancel</button>` : ""}
        </td>
      </tr>`;
    }).join("");

    const table = data.items.length
      ? `<div class="table-wrap"><table>
          <thead><tr><th>Run</th><th>Channel</th><th>Trigger</th><th>Status</th><th>Checked</th>
          <th>Results</th><th>Started</th><th>Duration</th><th style="text-align:right">Actions</th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
      : emptyBlock("No runs yet", "A run is created when a channel becomes due or when you start one manually.", null, null);

    const from = data.total === 0 ? 0 : data.offset + 1;
    const to = Math.min(data.offset + data.limit, data.total);
    $("view").innerHTML = `
      <div class="toolbar">
        <span class="muted">Every run is logged with per-video results and diagnostics.</span>
        <div class="spacer"></div>
        <button class="btn small" type="button" data-action="run-due">Run due channels</button>
      </div>
      ${table}
      <div class="pagination ${data.total ? "" : "hidden"}">
        <span>Showing ${from}–${to} of ${data.total}</span>
        <span style="display:flex;gap:6px">
          <button class="btn small" type="button" data-action="runs-prev" ${data.offset <= 0 ? "disabled" : ""}>Previous</button>
          <button class="btn small" type="button" data-action="runs-next" ${to >= data.total ? "disabled" : ""}>Next</button>
        </span>
      </div>`;
  }

  /* ------------------------------- channels ------------------------------- */

  async function loadChannelsView(token) {
    const data = await api("/channels");
    if (token !== undefined && token !== state.viewToken) return;
    state.channels = data.items || [];
    renderChannels(state.channels);
  }

  function renderChannels(channels) {
    const cards = channels.map((channel) => `
      <div class="card">
        <div class="channel-card-head">
          <div style="min-width:0">
            <div class="channel-name">${esc(channel.channel_name || channel.channel_id || `Channel #${channel.id}`)}</div>
            <div class="channel-id mono">${esc(channel.channel_id || "unresolved")}</div>
          </div>
          ${channel.monitoring_enabled ? '<span class="pill ok">Enabled</span>' : '<span class="pill neutral">Disabled</span>'}
        </div>
        <div style="margin-top:12px" class="kv">
          <dt>URL</dt><dd><a href="${esc(channel.canonical_url || channel.channel_url)}" target="_blank" rel="noopener noreferrer" class="truncate">${esc(channel.canonical_url || channel.channel_url)}</a></dd>
          <dt>Interval</dt><dd>${esc(intervalLabel(channel.monitoring_interval_minutes))}</dd>
          <dt>Next run</dt><dd>${channel.next_run_at ? `${fmtDate(channel.next_run_at)} <span class="muted">(${timeAgo(channel.next_run_at)})</span>` : "—"}</dd>
          <dt>Shorts</dt><dd>${channel.shorts_count} listed · ${channel.problem_count} with issues · ${channel.delisted_count} delisted</dd>
          <dt>Max per run</dt><dd>${channel.max_shorts_per_run} · random 60–100% playback per Short</dd>
          <dt>Incremental</dt><dd>${channel.incremental_checks ? "Yes — PASS videos are re-checked oldest first" : "No — every listed Short each run"}</dd>
          ${channel.resolution_error ? `<dt>Resolution</dt><dd style="color:var(--danger)">${esc(channel.resolution_error)}</dd>` : ""}
        </div>
        <div class="channel-actions">
          <button class="btn small primary" type="button" data-action="run-channel" data-id="${channel.id}">Run now</button>
          <button class="btn small" type="button" data-action="edit-channel" data-id="${channel.id}">Edit</button>
          <button class="btn small" type="button" data-action="resolve-channel" data-id="${channel.id}">Re-resolve</button>
          <button class="btn small danger" type="button" data-action="delete-channel" data-id="${channel.id}">Delete</button>
        </div>
      </div>`).join("");

    $("view").innerHTML = `
      <div class="toolbar">
        <span class="muted">${channels.length} channel${channels.length === 1 ? "" : "s"} configured</span>
        <div class="spacer"></div>
        <button class="btn primary" type="button" data-action="add-channel">Add channel</button>
      </div>
      ${channels.length ? `<div class="channel-grid">${cards}</div>` : emptyBlock(
        "No channels yet",
        "Add a channel URL in any supported format: /@handle, /channel/UC…, /c/name or /user/name.",
        "Add channel",
        "add-channel"
      )}`;
  }

  /* -------------------------------- alerts -------------------------------- */

  async function loadAlerts(token) {
    const data = await api(`/alerts?state=${encodeURIComponent(state.alerts.state)}&limit=100`);
    if (token !== undefined && token !== state.viewToken) return;
    renderAlerts(data);
    const open = await api("/alerts?state=open&limit=1");
    if (token !== undefined && token !== state.viewToken) return;
    updateAlertBadge(open.total);
  }

  function alertRow(alert, withActions) {
    const severity = alert.severity || "warning";
    const actionButtons = withActions
      ? `<div style="display:flex;gap:6px;margin-top:6px">
          ${alert.acknowledged_at ? "" : `<button class="btn small ghost" type="button" data-action="ack-alert" data-id="${alert.id}">Acknowledge</button>`}
          ${alert.resolved_at ? "" : `<button class="btn small ghost" type="button" data-action="resolve-alert" data-id="${alert.id}">Resolve</button>`}
        </div>`
      : "";
    return `<div class="alert-row">
      <span class="alert-dot ${esc(severity)}"></span>
      <div class="alert-main">
        <div class="alert-title">${esc(alert.message)}</div>
        <div class="alert-sub">
          ${esc(alert.alert_type)}${alert.video_id ? ` · ${esc(alert.video_id)}` : ""}
          ${alert.previous_status ? ` · ${esc(alert.previous_status)} → ${esc(alert.current_status || "")}` : ""}
          · ${fmtDate(alert.created_at)} (${timeAgo(alert.created_at)})
          ${alert.notification_status ? ` · notification: ${esc(alert.notification_status)}` : ""}
        </div>
        ${alert.video_id ? `<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
          <a class="btn small ghost" href="https://www.youtube.com/shorts/${esc(alert.video_id)}" target="_blank" rel="noopener noreferrer">Open Short</a>
          <button class="btn small ghost" type="button" data-action="alert-video" data-id="${alert.short_id}">Monitoring history</button>
        </div>` : ""}
        ${actionButtons}
      </div>
    </div>`;
  }

  function renderAlerts(data) {
    const chips = [
      ["open", "Open"],
      ["all", "All"],
      ["resolved", "Resolved"],
    ].map(([key, label]) => `<button class="chip ${state.alerts.state === key ? "active" : ""}" type="button" data-alert-state="${key}">${label}</button>`).join("");

    $("view").innerHTML = `
      <div class="toolbar"><div class="chips">${chips}</div></div>
      <div class="card">
        ${data.items.length ? data.items.map((alert) => alertRow(alert, true)).join("") : '<div class="notice">No alerts in this view.</div>'}
      </div>`;
  }

  /* ------------------------------- settings ------------------------------- */

  async function loadSettings(token) {
    const data = await api("/settings");
    if (token !== undefined && token !== state.viewToken) return;
    state.settings = data;
    renderSettings(data);
  }

  function field(key, label, input, help) {
    return `<div class="field">
      <label for="set-${esc(key)}">${esc(label)}</label>
      ${input}
      ${help ? `<div class="help">${esc(help)}</div>` : ""}
    </div>`;
  }

  function numberInput(key, value, min, max, step) {
    return `<input class="input" id="set-${esc(key)}" data-setting="${esc(key)}" type="number"
      value="${esc(value)}" min="${min}" max="${max}" step="${step || 1}" />`;
  }

  function textInput(key, value, placeholder) {
    return `<input class="input" id="set-${esc(key)}" data-setting="${esc(key)}" type="text"
      value="${esc(value)}" placeholder="${esc(placeholder || "")}" />`;
  }

  function checkInput(key, checked, label) {
    return `<label class="check"><input type="checkbox" data-setting="${esc(key)}" ${checked ? "checked" : ""} /> ${esc(label)}</label>`;
  }

  function renderSettings(data) {
    const s = data.settings;
    const env = data.environment;
    $("view").innerHTML = `
      <div class="toolbar">
        <span class="muted">Changes apply to the next monitoring run. The browser restarts with the app.</span>
        <div class="spacer"></div>
        <button class="btn primary" type="button" data-action="save-settings">Save settings</button>
      </div>
      <div class="settings-grid">
        <div class="card">
          <div class="card-head"><div class="card-title">Browser verification</div></div>
          ${checkInput("browser.headless", s["browser.headless"], "Run Chromium headless")}
          ${checkInput("browser.mute_playback", s["browser.mute_playback"], "Mute verification playback")}
          ${checkInput("browser.screenshot_on_failure", s["browser.screenshot_on_failure"], "Capture a screenshot on non-PASS results")}
          ${field("browser.max_concurrent_checks", "Concurrent pages", numberInput("browser.max_concurrent_checks", s["browser.max_concurrent_checks"], 1, 4), "Keep this low: 1–2 pages is gentle on YouTube.")}
          ${field("browser.page_timeout_ms", "Page load timeout (ms)", numberInput("browser.page_timeout_ms", s["browser.page_timeout_ms"], 5000, 120000, 1000))}
          ${field("browser.playback_start_timeout_ms", "Playback start timeout (ms)", numberInput("browser.playback_start_timeout_ms", s["browser.playback_start_timeout_ms"], 3000, 60000, 1000))}
          ${field("browser.stall_timeout_ms", "Stall detection window (ms)", numberInput("browser.stall_timeout_ms", s["browser.stall_timeout_ms"], 1500, 20000, 500))}
          ${field("browser.max_retries", "Navigation retries", numberInput("browser.max_retries", s["browser.max_retries"], 0, 3), "Only network/timeout failures are retried — never restriction messages.")}
          ${field("browser.locale", "Browser locale", textInput("browser.locale", s["browser.locale"], "en-US"))}
          ${field("browser.user_agent", "Custom user agent (optional)", textInput("browser.user_agent", s["browser.user_agent"], "Leave blank for Chromium default"))}
          <div class="field-row">
            ${field("browser.discovery_max_scrolls", "Discovery scrolls", numberInput("browser.discovery_max_scrolls", s["browser.discovery_max_scrolls"], 0, 60))}
            ${field("browser.discovery_scroll_delay_ms", "Scroll delay (ms)", numberInput("browser.discovery_scroll_delay_ms", s["browser.discovery_scroll_delay_ms"], 200, 5000, 100))}
          </div>
        </div>

        <div class="card">
          <div class="card-head"><div class="card-title">Monitoring defaults</div></div>
          ${field("monitoring.max_shorts_per_run", "Max Shorts per run", numberInput("monitoring.max_shorts_per_run", s["monitoring.max_shorts_per_run"], 1, 500), "Per-channel values override this default.")}
          <div class="field-row">
            ${field("monitoring.playback_min_percent", "Minimum coverage (%)", numberInput("monitoring.playback_min_percent", s["monitoring.playback_min_percent"], 1, 100), "Each check watches a random share of the Short between these two values.")}
            ${field("monitoring.playback_max_percent", "Maximum coverage (%)", numberInput("monitoring.playback_max_percent", s["monitoring.playback_max_percent"], 1, 100))}
          </div>
          ${field("monitoring.playback_verification_seconds", "Fallback playback window (seconds)", numberInput("monitoring.playback_verification_seconds", s["monitoring.playback_verification_seconds"], 2, 15, 0.5), "Only used when the player reports no duration.")}
          ${checkInput("monitoring.continuous_cycles", s["monitoring.continuous_cycles"], "Start the next cycle immediately after one finishes (continuous)")}
          ${checkInput("monitoring.randomize_order", s["monitoring.randomize_order"], "Check Shorts in a random order every cycle")}
          ${field("monitoring.pass_recheck_factor", "PASS re-check factor", numberInput("monitoring.pass_recheck_factor", s["monitoring.pass_recheck_factor"], 0.1, 20, 0.1), "Multiplier on the channel interval before healthy Shorts are re-checked.")}
          ${checkInput("monitoring.incremental_checks", s["monitoring.incremental_checks"], "Incremental checks (prioritise failing and unchecked Shorts)")}
          <div class="notice" style="margin-top:10px">
            Playback is muted and limited to a few seconds so checks verify functionality without
            accumulating watch time. The monitor never likes, subscribes, comments or shares.
          </div>
        </div>

        <div class="card">
          <div class="card-head"><div class="card-title">Notifications</div></div>
          ${checkInput("notifications.enabled", s["notifications.enabled"], "Enable external notifications")}
          ${field("notifications.webhook_url", "Webhook URL", `<input class="input" id="set-notifications.webhook_url" data-setting="notifications.webhook_url" type="text" value="" placeholder="${esc(s["notifications.webhook_url"] || "https://…")}" />`, s["notifications.webhook_url_set"] ? "A webhook is stored. Leave blank to keep the current value." : "Generic JSON POST, Slack or Discord webhook.")}
          ${field("notifications.webhook_format", "Webhook format", `<select class="select" id="set-notifications.webhook_format" data-setting="notifications.webhook_format">
            ${["generic", "slack", "discord"].map((format) => `<option value="${format}" ${s["notifications.webhook_format"] === format ? "selected" : ""}>${format}</option>`).join("")}
          </select>`)}
          ${field("notifications.email_to", "E-mail recipients", textInput("notifications.email_to", s["notifications.email_to"], "alerts@example.com (SMTP must be set in .env)"), "SMTP host and credentials come from environment variables, never the database.")}
          ${checkInput("notifications.notify_on_new_shorts", s["notifications.notify_on_new_shorts"], "Notify about newly discovered Shorts")}
          ${checkInput("notifications.notify_on_recovery", s["notifications.notify_on_recovery"], "Notify when a Short recovers")}
          ${field("notifications.repeat_reminder_hours", "Repeat reminder window (hours)", numberInput("notifications.repeat_reminder_hours", s["notifications.repeat_reminder_hours"], 0, 720), "An unresolved issue is not re-notified inside this window.")}
          ${field("notifications.run_failure_threshold", "Consecutive failures before alerting", numberInput("notifications.run_failure_threshold", s["notifications.run_failure_threshold"], 1, 50))}
          ${field("notifications.playback_regression_threshold_ms", "Playback regression threshold (ms)", numberInput("notifications.playback_regression_threshold_ms", s["notifications.playback_regression_threshold_ms"], 500, 60000, 100))}
          <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
            <button class="btn" type="button" data-action="test-notification">Send test notification</button>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><div class="card-title">Evidence &amp; environment</div></div>
          ${field("evidence.retention_days", "Evidence retention (days)", numberInput("evidence.retention_days", s["evidence.retention_days"], 1, 365))}
          ${field("evidence.max_screenshots_per_run", "Max screenshots per run", numberInput("evidence.max_screenshots_per_run", s["evidence.max_screenshots_per_run"], 0, 500))}
          <div class="kv" style="margin-top:12px">
            <dt>YouTube API key</dt><dd>${env.youtube_api_key_configured ? '<span class="pill ok">Configured</span>' : '<span class="pill neutral">Not set (env: YOUTUBE_API_KEY)</span>'}</dd>
            <dt>SMTP</dt><dd>${env.smtp_configured ? '<span class="pill ok">Configured</span>' : '<span class="pill neutral">Not configured</span>'}</dd>
            <dt>Database</dt><dd class="mono">${esc(env.database_path)}</dd>
            <dt>Evidence folder</dt><dd class="mono">${esc(env.evidence_dir)}</dd>
            <dt>Browser</dt><dd>${esc(env.browser_version || "not started yet")}</dd>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
            <button class="btn" type="button" data-action="cleanup-evidence">Delete expired evidence</button>
          </div>
          <div class="notice" style="margin-top:12px">
            This server binds to loopback by default and has no user accounts: it is a single-user
            local operations tool. Do not expose it to the internet without an authenticating proxy.
          </div>
        </div>
      </div>`;
  }

  async function saveSettings() {
    const values = {};
    const invalid = [];
    document.querySelectorAll("[data-setting]").forEach((element) => {
      const key = element.dataset.setting;
      if (element.type === "checkbox") {
        values[key] = element.checked;
        return;
      }
      if (element.type === "number") {
        const raw = String(element.value).trim();
        const numeric = Number(raw);
        const min = element.min === "" ? -Infinity : Number(element.min);
        const max = element.max === "" ? Infinity : Number(element.max);
        if (!raw || Number.isNaN(numeric) || numeric < min || numeric > max) {
          invalid.push(element);
          return;
        }
        values[key] = numeric;
        return;
      }
      values[key] = element.value;
    });
    if (invalid.length) {
      const label = invalid[0].closest(".field")?.querySelector("label")?.textContent || "this field";
      toast(`Check “${label}”: enter a number within the allowed range.`, "error");
      invalid[0].focus();
      return;
    }
    const button = document.querySelector('[data-action="save-settings"]');
    const buttonLabel = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "Saving…";
    }
    try {
      await api("/settings", { method: "PUT", body: JSON.stringify({ settings: values }) });
      toast("Settings saved.", "ok");
      await loadSettings();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button && document.contains(button)) {
        button.disabled = false;
        button.textContent = buttonLabel;
      }
    }
  }

  /* ------------------------------ scheduler UI ---------------------------- */

  async function loadScheduler() {
    try {
      const status = await api("/scheduler");
      renderSchedulerPill(status);
    } catch (error) { /* non-fatal */ }
  }

  function renderSchedulerPill(status) {
    if (!status) return;
    const pill = $("scheduler-pill");
    const pauseButton = $("btn-pause");
    pauseButton.textContent = status.paused ? "Resume" : "Pause";
    pauseButton.dataset.paused = status.paused ? "1" : "0";
    const active = status.active_run;
    let headline;
    let detail;
    if (status.paused) {
      headline = "Monitoring paused";
      detail = "Queued runs wait until you resume.";
    } else if (active) {
      headline = `Run #${active.id} · ${active.status}`;
      detail = `${active.channel_name || "channel"} · ${active.total_checked} checked`;
    } else if (status.next_due_at) {
      headline = `Next ${timeAgo(status.next_due_at)}`;
      detail = fmtDate(status.next_due_at);
    } else {
      headline = "Scheduler idle";
      detail = "No channel scheduled";
    }
    pill.innerHTML = `<strong>${esc(headline)}</strong><span>${esc(detail)}</span>`;
  }

  async function togglePause() {
    const paused = $("btn-pause").dataset.paused === "1";
    try {
      const status = await api(paused ? "/scheduler/resume" : "/scheduler/pause", { method: "POST" });
      renderSchedulerPill(status);
      toast(paused ? "Monitoring resumed." : "Monitoring paused.", "ok");
      loadCurrent();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function runDue() {
    try {
      const result = await api("/scheduler/run-due", { method: "POST" });
      if (!result.count) {
        toast("No channel is due right now.", "warn");
        return;
      }
      toast(`Queued ${result.count} run(s).`, "ok");
      pollUntilIdle();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function pollUntilIdle(attempt = 0, sawActive = false) {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      try {
        const status = await api("/scheduler");
        renderSchedulerPill(status);
        if (status.active_run && attempt < 90) {
          pollUntilIdle(attempt + 1, true);
          return;
        }
        if (status.active_run) {
          toast("The run is still in progress. Refresh in a moment.", "warn");
        } else if (sawActive) {
          toast("Monitoring run finished.", "ok");
        }
        await loadCurrent();
      } catch (error) {
        // stop polling on error
      }
    }, attempt === 0 ? 1500 : 4000);
  }

  /* -------------------------------- drawers ------------------------------- */

  let lastFocused = null;

  function anyDialogOpen() {
    return !$("modal").classList.contains("hidden") || !$("drawer").classList.contains("hidden");
  }

  function rememberFocus() {
    if (!anyDialogOpen()) lastFocused = document.activeElement;
  }

  function syncDialogState() {
    const open = anyDialogOpen();
    document.body.classList.toggle("no-scroll", open);
    const app = document.querySelector(".app");
    if (app) app.inert = open;
    if (!open) {
      if (lastFocused && document.contains(lastFocused)) {
        try { lastFocused.focus({ preventScroll: true }); } catch (error) { /* detached */ }
      }
      lastFocused = null;
    }
  }

  function openDrawer(title, subtitle) {
    rememberFocus();
    $("drawer-title").textContent = title;
    $("drawer-sub").textContent = subtitle || "";
    $("drawer-body").innerHTML = loadingBlock();
    $("drawer-body").scrollTop = 0;
    $("drawer").classList.remove("hidden");
    $("overlay").classList.remove("hidden");
    syncDialogState();
    $("drawer-close").focus({ preventScroll: true });
  }

  function closeDrawer() {
    const wasOpen = !$("drawer").classList.contains("hidden");
    $("drawer").classList.add("hidden");
    if ($("modal").classList.contains("hidden")) $("overlay").classList.add("hidden");
    if (wasOpen) syncDialogState();
  }

  async function openShortDrawer(shortId, tab) {
    openDrawer("Short monitoring history", "");
    try {
      const data = await api(`/shorts/${shortId}`);
      renderShortDrawer(data, tab);
    } catch (error) {
      $("drawer-body").innerHTML = `<div class="notice">${esc(error.message)}</div>`;
    }
  }

  function renderShortDrawer(data, tab) {
    const short = data.short;
    const latest = data.latest;
    const url = short.video_url || `https://www.youtube.com/shorts/${short.video_id}`;
    $("drawer-title").textContent = short.title || short.video_id;
    $("drawer-sub").textContent = `${short.video_id} · ${short.channel_name || "channel"} · ${short.is_listed ? "listed" : "delisted"}`;

    const latestBlock = latest ? `
      <div class="card" style="padding:13px">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
          ${statusPill(latest.availability_status)}
          ${playbackPill(latest.playback_status)}
          ${latest.restriction_type && latest.restriction_type !== "NONE" ? `<span class="pill neutral">${esc(latest.restriction_type)}</span>` : ""}
          <span class="muted" style="font-size:12px">${fmtDate(latest.checked_at)} · ${fmtDuration(latest.check_duration_ms)} · ${latest.attempt_count || 1} attempt(s)</span>
        </div>
        <div class="timeline-msg">${esc(latest.diagnostic_message || "No diagnostic message recorded.")}</div>
        <div class="kv" style="margin-top:10px">
          <dt>Playback start</dt><dd>${fmtMs(latest.playback_start_time_ms)}</dd>
          <dt>Playback covered</dt><dd>${fmtMs(latest.playback_checked_ms)}</dd>
          <dt>Video duration</dt><dd>${latest.video_duration_seconds ? `${Number(latest.video_duration_seconds).toFixed(1)}s` : "—"}</dd>
          <dt>Coverage</dt><dd>${latest.video_duration_seconds ? `${Math.min(100, Math.round((Number(latest.playback_checked_ms || 0) / 1000 / Number(latest.video_duration_seconds)) * 100))}%` : "—"}</dd>
          <dt>Coverage target reached</dt><dd>${latest.playback_completed ? "Yes" : "No"}</dd>
          <dt>readyState</dt><dd>${latest.ready_state == null ? "—" : esc(latest.ready_state)}</dd>
          <dt>Frame size</dt><dd>${latest.video_width ? `${latest.video_width}×${latest.video_height}` : "—"}</dd>
          <dt>Run</dt><dd>${latest.monitoring_run_id ? `#${latest.monitoring_run_id}` : "manual"}</dd>
        </div>
      </div>` : '<div class="notice">No checks recorded yet.</div>';

    const historyHtml = data.history.length
      ? `<div class="timeline">${data.history.map((row) => `
          <div class="timeline-item">
            <div class="timeline-head">
              <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                ${statusPill(row.availability_status)} ${playbackPill(row.playback_status)}
                ${row.restriction_type && row.restriction_type !== "NONE" ? `<span class="pill neutral">${esc(row.restriction_type)}</span>` : ""}
              </span>
              <span class="muted" style="font-size:11.5px">${fmtDate(row.checked_at)}</span>
            </div>
            <div class="timeline-msg">${esc(row.diagnostic_message || "")}</div>
            <div style="display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap">
              <span class="muted mono">start ${fmtMs(row.playback_start_time_ms)}</span>
              ${row.trigger_type ? `<span class="muted" style="font-size:11.5px">· ${esc(row.trigger_type)}</span>` : ""}
              ${row.evidence_id ? `<button class="btn small ghost" type="button" data-action="show-evidence" data-id="${row.evidence_id}">Evidence</button>` : ""}
            </div>
          </div>`).join("")}</div>`
      : '<div class="notice">No history yet.</div>';

    const alertsHtml = data.alerts.length
      ? `<div class="timeline">${data.alerts.map((alert) => `
          <div class="timeline-item">
            <div class="timeline-head">
              <span>${esc(alert.alert_type)}</span>
              <span class="muted" style="font-size:11.5px">${fmtDate(alert.created_at)}</span>
            </div>
            <div class="timeline-msg">${esc(alert.message)}</div>
            <div class="muted" style="font-size:11.5px;margin-top:4px">
              ${alert.resolved_at ? `resolved ${fmtDate(alert.resolved_at)}` : "open"} · notification ${esc(alert.notification_status)}
            </div>
          </div>`).join("")}</div>`
      : '<div class="notice">No alerts for this Short.</div>';

    $("drawer-body").innerHTML = `
      <div class="drawer-section">
        <h4>Video</h4>
        <div class="kv">
          <dt>Title</dt><dd>${esc(short.title || "—")}</dd>
          <dt>Video ID</dt><dd class="mono">${esc(short.video_id)}</dd>
          <dt>URL</dt><dd><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a></dd>
          <dt>Channel</dt><dd>${esc(short.channel_name || "—")}</dd>
          <dt>First discovered</dt><dd>${fmtDate(short.first_discovered_at)}</dd>
          <dt>Last discovered</dt><dd>${fmtDate(short.last_discovered_at)}</dd>
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <a class="btn small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open Short</a>
          <button class="btn small" type="button" data-action="recheck-short" data-id="${short.id}">Run check again</button>
        </div>
      </div>
      <div class="drawer-section" id="section-current"><h4>Current result</h4>${latestBlock}</div>
      <div class="drawer-section" id="section-history"><h4>Monitoring history</h4>${historyHtml}</div>
      <div class="drawer-section" id="section-alerts"><h4>Alerts</h4>${alertsHtml}</div>
      <div class="drawer-section" id="section-details"><h4>Diagnostics</h4>
        ${latest && latest.evidence_id
          ? `<button class="btn small" type="button" data-action="show-evidence" data-id="${latest.evidence_id}">Open latest diagnostic evidence</button>`
          : '<div class="notice">Diagnostics are captured for failed, restricted and inconclusive checks.</div>'}
      </div>`;

    if (tab === "details" || tab === "history") {
      const target = tab === "details" ? "section-details" : "section-history";
      const element = document.getElementById(target);
      if (element) element.scrollIntoView({ block: "start" });
    }
  }

  async function openEvidence(evidenceId) {
    openDrawer("Diagnostic evidence", `Evidence #${evidenceId}`);
    try {
      const evidence = await api(`/evidence/${evidenceId}`);
      const consoleErrors = (evidence.console_errors || []).filter(Boolean);
      const pageErrors = (evidence.page_errors || []).filter(Boolean);
      const failedRequests = evidence.failed_requests || [];
      const signals = evidence.page_signals || {};
      $("drawer-body").innerHTML = `
        <div class="drawer-section">
          <h4>Capture</h4>
          <div class="kv">
            <dt>Captured</dt><dd>${fmtDate(evidence.captured_at)}</dd>
            <dt>Video</dt><dd class="mono">${esc(evidence.video_id || "—")}</dd>
            <dt>Run</dt><dd>${evidence.monitoring_run_id ? `#${evidence.monitoring_run_id}` : "—"}</dd>
            <dt>Page</dt><dd><a href="${esc(evidence.page_url || "#")}" target="_blank" rel="noopener noreferrer">${esc(evidence.page_url || "—")}</a></dd>
            <dt>Browser</dt><dd>${esc(evidence.browser_version || "—")}</dd>
            <dt>Expires</dt><dd>${fmtDate(evidence.expires_at)}</dd>
          </div>
        </div>
        <div class="drawer-section">
          <h4>Screenshot</h4>
          ${evidence.screenshot_url
            ? `<img class="screenshot" src="${esc(evidence.screenshot_url)}" alt="Screenshot of the monitored Short page" />`
            : '<div class="notice">No screenshot was retained for this check.</div>'}
        </div>
        <div class="drawer-section">
          <h4>YouTube signals</h4>
          <pre class="json">${esc(JSON.stringify(signals, null, 2))}</pre>
        </div>
        <div class="drawer-section">
          <h4>Browser console errors</h4>
          ${consoleErrors.length ? `<ul class="error-list">${consoleErrors.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : '<div class="notice">None captured.</div>'}
        </div>
        <div class="drawer-section">
          <h4>Page errors</h4>
          ${pageErrors.length ? `<ul class="error-list">${pageErrors.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : '<div class="notice">None captured.</div>'}
        </div>
        <div class="drawer-section">
          <h4>Failed network requests</h4>
          ${failedRequests.length ? `<pre class="json">${esc(JSON.stringify(failedRequests, null, 2))}</pre>` : '<div class="notice">None captured.</div>'}
        </div>
        <div class="drawer-section">
          <h4>Video element state</h4>
          <pre class="json">${esc(JSON.stringify(evidence.video_state || {}, null, 2))}</pre>
        </div>`;
    } catch (error) {
      $("drawer-body").innerHTML = `<div class="notice">${esc(error.message)}</div>`;
    }
  }

  async function openRunDrawer(runId) {
    openDrawer("Monitoring run", `Run #${runId}`);
    try {
      const data = await api(`/runs/${runId}`);
      const run = data.run;
      const results = data.results || [];
      const logs = data.logs || [];
      const active = run.status === "RUNNING" || run.status === "QUEUED";
      $("drawer-sub").textContent = `Run #${run.id} · ${run.channel_name || "channel"} · ${run.trigger_type}`;
      $("drawer-body").innerHTML = `
        <div class="drawer-section">
          <h4>Summary</h4>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">${statusPill(run.status)}</div>
          <div class="kv">
            <dt>Started</dt><dd>${fmtDate(run.started_at || run.queued_at)}</dd>
            <dt>Completed</dt><dd>${fmtDate(run.completed_at)}</dd>
            <dt>Duration</dt><dd>${fmtDuration(run.duration_ms)}</dd>
            <dt>Scope</dt><dd>${esc(run.scope)}${run.target_video_id ? ` · ${esc(run.target_video_id)}` : ""}</dd>
            <dt>Checked</dt><dd>${run.total_checked}</dd>
            <dt>Results</dt><dd>${run.passed_count} pass · ${run.warning_count} warning · ${run.restricted_count} restricted · ${run.failed_count} failed · ${run.inconclusive_count} inconclusive</dd>
            <dt>Discovery</dt><dd>${run.discovery_total_count} found · ${run.discovery_new_count} new · ${run.discovery_removed_count} delisted${run.discovery_truncated ? " · truncated" : ""} · ${esc(run.discovery_source || "—")}</dd>
            ${run.error_message ? `<dt>Error</dt><dd style="color:var(--danger)">${esc(run.error_message)}</dd>` : ""}
          </div>
          ${active ? `<button class="btn small danger" style="margin-top:12px" type="button" data-action="cancel-run" data-id="${run.id}">Cancel run</button>` : ""}
        </div>
        <div class="drawer-section">
          <h4>Run log</h4>
          ${logs.length ? `<pre class="log">${logs.map((line) => `[${esc(line.created_at)}] ${esc(line.level.toUpperCase())} ${esc(line.message)}`).join("\n")}</pre>` : '<div class="notice">No log lines yet.</div>'}
        </div>
        <div class="drawer-section">
          <h4>Per-video results</h4>
          ${results.length ? `<div class="timeline">${results.map((row) => `
            <div class="timeline-item">
              <div class="timeline-head">
                <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                  ${statusPill(row.availability_status)} ${playbackPill(row.playback_status)}
                </span>
                <span class="mono muted">${esc(row.video_id)}</span>
              </div>
              <div class="timeline-msg">${esc(row.title || "")} — ${esc(row.diagnostic_message || "")}</div>
              <div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">
                ${row.short_id ? `<button class="btn small ghost" type="button" data-action="alert-video" data-id="${row.short_id}">History</button>` : ""}
                ${row.evidence_id ? `<button class="btn small ghost" type="button" data-action="show-evidence" data-id="${row.evidence_id}">Evidence</button>` : ""}
              </div>
            </div>`).join("")}</div>` : '<div class="notice">No per-video results recorded.</div>'}
        </div>`;
    } catch (error) {
      $("drawer-body").innerHTML = `<div class="notice">${esc(error.message)}</div>`;
    }
  }

  /* --------------------------------- modal -------------------------------- */

  function closeModal() {
    const wasOpen = !$("modal").classList.contains("hidden");
    $("modal").classList.add("hidden");
    if ($("drawer").classList.contains("hidden")) $("overlay").classList.add("hidden");
    if (wasOpen) syncDialogState();
  }

  function openChannelModal(channel) {
    const editing = Boolean(channel);
    const intervalOptions = Object.entries(INTERVALS).map(([value, label]) =>
      `<option value="${value}" ${Number(channel ? channel.monitoring_interval_minutes : 60) === Number(value) ? "selected" : ""}>${label}</option>`
    ).join("");
    $("modal").innerHTML = `
      <h2>${editing ? "Edit channel" : "Add a YouTube channel"}</h2>
      <div class="sub">Any supported channel URL: /@handle, /channel/UC…, /c/name or /user/name.</div>
      <form id="channel-form">
        <div class="field">
          <label for="ch-url">Channel URL *</label>
          <input class="input" id="ch-url" name="channel_url" type="text" required
            placeholder="https://www.youtube.com/@yourchannel" value="${esc(channel ? channel.channel_url : "")}" />
        </div>
        ${editing ? `
          <div class="field">
            <label>Resolved channel</label>
            <div class="input mono" style="background:#0d1118">${esc(channel.channel_id || "unresolved")}</div>
          </div>` : `
          <div class="field">
            <label for="ch-id">Channel ID (optional)</label>
            <input class="input" id="ch-id" name="channel_id" type="text" placeholder="UC… (leave blank to detect)"
              value="${esc(channel ? channel.channel_id || "" : "")}" />
            <div class="help">Useful when YouTube blocks automatic channel resolution.</div>
          </div>`}
        <div class="field-row">
          <div class="field">
            <label for="ch-interval">Monitoring frequency</label>
            <select class="select" id="ch-interval" name="monitoring_interval_minutes">${intervalOptions}</select>
          </div>
          <div class="field">
            <label for="ch-max">Max Shorts per run</label>
            <input class="input" id="ch-max" name="max_shorts_per_run" type="number" min="1" max="500"
              value="${esc(channel ? channel.max_shorts_per_run : 40)}" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Playback verification</label>
            <div class="notice">Each check watches a random 60–100% of the Short, muted.</div>
          </div>
          <div class="field">
            <label>Options</label>
            ${checkInputModal("monitoring_enabled", channel ? Boolean(channel.monitoring_enabled) : true, "Monitoring enabled")}
            ${checkInputModal("incremental_checks", channel ? Boolean(channel.incremental_checks) : true, "Incremental checks")}
          </div>
        </div>
        ${checkInputModal("notify_new_shorts", channel ? Boolean(channel.notify_new_shorts) : true, "Notify about new Shorts from this channel")}
        <div id="channel-validate-result" class="notice" style="margin-top:10px">Validate the URL to resolve the channel before saving (recommended).</div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" data-modal-action="cancel">Cancel</button>
          <button class="btn" type="button" data-modal-action="validate">Validate</button>
          <button class="btn primary" type="submit">${editing ? "Save changes" : "Add channel"}</button>
        </div>
      </form>`;
    rememberFocus();
    $("modal").classList.remove("hidden");
    $("overlay").classList.remove("hidden");
    $("modal").dataset.channelId = editing ? String(channel.id) : "";
    $("channel-form").addEventListener("submit", onSubmitChannelForm);
    syncDialogState();
    $("ch-url").focus();
    $("ch-url").select();
  }

  function checkInputModal(name, checked, label) {
    return `<label class="check" style="margin-top:6px"><input type="checkbox" name="${esc(name)}" ${checked ? "checked" : ""} /> ${esc(label)}</label>`;
  }

  async function onSubmitChannelForm(event) {
    event.preventDefault();
    const form = event.target;
    const payload = {
      channel_url: form.channel_url.value.trim(),
      monitoring_interval_minutes: Number(form.monitoring_interval_minutes.value),
      max_shorts_per_run: Number(form.max_shorts_per_run.value),
      incremental_checks: form.incremental_checks.checked,
      notify_new_shorts: form.notify_new_shorts.checked,
      monitoring_enabled: form.monitoring_enabled.checked,
    };
    if (form.channel_id) payload.channel_id = form.channel_id.value.trim() || null;
    const channelId = $("modal").dataset.channelId;
    const submit = form.querySelector('button[type="submit"]');
    const submitLabel = submit ? submit.textContent : "";
    if (submit) {
      submit.disabled = true;
      submit.textContent = "Saving…";
    }
    try {
      if (channelId) {
        const { channel_id, ...updates } = payload;
        void channel_id;
        await api(`/channels/${channelId}`, { method: "PATCH", body: JSON.stringify(updates) });
        toast("Channel updated.", "ok");
      } else {
        const created = await api("/channels", { method: "POST", body: JSON.stringify(payload) });
        toast(`Channel added: ${created.channel_name || created.channel_id}`, "ok");
      }
      state.channels = null;
      closeModal();
      await loadCurrent();
    } catch (error) {
      toast(error.message, "error");
      if (submit && document.contains(submit)) {
        submit.disabled = false;
        submit.textContent = submitLabel;
      }
    }
  }

  async function validateChannelForm() {
    const form = $("channel-form");
    if (!form) return;
    const resultBox = $("channel-validate-result");
    resultBox.textContent = "Resolving channel…";
    try {
      const payload = { channel_url: form.channel_url.value.trim() };
      if (form.channel_id && form.channel_id.value.trim()) payload.channel_id = form.channel_id.value.trim();
      const result = await api("/channels/validate", { method: "POST", body: JSON.stringify(payload) });
      if (!result.ok) {
        resultBox.innerHTML = `<span style="color:var(--danger)">${esc(result.error)}</span>`;
        return;
      }
      const resolved = result.resolved || {};
      resultBox.innerHTML = `<span style="color:var(--ok)">Resolved:</span> ${esc(resolved.channel_name || "(name unavailable)")} · <span class="mono">${esc(resolved.channel_id)}</span><br/>
        <span class="muted">${esc(resolved.canonical_url || result.parsed.canonical_url)}</span>
        ${(resolved.warnings || []).length ? `<br/><span style="color:var(--warn)">${esc(resolved.warnings.join(" "))}</span>` : ""}`;
    } catch (error) {
      resultBox.innerHTML = `<span style="color:var(--danger)">${esc(error.message)}</span>`;
    }
  }

  /* -------------------------------- actions ------------------------------- */

  async function recheckShort(shortId) {
    try {
      const run = await api(`/shorts/${shortId}/check`, { method: "POST" });
      toast(`Check queued as run #${run.id}.`, "ok");
      pollUntilIdle();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function runChannel(channelId) {
    try {
      const run = await api(`/channels/${channelId}/run`, { method: "POST" });
      toast(`Run #${run.id} queued.`, "ok");
      pollUntilIdle();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function cancelRun(runId) {
    try {
      await api(`/runs/${runId}/cancel`, { method: "POST" });
      toast(`Cancellation requested for run #${runId}.`, "warn");
      pollUntilIdle();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function resolveChannel(channelId) {
    try {
      await api(`/channels/${channelId}/resolve`, { method: "POST" });
      toast("Channel resolved.", "ok");
      state.channels = null;
      await loadCurrent();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function deleteChannel(channelId) {
    if (!window.confirm("Remove this channel and all of its stored Shorts and history?")) return;
    try {
      await api(`/channels/${channelId}`, { method: "DELETE" });
      toast("Channel removed.", "ok");
      state.channels = null;
      await loadCurrent();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function acknowledgeAlert(alertId) {
    try {
      await api(`/alerts/${alertId}/acknowledge`, { method: "POST" });
      await loadAlerts();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function resolveAlert(alertId) {
    try {
      await api(`/alerts/${alertId}/resolve`, { method: "POST" });
      await loadAlerts();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function findChannel(channelId) {
    return (state.channels || []).find((channel) => String(channel.id) === String(channelId));
  }

  /* ------------------------------- delegation ----------------------------- */

  async function onViewClick(event) {
    const actionTarget = event.target.closest("[data-action]");
    const filterTarget = event.target.closest("[data-filter]");
    const alertStateTarget = event.target.closest("[data-alert-state]");
    if (filterTarget) {
      onShortsFilter(filterTarget.dataset.filter);
      return;
    }
    if (alertStateTarget) {
      state.alerts.state = alertStateTarget.dataset.alertState;
      await loadAlerts();
      return;
    }
    if (!actionTarget) return;
    const action = actionTarget.dataset.action;
    const id = actionTarget.dataset.id;
    try {
      switch (action) {
        case "reload-view":
          await loadCurrent();
          break;
        case "add-channel":
          openChannelModal(null);
          break;
        case "manage-channels":
          await showView("channels");
          break;
        case "open-alerts":
          await showView("alerts");
          break;
        case "open-short-link":
          window.open(actionTarget.dataset.url, "_blank", "noopener,noreferrer");
          break;
        case "recheck-short":
          await recheckShort(id);
          break;
        case "short-history":
          await openShortDrawer(id, "history");
          break;
        case "short-details":
          await openShortDrawer(id, "details");
          break;
        case "open-run":
          await openRunDrawer(id);
          break;
        case "cancel-run":
          await cancelRun(id);
          break;
        case "run-channel":
          await runChannel(id);
          break;
        case "edit-channel":
          openChannelModal(findChannel(id));
          break;
        case "resolve-channel":
          await resolveChannel(id);
          break;
        case "delete-channel":
          await deleteChannel(id);
          break;
        case "ack-alert":
          await acknowledgeAlert(id);
          break;
        case "resolve-alert":
          await resolveAlert(id);
          break;
        case "alert-video":
          if (id && id !== "null") await openShortDrawer(id, "history");
          break;
        case "show-evidence":
          await openEvidence(id);
          break;
        case "page-prev":
          state.shorts.offset = Math.max(0, state.shorts.offset - state.shorts.limit);
          await loadShorts();
          break;
        case "page-next":
          state.shorts.offset += state.shorts.limit;
          await loadShorts();
          break;
        case "runs-prev":
          state.runs.offset = Math.max(0, state.runs.offset - state.runs.limit);
          await loadRuns();
          break;
        case "runs-next":
          state.runs.offset += state.runs.limit;
          await loadRuns();
          break;
        case "run-due":
          await runDue();
          break;
        case "save-settings":
          await saveSettings();
          break;
        case "test-notification":
          await testNotification();
          break;
        case "cleanup-evidence":
          await cleanupEvidence();
          break;
        default:
          break;
      }
    } catch (error) {
      toast(error.message, "error");
    }
  }

  let searchTimer = null;

  function onViewInput(event) {
    if (event.target.id === "short-search") {
      const value = event.target.value;
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.shorts.q = value;
        state.shorts.offset = 0;
        loadShorts().catch((error) => toast(error.message, "error"));
      }, 350);
    }
  }

  function onViewChange(event) {
    if (event.target.id === "short-channel") {
      state.shorts.channel_id = event.target.value;
      state.shorts.offset = 0;
      loadShorts().catch((error) => toast(error.message, "error"));
    }
    if (event.target.id === "short-sort") {
      state.shorts.sort = event.target.value;
      state.shorts.offset = 0;
      loadShorts().catch((error) => toast(error.message, "error"));
    }
  }

  function onModalClick(event) {
    const trigger = event.target.closest("[data-modal-action]");
    if (!trigger) return;
    if (trigger.dataset.modalAction === "cancel") closeModal();
    if (trigger.dataset.modalAction === "validate") validateChannelForm();
  }

  async function testNotification() {
    try {
      const result = await api("/settings/test-notification", { method: "POST" });
      toast(result.message || (result.ok ? "Test sent." : "Test failed."), result.ok ? "ok" : "error");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function cleanupEvidence() {
    try {
      const result = await api("/settings/cleanup-evidence", { method: "POST" });
      toast(`Removed ${result.removed} expired evidence record(s).`, "ok");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  /* --------------------------------- boot --------------------------------- */

  function init() {
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.addEventListener("click", () => showView(button.dataset.view));
    });
    $("btn-refresh").addEventListener("click", () => loadCurrent().catch((error) => toast(error.message, "error")));
    $("btn-run-due").addEventListener("click", runDue);
    $("btn-pause").addEventListener("click", togglePause);
    $("drawer-close").addEventListener("click", closeDrawer);
    $("overlay").addEventListener("click", () => {
      closeModal();
      closeDrawer();
    });
    $("view").addEventListener("click", onViewClick);
    $("view").addEventListener("input", onViewInput);
    $("view").addEventListener("change", onViewChange);
    $("modal").addEventListener("click", onModalClick);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!$("modal").classList.contains("hidden")) closeModal();
      else if (!$("drawer").classList.contains("hidden")) closeDrawer();
    });
    loadScheduler();
    showView("overview");
    setInterval(() => {
      loadScheduler();
      if (state.view === "overview") loadOverview().catch(() => {});
    }, 20000);
    hideBanner();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
