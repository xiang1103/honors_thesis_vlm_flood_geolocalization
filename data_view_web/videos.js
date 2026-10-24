"use strict";

// The Flood videos tab of the data_view site, over /api/videos: one card per
// valid flood video (at least one flood shot in the scan, not judged AI;
// data_view_server.is_valid_flood). The viewer lists the video's shots with
// the frames the model judged, plays any shot's range in YouTube's player,
// and shows each flood shot's water-depth answer under conditions C1-C5.

const PAGE_SIZE = 24;
const CONDS = ["C1", "C2", "C3", "C4", "C5"];
const SORT_COND = "C5";

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
  page: 1,
  viewerIndex: -1,
  activeShot: -1,
};

const $ = (selector) => document.querySelector(selector);
const ui = {
  summary: $("#summary"),
  location: $("#location"),
  depthFilter: $("#depthFilter"),
  sortOrder: $("#sortOrder"),
  search: $("#search"),
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
  viewerDescription: $("#viewerDescription"),
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

function frameUrl(item, shotIndex, frameIndex) {
  // scan_shots.frame_path(): shot numbers from 1, three digits; frames from 0.
  return `/frames/${item.video_id}/shot${String(shotIndex + 1).padStart(3, "0")}_${frameIndex}.jpg`;
}

// -- depth answers ----------------------------------------------------------

/** "35-40 cm" -> 37.5; "35 cm" -> 35; X / null -> null (levels.midpoint_cm). */
function midpoint(answer) {
  const match = /^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))? cm$/.exec(answer || "");
  if (!match) return null;
  return match[2] === undefined ? Number(match[1]) : (Number(match[1]) + Number(match[2])) / 2;
}

function depthClass(answer) {
  if (answer === "X") return "depth-x";
  const cm = midpoint(answer);
  if (cm === null) return "depth-null";
  if (cm < 20) return "depth-shallow";
  if (cm < 60) return "depth-mid";
  if (cm < 110) return "depth-deep";
  return "depth-vdeep";
}

/** C1-C5 answers as chips; X = the model saw no measurable water. */
function depthChips(answers) {
  const chips = node("div", "depth-chips");
  for (const c of CONDS) {
    const answer = answers[c];
    const chip = node("span", `depth-chip ${depthClass(answer)}`);
    chip.append(node("b", "", c), document.createTextNode(answer ?? "no answer"));
    chips.append(chip);
  }
  return chips;
}

function deepestCm(item) {
  return item.depth_label ? midpoint(item.depth_label[SORT_COND]) : null;
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

function locationBadge(location) {
  const [label, cls] = LOCATION_BADGE[location] || LOCATION_BADGE.unknown;
  const badges = node("div", "badges");
  badges.append(node("span", `badge ${cls}`, label));
  return badges;
}

/** The whole card opens the detail view; the title link and any button
 *  inside keep their own action (the cover button already opens it). */
function openOnCardClick(card, onOpen) {
  card.classList.add("card-clickable");
  card.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    onOpen();
  });
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
  openOnCardClick(card, () => openViewer(index));
  const body = node("div", "card-body");
  body.append(timeline(item));
  body.append(locationBadge(item.location));
  const heading = node("h2", "card-title");
  const link = node("a", "card-title-link", item.title);
  link.href = item.source_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  heading.append(link);
  body.append(heading);
  body.append(node("p", "card-meta",
    [item.published ? `uploaded ${item.published.slice(0, 10)}` : "", clock(item.duration_s),
      `${item.n_flood} flood shot${item.n_flood === 1 ? "" : "s"}`].filter(Boolean).join(" · ")));
  if (item.depth_label && item.depth_label[SORT_COND]) {
    body.append(node("p", "card-meta", `deepest water: ${item.depth_label[SORT_COND]}`));
  }
  card.append(body);
  return card;
}

// -- filtering --------------------------------------------------------------

function matches(item, words) {
  if (ui.location.value !== "all" && item.location !== ui.location.value) return false;
  const depth = ui.depthFilter.value;
  if (depth === "with" && !item.depth_label) return false;
  if (depth === "without" && item.depth_label) return false;
  if (words.length) {
    const haystack = item._search || (item._search = `${item.title}\n${item.video_id}`.toLowerCase());
    if (!words.every((word) => haystack.includes(word))) return false;
  }
  return true;
}

function filteredItems() {
  const words = ui.search.value.toLowerCase().split(/\s+/).filter(Boolean);
  const order = ui.sortOrder.value;
  return state.items.filter((item) => matches(item, words)).sort((a, b) => {
    if (order === "deep") return (deepestCm(b) ?? -1) - (deepestCm(a) ?? -1);
    if (order === "flood_s") return b.flood_s - a.flood_s;
    if (order === "shots") return b.n_flood - a.n_flood;
    return (b.published || "").localeCompare(a.published || "");
  });
}

// -- rendering --------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  ui.summary.textContent =
    `${s.videos.toLocaleString()} flood videos (of ${s.scanned.toLocaleString()} scanned): `
    + `${s.flood_shots.toLocaleString()} flood shots, ${s.flood_hours} hours of flood footage. `
    + `${s.with_depth.toLocaleString()} have water-depth estimates. `
    + `${s.nyc.toLocaleString()} in NYC, ${s.ny_not_nyc.toLocaleString()} elsewhere in New York State, `
    + `${s.outside_ny.toLocaleString()} outside New York, ${s.location_unknown.toLocaleString()} unknown.`;
}

function renderGrid() {
  state.filtered = filteredItems();
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
  if (!ui.viewer.open) ui.viewer.showModal();
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
  const card = node("article", `shot-card ${shot.flood ? "shot-flood" : "shot-dry"}`);
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
  card.append(head);
  if (shot.depth) card.append(depthChips(shot.depth));
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
  ui.viewerBadges.replaceChildren(...locationBadge(item.location).childNodes);
  ui.viewerTitle.textContent = item.title;
  const rows = [
    metadataRow("Uploaded", item.published ? item.published.slice(0, 10) : "unknown"),
    metadataRow("Duration", clock(item.duration_s)),
    metadataRow("Flood shots", `${item.n_flood} of ${item.n_shots}, ${item.flood_s.toFixed(1)} s `
      + `(${Math.round(100 * item.flood_share)}% of the video)`),
  ];
  if (item.depth_label) {
    rows.push(metadataRow("Deepest water", CONDS.map((c) => `${c} ${item.depth_label[c] ?? "–"}`).join(" · ")));
  } else {
    rows.push(metadataRow("Deepest water", "not estimated yet"));
  }
  rows.push(metadataRow("Video id", item.video_id));
  ui.viewerMetadata.replaceChildren(...rows);
  ui.viewerDescription.textContent = item.description || "";
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

// -- wiring -----------------------------------------------------------------

for (const control of [ui.location, ui.depthFilter, ui.sortOrder]) {
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
});

fetch("/api/videos")
  .then((response) => {
    if (!response.ok) throw new Error(`Video request failed: ${response.status}`);
    return response.json();
  })
  .then((catalog) => {
    if (!catalog.available) {
      throw new Error(`No flood videos yet in ${catalog.source_dir}. `
        + "Run flood_depth_experiments/scene_split/run_scan.sh, then reload.");
    }
    state.items = catalog.items;
    state.summary = catalog.summary;
    renderSummary();
    renderGrid();
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the flood videos";
    ui.resultCount.textContent = "";
    ui.empty.hidden = false;
    ui.empty.querySelector("h2").textContent = "The flood videos could not be loaded";
    ui.empty.querySelector("p").textContent = error.message;
  });
