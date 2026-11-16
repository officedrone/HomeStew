"""Throwaway diagnostic: probe each ddgs text engine individually."""
import time
import traceback

from ddgs.engines import ENGINES

QUERY = "Schwinn IC4 manual filetype:pdf"


def probe(name, cls, query=QUERY):
    try:
        eng = cls(proxy=None, timeout=15, verify=True)
        t0 = time.time()
        res = eng.search(query, region="us-en", safesearch="moderate", timelimit=None, page=1)
        dt = time.time() - t0
        return f"{name:12s} OK   {len(res):3d} result(s) in {dt:.1f}s  first={getattr(res[0], 'href', '')[:90] if res else '-'}"
    except Exception as exc:
        return f"{name:12s} ERR  {type(exc).__name__}: {str(exc)[:160]}"


print("=== text engines ===")
for name, cls in sorted(ENGINES["text"].items()):
    print(probe(name, cls))
    time.sleep(1)
