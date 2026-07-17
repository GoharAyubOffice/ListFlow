"""Listing content rules: title cleaning (<=80 chars), forbidden-token validation,
eBay-safe description HTML, item-specifics mapping (spec §6).

Implemented in Phase 1. Pure logic — no I/O (stdlib html.parser only, no network libs).
"""

import html
import re
from collections.abc import Iterable
from html.parser import HTMLParser

from listflow.models import Product

EBAY_TITLE_LIMIT = 80

# Tags we emit in descriptions (spec §6.3 allows p/ul/li/b/br/img; source <img> is
# dropped on purpose — gallery images are re-hosted via the Media API, supplier CDN
# links must never be hotlinked).
ALLOWED_TAGS = frozenset({"p", "ul", "li", "b", "br"})

_VOID_TAGS = frozenset({"br"})
_DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "noscript", "svg", "head", "title"})
_BLOCK_TAGS = frozenset({
    "p", "div", "li", "ul", "ol", "tr", "table", "br", "section", "article",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
})

_SEP_CHARS = "-–—|,;:/·.•"

_EMOJI_RE = re.compile(
    "["
    "🀀-🯿"  # emoji, pictographs, symbols-extended
    "←-⇿"  # arrows
    "⌀-➿"  # technical, enclosed alnum, shapes, misc symbols, dingbats
    "⬀-⯿"  # more arrows/stars
    "︀-️"  # variation selectors
    "‍"  # zero-width joiner
    "©®™"  # (c) (r) (tm)
    "]+"
)

# Supplier noise phrases (spec §6.1) and platform names — all removed from titles.
_NOISE_PATTERNS = (
    r"hot\s+sales?",
    r"free\s+(?:shipping|delivery)",
    r"drop\s?ship\w*",
    r"(?:19|20)\d{2}\s+new",
    r"new\s+(?:19|20)\d{2}",
    r"new\s+arrivals?",
    r"best\s+sellers?",
    r"big\s+sale",
    r"high\s+quality",
    r"wholesale",
    r"limited\s+time\s+(?:offer|deal)?",
    r"ali\s*-?\s*express",
    r"amazon",
    r"alibaba",
    r"\bchoice\b",
)
_NOISE_RE = re.compile("|".join(f"(?:{p})" for p in _NOISE_PATTERNS), re.IGNORECASE)

# A separator token standing alone between spaces (left behind by noise removal).
_LONE_SEP_RE = re.compile(rf"(?:(?<=\s)|^)[{re.escape(_SEP_CHARS)}]+(?=\s|$)")

# Forbidden anywhere in any listing field (spec §6.2). Platform names match as
# substrings (catches "AmazonBasics"); "choice" only as a whole word so ordinary
# English ("many choices") is not a false positive.
_FORBIDDEN_SUBSTRINGS = (
    "aliexpress", "ali express", "ali-express", "amazon", "alibaba", "dropship",
)
_FORBIDDEN_WORD_RES = (re.compile(r"\bchoice\b"),)


class ForbiddenTokenError(ValueError):
    """A forbidden supplier token survived into a listing field — hard failure (spec §6.2)."""

    def __init__(self, token: str, field: str):
        self.token = token
        self.field = field
        super().__init__(f"forbidden token '{token}' in {field} — listing blocked")


# ------------------------------------------------------------------ title


def _decap_shouting(word: str) -> str:
    # ALL-CAPS runs read as spam; keep short acronyms (USB, LED, 4K) untouched.
    letters = [c for c in word if c.isalpha()]
    if len(letters) >= 5 and all(c.isupper() for c in letters):
        return word.title()
    return word


def clean_title(raw: str, primary_keyword: str | None = None) -> str:
    """Clean a supplier title into an eBay-legal one (<=80 chars, noise stripped).

    If primary_keyword is given it is moved to the front (ASO front-loading),
    deduplicating any occurrence already in the title.
    """
    s = _EMOJI_RE.sub(" ", raw)
    s = _NOISE_RE.sub(" ", s)
    s = " ".join(_decap_shouting(word) for word in s.split())
    s = _LONE_SEP_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip().strip(_SEP_CHARS + " ")

    if primary_keyword:
        keyword = re.sub(r"\s+", " ", primary_keyword).strip()
        if keyword:
            s = re.sub(re.escape(keyword), " ", s, flags=re.IGNORECASE)
            s = re.sub(r"\s{2,}", " ", s).strip()
            s = f"{keyword} {s}".strip()

    if len(s) > EBAY_TITLE_LIMIT:
        cut = s[:EBAY_TITLE_LIMIT]
        if s[EBAY_TITLE_LIMIT] != " " and " " in cut:
            cut = cut[: cut.rfind(" ")]
        s = cut.strip(_SEP_CHARS + " ")
    return s


# ------------------------------------------------------- forbidden tokens


def _find_forbidden(text: str, extra: tuple[str, ...]) -> str | None:
    low = text.lower()
    for token in _FORBIDDEN_SUBSTRINGS + extra:
        if token and token in low:
            return token
    for word_re in _FORBIDDEN_WORD_RES:
        match = word_re.search(low)
        if match:
            return match.group(0)
    return None


