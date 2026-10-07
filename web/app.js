"use strict";
// Tadween web UI - plain JavaScript, no build step. Talks to the local API in tadween/server.py.
// Transcript text is untrusted (it comes from audio), so it is only ever inserted as text nodes.

const $ = (sel, root = document) => root.querySelector(sel);
const PALETTE = ["#2563eb", "#db2777", "#059669", "#d97706", "#7c3aed", "#0891b2", "#dc2626", "#65a30d", "#c026d3", "#64748b"];
const ME_COLOR = "#0d9488";
const state = { list: [], t: null, settings: {}, live: { running: false }, capture: true, query: "", follow: true, editing: null, stale: false, playingId: null };

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
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const escapeRx = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const isDefaultName = (name) => /^Speaker \d+$/.test(name);

function color(label) {
  if (label === "ME") return ME_COLOR;
  const n = parseInt(String(label).slice(1), 10);
  return PALETTE[((n || 1) - 1) % PALETTE.length];
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
  state.closePop = () => { pop.hidden = true; document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", esc); state.closePop = null; };
  pop.querySelector("input:not([type=checkbox])")?.focus();
}
function closePopover() { state.closePop?.(); }

const view = (...nodes) => $("#view").replaceChildren(...nodes);
const field = (label, input, help) => el("label", { class: "field" }, el("span", {}, label), input, help ? el("div", { class: "help" }, help) : null);

// ---------- sidebar ----------

async function loadList() {
  state.list = await api("GET", "/api/transcripts");
  renderList();
}

function statusText(item) {
  if (item.status === "live") return "● Live now";
  if (item.status === "queued") return "Waiting to process…";
  if (item.status === "processing") return item.progress?.stage || "Processing…";
  if (item.status === "error") return "Failed - open to retry";
  return `${fmtDate(item.created)} · ${fmtDur(item.duration)}`;
}

function renderList() {
  const items = state.list.map((item) => el("li", {},
    el("a", { href: `#/t/${item.id}`, class: state.t?.id === item.id ? "active" : "" },
      el("span", { class: "title" }, item.title),
      el("span", { class: `sub${item.status === "live" ? " live" : ""}` }, statusText(item)))));
  $("#transcript-list").replaceChildren(...(items.length ? items : [el("li", { class: "empty" }, "Nothing yet")]));
  const live = state.live.running;
  $("#live-link").classList.toggle("on", live);
  $("#live-label").textContent = live ? "Live - recording now" : "Start live transcription";
}

// ---------- router ----------

function route() {
  closePopover();
  hideFixButton();
  const [, page, id] = (location.hash.slice(1) || "/").split("/");
  if (page !== "t") { state.t = null; $("#player-bar").hidden = true; }
  if (page === "t" && id) return openTranscript(id);
  renderList();
  if (page === "live") return renderLive();
  if (page === "vocabulary") return renderVocabulary();
  if (page === "people") return renderPeople();
  if (page === "settings") return renderSettings();
  renderHome();
}

function renderHome() {
  view(el("div", { class: "page narrow" },
    el("h1", {}, "Tadween"),
    el("p", { class: "muted" }, "Private call transcripts with speaker names. Everything runs on this Mac - audio never leaves it."),
    el("label", { class: "dropzone" },
      el("strong", {}, "Drop a recording here, or click to choose one"),
      "Zoom, Meet or Teams recordings, voice memos - m4a, mp3, mp4, wav, mov…",
      el("input", { type: "file", hidden: true, accept: "audio/*,video/*", onchange: (e) => e.target.files[0] && upload(e.target.files[0]) })),
    el("div", { class: "cards" },
      el("div", { class: "card" }, el("b", {}, "Live calls"), "Start live transcription before a call. Your mic is you; the call audio is everyone else."),
      el("div", { class: "card" }, el("b", {}, "Names that stick"), "Name a speaker once. Tadween remembers the voice and labels them in future calls."),
      el("div", { class: "card" }, el("b", {}, "Fix once, fixed everywhere"), "Correct a word and Tadween offers to fix it everywhere - and in future transcripts."))));
}

// ---------- transcript ----------

async function openTranscript(id) {
  try {
    state.t = await api("GET", `/api/transcripts/${id}`);
  } catch (e) {
    fail(e);
    location.hash = "#/";
    return;
  }
  if (state.t.status === "live") { location.hash = "#/live"; return; }
  renderList();
  renderTranscript();
}

async function reloadTranscript() {
  if (!state.t) return;
  if (state.editing) { state.stale = true; return; }
  state.t = await api("GET", `/api/transcripts/${state.t.id}`);
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
  view(
    el("div", { class: "header" },
      el("div", { class: "row1" }, title,
        ready ? el("button", { onclick: exportMenu }, "Export") : null,
        t.status !== "live" && t.status !== "processing" ? el("button", { class: "ghost danger", title: "Delete", onclick: remove }, "Delete") : null),
      el("div", { class: "meta-line" }, `${fmtDate(t.created)} · ${fmtDur(t.duration)}${ready ? ` · ${plural(words, "word")}` : ""}`),
      statusEl(t),
      ready || t.turns.length ? el("div", { id: "speakers" }, speakersEl(t)) : null,
      ready ? toolbarEl(t) : null),
    el("div", { id: "turns-scroll", class: "scroll" }, el("div", { id: "turns" }, turnsEls(t))));
  $("#turns-scroll").scrollTop = top;
  renderPlayer(t);
}

function statusEl(t) {
  if (t.status === "ready" || t.status === "live") return null;
  if (t.status === "error") {
    return el("div", { class: "banner error" }, el("div", { style: "flex:1" }, el("b", {}, "Processing failed. "), t.error || ""),
      el("button", { onclick: async () => { try { await api("POST", `/api/transcripts/${t.id}/retry`); await reloadTranscript(); } catch (e) { fail(e); } } }, "Retry"));
  }
  const p = t.progress || {};
  return el("div", { class: "banner", id: "progress" },
    el("span", { id: "progress-stage" }, t.status === "queued" ? "Waiting for the previous job…" : p.stage || "Starting…"),
    el("div", { class: "bar" }, el("div", { id: "progress-fill", style: `width:${Math.round((p.fraction || 0) * 100)}%` })));
}

function speakersEl(t) {
  const labels = Object.keys(t.speakers || {});
  const total = labels.reduce((sum, k) => sum + (t.speakers[k].talk || 0), 0) || 1;
  const ready = t.status === "ready";
  return el("div", { class: "speakers" },
    labels.map((k) => {
      const s = t.speakers[k];
      return el("button", { class: "chip", style: `--c:${color(k)}`, disabled: !ready, onclick: (e) => speakerMenu(e.currentTarget, k) },
        el("span", { class: "dot" }), s.name,
        el("span", { class: "talk" }, `${Math.round((100 * (s.talk || 0)) / total)}%`),
        s.person && !s.manual ? el("span", { class: "badge", title: `Recognised from a saved voice (${Math.round((s.score || 0) * 100)}% match). Click to correct.` }, "auto") : null);
    }),
    ready && labels.some((k) => isDefaultName(t.speakers[k].name))
      ? el("span", { class: "hint" }, "Click a speaker to name them - their voice is remembered for next time.") : null);
}

function toolbarEl(t) {
  const count = el("span", { class: "muted small", id: "match-count" });
  const search = el("input", { type: "search", placeholder: "Search this transcript", value: state.query,
    oninput: (e) => { state.query = e.target.value; renderTurns(); } });
  const regroupSel = el("select", { title: "How many people talk in this call? Tadween regroups the voices.", onchange: (e) => regroup(e.target.value) },
    el("option", { value: "" }, "Auto"),
    Array.from({ length: 11 }, (_, i) => i + 2).map((k) => el("option", { value: k, selected: t.num_speakers === k }, String(k))));
  return el("div", { class: "toolbar" }, search, count,
    el("button", { class: "link", title: "Replace this word or phrase everywhere", onclick: (e) => state.query.trim() && openReplace(e.currentTarget, state.query.trim()) }, "Replace…"),
    el("span", { class: "spacer" }),
    el("label", {}, el("input", { type: "checkbox", checked: state.follow, onchange: (e) => { state.follow = e.target.checked; } }), "Follow audio"),
    el("label", {}, "People", regroupSel));
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
  if (counter) counter.textContent = q ? plural(matches, "match") : "";
  if (!t.turns.length) out.push(el("p", { class: "muted" }, t.status === "ready" ? "No speech was found in this recording." : "The transcript appears here when processing is done."));
  return out;
}

function turnEl(t, turn, prev) {
  const s = (t.speakers || {})[turn.speaker] || { name: turn.speaker || "…" };
  const cont = prev && prev.speaker === turn.speaker;
  const ready = t.status === "ready";
  return el("div", { class: `turn${cont ? " cont" : ""}`, "data-id": turn.id, style: `--c:${color(turn.speaker)}` },
    el("div", { class: "gutter" }, el("button", { class: "time", title: "Play from here", onclick: () => playFrom(turn.start) }, fmt(turn.start)),
      cont && ready ? el("button", { class: "mini", title: `${s.name} - change speaker`, onclick: (e) => turnSpeakerMenu(e.currentTarget, turn) }, "⇄") : null),
    el("div", {},
      el("button", { class: "who", title: ready ? "Change or name this speaker" : "", disabled: !ready, onclick: (e) => turnSpeakerMenu(e.currentTarget, turn) }, s.name),
      textEl(turn, ready)));
}

function textEl(turn, editable = true) {
  const p = el("p", { class: "text", onclick: editable ? (e) => maybeEdit(e, turn) : null });
  const q = state.query.trim();
  if (q) p.append(...highlight(turn.text, [q]));
  else if (!turn.edited && !turn.autofix && turn.words?.length) {
    turn.words.forEach((w, i) => {
      const txt = i === 0 ? w.w.trimStart() : w.w;
      p.append(w.p < 0.25 && w.w.replace(/[^a-z0-9]/gi, "").length >= 3
        ? el("span", { class: "unsure", title: `Whisper wasn't sure about this word (${Math.round(w.p * 100)}%). Click to fix.` }, txt)
        : txt);
    });
  } else if (turn.autofix?.length) p.append(...highlight(turn.text, turn.autofix, "span", "fixed"));
  else p.append(turn.text);
  return p;
}

// ---------- editing: fix a word once, fix it everywhere ----------

function maybeEdit(e, turn) {
  if (state.editing) return;
  const sel = getSelection();
  if (sel && !sel.isCollapsed && sel.toString().trim()) return; // a selection opens "Fix everywhere" instead
  const p = e.currentTarget;
  const { clientX: x, clientY: y } = e;
  state.editing = turn.id;
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
        const res = await api("PUT", `/api/transcripts/${state.t.id}/turns/${turn.id}`, { text });
        Object.assign(turn, res.turn);
        offerFixes(res.suggestions);
      }
    } catch (err) { fail(err); }
    p.replaceWith(textEl(turn));
    state.editing = null;
    if (state.stale) { state.stale = false; reloadTranscript(); }
  };
  p.addEventListener("keydown", onKey);
  p.addEventListener("blur", onBlur);
}

