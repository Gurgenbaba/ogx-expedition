# app/i18n.py
"""
Minimal i18n for OGX Expedition.
Supports: en, de, fr
Language priority: ?lang= query param > Accept-Language header > default (en)
"""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

LANG_DIR   = Path(__file__).parent / "lang"
SUPPORTED  = ("en", "de", "fr")
DEFAULT    = "en"

FLAG = {"en": "🇬🇧", "de": "🇩🇪", "fr": "🇫🇷"}
LABEL = {"en": "EN", "de": "DE", "fr": "FR"}


@lru_cache(maxsize=None)
def _load(lang: str) -> dict:
    f = LANG_DIR / f"{lang}.json"
    if not f.exists():
        return {}
    return json.loads(f.read_text("utf-8"))


from urllib.parse import parse_qs

from urllib.parse import parse_qs

def get_lang(request) -> str:
    """
    Priority:
      1) query param ?lang=de|en|fr
      2) Accept-Language header
      3) DEFAULT
    """
    # 1) query override like PHP
    q = request.query_params.get("lang", "").strip().lower()[:2]
    if q in SUPPORTED:
        return q

    # 2) Accept-Language
    al = request.headers.get("accept-language", "")
    parts = []
    for part in al.split(","):
        part = part.strip()
        if not part:
            continue
        if ";q=" in part:
            tag, qv = part.split(";q=", 1)
            try:
                weight = float(qv)
            except ValueError:
                weight = 0.0
        else:
            tag, weight = part, 1.0
        code = tag.strip().split("-")[0].lower()[:2]
        parts.append((weight, code))

    parts.sort(key=lambda x: -x[0])
    for _, code in parts:
        if code in SUPPORTED:
            return code

    return DEFAULT


def make_translator(lang: str) -> Callable:
    """Return a t(key, **fmt) function for the given language."""
    strings  = _load(lang)
    fallback = _load(DEFAULT) if lang != DEFAULT else {}

    def t(key: str, **kwargs) -> str:
        val = strings.get(key) or fallback.get(key) or key
        if kwargs:
            try:
                return val.format(**kwargs)
            except (KeyError, ValueError):
                return val
        return val

    return t


def get_translations_js(lang: str) -> dict:
    """Return the full translation dict for injection into JS."""
    strings  = _load(lang)
    fallback = _load(DEFAULT) if lang != DEFAULT else {}
    merged   = {**fallback, **strings}
    return merged
