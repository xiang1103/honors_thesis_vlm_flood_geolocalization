"use strict";

// The video page of the GIS review site. Same layout, decisions and keyboard
// as app.js (the photo page), over /api/videos. Thumbnails and the player load
// from YouTube in the browser; nothing is downloaded by the server.

const PAGE_SIZE = 24;
const TAGS_ON_CARD = 6;

// Decisions live ONLY in this browser. A separate key and id scheme from the
// photo page, so a photo decision can never be read as a video decision.
const STORAGE_KEY = "vlm-flood-video-reviews-v1";
const EXPECTED_SCHEME = "youtube_review_v1";

const REVIEW_CHOICES = [
  ["useful", "1 · Useful"],
  ["reject", "2 · Reject"],
  ["unsure", "3 · Unsure"],
];

const LOCATION_BADGE = {
  nyc: ["NYC", "nyc"],
  ny: ["NY STATE", "ny"],
  outside: ["OUTSIDE NY", "warning"],
  unknown: ["LOCATION UNKNOWN", "unknown"],
};

let storedScheme = null;

const state = {
  items: [],
  filtered: [],
  summary: null,
  reviews: loadReviews(),
  page: 1,
  viewerIndex: -1,
};

const ui = {
  summary: document.querySelector("#summary"),
  flood: document.querySelector("#flood"),
  location: document.querySelector("#location"),
  reviewFilter: document.querySelector("#reviewFilter"),
  sortOrder: document.querySelector("#sortOrder"),
  search: document.querySelector("#search"),
  exportReviews: document.querySelector("#exportReviews"),
  resultCount: document.querySelector("#resultCount"),
  pageStatus: document.querySelector("#pageStatus"),
  paginationStatus: document.querySelector("#paginationStatus"),
  previousPage: document.querySelector("#previousPage"),
  nextPage: document.querySelector("#nextPage"),
  grid: document.querySelector("#videoGrid"),
  empty: document.querySelector("#emptyState"),
  viewer: document.querySelector("#viewer"),
  closeViewer: document.querySelector("#closeViewer"),
  player: document.querySelector("#player"),
  viewerFrames: document.querySelector("#viewerFrames"),
  viewerPosition: document.querySelector("#viewerPosition"),
  viewerBadges: document.querySelector("#viewerBadges"),
  viewerTitle: document.querySelector("#viewerTitle"),
  viewerTags: document.querySelector("#viewerTags"),
  viewerReview: document.querySelector("#viewerReview"),
  viewerMetadata: document.querySelector("#viewerMetadata"),
  viewerDescription: document.querySelector("#viewerDescription"),
  viewerSourceLink: document.querySelector("#viewerSourceLink"),
  viewerMapLink: document.querySelector("#viewerMapLink"),
  viewerStreetViewLink: document.querySelector("#viewerStreetViewLink"),
  previousVideo: document.querySelector("#previousVideo"),
  nextVideo: document.querySelector("#nextVideo"),
};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

// -- stored decisions -------------------------------------------------------

function loadReviews() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!raw || typeof raw !== "object") return {};
    storedScheme = typeof raw.scheme === "string" ? raw.scheme : null;
    return raw.reviews && typeof raw.reviews === "object" ? raw.reviews : {};
  } catch {
    return {};
  }
}

function saveReviews() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ scheme: EXPECTED_SCHEME, reviews: state.reviews }));
}

function showNotice(text, tone = "info") {
  document.querySelector("main").prepend(node("p", `storage-notice storage-notice-${tone}`, text));
}

function setReview(item, value) {
  if (state.reviews[item.id] === value) delete state.reviews[item.id];
  else state.reviews[item.id] = value;
  saveReviews();
  renderSummary();
  renderGrid({ refilter: false });   // see app.js: keep the card under the cursor
  if (ui.viewer.open) renderViewerReview();
}

function reviewButtons(item, compact = false) {
  const container = node("div", "review-buttons");
  for (const [value, label] of REVIEW_CHOICES) {
    const button = node("button", "", compact ? label.split(" · ")[1] : label);
    button.type = "button";
    button.dataset.value = value;
    button.setAttribute("aria-pressed", String(state.reviews[item.id] === value));
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
      setReview(item, value);
    });
    container.append(button);
  }
  return container;
}

