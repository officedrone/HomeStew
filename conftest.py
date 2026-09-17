"""Repo-root conftest: makes ``pytest`` importable from the repository root.

With pytest's default "prepend" import mode, this file's directory is added
to ``sys.path``, so tests can ``import homestew...`` without installing the
package or invoking ``python -m pytest``. It also pins DATA_DIR to a throwaway
path *before* ``homestew.config`` builds its singleton at first import —
otherwise the default ``/data`` would resolve to e.g. ``C:\\data`` on Windows.
"""
import os
import tempfile

# Must run before any test module imports homestew.config (conftest is loaded
# first by pytest). A non-existent temp path keeps load_settings_overrides()
# a no-op at import time; individual tests monkeypatch settings.DATA_DIR.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="homestew-test-"))
