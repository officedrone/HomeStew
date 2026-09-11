"""Full-text search engine using SQLite FTS5 (trigram tokenizer) + Python re-rank.

The pdf_index table is tokenized with FTS5's ``trigram`` tokenizer, which
indexes every 3-character sequence. That gives us cheap *candidate*
retrieval with substring matching: a search for "processor" also finds
"Microprocessor". Raw trigram matches are noisy though — "RAM" would also
hit "program" or "framerate" — so every candidate row is re-verified and
scored in Python, mirroring how real search engines separate retrieval from
ranking:

1. **Retrieval (OR)**: candidates are pages matching *any* query term, not
   all of them. Requiring every term kills verbose LLM queries like
   "TPM Trusted Platform Module type version" — no single page contains
   all six words even though the TPM chapter is obviously relevant.
2. **Qualification** (word-boundary + length heuristic): short terms
   (<= SHORT_TERM_MAX_LEN chars, e.g. acronyms like "ram") only count when
   they start at a word boundary; longer terms may also match mid-word, so
   "processor" still finds "microprocessor".
3. **Weighted partial matching**: a page qualifies when the terms it does
   contain carry enough IDF weight (rare words like "tpm" count heavily,
   filler words like "type" barely at all). This lets partial matches
   through without letting common-word-only pages flood the results.
4. **Tiered ranking**: exact full phrase > all terms matched as whole words
   in close proximity > weighted partial coverage, with hit density and
   bm25 as tie-breakers.

Snippets are built in Python (not SQL) so each hit paragraph can be shown
separately with the matched keyword wrapped in <mark> tags.
"""
import logging
import html
import math
import re
from typing import NamedTuple, Optional, List, Dict, Tuple

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
CANDIDATE_LIMIT = 400

# A page qualifies when the IDF weight of the terms it contains reaches this
# fraction of the total query weight (see _plan_search), or when it contains
# at least one rare term (see RARE_DF_RATIO). 0.3 means e.g. a page holding
# most of the query's distinctive words surfaces even without an exact
# phrase, while pages matching only ubiquitous filler words do not.
MIN_WEIGHT_COVERAGE = 0.3

# Terms appearing in more than this fraction of indexed pages are treated as
# filler ("the", "computer", ...) and contribute no weight on their own: a
# page matching *only* such terms does not qualify.
COMMON_TERM_DF_RATIO = 0.6

# Terms appearing in at most this fraction of indexed pages are "rare"
# (acronyms like "tpm", spec words like "specifications"): one rare-term hit
# qualifies a page by itself, so a verbose query whose absent words inflate
# the total weight still surfaces the chapter that matches its key term.
RARE_DF_RATIO = 0.10

# Character classes used for word-boundary checks on lowercase text.
_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")

# Table-of-contents pages list every topic in the manual with dot leaders
# ("Features and specifications . . . . . 9"), so they contain almost any
# query's terms while holding no actual answers. When this fraction of a
# row's qualified hits is followed by a dot-leader run, the row is demoted
# one ranking tier.
TOC_LEADER_FRACTION = 0.5
_TOC_LEADER_RE = re.compile(r"\.\s*(?:\.\s*){2,}")


# Common English function/question words. They carry no topical signal in
# manuals ("what type of TPM does my laptop have") and, when absent from the
# index, would otherwise get maximum IDF and let noise pages qualify.
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "he", "her",
    "his", "how", "i", "if", "in", "is", "it", "its", "me", "my", "no",
    "not", "of", "on", "or", "our", "she", "so", "than", "that", "the",
    "their", "them", "then", "there", "they", "this", "to", "too", "was",
    "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your",
})


def _extract_terms(query: str) -> List[str]:
    """Pull the meaningful search terms out of a user query.

    Stopwords are dropped so natural-language questions don't dilute the
    weighted matching; if that removes everything (the query was all
    stopwords), the raw terms are kept so the search still does something.
    """
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", query)]
    filtered = [t for t in tokens if t not in STOPWORDS]
    return filtered or tokens


def _build_match_query(terms: List[str]) -> str:
    """Build an FTS5 MATCH expression that does substring matching.

    Each term is quoted as a phrase (the trigram tokenizer treats a quoted
    string as a substring match), and terms are OR-ed together so that a
    page matching any subset of the query becomes a candidate — final
    qualification happens in Python via weighted coverage (_score_row).
    """
    return ' OR '.join(f'"{t}"' for t in terms)


class SearchPlan(NamedTuple):
    """Per-query term statistics used for qualification and ranking."""

    weights: Dict[str, float]  # IDF weight per term (0.0 = filler word)
    rare: frozenset  # terms rare enough to qualify a page on their own