function offerFixes(suggestions) {
  for (const s of (suggestions || []).slice(0, 3)) {
    const what = `“${s.from}” → “${s.to}”`;
    if (s.count > 0) {
      toast(`${what}. It appears ${plural(s.count, "more time")} in this transcript.`, [
        [`Fix all ${s.count}`, () => replaceAll(s.from, s.to, false), true],
        ["Fix all + remember", () => replaceAll(s.from, s.to, true)],
        ["Review…", () => openReplace(null, s.from, s.to)],
      ], { timeout: 0 });
    } else {
      toast(`${what}. Fix it automatically in future transcripts too?`, [["Remember this fix", () => rememberFix(s.from, s.to), true]], { timeout: 12000 });
    }
  }
}

async function replaceAll(from, to, remember) {
  try {
    const r = await api("POST", `/api/transcripts/${state.t.id}/replace`, { from, to, remember });
    toast(`Replaced ${plural(r.count, "time")}${remember ? " - future transcripts will be fixed too" : ""}.`);
    await reloadTranscript();
  } catch (e) { fail(e); }
}

async function rememberFix(from, to) {
  try { await api("POST", "/api/vocabulary/corrections", { from, to }); toast(`Saved. “${from}” will become “${to}” in future transcripts.`); } catch (e) { fail(e); }
}

