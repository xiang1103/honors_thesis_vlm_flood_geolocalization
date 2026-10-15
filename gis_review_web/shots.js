"use strict";

// The shot page of the GIS review site: the per-shot flood scan
// (experiments/scene_split/scan_shots.py) over /api/shots. One card per
// scanned video with its shots on a timeline; the viewer lists every shot
// with the frames the model judged and its answers, and plays any shot's
// range in YouTube's player. Same look and conventions as videos.js.

const PAGE_SIZE = 24;

// Decisions are PER SHOT and live only in this browser. A shot's id is its
// video and time range, so a shot the union rule later splits differently is
// a different shot (its old decision is kept, just no longer shown).
const STORAGE_KEY = "vlm-flood-shot-reviews-v1";
const SHOT_CHOICES = [
  // [stored value, label, styles.css value that colours the pressed button]
  ["flood", "1 · Flood", "useful"],
  ["not_flood", "2 · Not flood", "reject"],
  ["unsure", "3 · Unsure", "unsure"],
];

const LOCATION_BADGE = {
  nyc: ["NYC", "nyc"],
  ny: ["NY STATE", "ny"],
  outside: ["OUTSIDE NY", "warning"],
  unknown: ["LOCATION UNKNOWN", "unknown"],
};

const state = {
  items: [],
  filtered: [],
  summary: null,
  reviews: loadReviews(),
  page: 1,
  viewerIndex: -1,
  activeShot: -1,
};

const $ = (selector) => document.querySelector(selector);
const ui = {
  summary: $("#summary"),
  floodFilter: $("#floodFilter"),
  location: $("#location"),
  reviewFilter: $("#reviewFilter"),
  sortOrder: $("#sortOrder"),
  search: $("#search"),
  exportReviews: $("#exportReviews"),
  resultCount: $("#resultCount"),
  pageStatus: $("#pageStatus"),
  paginationStatus: $("#paginationStatus"),
  previousPage: $("#previousPage"),
  nextPage: $("#nextPage"),
  grid: $("#videoGrid"),
  empty: $("#emptyState"),
  viewer: $("#viewer"),
  closeViewer: $("#closeViewer"),
  player: $("#player"),
  viewerTimeline: $("#viewerTimeline"),
  shotFilter: $("#shotFilter"),
  shotCount: $("#shotCount"),
  shotList: $("#shotList"),
  viewerBadges: $("#viewerBadges"),
  viewerTitle: $("#viewerTitle"),
  viewerMetadata: $("#viewerMetadata"),
  viewerSourceLink: $("#viewerSourceLink"),
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

function shotId(item, shot) {
  return `${item.video_id}@${shot.start}-${shot.end}`;
}

function frameUrl(item, shotIndex, frameIndex) {
  // scan_shots.frame_path(): shot numbers from 1, three digits; frames from 0.
  return `/frames/${item.video_id}/shot${String(shotIndex + 1).padStart(3, "0")}_${frameIndex}.jpg`;
}

// -- stored decisions -------------------------------------------------------

function loadReviews() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    return raw && raw.reviews && typeof raw.reviews === "object" ? raw.reviews : {};
  } catch {
    return {};
  }
}

function saveReviews() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ scheme: "video_id@start-end", reviews: state.reviews }));
  } catch {
    showNotice("Could not save decisions in this browser (storage blocked).", "warn");
  }
}

function showNotice(text, tone = "info") {
  document.querySelector("main").prepend(node("p", `storage-notice storage-notice-${tone}`, text));
}

function reviewedCount(item) {
  return item.shots.filter((shot) => state.reviews[shotId(item, shot)]).length;
}

function disagreements(item) {
  return item.shots.filter((shot) => {
    const decision = state.reviews[shotId(item, shot)];
    return (decision === "flood" && !shot.flood) || (decision === "not_flood" && shot.flood);
  }).length;
}

function setReview(item, shot, value) {
  const id = shotId(item, shot);
  if (state.reviews[id] === value) delete state.reviews[id];
  else state.reviews[id] = value;
  saveReviews();
  renderSummary();
  renderGrid({ refilter: false });
  if (ui.viewer.open) renderShots();
}

