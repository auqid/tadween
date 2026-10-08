"use strict";
// Tadween web UI - plain JavaScript, no build step. Talks to the local API in tadween/server.py.
// Transcript text is untrusted (it comes from audio), so it is only ever inserted as text nodes.

const $ = (sel, root = document) => root.querySelector(sel);
const SPEAKER_HUES = 8; // --s1 … --s8 in style.css; later speakers share a neutral grey rather than repeating a hue
const RATES = [1, 1.25, 1.5, 1.75, 2];
const state = { list: [], t: null, settings: {}, live: { running: false }, capture: true, query: "", follow: true, editing: null, stale: false, regrouping: 0, reloadedAt: 0, playingId: null, seeking: false, rate: 1 };
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

// ---------- small helpers ----------

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (["value", "checked", "selected", "disabled"].includes(k)) node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) node.append(kid instanceof Node ? kid : String(kid));
  return node;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Tadween": "1" } };
  if (body instanceof Blob) opts.body = body;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const isJson = (res.headers.get("Content-Type") || "").includes("json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((isJson && data.error) || res.statusText);
  return data;
}

const fmt = (sec) => {
  const s = Math.max(0, Math.floor(sec || 0)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
};
const fmtDur = (sec) => { const m = Math.round((sec || 0) / 60); return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`; };
const fmtDate = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const plural = (n, word, many = `${word}s`) => `${n.toLocaleString()} ${n === 1 ? word : many}`;
const escapeRx = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const squash = (s) => s.replace(/\s+/g, " ").trim();
const isDefaultName = (name) => /^Speaker \d+$/.test(name);
const mac = () => (state.platform || "mac") === "mac";
const computer = () => (mac() ? "Mac" : "computer");

function color(label) {
  if (label === "ME") return "var(--me)";
  const n = parseInt(String(label).slice(1), 10) || 1;
  return n <= SPEAKER_HUES ? `var(--s${n})` : "var(--s-other)";
}

// Static, trusted SVG markup only - never put transcript text in here.
const ICONS = {
  play: '<path d="M5 3.3v9.4a.6.6 0 0 0 .9.5l7.4-4.7a.6.6 0 0 0 0-1L5.9 2.8a.6.6 0 0 0-.9.5z"/>',
  pause: '<rect x="4" y="3" width="2.8" height="10" rx=".8"/><rect x="9.2" y="3" width="2.8" height="10" rx=".8"/>',
  back: '<path d="M3.2 6.2A5.3 5.3 0 1 1 2.7 9"/><path d="M2.8 2.9v3.4h3.4"/><text x="8.6" y="10.9" font-size="5.6" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none" font-family="-apple-system, system-ui, sans-serif">5</text>',
  search: '<circle cx="7" cy="7" r="4.3"/><path d="m10.2 10.2 3.3 3.3"/>',
  upload: '<path d="M8 10.5V2.8M4.9 5.8 8 2.7l3.1 3.1M2.8 10.2v1.9c0 .7.6 1.3 1.3 1.3h7.8c.7 0 1.3-.6 1.3-1.3v-1.9"/>',
  stop: '<rect x="2" y="2" width="12" height="12" rx="2"/>',
  voice: '<path d="M3 6.5v3M6 4v8M9 5.5v5M12 7v2"/>',
};
const SOLID = new Set(["play", "pause", "stop"]);
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon i-${name}${SOLID.has(name) ? " solid" : ""}`);
  svg.innerHTML = ICONS[name];
  return svg;
}

/** Split text into nodes, wrapping every match of the phrases in <tag class=cls>. */
function highlight(text, phrases, tag = "mark", cls = "") {
  const list = phrases.filter((p) => p && p.trim());
  if (!list.length) return [text];
  const rx = new RegExp(`(${list.map((p) => escapeRx(p.trim())).join("|")})`, "gi");
  return text.split(rx).map((part, i) => (i % 2 ? el(tag, { class: cls, title: cls === "fixed" ? "Fixed automatically from your word list" : null }, part) : part));
}

function toast(message, actions = [], { timeout = 5000, kind = "" } = {}) {
  const area = $("#toast-area");
  while (area.children.length >= 4) area.firstChild.remove();
  const box = el("div", { class: `toast ${kind}` }, el("div", {}, message));
  const close = () => box.remove();
  if (actions.length) {
    box.append(el("div", { class: "actions" },
      actions.map(([label, fn, primary]) => el("button", { class: primary ? "primary" : "", onclick: async () => { close(); await fn?.(); } }, label)),
      el("button", { class: "ghost", title: "Dismiss", onclick: close }, "✕")));
  }
  area.append(box);
  if (timeout) setTimeout(close, timeout);
  return close;
}
const fail = (err) => toast(err?.message || String(err), [], { kind: "error", timeout: 9000 });

function popover(anchor, content) {
  closePopover();
  if (!anchor) return;
  const opener = document.activeElement;
  const pop = $("#popover");
  pop.replaceChildren(content);
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.left = `${Math.max(10, Math.min(r.left, innerWidth - pop.offsetWidth - 10))}px`;
  const below = r.bottom + 6, above = r.top - pop.offsetHeight - 6;
  pop.style.top = `${below + pop.offsetHeight < innerHeight - 10 || above < 10 ? below : above}px`;
  const outside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) closePopover(); };
  const esc = (e) => { if (e.key === "Escape") closePopover(); };
  setTimeout(() => { document.addEventListener("mousedown", outside); document.addEventListener("keydown", esc); });
  state.closePop = () => {
    const inside = pop.contains(document.activeElement);
    pop.hidden = true; document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", esc); state.closePop = null;
    if (inside) (anchor.isConnected ? anchor : opener)?.focus?.(); // keyboard users carry on where they were
  };
  (pop.querySelector("input:not([type=checkbox])") || pop.querySelector("button:not(:disabled), a[href], select"))?.focus();
}
function closePopover() { state.closePop?.(); }

// Blur first: WebKit and Firefox fire no blur for a removed element, so a line or title being edited would never be saved.
const view = (...nodes) => { if ($("#view").contains(document.activeElement)) document.activeElement.blur(); $("#view").replaceChildren(...nodes); };
const field = (label, input, help) => el("label", { class: "field" }, el("span", {}, label), input, help ? el("div", { class: "help" }, help) : null);

// ---------- sidebar ----------

async function loadList() {
  state.list = await api("GET", "/api/transcripts");
  renderList();
}

function statusEls(item) {
  if (item.status === "live") return el("span", { class: "sub live" }, el("span", { class: "rec" }), "Recording now");
  if (item.status === "queued") return el("span", { class: "sub" }, el("span", {}, "Waiting to process…"));
  if (item.status === "processing") {
    return [el("span", { class: "sub" }, el("span", {}, item.progress?.stage || "Processing…")),
      el("span", { class: "mini-bar" }, el("i", { style: `width:${Math.round((item.progress?.fraction || 0) * 100)}%` }))];
  }
  if (item.status === "error") return el("span", { class: "sub error" }, el("span", {}, "Failed. Open it to retry."));
  return el("span", { class: "sub" }, el("span", {}, fmtDate(item.created)), el("span", { class: "num" }, fmtDur(item.duration)));
}

function renderList() {
  const items = state.list.map((item) => el("li", {},
    el("a", { href: `#/t/${item.id}`, class: state.t?.id === item.id ? "active" : "", "aria-current": state.t?.id === item.id ? "page" : null },
      el("span", { class: "title" }, item.title), statusEls(item))));
  const focused = $("#transcript-list").contains(document.activeElement) && document.activeElement.getAttribute("href");
  $("#transcript-list").replaceChildren(...(items.length ? items : [el("li", { class: "empty" }, "No transcripts yet")]));
  if (focused) $(`#transcript-list a[href="${focused}"]`)?.focus(); // progress updates rebuild the list
  const live = state.live.running;
  $("#live-link").classList.toggle("on", live);
  $("#live-label").textContent = live ? "Recording" : "Start live transcription";
  tickClocks();
}

function tickClocks() {
  const live = state.live.running;
  const since = live ? fmt(Date.now() / 1000 - state.live.started) : "";
  $("#live-time").textContent = since;
  const node = $("#elapsed");
  if (node && live) node.textContent = since;
  const notice = $("#live-notice"), content = live ? liveNotice() : null;
  if (notice && notice.childElementCount !== (content ? 1 : 0)) notice.replaceChildren(...(content ? [content] : []));
}

