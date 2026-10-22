"use strict";

// The depth page of the GIS review site: one full run of the water-depth
// benchmark (experiments/flood_depth/run_all.py). The run's fixed parts come
// once from /api/depth/meta; videos come ONE PAGE at a time from /api/depth,
// which filters, sorts and paginates on the server (depth_review.query) --
// the whole run is ~5 MB, a page ~40 KB.
//
// One card per video; inside it, every flood shot with its key frame and the
// answer of each condition C1-C5. Clicking a shot opens the viewer: the
// player, all three frames, and the exact prompt each condition was given --
// the uploader text (C3), the height table (C4, C5) and the detector boxes
// drawn as C5 saw them (conditions.draw_boxes: numbered from 1, in the cached
// order, colours from BOX_COLOURS). Read-only: no decisions.

const CONDS = ["C1", "C2", "C3", "C4", "C5"];
const COND_NAMES = {
  C1: "one frame", C2: "three frames", C3: "+ uploader text", C4: "+ height table", C5: "+ detector boxes",
};
const KEY = 1;

const state = {
  meta: null,
  items: [],        // the current page's videos
  total: 0,
  page: 1,
  pages: 1,
  request: 0,       // drops answers to requests that a newer one replaced
  viewerIndex: -1,  // index into state.items
  shot: 0,          // index into the open video's shots
  cond: "C5",       // condition whose prompt is shown
};

const $ = (selector) => document.querySelector(selector);
const ui = {
  summary: $("#summary"),
  promptWarning: $("#promptWarning"),
  condition: $("#condition"),
  filter: $("#filter"),
  location: $("#location"),
  sortOrder: $("#sortOrder"),
  search: $("#search"),
  showBoxes: $("#showBoxes"),
  resultCount: $("#resultCount"),
  pageStatus: $("#pageStatus"),
  paginationStatus: $("#paginationStatus"),
  previousPage: $("#previousPage"),
  nextPage: $("#nextPage"),
  pageJump: $("#pageJump"),
  grid: $("#videoGrid"),
  empty: $("#emptyState"),
  viewer: $("#viewer"),
  closeViewer: $("#closeViewer"),
  player: $("#player"),
  shotCount: $("#shotCount"),
  shotList: $("#shotList"),
  viewerTitle: $("#viewerTitle"),
  viewerSourceLink: $("#viewerSourceLink"),
  labelTable: $("#labelTable"),
  promptTitle: $("#promptTitle"),
  promptTabs: $("#promptTabs"),
  promptBody: $("#promptBody"),
  viewerPosition: $("#viewerPosition"),
  previousVideo: $("#previousVideo"),
  nextVideo: $("#nextVideo"),
};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

/** 83.4 -> "1:23.4" */
function clock(seconds) {
  const m = Math.floor(seconds / 60);
  return `${m}:${(seconds - 60 * m).toFixed(1).padStart(4, "0")}`;
}

function frameUrl(item, shot, j) {
  // scan_shots.frame_path(): shot numbers from 1, three digits; frames from 0.
  return `/frames/${item.video_id}/shot${String(shot.s).padStart(3, "0")}_${j}.jpg`;
}

/** "35-40 cm" -> 37.5; "35 cm" -> 35; X / null -> null (levels.midpoint_cm). */
function midpoint(answer) {
  const match = /^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))? cm$/.exec(answer || "");
  if (!match) return null;
  return match[2] === undefined ? Number(match[1]) : (Number(match[1]) + Number(match[2])) / 2;
}

function answerOf(shot, cond) {
  return cond === "C1" ? shot.C1[KEY] : shot[cond];
}

function answerClass(answer) {
  if (answer === "X") return "depth-x";
  if (answer === null || answer === undefined) return "depth-null";
  const cm = midpoint(answer);
  if (cm === null) return "depth-null";
  if (cm < 20) return "depth-shallow";
  if (cm < 60) return "depth-mid";
  if (cm < 110) return "depth-deep";
  return "depth-vdeep";
}

function chip(text, cond = "", tag = "span") {
  const element = node(tag, `depth-chip ${answerClass(text)}`);
  if (cond) element.append(node("b", "", cond));
  element.append(document.createTextNode(text ?? "no answer"));
  return element;
}

const LOCATION_BADGE = {
  nyc: ["NYC", "nyc"], ny: ["NY STATE", "ny"], outside: ["OUTSIDE NY", "warning"], unknown: ["LOCATION UNKNOWN", "unknown"],
};

// -- frames with boxes ------------------------------------------------------

