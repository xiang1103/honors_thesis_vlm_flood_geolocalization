# Qwen flood-image yes/no verification workflow

This guide describes the complete image-level verification pass after the news
articles have been scraped. It reads remote image URLs from the outlet JSON
files, asks a Hugging Face-hosted Qwen vision-language model whether each image
is a usable street-level photograph, and records a normalized `yes` or `no`
plus the model's raw text.

The script does **not** download the images. The remote image URL is sent to the
inference provider so the provider can retrieve and inspect it.

## Workflow overview

```text
data/outlets/*_flood.json
        |
        v
verification/verify_images_vlm.py
        |
        +--> data/image_vlm_verification.jsonl
        |    intermediate, written and flushed per result so a crash
        |    mid-run loses nothing; MERGED into the canonical JSON and
        |    DELETED when the run finishes (--keep-jsonl to retain it)
        |
        +--> data/image_vlm_verification_final.json
             canonical results used by the website, and the store of
             record a later run reads to resume
```

The default model is:

```text
Qwen/Qwen3.8-27B:novita
```

The classification prompt is defined in `verify_images_vlm.py`. It accepts any
ground-level photograph of a real place -- roads, vehicles, people, buildings,
storefronts, signs -- and rejects maps, radar and weather graphics, satellite
and aerial views, charts, diagrams, logos, screenshots, and bare portraits.
The requested response is `yes` or `no`.

**Water is not required.** Flood relevance is established upstream, at the
article level, by `verify_text.py` (`flood_score` / `flood_verified`), so every
image reaching this pass already comes from a flood story. A `yes` means the
photo carries the ground-level detail geolocation needs -- not that flooding
is visible in it.

Resume is **prompt-agnostic**: editing `PROMPT` (or passing a different
`--model`) does not re-classify images that already have an answer. Re-running
costs provider credits, and an existing answer is kept rather than paid for
twice.

So after an edit the canonical JSON holds answers from more than one prompt.
That is expected, and it is visible rather than hidden: every row records the
exact `prompt` and `model` behind it, the run prints how many rows predate the
current prompt, and the file's `summary` reports `on_current_prompt`,
`on_earlier_prompt`, and `distinct_prompts`.

To re-score everything under a new prompt, move the canonical JSON aside first
-- every occurrence then reads as pending and is classified fresh.

## 1. Prepare the scraped article files

The verifier expects one or more files matching:

```text
data/outlets/*_flood.json
```

Each file must contain a JSON array of articles. Every article may have an
`images` array whose items contain at least a remote `url`. For example:

```json
{
  "title": "Example flood article",
  "outlet": "cbs",
  "date": "2026-09-08T14:40:43Z",
  "url": "https://example.com/article",
  "images": [
    {
      "url": "https://example.com/news-image.jpg",
      "caption": "A road covered by floodwater",
      "source": "figure"
    }
  ]
}
```

The direct-outlet scraper and its options are documented in `README.md` in
this directory. The image-level verifier can also be run on already collected
outlet files; scraping does not have to be repeated.

For a new collection, activate the environment as shown below and run:

```powershell
python agent_scraping\scrape.py --outlets all --since-days 365 --limit 5000
```

This writes the per-outlet files into `data/outlets` without downloading the
images. If collection is interrupted, resume it with:

```powershell
python agent_scraping\scrape.py --outlets all --since-days 365 --limit 5000 --resume
```

The existing files in this project are already populated, so these scraping
commands are only needed when collecting or refreshing the source articles.

## 2. Activate the environment

Open **Anaconda PowerShell Prompt** or PowerShell:

```powershell
cd "C:\Users\paoca\OneDrive\Documents\SPELL\VLM-flooding\honors_thesis_vlm_flood_geolocalization-main"
conda activate codex
```

The verifier requires the `huggingface_hub` Python package. If it is not already
available in this environment, install it once:

```powershell
python -m pip install --upgrade huggingface_hub
```

## 3. Supply the Hugging Face token safely

Do not paste the token into Python code, a README, or a committed configuration
file.

The token is looked up in three places, in this order. The first one that has
it wins, and the token itself is never printed -- only where it came from.

1. **An exported `HF_TOKEN`.** Set it for the current PowerShell window only,
   without writing it into the command history:

   ```powershell
   $env:HF_TOKEN = Read-Host "HF token"
   ```

2. **A `.env` file** in the project root (gitignored). Copy `.env.example`:

   ```text
   HF_TOKEN=hf_your_token_here
   ```

   Use `--env-file PATH` to read a different one. A missing file is not an
   error. Variables already exported are never overwritten, so an explicit
   shell setting always beats a possibly stale file.

3. **An interactive prompt.** Leave `HF_TOKEN` unset and skip the `.env`; the
   script asks for `HF token:` when it begins making API calls, and the pasted
   text is not shown on screen.

The token needs permission to call Hugging Face inference providers. API calls
may consume provider credits, depending on the account and provider.

## 4. Test exactly one image

Use separate test output files so the test does not modify the main history:

```powershell
python verification\verify_images_vlm.py `
  --limit 1 `
  --workers 1 `
  --output data\image_vlm_verification_test.jsonl `
  --final-output data\image_vlm_verification_test_final.json
```