def validate_forbidden(product: Product, extra_forbidden: Iterable[str] = ()) -> None:
    """Raise ForbiddenTokenError if any listing-bound field contains a forbidden token.

    extra_forbidden carries per-product tokens such as the supplier store name.
    Only fields that reach eBay are checked (title_raw stays internal).
    """
    extra = tuple(t.strip().lower() for t in extra_forbidden if t and t.strip())
    checks: list[tuple[str, str]] = [
        ("title_ebay", product.title_ebay),
        ("description_html", product.description_html),
    ]
    checks += [(f"bullet_points[{i}]", b) for i, b in enumerate(product.bullet_points)]
    checks += [(f"item_specifics[{k}]", f"{k} {v}") for k, v in product.item_specifics.items()]
    for i, variant in enumerate(product.variants):
        for key, value in variant.attributes.items():
            checks.append((f"variants[{i}].attributes[{key}]", f"{key} {value}"))

    for field, text in checks:
        token = _find_forbidden(text, extra)
        if token is not None:
            raise ForbiddenTokenError(token=token, field=field)


# ------------------------------------------------------------ description


class _Sanitizer(HTMLParser):
    """Rebuild HTML keeping only allow-listed, attribute-free tags.

    With an empty allow-list it degrades to a plain-text extractor: block tags
    become newlines and script/style/iframe content is dropped entirely.
    """

    def __init__(self, allowed: frozenset[str], escape_text: bool):
        super().__init__(convert_charrefs=True)
        self._allowed = allowed
        self._escape = escape_text
        self._skip = 0
        self._open: list[str] = []
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in self._allowed:
            self._out.append(f"<{tag}>")
            if tag not in _VOID_TAGS:
                self._open.append(tag)
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in self._allowed and tag not in _VOID_TAGS:
            if tag in self._open:
                # close unclosed children first so the output stays balanced
                while self._open:
                    top = self._open.pop()
                    self._out.append(f"</{top}>")
                    if top == tag:
                        break
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        self._out.append(html.escape(data) if self._escape else data)

    def result(self) -> str:
        self.close()
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def sanitize_html(raw_html: str, allowed: frozenset[str] = ALLOWED_TAGS) -> str:
    """Strip raw_html down to the eBay-safe tag subset, attributes removed."""
    parser = _Sanitizer(allowed=allowed, escape_text=True)
    parser.feed(raw_html)
    return re.sub(r"\n{2,}", "\n", parser.result()).strip()


def _text_blocks(raw_html: str) -> list[str]:
    """Plain-text paragraphs of raw_html (unescaped; caller re-escapes on output)."""
    parser = _Sanitizer(allowed=frozenset(), escape_text=False)
    parser.feed(raw_html)
    blocks = []
    for line in parser.result().split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            blocks.append(line)
    return blocks


def build_description(product: Product, boilerplate: str = "") -> str:
    """eBay-safe description HTML: opening line -> benefits -> specs -> boilerplate.

    Source description HTML is reduced to plain text and rebuilt — no supplier
    markup, images or active content can survive. Boilerplate (from config) keeps
    the allowed tag subset.
    """
    parts: list[str] = []
    title = product.title_ebay or product.title_raw
    if title:
        parts.append(f"<p><b>{html.escape(title)}</b></p>")
    parts.extend(f"<p>{html.escape(block)}</p>" for block in _text_blocks(product.description_html))

    bullets = [b.strip() for b in product.bullet_points if b.strip()]
    if bullets:
        items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
        parts.append(f"<ul>{items}</ul>")

    specifics = {k: v for k, v in product.item_specifics.items() if k.strip() and v.strip()}
    if specifics:
        rows = "".join(
            f"<li><b>{html.escape(k)}:</b> {html.escape(v)}</li>" for k, v in specifics.items()
        )
        parts.append(f"<ul>{rows}</ul>")

    if boilerplate:
        cleaned = sanitize_html(boilerplate)
        if cleaned:
            parts.append(cleaned)
    return "\n".join(parts)


# -------------------------------------------------------- item specifics

_SPECIFIC_ALIASES = {
    "color": "Colour",
    "colors": "Colour",
    "colour": "Colour",
    "colours": "Colour",
    "material": "Material",
    "materials": "Material",
    "size": "Size",
    "sizes": "Size",
    "brand": "Brand",
    "brand name": "Brand",
    "manufacturer": "Brand",
    "type": "Type",
    "style": "Style",
    "pattern": "Pattern",
    "model": "Model",
    "model number": "Model",
    "mpn": "MPN",
    "feature": "Features",
    "features": "Features",
    "capacity": "Capacity",
    "power": "Power",
    "theme": "Theme",
    "occasion": "Occasion",
    "department": "Department",
    "room": "Room",
}


def map_item_specifics(attrs: dict[str, str]) -> dict[str, str]:
    """Map raw attribute names onto eBay aspect names; default Brand to Unbranded."""
    out: dict[str, str] = {}
    for raw_key, raw_value in attrs.items():
        key = raw_key.strip()
        value = raw_value.strip()
        if not key or not value:
            continue
        canonical = _SPECIFIC_ALIASES.get(key.lower(), key.title())
        out.setdefault(canonical, value)
    out.setdefault("Brand", "Unbranded")
    return out