/** A frame, optionally with the C5 detector boxes drawn over it. Boxes are
 *  0-1 fractions of the frame, so they line up at any display size. */
function frameView(item, shot, j, { boxes = false } = {}) {
  const wrap = node("span", "depth-frame");
  const image = node("img");
  image.alt = `Shot ${shot.s}, frame ${j + 1} of 3`;
  image.loading = "lazy";
  image.decoding = "async";
  image.onerror = () => image.replaceWith(node("span", "no-frame", "no frame"));
  // Size the frame to the picture's own shape (Shorts are portrait): as wide
  // as fits, at most --frame-h tall, so the image fills it exactly and the
  // box fractions land on the right pixels.
  image.onload = () => {
    const ratio = image.naturalWidth / image.naturalHeight;
    wrap.style.aspectRatio = String(ratio);
    wrap.style.maxWidth = `calc(var(--frame-h) * ${ratio})`;
  };
  image.src = frameUrl(item, shot, j);
  wrap.append(image);
  if (boxes && shot.b && shot.b.length) {
    const colours = state.meta.box_colours;
    shot.b.forEach(([label, score, x0, y0, x1, y1], n) => {
      const box = node("span", "det-box");
      box.style.left = `${100 * x0}%`;
      box.style.top = `${100 * y0}%`;
      box.style.width = `${100 * (x1 - x0)}%`;
      box.style.height = `${100 * (y1 - y0)}%`;
      box.style.borderColor = colours[n % colours.length];
      box.title = `[${n + 1}] ${label} (score ${score})`;
      const tag = node("span", "det-tag", String(n + 1));
      tag.style.background = colours[n % colours.length];
      box.append(tag);
      wrap.append(box);
    });
  }
  return wrap;
}

// -- cards: one per video, its shots inside ---------------------------------

function labelText(label) {
  if (!label.deepest) return label.x ? "X" : null;
  return label.median_cm == null ? label.deepest : `${label.deepest} · med ${label.median_cm}`;
}

/** One shot inside a video card: key frame (boxes optional) and the five answers. */
function shotTile(item, shot, index, videoIndex) {
  const tile = node("button", "depth-tile");
  tile.type = "button";
  tile.title = "Open this shot: player, all frames and each condition's prompt";
  tile.addEventListener("click", () => openViewer(videoIndex, index));
  const picture = node("span", "depth-tile-frame");
  picture.append(frameView(item, shot, KEY, { boxes: ui.showBoxes.checked }));
  tile.append(picture);
  tile.append(node("span", "depth-tile-head",
    `Shot ${shot.s}${shot.t ? ` · ${clock(shot.t[0])}–${clock(shot.t[1])}` : ""}`
    + (shot.b && shot.b.length ? ` · ${shot.b.length} box${shot.b.length === 1 ? "" : "es"}` : "")));
  const rows = node("span", "depth-answers");
  const current = ui.condition.value;
  for (const c of CONDS) {
    const row = node("span", `depth-answer-row ${c === current ? "is-current" : ""}`.trim());
    row.append(node("b", "", c));
    row.append(chip(answerOf(shot, c)));
    if (c === "C1") {
      // the other two frames, asked separately: shows how much one frame moves the answer
      row.append(node("span", "depth-c1-others", `${shot.C1[0] ?? "?"} / ${shot.C1[2] ?? "?"}`));
    }
    rows.append(row);
  }
  tile.append(rows);
  return tile;
}

function renderCard(item, videoIndex) {
  const card = node("article", "depth-card");
  const head = node("div", "depth-card-head");
  const heading = node("h2", "card-title");
  const link = node("a", "card-title-link", item.title);
  link.href = item.source_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  heading.append(link);
  const badges = node("div", "badges");
  badges.append(node("span", "badge", `${item.shots.length} FLOOD SHOT${item.shots.length === 1 ? "" : "S"}`));
  const [loc, cls] = LOCATION_BADGE[item.location] || LOCATION_BADGE.unknown;
  badges.append(node("span", `badge ${cls}`, loc));
  badges.append(node("span", `badge ${item.spread_cm > 30 ? "warning" : ""}`.trim(),
    `CONDITIONS SPREAD ${Math.round(item.spread_cm)} CM`));
  if (item.published) badges.append(node("span", "badge", `UPLOADED ${item.published.slice(0, 10)}`));
  head.append(heading, badges);
  card.append(head);

  // the video's label per condition: its deepest shot (and the median)
  const labels = node("div", "depth-chips depth-video-labels");
  labels.append(node("span", "depth-labels-title", "Video label (deepest shot):"));
  for (const c of CONDS) {
    const element = chip(labelText(item.label[c]), c);
    if (c === ui.condition.value) element.classList.add("is-current");
    labels.append(element);
  }
  card.append(labels);

  const shots = node("div", "depth-tiles");
  item.shots.forEach((shot, index) => shots.append(shotTile(item, shot, index, videoIndex)));
  card.append(shots);
  return card;
}

