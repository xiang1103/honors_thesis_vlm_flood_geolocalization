"use strict";

const PAGE_SIZE = 24;

// Decisions live ONLY here -- there is no server-side copy -- so the storage
// format is versioned and every read is checked against the scheme the server
// says it is currently emitting. A future id change then surfaces as a visible
// banner and a migration, never as a review list that silently reads empty.
const LEGACY_STORAGE_KEY = "vlm-flood-image-reviews-v1";
const STORAGE_KEY = "vlm-flood-image-reviews-v2";
const EXPECTED_SCHEME = "human_review_v2";

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
  search: document.querySelector("#search"),
  outlet: document.querySelector("#outlet"),
  verification: document.querySelector("#verification"),
  visualHint: document.querySelector("#visualHint"),
  reviewFilter: document.querySelector("#reviewFilter"),
  exportReviews: document.querySelector("#exportReviews"),
  resultCount: document.querySelector("#resultCount"),
  pageStatus: document.querySelector("#pageStatus"),
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
  viewerMetadata: document.querySelector("#viewerMetadata"),
  viewerArticleLink: document.querySelector("#viewerArticleLink"),
  viewerImageLink: document.querySelector("#viewerImageLink"),
  viewerReviewButtons: document.querySelector("#viewerReviewButtons"),
  previousImage: document.querySelector("#previousImage"),
  nextImage: document.querySelector("#nextImage"),
};

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
      orphaned += 1;                      // image no longer in data/outlets
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

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function addBadge(parent, text, className = "") {
  parent.append(node("span", `badge ${className}`.trim(), text));
}

function formatScore(score) {
  const numeric = Number(score);
  return Number.isFinite(numeric) ? numeric.toFixed(3) : "—";
}

function reviewButtons(item, compact = false) {
  const container = node("div", "review-buttons");
  const choices = [
    ["useful", compact ? "Useful" : "1 · Useful"],
    ["reject", compact ? "Reject" : "2 · Reject"],
    ["unsure", compact ? "Unsure" : "3 · Unsure"],
  ];
  for (const [value, label] of choices) {
    const button = node("button", "", label);
    button.type = "button";
    button.dataset.value = value;
    button.setAttribute("aria-pressed", String(state.reviews[item.id] === value));
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setReview(item, value);
    });
    container.append(button);
  }
  return container;
}

function setReview(item, value) {
  if (state.reviews[item.id] === value) delete state.reviews[item.id];
  else state.reviews[item.id] = value;
  saveReviews();
  renderSummary();
  if (ui.viewer.open) renderViewer();
  renderGrid();
}

function badgesFor(item) {
  const badges = node("div", "badges");
  addBadge(badges, item.outlet.toUpperCase(), "outlet");
  if (item.flood_verified) addBadge(badges, "article verified", "verified");
  else addBadge(badges, "article unverified", "warning");
  if (item.suspect_nonstreet) addBadge(badges, "map / file-photo hint", "warning");
  if (item.duplicate_count > 1) addBadge(badges, `${item.duplicate_count}× repeated URL`, "warning");
  if (state.reviews[item.id]) addBadge(badges, `review: ${state.reviews[item.id]}`);
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
  fallback.append(node("span", "", "Open the original image link to inspect it."));
  image.addEventListener("error", () => {
    image.hidden = true;
    fallback.hidden = false;
  }, { once: true });
  button.append(image, fallback);
  button.addEventListener("click", onOpen);
  return button;
}

function renderCard(item, index) {
  const card = node("article", "image-card");
  card.append(imageSurface(item, () => openViewer(index)));
  const body = node("div", "card-body");
  body.append(badgesFor(item));
  body.append(node("h2", "card-title", item.article_title));
  body.append(node("p", "caption", item.caption || "No caption available."));
  body.append(reviewButtons(item, true));
  card.append(body);
  return card;
}

function filteredItems() {
  const query = ui.search.value.trim().toLocaleLowerCase();
  return state.items.filter((item) => {
    if (ui.outlet.value !== "all" && item.outlet !== ui.outlet.value) return false;
    if (ui.verification.value === "verified" && !item.flood_verified) return false;
    if (ui.verification.value === "unverified" && item.flood_verified) return false;
    if (ui.visualHint.value === "candidate" && item.suspect_nonstreet) return false;
    if (ui.visualHint.value === "suspect" && !item.suspect_nonstreet) return false;
    if (ui.visualHint.value === "duplicate" && item.duplicate_count < 2) return false;
    const review = state.reviews[item.id];
    if (ui.reviewFilter.value === "unreviewed" && review) return false;
    if (["useful", "reject", "unsure"].includes(ui.reviewFilter.value) && review !== ui.reviewFilter.value) return false;
    if (query && !`${item.article_title} ${item.caption}`.toLocaleLowerCase().includes(query)) return false;
    return true;
  });
}

