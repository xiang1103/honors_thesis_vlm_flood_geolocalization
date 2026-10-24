"use strict";

const PAGE_SIZE = 24;

const LOCATION_BADGE = {
  nyc: ["NYC", "nyc"],
  ny: ["NY STATE", "ny"],
  unknown: ["NYC UNKNOWN", "unknown"],
};

const state = {
  items: [],
  filtered: [],
  summary: null,
  page: 1,
  viewerIndex: -1,
};

const ui = {
  summary: document.querySelector("#summary"),
  event: document.querySelector("#event"),
  location: document.querySelector("#location"),
  sortOrder: document.querySelector("#sortOrder"),
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
  // noopener: the opened tab gets no handle on this page. noreferrer: the
  // source never sees this site as a referrer.
  link.rel = "noopener noreferrer";
  return link;
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

function locationBadge(location) {
  const [label, cls] = LOCATION_BADGE[location] || LOCATION_BADGE.unknown;
  const badges = node("div", "badges");
  badges.append(node("span", `badge ${cls}`, label));
  return badges;
}

function renderCard(item, index) {
  const card = node("article", "image-card");
  card.append(imageSurface(item, () => openViewer(index)));
  openOnCardClick(card, () => openViewer(index));
  const body = node("div", "card-body");
  body.append(locationBadge(item.location));
  const heading = node("h2", "card-title");
  heading.append(item.source_url
    ? externalLink(item.source_url, "card-title-link", item.title)
    : document.createTextNode(item.title));
  body.append(heading);
  const meta = [item.date ? item.date.slice(0, 10) : "undated", item.event_label].filter(Boolean).join(" · ");
  body.append(node("p", "card-meta", meta));
  body.append(node("p", "caption", item.text || "No description available."));
  card.append(body);
  return card;
}

// -- filtering --------------------------------------------------------------

function matchesLocation(item, mode) {
  if (mode === "all") return true;
  if (mode === "coords") return item.lat !== null && item.lat !== undefined;
  return item.location === mode;
}

function sortItems(items) {
  const order = ui.sortOrder.value;
  return items.sort((a, b) => {
    if (order === "oldest") return (a.date || "9999").localeCompare(b.date || "9999");
    return b.date.localeCompare(a.date);
  });
}

function filteredItems() {
  const matches = state.items.filter((item) => {
    if (ui.event.value !== "all" && (item.event_label || "(none)") !== ui.event.value) return false;
    return matchesLocation(item, ui.location.value);
  });
  return sortItems(matches);
}

/** Report types, with counts, from the catalog's summary. */
function populateEvents() {
  const counts = new Map();
  for (const source of Object.keys(state.summary.events)) {
    for (const [event, count] of state.summary.events[source] || []) {
      counts.set(event, (counts.get(event) || 0) + count);
    }
  }
  const options = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  ui.event.replaceChildren(node("option", "", "All report types"));
  ui.event.firstChild.value = "all";
  for (const [event, count] of options) {
    const option = node("option", "", `${event} (${count.toLocaleString()})`);
    option.value = event;
    ui.event.append(option);
  }
}

// -- rendering --------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  ui.summary.textContent =
    `${s.images.toLocaleString()} flood photos from MyCoast citizen reports: `
    + `${s.nyc.toLocaleString()} in NYC, ${s.ny_not_nyc.toLocaleString()} elsewhere in New York State, `
    + `${s.location_unknown.toLocaleString()} without coordinates.`;
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
  ui.viewerBadges.replaceChildren(...badgesFor(item).childNodes);

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

// -- wiring -----------------------------------------------------------------

for (const control of [ui.event, ui.location, ui.sortOrder]) {
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
});

fetch("/api/images.json")
  .then((response) => {
    if (!response.ok) throw new Error(`Catalog request failed: ${response.status}`);
    return response.json();
  })
  .then((catalog) => {
    state.items = catalog.items;
    state.summary = catalog.summary;

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