function markNav(page) {
  document.querySelectorAll(".nav-link").forEach((a) => {
    if (a.getAttribute("href") === `#/${page}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function setNav(open) {
  const was = document.body.classList.contains("nav-open");
  document.body.classList.toggle("nav-open", open);
  $("#menu-button").setAttribute("aria-expanded", String(open));
  if (open && !was) $("#live-link").focus();
  else if (!open && was && $("#sidebar").contains(document.activeElement)) $("#menu-button").focus();
}
addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("nav-open")) setNav(false); });
$("#menu-button").addEventListener("click", () => setNav(!document.body.classList.contains("nav-open")));
$("#scrim").addEventListener("click", () => setNav(false));

// ---------- router ----------

function route() {
  closePopover();
  hideFixButton();
  fixToasts.splice(0).forEach((close) => close());
  const [, page, id] = (location.hash.slice(1) || "/").split("/");
  setNav(false);
  markNav(page);
  if (page !== "t") { state.t = null; $("#player-bar").hidden = true; $("#player-bar audio")?.pause(); }
  if (page === "t" && id) return openTranscript(id);
  renderList();
  if (page === "live") return renderLive();
  if (page === "vocabulary") return renderVocabulary();
  if (page === "people") return renderPeople();
  if (page === "settings") return renderSettings();
  renderHome();
}

const pageView = (...kids) => el("div", { class: "page" }, el("div", { class: "col" }, kids));

function renderHome() {
  view(pageView(
    el("h1", {}, "Transcribe a call"),
    el("p", { class: "lede" }, `Add a recording, or transcribe a call live while it happens. Everything runs on this ${computer()}, so audio never leaves it.`),
    el("label", { class: "dropzone" },
      icon("upload"),
      el("strong", {}, "Drop a recording here, or choose a file"),
      el("span", { class: "muted" }, "Zoom, Meet and Teams recordings or voice memos: m4a, mp3, mp4, wav or mov"),
      el("input", { type: "file", class: "visually-hidden", accept: "audio/*,video/*,.m4a,.mp3,.mp4,.wav,.mov,.webm,.ogg,.flac",
        onchange: (e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; } })),
    el("div", { class: "or-live" },
      el("a", { class: "btn", href: "#/live" }, el("span", { class: "rec" }), "Start live transcription"),
      el("span", { class: "muted" }, "Your mic is labelled as you. The call audio is everyone else.")),
    el("dl", { class: "facts" },
      el("div", {}, el("dt", {}, "Names that stick"), el("dd", {}, "Click a speaker in a transcript and type their name. Tadween remembers the voice and labels them in later calls.")),
      el("div", {}, el("dt", {}, "Fix once"), el("dd", {}, "Correct a word and Tadween offers to fix every occurrence, and to fix it in future transcripts too.")),
      el("div", {}, el("dt", {}, `Stays on this ${computer()}`), el("dd", {}, "Speech recognition and voice matching run locally. Voices are stored as voiceprints, not audio.")))));
}

// ---------- transcript ----------

async function openTranscript(id) {
  let t;
  try {
    t = await api("GET", `/api/transcripts/${id}`);
  } catch (e) {
    if (location.hash === `#/t/${id}`) { fail(e); location.hash = "#/"; }
    return;
  }
  if (location.hash !== `#/t/${id}`) return; // the user opened something else while it loaded
  state.t = t;
  state.stale = false;
  if (t.status === "live") { location.hash = "#/live"; return; }
  renderList();
  renderTranscript();
}

// While the user types in a line, the title or the search box, a reload would replace it, so it waits (state.stale).
const busy = () => state.editing || document.activeElement?.matches(".header h1, .search input");

async function reloadTranscript() {
  const id = state.t?.id;
  if (!id) return;
  if (busy()) { state.stale = true; return; }
  const fresh = await api("GET", `/api/transcripts/${id}`);
  if (state.t?.id !== id || location.hash !== `#/t/${id}`) return; // the user moved on meanwhile
  if (busy()) { state.stale = true; return; }
  state.t = fresh;
  state.stale = false;
  state.reloadedAt = Date.now();
  renderTranscript(true);
}

function renderTranscript(keepScroll = false) {
  const t = state.t;
  const ready = t.status === "ready";
  const top = keepScroll ? $("#turns-scroll")?.scrollTop || 0 : 0;
  const title = el("h1", { contenteditable: ready ? "plaintext-only" : "false", spellcheck: "false",
    onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } },
    onblur: async (e) => {
      const v = e.target.textContent.trim();
      if (v && v !== t.title) { try { await api("PATCH", `/api/transcripts/${t.id}`, { title: v }); t.title = v; await loadList(); } catch (err) { fail(err); } }
      else e.target.textContent = t.title;
      if (state.stale) reloadTranscript();
    } }, t.title);
  const exportMenu = (e) => popover(e.currentTarget, el("div", {},
    el("div", { class: "pop-title" }, "Download transcript"),
    [["txt", "Plain text (.txt)"], ["md", "Markdown (.md)"], ["srt", "Subtitles (.srt)"], ["vtt", "WebVTT (.vtt)"]].map(([f, label]) =>
      el("a", { class: "btn block", href: `/api/transcripts/${t.id}/export?format=${f}`, download: "", onclick: closePopover }, label))));
  const remove = async () => {
    if (!confirm(`Delete "${t.title}" and its audio? This can't be undone.`)) return;
    try { await api("DELETE", `/api/transcripts/${t.id}`); location.hash = "#/"; await loadList(); } catch (e) { fail(e); }
  };
  const words = t.turns.reduce((n, x) => n + x.text.split(/\s+/).length, 0);
  renderPlayer(t); // first: a different call's audio resets the playing line before the lines are drawn
  view(
    el("div", { class: "header" },
      el("div", { class: "col indent" },
        el("div", { class: "row1" }, title,
          ready ? el("button", { onclick: exportMenu }, "Export") : null,
          t.status !== "live" && t.status !== "processing" ? el("button", { class: "ghost quiet-danger", onclick: remove }, "Delete") : null),
        el("div", { class: "meta-line" }, el("span", {}, fmtDate(t.created)), el("span", {}, fmtDur(t.duration)), ready ? el("span", {}, plural(words, "word")) : null),
        statusEl(t),
        ready || t.turns.length ? el("div", { id: "speakers" }, speakersEl(t)) : null,
        ready ? toolbarEl() : null)),
    el("div", { id: "turns-scroll", class: "scroll" }, el("div", { id: "turns", class: "col" }, turnsEls(t))));
  $("#turns-scroll").scrollTop = top;
}

function statusEl(t) {
  if (t.status === "ready" || t.status === "live") return null;
  if (t.status === "error") {
    return el("div", { class: "banner error" }, el("div", { style: "flex:1" }, el("b", {}, "Processing failed. "), t.error || ""),
      el("button", { onclick: async () => { try { await api("POST", `/api/transcripts/${t.id}/retry`); await reloadTranscript(); } catch (e) { fail(e); } } }, "Retry"));
  }
  const p = t.progress || {};
  const pct = Math.round((p.fraction || 0) * 100);
  return el("div", { class: "banner", id: "progress" },
    el("div", { class: "progress" },
      el("div", { class: "progress-top" },
        el("span", { id: "progress-stage" }, t.status === "queued" ? "Waiting for the previous job…" : p.stage || "Starting…"),
        el("span", { class: "num", id: "progress-pct" }, `${pct}%`)),
      el("div", { class: "bar" }, el("div", { id: "progress-fill", style: `width:${pct}%` }))));
}

function speakersEl(t) {
  const labels = Object.keys(t.speakers || {});
  const total = labels.reduce((sum, k) => sum + (t.speakers[k].talk || 0), 0) || 1;
  const ready = t.status === "ready";
  return [
    el("div", { class: "speakers" }, labels.map((k) => {
      const s = t.speakers[k];
      return el("button", { class: "chip", style: `--c:${color(k)}`, disabled: !ready, onclick: (e) => speakerMenu(e.currentTarget, k) },
        el("span", { class: "swatch" }), s.name,
        el("span", { class: "talk" }, `${Math.round((100 * (s.talk || 0)) / total)}%`),
        s.person && !s.manual ? voiceMatchEl(s) : null);
    })),
    ready ? el("div", { class: "speakers-foot" },
      labels.some((k) => isDefaultName(t.speakers[k].name))
        ? el("span", { class: "hint" }, "Click a speaker to name them. Tadween remembers their voice for next time.") : null,
      regroupEl(t)) : null,
  ];
}

