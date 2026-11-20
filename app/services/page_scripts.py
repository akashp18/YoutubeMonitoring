"""JavaScript helpers injected into YouTube pages.

All page inspection happens through these scripts so the monitor only needs a
narrow, fake-friendly page interface (goto / evaluate / click / screenshot).
"""

from __future__ import annotations

INIT_COLLECTOR = r"""
(() => {
  if (window.__ytmon) return;
  const state = {
    consoleErrors: [],
    pageErrors: [],
    rejected: [],
    startedAt: Date.now()
  };
  window.__ytmon = state;
  const push = (arr, value) => {
    if (!value) return;
    if (arr.length >= 25) return;
    arr.push(String(value).slice(0, 500));
  };
  const original = console.error ? console.error.bind(console) : null;
  console.error = function (...args) {
    try {
      push(state.consoleErrors, args.map(a => {
        try { return typeof a === 'string' ? a : JSON.stringify(a); } catch (e) { return String(a); }
      }).join(' '));
    } catch (e) {}
    if (original) original.apply(null, args);
  };
  window.addEventListener('error', ev => {
    push(state.pageErrors, (ev && (ev.message || (ev.error && ev.error.message))) || 'script error');
  });
  window.addEventListener('unhandledrejection', ev => {
    const reason = ev && ev.reason;
    push(state.rejected, (reason && (reason.message || reason)) || 'unhandled rejection');
  });
})();
"""

# Rich page snapshot used for availability classification.
PROBE_PAGE = r"""
() => {
  const norm = v => (v == null ? '' : String(v).replace(/\s+/g, ' ').trim());
  const textOf = el => norm(el && (el.innerText || el.textContent));
  const collectTexts = sels => {
    const out = [];
    for (const sel of sels) {
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
      for (const node of nodes) {
        const text = textOf(node);
        if (text && text.length <= 500 && out.indexOf(text) === -1) out.push(text);
      }
    }
    return out.slice(0, 8);
  };

  const errorSelectors = [
    'ytd-player-error-message-renderer',
    '#error-screen',
    '.ytp-error-content-wrap',
    'yt-playability-error-supported-renderers',
    'ytd-enforcement-message-view-model',
    '#ytd-player .ytp-error',
    'ytd-reel-video-renderer ytd-player-error-message-renderer'
  ];

  let playability = null;
  try {
    const pr = window.ytInitialPlayerResponse;
    if (pr && pr.playabilityStatus) {
      const ps = pr.playabilityStatus;
      let subreason = norm(ps.subeason);
      if (!subreason && ps.errorScreen && ps.errorScreen.playerErrorMessageRenderer) {
        const r = ps.errorScreen.playerErrorMessageRenderer;
        subreason = norm(r.reason && r.reason.simpleText) || norm(r.subreason && r.subreason.simpleText);
      }
      playability = {
        status: norm(ps.status),
        reason: norm(ps.reason),
        subreason: subreason,
        hasVideoDetails: !!(pr.videoDetails && pr.videoDetails.videoId),
        isLive: !!(pr.videoDetails && pr.videoDetails.isLiveContent)
      };
    }
  } catch (e) {}

  const videos = Array.from(document.querySelectorAll('video'));
  let video = null;
  try {
    video = document.querySelector('ytd-reel-video-renderer[is-active] video')
      || document.querySelector('ytd-shorts .reel-video-in-sequence[is-active] video')
      || null;
  } catch (e) {}
  if (!video) {
    video = videos.find(v => v.videoWidth > 0 && v.getBoundingClientRect().width > 40) || videos[0] || null;
  }

  let videoState = null;
  if (video) {
    let rect = {x: 0, y: 0, width: 0, height: 0};
    try {
      const r = video.getBoundingClientRect();
      rect = {x: r.x, y: r.y, width: r.width, height: r.height};
    } catch (e) {}
    let quality = null;
    try {
      if (video.getVideoPlaybackQuality) {
        const q = video.getVideoPlaybackQuality();
        quality = {dropped: q.droppedVideoFrames, total: q.totalVideoFrames};
      }
    } catch (e) {}
    videoState = {
      present: true,
      readyState: Number(video.readyState || 0),
      networkState: Number(video.networkState || 0),
      paused: !!video.paused,
      ended: !!video.ended,
      muted: !!video.muted,
      currentTime: Number(video.currentTime || 0),
      duration: Number.isFinite(video.duration) ? Number(video.duration) : null,
      videoWidth: Number(video.videoWidth || 0),
      videoHeight: Number(video.videoHeight || 0),
      playbackRate: Number(video.playbackRate || 1),
      seekable: !!(video.seekable && video.seekable.length),
      error: video.error ? {code: Number(video.error.code || 0), message: norm(video.error.message)} : null,
      rect: rect,
      quality: quality
    };
  }

  const bodyText = norm(document.body && document.body.innerText).slice(0, 8000);
  const title = norm(document.title);
  const haystack = bodyText + ' ' + title;
  return {
    url: location.href,
    title: title,
    readyState: document.readyState,
    playability: playability,
    errorTexts: collectTexts(errorSelectors),
    videoCount: videos.length,
    video: videoState,
    bodyText: bodyText,
    hasChallenge: /confirm you(?:'|’)re not a bot|unusual traffic|recaptcha|verify you(?:'|’)re human|automated queries/i.test(haystack)
      || !!document.querySelector('iframe[src*="recaptcha"], .g-recaptcha, yt-captcha-renderer'),
    consentWall: /before you continue to youtube/i.test(bodyText) || location.hostname === 'consent.youtube.com',
    loginWall: /sign in to youtube|you need to sign in|sign in to watch/i.test(bodyText),
    mon: window.__ytmon || null
  };
}
"""