async def _plan_search(
    db, terms: List[str], device_id: Optional[int]
) -> SearchPlan:
    """Compute per-term IDF weights and the rare-term set for a query.

    ``df`` is the number of indexed pages containing the term (trigram
    phrase match), optionally scoped to one device; terms absent from the
    index get df=0. Weight is classic smooth IDF, ``log(1 + N / (1 + df))``,
    so rare terms ("tpm") dominate common ones ("type"). Terms occurring on
    more than COMMON_TERM_DF_RATIO of pages are zeroed out entirely — they
    are filler and must not qualify a page by themselves. Terms occurring on
    at most RARE_DF_RATIO of pages are marked *rare*: one rare-term hit
    qualifies a page even when the rest of the query is long-tail noise,
    because verbose LLM queries inflate total weight with words no manual
    ever uses ("laptop", "trusted").

    Returns:
        A SearchPlan. If *every* weight comes out zero (e.g. the whole query
        is filler words), weights fall back to 1.0 so at least one result
        set can still be produced.
    """
    where = "device_id = ?" if device_id is not None else None
    scope_params: list = [device_id] if device_id is not None else []

    total_sql = "SELECT COUNT(*) FROM pdf_index" + (f" WHERE {where}" if where else "")
    cursor = await db.execute(total_sql, scope_params)
    n_pages = (await cursor.fetchone())[0] or 1

    weights: Dict[str, float] = {}
    dfs: Dict[str, int] = {}
    for term in terms:
        if len(term) >= MIN_TRIGRAM_LEN:
            sql = "SELECT COUNT(*) FROM pdf_index WHERE pdf_index MATCH ?"
            params: list = [f'"{term}"']
        else:
            # Too short for the trigram index — count LIKE matches instead,
            # otherwise every short term would look absent (df=0).
            sql = "SELECT COUNT(*) FROM pdf_index WHERE lower(content) LIKE ?"
            params = [f"%{term}%"]
        if where:
            sql += f" AND {where}"
            params.extend(scope_params)
        cursor = await db.execute(sql, params)
        df = (await cursor.fetchone())[0]
        dfs[term] = df
        # Terms absent from the index get weight 0: no page can ever match
        # them, so they must not inflate the total query weight that partial
        # matches are measured against.
        weights[term] = math.log(1 + n_pages / (1 + df)) if df > 0 else 0.0

    cutoff = COMMON_TERM_DF_RATIO * n_pages
    weights = {t: (0.0 if dfs[t] > cutoff else w) for t, w in weights.items()}
    if all(w <= 0 for w in weights.values()):
        # The whole query is filler/absent words; without a fallback nothing
        # could ever qualify, so treat every term as equally important.
        weights = {t: 1.0 for t in terms}

    rare_cutoff = RARE_DF_RATIO * n_pages
    rare = frozenset(t for t, df in dfs.items() if 0 < df <= rare_cutoff)

    return SearchPlan(weights=weights, rare=rare)


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
    plan: SearchPlan,
    bm25: float,
) -> Optional[tuple]:
    """Rank key for a candidate row; lower sorts first. ``None`` = drop it.

    Partial matching: the row does *not* need every term — it qualifies
    when either (a) at least one matched term is rare (see SearchPlan.rare,
    e.g. "tpm"), or (b) the IDF weight of the terms with at least one
    qualified occurrence reaches MIN_WEIGHT_COVERAGE of the total query
    weight. A page containing only filler-weight terms ("type", "the")
    satisfies neither and is dropped.

    Tiers (see module docstring): 0 = exact phrase, 1 = all terms as whole
    words within a proximity window, 2 = weighted partial coverage. Within
    each tier, higher weight coverage sorts first, then hit density and
    bm25 break ties.
    """
    by_term: dict = {}
    for occ in occurrences:
        if occ["qualified"]:
            by_term.setdefault(occ["term"], []).append(occ)

    unique_terms = set(terms)
    total_weight = sum(plan.weights.get(t, 0.0) for t in unique_terms)
    matched_weight = sum(plan.weights.get(t, 0.0) for t in by_term)
    if not by_term:
        return None
    rare_hit = any(t in plan.rare for t in by_term)
    coverage = matched_weight / total_weight if total_weight > 0 else 1.0
    if not rare_hit and coverage < MIN_WEIGHT_COVERAGE:
        return None

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
            if all(whole_lists) and len(by_term) == len(unique_terms)
            else None
        )
        tier = 1 if span is not None and span <= _PROXIMITY_WINDOW + sum(len(t) for t in terms) else 2

    # Demote table-of-contents pages: they match nearly every query (every
    # topic is listed there) but only point at other pages, so citing them
    # sends the user to an unrelated page. One tier down keeps them as a
    # last resort instead of crowding out the real answer pages.
    if _toc_like(lowered, qualified):
        tier += 1

    return (tier, -coverage, density, bm25)


def _toc_like(text_lower: str, qualified: List[dict]) -> bool:
    """True when most hits sit on TOC-style dot-leader lines.

    A hit is considered TOC-like when a run of spaced dots (the leader that
    connects an entry to its page number) follows it within ~60 characters,
    e.g. "Power input through a USB-C port . . . . 17". Content pages use
    dot runs this way only rarely, so requiring a majority of hits keeps
    legitimate spec text unaffected.
    """
    if not qualified:
        return False
    leaders = sum(
        1
        for o in qualified
        if _TOC_LEADER_RE.search(text_lower[o["end"]:o["end"] + 60])
    )
    return leaders / len(qualified) >= TOC_LEADER_FRACTION


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