function shotButtons(item, shot) {
  const container = node("div", "review-buttons");
  for (const [value, label, cssValue] of SHOT_CHOICES) {
    const button = node("button", "", label.split(" · ")[1]);
    button.type = "button";
    button.dataset.value = cssValue;
    button.setAttribute("aria-pressed", String(state.reviews[shotId(item, shot)] === value));
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setReview(item, shot, value);
    });
    container.append(button);
  }
  return container;
}

// -- timeline ---------------------------------------------------------------

/** Flood / not-flood shots as coloured blocks; the gaps between them
 *  (transitions no model put in a shot) show the bar's own background. */
function timeline(item, large = false, onPick = null) {
  const bar = node("div", large ? "timeline timeline-large" : "timeline");
  const duration = item.duration_s || Math.max(1, ...item.shots.map((s) => s.end));
  item.shots.forEach((shot, index) => {
    const block = node(onPick ? "button" : "span", `tl-shot ${shot.flood ? "tl-flood" : "tl-dry"}`);
    block.style.left = `${(100 * shot.start) / duration}%`;
    block.style.width = `${Math.max(0.3, (100 * (shot.end - shot.start)) / duration)}%`;
    block.title = `#${index + 1} ${clock(shot.start)}–${clock(shot.end)} ${shot.flood ? "flood" : "not flood"}`;
    if (onPick) {
      block.type = "button";
      block.dataset.index = index;
      block.addEventListener("click", () => onPick(index));
    }
    bar.append(block);
  });
  return bar;
}

// -- cards ------------------------------------------------------------------

function addBadge(parent, text, className = "") {
  parent.append(node("span", `badge ${className}`.trim(), text));
}

function badgesFor(item) {
  const badges = node("div", "badges");
  addBadge(badges, `${item.n_flood}/${item.n_shots} FLOOD SHOTS`, item.n_flood ? "yes" : "no");
  addBadge(badges, `FLOOD ${Math.round(item.flood_s)} OF ${Math.round(item.duration_s)} S`,
    item.flood_share >= 0.5 ? "" : "warning");
  if (item.ai_video !== null && item.ai_video !== undefined) {
    addBadge(badges, `${item.ai_video ? "AI VIDEO" : "REAL"} · ${Math.round(100 * item.ai_share)}% AI SHOTS`,
      item.ai_video ? "no" : "");
  }
  const [label, cls] = LOCATION_BADGE[item.location] || LOCATION_BADGE.unknown;
  addBadge(badges, label, cls);
  const reviewed = reviewedCount(item);
  if (reviewed) addBadge(badges, `reviewed ${reviewed}/${item.n_shots}`, "review-useful");
  const disagree = disagreements(item);
  if (disagree) addBadge(badges, `${disagree} disagree`, "review-reject");
  return badges;
}

/** The card's picture: the middle frame of the first flood shot if the scan
 *  saved frames, else YouTube's cover. */
function coverSurface(item, onOpen) {
  const button = node("button", "image-button video-cover");
  button.type = "button";
  button.setAttribute("aria-label", `Open shots of: ${item.title}`);
  const image = node("img");
  image.alt = item.title;
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  const first = Math.max(0, item.shots.findIndex((s) => s.flood));
  const middle = item.shots[first] && item.shots[first].answers.length === 3 ? 1 : 0;
  const candidates = [item.has_frames && item.shots.length ? frameUrl(item, first, middle) : "", item.cover_url]
    .filter(Boolean);
  let attempt = 0;
  image.onerror = () => {
    attempt += 1;
    if (attempt < candidates.length) image.src = candidates[attempt];
  };
  image.src = candidates[0];
  button.append(image, node("span", "duration", clock(item.duration_s)), node("span", "play-mark", "▶"));
  button.addEventListener("click", onOpen);
  return button;
}

