"use strict";

const PAGE_SIZE = 24;

// Decisions live ONLY here -- there is no server-side copy -- so the storage
// format is versioned and every read is checked against the scheme the server
// says it is currently emitting. A future id change then surfaces as a visible
// banner and a migration, never as a review list that silently reads empty.
//
// The merge of the two review sites deliberately did NOT touch these: the keys
// and the scheme are the same strings the labelling page always used, so every
// decision saved before the merge is still found after it.
const LEGACY_STORAGE_KEY = "vlm-flood-image-reviews-v1";
const STORAGE_KEY = "vlm-flood-image-reviews-v2";
const EXPECTED_SCHEME = "human_review_v2";

const REVIEW_CHOICES = [
  ["useful", "1 · Useful"],
  ["reject", "2 · Reject"],
  ["unsure", "3 · Unsure"],
];

// Set by loadReviews(), which runs while `state` is still being constructed
// and therefore cannot write to it.
let storedScheme = null;
let migrationDone = false;

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
  outlet: document.querySelector("#outlet"),
  nyc: document.querySelector("#nyc"),
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
  viewerCaption: document.querySelector("#viewerCaption"),
  viewerReview: document.querySelector("#viewerReview"),
  viewerModelOutput: document.querySelector("#viewerModelOutput"),
  viewerMetadata: document.querySelector("#viewerMetadata"),
  viewerArticleLink: document.querySelector("#viewerArticleLink"),
  viewerImageLink: document.querySelector("#viewerImageLink"),
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
    migrationDone = raw.migrated === true;
    return raw.reviews && typeof raw.reviews === "object" ? raw.reviews : {};
  } catch {
    return {};
  }
}

function loadLegacyReviews() {
  try {
    const value = JSON.parse(localStorage.getItem(LEGACY_STORAGE_KEY) || "null");
    return value && typeof value === "object" && !value.reviews ? value : {};
  } catch {
    return {};
  }
}

function saveReviews() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      scheme: EXPECTED_SCHEME,
      migrated: migrationDone,
      reviews: state.reviews,
    }),
  );
}

/** Carry pre-v2 decisions onto the new ids, keyed by the server's legacy_id.
 *
 *  Runs ONCE: the v2 payload records that it happened, so a decision the user
 *  later deletes is not resurrected on the next load, and an unmatched leftover
 *  is not re-reported on every page view. The v1 key is deliberately left in
 *  place -- if this remap is ever wrong, the originals are still recoverable.
 *
 *  Returns null when there was nothing to do, else {moved, orphaned}. */
function migrateLegacyReviews(items) {
  if (migrationDone) return null;

  const legacy = loadLegacyReviews();
  const legacyIds = Object.keys(legacy);
  if (!legacyIds.length) {
    migrationDone = true;
    return null;
  }

  const byLegacyId = new Map(items.map((item) => [item.legacy_id, item]));
  let moved = 0;
  let orphaned = 0;
  for (const legacyId of legacyIds) {
    const item = byLegacyId.get(legacyId);
    if (!item) {
      orphaned += 1;                      // image no longer in the dataset
      continue;
    }
    if (!state.reviews[item.id]) {
      state.reviews[item.id] = legacy[legacyId];
      moved += 1;
    }
  }
  migrationDone = true;
  saveReviews();
  return { moved, orphaned };
}

function showNotice(text, tone = "info") {
  const banner = node("p", `storage-notice storage-notice-${tone}`, text);
  document.querySelector("main").prepend(banner);
}

function setReview(item, value) {
  if (state.reviews[item.id] === value) delete state.reviews[item.id];
  else state.reviews[item.id] = value;
  saveReviews();
  renderSummary();
  renderGrid();
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
  addBadge(badges, item.model_answer.toUpperCase(), item.model_answer);
  addBadge(badges, item.outlet.toUpperCase(), "outlet");
  if (item.nyc_label === "nyc") {
    addBadge(badges, item.nyc_place || "NEW YORK CITY", "nyc");
  } else if (item.nyc_label === "nyc_metro_not_nyc") {
    addBadge(badges, item.nyc_place || "NYC METRO", "nyc-metro");
  }
  if (!item.article_flood_verified) addBadge(badges, "article unverified", "warning");
  if (item.duplicate_count > 1) addBadge(badges, `${item.duplicate_count}× repeated URL`, "warning");
  const decision = state.reviews[item.id];
  if (decision) addBadge(badges, `review: ${decision}`, `review-${decision}`);
  return badges;
}