async def _fetch_candidates(
    db,
    trigram_terms: List[str],
    short_terms: List[str],
    device_id: Optional[int],
) -> Dict[int, dict]:
    """Pull candidate pages matching ANY query term (OR retrieval).

    Trigram-indexed terms go through one FTS5 MATCH query; terms shorter
    than a trigram can't use the index and are matched with LIKE instead.
    SQLite forbids combining ``MATCH`` and ``LIKE`` with OR inside a single
    WHERE clause, so short-term matches come from separate queries whose
    rows are merged (by rowid) into the trigram candidates. bm25 order is
    only a rough pre-filter — final ordering happens in Python — so each
    query pulls up to CANDIDATE_LIMIT rows.

    Returns:
        Mapping of rowid -> row dict, deduplicated across queries.
    """
    base_cols = "rowid, device_id, manual_id, filename, page_number, content"
    candidates: Dict[int, dict] = {}

    if trigram_terms:
        sql = f"""
            SELECT {base_cols}, bm25(pdf_index) AS rank
            FROM pdf_index
            WHERE pdf_index MATCH ?
        """
        params: list = [_build_match_query(trigram_terms)]
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += f" ORDER BY rank LIMIT {CANDIDATE_LIMIT}"
        cursor = await db.execute(sql, params)
        for row in await cursor.fetchall():
            candidates[row["rowid"]] = dict(row)

    # Terms too short for the trigram index are matched with LIKE. bm25()
    # cannot be used without a MATCH clause, so rank is left at 0 and only
    # the Python-side score matters for these rows.
    for t in short_terms:
        sql = f"""
            SELECT {base_cols}, 0 AS rank
            FROM pdf_index
            WHERE lower(content) LIKE ?
        """
        params = [f"%{t}%"]
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += f" LIMIT {CANDIDATE_LIMIT}"
        cursor = await db.execute(sql, params)
        for row in await cursor.fetchall():
            candidates.setdefault(row["rowid"], dict(row))

    return candidates


async def search_manuals(
    query: str,
    device_id: Optional[int] = None,
    limit: int = 10
) -> List[SearchResult]:
    """
    Search indexed PDF content with candidate retrieval + Python re-ranking.

    FTS5 (trigram) fetches pages matching *any* query term cheaply; each
    candidate is then verified and scored in Python so that:

    - short terms like "RAM" only match at word boundaries ("program" and
      "framerate" are dropped), while longer terms still match inside
      compound words ("processor" finds "Microprocessor");
    - partial matches qualify when the matched terms carry enough IDF
      weight, so "TPM Trusted Platform Module type version" surfaces TPM
      pages even though no page contains every word;
    - pages containing the exact phrase rank first, then pages where all
      terms appear as whole words close together, then weighted partial
      matches (rare-term hits outrank common-word-only hits).

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
    if not trigram_terms and not short_terms:
        return []

    try:
        async with get_db_context() as db:
            plan = await _plan_search(db, terms, device_id)
            candidates = await _fetch_candidates(
                db, trigram_terms, short_terms, device_id
            )

        # Verify + score every candidate row.
        scored_rows = []
        for row in candidates.values():
            # Collapse newlines/whitespace runs: PDFs use single \n for soft
            # wraps, and HTML renders the snippet as flowing text anyway.
            text = " ".join((row["content"] or "").split())
            occurrences = _find_term_occurrences(text, terms)
            rank_key = _score_row(
                text.lower(), occurrences, terms, plan, row["rank"]
            )
            if rank_key is None:
                continue  # weight coverage too low (e.g. only filler words)

            qualified = [o for o in occurrences if o["qualified"]]
            scored_rows.append((rank_key, row, text, qualified))

        scored_rows.sort(key=lambda item: item[0])

        results: List[SearchResult] = []
        for rank_key, row, text, qualified in scored_rows:
            snippets = _snippets_for_row(text, qualified)
            if not snippets:
                continue

            tier, neg_coverage, density, bm25_rank = rank_key
            score = float(
                -tier * 1000 + neg_coverage * 100 - density * 100 - bm25_rank
            )
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
            f"({len(candidates)} candidates) for: {query}"
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

    Uses the same OR retrieval and weighted partial-match qualification as
    search_manuals so the count reflects what users actually see (including
    partial matches), without noise like "ram" inside "program".
    """
    terms = _extract_terms(query)
    if not terms:
        return 0
    trigram_terms = [t for t in terms if len(t) >= MIN_TRIGRAM_LEN]
    short_terms = [t for t in terms if len(t) < MIN_TRIGRAM_LEN]
    if not trigram_terms and not short_terms:
        return 0

    try:
        async with get_db_context() as db:
            plan = await _plan_search(db, terms, device_id)
            candidates = await _fetch_candidates(
                db, trigram_terms, short_terms, device_id
            )

        count = 0
        for row in candidates.values():
            text = " ".join((row["content"] or "").split())
            occurrences = _find_term_occurrences(text, terms)
            if _score_row(
                text.lower(), occurrences, terms, plan, row["rank"]
            ) is not None:
                count += 1
        return count

    except Exception as e:
        logger.error(f"Search count failed: {e}")
        return 0