const voiceMatchEl = (s) => el("span", { class: "voice-match", title: `Recognised from a saved voice${s.score ? ` (${Math.round(s.score * 100)}% match)` : ""}. Click to correct.` },
  icon("voice"), el("span", { class: "visually-hidden" }, "recognised by voice"));

function regroupEl(t) {
  const sel = el("select", { title: "How many people talk in this call? Tadween regroups the voices.", onchange: (e) => regroup(e.target.value) },
    el("option", { value: "" }, "Auto"),
    Array.from({ length: 11 }, (_, i) => i + 2).map((k) => el("option", { value: k, selected: t.num_speakers === k }, String(k))));
  return el("label", { class: "regroup" }, "Speakers", sel);
}

function toolbarEl() {
  const count = el("span", { id: "match-count" });
  const replace = el("button", { class: "link", hidden: !state.query.trim(), title: "Replace this word or phrase everywhere",
    onclick: (e) => state.query.trim() && openReplace(e.currentTarget, state.query.trim()) }, "Replace…");
  const search = el("input", { type: "search", placeholder: "Search this transcript", "aria-label": "Search this transcript", value: state.query,
    oninput: (e) => { state.query = e.target.value; replace.hidden = !state.query.trim(); renderTurns(); },
    onblur: () => { if (state.stale) reloadTranscript(); } });
  return el("div", { class: "toolbar" }, el("div", { class: "search" }, icon("search"), search), count, replace);
}

function renderTurns() {
  const box = $("#turns");
  if (box) box.replaceChildren(...turnsEls(state.t));
}

function turnsEls(t) {
  const q = state.query.trim().toLowerCase();
  const out = [];
  let prev = null, matches = 0;
  for (const turn of t.turns) {
    if (q && !turn.text.toLowerCase().includes(q)) { prev = null; continue; }
    if (q) matches += turn.text.toLowerCase().split(q).length - 1;
    out.push(turnEl(t, turn, prev));
    prev = turn;
  }
  const counter = $("#match-count");
  if (counter) counter.textContent = q ? plural(matches, "match", "matches") : "";
  if (!t.turns.length) out.push(el("p", { class: "empty-turns" }, t.status === "ready" ? "No speech was found in this recording." : "The transcript appears here when processing is done."));
  else if (q && !out.length) out.push(el("p", { class: "empty-turns" }, `Nothing in this transcript matches “${state.query.trim()}”.`));
  return out;
}

function turnEl(t, turn, prev) {
  const s = (t.speakers || {})[turn.speaker] || { name: turn.speaker || "…" };
  const cont = prev && prev.speaker === turn.speaker;
  const ready = t.status === "ready";
  return el("div", { class: `turn${cont ? " cont" : ""}${turn.id === state.playingId ? " playing" : ""}`, "data-id": turn.id, style: `--c:${color(turn.speaker)}` },
    el("div", { class: "gutter" },
      cont && ready ? el("button", { class: "mini", title: `${s.name}. Click to change the speaker of this line`, "aria-label": `Change speaker (${s.name})`, onclick: (e) => turnSpeakerMenu(e.currentTarget, turn) }, "⇄") : null,
      ready ? el("button", { class: "time", title: "Play from here", onclick: () => playFrom(turn.start) }, fmt(turn.start))
        : el("span", { class: "time" }, fmt(turn.start))), // there's no player until it's ready
    el("div", {},
      el("button", { class: "who", title: ready ? "Change or name this speaker" : "", disabled: !ready, onclick: (e) => turnSpeakerMenu(e.currentTarget, turn) }, s.name),
      textEl(turn, ready)));
}

function textEl(turn, editable = true) {
  const p = el("p", { class: editable ? "text editable" : "text", dir: "auto", onclick: editable ? (e) => maybeEdit(e, turn) : null });
  const q = state.query.trim();
  if (q) p.append(...highlight(turn.text, [q]));
  // Whisper's own words, while they still spell the line: fixes replayed by a regroup change only the text.
  else if (!turn.edited && !turn.autofix && turn.words?.length && squash(turn.words.map((w) => w.w).join("")) === squash(turn.text)) {
    turn.words.forEach((w, i) => {
      const txt = i === 0 ? w.w.trimStart() : w.w;
      p.append(w.p < 0.25 && w.w.replace(/[^\p{L}\p{M}\p{N}]/gu, "").length >= 3
        ? el("span", { class: "unsure", title: `Whisper wasn't sure about this word (${Math.round(w.p * 100)}%). Click to fix.` }, txt)
        : txt);
    });
  } else if (turn.autofix?.length) p.append(...highlight(turn.text, turn.autofix, "span", "fixed"));
  else p.append(turn.text);
  return p;
}

// ---------- editing: fix a word once, fix it everywhere ----------

function maybeEdit(e, turn) {
  if (state.editing || state.regrouping) return; // regrouping renumbers the lines
  const sel = getSelection();
  if (sel && !sel.isCollapsed && sel.toString().trim()) return; // a selection opens "Fix everywhere" instead
  if (state.stale) return reloadTranscript(); // these lines may be out of date: show the current ones first
  const p = e.currentTarget;
  const { clientX: x, clientY: y } = e;
  const tid = state.t.id; // another transcript may be open by the time this line is saved
  let saved;
  state.editing = new Promise((resolve) => { saved = resolve; }); // settles once the line is saved (or left as it was)
  hideFixButton();
  p.textContent = turn.text;
  p.contentEditable = "plaintext-only";
  if (p.contentEditable !== "plaintext-only") p.contentEditable = "true";
  p.classList.add("editing");
  p.focus();
  const caret = document.caretRangeFromPoint?.(x, y);
  if (caret && p.contains(caret.startContainer)) { sel.removeAllRanges(); sel.addRange(caret); }
  let cancelled = false;
  const onKey = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); p.blur(); }
    else if (ev.key === "Escape") { ev.preventDefault(); cancelled = true; p.blur(); }
  };
  const onBlur = async () => {
    p.removeEventListener("keydown", onKey);
    p.removeEventListener("blur", onBlur);
    p.contentEditable = "false";
    p.classList.remove("editing");
    const text = p.innerText.replace(/\s+/g, " ").trim();
    try {
      if (!cancelled && text && text !== turn.text) {
        const res = await api("PUT", `/api/transcripts/${tid}/turns/${turn.id}`, { text });
        Object.assign(turn, res.turn);
        if (state.t?.id === tid) offerFixes(res.suggestions, tid);
      }
    } catch (err) { fail(err); }
    p.replaceWith(textEl(turn));
    state.editing = null;
    saved();
    if (state.stale) reloadTranscript();
  };
  p.addEventListener("keydown", onKey);
  p.addEventListener("blur", onBlur);
}

const fixToasts = []; // closers of the "Fix all" offers: they are about the transcript on screen, so leaving it closes them

function offerFixes(suggestions, tid) {
  for (const s of (suggestions || []).slice(0, 3)) {
    const what = `“${s.from}” → “${s.to}”`;
    if (s.count > 0) {
      fixToasts.push(toast(`${what}. It appears ${plural(s.count, "more time")} in this transcript.`, [
        [`Fix all ${s.count}`, () => replaceAll(tid, s.from, s.to, false), true],
        ["Fix all + remember", () => replaceAll(tid, s.from, s.to, true)],
        ["Review…", () => openReplace(null, s.from, s.to)],
      ], { timeout: 0 }));
    } else {
      toast(`${what}. Fix it automatically in future transcripts too?`, [["Remember this fix", () => rememberFix(s.from, s.to), true]], { timeout: 12000 });
    }
  }
}

async function replaceAll(tid, from, to, remember) {
  try {
    const r = await api("POST", `/api/transcripts/${tid}/replace`, { from, to, remember });
    toast(`Replaced ${plural(r.count, "time")}${remember ? " - future transcripts will be fixed too" : ""}.`);
    if (state.t?.id === tid) await reloadTranscript();
  } catch (e) { fail(e); }
}

async function rememberFix(from, to) {
  try { await api("POST", "/api/vocabulary/corrections", { from, to }); toast(`Saved. “${from}” will become “${to}” in future transcripts.`); } catch (e) { fail(e); }
}

