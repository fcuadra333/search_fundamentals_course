"""Bilingual layer (Spanish / English).

Rules:

- No user-visible string is written in a template or in a Python module. Every
  one of them comes from `catalog/<locale>.json`.
- Both catalogs hold exactly the same keys. `tests/test_i18n.py` fails when they
  drift apart, which is what stops one language from decaying into a stub.
- Locale resolution order: explicit `?lang=` → user preference → organization
  default → `Accept-Language` → Spanish.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOG_DIR = Path(__file__).parent / "catalog"
SUPPORTED_LOCALES = ("es", "en")
DEFAULT_LOCALE = "es"

LOCALE_NAMES = {"es": "Español", "en": "English"}


@lru_cache(maxsize=None)
def load_catalog(locale: str) -> dict[str, str]:
    path = CATALOG_DIR / f"{locale}.json"
    if not path.exists():
        raise FileNotFoundError(f"missing catalog for locale {locale!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_locale(raw: str | None) -> str | None:
    """Accept `es`, `es-AR`, `en_US`; return a supported locale or None."""
    if not raw:
        return None
    candidate = raw.strip().lower().replace("_", "-").split("-")[0]
    return candidate if candidate in SUPPORTED_LOCALES else None


def parse_accept_language(header: str | None) -> str | None:
    """Pick the best supported locale out of an `Accept-Language` header."""
    if not header:
        return None
    candidates: list[tuple[float, str]] = []
    for part in header.split(","):
        piece = part.strip()
        if not piece:
            continue
        quality = 1.0
        if ";" in piece:
            piece, _, params = piece.partition(";")
            for param in params.split(";"):
                key, _, value = param.partition("=")
                if key.strip() == "q":
                    try:
                        quality = float(value)
                    except ValueError:
                        quality = 0.0
        locale = normalize_locale(piece)
        if locale:
            candidates.append((quality, locale))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def resolve_locale(
    *,
    explicit: str | None = None,
    user_locale: str | None = None,
    organization_locale: str | None = None,
    accept_language: str | None = None,
) -> str:
    """Resolution order documented at the top of this module."""
    for candidate in (
        normalize_locale(explicit),
        normalize_locale(user_locale),
        normalize_locale(organization_locale),
        parse_accept_language(accept_language),
    ):
        if candidate:
            return candidate
    return DEFAULT_LOCALE


class Translator:
    """Resolves keys for one locale, falling back to Spanish then to the key."""

    def __init__(self, locale: str = DEFAULT_LOCALE):
        self.locale = normalize_locale(locale) or DEFAULT_LOCALE
        self._catalog = load_catalog(self.locale)
        self._fallback = load_catalog(DEFAULT_LOCALE)

    def __call__(self, key: str, **params) -> str:
        return self.translate(key, **params)

    def translate(self, key: str, **params) -> str:
        template = self._catalog.get(key)
        if template is None:
            template = self._fallback.get(key)
        if template is None:
            # Showing the key is better than showing nothing: it is obvious in
            # review and it never crashes a page.
            return key
        if not params:
            return template
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template

    def has(self, key: str) -> bool:
        return key in self._catalog or key in self._fallback

    def enum_label(self, member) -> str:
        """Translate an enum member through its `label_key`."""
        return self.translate(member.label_key)

    @property
    def other_locales(self) -> list[str]:
        return [locale for locale in SUPPORTED_LOCALES if locale != self.locale]


@lru_cache(maxsize=len(SUPPORTED_LOCALES))
def get_translator(locale: str) -> Translator:
    return Translator(locale)


def catalog_keys(locale: str) -> set[str]:
    return set(load_catalog(locale))


def missing_keys() -> dict[str, set[str]]:
    """Keys present in one catalog and absent from another. Must stay empty."""
    catalogs = {locale: catalog_keys(locale) for locale in SUPPORTED_LOCALES}
    every_key: set[str] = set()
    for keys in catalogs.values():
        every_key |= keys
    return {locale: every_key - keys for locale, keys in catalogs.items() if every_key - keys}
