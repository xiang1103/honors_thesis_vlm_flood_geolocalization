# API-based scraping — design & decisions

The GDELT-discovery approach (`news_scrape.py`). **Superseded** by the direct
outlet scraper in the parent directory (`scrape.py` + `adapters.py`), which
discovers articles from each outlet's own flood index rather than a news API
and yields far more captioned images. Kept because the reasoning below still
holds and should not be re-derived — especially why no news API can supply
inline images, and the image failure taxonomy.

Was `scraping_api/CLAUDE.md`. Renamed and merged here so it reads as
documentation rather than agent instructions; the root `CLAUDE.md` describes
the pipeline actually in use.

---

## Goal

Aggregate a dataset of recent flood news for a **VLM (vision-language)** use
case — needs both clean article **text** and real flood **images with
captions**. Output JSON schema (one object per article):

```json
{
  "title": "str",
  "date":  "ISO-8601 str",
  "all_text": "str",
  "image_links": ["..."]   // see "Schema evolution" — moving to {url, caption}
}
```

## Architecture (and why)

```
GDELT DOC 2.0 API  ──►  {url, title, date, ONE lead image}   (discovery)
        │
        ▼
Fetch each article URL  ──►  full text (trafilatura)
        │                    + images (custom extractor, see scope below)
        ▼
Merge, dedupe, filter  ──►  JSON
```

Three stages, each chosen deliberately:

1. **Discovery = GDELT**, not direct CNN/Fox scraping.
   - Direct scraping of major outlets fails: Cloudflare/Akamai bot walls,
     JS-rendered bodies, no topic search, per-site parsers, ToS bans.
     (Confirmed: a `requests` fetch of a CNN flood URL returns nothing; the
     Irish Mirror returned a **181-byte bot stub**.)
   - GDELT indexes global news every 15 min, is free/no-key, and supports a
     **topic query** so we don't have to crawl to discover flood articles.

2. **Text = trafilatura**, not hand-written per-site parsers. Auto-extracts
   body text from arbitrary news HTML. `lxml_html_clean` is a required
   companion package (lxml 6.x split it out).

3. **Images = custom extractor** (see "Image scope"). trafilatura is
   unreliable for images (see "trafilatura image limits").

### Why not NewsAPI / GNews / Mediastack instead of GDELT?

They have the **same image limitation** — all return exactly **one** lead
(OpenGraph/social) image per article, never inline photos. They also
**truncate** article `content` (~200 chars) even on paid tiers, so you'd fetch
the URL for full text anyway. Free tiers are more restrictive than GDELT
(~100 req/day, ~1-month history, sometimes 24h delay). The image problem lives
**downstream** of discovery — on the article page — so switching discovery API
changes nothing about it. GDELT wins on precision (themes), history, and cost.

## Query precision — use the theme, not the word

`query="flood"` produced false positives like *"AI boom heats up Bay Area
housing market"* ("flood of demand"). Fixed by using the GDELT GKG theme:

```
FLOOD_THEME_QUERY = "theme:NATURAL_DISASTER_FLOOD"
```

This returns genuine flood events (Texas/Pakistan/Nepal floods, etc.). It's the
default `--query`; pass a keyword like `--query '"flash flood"'` to override.

## trafilatura image limits — why we don't rely on it for images

Two distinct failure modes (verified empirically on collected data):

- **Mode A — URL is in the static HTML but trafilatura discards it.** It only
  keeps images inside its detected "main content" block. Example: a Southeast
  Asia Post flood article had **33 `<img>` tags and trafilatura emitted 0
  `<graphic>`** even in recall mode. A custom parser reading the same bytes
  recovers these. **This is the case we can improve.**
- **Mode B — URL is genuinely absent from static bytes** (JS-rendered, or a
  bot-stub response). No static parser (trafilatura *or* custom) can help;
  only a headless browser (Playwright) or the discovery API's precomputed
  lead image. Example: Irish Mirror = 181-byte stub, 0 images anywhere.

Note: `<graphic>` is **trafilatura's own output tag**, not something sites use.
Sites use `<img>`/`<picture>`/`<figure>`/CSS; trafilatura re-emits a filtered
subset as `<graphic>`. In practice that XML step contributed ~nothing on real
pages — the images that landed in the dataset came from GDELT's `socialimage`
and trafilatura's `og:image` metadata (the single lead image), NOT `<graphic>`.

The lead/social image is still worth keeping as a **baseline**: the API/GDELT
crawled the page server-side and often has an image even when our own fetch
hits a bot wall.

## Image scope (DECIDED)

Deliberately **narrow** to the highest-value, statically-available images:

- **IN scope:** images in **galleries / carousels / slideshows** and
  captioned **`<figure>`** elements, each **paired with its caption**
  (`<figcaption>`, else `alt`/`aria-label`). For a VLM the (image, caption)
  pair is the gold — a flat URL list loses the text↔image association.
- **OUT of scope (explicitly):** lazy-loaded images (`data-src` etc.),
  JS-rendered / non-static images, CSS `background-image`. Not worth the
  Playwright complexity for this dataset.
- **Filtering:** drop icons/logos/avatars/sprites/tracking pixels, tiny
  images (small `width`/`height`), `.svg`, and cross-promo thumbnails of
  *other* articles (real pages mix these in — e.g. unrelated `John-Ternus`,
  `Shein` thumbnails appeared alongside the real flood photo).