async function openReplace(anchor, from, to = "") {
  const tid = state.t.id;
  const input = el("input", { value: to, placeholder: "Correct spelling" });
  const remember = el("input", { type: "checkbox", checked: true });
  const count = el("div", { class: "muted small" }, "Counting…");
  const list = el("div", { class: "occ" });
  const go = el("button", { class: "primary", onclick: async () => {
    const v = input.value.trim();
    if (!v) return input.focus();
    closePopover();
    await replaceAll(tid, from, v, remember.checked);
  } }, "Replace all");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
  const target = anchor || $(".header h1");
  popover(target, el("div", {},
    el("div", { class: "pop-title" }, "Fix everywhere"),
    el("div", {}, "Replace ", el("b", {}, `“${from}”`), " with"), input, count, list,
    el("label", { class: "check" }, remember, "Also fix it in future transcripts"),
    el("div", { class: "row" }, go)));
  try {
    const hits = await api("GET", `/api/transcripts/${tid}/occurrences?q=${encodeURIComponent(from)}`);
    count.textContent = `Appears ${plural(hits.length, "time")} in this transcript${hits.length > 6 ? " - first 6:" : ""}`;
    list.replaceChildren(...hits.slice(0, 6).map((h) => el("div", { class: "hit" },
      el("button", { class: "time", onclick: () => playFrom(h.start) }, fmt(h.start)), "…", h.before, el("mark", {}, h.match), h.after, "…")));
  } catch (e) { count.textContent = e.message; }
}

function hideFixButton() { $("#fix-button").hidden = true; }

document.addEventListener("mouseup", (e) => {
  if (state.editing || !state.t || state.t.status !== "ready" || e.target.closest("#fix-button, #popover")) return;
  setTimeout(() => {
    const sel = getSelection();
    const text = sel?.toString().replace(/\s+/g, " ").replace(/^[\s\p{P}]+|[\s\p{P}]+$/gu, ""); // without a comma or quote dragged in
    const inText = sel?.anchorNode?.parentElement?.closest?.(".text");
    if (!text || text.length > 60 || !inText) return hideFixButton();
    const r = sel.getRangeAt(0).getBoundingClientRect();
    const box = $("#fix-button");
    box.replaceChildren(el("button", { class: "primary", onmousedown: (ev) => ev.preventDefault(),
      onclick: (ev) => { openReplace(ev.currentTarget, text); hideFixButton(); } },
      `Fix “${text.length > 24 ? text.slice(0, 24) + "…" : text}” everywhere`));
    box.hidden = false;
    box.style.left = `${Math.max(10, r.left)}px`;
    box.style.top = `${Math.max(10, r.top - 40)}px`;
  });
});

// ---------- speakers ----------

function speakerMenu(anchor, label) {
  if (state.regrouping) return; // the labels are about to change
  const t = state.t, s = t.speakers[label];
  const name = el("input", { value: isDefaultName(s.name) ? "" : s.name, placeholder: isDefaultName(s.name) ? "Their name" : s.name });
  const remember = el("input", { type: "checkbox", checked: true });
  const save = async () => {
    const v = name.value.trim();
    if (!v) return name.focus();
    closePopover();
    try {
      const spk = await api("POST", `/api/transcripts/${t.id}/speakers/${label}/rename`, { name: v, remember: remember.checked });
      toast(!remember.checked || label === "ME" ? `Renamed to ${v}.`
        : spk[label]?.person ? `Saved. Tadween will recognise ${v}'s voice in future calls.`
          : `Renamed to ${v}. There isn't enough of their voice yet for Tadween to remember it.`);
      await reloadTranscript();
      loadList();
    } catch (e) { fail(e); }
  };
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  const others = Object.keys(t.speakers).filter((k) => k !== label);
  const into = el("select", {}, others.map((k) => el("option", { value: k }, t.speakers[k].name)));
  popover(anchor, el("div", {},
    el("div", { class: "pop-title" }, label === "ME" ? "Your name" : `Who is ${s.name}?`),
    el("button", { class: "link", onclick: () => playSample(label) }, "Play a sample of their voice"),
    name,
    label !== "ME" ? el("label", { class: "check" }, remember, "Remember this voice for future calls") : null,
    el("div", { class: "row" }, el("button", { class: "primary", onclick: save }, "Save name")),
    others.length ? el("div", { class: "sep" }) : null,
    others.length ? el("div", { class: "row" }, el("span", { class: "muted" }, "Same person as"), into,
      el("button", { onclick: () => merge(label, into.value) }, "Merge")) : null));
}

function turnSpeakerMenu(anchor, turn) {
  if (state.regrouping) return; // the lines are about to be renumbered
  const t = state.t;
  const move = async (label) => {
    closePopover();
    try { await api("PUT", `/api/transcripts/${t.id}/turns/${turn.id}`, { speaker: label }); await reloadTranscript(); } catch (e) { fail(e); }
  };
  popover(anchor, el("div", {},
    el("div", { class: "pop-title" }, "Who said this?"),
    Object.keys(t.speakers).map((k) => el("button", { class: "menu-item", style: `--c:${color(k)}`, disabled: k === turn.speaker, onclick: () => move(k) },
      el("span", { class: "swatch" }), t.speakers[k].name, k === turn.speaker ? el("span", { class: "muted small" }, "(now)") : null)),
    el("button", { class: "menu-item", onclick: () => move("new") }, "+ Someone else (new speaker)"),
    el("div", { class: "sep" }),
    el("button", { class: "link", onclick: (e) => speakerMenu(anchor, turn.speaker) }, `Rename ${(t.speakers[turn.speaker] || {}).name || "this speaker"}…`)));
}

async function merge(src, dst) {
  const t = state.t;
  closePopover();
  try {
    await api("POST", `/api/transcripts/${t.id}/speakers/merge`, { from: src, into: dst });
    await reloadTranscript();
    toast(`Merged into ${state.t?.speakers?.[dst]?.name || t.speakers[dst].name}.`);
  } catch (e) { fail(e); }
}

async function regroup(n) {
  const t = state.t;
  $(".text.editing")?.blur();
  await state.editing; // regrouping renumbers the lines, so a line being edited is saved under its id first
  if (t.turns.some((x) => x.edited) && !confirm("Regrouping voices rebuilds the lines. Word fixes are kept, but other hand edits will be lost. Continue?")) {
    renderTranscript(true);
    return;
  }
  state.regrouping++;
  const close = toast("Regrouping voices…", [], { timeout: 0 });
  try { await api("POST", `/api/transcripts/${t.id}/regroup`, { speakers: n ? Number(n) : null }); await reloadTranscript(); } catch (e) { fail(e); }
  state.regrouping--;
  close();
}

function playSample(label) {
  const turns = state.t.turns.filter((x) => x.speaker === label);
  const best = turns.find((x) => x.end - x.start >= 4) || turns.sort((a, b) => (b.end - b.start) - (a.end - a.start))[0];
  if (best) playFrom(best.start);
}

// ---------- audio: the seek bar is the call's voice timeline ----------

const audioEl = () => $("#player-bar audio");

function renderPlayer(t) {
  const bar = $("#player-bar");
  if (t.status !== "ready") { // nothing to play yet: drop the last call's player too, so no click can play it unseen
    audioEl()?.pause(); bar.replaceChildren(); delete bar.dataset.id; bar.hidden = true; state.playingId = null; return;
  }
  if (bar.dataset.id !== t.id) {
    bar.dataset.id = t.id;
    state.playingId = null;
    const audio = el("audio", { preload: "metadata", src: `/api/transcripts/${t.id}/audio`,
      ontimeupdate: onTime, onplay: syncPlayer, onpause: syncPlayer, onended: syncPlayer, onloadedmetadata: syncPlayer });
    audio.defaultPlaybackRate = audio.playbackRate = state.rate;
    bar.replaceChildren(el("div", { class: "player" }, audio,
      el("div", { class: "transport" },
        el("button", { class: "icon-btn", title: "Back 5 seconds", "aria-label": "Back 5 seconds", onclick: () => skip(-5) }, icon("back")),
        el("button", { class: "play-btn", "aria-label": "Play", onclick: togglePlay }, icon("play"), icon("pause"))),
      el("div", { class: "track-row" },
        el("span", { class: "clock now" }, "0:00"),
        timelineEl(t),
        el("span", { class: "clock total" }, fmt(t.duration)),
        el("button", { class: "rate", title: "Playback speed", onclick: cycleRate }, `${state.rate}×`),
        el("label", { class: "follow", title: "Keep the line being played in view" },
          el("input", { type: "checkbox", checked: state.follow, onchange: (e) => { state.follow = e.target.checked; } }),
          "Follow audio"))));
  }
  drawTimeline(t);
  bar.hidden = false;
  syncPlayer();
}