# Lightweight poll used while waiting for the player to become ready.
VIDEO_STATE = r"""
() => {
  const norm = v => (v == null ? '' : String(v).replace(/\s+/g, ' ').trim());
  const videos = Array.from(document.querySelectorAll('video'));
  let video = null;
  try {
    video = document.querySelector('ytd-reel-video-renderer[is-active] video') || null;
  } catch (e) {}
  if (!video) {
    video = videos.find(v => v.videoWidth > 0 && v.getBoundingClientRect().width > 40) || videos[0] || null;
  }
  const errors = [];
  document.querySelectorAll('ytd-player-error-message-renderer, .ytp-error-content-wrap, #error-screen').forEach(el => {
    const t = norm(el.innerText || el.textContent);
    if (t) errors.push(t.slice(0, 400));
  });
  if (!video) return {present: false, errorTexts: errors};
  let rect = {x: 0, y: 0, width: 0, height: 0};
  try {
    const r = video.getBoundingClientRect();
    rect = {x: r.x, y: r.y, width: r.width, height: r.height};
  } catch (e) {}
  return {
    present: true,
    readyState: Number(video.readyState || 0),
    paused: !!video.paused,
    muted: !!video.muted,
    ended: !!video.ended,
    currentTime: Number(video.currentTime || 0),
    duration: Number.isFinite(video.duration) ? Number(video.duration) : null,
    videoWidth: Number(video.videoWidth || 0),
    videoHeight: Number(video.videoHeight || 0),
    error: video.error ? {code: Number(video.error.code || 0), message: norm(video.error.message)} : null,
    rect: rect,
    errorTexts: errors
  };
}
"""

# Attach playback sampling to the active video element.
START_PLAYBACK_MONITOR = r"""
() => {
  const videos = Array.from(document.querySelectorAll('video'));
  let video = null;
  try {
    video = document.querySelector('ytd-reel-video-renderer[is-active] video') || null;
  } catch (e) {}
  if (!video) {
    video = videos.find(v => v.videoWidth > 0 && v.getBoundingClientRect().width > 40) || videos[0] || null;
  }
  if (!video) return {ok: false, reason: 'no-video-element'};
  const boot = performance.now();
  const state = {
    samples: [],
    errors: [],
    maxTime: Number(video.currentTime || 0),
    startedAt: boot,
    lastProgressAt: boot,
    blocked: false
  };
  window.__ytplay = state;
  const sample = () => {
    state.samples.push({
      t: Math.round(performance.now() - boot),
      ct: Number(Number(video.currentTime || 0).toFixed(2)),
      paused: !!video.paused,
      rs: Number(video.readyState || 0)
    });
    if (state.samples.length > 120) state.samples.shift();
  };
  sample();
  state.onTime = () => {
    sample();
    const ct = Number(video.currentTime || 0);
    if (ct > state.maxTime + 0.01) {
      state.maxTime = ct;
      state.lastProgressAt = performance.now();
    }
  };
  state.onError = () => {
    state.errors.push({
      code: video.error ? Number(video.error.code || 0) : -1,
      message: video.error ? String(video.error.message || 'media error') : 'media error',
      at: Math.round(performance.now() - boot)
    });
  };
  state.onPlaying = () => { state.playingAt = Math.round(performance.now() - boot); };
  video.addEventListener('timeupdate', state.onTime);
  video.addEventListener('error', state.onError);
  video.addEventListener('playing', state.onPlaying);
  return {
    ok: true,
    muted: !!video.muted,
    paused: !!video.paused,
    currentTime: Number(video.currentTime || 0),
    readyState: Number(video.readyState || 0)
  };
}
"""

