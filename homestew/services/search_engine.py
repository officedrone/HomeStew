"""Full-text search engine using SQLite FTS5 (trigram tokenizer) + Python re-rank.

The pdf_index table is tokenized with FTS5's ``trigram`` tokenizer, which
indexes every 3-character sequence. That gives us cheap *candidate*
retrieval with substring matching: a search for "processor" also finds
"Microprocessor". Raw trigram matches are noisy though - "RAM" would also
hit "program" or "framerate" - so every candidate row is re-verified and
scored in Python, mirroring how real search engines separate retrieval from
ranking:

1. **Retrieval (OR)**: candidates are pages matching *any* query term, not
   all of them. Requiring every term kills verbose LLM queries like
   "TPM Trusted Platform Module type version" - no single page contains
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

Two query features sit on top of that pipeline:

- **Explicit phrases**: text in double quotes ("memory speed") must appear
  verbatim (whole words, adjacent). Phrase groups are OR-ed with the loose
  terms for retrieval - like mainstream search engines - and a page holding
  an explicit phrase always ranks in the top tier.
- **Device entries**: when no device filter is set, each device's own
  record (name, brand, model, description, serial/product numbers and
  custom attributes) is scored with the same qualification + tiering logic
  as manual pages, so "How much RAM does my laptop support" surfaces a
  device whose entry says "RAM: 64GB" even when no manual page matches.

Snippets are built in Python (not SQL) so each hit paragraph can be shown
separately with the matched keyword wrapped in <mark> tags.
"""
import logging
import html
import math
import re
from typing import NamedTuple, Optional, List, Dict, Tuple

from homestew.db import get_db_context
from homestew.models.schemas import SearchResult

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


_PHRASE_RE = re.compile(r'"([^"]+)"')