function timelineEl(t) {
  const seek = el("input", { type: "range", class: "seek", min: 0, max: t.duration || 0, step: "any", "aria-label": "Position in the call", value: 0,
    oninput: (e) => { const a = audioEl(); if (a) { a.currentTime = Number(e.target.value); syncPlayer(); } },
    onkeydown: (e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); skip(e.key === "ArrowLeft" ? -5 : 5); } },
    onpointerdown: () => { state.seeking = true; },
    onpointerup: () => { state.seeking = false; },
    onchange: () => { state.seeking = false; } });
  return el("div", { class: "timeline", id: "timeline", onpointermove: timelineHover, onpointerleave: (e) => {
    e.currentTarget.querySelector(".tip").hidden = true;
    e.currentTarget.querySelector(".hover-line").hidden = true;
  } }, el("div", { class: "track" }), el("div", { class: "hover-line", hidden: true }), el("div", { class: "playhead" }), el("div", { class: "tip", hidden: true }), seek);
}

/** Consecutive lines by the same speaker, joined across short pauses, so the strip shows who held the floor. */
function voiceRuns(t) {
  const runs = [];
  for (const x of t.turns) {
    const last = runs[runs.length - 1];
    if (last && last.speaker === x.speaker && x.start - last.end < 2) last.end = Math.max(last.end, x.end);
    else runs.push({ speaker: x.speaker, start: x.start, end: x.end });
  }
  return runs;
}

function drawTimeline(t) {
  const track = $("#timeline .track");
  if (!track) return;
  const d = t.duration || 1;
  const pct = (sec) => (100 * sec / d).toFixed(3);
  track.replaceChildren(...voiceRuns(t).map((r) => el("i", { class: "seg",
    style: `--c:${color(r.speaker)};left:${pct(r.start)}%;width:max(1px, calc(${pct(r.end - r.start)}% - 2px))` })));
  $("#timeline .seek").max = t.duration || 0;
}

function turnAt(t, sec) {
  let lo = 0, hi = t.turns.length - 1;
  if (hi < 0) return null;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (t.turns[mid].start <= sec) lo = mid; else hi = mid - 1; }
  const turn = t.turns[lo];
  return turn.start <= sec && sec <= turn.end + 0.5 ? turn : null;
}

function timelineHover(e) {
  const t = state.t;
  if (!t) return;
  const box = e.currentTarget, r = box.getBoundingClientRect();
  const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const sec = f * (t.duration || 0);
  const turn = turnAt(t, sec);
  const tip = box.querySelector(".tip"), line = box.querySelector(".hover-line");
  tip.replaceChildren(el("b", {}, fmt(sec)), ...(turn ? [el("span", { class: "key", style: `--c:${color(turn.speaker)}` }),
    el("span", { class: "who-name" }, (t.speakers[turn.speaker] || {}).name || turn.speaker)] : []));
  tip.hidden = line.hidden = false;
  line.style.left = `${f * 100}%`;
  tip.style.left = `${Math.min(Math.max(0, f * r.width - tip.offsetWidth / 2), r.width - tip.offsetWidth)}px`;
}

function syncPlayer() {
  const a = audioEl(), box = $("#timeline");
  if (!a || !box || !state.t) return;
  const d = state.t.duration || a.duration || 0, now = a.currentTime || 0;
  $(".player").classList.toggle("is-playing", !a.paused);
  $(".play-btn").setAttribute("aria-label", a.paused ? "Play" : "Pause");
  $(".clock.now").textContent = fmt(now);
  box.style.setProperty("--p", d ? Math.min(1, now / d) : 0);
  const seek = box.querySelector(".seek");
  if (!state.seeking) seek.value = now;
  seek.setAttribute("aria-valuetext", `${fmt(now)} of ${fmt(d)}`);
}

function togglePlay() {
  const a = audioEl();
  if (a) a.paused ? a.play().catch(() => {}) : a.pause();
}

function skip(delta) {
  const a = audioEl();
  if (!a) return;
  a.currentTime = Math.min(Math.max(0, a.currentTime + delta), a.duration || state.t?.duration || 0);
  syncPlayer();
}

function cycleRate(e) {
  state.rate = RATES[(RATES.indexOf(state.rate) + 1) % RATES.length];
  const a = audioEl();
  if (a) a.defaultPlaybackRate = a.playbackRate = state.rate;
  e.currentTarget.textContent = `${state.rate}×`;
  try { localStorage.setItem("tadween.rate", String(state.rate)); } catch {}
}

function playFrom(sec) {
  const audio = audioEl();
  if (!audio) return;
  audio.currentTime = Math.max(0, sec - 0.3);
  audio.play().catch(() => {});
}

function onTime(e) {
  if (e.target !== audioEl()) return; // a replaced player's last event
  syncPlayer();
  const t = state.t;
  if (!t || !t.turns.length) return;
  const now = e.target.currentTime;
  let lo = 0, hi = t.turns.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (t.turns[mid].start <= now) lo = mid; else hi = mid - 1; }
  const turn = t.turns[lo];
  if (turn.id === state.playingId) return;
  state.playingId = turn.id;
  document.querySelectorAll(".turn.playing").forEach((n) => n.classList.remove("playing"));
  const node = document.querySelector(`.turn[data-id="${turn.id}"]`);
  if (!node) return;
  node.classList.add("playing");
  if (state.follow && !e.target.paused && !state.editing) node.scrollIntoView({ block: "center", behavior: reduceMotion.matches ? "auto" : "smooth" });
}

// ---------- uploads ----------

async function upload(file) {
  const close = toast(`Uploading ${file.name}…`, [], { timeout: 0 });
  try {
    const r = await api("POST", `/api/transcripts/upload?name=${encodeURIComponent(file.name)}`, file);
    close();
    await loadList();
    location.hash = `#/t/${r.id}`;
  } catch (e) { close(); fail(e); }
}

$("#file-input").addEventListener("change", (e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; });
let dragDepth = 0;
addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("#drop-overlay").hidden = false; } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
addEventListener("dragover", (e) => e.preventDefault());
addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("#drop-overlay").hidden = true;
  const file = e.dataTransfer?.files?.[0];
  if (file) upload(file);
});

// ---------- live ----------

function renderLive() {
  if (!state.live.running) return renderLiveStart();
  const L = state.live;
  const stop = el("button", { class: "stop", onclick: async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.replaceChildren("Saving…");
    try {
      const r = await api("POST", "/api/live/stop");
      state.live = { running: false };
      renderList();
      location.hash = r.id ? `#/t/${r.id}` : "#/";
    } catch (err) { fail(err); btn.disabled = false; btn.replaceChildren(icon("stop"), "Stop"); }
  } }, icon("stop"), "Stop");
  view(
    el("div", { class: "header" },
      el("div", { class: "col indent" },
        el("div", { class: "live-head" }, el("h1", {}, L.title),
          el("span", { class: "recording", id: "elapsed", title: "Recording time" }, fmt((Date.now() / 1000) - L.started)), stop),
        el("div", { class: "sources", id: "sources" }, sourcesEls()),
        el("div", { id: "live-notice" }, liveNotice()),
        el("div", { id: "speakers" }, liveSpeakersEl()))),
    el("div", { id: "turns-scroll", class: "scroll" }, el("div", { id: "turns", class: "col" }, liveLinesEls())));
  scrollLiveToEnd(true);
}

/** Call audio silent while the mic hears speech: say why everyone shows as you, and what happens next. */
function liveNotice() {
  const src = state.live.sources || [];
  const call = src.find((s) => s.name === "system"), mic = src.find((s) => s.name === "mic");
  const quiet = state.live.running && call?.ready && !call.heard && !call.error && mic?.heard
    && Date.now() / 1000 - state.live.started > 20;
  return quiet ? el("p", { class: "banner notice" },
    "The call audio is silent, so everyone your mic hears is shown as you for now. In a meeting with people in the room, "
    + `Tadween tells the voices apart when you press Stop. On a call on this ${computer()}, check that its sound plays on this ${computer()}`
    + { mac: ", and that the app running Tadween is allowed in System Settings › Privacy & Security › Screen & System Audio Recording.",
        windows: ", through the default speakers or headphones.",
        linux: ", through the default output device (PulseAudio or PipeWire)." }[state.platform || "mac"]) : null;
}