function renderCard(item, index) {
  const card = node("article", "image-card");
  card.append(coverSurface(item, () => openViewer(index)));
  const body = node("div", "card-body");
  body.append(timeline(item));
  body.append(badgesFor(item));
  const heading = node("h2", "card-title");
  const link = node("a", "card-title-link", item.title);
  link.href = item.source_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  heading.append(link);
  body.append(heading);
  body.append(node("p", "card-meta",
    [item.published ? `uploaded ${item.published.slice(0, 10)}` : "", `scanned ${item.scanned_at.slice(0, 10)}`,
      item.has_frames ? "" : "no saved frames"].filter(Boolean).join(" · ")));
  card.append(body);
  return card;
}

// -- filtering --------------------------------------------------------------

function matches(item, words) {
  const flood = ui.floodFilter.value;
  if (flood === "some" && !item.n_flood) return false;
  if (flood === "none" && item.n_flood) return false;
  if (flood === "mixed" && !(item.n_flood && item.n_flood < item.n_shots)) return false;
  if (flood === "ai" && item.ai_video !== true) return false;
  if (flood === "not_ai" && item.ai_video !== false) return false;
  if (ui.location.value !== "all" && item.location !== ui.location.value) return false;
  const review = ui.reviewFilter.value;
  if (review === "unreviewed" && reviewedCount(item)) return false;
  if (review === "reviewed" && !reviewedCount(item)) return false;
  if (review === "disagree" && !disagreements(item)) return false;
  if (words.length) {
    const haystack = item._search || (item._search =
      `${item.title}\n${(item.tags || []).join(" ")}\n${item.video_id}`.toLowerCase());
    if (!words.every((word) => haystack.includes(word))) return false;
  }
  return true;
}

function filteredItems() {
  const words = ui.search.value.toLowerCase().split(/\s+/).filter(Boolean);
  const order = ui.sortOrder.value;
  return state.items.filter((item) => matches(item, words)).sort((a, b) => {
    if (order === "flood_s") return b.flood_s - a.flood_s;
    if (order === "share_low") return a.flood_share - b.flood_share;
    if (order === "shots") return b.n_shots - a.n_shots;
    if (order === "newest") return (b.published || "").localeCompare(a.published || "");
    return b.scanned_at.localeCompare(a.scanned_at);
  });
}

// -- rendering --------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  const reviewed = Object.keys(state.reviews).length;
  ui.summary.textContent =
    `${s.videos.toLocaleString()} videos scanned so far: ${s.shots.toLocaleString()} shots, `
    + `${s.flood_shots.toLocaleString()} judged flood (at least 2 of 3 frames). `
    + `${s.videos_with_flood.toLocaleString()} videos have at least one flood shot; `
    + `${s.flood_hours} of ${s.hours} hours of footage is in flood shots. `
    + (s.ai_checked ? `${s.ai_videos.toLocaleString()} of ${s.ai_checked.toLocaleString()} AI-checked videos judged AI-generated. ` : "")
    + `${reviewed.toLocaleString()} shots reviewed in this browser.`;
}

function renderGrid({ refilter = true } = {}) {
  if (refilter) state.filtered = filteredItems();
  const pageCount = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pageCount);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = state.filtered.slice(start, start + PAGE_SIZE);
  ui.grid.replaceChildren(...pageItems.map((item, offset) => renderCard(item, start + offset)));
  ui.empty.hidden = pageItems.length > 0;
  const first = pageItems.length ? start + 1 : 0;
  const last = Math.min(start + PAGE_SIZE, state.filtered.length);
  ui.resultCount.textContent = `${state.filtered.length.toLocaleString()} matching videos · showing ${first}–${last}`;
  ui.pageStatus.textContent = `Page ${state.page} of ${pageCount}`;
  ui.paginationStatus.textContent = `${state.page} / ${pageCount}`;
  ui.previousPage.disabled = state.page <= 1;
  ui.nextPage.disabled = state.page >= pageCount;
}

function metadataRow(term, detail) {
  const fragment = document.createDocumentFragment();
  fragment.append(node("dt", "", term), node("dd", "", detail));
  return fragment;
}

