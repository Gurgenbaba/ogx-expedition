# app/i18n.py
"""
Minimal i18n for OGX Expedition.
Supports: en, de, fr
Language priority: cookie ogx_lang > Accept-Language header > default (en)
"""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
from typing import Callable

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


def get_lang(request) -> str:
    """Detect language from cookie, then Accept-Language header."""
    lang = request.cookies.get("ogx_lang", "")
    if lang in SUPPORTED:
        return lang
    al = request.headers.get("accept-language", "")
    for part in al.lower().split(","):
        code = part.strip().split(";")[0].strip()[:2]
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
