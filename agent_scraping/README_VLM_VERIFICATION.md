# Qwen flood-image yes/no verification workflow

This guide describes the complete image-level verification pass after the news
articles have been scraped. It reads remote image URLs from the outlet JSON
files, asks a Hugging Face-hosted Qwen vision-language model whether each image
shows flooding, and records a normalized `yes` or `no` plus the model's raw
text.

The script does **not** download the images. The remote image URL is sent to the
inference provider so the provider can retrieve and inspect it.

## Workflow overview

```text
data/outlets/*_flood.json
        |
        v
agent_scraping/verify_images_vlm.py
        |
        +--> data/image_vlm_verification.jsonl
        |    append-only history; supports resuming
        |
        +--> data/image_vlm_verification_final.json
             clean canonical results used by the website
```

The default model is:

```text
Qwen/Qwen3.8-27B:novita
```

The classification prompt is defined in `verify_images_vlm.py`. It requires
visible standing or rising water to cover normally dry ground, roads, or
building foundations, with inundation as a primary foreground or midground
element. The requested response is `yes` or `no`.

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

The simplest option is to leave `HF_TOKEN` unset. The script will securely ask
for `HF token:` when it begins making API calls; the pasted text is not shown
on screen.

Alternatively, set it only for the current PowerShell window without writing
the token into the command history:

```powershell
$env:HF_TOKEN = Read-Host "HF token"
```

The token needs permission to call Hugging Face inference providers. API calls
may consume provider credits, depending on the account and provider.

## 4. Test exactly one image

Use separate test output files so the test does not modify the main history:

```powershell
python agent_scraping\verify_images_vlm.py `
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
python agent_scraping\verify_images_vlm.py --workers 4
```

The default paths are:

```text
Input:             data/outlets/*_flood.json
Resumable history: data/image_vlm_verification.jsonl
Canonical output:  data/image_vlm_verification_final.json
```

Four concurrent workers balance throughput and provider pressure. Reduce the
number if rate limits occur:

```powershell
python agent_scraping\verify_images_vlm.py --workers 1
```

The script automatically retries transient network errors and provider errors
such as HTTP 429 or 5xx responses. The default is three attempts per URL.

## 6. Resume an interrupted or limited run

Run the same command again:

```powershell
python agent_scraping\verify_images_vlm.py --workers 4
```

Before calling the model, the script reads the JSONL history. Completed image
occurrences are skipped, so an interruption does not require starting over.
Duplicate image URLs are also reused: the model is called once for the URL and
the answer is copied to every occurrence of that image.

Do not delete or manually truncate `image_vlm_verification.jsonl` while a run is
in progress.

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
| `occurrence_id` | Stable ID for this image occurrence in an article |
| `model` | Hugging Face model/provider identifier |
| `prompt` | Exact prompt used for classification |
| `status` | `completed`, `invalid_output`, or `error` in the history |
| `answer` | Normalized lowercase `yes` or `no` |
| `model_output` | Raw text returned by Qwen |
| `attempts` | Number of attempts needed for the model call |
| `reused_for_duplicate_url` | Whether an earlier classification was reused |
| `verified_at` | UTC timestamp for the result |

The append-only JSONL history can contain failed attempts or older records. The
canonical JSON includes only the latest valid completed result for each source
occurrence, in source order.

## Useful options

```text
--data-dir PATH       Input directory containing *_flood.json files
--output PATH         Append-only JSONL history path
--final-output PATH   Canonical JSON result path
--model MODEL         Hugging Face model/provider identifier
--workers N           Number of concurrent API calls; default 4
--limit N             Maximum distinct URLs called during this run
--retries N           Attempts for transient failures; default 3
```

Show the script's current options at any time:

```powershell
python agent_scraping\verify_images_vlm.py --help
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
