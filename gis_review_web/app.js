"use strict";

const PAGE_SIZE = 24;

// Decisions live ONLY in this browser -- there is no server-side copy -- so the
// payload records the id scheme it was saved under, and a mismatch with what
// the server emits is shown as a banner rather than as an empty review list.
// A different port is a different origin, so this never meets the news site's
// storage; the distinct key is for clarity, not isolation.
const STORAGE_KEY = "vlm-flood-gis-reviews-v1";
const EXPECTED_SCHEME = "gis_review_v1";

const REVIEW_CHOICES = [
  ["useful", "1 · Useful"],
  ["reject", "2 · Reject"],
  ["unsure", "3 · Unsure"],
];

const LOCATION_BADGE = {
  nyc: ["NYC", "nyc"],
  ny: ["NY STATE", "ny"],
  unknown: ["NYC UNKNOWN", "unknown"],
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
  source: document.querySelector("#source"),
  event: document.querySelector("#event"),
  location: document.querySelector("#location"),
  reviewFilter: document.querySelector("#reviewFilter"),
  sortOrder: document.querySelector("#sortOrder"),
  exportReviews: document.querySelector("#exportReviews"),
  resultCount: document.querySelector("#resultCount"),
  pageStatus: document.querySelector("#pageStatus"),
  paginationStatus: document.querySelector("#paginationStatus"),
  previousPage: document.querySelector("#previousPage"),
  nextPage: document.querySelector("#nextPage"),
  grid: document.querySelector("#imageGrid"),
  empty: document.querySelector("#emptyState"),
  viewer: document.querySelector("#viewer"),
  closeViewer: document.querySelector("#closeViewer"),
  viewerImage: document.querySelector("#viewerImage"),
  viewerFallback: document.querySelector("#viewerFallback"),
  viewerPosition: document.querySelector("#viewerPosition"),
  viewerBadges: document.querySelector("#viewerBadges"),
  viewerTitle: document.querySelector("#viewerTitle"),
  viewerText: document.querySelector("#viewerText"),
  viewerReview: document.querySelector("#viewerReview"),
  viewerMetadata: document.querySelector("#viewerMetadata"),
  viewerSourceLink: document.querySelector("#viewerSourceLink"),
  viewerImageLink: document.querySelector("#viewerImageLink"),
  viewerMapLink: document.querySelector("#viewerMapLink"),
  viewerStreetViewLink: document.querySelector("#viewerStreetViewLink"),
  previousImage: document.querySelector("#previousImage"),
  nextImage: document.querySelector("#nextImage"),
};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
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
  // Re-filtering here would pull a just-reviewed card out from under the
  // cursor when "Not reviewed yet" is selected; the grid re-renders in place
  // and the filter applies on the next page change or filter change.
  renderGrid({ refilter: false });
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
  addBadge(badges, item.source_label.toUpperCase(), "source");
  const [label, cls] = LOCATION_BADGE[item.location];
  addBadge(badges, label, cls);
  if (item.nyc_basis === "category") addBadge(badges, "NYC from category", "warning");
  const decision = state.reviews[item.id];
  if (decision) addBadge(badges, `review: ${decision}`, `review-${decision}`);
  return badges;
}

/** thumbnail -> original -> "could not be loaded". A Commons thumbnail wider
 *  than its original is an HTTP error there, so the second step matters. */
function loadImage(image, fallback, item, preferThumbnail) {
  const candidates = preferThumbnail && item.thumbnail_url
    ? [item.thumbnail_url, item.image_url]
    : [item.image_url];
  let attempt = 0;
  image.hidden = false;
  fallback.hidden = true;
  image.onerror = () => {
    attempt += 1;
    if (attempt < candidates.length) {
      image.src = candidates[attempt];
    } else {
      image.hidden = true;
      fallback.hidden = false;
    }
  };
  image.referrerPolicy = "no-referrer";
  image.src = candidates[0];
}