// -- loading a page ---------------------------------------------------------

function params(page) {
  return new URLSearchParams({
    cond: ui.condition.value, filter: ui.filter.value, location: ui.location.value,
    sort: ui.sortOrder.value, q: ui.search.value.trim(), page: String(page),
  });
}

async function loadPage(page) {
  const request = ++state.request;
  ui.resultCount.textContent = "Loading…";
  const response = await fetch(`/api/depth?${params(page)}`);
  if (!response.ok) throw new Error(`Depth request failed: ${response.status}`);
  const body = await response.json();
  if (request !== state.request) return false;       // a newer request is on its way
  Object.assign(state, { items: body.items, total: body.total, page: body.page, pages: body.pages });
  renderGrid();
  return true;
}

function reload(page = 1) {
  loadPage(page).catch(showError);
}

function renderGrid() {
  ui.grid.replaceChildren(...state.items.map((item, index) => renderCard(item, index)));
  ui.empty.hidden = state.items.length > 0;
  const size = state.meta.page_size;
  const first = state.items.length ? (state.page - 1) * size + 1 : 0;
  const last = (state.page - 1) * size + state.items.length;
  ui.resultCount.textContent = `${state.total.toLocaleString()} matching videos · showing ${first}–${last}`;
  ui.pageStatus.textContent = `Page ${state.page} of ${state.pages}`;
  ui.paginationStatus.textContent = `of ${state.pages}`;
  ui.pageJump.value = String(state.page);
  ui.pageJump.max = String(state.pages);
  ui.previousPage.disabled = state.page <= 1;
  ui.nextPage.disabled = state.page >= state.pages;
}

function renderSummary() {
  const m = state.meta;
  ui.summary.textContent =
    `${m.videos.toLocaleString()} videos, ${m.shots.toLocaleString()} flood shots. `
    + `Model ${m.run.model}, run started ${String(m.run.created || "").slice(0, 16).replace("T", " ")} UTC; `
    + `detector ${m.run.detector}. Answers: ${m.run.answers === "cm" ? "centimetre intervals" : "levels L0–L5"}. `
    + `Run folder: ${m.run_dir}`;
  if (m.prompt_changed.length) {
    ui.promptWarning.hidden = false;
    ui.promptWarning.textContent = `conditions.py has changed since this run for ${m.prompt_changed.join(", ")}: `
      + "the prompt text shown for those conditions is the CURRENT text, not what this run asked.";
  }
}

function showError(error) {
  ui.resultCount.textContent = "";
  ui.grid.replaceChildren();
  ui.empty.hidden = false;
  ui.empty.querySelector("h2").textContent = "The depth run could not be loaded";
  ui.empty.querySelector("p").textContent = error.message;
}

// -- viewer -----------------------------------------------------------------

function openViewer(index, shot = null) {
  state.viewerIndex = index;
  renderViewer(shot);
  if (!ui.viewer.open) ui.viewer.showModal();
}

function playShot(index) {
  const item = state.items[state.viewerIndex];
  const shot = item && item.shots[index];
  if (!shot || !shot.t) return;
  // The embed takes whole seconds; floor/ceil so the shot is never cut short.
  ui.player.src = `${item.embed_url}?start=${Math.floor(shot.t[0])}&end=${Math.ceil(shot.t[1])}`
    + "&autoplay=1&rel=0&modestbranding=1";
}

function selectShot(index, cond = state.cond, play = false) {
  state.shot = index;
  state.cond = cond;
  for (const el of ui.shotList.querySelectorAll(".is-active")) el.classList.remove("is-active");
  const card = ui.shotList.querySelector(`[data-index="${index}"]`);
  if (card) {
    card.classList.add("is-active");
    card.scrollIntoView({ block: "nearest" });
  }
  for (const el of ui.shotList.querySelectorAll(".depth-chip.is-current")) el.classList.remove("is-current");
  const current = ui.shotList.querySelector(`[data-index="${index}"] [data-cond="${cond}"]`);
  if (current) current.classList.add("is-current");
  if (play) playShot(index);
  renderPrompt();
}