// -- cards ------------------------------------------------------------------

function addBadge(parent, text, className = "") {
  parent.append(node("span", `badge ${className}`.trim(), text));
}

function badgesFor(item) {
  const badges = node("div", "badges");
  const score = item.flood_text_score.toFixed(2);
  addBadge(badges, item.flood ? `FLOOD ${score}` : `NOT FLOOD ${score}`, item.flood ? "yes" : "no");
  const [label, cls] = LOCATION_BADGE[item.location];
  addBadge(badges, label, cls);
  if (!item.available) addBadge(badges, "NO LONGER AVAILABLE", "warning");
  const decision = state.reviews[item.id];
  if (decision) addBadge(badges, `review: ${decision}`, `review-${decision}`);
  return badges;
}

function tagList(tags, limit) {
  const list = node("div", "tag-list");
  const shown = limit ? tags.slice(0, limit) : tags;
  for (const tag of shown) list.append(node("span", "tag", tag));
  if (limit && tags.length > limit) list.append(node("span", "tag tag-more", `+${tags.length - limit}`));
  return list;
}

/** small -> large -> "could not be loaded" */
function loadImage(image, fallback, candidates) {
  const urls = candidates.filter(Boolean);
  let attempt = 0;
  image.hidden = false;
  fallback.hidden = true;
  image.onerror = () => {
    attempt += 1;
    if (attempt < urls.length) {
      image.src = urls[attempt];
    } else {
      image.hidden = true;
      fallback.hidden = false;
    }
  };
  image.referrerPolicy = "no-referrer";
  image.src = urls[0] || "";
}

function coverSurface(item, onOpen) {
  const button = node("button", "image-button video-cover");
  button.type = "button";
  button.setAttribute("aria-label", `Open video: ${item.title}`);
  const image = node("img");
  image.alt = item.title;
  image.loading = "lazy";
  image.decoding = "async";
  const fallback = node("div", "image-fallback");
  fallback.append(node("strong", "", "Thumbnail could not be loaded"));
  fallback.append(node("span", "", "Open the video from the detailed view."));
  const cover = item.thumbnails[0] || {};
  // The card uses the 480px rendition: the 320px one is soft at card size,
  // the 1280px one is wasteful for 24 cards.
  loadImage(image, fallback, [cover.image_url && cover.image_url.replace(/maxresdefault\.jpg$/, "hqdefault.jpg"),
    cover.image_url, cover.thumbnail_url]);
  button.append(image, fallback);
  const duration = formatDuration(item.duration_s);
  if (duration) button.append(node("span", "duration", duration));
  button.append(node("span", "play-mark", "▶"));
  button.addEventListener("click", onOpen);
  return button;
}

