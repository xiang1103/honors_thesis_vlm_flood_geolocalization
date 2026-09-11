# local_vlm

Local GPU inference for the image classifier. Kept out of `agent_scraping/` so
that pipeline never imports torch or touches CUDA — nothing here loads unless
local inference is actually requested.

```
download_model.py   one-time weight download to /home/liu47/models/
backend.py          LocalVLM: load once, fetch image, prompt, normalise answer
__init__.py         exports LocalVLM + DEFAULT_MODEL_PATH
```

## Use

```bash
python3 local_vlm/download_model.py          # once, ~56 GB
python3 agent_scraping/verify_images_vlm.py  # --backend local is the default
```

```python
from local_vlm import LocalVLM, DEFAULT_MODEL_PATH

vlm = LocalVLM(DEFAULT_MODEL_PATH, device_map="cuda:0")
vlm.classify(url, prompt)   # {"status": "completed", "answer": "yes", ...}
```

## Design

`classify()` returns the **same dict shape** as the hosted path
(`status` / `answer` / `model_output` / `attempts`, plus `error` on failure), so
the verifier's orchestration — occurrence flattening, URL dedupe, resume, JSONL
merge — is identical for either backend and needs no per-backend branching.

Loading is lazy and idempotent; the first `classify()` pays for it. Generation
is serialised behind a lock (one GPU, one forward pass at a time) while image
fetching stays outside the lock, so several workers overlap network I/O with
compute.

Rows produced here record `model` as `local:<dir name>`, so local answers stay
distinguishable from hosted ones in the results file.

`local_files_only=True` on both loads: a wrong `--model-path` fails loudly
instead of silently re-downloading 56 GB from the hub.

## Differences from the hosted backend

- **The image is fetched by us**, not by the provider. It is decoded in memory
  for one forward pass and never written to disk.
- **No credits, no 402.** There is no auth to fail, so nothing is ever marked
  fatal and a run cannot be halted by billing.
- **`max_pixels` caps vision tokens per image** (default 1280·28·28). News
  photos vary hugely in resolution and cost scales with pixel count.