async function openReplace(anchor, from, to = "") {
  const input = el("input", { value: to, placeholder: "Correct spelling" });
  const remember = el("input", { type: "checkbox", checked: true });
  const count = el("div", { class: "muted small" }, "Counting…");
  const list = el("div", { class: "occ" });
  const go = el("button", { class: "primary", onclick: async () => {
    const v = input.value.trim();
    if (!v) return input.focus();
    closePopover();
    await replaceAll(from, v, remember.checked);
  } }, "Replace all");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
  const target = anchor || $(".header h1");
  popover(target, el("div", {},
    el("div", { class: "pop-title" }, "Fix everywhere"),
    el("div", {}, "Replace ", el("b", {}, `“${from}”`), " with"), input, count, list,
    el("label", { class: "check" }, remember, "Also fix it in future transcripts"),
    el("div", { class: "row" }, go)));
  try {
    const hits = await api("GET", `/api/transcripts/${state.t.id}/occurrences?q=${encodeURIComponent(from)}`);
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
    const text = sel?.toString().replace(/\s+/g, " ").trim();
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
  const t = state.t, s = t.speakers[label];
  const name = el("input", { value: isDefaultName(s.name) ? "" : s.name, placeholder: isDefaultName(s.name) ? "Their name" : s.name });
  const remember = el("input", { type: "checkbox", checked: true });
  const save = async () => {
    const v = name.value.trim();
    if (!v) return name.focus();
    closePopover();
    try {
      await api("POST", `/api/transcripts/${t.id}/speakers/${label}/rename`, { name: v, remember: remember.checked });
      toast(remember.checked && label !== "ME" ? `Saved. Tadween will recognise ${v}'s voice in future calls.` : `Renamed to ${v}.`);
      await reloadTranscript();
      loadList();
    } catch (e) { fail(e); }
  };
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  const others = Object.keys(t.speakers).filter((k) => k !== label);
  const into = el("select", {}, others.map((k) => el("option", { value: k }, t.speakers[k].name)));
  popover(anchor, el("div", {},
    el("div", { class: "pop-title" }, label === "ME" ? "Your name" : `Who is ${s.name}?`),
    el("button", { class: "link", style: "align-self:flex-start", onclick: () => playSample(label) }, "▶ Play a sample of their voice"),
    name,
    label !== "ME" ? el("label", { class: "check" }, remember, "Remember this voice for future calls") : null,
    el("div", { class: "row" }, el("button", { class: "primary", onclick: save }, "Save name")),
    others.length ? el("div", { class: "sep" }) : null,
    others.length ? el("div", { class: "row" }, el("span", { class: "muted" }, "Same person as"), into,
      el("button", { onclick: () => merge(label, into.value) }, "Merge")) : null));
}

function turnSpeakerMenu(anchor, turn) {
  const t = state.t;
  const move = async (label) => {
    closePopover();
    try { await api("PUT", `/api/transcripts/${t.id}/turns/${turn.id}`, { speaker: label }); await reloadTranscript(); } catch (e) { fail(e); }
  };
  popover(anchor, el("div", {},
    el("div", { class: "pop-title" }, "Who said this?"),
    Object.keys(t.speakers).map((k) => el("button", { class: "menu-item", style: `--c:${color(k)}`, disabled: k === turn.speaker, onclick: () => move(k) },
      el("span", { class: "dot" }), t.speakers[k].name, k === turn.speaker ? el("span", { class: "muted small" }, " (now)") : null)),
    el("button", { class: "menu-item", onclick: () => move("new") }, "+ Someone else (new speaker)"),
    el("div", { class: "sep" }),
    el("button", { class: "link", onclick: (e) => speakerMenu(anchor, turn.speaker) }, `Rename ${(t.speakers[turn.speaker] || {}).name || "this speaker"}…`)));
}

async function merge(src, dst) {
  const t = state.t;
  closePopover();
  try {
    await api("POST", `/api/transcripts/${t.id}/speakers/merge`, { from: src, into: dst });
    toast(`Merged into ${t.speakers[dst].name}.`);
    await reloadTranscript();
  } catch (e) { fail(e); }
}

async function regroup(n) {
  const t = state.t;
  if (t.turns.some((x) => x.edited) && !confirm("Regrouping voices rebuilds the lines. Word fixes are kept, but other hand edits will be lost. Continue?")) {
    renderTranscript(true);
    return;
  }
  const close = toast("Regrouping voices…", [], { timeout: 0 });
  try { await api("POST", `/api/transcripts/${t.id}/regroup`, { speakers: n ? Number(n) : null }); await reloadTranscript(); } catch (e) { fail(e); }
  close();
}

function playSample(label) {
  const turns = state.t.turns.filter((x) => x.speaker === label);
  const best = turns.find((x) => x.end - x.start >= 4) || turns.sort((a, b) => (b.end - b.start) - (a.end - a.start))[0];
  if (best) playFrom(best.start);
}

// ---------- audio ----------

function renderPlayer(t) {
  const bar = $("#player-bar");
  if (t.status !== "ready") { bar.hidden = true; return; }
  if (bar.dataset.id !== t.id) {
    bar.dataset.id = t.id;
    bar.replaceChildren(el("audio", { controls: true, preload: "metadata", src: `/api/transcripts/${t.id}/audio`, ontimeupdate: onTime }));
    state.playingId = null;
  }
  bar.hidden = false;
}

function playFrom(sec) {
  const audio = $("#player-bar audio");
  if (!audio) return;
  audio.currentTime = Math.max(0, sec - 0.3);
  audio.play();
}

function onTime(e) {
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
  if (state.follow && !e.target.paused && !state.editing) node.scrollIntoView({ block: "center", behavior: "smooth" });
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
  const stop = el("button", { class: "primary", style: "background:var(--rec)", onclick: async (e) => {
    e.currentTarget.disabled = true;
    e.currentTarget.textContent = "Saving…";
    try {
      const r = await api("POST", "/api/live/stop");
      state.live = { running: false };
      renderList();
      location.hash = r.id ? `#/t/${r.id}` : "#/";
    } catch (err) { fail(err); }
  } }, "■ Stop");
  view(
    el("div", { class: "header" },
      el("div", { class: "live-head" }, el("h1", {}, L.title), el("span", { class: "elapsed", id: "elapsed" }, fmt((Date.now() / 1000) - L.started)), stop),
      el("div", { class: "sources", id: "sources" }, sourcesEls()),
      el("div", { id: "speakers" }, liveSpeakersEl())),
    el("div", { id: "turns-scroll", class: "scroll" }, el("div", { id: "turns" }, liveLinesEls())));
  scrollLiveToEnd(true);
}

function sourcesEls() {
  const names = { mic: "Your mic", system: "Call audio" };
  return (state.live.sources || []).map((s) => el("span", { class: `src${s.error ? " error" : s.speaking ? " speaking" : s.ready ? " ready" : ""}`, title: s.error || "" },
    `${names[s.name] || s.name}${s.error ? ` - ${s.error}` : s.ready ? "" : " - starting…"}`));
}

function liveSpeakersEl() {
  const sp = state.live.speakers || {};
  return el("div", { class: "speakers" }, Object.keys(sp).map((k) =>
    el("button", { class: "chip", style: `--c:${color(k)}`, onclick: (e) => liveRename(e.currentTarget, k) }, el("span", { class: "dot" }), sp[k].name,
      sp[k].person && !sp[k].manual ? el("span", { class: "badge" }, "auto") : null)),
    Object.values(sp).some((s) => isDefaultName(s.name)) ? el("span", { class: "hint" }, "Click a speaker to name them as you go.") : null);
}

function liveLinesEls() {
  const sp = state.live.speakers || {};
  const lines = state.live.lines || [];
  if (!lines.length) return [el("p", { class: "muted" }, "Listening… lines appear a moment after each person finishes a sentence.")];
  let prev = null;
  return lines.map((line) => {
    const cont = prev && prev.speaker === line.speaker;
    prev = line;
    return el("div", { class: `turn${cont ? " cont" : ""}`, "data-index": line.index, style: `--c:${color(line.speaker)}` },
      el("div", { class: "gutter" }, el("span", { class: "time" }, fmt(line.start))),
      el("div", {}, el("button", { class: "who", onclick: (e) => liveRename(e.currentTarget, line.speaker) }, (sp[line.speaker] || {}).name || line.speaker),
        el("p", { class: "text" }, ...(line.autofix ? highlight(line.text, line.autofix, "span", "fixed") : [line.text]))));
  });
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
    el("div", { class: "row" }, el("button", { class: "primary", onclick: save }, "Save"))));
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
  const apps = el("input", { placeholder: "Optional, e.g. zoom.us or Chrome" });
  const go = el("button", { class: "primary big", disabled: !state.capture, onclick: async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "Starting (loading the speech model)…";
    try {
      state.live = await api("POST", "/api/live/start", { title: title.value.trim() || null, mic: mic.checked, system: sys.checked, apps: apps.value.trim() });
      renderList();
      renderLive();
    } catch (err) { fail(err); btn.disabled = false; btn.textContent = "● Start"; }
  } }, "● Start");
  view(el("div", { class: "page narrow" },
    el("h1", {}, "Live transcription"),
    el("p", { class: "muted" }, "Tadween listens to your microphone (that's you) and to this Mac's sound output (everyone else on the call), and writes down who said what while you talk."),
    state.capture ? null : el("div", { class: "warn" }, "The audio capture helper isn't built yet. In a terminal, run ", el("code", {}, "./setup.sh"), " in the Tadween folder, then reload this page."),
    field("Title", title),
    el("label", { class: "check" }, mic, "Your microphone - labelled as you"),
    el("label", { class: "check" }, sys, "Call audio - everyone else (Zoom, Meet, Teams, Slack…)"),
    field("Only capture sound from this app", apps, "Leave empty to capture all sound. Start Tadween after the meeting app is open."),
    go,
    el("div", { class: "note" },
      el("b", {}, "First time? "), "macOS will ask for Microphone and Screen & System Audio Recording permission for the app that runs Tadween (Terminal or VS Code). Allow both, then quit and reopen that app. ",
      "Headphones give the cleanest result. When you press Stop, Tadween transcribes the whole call again with full context and regroups the voices for the final version.")));
}

