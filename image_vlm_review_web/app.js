"use strict";

const PAGE_SIZE = 24;

const state = {
  items: [],
  filtered: [],
  summary: null,
  answer: "all",
  page: 1,
  viewerIndex: -1,
};

const ui = {
  summary: document.querySelector("#summary"),
  totalCount: document.querySelector("#totalCount"),
  yesCount: document.querySelector("#yesCount"),
  noCount: document.querySelector("#noCount"),
  yesRate: document.querySelector("#yesRate"),
  allButtonCount: document.querySelector("#allButtonCount"),
  yesButtonCount: document.querySelector("#yesButtonCount"),
  noButtonCount: document.querySelector("#noButtonCount"),
  answerButtons: [...document.querySelectorAll("[data-answer]")],
  search: document.querySelector("#search"),
  outlet: document.querySelector("#outlet"),
  verification: document.querySelector("#verification"),
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
  viewerCaption: document.querySelector("#viewerCaption"),
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

function addBadge(parent, text, className = "") {
  parent.append(node("span", `badge ${className}`.trim(), text));
}

function badgesFor(item) {
  const badges = node("div", "badges");
  addBadge(badges, item.answer.toUpperCase(), item.answer);
  addBadge(badges, item.outlet.toUpperCase(), "outlet");
  addBadge(
    badges,
    item.article_flood_verified ? "article verified" : "article unverified",
    item.article_flood_verified ? "verified" : "warning",
  );
  if (item.duplicate_count > 1) addBadge(badges, `${item.duplicate_count}× repeated URL`, "warning");
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

function renderCard(item, index) {
  const card = node("article", `image-card result-${item.answer}`);
  card.append(imageSurface(item, () => openViewer(index)));
  const body = node("div", "card-body");
  body.append(badgesFor(item));
  body.append(node("h2", "card-title", item.article_title));
  body.append(node("p", "caption", item.caption || "No caption available."));
  const output = node("p", "model-inline");
  output.append(node("strong", "", "Qwen: "));
  output.append(document.createTextNode(item.model_output || item.answer));
  body.append(output);
  card.append(body);
  return card;
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
  const query = ui.search.value.trim().toLocaleLowerCase();
  const matches = state.items.filter((item) => {
    if (state.answer !== "all" && item.answer !== state.answer) return false;
    if (ui.outlet.value !== "all" && item.outlet !== ui.outlet.value) return false;
    if (ui.verification.value === "verified" && !item.article_flood_verified) return false;
    if (ui.verification.value === "unverified" && item.article_flood_verified) return false;
    const text = `${item.article_title} ${item.caption} ${item.model_output} ${item.outlet}`.toLocaleLowerCase();
    return !query || text.includes(query);
  });
  return sortItems(matches);
}

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  ui.totalCount.textContent = s.total.toLocaleString();
  ui.yesCount.textContent = s.yes.toLocaleString();
  ui.noCount.textContent = s.no.toLocaleString();
  ui.yesRate.textContent = `${s.yes_rate}%`;
  ui.allButtonCount.textContent = s.total.toLocaleString();
  ui.yesButtonCount.textContent = s.yes.toLocaleString();
  ui.noButtonCount.textContent = s.no.toLocaleString();
  ui.summary.textContent = `${s.unique_images.toLocaleString()} distinct image URLs across ${s.outlets.length} outlets.`;
}

function renderGrid() {
  state.filtered = filteredItems();
  const pageCount = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pageCount);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = state.filtered.slice(start, start + PAGE_SIZE);
  ui.grid.replaceChildren(...pageItems.map((item, offset) => renderCard(item, start + offset)));
  ui.empty.hidden = pageItems.length > 0;

  const label = state.answer === "all" ? "results" : `${state.answer.toUpperCase()} results`;
  const first = pageItems.length ? start + 1 : 0;
  const last = Math.min(start + PAGE_SIZE, state.filtered.length);
  ui.resultCount.textContent = `${state.filtered.length.toLocaleString()} matching ${label} · showing ${first}–${last}`;
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
  ui.viewerModelOutput.textContent = item.model_output || item.answer;
  ui.viewerMetadata.replaceChildren(
    metadataRow("Outlet", item.outlet.toUpperCase()),
    metadataRow("Article date", item.article_date || "Unknown"),
    metadataRow("Article flood score", formatScore(item.article_flood_score)),
    metadataRow("Image position", String(item.image_index)),
    metadataRow("Markup source", item.image_source),
    metadataRow("Model", item.model || "Unknown"),
    metadataRow("Attempts", String(item.attempts)),
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

ui.answerButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.answer = button.dataset.answer;
    ui.answerButtons.forEach((candidate) => {
      const active = candidate === button;
      candidate.classList.toggle("active", active);
      candidate.setAttribute("aria-pressed", String(active));
    });
    state.page = 1;
    renderGrid();
  });
});

for (const control of [ui.search, ui.outlet, ui.verification, ui.sortOrder]) {
  control.addEventListener(control === ui.search ? "input" : "change", () => {
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

fetch("/api/results")
  .then((response) => {
    if (!response.ok) throw new Error(`Results request failed: ${response.status}`);
    return response.json();
  })
  .then((catalog) => {
    state.items = catalog.items;
    state.summary = catalog.summary;
    for (const outlet of catalog.summary.outlets) {
      const option = node("option", "", outlet.toUpperCase());
      option.value = outlet;
      ui.outlet.append(option);
    }
    renderSummary();
    renderGrid();
  })
  .catch((error) => {
    ui.summary.textContent = "Could not load the verification results";
    ui.empty.hidden = false;
    ui.empty.querySelector("h2").textContent = "The result catalog could not be loaded";
    ui.empty.querySelector("p").textContent = error.message;
  });
