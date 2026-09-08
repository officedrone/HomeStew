"""Full-text search engine using SQLite FTS5 (trigram tokenizer) + Python re-rank.

The pdf_index table is tokenized with FTS5's ``trigram`` tokenizer, which
indexes every 3-character sequence. That gives us cheap *candidate*
retrieval with substring matching: a search for "processor" also finds
"Microprocessor". Raw trigram matches are noisy though — "RAM" would also
hit "program" or "framerate" — so every candidate row is re-verified and
scored in Python, mirroring how real search engines separate retrieval from
ranking:

1. **Qualification** (word-boundary + length heuristic): short terms
   (<= SHORT_TERM_MAX_LEN chars, e.g. acronyms like "ram") only count when
   they start at a word boundary; longer terms may also match mid-word, so
   "processor" still finds "microprocessor".
2. **Tiered ranking**: exact full phrase > all terms matched as whole words
   in close proximity > qualified matches, with hit density and bm25 as
   tie-breakers.

Snippets are built in Python (not SQL) so each hit paragraph can be shown
separately with the matched keyword wrapped in <mark> tags.
"""
import logging
import html
import re
from typing import Optional, List, Tuple

from homebrain.db import get_db_context
from homebrain.models.schemas import SearchResult

logger = logging.getLogger(__name__)

# Words shorter than this cannot form a trigram token, so FTS5 can't match
# them via MATCH; they fall back to a LIKE scan instead.
MIN_TRIGRAM_LEN = 3

# Terms at or below this length are treated as acronyms/short words and must
# match at a word boundary (so "ram" hits "RAM module" but not "program").
SHORT_TERM_MAX_LEN = 4

# How many candidate rows to pull from FTS5 before Python re-ranking. The
# SQL bm25 order is only a rough pre-filter; final order comes from _score_row.
CANDIDATE_LIMIT = 200

# Character classes used for word-boundary checks on lowercase text.
_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")


def _extract_terms(query: str) -> List[str]:
    """Pull the individual search terms out of a user query."""
    return [t.lower() for t in re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", query)]


def _build_match_query(terms: List[str]) -> str:
    """Build an FTS5 MATCH expression that does substring matching.

    Each term is quoted as a phrase (the trigram tokenizer treats a quoted
    string as a substring match), and terms are AND-ed together.
    """
    return ' '.join(f'"{t}"' for t in terms)


def _is_word_boundary(text_lower: str, pos: int) -> bool:
    """True if ``pos`` is the start of a word (not inside an alphanumeric run)."""
    return pos == 0 or text_lower[pos - 1] not in _WORD_CHARS


def _find_term_occurrences(
    text: str, terms: List[str]
) -> List[dict]:
    """Find every case-insensitive occurrence of each term and classify it.

    Each occurrence is tagged with:
      - ``whole``: the match spans exactly one word ("RAM" in "the RAM,").
      - ``qualified``: usable as a hit. Short terms (<= SHORT_TERM_MAX_LEN)
        must start at a word boundary, which stops "ram" from matching
        inside "program"/"framerate". Longer terms always qualify, keeping
        compound-word hits like "processor" in "microprocessor".
    """
    lowered = text.lower()
    occurrences: List[dict] = []
    for term in terms:
        short = len(term) <= SHORT_TERM_MAX_LEN
        start = 0
        while True:
            idx = lowered.find(term, start)
            if idx == -1:
                break
            end = idx + len(term)
            boundary = _is_word_boundary(lowered, idx)
            whole = boundary and (end >= len(text) or not _is_word_char(lowered[end:end + 1]))
            occurrences.append({
                "start": idx,
                "end": end,
                "term": term,
                "whole": whole,
                "qualified": (boundary if short else True),
            })
            start = idx + 1
    return sorted(occurrences, key=lambda o: (o["start"], o["end"]))


def _is_word_char(ch: str) -> bool:
    return bool(ch) and ch in _WORD_CHARS


# How close (in characters) all query terms must sit to count as "together"
# for the whole-words proximity tier.
_PROXIMITY_WINDOW = 250


def _min_span(occ_lists: List[List[int]]) -> Optional[int]:
    """Length of the smallest character span containing one position per list."""
    merged: List[Tuple[int, int]] = sorted(
        (pos, i) for i, lst in enumerate(occ_lists) for pos in lst
    )
    have = 0
    counts = [0] * len(occ_lists)
    left = 0
    best: Optional[int] = None
    for right, (pos_r, list_r) in enumerate(merged):
        if counts[list_r] == 0:
            have += 1
        counts[list_r] += 1
        while have == len(occ_lists):
            pos_l, list_l = merged[left]
            span = pos_r - pos_l
            if best is None or span < best:
                best = span
            counts[list_l] -= 1
            if counts[list_l] == 0:
                have -= 1
            left += 1
    return best


def _phrase_present(text_lower: str, terms: List[str]) -> bool:
    """True if all query terms occur adjacently, in order, as whole words.

    Matches the normalized text (whitespace collapsed), so a multi-word
    phrase like "power adapter" is found across soft PDF line wraps too.
    """
    if not terms:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(t) for t in terms) + r"\b"
    return re.search(pattern, text_lower) is not None