function sourcesEls() {
  const names = { mic: "Your mic", system: "Call audio" };
  return (state.live.sources || []).map((s) => el("span", { class: `src${s.error ? " error" : s.speaking ? " speaking" : s.ready ? " ready" : ""}`, title: s.error || "" },
    el("span", { class: "level", "aria-hidden": "true" }, el("i"), el("i"), el("i")),
    `${names[s.name] || s.name}${s.error ? `: ${s.error}` : s.ready ? "" : " (starting…)"}`));
}

function liveSpeakersEl() {
  const sp = state.live.speakers || {};
  return el("div", {},
    el("div", { class: "speakers" }, Object.keys(sp).map((k) =>
      el("button", { class: "chip", style: `--c:${color(k)}`, onclick: (e) => liveRename(e.currentTarget, k) }, el("span", { class: "swatch" }), sp[k].name,
        sp[k].person && !sp[k].manual ? voiceMatchEl(sp[k]) : null))),
    Object.values(sp).some((s) => isDefaultName(s.name))
      ? el("div", { class: "speakers-foot" }, el("span", { class: "hint" }, "Click a speaker to name them as you go.")) : null);
}

function liveLinesEls() {
  const sp = state.live.speakers || {};
  const lines = state.live.lines || [];
  if (!lines.length) return [el("p", { class: "empty-turns" }, "Listening… Lines appear a moment after each person finishes a sentence.")];
  return lines.map((line, i) => liveLineEl(line, lines[i - 1], sp));
}

function liveLineEl(line, prev, sp = state.live.speakers || {}) {
  const cont = prev && prev.speaker === line.speaker;
  return el("div", { class: `turn${cont ? " cont" : ""}`, "data-index": line.index, style: `--c:${color(line.speaker)}` },
    el("div", { class: "gutter" }, el("span", { class: "time" }, fmt(line.start))),
    el("div", {}, el("button", { class: "who", title: "Name this speaker", onclick: (e) => liveRename(e.currentTarget, line.speaker) }, (sp[line.speaker] || {}).name || line.speaker),
      el("p", { class: "text", dir: "auto" }, ...(line.autofix ? highlight(line.text, line.autofix, "span", "fixed") : [line.text]))));
}

function scrollLiveToEnd(force = false) {
  const sc = $("#turns-scroll");
  if (sc && (force || sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160)) sc.scrollTop = sc.scrollHeight;
}

function liveRename(anchor, label) {
  const s = (state.live.speakers || {})[label];
  if (!s) return;
  const name = el("input", { value: isDefaultName(s.name) ? "" : s.name, placeholder: "Their name" });
  const remember = el("input", { type: "checkbox", checked: true });
  const save = async () => {
    const v = name.value.trim();
    if (!v) return name.focus();
    closePopover();
    try { state.live.speakers = await api("POST", `/api/live/speakers/${label}`, { name: v, remember: remember.checked }); refreshLive(); } catch (e) { fail(e); }
  };
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  popover(anchor, el("div", {}, el("div", { class: "pop-title" }, label === "ME" ? "Your name" : `Who is ${s.name}?`), name,
    label !== "ME" ? el("label", { class: "check" }, remember, "Remember this voice for future calls") : null,
    el("div", { class: "row" }, el("button", { class: "primary", onclick: save }, "Save name"))));
}

function refreshLive() {
  if (location.hash !== "#/live" || !state.live.running) return;
  $("#speakers")?.replaceChildren(liveSpeakersEl());
  $("#sources")?.replaceChildren(...sourcesEls());
  $("#turns")?.replaceChildren(...liveLinesEls());
  scrollLiveToEnd();
}

function renderLiveStart() {
  const title = el("input", { placeholder: `Call ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}` });
  const mic = el("input", { type: "checkbox", checked: true });
  const sys = el("input", { type: "checkbox", checked: true });
  const apps = el("input", { placeholder: "Optional, for example zoom.us or Google Chrome" });
  const startLabel = () => [el("span", { class: "rec" }), "Start transcribing"];
  const go = el("button", { class: "primary big", disabled: !state.capture, onclick: async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.replaceChildren("Starting (loading the speech model)…");
    try {
      state.live = await api("POST", "/api/live/start", { title: title.value.trim() || null, mic: mic.checked, system: sys.checked, apps: apps.value.trim() });
      renderList();
      if (location.hash === "#/live") renderLive(); // the user may have opened another page while the model loaded
    } catch (err) { fail(err); btn.disabled = false; btn.replaceChildren(...startLabel()); }
  } }, startLabel());
  const source = (input, label, help) => el("label", { class: "check" }, input, el("span", { class: "check-text" }, el("span", {}, label), el("span", { class: "muted" }, help)));
  view(pageView(
    el("h1", {}, "Live transcription"),
    el("p", { class: "lede" }, `Tadween listens to your microphone (that's you) and to this ${computer()}'s sound output (everyone else on the call), and writes down who said what while you talk.`),
    state.capture ? null : el("div", { class: "warn" }, "Live calls aren't set up yet. In a terminal in the Tadween folder, run ",
      el("code", {}, state.platform === "windows" ? "powershell -ExecutionPolicy Bypass -File setup.ps1" : "./setup.sh"), ", then reload this page."),
    field("Title", title),
    el("div", { class: "field" }, el("span", {}, "Listen to"),
      el("div", { class: "group" },
        source(mic, "Your microphone", "Labelled as you"),
        source(sys, "Call audio", "Everyone else on Zoom, Meet, Teams, Slack or any other app"))),
    mac() ? field("Only capture sound from this app", apps, "Leave empty to capture all sound. Open the meeting app before you start.") : null,
    go,
    el("p", { class: "note" },
      el("b", {}, "First time? "), {
        mac: "macOS asks for Microphone and Screen & System Audio Recording permission for the app that runs Tadween (Terminal or VS Code). Allow both, then quit and reopen that app. ",
        windows: "If your microphone stays silent, allow desktop apps to use it in Settings › Privacy & security › Microphone. ",
        linux: "Tadween records your default microphone and what your default output plays (PulseAudio or PipeWire). ",
      }[state.platform || "mac"],
      "Headphones give the cleanest result. When you press Stop, Tadween transcribes the whole call again with full context and regroups the voices for the final version.")));
}

// ---------- library pages ----------

async function renderVocabulary() {
  let data;
  try { data = await api("GET", "/api/vocabulary"); } catch (e) { return fail(e); }
  const from = el("input", { placeholder: "Heard as, e.g. post gress", "aria-label": "Heard as" });
  const to = el("input", { placeholder: "Should be, e.g. Postgres", "aria-label": "Should be" });
  const add = async () => {
    if (!from.value.trim() || !to.value.trim()) return (from.value.trim() ? to : from).focus();
    try { await api("POST", "/api/vocabulary/corrections", { from: from.value, to: to.value }); renderVocabulary(); } catch (e) { fail(e); }
  };
  for (const input of [from, to]) input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  const terms = el("textarea", { rows: 6, placeholder: "One per line: names, products, jargon", "aria-label": "Words to expect" });
  terms.value = data.terms.join("\n");
  view(pageView(
    el("h1", {}, "Word fixes"),
    el("p", { class: "lede" }, "Fixes you choose to remember are applied to every new transcript. The corrected words are also given to Whisper, so it spells them right in the first place."),
    el("table", { class: "list" },
      el("thead", {}, el("tr", {}, el("th", {}, "Heard as"), el("th", {}, "Becomes"), el("th", {}, el("span", { class: "visually-hidden" }, "Actions")))),
      el("tbody", {}, data.corrections.length
        ? data.corrections.map((c) => el("tr", {}, el("td", { class: "heard" }, c.from), el("td", { class: "strong" }, c.to),
          el("td", {}, el("button", { class: "ghost quiet-danger", onclick: async () => {
            try { await api("DELETE", `/api/vocabulary/corrections?from=${encodeURIComponent(c.from)}`); renderVocabulary(); } catch (e) { fail(e); }
          } }, "Remove"))))
        : el("tr", {}, el("td", { colspan: 3, class: "empty" }, "No fixes yet. Edit a word in a transcript and choose “remember”, or add one below.")))),
    el("div", { class: "inline-form" }, from, to, el("button", { onclick: add }, "Add fix")),
    el("h2", {}, "Words to expect"),
    el("p", { class: "lede" }, "Names, products and jargon that come up in your calls. Whisper is told to expect them."),
    terms,
    el("div", { class: "actions-row" }, el("button", { class: "primary", onclick: async () => {
      try { await api("PUT", "/api/vocabulary/terms", { terms: terms.value.split("\n") }); toast("Words saved."); } catch (e) { fail(e); }
    } }, "Save words"))));
}

