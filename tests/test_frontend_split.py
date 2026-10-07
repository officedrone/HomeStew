"""Guards for the realm-split frontend (frontend/js/**).

The app loads as classic scripts sharing one global lexical scope, and both
index.html and JS-generated HTML strings reference top-level functions BY NAME
through inline on*="..." attributes. A function lost in a refactor fails
SILENTLY at runtime (the button just does nothing), so this test statically
verifies the wiring that the browser would otherwise only check by clicking:

1. every inline event-handler name resolves to a top-level function
   declaration somewhere under frontend/js/;
2. no two files declare the same top-level function or let/const name (with
   classic scripts the last file loaded silently wins - functions - or throws
   at load - let/const);
3. every <script src="/static/..."> in index.html exists on disk, and
   bootstrap.js is loaded LAST so its DOMContentLoaded listener runs after all
   realm files have been evaluated;
4. wireAll() only calls wiring functions that actually exist;
5. frontend/lucide-icon-names.js stays at its path (test_device_tool.py
   hardcodes it) and the old monolithic app.js is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
JS_DIR = FRONTEND / "js"
INDEX_HTML = FRONTEND / "index.html"

# Top-level function declaration: column-0 (classic scripts, no nesting).
FUNC_DECL = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)
# Column-0 let/const = top-level lexical declarations (shared global scope).
LEXICAL_DECL = re.compile(r"^(?:let|const)\s+([A-Za-z_$][\w$]*)", re.M)
# Inline handlers in HTML attributes: onclick="...", onchange="...", etc.
INLINE_ATTR = re.compile(
    r"""\bon(?:click|change|input|keydown|keyup|keypress|submit|focus|blur)\s*=\s*"([^"]*)\""""
)
# A call expression inside an attribute body, not a member access (x.fn()).
CALL_IN_ATTR = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
SCRIPT_SRC = re.compile(r"""<script\s+src="([^"]+)\"""")

# Names that are legal inside an inline attribute but not app declarations
# (browser globals used directly in generated markup).
ALLOWED_GLOBALS = {"event", "parseInt", "parseFloat", "Number", "String", "Boolean", "isNaN"}


def _js_files() -> list[Path]:
    files = sorted(JS_DIR.rglob("*.js"))
    assert files, f"no js files under {JS_DIR}"
    return files


def _declarations() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    funcs: dict[str, list[str]] = {}
    lexicals: dict[str, list[str]] = {}
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(FRONTEND))
        for name in FUNC_DECL.findall(text):
            funcs.setdefault(name, []).append(rel)
        for name in LEXICAL_DECL.findall(text):
            lexicals.setdefault(name, []).append(rel)
    return funcs, lexicals


def test_old_monolith_is_gone():
    assert not (FRONTEND / "app.js").exists(), (
        "frontend/app.js must stay deleted: index.html loads frontend/js/** instead"
    )


def test_lucide_icon_names_path_is_stable():
    # tests/test_device_tool.py reads this exact path.
    assert (FRONTEND / "lucide-icon-names.js").is_file()


def test_script_tags_exist_and_bootstrap_is_last():
    html = INDEX_HTML.read_text(encoding="utf-8")
    srcs = SCRIPT_SRC.findall(html)
    assert srcs, "no <script src> tags found in index.html"
    # Only local scripts are checked for existence; external CDN tags (e.g. the
    # optional htmx include) resolve over the network and have no file on disk.
    local = [s for s in srcs if s.startswith("/static/")]
    assert local, "no /static/ scripts found in index.html"
    for src in local:
        on_disk = FRONTEND / src[len("/static/"):]
        assert on_disk.is_file(), f"<script src={src!r}> has no file at {on_disk}"
    app_srcs = [s for s in local if "/vendor/" not in s and not s.endswith("lucide-icon-names.js")]
    assert app_srcs[-1].endswith("js/bootstrap.js"), (
        "bootstrap.js must be the LAST script so its DOMContentLoaded listener "
        "runs after every realm file has declared its functions"
    )


def test_no_duplicate_top_level_functions():
    funcs, _ = _declarations()
    dupes = {name: files for name, files in funcs.items() if len(files) > 1}
    assert not dupes, (
        "duplicate top-level function declarations (last script loaded silently "
        f"wins): {dupes}"
    )


def test_no_duplicate_top_level_lexicals():
    _, lexicals = _declarations()
    dupes = {name: files for name, files in lexicals.items() if len(files) > 1}
    assert not dupes, (
        "duplicate top-level let/const across classic scripts throws a "
        f"SyntaxError at load time: {dupes}"
    )


def test_every_inline_handler_resolves():
    funcs, _ = _declarations()
    sources = {"index.html": INDEX_HTML.read_text(encoding="utf-8")}
    for path in _js_files():
        sources[str(path.relative_to(FRONTEND))] = path.read_text(encoding="utf-8")

    missing: dict[str, list[str]] = {}
    seen_any = False
    for where, text in sources.items():
        for attr_body in INLINE_ATTR.findall(text):
            for name in CALL_IN_ATTR.findall(attr_body):
                seen_any = True
                if name in ALLOWED_GLOBALS or name in funcs:
                    continue
                missing.setdefault(name, []).append(where)
    assert seen_any, "no inline handlers found at all - the scanner broke"
    assert not missing, (
        "inline on* handlers reference functions that are not declared at top "
        f"level anywhere under frontend/js/: {missing}"
    )


def test_wire_all_calls_existing_functions():
    bootstrap = (JS_DIR / "bootstrap.js").read_text(encoding="utf-8")
    body = re.search(r"function\s+wireAll\s*\(\s*\)\s*\{(.*?)\n\}", bootstrap, re.S)
    assert body, "wireAll() not found in bootstrap.js"
    funcs, _ = _declarations()
    called = re.findall(r"(?<![.\w$])(wire[A-Za-z0-9_$]*)\s*\(\s*\)", body.group(1))
    assert called, "wireAll() calls no wiring functions"
    unknown = [name for name in called if name not in funcs]
    assert not unknown, f"wireAll() calls undeclared functions: {unknown}"