function externalLink(href, className, text) {
  const link = node("a", className, text);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function renderCard(item, index) {
  const card = node("article", "image-card");
  if (state.reviews[item.id]) card.classList.add(`reviewed-${state.reviews[item.id]}`);
  card.append(coverSurface(item, () => openViewer(index)));
  const body = node("div", "card-body");
  body.append(badgesFor(item));
  const heading = node("h2", "card-title");
  heading.append(externalLink(item.source_url, "card-title-link", item.title));
  body.append(heading);
  const meta = [item.published ? item.published.slice(0, 10) : "undated", formatDuration(item.duration_s)]
    .filter(Boolean).join(" · ");
  body.append(node("p", "card-meta", meta));
  if (item.tags.length) body.append(tagList(item.tags, TAGS_ON_CARD));
  body.append(node("p", "caption", item.description || "No description."));
  body.append(reviewButtons(item, true));
  card.append(body);
  return card;
}

// -- filtering --------------------------------------------------------------

function matchesFlood(item, mode) {
  if (mode === "all") return true;
  return mode === "flood" ? item.flood : !item.flood;
}

function matchesLocation(item, mode) {
  if (mode === "all") return true;
  if (mode === "coords") return item.lat !== null && item.lat !== undefined;
  return item.location === mode;
}

function matchesReview(item, mode) {
  const decision = state.reviews[item.id];
  if (mode === "all") return true;
  if (mode === "unreviewed") return !decision;
  return decision === mode;
}

function matchesSearch(item, words) {
  if (!words.length) return true;
  const haystack = item._search || (item._search =
    `${item.title}\n${item.tags.join(" ")}\n${item.description}`.toLowerCase());
  return words.every((word) => haystack.includes(word));
}

function sortItems(items) {
  const order = ui.sortOrder.value;
  return items.sort((a, b) => {
    if (order === "oldest") return (a.published || "9999").localeCompare(b.published || "9999");
    if (order === "score") return b.flood_text_score - a.flood_text_score || b.published.localeCompare(a.published);
    if (order === "shortest") return (a.duration_s ?? Infinity) - (b.duration_s ?? Infinity);
    return b.published.localeCompare(a.published);
  });
}

function filteredItems() {
  const words = ui.search.value.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = state.items.filter((item) =>
    matchesFlood(item, ui.flood.value)
    && matchesLocation(item, ui.location.value)
    && matchesReview(item, ui.reviewFilter.value)
    && matchesSearch(item, words));
  return sortItems(matches);
}

// -- rendering --------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  const reviewed = state.items.filter((item) => state.reviews[item.id]).length;
  ui.summary.textContent =
    `${s.videos.toLocaleString()} YouTube videos, ${s.flood.toLocaleString()} flood-related `
    + `(text score ≥ ${s.flood_threshold}): ${s.nyc.toLocaleString()} in NYC, `
    + `${s.ny_not_nyc.toLocaleString()} elsewhere in New York State, `
    + `${s.outside_ny.toLocaleString()} outside New York, ${s.location_unknown.toLocaleString()} unknown. `
    + `${reviewed.toLocaleString()} reviewed in this browser.`;
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

function closeViewer() {
  // Clearing the src stops playback; closing the dialog alone would not.
  ui.player.src = "about:blank";
}

function renderViewerReview() {
  const item = state.filtered[state.viewerIndex];
  if (!item) return;
  ui.viewerReview.replaceChildren(reviewButtons(item));
  ui.viewerBadges.replaceChildren(...badgesFor(item).childNodes);
}

function setLink(link, href) {
  link.hidden = !href;
  link.href = href || "#";
}

function renderFrames(item) {
  // Skip the cover (index 0): the strip is the honest view, YouTube's own
  // frames at ~25/50/75% of the video, not the uploader's chosen image.
  const frames = item.thumbnails.slice(1);
  ui.viewerFrames.replaceChildren(...frames.map((frame, n) => {
    const link = externalLink(frame.image_url, "frame", "");
    const image = node("img");
    image.alt = `Automatic frame ${n + 1}`;
    image.referrerPolicy = "no-referrer";
    image.loading = "lazy";
    image.src = frame.image_url;
    link.append(image);
    return link;
  }));
  ui.viewerFrames.hidden = frames.length === 0;
}

function renderViewer() {
  const item = state.filtered[state.viewerIndex];
  if (!item) return;
  // YouTube's player refuses to start without a referrer (error 153), and
  // this site sends none by default; the origin alone is enough for it.
  ui.player.referrerPolicy = "strict-origin-when-cross-origin";
  ui.player.src = `${item.embed_url}?rel=0&modestbranding=1`;
  renderFrames(item);
  ui.viewerPosition.textContent = `${state.viewerIndex + 1} / ${state.filtered.length}`;
  ui.viewerTitle.textContent = item.title;
  ui.viewerTags.replaceChildren(...tagList(item.tags, 0).childNodes);
  ui.viewerDescription.textContent = item.description || "No description.";
  renderViewerReview();

  const hasCoords = item.lat !== null && item.lat !== undefined;
  ui.viewerMetadata.replaceChildren(
    metadataRow("Uploaded", item.published ? item.published.slice(0, 10) : "Unknown"),
    metadataRow("Recorded", item.recording_date ? item.recording_date.slice(0, 10) : "Not set by uploader"),
    metadataRow("Duration", formatDuration(item.duration_s) || "Unknown"),
    metadataRow("Flood text score", `${item.flood_text_score.toFixed(3)} (${item.flood ? "flood" : "below threshold"})`),
    metadataRow("Location", LOCATION_BADGE[item.location][0].toLowerCase()),
    metadataRow("Coordinates", hasCoords ? `${Number(item.lat).toFixed(5)}, ${Number(item.lon).toFixed(5)}` : "None"),
    metadataRow("Video id", item.video_id),
  );

  setLink(ui.viewerSourceLink, item.source_url);
  setLink(ui.viewerMapLink, hasCoords
    ? `https://www.openstreetmap.org/?mlat=${item.lat}&mlon=${item.lon}#map=16/${item.lat}/${item.lon}`
    : "");
  setLink(ui.viewerStreetViewLink, hasCoords
    ? `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${item.lat},${item.lon}`
    : "");
  ui.previousVideo.disabled = state.viewerIndex <= 0;
  ui.nextVideo.disabled = state.viewerIndex >= state.filtered.length - 1;
}

function moveViewer(delta) {
  const next = state.viewerIndex + delta;
  if (next < 0 || next >= state.filtered.length) return;
  state.viewerIndex = next;
  renderViewer();
}

function exportReviews() {
  const selected = state.items
    .filter((item) => state.reviews[item.id])
    .map((item) => ({
      review: state.reviews[item.id],
      id: item.id,
      id_scheme: EXPECTED_SCHEME,
      record_id: item.record_id,
      video_id: item.video_id,
      title: item.title,
      published: item.published,
      flood_text_score: item.flood_text_score,
      location: item.location,
      lat: item.lat,
      lon: item.lon,
      source_url: item.source_url,
    }));
  const blob = new Blob([JSON.stringify(selected, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "youtube_video_review_decisions.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

// -- wiring -----------------------------------------------------------------

for (const control of [ui.flood, ui.location, ui.reviewFilter, ui.sortOrder]) {
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
ui.exportReviews.addEventListener("click", exportReviews);
ui.closeViewer.addEventListener("click", () => ui.viewer.close());
ui.viewer.addEventListener("close", closeViewer);       // also fires on Escape
ui.previousVideo.addEventListener("click", () => moveViewer(-1));
ui.nextVideo.addEventListener("click", () => moveViewer(1));
ui.viewer.addEventListener("click", (event) => {
  if (event.target === ui.viewer) ui.viewer.close();
});
document.addEventListener("keydown", (event) => {
  if (!ui.viewer.open) return;
  if (event.key === "ArrowLeft") moveViewer(-1);
  if (event.key === "ArrowRight") moveViewer(1);
  const choice = REVIEW_CHOICES[["1", "2", "3"].indexOf(event.key)];
  if (choice) {
    const item = state.filtered[state.viewerIndex];
    if (item) setReview(item, choice[0]);
  }
});

fetch("/api/videos")
  .then((response) => {
    if (!response.ok) throw new Error(`Catalog request failed: ${response.status}`);
    return response.json();
  })
  .then((catalog) => {
    if (!catalog.available) {
      throw new Error(`No video data yet (${catalog.source_file}). `
        + "Run scraping/video_scraping/youtube_scrape.py, then restart the server.");
    }
    state.items = catalog.items;
    state.summary = catalog.summary;
    if (catalog.review_id_scheme !== EXPECTED_SCHEME) {
      showNotice(
        `Review id scheme mismatch: this page expects "${EXPECTED_SCHEME}" but the server emits `
        + `"${catalog.review_id_scheme}". Saved decisions will not match.`, "warn");
    } else if (storedScheme && storedScheme !== EXPECTED_SCHEME) {
      showNotice(
        `Saved decisions use id scheme "${storedScheme}", but this page expects "${EXPECTED_SCHEME}". `
        + "They are preserved in localStorage but are not being shown.", "warn");
    }
    renderSummary();
    renderGrid();
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the video dataset";
    ui.resultCount.textContent = "";
    ui.empty.hidden = false;
    ui.empty.querySelector("h2").textContent = "The video catalog could not be loaded";
    ui.empty.querySelector("p").textContent = error.message;
  });