function imageSurface(item, onOpen) {
  const button = node("button", "image-button");
  button.type = "button";
  button.setAttribute("aria-label", `Open image: ${item.title}`);
  const image = node("img");
  image.alt = item.text || item.title;
  image.loading = "lazy";
  image.decoding = "async";
  const fallback = node("div", "image-fallback");
  fallback.append(node("strong", "", "Image could not be loaded"));
  fallback.append(node("span", "", "Open it from the detailed view."));
  loadImage(image, fallback, item, true);
  button.append(image, fallback);
  button.addEventListener("click", onOpen);
  return button;
}

function externalLink(href, className, text) {
  const link = node("a", className, text);
  link.href = href;
  link.target = "_blank";
  // noopener: the opened tab must not get a handle on this page, which holds
  // the decisions. noreferrer: the source never sees a localhost referrer.
  link.rel = "noopener noreferrer";
  return link;
}

function renderCard(item, index) {
  const card = node("article", "image-card");
  if (state.reviews[item.id]) card.classList.add(`reviewed-${state.reviews[item.id]}`);
  card.append(imageSurface(item, () => openViewer(index)));
  const body = node("div", "card-body");
  body.append(badgesFor(item));
  const heading = node("h2", "card-title");
  heading.append(item.source_url
    ? externalLink(item.source_url, "card-title-link", item.title)
    : document.createTextNode(item.title));
  body.append(heading);
  const meta = [item.date ? item.date.slice(0, 10) : "undated", item.event_label].filter(Boolean).join(" · ");
  body.append(node("p", "card-meta", meta));
  body.append(node("p", "caption", item.text || "No description available."));
  body.append(reviewButtons(item, true));
  card.append(body);
  return card;
}

// -- filtering --------------------------------------------------------------

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

function sortItems(items) {
  const order = ui.sortOrder.value;
  return items.sort((a, b) => {
    if (order === "oldest") return (a.date || "9999").localeCompare(b.date || "9999");
    if (order === "source") return a.source_label.localeCompare(b.source_label) || b.date.localeCompare(a.date);
    return b.date.localeCompare(a.date);
  });
}

function filteredItems() {
  const matches = state.items.filter((item) => {
    if (ui.source.value !== "all" && item.source !== ui.source.value) return false;
    if (ui.event.value !== "all" && (item.event_label || "(none)") !== ui.event.value) return false;
    if (!matchesLocation(item, ui.location.value)) return false;
    return matchesReview(item, ui.reviewFilter.value);
  });
  return sortItems(matches);
}

/** Events depend on the chosen source: Commons alone has ~50 categories. */
function populateEvents() {
  const previous = ui.event.value;
  const counts = new Map();
  const sources = ui.source.value === "all" ? Object.keys(state.summary.events) : [ui.source.value];
  for (const source of sources) {
    for (const [event, count] of state.summary.events[source] || []) {
      counts.set(event, (counts.get(event) || 0) + count);
    }
  }
  const options = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  ui.event.replaceChildren(node("option", "", "All events"));
  ui.event.firstChild.value = "all";
  for (const [event, count] of options) {
    const option = node("option", "", `${event} (${count.toLocaleString()})`);
    option.value = event;
    ui.event.append(option);
  }
  ui.event.value = counts.has(previous) ? previous : "all";
}

// -- rendering --------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  const reviewed = state.items.filter((item) => state.reviews[item.id]).length;
  ui.summary.textContent =
    `${s.images.toLocaleString()} flood photos from ${s.sources.length} sources: `
    + `${s.nyc.toLocaleString()} in NYC, ${s.ny_not_nyc.toLocaleString()} elsewhere in New York State, `
    + `${s.location_unknown.toLocaleString()} without coordinates. `
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
  ui.resultCount.textContent = `${state.filtered.length.toLocaleString()} matching images · showing ${first}–${last}`;
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