function openViewer(index) {
  state.viewerIndex = index;
  renderViewer();
  ui.viewer.showModal();
}

function playShot(index) {
  const item = state.filtered[state.viewerIndex];
  const shot = item && item.shots[index];
  if (!shot) return;
  state.activeShot = index;
  // The embed takes whole seconds; floor/ceil so the shot is never cut short.
  ui.player.src = `${item.embed_url}?start=${Math.floor(shot.start)}&end=${Math.ceil(shot.end)}`
    + "&autoplay=1&rel=0&modestbranding=1";
  for (const el of ui.viewer.querySelectorAll(".is-active")) el.classList.remove("is-active");
  for (const el of ui.viewer.querySelectorAll(`[data-index="${index}"]`)) el.classList.add("is-active");
  const card = ui.shotList.querySelector(`.shot-card[data-index="${index}"]`);
  if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function shotCard(item, shot, index) {
  const decision = state.reviews[shotId(item, shot)];
  const card = node("article", `shot-card ${shot.flood ? "shot-flood" : "shot-dry"}`);
  if (decision) card.classList.add(`reviewed-${SHOT_CHOICES.find((c) => c[0] === decision)[2]}`);
  card.dataset.index = index;
  if (index === state.activeShot) card.classList.add("is-active");

  const frames = node("button", `shot-frames frames-${shot.answers.length}`);
  frames.type = "button";
  frames.setAttribute("aria-label", `Play shot ${index + 1}`);
  shot.answers.forEach((answer, j) => {
    const cell = node("span", `shot-frame ${answer === "yes" ? "frame-yes" : "frame-no"}`);
    if (item.has_frames) {
      const image = node("img");
      image.alt = `Shot ${index + 1}, frame at ${clock(shot.times[j] || 0)}`;
      image.loading = "lazy";
      image.src = frameUrl(item, index, j);
      image.onerror = () => image.replaceWith(node("span", "no-frame", "no frame"));
      cell.append(image);
    } else {
      cell.append(node("span", "no-frame", `${clock(shot.times[j] || 0)}`));
    }
    cell.append(node("span", "frame-answer", answer || "?"));
    frames.append(cell);
  });
  frames.addEventListener("click", () => playShot(index));
  card.append(frames);

  const head = node("div", "shot-head");
  const yes = shot.answers.filter((a) => a === "yes").length;
  head.append(node("b", "", `#${index + 1}`),
    node("span", "", `${clock(shot.start)}–${clock(shot.end)} (${(shot.end - shot.start).toFixed(1)} s)`),
    node("span", `badge ${shot.flood ? "yes" : "no"}`, `${shot.flood ? "FLOOD" : "NOT FLOOD"} ${yes}/${shot.answers.length}`));
  if (shot.ai === "yes") head.append(node("span", "badge no", "AI"));
  card.append(head, shotButtons(item, shot));
  return card;
}

function renderShots() {
  const item = state.filtered[state.viewerIndex];
  if (!item) return;
  const mode = ui.shotFilter.value;
  const cards = item.shots
    .map((shot, index) => [shot, index])
    .filter(([shot]) => mode === "all" || (mode === "flood") === shot.flood)
    .map(([shot, index]) => shotCard(item, shot, index));
  ui.shotList.replaceChildren(...cards);
  ui.shotCount.textContent = `${cards.length} of ${item.n_shots} shots`;
  ui.viewerBadges.replaceChildren(...badgesFor(item).childNodes);
}

function renderViewer() {
  const item = state.filtered[state.viewerIndex];
  if (!item) return;
  state.activeShot = -1;
  // YouTube's player refuses to start without a referrer (error 153).
  ui.player.referrerPolicy = "strict-origin-when-cross-origin";
  ui.player.src = `${item.embed_url}?rel=0&modestbranding=1`;
  ui.viewerTimeline.replaceWith(ui.viewerTimeline = Object.assign(timeline(item, true, playShot),
    { id: "viewerTimeline" }));
  ui.viewerTitle.textContent = item.title;
  ui.viewerMetadata.replaceChildren(
    metadataRow("Duration", clock(item.duration_s)),
    metadataRow("Shots", `${item.n_shots} (TransNetV2 ${item.raw_shot_counts.transnetv2 ?? "?"}, `
      + `AutoShot ${item.raw_shot_counts.autoshot ?? "?"}, union ${item.n_shots})`),
    metadataRow("Flood shots", `${item.n_flood}, ${item.flood_s.toFixed(1)} s (${Math.round(100 * item.flood_share)}% of the video)`),
    metadataRow("AI check", item.ai_video == null ? "not checked (scanned before 2026-10-15's AI check)"
      : `${item.ai_video ? "AI-generated" : "real"} -- ${Math.round(100 * item.ai_share)}% of shots judged AI`),
    metadataRow("Video", `${item.width}×${item.height} @ ${item.fps} fps`),
    metadataRow("Text score", item.flood_text_score == null ? "–" : Number(item.flood_text_score).toFixed(3)),
    metadataRow("Uploaded", item.published ? item.published.slice(0, 10) : "unknown"),
    metadataRow("Scanned", item.scanned_at.replace("T", " ").slice(0, 16)),
    metadataRow("Video id", item.video_id),
  );
  ui.viewerSourceLink.href = item.source_url;
  ui.viewerPosition.textContent = `${state.viewerIndex + 1} / ${state.filtered.length}`;
  ui.previousVideo.disabled = state.viewerIndex <= 0;
  ui.nextVideo.disabled = state.viewerIndex >= state.filtered.length - 1;
  renderShots();
  ui.shotList.parentElement.scrollTop = 0;
}

function moveViewer(delta) {
  const next = state.viewerIndex + delta;
  if (next < 0 || next >= state.filtered.length) return;
  state.viewerIndex = next;
  renderViewer();
}

function exportReviews() {
  const rows = [];
  for (const item of state.items) {
    item.shots.forEach((shot, index) => {
      const review = state.reviews[shotId(item, shot)];
      if (!review) return;
      rows.push({
        review, video_id: item.video_id, shot: index + 1, start: shot.start, end: shot.end,
        model_flood: shot.flood, model_answers: shot.answers, frame_times: shot.times,
        title: item.title, source_url: item.source_url,
      });
    });
  }
  const blob = new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "youtube_shot_review_decisions.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

// -- wiring -----------------------------------------------------------------

for (const control of [ui.floodFilter, ui.location, ui.reviewFilter, ui.sortOrder]) {
  control.addEventListener("change", () => {
    state.page = 1;
    renderGrid();
  });
}
let searchTimer = null;
ui.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.page = 1;
    renderGrid();
  }, 200);
});
ui.previousPage.addEventListener("click", () => {
  state.page -= 1;
  renderGrid();
  window.scrollTo({ top: 0, behavior: "smooth" });
});
ui.nextPage.addEventListener("click", () => {
  state.page += 1;
  renderGrid();
  window.scrollTo({ top: 0, behavior: "smooth" });
});
ui.shotFilter.addEventListener("change", renderShots);
ui.exportReviews.addEventListener("click", exportReviews);
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
  const choice = SHOT_CHOICES[["1", "2", "3"].indexOf(event.key)];
  const item = state.filtered[state.viewerIndex];
  if (choice && item && item.shots[state.activeShot]) setReview(item, item.shots[state.activeShot], choice[0]);
});

fetch("/api/shots")
  .then((response) => {
    if (!response.ok) throw new Error(`Scan request failed: ${response.status}`);
    return response.json();
  })
  .then((catalog) => {
    if (!catalog.available) {
      throw new Error(`No scanned videos yet in ${catalog.source_dir}. `
        + "Run experiments/scene_split/run_scan.sh, then reload.");
    }
    state.items = catalog.items;
    state.summary = catalog.summary;
    renderSummary();
    renderGrid();
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the shot scan";
    ui.resultCount.textContent = "";
    ui.empty.hidden = false;
    ui.empty.querySelector("h2").textContent = "The shot scan could not be loaded";
    ui.empty.querySelector("p").textContent = error.message;
  });