function renderSummary() {
  if (!state.summary) return;
  const reviewed = Object.keys(state.reviews).length;
  ui.summary.textContent = `${state.summary.images.toLocaleString()} images · ${state.summary.articles.toLocaleString()} articles · ${reviewed.toLocaleString()} reviewed`;
}

function renderGrid() {
  state.filtered = filteredItems();
  const pageCount = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pageCount);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = state.filtered.slice(start, start + PAGE_SIZE);
  ui.grid.replaceChildren(...pageItems.map((item, offset) => renderCard(item, start + offset)));
  ui.empty.hidden = pageItems.length > 0;
  ui.resultCount.textContent = `${state.filtered.length.toLocaleString()} matching images · showing ${pageItems.length ? start + 1 : 0}–${Math.min(start + PAGE_SIZE, state.filtered.length)}`;
  ui.pageStatus.textContent = `Page ${state.page} of ${pageCount}`;
  ui.previousPage.disabled = state.page <= 1;
  ui.nextPage.disabled = state.page >= pageCount;
}

function openViewer(index) {
  state.viewerIndex = index;
  renderViewer();
  ui.viewer.showModal();
}

function metadataRow(term, detail) {
  const fragment = document.createDocumentFragment();
  fragment.append(node("dt", "", term), node("dd", "", detail));
  return fragment;
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
  ui.viewerBadges.replaceChildren(...badgesFor(item).childNodes);
  ui.viewerTitle.textContent = item.article_title;
  ui.viewerCaption.textContent = item.caption || "No caption available.";
  ui.viewerMetadata.replaceChildren(
    metadataRow("Outlet", item.outlet.toUpperCase()),
    metadataRow("Article date", item.article_date || "Unknown"),
    metadataRow("Flood score", formatScore(item.flood_score)),
    metadataRow("Image", `${item.image_index} of ${item.article_image_count}`),
    metadataRow("Markup source", item.image_source),
  );
  ui.viewerArticleLink.href = item.article_url || "#";
  ui.viewerArticleLink.hidden = !item.article_url;
  ui.viewerImageLink.href = item.image_url;
  ui.viewerReviewButtons.replaceChildren(...reviewButtons(item).childNodes);
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
      outlet: item.outlet,
      article_title: item.article_title,
      article_date: item.article_date,
      article_url: item.article_url,
      image_url: item.image_url,
      caption: item.caption,
      flood_verified: item.flood_verified,
      flood_score: item.flood_score,
    }));
  const blob = new Blob([JSON.stringify(selected, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "flood_image_review_decisions.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

for (const control of [ui.search, ui.outlet, ui.verification, ui.visualHint, ui.reviewFilter]) {
  control.addEventListener(control === ui.search ? "input" : "change", () => {
    state.page = 1;
    renderGrid();
  });
}
ui.previousPage.addEventListener("click", () => { state.page -= 1; renderGrid(); window.scrollTo({ top: 0, behavior: "smooth" }); });
ui.nextPage.addEventListener("click", () => { state.page += 1; renderGrid(); window.scrollTo({ top: 0, behavior: "smooth" }); });
ui.closeViewer.addEventListener("click", () => ui.viewer.close());
ui.previousImage.addEventListener("click", () => moveViewer(-1));
ui.nextImage.addEventListener("click", () => moveViewer(1));
ui.exportReviews.addEventListener("click", exportReviews);
ui.viewer.addEventListener("click", (event) => { if (event.target === ui.viewer) ui.viewer.close(); });
document.addEventListener("keydown", (event) => {
  if (!ui.viewer.open) return;
  if (event.key === "ArrowLeft") moveViewer(-1);
  if (event.key === "ArrowRight") moveViewer(1);
  const item = state.filtered[state.viewerIndex];
  if (event.key === "1") setReview(item, "useful");
  if (event.key === "2") setReview(item, "reject");
  if (event.key === "3") setReview(item, "unsure");
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
          `data/outlets and could not be carried forward. They remain under ` +
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