function renderViewer() {
  const item = state.filtered[state.viewerIndex];
  if (!item) return;
  // The viewer shows the original, not the thumbnail.
  loadImage(ui.viewerImage, ui.viewerFallback, item, false);
  ui.viewerImage.alt = item.text || item.title;
  ui.viewerPosition.textContent = `${state.viewerIndex + 1} / ${state.filtered.length}`;
  ui.viewerTitle.textContent = item.title;
  ui.viewerText.textContent = item.text || "No description available.";
  renderViewerReview();

  const hasCoords = item.lat !== null && item.lat !== undefined;
  const rows = [
    metadataRow("Source", item.source_label),
    metadataRow("Date", item.date || "Unknown"),
    metadataRow("Event / type", item.event_label || "—"),
    metadataRow("Coordinates", hasCoords ? `${Number(item.lat).toFixed(5)}, ${Number(item.lon).toFixed(5)}` : "None"),
    metadataRow("NYC", `${item.location}${item.nyc_basis ? ` (from ${item.nyc_basis})` : ""}`),
  ];
  if (item.license) rows.push(metadataRow("License", item.license));
  if (item.credit) rows.push(metadataRow("Credit", item.credit));
  for (const [key, value] of Object.entries(item.extra)) {
    rows.push(metadataRow(key.replaceAll("_", " "), String(value)));
  }
  rows.push(metadataRow("Record id", item.record_id));
  ui.viewerMetadata.replaceChildren(...rows);

  setLink(ui.viewerSourceLink, item.source_url);
  setLink(ui.viewerImageLink, item.image_url);
  setLink(ui.viewerMapLink, hasCoords
    ? `https://www.openstreetmap.org/?mlat=${item.lat}&mlon=${item.lon}#map=18/${item.lat}/${item.lon}`
    : "");
  setLink(ui.viewerStreetViewLink, hasCoords
    ? `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${item.lat},${item.lon}`
    : "");
  ui.previousImage.disabled = state.viewerIndex <= 0;
  ui.nextImage.disabled = state.viewerIndex >= state.filtered.length - 1;
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
      source: item.source,
      title: item.title,
      date: item.date,
      event: item.event,
      lat: item.lat,
      lon: item.lon,
      location: item.location,
      image_url: item.image_url,
      source_url: item.source_url,
    }));
  const blob = new Blob([JSON.stringify(selected, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "gis_flood_review_decisions.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

// -- wiring -----------------------------------------------------------------

ui.source.addEventListener("change", () => {
  populateEvents();
  state.page = 1;
  renderGrid();
});
for (const control of [ui.event, ui.location, ui.reviewFilter, ui.sortOrder]) {
  control.addEventListener("change", () => {
    state.page = 1;
    renderGrid();
  });
}

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
ui.previousImage.addEventListener("click", () => moveViewer(-1));
ui.nextImage.addEventListener("click", () => moveViewer(1));
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
    if (item) {
      setReview(item, choice[0]);
      renderViewer();
    }
  }
});

fetch("/api/images")
  .then((response) => {
    if (!response.ok) throw new Error(`Catalog request failed: ${response.status}`);
    return response.json();
  })
  .then((catalog) => {
    state.items = catalog.items;
    state.summary = catalog.summary;

    const serverScheme = catalog.review_id_scheme || null;
    if (serverScheme && serverScheme !== EXPECTED_SCHEME) {
      showNotice(
        `Review id scheme mismatch: this page expects "${EXPECTED_SCHEME}" but the server emits `
        + `"${serverScheme}". Saved decisions will not match until app.js and gis_review_server.py agree.`,
        "warn",
      );
    } else if (storedScheme && storedScheme !== EXPECTED_SCHEME) {
      showNotice(
        `Saved decisions use id scheme "${storedScheme}", but this page expects "${EXPECTED_SCHEME}". `
        + "They are preserved in localStorage but are not being shown.",
        "warn",
      );
    }

    for (const source of catalog.summary.sources) {
      const option = node("option", "", `${source.label} (${source.count.toLocaleString()})`);
      option.value = source.value;
      ui.source.append(option);
    }
    populateEvents();
    renderSummary();
    renderGrid();
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the dataset";
    ui.empty.hidden = false;
    ui.empty.querySelector("h2").textContent = "The catalog could not be loaded";
    ui.empty.querySelector("p").textContent = error.message;
  });
