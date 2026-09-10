# VLM flood-image results website

This is the second, separate image-review website. It displays the completed
Qwen `yes`/`no` classifications from:

```text
data/image_vlm_verification_final.json
```

It does not alter the result file and does not download the news images. The
browser requests each image directly from its original remote URL.

## Start the website

Open **Anaconda PowerShell Prompt** or PowerShell, then run:

```powershell
cd "C:\Users\paoca\OneDrive\Documents\SPELL\VLM-flooding\honors_thesis_vlm_flood_geolocalization-main"
conda activate codex
python image_vlm_review_server.py
```

When the terminal reports that the server is ready, open:

<http://127.0.0.1:8766/>

Keep the terminal open while using the website. Press `Ctrl+C` in that terminal
to stop the server.

The website server uses only Python's standard library, so it does not require
additional packages.

## What the controls do

- **All / Yes / No** filters the cards by Qwen's normalized answer.
- **Search** checks the article title, image caption, outlet, and raw model
  response.
- **Outlet** shows results from one news organization.
- **Article check** filters using the earlier article-level flood-verification
  field. This is separate from the image-level Qwen answer.
- **Sort** orders results by article date or outlet.
- Selecting an image opens a larger view with its raw Qwen response, metadata,
  article link, and original image link.

The default result set currently contains 1,624 image occurrences: 395 `yes`
and 1,229 `no`.

## Other launch options

If port 8766 is already occupied, choose another port:

```powershell
python image_vlm_review_server.py --port 8767
```

Then open <http://127.0.0.1:8767/>.

To display another canonical result file:

```powershell
python image_vlm_review_server.py --results-file "C:\path\to\another_final.json"
```

The file must have the same structure produced by
`agent_scraping/verify_images_vlm.py`.

## Files used by the website

```text
image_vlm_review_server.py       Python web server and result loader
image_vlm_review_web/
  index.html                     Page structure
  styles.css                     Page appearance and responsive layout
  app.js                         Filters, pagination, cards, and image viewer
data/
  image_vlm_verification_final.json
                                 Canonical Qwen results displayed by the site
```

These files are independent of the original website on port 8765.

## Troubleshooting

### The page does not open

Confirm that the terminal is still running and shows the URL. If the port is
busy, use the alternate-port command above.

### The page says the result file is missing

Run the verification workflow described in
`../agent_scraping/README_VLM_VERIFICATION.md`, or pass the path to an existing
canonical result file with `--results-file`.

### A particular image does not appear

Some outlets block embedded images or let old image URLs expire. Open the card
and use **Open original image**. A failed remote image does not mean its stored
classification or metadata was lost.