function shotCard(item, shot, index) {
  const card = node("article", "shot-card depth-shot");
  card.dataset.index = index;

  const head = node("div", "shot-head");
  const play = node("button", "secondary depth-play", "▶ Play");
  play.type = "button";
  play.addEventListener("click", () => selectShot(index, state.cond, true));
  head.append(node("b", "", `Shot ${shot.s}`),
    node("span", "", shot.t ? `${clock(shot.t[0])}–${clock(shot.t[1])} (${(shot.t[1] - shot.t[0]).toFixed(1)} s)` : ""),
    node("span", "muted", shot.b && shot.b.length ? `${shot.b.length} detected object${shot.b.length === 1 ? "" : "s"}` : "no detections"),
    play);
  card.append(head);

  // The three frames, each with its own C1 answer; the key frame (C1/C3-C5's
  // input) is marked and carries the boxes.
  const frames = node("div", "shot-frames frames-3");
  [0, 1, 2].forEach((j) => {
    const cell = node("span", `shot-frame ${j === KEY ? "frame-key" : ""}`.trim());
    cell.append(frameView(item, shot, j, { boxes: j === KEY && ui.showBoxes.checked }));
    cell.append(node("span", "frame-answer", `${j === KEY ? "key · " : ""}C1 ${shot.C1[j] ?? "?"}`));
    frames.append(cell);
  });
  card.append(frames);

  const answers = node("div", "depth-chips");
  for (const c of CONDS) {
    const element = chip(answerOf(shot, c), c, "button");
    element.type = "button";
    element.dataset.cond = c;
    element.title = `Show the ${c} prompt for this shot`;
    element.addEventListener("click", () => selectShot(index, c));
    answers.append(element);
  }
  card.append(answers);
  card.addEventListener("click", (event) => {
    if (!event.target.closest("button")) selectShot(index);
  });
  return card;
}

function renderLabelTable(item) {
  const head = node("tr");
  for (const h of ["", "deepest", "shot", "median", "answered", "X"]) head.append(node("th", "", h));
  const rows = CONDS.map((c) => {
    const l = item.label[c];
    const row = node("tr");
    row.append(node("th", "", `${c} · ${COND_NAMES[c]}`));
    const deepest = node("td");
    deepest.append(chip(l.deepest || (l.x ? "X" : null)));
    row.append(deepest,
      node("td", "", l.deepest_shot == null ? "–" : String(l.deepest_shot)),
      node("td", "", l.median_cm == null ? (l.median_level || "–") : `${l.median_cm} cm`),
      node("td", "", String(l.answered)), node("td", "", String(l.x)));
    return row;
  });
  ui.labelTable.replaceChildren(head, ...rows);
}

/** The selected shot's prompt for the selected condition, part by part, in
 *  the order the model received it (depth_review.prompt_layouts). */
function renderPrompt() {
  const item = state.items[state.viewerIndex];
  const shot = item && item.shots[state.shot];
  if (!shot) return;
  const cond = state.cond;
  ui.promptTitle.textContent = `shot ${shot.s}, ${cond} (${COND_NAMES[cond]})`;
  ui.promptTabs.replaceChildren(...CONDS.map((c) => {
    const tab = node("button", "prompt-tab", c);
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(c === cond));
    tab.addEventListener("click", () => selectShot(state.shot, c));
    return tab;
  }));

  const parts = state.meta.layouts[cond].map((part) => {
    if (part.text !== undefined) return node("pre", "prompt-text", part.text);
    if (part.slot === "uploader_text") return node("pre", "prompt-text prompt-video", item.uploader_text);
    if (part.slot === "boxes") {
      const text = shot.b && shot.b.length
        ? state.meta.boxes_intro.replace("{listing}",
          shot.b.map(([label], n) => `[${n + 1}] ${label}`).join("\n"))
        : state.meta.no_boxes;
      return node("pre", "prompt-text prompt-video", text);
    }
    const js = Array.isArray(part.image) ? part.image : [part.image];
    const images = node("div", `prompt-images prompt-images-${js.length}`);
    for (const j of js) images.append(frameView(item, shot, j, { boxes: Boolean(part.boxes) }));
    images.append(node("span", "prompt-note",
      `image${js.length > 1 ? "s" : ""}: frame${js.length > 1 ? "s" : ""} ${js.map((j) => j + 1).join(", ")} of 3`
      + `, upscaled to ${state.meta.image_long_side} px${part.boxes ? ", boxes drawn" : ""}`));
    return images;
  });
  const answer = node("div", "prompt-answer");
  answer.append(node("span", "", "Model answered"), chip(answerOf(shot, cond)));
  if (cond === "C1") {
    answer.append(node("span", "muted",
      `(key frame; the other frames, asked separately: ${shot.C1[0] ?? "?"}, ${shot.C1[2] ?? "?"})`));
  }
  ui.promptBody.replaceChildren(
    node("p", "prompt-legend", "Blue-edged text is the same for every video; amber-edged text is this video's or shot's own."),
    ...parts, answer);
}

