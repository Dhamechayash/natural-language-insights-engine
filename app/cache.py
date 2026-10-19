"""Question cache: keys and savings.

Datasets are immutable (every upload gets a new id), so a cached answer never goes stale for its dataset.
The key also covers the model and PROMPT_VERSION, so changing either starts a fresh cache.
"""

import hashlib
import json
import re

# Bump when SQL_SYSTEM, NARRATE_SYSTEM or the tool schemas change in a way that alters answers.
PROMPT_VERSION = "1"

# USD per million tokens (input, output), Claude API list prices. Unknown models report no cost.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}


def normalize_question(question: str) -> str:
    """Case, repeated whitespace and trailing punctuation do not change the answer."""
    return re.sub(r"\s+", " ", question).strip().rstrip("?.!").strip().lower()


def cache_key(dataset_id: str, question: str, history: list[dict], model: str) -> str:
    # A follow-up like "and by month?" means different things in different threads, so the earlier turns are part of the key.
    context = [{"q": normalize_question(t["question"]), "sql": t.get("sql"), "refused": t.get("refused")} for t in history]
    raw = json.dumps([dataset_id, normalize_question(question), context, model, PROMPT_VERSION], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def savings(usage: dict, model: str) -> dict:
    """What re-running the original question would have cost."""
    tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    price = PRICES.get(model)
    cost = round((tin * price[0] + tout * price[1]) / 1e6, 6) if price else None
    return {"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout, "cost_usd": cost, "model": model}