def _tokenize(text: str) -> List[str]:
    """Lowercase word tokens of a text, keeping hyphenated words intact."""
    return [t.lower() for t in re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", text)]


class ParsedQuery(NamedTuple):
    """Explicit quoted phrases plus the loose (unquoted) terms of a query."""

    phrases: List[List[str]]  # each explicit "quoted phrase" as its word list
    loose: List[str]          # unquoted terms with stopwords dropped
    terms: List[str]          # every unique term (loose + phrase words)


def _parse_query(query: str) -> ParsedQuery:
    """Split a query into explicit quoted phrases and loose terms.

    Text in double quotes is an explicit phrase that must appear verbatim,
    like on mainstream search engines ('"memory speed" upgrade'). Phrase
    groups are OR-ed with the loose terms for retrieval (a page holding the
    phrase qualifies even without the other words, and vice versa), but a
    page containing the phrase always ranks in the top tier. Stopwords are
    dropped from the loose terms only - inside quotes every word counts.
    If stopword filtering removes everything, the raw tokens are kept so
    the search still does something.
    """
    phrases: List[List[str]] = []

    def _grab(match: re.Match) -> str:
        words = _tokenize(match.group(1))
        if words:
            phrases.append(words)
        return " "  # keep the remainder tokenizable at the same positions

    remainder = _PHRASE_RE.sub(_grab, query)
    loose_tokens = _tokenize(remainder)
    loose = [t for t in loose_tokens if t not in STOPWORDS] or loose_tokens

    terms: List[str] = []
    seen: set = set()
    for t in loose + [w for ph in phrases for w in ph]:
        if t not in seen:
            seen.add(t)
            terms.append(t)
    return ParsedQuery(phrases=phrases, loose=loose, terms=terms)


def _phrase_strings(phrases: List[List[str]]) -> Tuple[List[str], List[str]]:
    """Split joined phrase strings into trigram-indexable and LIKE-only ones.

    A quoted phrase is matched as a verbatim substring by the trigram
    tokenizer, but phrases shorter than one trigram cannot go through MATCH
    and need a LIKE fallback instead (same rule as single short terms).
    """
    joined = [" ".join(ph) for ph in phrases]
    indexable = [p for p in joined if len(p) >= MIN_TRIGRAM_LEN]
    like_only = [p for p in joined if len(p) < MIN_TRIGRAM_LEN]
    return indexable, like_only


def _build_match_query(terms: List[str], phrases: List[List[str]]) -> str:
    """Build an FTS5 MATCH expression that does substring matching.

    Each term is quoted as a phrase (the trigram tokenizer treats a quoted
    string as a substring match) and so is every explicit query phrase, all
    OR-ed together so that a page matching any subset of the query becomes
    a candidate - final qualification happens in Python (_score_row).
    """
    indexable_phrases, _ = _phrase_strings(phrases)
    parts = [f'"{t}"' for t in terms]
    parts += [f'"{p}"' for p in indexable_phrases]
    return ' OR '.join(parts)


class SearchPlan(NamedTuple):
    """Per-query term statistics used for qualification and ranking."""

    weights: Dict[str, float]  # IDF weight per term (0.0 = filler word)
    rare: frozenset  # terms rare enough to qualify a page on their own


async def _plan_search(
    db,
    terms: List[str],
    device_id: Optional[int],
    extra_docs: Optional[List[str]] = None,
) -> SearchPlan:
    """Compute per-term IDF weights and the rare-term set for a query.

    ``df`` is the number of indexed pages containing the term (trigram
    phrase match), optionally scoped to one device; terms absent from the
    index get df=0. Weight is classic smooth IDF, ``log(1 + N / (1 + df))``,
    so rare terms ("tpm") dominate common ones ("type"). Terms occurring on
    more than COMMON_TERM_DF_RATIO of pages are zeroed out entirely - they
    are filler and must not qualify a page by themselves. Terms occurring on
    at most RARE_DF_RATIO of pages are marked *rare*: one rare-term hit
    qualifies a page even when the rest of the query is long-tail noise,
    because verbose LLM queries inflate total weight with words no manual
    ever uses ("laptop", "trusted").

    ``extra_docs`` is the list of lowercased device-entry texts (see
    _fetch_device_entries) that participate in the search when no device
    filter is set. They count as documents in the corpus so a term that
    only ever appears in a user's own device records still gets a real IDF
    instead of weight 0, which would make such pages unqualifiable.

    Returns:
        A SearchPlan. If *every* weight comes out zero (e.g. the whole query
        is filler words), weights fall back to 1.0 so at least one result
        set can still be produced.
    """
    where = "device_id = ?" if device_id is not None else None
    scope_params: list = [device_id] if device_id is not None else []
    extra_docs = extra_docs or []

    total_sql = "SELECT COUNT(*) FROM pdf_index" + (f" WHERE {where}" if where else "")
    cursor = await db.execute(total_sql, scope_params)
    n_pages = ((await cursor.fetchone())[0] or 1) + len(extra_docs)

    weights: Dict[str, float] = {}
    dfs: Dict[str, int] = {}
    for term in terms:
        if len(term) >= MIN_TRIGRAM_LEN:
            sql = "SELECT COUNT(*) FROM pdf_index WHERE pdf_index MATCH ?"
            params: list = [f'"{term}"']
        else:
            # Too short for the trigram index - count LIKE matches instead,
            # otherwise every short term would look absent (df=0).
            sql = "SELECT COUNT(*) FROM pdf_index WHERE lower(content) LIKE ?"
            params = [f"%{term}%"]
        if where:
            sql += f" AND {where}"
            params.extend(scope_params)
        cursor = await db.execute(sql, params)
        df = (await cursor.fetchone())[0]
        # Device entries count as documents containing the term too.
        df += sum(1 for doc in extra_docs if term in doc)
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
    phrases: Optional[List[List[str]]] = None,
    loose_terms: Optional[List[str]] = None,
) -> Optional[tuple]:
    """Rank key for a candidate row; lower sorts first. ``None`` = drop it.

    Explicit quoted phrases (``phrases``) dominate: a row containing one
    qualifies outright and lands in the top tier - like mainstream search
    engines, the phrase group is OR-ed with the loose terms, so a page with
    only the loose words can still surface (lower-ranked). Without an
    explicit-phrase hit the row must qualify through the loose terms:
    either (a) at least one matched term is rare (see SearchPlan.rare,
    e.g. "tpm"), or (b) the IDF weight of the qualified loose-term matches
    reaches MIN_WEIGHT_COVERAGE of the total *loose* query weight. A page
    containing only filler-weight terms ("type", "the") satisfies neither
    and is dropped.

    Tiers (see module docstring): 0 = explicit phrase hit, or - when the
    query has none - all terms adjacent as an exact phrase; 1 = all terms
    as whole words within a proximity window; 2 = weighted partial
    coverage. Within each tier, higher weight coverage sorts first, then
    hit density and bm25 break ties.

    ``loose_terms`` is the unquoted part of the query that coverage is
    measured against (defaults to all *terms* for un-quoted queries).
    """
    phrases = phrases or []
    loose_terms = terms if loose_terms is None else loose_terms

    by_term: dict = {}
    for occ in occurrences:
        if occ["qualified"]:
            by_term.setdefault(occ["term"], []).append(occ)

    # An explicit phrase hit qualifies on its own; coverage otherwise only
    # considers the loose terms so phrase words don't inflate the bar.
    phrase_hit = any(_phrase_present(lowered, ph) for ph in phrases)

    # When the query is *only* quoted phrases there are no loose terms to
    # measure against; fall back to all terms so pages with a partial
    # phrase-word match can still surface when nothing holds the exact
    # phrase (graceful degradation, like "showing results for ...").
    weight_terms = set(loose_terms) if phrases and loose_terms else set(terms)
    total_weight = sum(plan.weights.get(t, 0.0) for t in weight_terms)
    matched_weight = sum(plan.weights.get(t, 0.0) for t in by_term if t in weight_terms)
    coverage = matched_weight / total_weight if total_weight > 0 else 1.0
    if not phrase_hit:
        if not by_term:
            return None
        rare_hit = any(t in plan.rare for t in by_term if t in weight_terms)
        # OR semantics (explicit-phrase group | rest of words): a page
        # holding only loose words still qualifies, but at least one
        # matched term must carry real IDF weight - pages matching *only*
        # filler or absent words are noise, not results.
        if not rare_hit and (matched_weight <= 0 or coverage < MIN_WEIGHT_COVERAGE):
            return None

    qualified = [o for lst in by_term.values() for o in lst]
    density = sum(o["end"] - o["start"] for o in qualified) / max(len(lowered), 1)

    # Coverage used as the within-tier sort key; phrase hits get full marks.
    coverage_key = 1.0 if phrase_hit else coverage

    if phrase_hit or (not phrases and _phrase_present(lowered, terms)):
        tier = 0
    else:
        prox_terms = weight_terms if phrases else set(terms)
        # Tier 1 needs a whole-word occurrence of every proximity term.
        whole_lists = [
            [o["start"] for o in by_term[t] if o["whole"]]
            for t in prox_terms
            if t in by_term
        ] if len(prox_terms & set(by_term)) == len(prox_terms) else []
        span = _min_span(whole_lists) if whole_lists else None
        tier = 1 if span is not None and span <= _PROXIMITY_WINDOW + sum(len(t) for t in prox_terms) else 2

    # Demote table-of-contents pages: they match nearly every query (every
    # topic is listed there) but only point at other pages, so citing them
    # sends the user to an unrelated page. One tier down keeps them as a
    # last resort instead of crowding out the real answer pages.
    if _toc_like(lowered, qualified):
        tier += 1

    return (tier, -coverage_key, density, bm25)


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
    phrases: Optional[List[List[str]]] = None,
) -> Dict[int, dict]:
    """Pull candidate pages matching ANY query term or phrase (OR retrieval).

    Trigram-indexed terms go through one FTS5 MATCH query; terms shorter
    than a trigram can't use the index and are matched with LIKE instead.
    SQLite forbids combining ``MATCH`` and ``LIKE`` with OR inside a single
    WHERE clause, so short-term matches come from separate queries whose
    rows are merged (by rowid) into the trigram candidates. bm25 order is
    only a rough pre-filter - final ordering happens in Python - so each
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
        params: list = [_build_match_query(trigram_terms, phrases or [])]
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
    _, like_phrases = _phrase_strings(phrases or [])
    for needle in short_terms + like_phrases:
        sql = f"""
            SELECT {base_cols}, 0 AS rank
            FROM pdf_index
            WHERE lower(content) LIKE ?
        """
        params = [f"%{needle}%"]
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += f" LIMIT {CANDIDATE_LIMIT}"
        cursor = await db.execute(sql, params)
        for row in await cursor.fetchall():
            candidates.setdefault(row["rowid"], dict(row))

    return candidates


async def _fetch_device_entries(db, device_id: Optional[int] = None) -> List[dict]:
    """Build one searchable document per device from its own record.

    A device's name, brand, model, description, serial/product numbers and
    custom attributes are joined into a single text - the same fields the
    chat UI already exposes as the "device entry". Users type real specs
    into these fields (e.g. an attribute "RAM: 64GB max"), so when no
    device filter is set they must be searchable alongside the manuals:
    "How much RAM does my laptop support" should surface the device whose
    entry answers it even when no manual page matches.
    """
    dev_sql = (
        "SELECT id, name, brand, model, description, serial_number, "
        "product_number FROM devices"
    )
    dev_params: list = []
    if device_id is not None:
        dev_sql += " WHERE id = ?"
        dev_params.append(device_id)
    cursor = await db.execute(dev_sql + " ORDER BY id", dev_params)
    devices = [dict(r) for r in await cursor.fetchall()]

    attr_sql = (
        "SELECT device_id, attribute_name, attribute_value "
        "FROM device_attributes"
    )
    attr_params: list = []
    if device_id is not None:
        attr_sql += " WHERE device_id = ?"
        attr_params.append(device_id)
    attr_cursor = await db.execute(attr_sql + " ORDER BY id", attr_params)
    attrs_by_device: Dict[int, List[str]] = {}
    for a in await attr_cursor.fetchall():
        attrs_by_device.setdefault(a["device_id"], []).append(
            f"{a['attribute_name']}: {a['attribute_value']}"
        )

    entries: List[dict] = []
    for dev in devices:
        parts = [dev.get("name") or "", dev.get("brand") or "", dev.get("model") or ""]
        if dev.get("description"):
            parts.append(dev["description"])
        if dev.get("serial_number"):
            parts.append(f"Serial number: {dev['serial_number']}")
        if dev.get("product_number"):
            parts.append(f"Product number: {dev['product_number']}")
        parts += attrs_by_device.get(dev["id"], [])
        text = " ".join(" ".join(p.split()) for p in parts if p)
        if text.strip():
            entries.append({"device": dev, "text": text})
    return entries


def _entry_result(entry: dict, rank_key: tuple) -> SearchResult:
    """Turn a qualifying device entry into a SearchResult.

    ``manual_id=0`` / ``page_number=0`` mark it as a device (not manual page)
    hit; the frontend renders those as a device card instead of a PDF link.
    The score uses the same formula as manual rows, offset by -50 so an
    equally-good device entry reports slightly below a manual page - manuals
    are the authoritative source, but an exact spec in the user's own entry
    still beats a weak partial match (tier dominates the offset).
    """
    tier, neg_coverage, density, _bm25 = rank_key
    score = float(-tier * 1000 + neg_coverage * 100 - density * 100) - 50.0
    dev = entry["device"]
    label = " \u2014 ".join(p for p in (dev.get("brand"), dev.get("model")) if p)
    return SearchResult(
        manual_id=0,
        device_id=dev["id"],
        filename=f"{dev['name']} ({label})" if label else dev["name"],
        page_number=0,
        snippet=" \u2026 ".join(_snippets_for_row(entry["text"], entry["qualified"])),
        score=score,
    )


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

    Explicit "quoted phrases" in the query must appear verbatim and rank
    first; the rest of the words are OR-ed with them (see module docstring).
    When no device filter is set, every device's own record (details +
    custom attributes) is searched alongside the manuals so cross-device
    questions like "How much RAM does my laptop support" surface the right
    device even when no manual page matches.

    Args:
        query: Search query.
        device_id: Optional filter by device ID
        limit: Maximum number of snippet results to return

    Returns:
        List of SearchResult objects, best matches first; a page with hits
        in multiple sections yields multiple results (one per section).
        Device-entry hits use manual_id=0 and page_number=0.
    """
    parsed = _parse_query(query)
    terms = parsed.terms
    if not terms:
        return []

    trigram_terms = [t for t in terms if len(t) >= MIN_TRIGRAM_LEN]
    short_terms = [t for t in terms if len(t) < MIN_TRIGRAM_LEN]
    if not trigram_terms and not short_terms:
        return []

    try:
        async with get_db_context() as db:
            # Device entries always join; when a filter is set they are
            # scoped to it, so custom attributes stay searchable per device.
            entries = await _fetch_device_entries(db, device_id)
            plan = await _plan_search(
                db, terms, device_id,
                extra_docs=[e["text"].lower() for e in entries],
            )
            candidates = await _fetch_candidates(
                db, trigram_terms, short_terms, device_id, parsed.phrases
            )

        # Verify + score every candidate manual row.
        scored_rows: List[tuple] = []  # (rank_key, "row", row, text, qualified)
        for row in candidates.values():
            # Collapse newlines/whitespace runs: PDFs use single \n for soft
            # wraps, and HTML renders the snippet as flowing text anyway.
            text = " ".join((row["content"] or "").split())
            occurrences = _find_term_occurrences(text, terms)
            rank_key = _score_row(
                text.lower(), occurrences, terms, plan, row["rank"],
                phrases=parsed.phrases, loose_terms=parsed.loose,
            )
            if rank_key is None:
                continue  # weight coverage too low (e.g. only filler words)

            qualified = [o for o in occurrences if o["qualified"]]
            scored_rows.append((rank_key, "row", row, text, qualified))

        # Score the device entries with the same qualification + tiering. A
        # device's record is short, so a hit there is dense by definition;
        # zero the density term to keep entries comparable with page rows.
        for entry in entries:
            text = entry["text"]
            occurrences = _find_term_occurrences(text, terms)
            rank_key = _score_row(
                text.lower(), occurrences, terms, plan, 0.0,
                phrases=parsed.phrases, loose_terms=parsed.loose,
            )
            if rank_key is None:
                continue
            tier, neg_cov, _density, bm25 = rank_key
            entry["qualified"] = [o for o in occurrences if o["qualified"]]
            scored_rows.append(((tier, neg_cov, 0.0, bm25), "entry", entry))

        scored_rows.sort(key=lambda item: item[0])

        results: List[SearchResult] = []
        for rank_key, kind, *rest in scored_rows:
            tier, neg_coverage, density, bm25_rank = rank_key
            if kind == "entry":
                result = _entry_result(rest[0], rank_key)
                if not result.snippet:
                    continue
                results.append(result)
                if len(results) >= limit:
                    break
                continue

            row, text, qualified = rest
            snippets = _snippets_for_row(text, qualified)
            if not snippets:
                continue

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

    Uses the same OR retrieval, explicit-phrase handling and weighted
    partial-match qualification as search_manuals so the count reflects what
    users actually see (including partial matches), without noise like "ram"
    inside "program". Device entries are not counted (they are not pages).
    """
    parsed = _parse_query(query)
    terms = parsed.terms
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
                db, trigram_terms, short_terms, device_id, parsed.phrases
            )

        count = 0
        for row in candidates.values():
            text = " ".join((row["content"] or "").split())
            occurrences = _find_term_occurrences(text, terms)
            if _score_row(
                text.lower(), occurrences, terms, plan, row["rank"],
                phrases=parsed.phrases, loose_terms=parsed.loose,
            ) is not None:
                count += 1
        return count

    except Exception as e:
        logger.error(f"Search count failed: {e}")
        return 0