async function renderPeople() {
  let people;
  try { people = await api("GET", "/api/people"); } catch (e) { return fail(e); }
  view(pageView(
    el("h1", {}, "Known voices"),
    el("p", { class: "lede" }, "When you name a speaker with “remember this voice”, Tadween stores a voiceprint (numbers describing the voice, not audio) and labels that person automatically in later calls."),
    el("table", { class: "list" },
      el("thead", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Samples"), el("th", {}, "Last updated"), el("th", {}, el("span", { class: "visually-hidden" }, "Actions")))),
      el("tbody", {}, people.length
        ? people.map((p) => el("tr", {},
          el("td", { class: "strong" }, p.name), el("td", { class: "num" }, String(p.samples)),
          el("td", { class: "heard" }, new Date(p.updated * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })),
          el("td", {},
            el("button", { class: "ghost", onclick: (e) => renamePerson(e.currentTarget, p) }, "Rename"),
            el("button", { class: "ghost quiet-danger", onclick: async () => {
              if (!confirm(`Forget ${p.name}'s voice?`)) return;
              try { await api("DELETE", `/api/people/${p.id}`); renderPeople(); } catch (e) { fail(e); }
            } }, "Forget"))))
        : el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No voices yet. Open a transcript and click a speaker to name them."))))));
}

function renamePerson(anchor, p) {
  const name = el("input", { value: p.name, "aria-label": "Name" });
  const save = async () => {
    const v = name.value.trim();
    if (!v) return name.focus();
    closePopover();
    if (v === p.name) return;
    try { await api("PATCH", `/api/people/${p.id}`, { name: v }); renderPeople(); } catch (e) { fail(e); }
  };
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  popover(anchor, el("div", {}, el("div", { class: "pop-title" }, "Rename this voice"), name,
    el("div", { class: "row" }, el("button", { class: "primary", onclick: save }, "Save name"))));
  name.select();
}

// ---------- this computer: where Whisper runs, chosen automatically unless Settings say otherwise ----------

const ENGINE = { neural_engine: "Neural Engine", gpu: "GPU", cpu: "CPU" };
const secs = (ms) => `${(ms / 1000).toFixed(1)} s`;

/** "Apple M1 · 8 cores (4 performance) · 8 GB memory · Neural Engine" */
function hardwareText(hw = {}) {
  const parts = [hw.cpu || "Unknown processor"];
  if (hw.cores) {
    parts.push(!hw.performance_cores || hw.performance_cores === hw.cores ? `${hw.cores} cores`
      : mac() ? `${hw.cores} cores (${hw.performance_cores} performance)` : `${hw.performance_cores} cores, ${hw.cores} threads`);
  }
  if (hw.memory) parts.push(`${Math.round(hw.memory / 2 ** 30)} GB memory`);
  parts.push(...(hw.gpus || []).filter((g) => !(mac() && hw.cpu && g.startsWith(hw.cpu))));  // a Mac's GPU is its chip
  if (hw.neural_engine) parts.push("Neural Engine");
  return parts.join(" · ");
}

/** What Automatic means here: the speed check's pick, else the first engine this computer has. */
const autoEngine = () => (state.engines.includes(state.speed?.engine) ? state.speed.engine : state.engines[0]);

function engineHelp(s) {
  const sp = state.speed;
  const measured = sp ? `Measured here: ${Object.entries(sp.times).map(([e, ms]) => `${ENGINE[e]} ${ms ? secs(ms) : "failed"}`).join(", ")} for 30 seconds of audio.`
    : "Not measured yet: use Check speed below.";
  const wanted = state.engines.includes(s.engine) ? s.engine : autoEngine();
  const forced = state.engine !== wanted ? ` Right now Whisper runs on the ${ENGINE[state.engine]}, as set by an environment variable when Tadween started.` : "";
  if (state.engines.length > 1) return `Automatic picks the fastest. ${measured}${forced}`;
  if (mac()) return `${measured} On Apple Silicon, ./whisper/build.sh adds the Neural Engine, about 1.7× faster on an M1.`;
  if (!state.whisperBuild) return "The whisper.cpp on your PATH, so Tadween can't tell whether it uses a GPU.";
  return `${measured} ${state.platform === "windows" ? "For an NVIDIA, AMD or Intel GPU, update its driver, then run setup.ps1 again."
    : "For a GPU, run ./whisper/build.sh: it says what to install (the CUDA toolkit for NVIDIA, Vulkan build tools for AMD and Intel)."}`;
}

function liveModelHelp(s) {
  const sp = state.speed, d = state.download || {};
  const auto = !sp ? "Automatic uses Large unless a speed check finds this computer too slow for live lines with it."
    : sp.live_model === "small" ? `Automatic uses Small here: Large needs ${secs(sp.times[sp.engine])} for 30 seconds of audio on the ${ENGINE[sp.engine]}, too slow for live lines.`
      : "Automatic uses Large: it's quick enough here.";
  const wantsSmall = s.live_model === "small" || (s.live_model === "auto" && sp?.live_model === "small");
  const download = d.downloading ? ` Downloading the small model… ${Math.round((d.fraction || 0) * 100)}%.`
    : d.error ? ` The small model's download failed (${d.error}); live lines use Large until it works.`
      : wantsSmall && !state.smallModel ? " The small model (264 MB) downloads when you save; until then live lines use Large." : "";
  return `${auto} Transcripts after a call always use Large.${download}`;
}

function speedText() {
  if (state.speedChecking) return state.speedMessage || "Checking… about a minute.";
  if (state.speedError) return `The check failed: ${state.speedError}`;
  return state.speed ? `Last checked ${new Date(state.speed.checked * 1000).toLocaleString()}. Run it again after a driver or hardware change.`
    : "Times Whisper on each engine this computer has, so Automatic can pick the fastest. About a minute.";
}

function speedRow() {
  const button = el("button", { id: "speed-check", disabled: state.speedChecking, onclick: async () => {
    try {
      await api("POST", "/api/speed-check");
      Object.assign(state, { speedChecking: true, speedMessage: null, speedError: null });
      updateSpeedRow();
    } catch (e) { fail(e); }
  } }, state.speedChecking ? "Checking…" : state.speed ? "Check again" : "Check speed");
  return el("div", { class: "setting", id: "speed-row" },
    el("span", { class: "setting-text" }, el("span", { class: "setting-label" }, "Speed check"), el("span", { class: "setting-help", id: "speed-status" }, speedText())),
    button);
}

function updateSpeedRow() { $("#speed-row")?.replaceWith(speedRow()); }

/** Where Whisper runs and how fast, from /api/state. */
function applyMachine(s) {
  Object.assign(state, { hardware: s.hardware, speed: s.speed, speedChecking: s.speed_checking, engines: s.engines,
    engine: s.engine, liveModel: s.live_model, smallModel: s.small_model, download: s.download, autoThreads: s.auto_threads,
    whisperBuild: s.whisper_build, neuralEngineInstalled: s.neural_engine_installed });
}

async function refreshMachine() {
  let s;
  try { s = await api("GET", "/api/state"); } catch { return; }
  applyMachine(s);
  if (location.hash === "#/settings" && !state.settingsDirty) renderSettings();
  else updateSpeedRow();
}

async function renderSettings() {
  let s;
  try { s = await api("GET", "/api/settings"); } catch (e) { return fail(e); }
  const name = el("input", { value: s.my_name });
  const lang = el("select", {}, [["en", "English"], ["auto", "Detect automatically"], ["ar", "Arabic"], ["hi", "Hindi"], ["ur", "Urdu"], ["es", "Spanish"], ["fr", "French"], ["de", "German"]]
    .map(([v, l]) => el("option", { value: v, selected: s.language === v }, l)));
  const threads = el("select", {}, [["auto", `Automatic (${state.autoThreads})`], ...Array.from({ length: 16 }, (_, i) => [i + 1, String(i + 1)])]
    .map(([v, l]) => el("option", { value: v, selected: String(s.threads) === String(v) }, l)));
  const engineChoice = el("select", { disabled: state.engines.length < 2 }, [["auto", `Automatic (${ENGINE[autoEngine()]})`], ...state.engines.map((e) => [e, ENGINE[e]])]
    .map(([v, l]) => el("option", { value: v, selected: s.engine === v }, l)));
  const liveModel = el("select", {}, [["auto", `Automatic (${state.speed?.live_model === "small" ? "Small" : "Large"})`], ["large", "Large: most accurate"], ["small", "Small: fastest"]]
    .map(([v, l]) => el("option", { value: v, selected: s.live_model === v }, l)));
  const sep = el("input", { type: "range", min: 0.5, max: 1.0, step: 0.05, value: s.speaker_threshold });
  const match = el("input", { type: "range", min: 0.3, max: 0.8, step: 0.05, value: s.voice_match_threshold });
  const prompt = el("input", { value: s.initial_prompt });
  const speed = el("select", {}, [["accurate", "Most accurate"], ["fast", "Faster"]]
    .map(([v, l]) => el("option", { value: v, selected: (s.speed || "accurate") === v }, l)));
  const setting = (label, help, control, stack = false) => el("label", { class: `setting${stack ? " stack" : ""}` },
    el("span", { class: "setting-text" }, el("span", { class: "setting-label" }, label), help ? el("span", { class: "setting-help" }, help) : null), control);
  const scale = (input, left, right) => el("span", {}, input, el("span", { class: "range-ends", "aria-hidden": "true" }, el("span", {}, left), el("span", {}, right)));
  state.settingsDirty = false;
  const edited = () => { state.settingsDirty = true; };  // a speed check finishing mustn't wipe unsaved changes
  const page = pageView(
    el("h1", {}, "Settings"),
    el("h2", {}, "Transcription"),
    el("div", { class: "group" },
      setting("Your name", "Used for your microphone in live calls.", name),
      setting("Language spoken in calls", null, lang),
      setting("Transcription speed", `Faster decodes greedily: ${mac() ? "about a quarter quicker" : "quicker on a GPU, but no quicker on a CPU alone"}. It drops most filler words like “um” and may word a few phrases differently.`, speed),
      setting("Whisper style prompt", "A punctuated sentence Whisper imitates. Keep it short.", prompt)),
    el("h2", {}, "Voices"),
    el("div", { class: "group" },
      setting("Voice grouping", "Applies to new transcripts. To regroup an existing one, use Speakers in its header.", scale(sep, "More speakers", "Fewer speakers"), true),
      setting("Recognising saved voices", "How sure Tadween must be before it names someone from a saved voice.", scale(match, "Name people more eagerly", "Only when very sure"), true)),
    el("h2", {}, "This computer"),
    el("div", { class: "group" },
      el("div", { class: "setting stack" }, el("span", { class: "setting-text" }, el("span", { class: "setting-label" }, "Hardware"),
        el("span", { class: "setting-help" }, hardwareText(state.hardware)))),
      setting("Whisper runs on", engineHelp(s), engineChoice),
      setting("Model for live lines", el("span", { id: "live-model-help" }, liveModelHelp(s)), liveModel),
      setting("CPU threads", "For Whisper and voice recognition. Automatic uses one per performance core. Restart Tadween after changing it.", threads),
      speedRow()),
    el("div", { class: "form-foot" }, el("button", { class: "primary", onclick: async () => {
      try {
        state.settings = await api("PUT", "/api/settings", { my_name: name.value.trim() || "Me", language: lang.value,
          threads: threads.value === "auto" ? "auto" : Number(threads.value), engine: engineChoice.value, live_model: liveModel.value,
          speaker_threshold: Number(sep.value), voice_match_threshold: Number(match.value), initial_prompt: prompt.value.trim(), speed: speed.value });
        state.settingsDirty = false;
        toast("Settings saved.");
        refreshMachine();
      } catch (e) { fail(e); }
    } }, "Save settings")));
  page.addEventListener("input", edited);
  page.addEventListener("change", edited);
  view(page);
}

// ---------- live updates from the server ----------

let reloadTimer = null;
const handlers = {
  transcripts: () => loadList(),
  speed: (m) => {
    Object.assign(state, { speedChecking: m.running, speedMessage: m.running ? m.message : null, speedError: m.error || null });
    if (m.running) updateSpeedRow();
    else { if (m.message) toast(m.message); refreshMachine(); }
  },
  download: (m) => {
    state.download = m;
    if (m.fraction === 1 && !m.downloading) state.smallModel = true;
    const help = $("#live-model-help");
    if (help) help.textContent = liveModelHelp(state.settings);
  },
  progress: (m) => {
    const item = state.list.find((x) => x.id === m.id);
    if (item) { item.status = "processing"; item.progress = m.progress; renderList(); }
    if (state.t?.id === m.id) {
      if (!$("#progress")) return reloadTranscript();
      const pct = `${Math.round((m.progress.fraction || 0) * 100)}%`;
      $("#progress-stage").textContent = m.progress.stage;
      $("#progress-pct").textContent = pct;
      $("#progress-fill").style.width = pct;
    }
  },
  transcript: (m) => {
    if (state.t?.id !== m.id) return;
    clearTimeout(reloadTimer);
    // An action here (Fix all, rename, merge…) already reloaded what this event announces.
    reloadTimer = setTimeout(() => { if (Date.now() - state.reloadedAt > 1500) reloadTranscript(); }, 250);
  },
  live_started: async () => { state.live = await api("GET", "/api/live"); renderList(); if (location.hash === "#/live") renderLive(); },
  live_line: (m) => {
    if (!state.live.running || state.live.id !== m.id) return;
    const prev = state.live.lines.at(-1);
    const sameSpeakers = JSON.stringify(m.speakers) === JSON.stringify(state.live.speakers);
    state.live.lines.push(m.line);
    state.live.speakers = m.speakers;
    const box = $("#turns");
    if (prev && sameSpeakers && box && location.hash === "#/live") { // the usual case: add just the new line
      box.append(liveLineEl(m.line, prev));
      scrollLiveToEnd();
    } else refreshLive();
  },
  live_remove: (m) => { if (state.live.running) { state.live.lines = state.live.lines.filter((x) => x.index !== m.index); refreshLive(); } },
  live_speakers: (m) => { if (state.live.running) { state.live.speakers = m.speakers; refreshLive(); } },
  live_activity: (m) => { const s = (state.live.sources || []).find((x) => x.name === m.source); if (s) { s.speaking = m.speaking; s.heard = m.heard ?? s.heard; s.ready = true; $("#sources")?.replaceChildren(...sourcesEls()); } },
  live_status: (m) => { const s = (state.live.sources || []).find((x) => x.name === m.source); if (s) { s.ready = m.ready; $("#sources")?.replaceChildren(...sourcesEls()); } },
  live_error: (m) => {
    const s = (state.live.sources || []).find((x) => x.name === m.source);
    if (s) s.error = m.message;
    $("#sources")?.replaceChildren(...sourcesEls());
    toast(`${m.source === "mic" ? "Microphone" : "Call audio"}: ${m.message}`, [], { kind: "error", timeout: 15000 });
  },
  live_stopped: () => { state.live = { running: false }; renderList(); if (location.hash === "#/live") renderLive(); },
};

function connectEvents() {
  let es = null, wasDown = false, idle = null;
  const open = () => {
    es = new EventSource("/api/events");
    es.onmessage = (e) => { const m = JSON.parse(e.data); handlers[m.type]?.(m); };
    es.onerror = () => { wasDown = true; };
    es.onopen = async () => {
      if (!wasDown) return;
      wasDown = false; // catch up on what was missed
      const running = state.live.running;
      state.live = await api("GET", "/api/live").catch(() => state.live);
      if (location.hash === "#/live" && (running || state.live.running)) renderLive();
      await loadList().catch(() => {});
      if (state.t) reloadTranscript().catch(() => {});
    };
  };
  // A browser gives all tabs together only 6 connections per host, and each event stream holds one for good,
  // so a tab left in the background for a minute closes its stream and catches up when it's shown again.
  const onVisibility = () => {
    clearTimeout(idle);
    if (!document.hidden) { if (!es) open(); }
    else idle = setTimeout(() => { es?.close(); es = null; wasDown = true; }, 60000);
  };
  document.addEventListener("visibilitychange", onVisibility);
  open();
  onVisibility();
}

setInterval(tickClocks, 1000);

(async function init() {
  try { const r = Number(localStorage.getItem("tadween.rate")); if (RATES.includes(r)) state.rate = r; } catch {}
  try {
    const s = await api("GET", "/api/state");
    state.settings = s.settings;
    state.live = s.live;
    state.capture = s.capture_helper;
    state.platform = s.platform;
    applyMachine(s);
    connectEvents();
    await loadList();
    addEventListener("hashchange", route);
    route();
  } catch (e) { fail(e); }
})();