A successful terminal result includes `yes` or `no`, followed by a summary.
The test history should contain one completed record. The canonical test file
will also report the remaining source occurrences as missing, which is expected
because `--limit 1` deliberately stops after one distinct image URL.

Inspect the test answer in PowerShell:

```powershell
python -c "import json; p=json.load(open(r'data\image_vlm_verification_test_final.json', encoding='utf-8')); print(p['results'][0]['answer'], '-', p['results'][0]['model_output'])"
```

## 5. Run every image

After the one-image test succeeds, run the canonical pass:

```powershell
python verification\verify_images_vlm.py --workers 4
```

The default paths are:

```text
Input:              data/outlets/*_flood.json
Intermediate:       data/image_vlm_verification.jsonl  (deleted after merge)
Canonical output:   data/image_vlm_verification_final.json  (resumable)
```

Four concurrent workers balance throughput and provider pressure. Reduce the
number if rate limits occur:

```powershell
python verification\verify_images_vlm.py --workers 1
```

The script automatically retries transient network errors and provider errors
such as HTTP 429 or 5xx responses. The default is three attempts per URL.

## 6. Resume an interrupted or limited run

Run the same command again:

```powershell
python verification\verify_images_vlm.py --workers 4
```

Before calling the model, the script reads the canonical JSON, then the
intermediate JSONL if a previous run crashed before merging it. Completed image
occurrences are skipped, so an interruption does not require starting over.
Duplicate image URLs are also reused: the model is called once for the URL and
the answer is copied to every occurrence of that image.

`image_vlm_verification_final.json` is what makes a re-run cheap -- it is the
only record of which images have already been paid for. Do not delete it, and
do not delete or truncate `image_vlm_verification.jsonl` while a run is in
progress. Results for articles that have since left `data/outlets/` are carried
forward into the canonical JSON rather than dropped, and counted as
`carried_occurrences` in its summary.

## 7. Verify completion

At the end of every run, the terminal prints a canonical summary. It can also be
checked directly:

```powershell
python -c "import json; p=json.load(open(r'data\image_vlm_verification_final.json', encoding='utf-8')); print(p['summary'])"
```

A complete run has:

```text
missing_occurrences: 0
completed_occurrences: same value as source_occurrences
yes + no: same value as completed_occurrences
```

The completed dataset in this project currently reports:

```text
source_occurrences:    1624
completed_occurrences: 1624
missing_occurrences:   0
yes:                   395
no:                    1229
```

## 8. View and filter the answers

Start the separate results website:

```powershell
python image_vlm_review_server.py
```

Then open <http://127.0.0.1:8766/>. See
`../image_vlm_review_web/README.md` for all website controls and troubleshooting.

## Output fields

Each completed result preserves the article and image metadata and adds:

| Field | Meaning |
|---|---|
| `occurrence_id` | `sha256(article_url, image_url)[:24]`. Identity only -- not the image's position in the article, and not the caption, so a re-crawl that reorders images or rewrites a caption does not strand a paid-for result |
| `model` | Hugging Face model/provider identifier |
| `prompt` | Exact prompt used for classification |
| `status` | `completed`, `invalid_output`, or `error` in the history |
| `answer` | Normalized lowercase `yes` or `no` |
| `model_output` | Raw text returned by Qwen |
| `attempts` | Number of attempts needed for the model call |
| `reused_for_duplicate_url` | Whether an earlier classification was reused |
| `verified_at` | UTC timestamp for the result |

The intermediate JSONL can contain failed attempts or older records. The
canonical JSON includes only the latest valid completed result for each source
occurrence: those matching the current `data/outlets/` first, in source order,
then any carried over from earlier runs. Failed and invalid-output attempts are
never stored as completed, so the next run retries them.

## Useful options

```text
--data-dir PATH       Input directory containing *_flood.json files
--output PATH         Intermediate JSONL path (deleted after the merge)
--final-output PATH   Canonical JSON result path
--model MODEL         Hugging Face model/provider identifier
--workers N           Number of concurrent API calls; default 4
--limit N             Maximum distinct URLs called during this run
--retries N           Attempts for transient failures; default 3
--keep-jsonl          Keep the intermediate JSONL instead of deleting it
```

Show the script's current options at any time:

```powershell
python verification\verify_images_vlm.py --help
```

## Common problems

### HTTP 401 or 403

The token is invalid, expired, or lacks inference permission. The script treats
these as fatal and stops safely. Correct the token and rerun the same command;
completed records remain resumable.

### HTTP 402

The provider reports a billing or credit problem. Add/restore the required
provider credit, then rerun to resume.

### HTTP 429

The provider is rate-limiting requests. The script retries automatically. If it
continues, rerun with fewer workers.

### `invalid_output`

The response did not contain the word `yes` or `no` after all retries. That
occurrence is excluded from the canonical results until a later run obtains a
valid response. Rerunning resumes it automatically.

### A remote image URL fails

The outlet may block the inference provider, or the URL may have expired. The
error is recorded in the JSONL history. Fix the source URL if possible and run
again. The verifier includes a special HTTPS-origin normalization for NPR
Brightspot resize URLs while preserving the original scraped URL in the result.

## Ending the session

If `HF_TOKEN` was set in PowerShell, remove it when finished:

```powershell
Remove-Item Env:HF_TOKEN
```