function imageSurface(item, onOpen) {
  const button = node("button", "image-button");
  button.type = "button";
  button.setAttribute("aria-label", `Open image: ${item.caption || item.article_title}`);
  const image = node("img");
  image.src = item.image_url;
  image.alt = item.caption || `News image from ${item.article_title}`;
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  const fallback = node("div", "image-fallback");
  fallback.hidden = true;
  fallback.append(node("strong", "", "Image could not be loaded"));
  fallback.append(node("span", "", "Open it from the detailed view."));
  image.addEventListener("error", () => {
    image.hidden = true;
    fallback.hidden = false;
  }, { once: true });
  button.append(image, fallback);
  button.addEventListener("click", onOpen);
  return button;
}

function articleLink(item, className, text) {
  const link = node("a", className, text);
  link.href = item.article_url;
  link.target = "_blank";
  // noopener: the opened tab must not get a handle back to this page, which
  // still holds the review decisions. noreferrer keeps the publisher from
  // seeing a localhost referrer.
  link.rel = "noopener noreferrer";
  return link;
}

function renderCard(item, index) {
  const card = node("article", `image-card result-${item.model_answer}`);
  if (state.reviews[item.id]) card.classList.add(`reviewed-${state.reviews[item.id]}`);
  card.append(imageSurface(item, () => openViewer(index)));
  const body = node("div", "card-body");
  body.append(badgesFor(item));
  const heading = node("h2", "card-title");
  heading.append(articleLink(item, "card-title-link", item.article_title));
  body.append(heading);
  body.append(node("p", "caption", item.caption || "No caption available."));
  body.append(reviewButtons(item, true));
  card.append(body);
  return card;
}

// -- filtering --------------------------------------------------------------

// "" means the image is not in the New York set at all -- either judged
// somewhere else or never a candidate. The two are indistinguishable from
// here, so "Outside New York" reads as "not in the New York set", not as a
// positive verdict of elsewhere.
function matchesNyc(item, mode) {
  if (mode === "all") return true;
  if (mode === "nyc") return item.nyc_label === "nyc";
  if (mode === "metro") return item.nyc_label === "nyc_metro_not_nyc";
  if (mode === "any") return Boolean(item.nyc_label);
  if (mode === "none") return !item.nyc_label;
  return true;
}

function sortItems(items) {
  const order = ui.sortOrder.value;
  return items.sort((a, b) => {
    if (order === "oldest") return a.article_date.localeCompare(b.article_date);
    if (order === "outlet") return a.outlet.localeCompare(b.outlet) || b.article_date.localeCompare(a.article_date);
    return b.article_date.localeCompare(a.article_date);
  });
}

function filteredItems() {
  const matches = state.items.filter((item) => {
    if (item.model_answer !== "yes") return false;
    if (ui.outlet.value !== "all" && item.outlet !== ui.outlet.value) return false;
    return matchesNyc(item, ui.nyc.value);
  });
  return sortItems(matches);
}

// -- rendering --------------------------------------------------------------

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  const nycNote = s.nyc_available
    ? ` ${s.nyc.toLocaleString()} labelled New York City, ${s.nyc_metro.toLocaleString()} NYC metro.`
    : "";
  ui.summary.textContent =
    `${s.model_yes.toLocaleString()} images the model accepted, from `
    + `${s.articles.toLocaleString()} articles across ${s.outlets.length} outlets.${nycNote}`;
  // Without the labels every image reads as "not New York", which would look
  // like a finding rather than a missing file. Disable the control instead.
  ui.nyc.disabled = !s.nyc_available;
  ui.nyc.title = s.nyc_available
    ? ""
    : "Run verification/filter_nyc.py to enable the New York filter.";
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

