"""Throwaway diagnostic #2: per-engine status codes + result counts, with/without filetype:pdf."""
import logging
import time

logging.basicConfig(level=logging.INFO)

from ddgs.engines import ENGINES  # noqa: E402

QUERIES = {
    "filetype": "Schwinn IC4 manual filetype:pdf",
    "plain": "Schwinn IC4 manual",
}


def probe(name, cls, query):
    try:
        eng = cls(proxy=None, timeout=15, verify=True)
        res = eng.search(query, region="us-en", safesearch="moderate", timelimit=None, page=1)
        n = len(res) if res else 0
        pdfs = sum(1 for r in (res or []) if ".pdf" in (r.href or "").lower())
        return f"{name:12s} -> {n:3d} results ({pdfs} pdf)"
    except Exception as exc:
        return f"{name:12s} -> ERR {type(exc).__name__}: {str(exc)[:100]}"


for label, q in QUERIES.items():
    print(f"\n===== {label}: {q}")
    for name, cls in sorted(ENGINES["text"].items()):
        print(probe(name, cls, q), flush=True)
        time.sleep(2)