def _score_row(
    lowered: str,
    occurrences: List[dict],
    terms: List[str],
    bm25: float,
) -> Optional[tuple]:
    """Rank key for a candidate row; lower sorts first. ``None`` = drop it.

    A row must have at least one *qualified* occurrence of every term.
    Tiers (see module docstring): 0 = exact phrase, 1 = all terms as whole
    words within a proximity window, 2 = qualified matches only. Hit density
    and bm25 break ties inside each tier.
    """
    by_term: dict = {}
    for occ in occurrences:
        if occ["qualified"]:
            by_term.setdefault(occ["term"], []).append(occ)

    if len(by_term) < len(set(terms)):
        return None  # some term only appears unqualified (e.g. "ram" inside "program")

    qualified = [o for lst in by_term.values() for o in lst]
    density = sum(o["end"] - o["start"] for o in qualified) / max(len(lowered), 1)

    if _phrase_present(lowered, terms):
        tier = 0
    else:
        whole_lists = [
            [o["start"] for o in lst if o["whole"]] for lst in by_term.values()
        ]
        span = (
            _min_span(whole_lists)
            if all(whole_lists) and len(by_term) == len(set(terms))
            else None
        )
        tier = 1 if span is not None and span <= _PROXIMITY_WINDOW + sum(len(t) for t in terms) else 2

    return (tier, density, bm25)


def _highlight(text: str, positions: List[tuple]) -> str:
    """Wrap the given character ranges of ``text`` in <mark> tags.

    Text outside the marks is HTML-escaped so PDF content can never inject
    markup into the frontend; only our own ``<mark>`` tags are emitted.
    """
    parts = []
    last = 0
    for start, end in positions:
        if start < last:  # overlapping matches from different terms
            continue
        parts.append(html.escape(text[last:start], quote=False))
        parts.append("<mark>")
        parts.append(html.escape(text[start:end], quote=False))
        parts.append("</mark>")
        last = end
    parts.append(html.escape(text[last:], quote=False))
    return "".join(parts)