# Force playback after a real user gesture has been attempted. Muting keeps the
# check quiet and avoids autoplay policies blocking it.
REQUEST_PLAY = r"""
() => {
  const videos = Array.from(document.querySelectorAll('video'));
  let video = null;
  try {
    video = document.querySelector('ytd-reel-video-renderer[is-active] video') || null;
  } catch (e) {}
  if (!video) {
    video = videos.find(v => v.videoWidth > 0 && v.getBoundingClientRect().width > 40) || videos[0] || null;
  }
  if (!video) return {ok: false, reason: 'no-video-element'};
  try { video.muted = true; } catch (e) {}
  let blocked = false;
  try {
    const result = video.play();
    if (result && typeof result.catch === 'function') {
      result.catch(() => { blocked = true; if (window.__ytplay) window.__ytplay.blocked = true; });
    }
  } catch (e) {
    blocked = true;
  }
  try { video.focus({preventScroll: true}); } catch (e) {}
  return {ok: true, paused: !!video.paused, currentTime: Number(video.currentTime || 0), blocked};
}
"""

READ_PLAYBACK = r"""
() => {
  const state = window.__ytplay || null;
  const videos = Array.from(document.querySelectorAll('video'));
  let video = null;
  try {
    video = document.querySelector('ytd-reel-video-renderer[is-active] video') || null;
  } catch (e) {}
  if (!video) {
    video = videos.find(v => v.videoWidth > 0 && v.getBoundingClientRect().width > 40) || videos[0] || null;
  }
  if (!video) return {present: false, playback: state};
  return {
    present: true,
    paused: !!video.paused,
    ended: !!video.ended,
    muted: !!video.muted,
    currentTime: Number(video.currentTime || 0),
    duration: Number.isFinite(video.duration) ? Number(video.duration) : null,
    readyState: Number(video.readyState || 0),
    networkState: Number(video.networkState || 0),
    videoWidth: Number(video.videoWidth || 0),
    videoHeight: Number(video.videoHeight || 0),
    error: video.error ? {code: Number(video.error.code || 0), message: String(video.error.message || '')} : null,
    quality: (function () {
      try {
        if (!video.getVideoPlaybackQuality) return null;
        const q = video.getVideoPlaybackQuality();
        return {dropped: q.droppedVideoFrames, total: q.totalVideoFrames};
      } catch (e) { return null; }
    })(),
    playback: state
  };
}
"""

CLEANUP_PLAYBACK = r"""
() => {
  try {
    const state = window.__ytplay;
    if (!state) return false;
    const videos = Array.from(document.querySelectorAll('video'));
    const video = videos.find(v => v.videoWidth > 0) || videos[0];
    if (video && state.onTime) video.removeEventListener('timeupdate', state.onTime);
    if (video && state.onError) video.removeEventListener('error', state.onError);
    try { if (video && !video.paused) video.pause(); } catch (e) {}
    delete window.__ytplay;
  } catch (e) {}
  return true;
}
"""

DOM_SCAN = r"""
() => {
  const norm = v => (v == null ? '' : String(v).replace(/\s+/g, ' ').trim());
  const cleanTitle = value => norm(value)
    .replace(/\s*[-–]\s*play Short\s*$/i, '')
    .replace(/\s*,\s*[\d.,KMB]+ views?\s*$/i, '')
    .replace(/\s*,\s*[\d.,KMB]+ watching\s*$/i, '')
    .replace(/^Shorts\s*[-–]\s*$/i, '');
  const items = [];
  const seen = new Set();
  const add = (videoId, title, thumb) => {
    if (!videoId || seen.has(videoId)) return;
    seen.add(videoId);
    items.push({video_id: videoId, title: title || null, thumbnail_url: thumb || null});
  };
  document.querySelectorAll('a[href*="/shorts/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const match = href.match(/\/shorts\/([A-Za-z0-9_-]{6,})/);
    if (!match) return;
    let title = norm(a.getAttribute('title')) || norm(a.getAttribute('aria-label'));
    if (!title) {
      const node = a.querySelector('#video-title, yt-formatted-string, span');
      title = norm(node && node.textContent);
    }
    const img = a.querySelector('img');
    const thumb = img ? (img.currentSrc || img.src) : null;
    add(match[1], cleanTitle(title), thumb);
  });
  const doc = document.documentElement;
  const body = document.body;
  return {
    items: items,
    scrollHeight: Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0),
    scrollY: window.scrollY || 0,
    viewport: window.innerHeight || 0
  };
}
"""

SCROLL_DOWN = r"""
(dy) => {
  window.scrollBy(0, dy);
  return {scrollY: window.scrollY || 0};
}
"""