// ---------- library pages ----------

async function renderVocabulary() {
  let data;
  try { data = await api("GET", "/api/vocabulary"); } catch (e) { return fail(e); }
  const from = el("input", { placeholder: "Heard as (e.g. post gress)" });
  const to = el("input", { placeholder: "Should be (e.g. Postgres)" });
  const add = async () => {
    if (!from.value.trim() || !to.value.trim()) return;
    try { await api("POST", "/api/vocabulary/corrections", { from: from.value, to: to.value }); renderVocabulary(); } catch (e) { fail(e); }
  };
  const terms = el("textarea", { rows: 6, placeholder: "One per line - names, products, jargon" });
  terms.value = data.terms.join("\n");
  view(el("div", { class: "page narrow" },
    el("h1", {}, "Word fixes"),
    el("p", { class: "muted" }, "Fixes you choose to remember are applied to every new transcript, and the corrected words are given to Whisper so it spells them right in the first place."),
    el("table", { class: "list" },
      el("tr", {}, el("th", {}, "Heard as"), el("th", {}, "Becomes"), el("th", {})),
      data.corrections.length ? data.corrections.map((c) => el("tr", {}, el("td", {}, c.from), el("td", {}, el("b", {}, c.to)),
        el("td", {}, el("button", { class: "ghost danger", onclick: async () => { try { await api("DELETE", `/api/vocabulary/corrections?from=${encodeURIComponent(c.from)}`); renderVocabulary(); } catch (e) { fail(e); } } }, "Remove"))))
        : el("tr", {}, el("td", { colspan: 3, class: "muted" }, "No fixes yet. Edit a word in a transcript and choose “remember”."))),
    el("div", { class: "inline-form" }, from, to, el("button", { onclick: add }, "Add")),
    el("h2", {}, "Words to expect"),
    el("p", { class: "muted small" }, "Names, products and jargon that come up in your calls. Whisper is told to expect them."),
    terms,
    el("div", { class: "inline-form" }, el("button", { class: "primary", onclick: async () => {
      try { await api("PUT", "/api/vocabulary/terms", { terms: terms.value.split("\n") }); toast("Saved."); } catch (e) { fail(e); }
    } }, "Save words"))));
}

