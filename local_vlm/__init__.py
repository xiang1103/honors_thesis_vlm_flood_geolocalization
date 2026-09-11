"""Local GPU inference for the flood-image classifier.

    from local_vlm import LocalVLM, DEFAULT_MODEL_PATH

    vlm = LocalVLM(DEFAULT_MODEL_PATH)
    vlm.classify(url, prompt)     # -> {"status": "completed", "answer": "yes", ...}

Kept separate from scraping/ so the scraping pipeline stays free of
torch/CUDA imports: nothing here is loaded unless local inference is asked for.
"""
from .backend import DEFAULT_MAX_PIXELS, LocalVLM

#: Where download_model.py puts the weights by default.
DEFAULT_MODEL_PATH = "/home/liu47/models/Qwen3.8-27B"

__all__ = ["LocalVLM", "DEFAULT_MODEL_PATH", "DEFAULT_MAX_PIXELS"]