- Keep the GDELT `socialimage` / `og:image` as a guaranteed baseline entry.

Full image failure taxonomy (for future reference, if scope ever widens):
Bucket 1 (static, recoverable): galleries/carousels, lead/hero figures,
`data-src` lazy-load, `srcset`/`<picture>` (higher-res of a kept image),
JSON-LD `image` arrays. Bucket 2 (needs headless browser): JS-rendered
galleries, bot-stub pages.

## Schema evolution

To carry captions, `image_links` should become a list of objects:

```json
"image_links": [ { "url": "...", "caption": "...", "source": "gallery|figure|social" } ]
```

`source` distinguishes the reliable baseline (`social`) from recovered gallery
images. Update `build_dataset` and `dedupe_preserve_order` accordingly (dedupe
on `url`).

## Operational gotchas

- **GDELT rate-limits hard** (~1 req/5s; HTTP 429; also just slow, 20s+ per
  call, and will start timing out connections entirely once throttled). Make
  **one** search per run. `search_gdelt` has 429/timeout backoff (4 attempts).
  If you see repeated connect timeouts, you're throttled — **stop and wait
  minutes**, don't retry in a tight loop (that's what caused it).
- **Python environment:** trafilatura + `lxml_html_clean` are installed under
  **`/home/liu47/miniconda3/bin/python3`**. The shell's default `python3` may
  resolve to `newEnv` (`/data/add_disk0/liu47/envs/newEnv`), which does **not**
  have trafilatura. Use the miniconda interpreter (or install into whichever
  env actually runs the pipeline).
- **Cross-outlet duplicates:** the same wire story (e.g. AP/ANI) appears from
  multiple domains with different URLs; URL-dedup won't catch them. A
  normalized-title dedup would. (Not yet implemented.)

## Gallery extractor — as implemented

`extract_gallery_images(html, base_url)` in `news_scrape.py`. Purely additive:
called inside `extract_article` on the *same* HTML trafilatura already
downloaded (no second fetch), and only ever *adds* images trafilatura missed.

- **Candidates:** `<img>` inside any gallery-like container (class/id contains
  a `GALLERY_HINTS` word) + every `<figure>//img` (figures pair image+caption).
- **URL:** largest `srcset` candidate, else `src`; made absolute; `data:` URIs
  skipped. (`srcset` is static, so picking the high-res one is free quality.)
- **Junk filter (`_is_junk_image`):** drops icon/logo/sprite/tracking/pixel
  URLs, `.svg`, and images with a declared `width`/`height` < 200.
- **Boilerplate filter (`_is_boilerplate`) — the important one:** drops images
  in `<nav>/<aside>/<footer>/<header>`, and images wrapped in an `<a href>`
  that points to *another page* (a related-article / recirculation thumbnail).
  A link to an image *file* is treated as a lightbox/full-res link and kept.
  This is what separates the real article photo from cross-promo thumbnails —
  verified on smokymountainnews, where the real photo is a bare `<figure>` but
  the 4 "related" thumbnails are each `<a href=other-article><figure>`.
- **Output:** each record gains `gallery_images: [{url, caption}]`; the flat
  `image_links` list also absorbs any new gallery URLs (deduped). Caption comes
  from `<figcaption>`, else `alt`/`aria-label`/`title`.

Empirical note: on the current flood sources these static galleries are **rare**
— most articles are single-lead-image (Southeast Asia Post's 33 `<img>` were all
icons/related thumbnails; iHeart is JS-rendered). When a real gallery/figure
exists (Smoky Mountain) it's captured with its caption. This matches the earlier
finding that these sources mostly expose one lead image statically.

## Status / TODO

- [x] GDELT discovery + theme query (precision) + trafilatura text — working,
      validated end-to-end.
- [x] `clean_text` strips trafilatura's `![](url)` markers from `all_text`.
- [x] **Gallery/carousel/slideshow + caption extractor** — implemented,
      static-only, with junk + recirculation-thumbnail filtering. Adds a
      `gallery_images` field and folds new URLs into `image_links`.
- [ ] Sweep to quantify how many flood sources carry static galleries/captions
      (`scratchpad/gallery_sweep.py`) — **could not complete: GDELT was
      throttling/timing out.** Re-run when GDELT is responsive.
- [ ] (Optional, deferred) Playwright fallback for Mode-B (bot-stub / JS)
      pages. Left out on purpose per scope decision.
- [ ] (Optional) normalized-title dedup for cross-outlet wire duplicates.
- [ ] (Optional) migrate the flat `image_links` to `{url, caption, source}`
      objects if the caption-per-image pairing is wanted for *all* images, not
      just the gallery subset.
```


---

# Appendix: the original planning sketch

The first design note, written before the GDELT implementation. Kept for the
source shortlist and the extraction failure modes it anticipated.

## Sources to scrape: 
- GDELT (news + image API) 
- API for news: NewsAPI.org, GNews, Mediastack 
- RSS sites (AP News, Reuters, BBC) 
- The Guardian (open API) 
- local small newspaper sites which are easier to scrape 


## Extract News (per-site) 
- trafilatura + newspaper3k 


### Failure models of extraction 
- gallery, carousel view images are not captured 
    - don't worry about non-static (JS-rendering), lazily loaded images
- failed to extract the captions for each image 

## Dataset 
```  
{
        [
            {
                title: str, 
                date: str, 
                all_text: str,  
                image_links:[] 
            }

        ]
    }
```