function formatScore(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(3) : "—";
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

function renderViewer() {
  const item = state.filtered[state.viewerIndex];
  if (!item) return;
  ui.viewerImage.hidden = false;
  ui.viewerFallback.hidden = true;
  ui.viewerImage.src = item.image_url;
  ui.viewerImage.alt = item.caption || `News image from ${item.article_title}`;
  ui.viewerImage.referrerPolicy = "no-referrer";
  ui.viewerImage.onerror = () => {
    ui.viewerImage.hidden = true;
    ui.viewerFallback.hidden = false;
  };
  ui.viewerPosition.textContent = `${state.viewerIndex + 1} / ${state.filtered.length}`;
  ui.viewerTitle.textContent = item.article_title;
  ui.viewerCaption.textContent = item.caption || "No caption available.";
  ui.viewerModelOutput.textContent = item.model_output || item.model_answer;
  renderViewerReview();
  ui.viewerMetadata.replaceChildren(
    metadataRow("Outlet", item.outlet.toUpperCase()),
    metadataRow("Article date", item.article_date || "Unknown"),
    metadataRow("Article flood score", formatScore(item.article_flood_score)),
    metadataRow("Image position", String(item.image_index)),
    metadataRow("Markup source", item.image_source),
    metadataRow("Model", item.model || "Unknown"),
    metadataRow("Attempts", String(item.attempts)),
    ...(item.nyc_label
      ? [
          metadataRow("New York verdict", `${item.nyc_label} (${item.nyc_confidence})`),
          metadataRow("Place", item.nyc_place || "Unspecified"),
          metadataRow("Evidence", item.nyc_evidence || "—"),
        ]
      : []),
  );
  ui.viewerArticleLink.href = item.article_url || "#";
  ui.viewerArticleLink.hidden = !item.article_url;
  ui.viewerImageLink.href = item.image_url;
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
      occurrence_id: item.occurrence_id,
      outlet: item.outlet,
      article_title: item.article_title,
      article_date: item.article_date,
      article_url: item.article_url,
      image_url: item.image_url,
      caption: item.caption,
      model_answer: item.model_answer,
      nyc_label: item.nyc_label,
      nyc_place: item.nyc_place,
    }));
  const blob = new Blob([JSON.stringify(selected, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "flood_image_review_decisions.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

// -- wiring -----------------------------------------------------------------

for (const control of [ui.outlet, ui.nyc, ui.sortOrder]) {
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

    // The server is the authority on the current id scheme. Disagreement means
    // the ids were changed without migrating, so say so loudly rather than
    // rendering a page that looks like the decisions were never made.
    const serverScheme = catalog.review_id_scheme || null;
    if (serverScheme && serverScheme !== EXPECTED_SCHEME) {
      showNotice(
        `Review id scheme mismatch: this page expects "${EXPECTED_SCHEME}" but ` +
        `the server emits "${serverScheme}". Saved decisions will not match ` +
        `until app.js and image_review_server.py agree.`,
        "warn",
      );
    } else if (storedScheme && storedScheme !== EXPECTED_SCHEME) {
      showNotice(
        `Saved decisions use id scheme "${storedScheme}", but this page ` +
        `expects "${EXPECTED_SCHEME}". They are preserved in localStorage but ` +
        `are not being shown.`,
        "warn",
      );
    } else {
      const result = migrateLegacyReviews(catalog.items);
      if (result && result.moved) {
        showNotice(
          `Carried ${result.moved} review decision` +
          `${result.moved === 1 ? "" : "s"} forward from the previous id ` +
          `scheme. The old copy is kept under "${LEGACY_STORAGE_KEY}".`,
        );
      }
      if (result && result.orphaned) {
        showNotice(
          `${result.orphaned} old review decision` +
          `${result.orphaned === 1 ? "" : "s"} did not match any image now in ` +
          `the dataset and could not be carried forward. They remain under ` +
          `"${LEGACY_STORAGE_KEY}".`,
          "warn",
        );
      }
    }
    for (const outlet of catalog.summary.outlets) {
      const option = node("option", "", outlet.toUpperCase());
      option.value = outlet;
      ui.outlet.append(option);
    }
    renderSummary();
    renderGrid();
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the dataset";
    ui.empty.hidden = false;
    ui.empty.querySelector("h2").textContent = "The catalog could not be loaded";
    ui.empty.querySelector("p").textContent = error.message;
  });