function renderViewer(shotIndex = null) {
  const item = state.items[state.viewerIndex];
  if (!item) return;
  // YouTube's player refuses to start without a referrer (error 153).
  ui.player.referrerPolicy = "strict-origin-when-cross-origin";
  ui.player.src = `${item.embed_url}?rel=0&modestbranding=1`;
  ui.viewerTitle.textContent = item.title;
  ui.viewerSourceLink.href = item.source_url;
  renderLabelTable(item);
  ui.shotList.replaceChildren(...item.shots.map((shot, index) => shotCard(item, shot, index)));
  ui.shotCount.textContent = `${item.shots.length} flood shot${item.shots.length === 1 ? "" : "s"} · `
    + "click a condition's answer to see its prompt";
  const size = state.meta.page_size;
  const position = (state.page - 1) * size + state.viewerIndex + 1;
  ui.viewerPosition.textContent = `${position} / ${state.total}`;
  ui.previousVideo.disabled = position <= 1;
  ui.nextVideo.disabled = position >= state.total;
  ui.shotList.parentElement.scrollTop = 0;
  if (shotIndex === null) {
    const deepest = item.label[ui.condition.value].deepest_shot;
    shotIndex = Math.max(0, item.shots.findIndex((s) => s.s === deepest));
  }
  selectShot(shotIndex, ui.condition.value);
}

/** Next / previous video, crossing into the neighbouring page when needed. */
async function moveViewer(delta) {
  const next = state.viewerIndex + delta;
  if (next >= 0 && next < state.items.length) {
    openViewer(next);
    return;
  }
  const page = state.page + delta;
  if (page < 1 || page > state.pages) return;
  if (await loadPage(page)) openViewer(delta > 0 ? 0 : state.items.length - 1);
}

// -- wiring -----------------------------------------------------------------

for (const control of [ui.condition, ui.filter, ui.location, ui.sortOrder]) {
  control.addEventListener("change", () => reload(1));
}
ui.showBoxes.addEventListener("change", () => {
  renderGrid();
  if (ui.viewer.open) renderViewer(state.shot);
});
let searchTimer = null;
ui.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => reload(1), 300);
});
function goTo(page) {
  reload(page);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
ui.previousPage.addEventListener("click", () => goTo(state.page - 1));
ui.nextPage.addEventListener("click", () => goTo(state.page + 1));
ui.pageJump.addEventListener("change", () => {
  const page = Math.min(state.pages, Math.max(1, Number(ui.pageJump.value) || 1));
  goTo(page);
});
ui.closeViewer.addEventListener("click", () => ui.viewer.close());
ui.viewer.addEventListener("close", () => { ui.player.src = "about:blank"; });   // stops playback
ui.previousVideo.addEventListener("click", () => moveViewer(-1));
ui.nextVideo.addEventListener("click", () => moveViewer(1));
ui.viewer.addEventListener("click", (event) => {
  if (event.target === ui.viewer) ui.viewer.close();
});
document.addEventListener("keydown", (event) => {
  if (!ui.viewer.open || event.target instanceof HTMLSelectElement) return;
  if (event.key === "ArrowLeft") moveViewer(-1);
  if (event.key === "ArrowRight") moveViewer(1);
  const item = state.items[state.viewerIndex];
  if (!item) return;
  if (event.key === "ArrowDown" && state.shot < item.shots.length - 1) selectShot(state.shot + 1);
  if (event.key === "ArrowUp" && state.shot > 0) selectShot(state.shot - 1);
  const c = CONDS[["1", "2", "3", "4", "5"].indexOf(event.key)];
  if (c) selectShot(state.shot, c);
});

fetch("/api/depth/meta")
  .then((response) => {
    if (!response.ok) throw new Error(`Depth request failed: ${response.status}`);
    return response.json();
  })
  .then((meta) => {
    if (!meta.available) {
      throw new Error(`No video_labels.json in ${meta.run_dir}. `
        + "Run experiments/flood_depth/make_video_labels.py --dir <run folder>, then reload.");
    }
    state.meta = meta;
    renderSummary();
    return loadPage(1);
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the depth run";
    showError(error);
  });