def _snippets_for_row(
    text: str,
    occurrences: List[dict],
    max_snippets: int = 3,
    window_len: int = 280,
) -> List[str]:
    """Build one snippet per distinct hit location in an indexed chunk.

    ``text`` must already be whitespace-normalized and ``occurrences`` are
    the *qualified* matches found in it (see _find_term_occurrences), so we
    never highlight noise like "ram" inside "program". PDF text rarely has
    reliable paragraph breaks, so instead of guessing sections we emit a
    context window around each cluster of hits (up to ``max_snippets``).
    Each snippet shows the keyword highlighted inside its surrounding text.
    """
    positions = [(o["start"], o["end"]) for o in occurrences]
    if not positions:
        return []

    snippets: List[str] = []
    covered_until = -1
    for start, _end in positions:
        if len(snippets) >= max_snippets:
            break
        if start < covered_until:
            continue  # this hit is already visible inside a previous snippet

        win_start = max(0, start - window_len // 3)
        win_end = min(len(text), win_start + window_len)
        prefix = "..." if win_start > 0 else ""
        suffix = "..." if win_end < len(text) else ""
        clipped = text[win_start:win_end]
        clipped_positions = [
            (s - win_start, e - win_start)
            for s, e in positions
            if s >= win_start and e <= win_end
        ]
        snippets.append(prefix + _highlight(clipped, clipped_positions) + suffix)
        covered_until = win_end

    return snippets


async def search_manuals(
    query: str,
    device_id: Optional[int] = None,
    limit: int = 10
) -> List[SearchResult]:
    """
    Search indexed PDF content with candidate retrieval + Python re-ranking.

    FTS5 (trigram) fetches candidate pages cheaply; each candidate is then
    verified and scored in Python so that:

    - short terms like "RAM" only match at word boundaries ("program" and
      "framerate" are dropped), while longer terms still match inside
      compound words ("processor" finds "Microprocessor");
    - pages containing the exact phrase rank first, then pages where all
      terms appear as whole words close together, then other qualified
      matches (e.g. "power adapter" pages above "power"-only pages).

    Args:
        query: Search query.
        device_id: Optional filter by device ID
        limit: Maximum number of snippet results to return

    Returns:
        List of SearchResult objects, best matches first; a page with hits
        in multiple sections yields multiple results (one per section).
    """
    terms = _extract_terms(query)
    if not terms:
        return []

    trigram_terms = [t for t in terms if len(t) >= MIN_TRIGRAM_LEN]
    short_terms = [t for t in terms if len(t) < MIN_TRIGRAM_LEN]

    try:
        async with get_db_context() as db:
            where = []
            params: list = []

            if trigram_terms:
                where.append("pdf_index MATCH ?")
                params.append(_build_match_query(trigram_terms))

            # Terms too short for the trigram index are matched with LIKE.
            for t in short_terms:
                where.append("lower(content) LIKE ?")
                params.append(f"%{t}%")

            if device_id is not None:
                where.append("device_id = ?")
                params.append(device_id)

            # bm25 order is only a rough pre-filter; the real ordering is
            # computed in Python below, so pull a wide candidate pool.
            sql = f"""
                SELECT 
                    rowid,
                    device_id,
                    manual_id,
                    filename,
                    page_number,
                    content,
                    bm25(pdf_index) AS rank
                FROM pdf_index
                WHERE {' AND '.join(where)}
                ORDER BY rank
                LIMIT {CANDIDATE_LIMIT}
            """

            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()

        # Verify + score every candidate row.
        scored_rows = []
        for row in rows:
            # Collapse newlines/whitespace runs: PDFs use single \n for soft
            # wraps, and HTML renders the snippet as flowing text anyway.
            text = " ".join((row["content"] or "").split())
            occurrences = _find_term_occurrences(text, terms)
            rank_key = _score_row(text.lower(), occurrences, terms, row["rank"])
            if rank_key is None:
                continue  # only unqualified substring hits (e.g. "ram" in "program")

            qualified = [o for o in occurrences if o["qualified"]]
            scored_rows.append((rank_key, row, text, qualified))

        scored_rows.sort(key=lambda item: item[0])

        results: List[SearchResult] = []
        for rank_key, row, text, qualified in scored_rows:
            snippets = _snippets_for_row(text, qualified)
            if not snippets:
                continue

            tier, density, bm25_rank = rank_key
            score = float(-tier * 1000 - density * 100 - bm25_rank)
            for snippet in snippets:
                results.append(SearchResult(
                    manual_id=row["manual_id"] or 0,
                    device_id=row["device_id"],
                    filename=row["filename"],
                    page_number=row["page_number"],
                    snippet=snippet,
                    score=score,
                ))
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break

        logger.info(
            f"Search found {len(scored_rows)} qualifying pages "
            f"({len(rows)} candidates) for: {query}"
        )
        return results

    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []


async def count_indexed_documents() -> int:
    """Count total indexed documents."""
    async with get_db_context() as db:
        cursor = await db.execute("SELECT COUNT(*) as count FROM pdf_index")
        row = await cursor.fetchone()
        return row['count'] if row else 0


async def search_count(query: str, device_id: Optional[int] = None) -> int:
    """Get total count of matching pages for a query.

    Uses the same word-boundary qualification as search_manuals so the
    count doesn't include noise like "ram" inside "program".
    """
    terms = _extract_terms(query)
    if not terms:
        return 0
    trigram_terms = [t for t in terms if len(t) >= MIN_TRIGRAM_LEN]
    short_terms = [t for t in terms if len(t) < MIN_TRIGRAM_LEN]

    try:
        async with get_db_context() as db:
            where = []
            params: list = []
            if trigram_terms:
                where.append("pdf_index MATCH ?")
                params.append(_build_match_query(trigram_terms))
            for t in short_terms:
                where.append("lower(content) LIKE ?")
                params.append(f"%{t}%")

            if device_id is not None:
                where.append("device_id = ?")
                params.append(device_id)

            sql = f"""
                SELECT content, bm25(pdf_index) AS rank
                FROM pdf_index
                WHERE {' AND '.join(where)}
                ORDER BY rank
                LIMIT {CANDIDATE_LIMIT}
            """
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()

        count = 0
        for row in rows:
            text = " ".join((row["content"] or "").split())
            occurrences = _find_term_occurrences(text, terms)
            if _score_row(text.lower(), occurrences, terms, row["rank"]) is not None:
                count += 1
        return count

    except Exception as e:
        logger.error(f"Search count failed: {e}")
        return 0