async function renderPeople() {
  let people;
  try { people = await api("GET", "/api/people"); } catch (e) { return fail(e); }
  view(el("div", { class: "page narrow" },
    el("h1", {}, "Known voices"),
    el("p", { class: "muted" }, "When you name a speaker with “remember this voice”, Tadween stores a voiceprint (numbers describing the voice, not audio) and labels that person automatically in later calls."),
    el("table", { class: "list" },
      el("tr", {}, el("th", {}, "Name"), el("th", {}, "Samples"), el("th", {}, "Last updated"), el("th", {})),
      people.length ? people.map((p) => el("tr", {},
        el("td", {}, el("b", {}, p.name)), el("td", {}, String(p.samples)), el("td", { class: "muted" }, new Date(p.updated * 1000).toLocaleDateString()),
        el("td", {},
          el("button", { class: "ghost", onclick: async () => {
            const name = prompt("New name", p.name);
            if (name && name.trim()) { try { await api("PATCH", `/api/people/${p.id}`, { name: name.trim() }); renderPeople(); } catch (e) { fail(e); } }
          } }, "Rename"),
          el("button", { class: "ghost danger", onclick: async () => {
            if (!confirm(`Forget ${p.name}'s voice?`)) return;
            try { await api("DELETE", `/api/people/${p.id}`); renderPeople(); } catch (e) { fail(e); }
          } }, "Forget"))))
        : el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No voices yet. Open a transcript and click a speaker to name them.")))));
}

async function renderSettings() {
  let s;
  try { s = await api("GET", "/api/settings"); } catch (e) { return fail(e); }
  const name = el("input", { value: s.my_name });
  const lang = el("select", {}, [["en", "English"], ["auto", "Detect automatically"], ["ar", "Arabic"], ["hi", "Hindi"], ["ur", "Urdu"], ["es", "Spanish"], ["fr", "French"], ["de", "German"]]
    .map(([v, l]) => el("option", { value: v, selected: s.language === v }, l)));
  const threads = el("input", { type: "number", min: 1, max: 16, value: s.threads });
  const sep = el("input", { type: "range", min: 0.5, max: 1.0, step: 0.05, value: s.speaker_threshold });
  const match = el("input", { type: "range", min: 0.3, max: 0.8, step: 0.05, value: s.voice_match_threshold });
  const prompt = el("input", { value: s.initial_prompt });
  view(el("div", { class: "page narrow" },
    el("h1", {}, "Settings"),
    field("Your name", name, "Used for your microphone in live calls."),
    field("Language spoken in calls", lang),
    field("Voice grouping", sep, "Left: split voices more readily (more speakers). Right: merge similar voices (fewer speakers). Applies to new transcripts; use “People” in a transcript to regroup it."),
    field("Recognising saved voices", match, "Left: name people more eagerly. Right: only when very sure."),
    field("Whisper style prompt", prompt, "A punctuated sentence Whisper imitates. Keep it short."),
    field("CPU threads for transcription", threads),
    el("button", { class: "primary", onclick: async () => {
      try {
        state.settings = await api("PUT", "/api/settings", { my_name: name.value.trim() || "Me", language: lang.value, threads: Number(threads.value) || 6,
          speaker_threshold: Number(sep.value), voice_match_threshold: Number(match.value), initial_prompt: prompt.value.trim() });
        toast("Settings saved.");
      } catch (e) { fail(e); }
    } }, "Save settings")));
}

// ---------- live updates from the server ----------

let reloadTimer = null;
const handlers = {
  transcripts: () => loadList(),
  progress: (m) => {
    const item = state.list.find((x) => x.id === m.id);
    if (item) { item.status = "processing"; item.progress = m.progress; renderList(); }
    if (state.t?.id === m.id) {
      if (!$("#progress")) return reloadTranscript();
      $("#progress-stage").textContent = m.progress.stage;
      $("#progress-fill").style.width = `${Math.round((m.progress.fraction || 0) * 100)}%`;
    }
  },
  transcript: (m) => {
    if (state.t?.id !== m.id) return;
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(reloadTranscript, 250);
  },
  live_started: async () => { state.live = await api("GET", "/api/live"); renderList(); if (location.hash === "#/live") renderLive(); },
  live_line: (m) => {
    if (!state.live.running || state.live.id !== m.id) return;
    state.live.lines.push(m.line);
    state.live.speakers = m.speakers;
    refreshLive();
  },
  live_remove: (m) => { if (state.live.running) { state.live.lines = state.live.lines.filter((x) => x.index !== m.index); refreshLive(); } },
  live_speakers: (m) => { if (state.live.running) { state.live.speakers = m.speakers; refreshLive(); } },
  live_activity: (m) => { const s = (state.live.sources || []).find((x) => x.name === m.source); if (s) { s.speaking = m.speaking; s.ready = true; $("#sources")?.replaceChildren(...sourcesEls()); } },
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
  const es = new EventSource("/api/events");
  let wasDown = false;
  es.onmessage = (e) => { const m = JSON.parse(e.data); handlers[m.type]?.(m); };
  es.onerror = () => { wasDown = true; };
  es.onopen = async () => {
    if (!wasDown) return;
    wasDown = false;
    await loadList().catch(() => {});
    if (state.t) reloadTranscript().catch(() => {});
  };
}

setInterval(() => {
  const node = $("#elapsed");
  if (node && state.live.running) node.textContent = fmt(Date.now() / 1000 - state.live.started);
}, 1000);

(async function init() {
  try {
    const s = await api("GET", "/api/state");
    state.settings = s.settings;
    state.live = s.live;
    state.capture = s.capture_helper;
    connectEvents();
    await loadList();
    addEventListener("hashchange", route);
    route();
  } catch (e) { fail(e); }
})();
