"""Extract a bars Document from a PDF with pymupdf.

Text blocks are read in column-aware order, running headers/footers and page numbers are removed,
and line-break hyphens and wrapped lines are rejoined. Everything before the Abstract (or, if there
is none, the first-page text before the Introduction) is front matter: it is harvested for
identities and kept in Document.front_matter for leak checking, but never sent to the reviewer.
Sections are split on a known heading set and classified with the JATS title rules.
"""

from __future__ import annotations

import itertools
import math
import re
from collections import Counter
from dataclasses import dataclass, field

try:
    import pymupdf
except ImportError:  # older PyMuPDF only ships the `fitz` module name
    import fitz as pymupdf

from .jats import EMAIL_RE, ORCID_RE, classify_title, institution_candidates, normalise_title
from .model import DROP_KINDS, Author, Document, Section

SUPERSCRIPT_FLAG = 1
EDGE_LINES = 3  # lines at the top and bottom of each page checked for running headers/footers
EDGE_ZONE = 0.12  # ... and only when they lie in the top/bottom 12% of the page
FRONT_PAGES = 2  # the Abstract heading and publisher metadata are only looked for on these pages

_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
              "\ufb05": "st", "\ufb06": "st"}
_PAGE_NUMBER_RE = re.compile(r"^(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
_NUMBERED_RE = re.compile(r"^(?P<num>(?:\d{1,2}(?:\.\d{1,2})*|[IVX]{1,4})\.?)\s+(?P<body>\S.*)$")
_INLINE_HEADING_RE = re.compile(r"^(?P<title>[A-Z][A-Za-z'\u2019 ,&/-]{2,60}?)\s*[:.]\s+(?P<rest>\S.*)$")
_ABSTRACT_RE = re.compile(
    r"^(?P<word>ABSTRACT|Abstract|SUMMARY|Summary)\b\s*(?P<sep>[:.\u2013\u2014-]?)\s*(?P<rest>.*)$")
# Publisher metadata labels ("Received: ...", "Citation: ...", "Copyright: (c) 2019 ...") on the first pages.
# Plain words need a colon or a date after them so that body text such as "Published estimates ..." stays.
_META_RE = re.compile(
    r"^[\s*\u2020\u2709]*(?:(?:citation|editor|academic editor|handling editor|received|accepted|revised"
    r"|published(?: online)?|copyright|correspondence|e-?mail|orcid|licen[cs]e|open access|present address"
    r"|current address|how to cite this article)\s*(?::|\d)"
    r"|(?:corresponding authors?|correspondence to|edited by|reviewed by|this is an open access)\b"
    r"|\u00a9|\(c\)\s*\d{4})", re.I)
_URLISH_RE = re.compile(r"(?:https?://|www\.|doi\.org/|\w\.\w{2,}/)\S*$")
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")

_BODY_HEADINGS = frozenset({
    "introduction", "background", "results", "result", "discussion", "results and discussion",
    "conclusion", "conclusions", "concluding remarks", "methods", "method", "materials and methods",
    "material and methods", "methods and materials", "experimental procedures", "experimental section",
    "star methods", "star\u2605methods", "online methods", "methods summary", "limitations", "ethics",
    "ethics approval", "ethics statement", "case report", "case presentation", "main text", "main",
})
_ABSTRACT_TITLES = frozenset({"abstract", "summary", "author summary", "significance", "significance statement",
                              "highlights", "graphical abstract"})
_BACK_TITLES = frozenset({"supporting information", "supplementary information", "supplementary material",
                          "supplementary materials", "supplementary data", "supplemental information",
                          "appendix", "appendices", "abbreviations", "glossary"})
_PARTICLES = frozenset({"van", "von", "de", "der", "den", "da", "das", "dos", "del", "della", "di", "du",
                        "la", "le", "ter", "ten", "bin", "al", "el", "y"})
_NOT_NAME = frozenset({
    "the", "of", "and", "for", "in", "on", "a", "with", "from", "by", "to", "at", "is", "are",
    "abstract", "introduction", "journal", "volume", "research", "article", "open", "access", "original",
    "letter", "review", "received", "accepted", "published", "editor", "correspondence", "keywords", "doi",
    "vol", "issue", "page", "copyright", "license", "citation", "supplementary", "information", "figure",
    "table", "data", "methods", "results", "discussion",
})
_INITIALS_RE = re.compile(r"^[^\W\d_]\.(?:-?[^\W\d_]\.)*$")
_NAME_PREFIX_RE = re.compile(r"^(?:Mc|Mac|De|Di|Da|Du|Le|La|Van|Von|Fitz)(?=[^\W\d_])")  # McDonald, DeWitt
# Academic degrees and suffixes after a name ('Alice Quokkason, PhD'), compared without dots.
_DEGREES = frozenset("PhD MD MSc BSc MPH DPhil DVM MBBS MBChB FRS FRCP FMedSci FACP DSc ScD MA MS BS MBA RN "
                     "PharmD DDS DMD MRes MPhil MHS Jr Sr II III".split())
_MARKERS = "\\d*\u2020\u2021\u00a7\u00b6#\u2217\u204e\u2709\u00b9\u00b2\u00b3\u2070-\u2079"
_TRAILING_MARKERS_RE = re.compile(rf"(?<=[^\W\d_.])[{_MARKERS}]+|[{_MARKERS}]+$")
_MARKERS_ONLY_RE = re.compile(rf"^[{_MARKERS}\s,a-z]{{0,4}}$")
# Author separators: commas, semicolons, middle dots (Springer), bars (Wiley), bullets, 'and'/'AND', '&'.
_NAME_SPLIT_RE = re.compile(r"\s*(?:[,;|\u00b7\u2022\u2219\u22c5]|\b(?i:and)\b|&)\s*")
# Affiliation footnotes ('1 Department of ...', 'a Institute of ...') in the lower part of the first pages.
_AFFILIATION_MARK_RE = re.compile(rf"^(?:[{_MARKERS}]+|[a-z](?=\s*[A-Z]))\s*[^\W\d_]")
FOOTNOTE_ZONE = 0.75  # affiliation footnotes are looked for below this fraction of the page height
BASELINE_TOLERANCE = 0.004  # lines whose centres differ by less than this share a baseline (table rows)
_JOURNAL_HINT_RE = re.compile(
    r"\b(?:Journal|Proceedings|Annals|Reports|Letters|Transactions|Reviews?|Bulletin|Communications|Research"
    r"|Science|Sciences|Nature|PLOS|PLoS|eLife|Cell|Genetics|Genomics|Biology|Medicine|Bioinformatics"
    r"|Evolution|Ecology|Microbiology|Virology|Epidemiology|Archives|Advances|Frontiers|BMC|Molecular)\b")
# Article-type labels in running heads ('Research Article', 'Open Access') that name no journal.
_GENERIC_HEAD_WORDS = _NOT_NAME | frozenset(
    "articles report reports brief communication short paper full resource note notes perspective commentary "
    "new methods".split())
_INSTITUTION_HINT_RE = re.compile(
    r"\b(?:University|Universit\w+|Institute|Institut\w*|College|Hospital|Center|Centre|Laboratory"
    r"|Laboratories|School|Department|Faculty|Academy|Foundation|Clinic|Division)\b")


@dataclass
class _Line:
    text: str
    plain: str  # text without superscript spans (author/affiliation markers)
    page: int
    block: int
    size: float
    y: float = 0.5  # vertical position of the line's centre as a fraction of the page height


@dataclass
class _Builder:
    title: str
    kind: str
    level: int
    paras: list[tuple[int, list[str]]] = field(default_factory=list)  # (block id, lines)

    def add(self, block: int, text: str) -> None:
        if self.paras and self.paras[-1][0] == block:
            self.paras[-1][1].append(text)
        else:
            self.paras.append((block, [text]))

    def section(self) -> Section:
        paragraphs: list[str] = []
        for _, lines in self.paras:
            text = join_lines(lines)
            if paragraphs and not re.search(r"[.!?:;)\]]$", paragraphs[-1]) and text[:1].islower():
                paragraphs[-1] = join_lines([paragraphs[-1], text])  # paragraph broken by a column/page
            elif text:
                paragraphs.append(text)
        return Section(title=self.title, text="\n\n".join(paragraphs), kind=self.kind, level=self.level)


# ---------------------------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------------------------

def _open(pdf_bytes: bytes):
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pymupdf raises several exception types for corrupt input
        raise ValueError(f"not a readable PDF: {exc}") from exc
    if doc.needs_pass and not doc.authenticate(""):
        raise ValueError("PDF is password protected")
    if doc.page_count == 0:
        raise ValueError("PDF has no pages")
    return doc


def _reading_order(blocks: list[dict], width: float) -> list[dict]:
    """Order blocks top to bottom, reading two-column bands left column first."""
    mid = width / 2

    def column(block: dict) -> int | None:
        x0, _, x1, _ = block["bbox"]
        if x0 < mid - 5 and x1 > mid + 5:
            return None  # spans the gutter (full-width text, centred headings)
        return 0 if x1 <= mid + 5 else 1

    ordered: list[dict] = []
    band: list[dict] = []
    for block in sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        if column(block) is None:
            ordered.extend(sorted(band, key=lambda b: (column(b), b["bbox"][1])))
            band = []
            ordered.append(block)
        else:
            band.append(block)
    ordered.extend(sorted(band, key=lambda b: (column(b), b["bbox"][1])))
    return ordered


def _page_lines(page, page_no: int, block_base: int) -> tuple[list[_Line], list[str]]:
    """Horizontal text lines of a page in reading order, plus any rotated (margin) text."""
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES & ~pymupdf.TEXT_PRESERVE_LIGATURES
    data = page.get_text("dict", sort=True, flags=flags)
    blocks = [b for b in data["blocks"] if b.get("type", 0) == 0 and b.get("lines")]
    lines: list[_Line] = []
    rotated: list[str] = []
    for i, block in enumerate(_reading_order(blocks, page.rect.width)):
        for line in block["lines"]:
            spans = line["spans"]
            text = _fix_ligatures("".join(s["text"] for s in spans)).strip()
            if not text:
                continue
            if abs(line.get("dir", (1, 0))[1]) > 0.1:
                rotated.append(text)
                continue
            visible = [s for s in spans if s["text"].strip()]
            plain = _fix_ligatures("".join(s["text"] for s in spans if not s["flags"] & SUPERSCRIPT_FLAG))
            lines.append(_Line(
                text=text, plain=plain.strip(), page=page_no, block=block_base + i,
                size=max(s["size"] for s in visible),
                y=(line["bbox"][1] + line["bbox"][3]) / 2 / (page.rect.height or 1),
            ))
    return lines, rotated


def _fix_ligatures(text: str) -> str:
    for ligature, letters in _LIGATURES.items():
        text = text.replace(ligature, letters)
    return text


def _edge(lines: list[_Line]) -> set[int]:
    """Indices of the lines that may be a running header or footer."""
    n = len(lines)
    top = {i for i in range(min(EDGE_LINES, n)) if lines[i].y <= EDGE_ZONE}
    bottom = {i for i in range(max(0, n - EDGE_LINES), n) if lines[i].y >= 1 - EDGE_ZONE}
    return top | bottom


def _strip_running(pages: list[list[_Line]]) -> tuple[list[list[_Line]], list[str]]:
    """Drop page numbers and header/footer lines repeated on >= 50% of pages (top/bottom lines only).

    Heads that alternate between even and odd pages (author head / journal head) are counted per
    page parity, leaving out the first page, which usually has neither.
    """
    def key(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.lower())).strip()

    counts: Counter[str] = Counter()
    by_parity: tuple[Counter[str], Counter[str]] = (Counter(), Counter())
    for page_no, lines in enumerate(pages):
        keys = {key(lines[i].text) for i in _edge(lines)}
        counts.update(keys)
        if page_no:
            by_parity[page_no % 2].update(keys)
    threshold = max(2, math.ceil(len(pages) / 2))
    per_parity = (len(range(2, len(pages), 2)), len(range(1, len(pages), 2)))  # pages after the first
    parity_threshold = [max(2, math.ceil(n / 2)) for n in per_parity]

    def running(text: str, page_no: int) -> bool:
        k, parity = key(text), page_no % 2
        return counts[k] >= threshold or by_parity[parity][k] >= parity_threshold[parity]

    kept_pages: list[list[_Line]] = []
    removed: list[str] = []
    for page_no, lines in enumerate(pages):
        edges = _edge(lines)
        kept = []
        for i, line in enumerate(lines):
            if i in edges and (_PAGE_NUMBER_RE.match(line.text) or running(line.text, page_no)):
                removed.append(line.text)
            else:
                kept.append(line)
        kept_pages.append(kept)
    return kept_pages, removed


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines into one paragraph, undoing line-break hyphenation (but not in URLs)."""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        last_token = out.rsplit(" ", 1)[-1]
        if out[-1] in "-\u2010\u00ad":
            if _URLISH_RE.search(last_token):
                out += line  # keep the hyphen: it is part of the URL
            elif out[-1] == "\u00ad" or (len(out) > 1 and out[-2].isalpha() and line[0].islower()):
                out = out[:-1] + line
            elif len(out) > 1 and out[-2] == " ":
                out += " " + line  # a free-standing dash
            else:
                out += line  # e.g. "COVID-" + "19"
        elif _URLISH_RE.search(last_token) and out[-1] in "/._=?&#~":
            out += line  # URL wrapped after a separator
        else:
            out += " " + line
    return re.sub(r"[ \t]+", " ", out)


# ---------------------------------------------------------------------------------------------
# Headings and front matter
# ---------------------------------------------------------------------------------------------

def _known_heading(title: str) -> bool:
    norm = normalise_title(title)
    return bool(norm) and (norm in _BODY_HEADINGS or norm in _ABSTRACT_TITLES or norm in _BACK_TITLES
                           or classify_title(title) is not None)


def _heading(text: str, first_in_block: bool) -> tuple[str, str, int, bool] | None:
    """(title, inline text, level, is_inline_declaration) when a line starts a known section."""
    s = text.strip()
    m = _NUMBERED_RE.match(s)
    num, body = (m["num"], m["body"]) if m else ("", s)
    title = body.rstrip(" :.")
    if len(s) <= 80 and not re.search(r"[:.;]\s", title) and _known_heading(title):
        level = min(2 + num.rstrip(".").count("."), 4)
        return (f"{num} {title}" if num else title), "", level, False
    if not first_in_block:
        return None
    if (m := _INLINE_HEADING_RE.match(body)) and classify_title(m["title"]):
        return m["title"].strip(), m["rest"], 2, True
    if (rest := _abstract_rest(body)) is not None:
        return body.split()[0].capitalize(), rest, 2, False
    return None


def _abstract_rest(text: str) -> str | None:
    """Inline text after an 'Abstract'/'Summary' heading, '' if the heading stands alone, else None."""
    m = _ABSTRACT_RE.match(text.strip())
    if not m:
        return None
    rest = m["rest"]
    if not rest or m["sep"] or rest[0].isupper() or rest[0].isdigit():
        return rest
    return None


def _front_end(lines: list[_Line], title: list[_Line]) -> tuple[int, str]:
    """Index of the first line after the front matter, and how it was found.

    A title that starts with 'Summary'/'Abstract' ('Summary Statistics Imputation ...') is not the
    Abstract heading: title lines and the first line of the paper only count when they stand alone.
    """
    for i, line in enumerate(lines):
        if line.page >= FRONT_PAGES:
            break
        rest = _abstract_rest(line.text)
        if rest is not None and (rest == "" or not (i == 0 or any(line is t for t in title))):
            return i, "abstract"
    for i, line in enumerate(lines):
        if line.page > 0:
            break
        heading = _heading(line.text, True)
        if heading and not heading[3] and (_NUMBERED_RE.match(line.text.strip())
                                           or normalise_title(heading[0]) in ("introduction", "background")):
            return i, "introduction"
    first_after = next((i for i, line in enumerate(lines) if line.page > 0), len(lines))
    return first_after, "page"


def _body_size(lines: list[_Line]) -> float:
    sizes = sorted(line.size for line in lines if len(line.text) >= 40) or sorted(line.size for line in lines)
    return sizes[len(sizes) // 2] if sizes else 10.0


def _title_lines(lines: list[_Line], body_size: float) -> list[_Line]:
    """The contiguous run of largest-font lines on page 1 (the article title)."""
    page0 = [line for line in lines if line.page == 0]
    if not page0:
        return []
    biggest = max(line.size for line in page0)
    if biggest < body_size * 1.15:
        return []
    start = next(i for i, line in enumerate(page0) if line.size >= biggest - 0.5)
    run = []
    for line in page0[start:]:
        if line.size < biggest - 0.5:
            break
        run.append(line)
    return run


def _usable_meta_title(title: str) -> bool:
    """False for empty or tool-generated titles ("Microsoft Word - x.docx", "untitled", "PII: S...")."""
    t = title.strip()
    junk = r"(?i)^(?:untitled|microsoft word|document\d*$|pii\b|doi\b|10\.\d)|\.(?:docx?|pdf|tex|indd)$"
    return len(t) >= 10 and not re.search(junk, t)


def _is_degree(word: str) -> bool:
    return word.replace(".", "").strip(",") in _DEGREES


def _name_part(part: str) -> bool:
    """'Alice', 'MÜLLER', 'Nguyễn', 'McDonald' or the 'O' of O'Brien (any script with letter case)."""
    if not part.isalpha():
        return False
    if part.isupper():
        return True
    for word in (part, _NAME_PREFIX_RE.sub("", part)):
        if word[:1].isupper() and (word[1:].islower() or not word[1:]):
            return True
    return False


def _name_word(word: str) -> bool:
    if _INITIALS_RE.match(word):
        return word[0].isupper()
    return all(_name_part(p) for p in re.split(r"[-'’]", word))


def _title_case(word: str) -> str:
    return re.sub(r"[^\W\d_]+", lambda m: m.group()[0] + m.group()[1:].lower(), word)


def _name_from_token(token: str) -> Author | None:
    words = _TRAILING_MARKERS_RE.sub("", token).split()
    while len(words) > 2 and _is_degree(words[-1]):
        words.pop()  # 'Alice Quokkason PhD'
    if not 2 <= len(words) <= 5 or words[0].lower() in _PARTICLES:
        return None
    for word in words:
        if word.lower() in _PARTICLES:
            continue
        if word.lower().strip(".") in _NOT_NAME or not _name_word(word):
            return None
    # Names set in capitals ('ALICE QUOKKASON', 'Alice QUOKKASON') are written normally in the text.
    shout = all(w.isupper() for w in words if not _INITIALS_RE.match(w))
    words = [_title_case(w) if w.isupper() and not _INITIALS_RE.match(w) and len(w) >= (2 if shout else 3)
             else w for w in words]
    i = len(words) - 1
    while i > 1 and words[i - 1].lower() in _PARTICLES:
        i -= 1
    surname = " ".join(words[i:])
    if surname.endswith(".") or len(surname) < 2:
        return None
    return Author(surname=surname, given=" ".join(words[:i]), heuristic=True)


def _names_in_line(line: _Line, allow_single: bool) -> list[Author]:
    text = line.plain or line.text
    if EMAIL_RE.search(text) or re.search(r"https?:|www\.|:|\d{3,}", text) or _INSTITUTION_HINT_RE.search(text):
        return []
    tokens = [t.strip() for t in _NAME_SPLIT_RE.split(text)]
    tokens = [t for t in tokens if t and not _MARKERS_ONLY_RE.match(t) and not _is_degree(t)]
    names = [n for n in (_name_from_token(t) for t in tokens) if n]
    if not names or len(names) < 0.6 * len(tokens):
        return []
    has_marker = line.plain != line.text or bool(re.search(rf"[{_MARKERS}]", line.text))
    if len(names) == 1 and not (has_marker or allow_single):
        return []
    return names


def _front_authors(front: list[_Line], title_lines: list[_Line]) -> list[Author]:
    """Name-like author lines; a lone unmarked name is accepted only right below the title."""
    authors: list[Author] = []
    candidates = [line for line in front if not any(line is t for t in title_lines)]
    for i, line in enumerate(candidates):
        authors.extend(_names_in_line(line, allow_single=bool(title_lines) and i == 0))
    return authors


def _affiliation_lines(texts: list[str]) -> list[str]:
    out = []
    for raw in texts:
        text = re.sub(rf"^(?:[{_MARKERS},]+|[a-z](?=\s*[A-Z]))\s*", "", raw).strip()
        if _INSTITUTION_HINT_RE.search(text) and not EMAIL_RE.search(text) and len(text) <= 250:
            out.append(text)
    return out


def _guess_year(text: str) -> int | None:
    m = re.search(r"(?i)\bpublished\b[^\n]{0,40}?\b((?:19|20)\d{2})\b", text) or re.search(
        r"(?i)(?:\u00a9|\(c\)|\bcopyright\b)\s*(?:\u00a9\s*)?((?:19|20)\d{2})\b", text)
    return int(m.group(1)) if m else None


def _guess_journal(running: list[str]) -> str:
    counts: Counter[str] = Counter()
    for text in running:
        for part in re.split(r"\s*[|\u2022\u00b7\u2013\u2014]\s*|\s{2,}", text):
            if not re.search(r"https?:|doi\.org|www\.|@", part, re.I):
                part = re.split(r"\s+(?=[\d(\[])", part, maxsplit=1)[0]  # 'Journal X 7 (2019) 123'
            part = part.strip(" ,.;:")
            words = part.split()
            if len(words) >= 2 and not re.search(r"\d|https?:|doi|www\.|@", part, re.I) \
                    and _JOURNAL_HINT_RE.search(part) \
                    and not all(w.lower().strip(".,:") in _GENERIC_HEAD_WORDS for w in words):
                counts[part] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _authors_from_metadata(value: str) -> list[Author]:
    authors = []
    for part in re.split(r"\s*(?:;|,|\band\b|&)\s*", value or ""):
        if re.search(r"(?i)user|admin|microsoft|author|office|windows|editor|\d", part):
            continue
        words = part.split()
        if 2 <= len(words) <= 5:
            authors.append(Author(surname=words[-1], given=" ".join(words[:-1])))
    return authors


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


# ---------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------

def _block_texts(lines: list[_Line], start: int) -> list[str]:
    return [other.text for other in itertools.takewhile(lambda o: o.block == lines[start].block, lines[start:])]


def _is_meta_block(lines: list[_Line], start: int) -> bool:
    """Publisher metadata blocks on the first pages (citation, editor, dates, copyright, e-mail).

    A block that starts a section ('Data Availability: ... alice@...') or has a heading merged into
    it is content, not metadata; scrubbing removes the e-mail.
    """
    line = lines[start]
    if line.page >= FRONT_PAGES:
        return False
    if _META_RE.match(line.text):
        return True
    block = _block_texts(lines, start)
    if _heading(block[0], True) or any(_heading(text, False) for text in block[1:]):
        return False
    block_text = " ".join(block)
    return bool(EMAIL_RE.search(block_text) or ORCID_RE.search(block_text)) and len(block_text) < 300


def _is_affiliation_block(lines: list[_Line], start: int) -> bool:
    """Affiliation footnotes at the foot of the first pages ('1 Department of ..., University of ...')."""
    line = lines[start]
    if line.page >= FRONT_PAGES or line.y < FOOTNOTE_ZONE:
        return False
    marked = (line.plain != line.text and not line.text.startswith(line.plain)) \
        or bool(_AFFILIATION_MARK_RE.match(line.text))
    block_text = " ".join(_block_texts(lines, start))
    return marked and bool(_INSTITUTION_HINT_RE.search(block_text)) and len(block_text) < 1000


def _in_table_row(lines: list[_Line], index: int) -> bool:
    """True when other cells share the line's baseline (a table header such as 'Species | Reference')."""
    line = lines[index]
    same = [other for j, other in enumerate(lines)
            if j != index and other.page == line.page and abs(other.y - line.y) < BASELINE_TOLERANCE]
    return len(same) >= 2 or any(other.block == line.block for other in same)


def _continues(builder: _Builder, text: str) -> bool:
    """True when `text` carries on the builder's unfinished last sentence (a block split mid-sentence)."""
    last = builder.paras[-1][1][-1] if builder.paras else ""
    return bool(last) and not re.search(r"[.!?:;]$", last.rstrip()) and text[:1].islower()


def _section_kind(title: str, seen_body: bool, seen_refs: bool) -> str:
    norm = normalise_title(title)
    if norm in _ABSTRACT_TITLES and not seen_body:
        return "abstract"
    if special := classify_title(title):
        return special
    if norm in _BACK_TITLES or seen_refs:
        return "back"
    return "body"


def _sections(lines: list[_Line], front_extra: list[str]) -> list[Section]:
    """Split body lines on known headings; inline declarations ('Funding: ...') end with their block."""
    builders: list[_Builder] = []
    main: _Builder | None = None
    aside: _Builder | None = None
    seen_body = seen_refs = False
    meta_block = -1
    for i, line in enumerate(lines):
        first_in_block = i == 0 or lines[i - 1].block != line.block
        if aside is not None and first_in_block and not _continues(aside, line.text):
            aside = None
        if line.block == meta_block or (first_in_block and (_is_meta_block(lines, i)
                                                             or _is_affiliation_block(lines, i))):
            meta_block = line.block
            front_extra.append(line.text)
            continue
        heading = _heading(line.text, first_in_block)
        if heading:
            title, rest, level, inline = heading
            kind = _section_kind(title, seen_body, seen_refs)
            if kind in DROP_KINDS and not inline and _in_table_row(lines, i):
                heading = None  # a 'Reference' or 'Funding' table column, not a section
        if heading:
            seen_body |= kind == "body"
            seen_refs |= kind == "refs"
            builder = _Builder(title, kind, level)
            builders.append(builder)
            if inline:
                aside = builder
            else:
                main, aside = builder, None
            if rest:
                builder.add(line.block, rest)
            continue
        target = aside or main
        if target is None:
            main = target = _Builder("Main text", "body", 2)
            builders.append(main)
        target.add(line.block, line.text)
    sections = [b.section() for b in builders]
    return [s for s in sections if s.text]


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------

def parse_pdf(pdf_bytes: bytes, source_id: str) -> Document:
    """Parse a PDF into a Document; front matter is harvested and set aside, never rendered."""
    pdf = _open(pdf_bytes)
    pages: list[list[_Line]] = []
    rotated: list[str] = []
    block_base = 0
    for page_no, page in enumerate(pdf):
        lines, margin = _page_lines(page, page_no, block_base)
        pages.append(lines)
        rotated.extend(margin)
        block_base += 10_000  # keeps block ids unique across pages
    metadata = pdf.metadata or {}
    pdf.close()

    all_text = "\n".join(line.text for lines in pages for line in lines)
    if not all_text.strip():
        raise ValueError("PDF has no extractable text (scanned image?)")
    top_of_first_page = [pages[0][i] for i in sorted(_edge(pages[0])) if pages[0][i].y <= EDGE_ZONE]
    pages, running = _strip_running(pages)
    lines = [line for page_lines in pages for line in page_lines]
    doc = Document(source_id=source_id, source_type="pdf")

    body_size = _body_size(lines)
    masthead = [line.text for line in top_of_first_page if line.size <= body_size]  # small print, not the title
    end, how = _front_end(lines, _title_lines(lines, body_size))
    front, rest = lines[:end], lines[end:]
    if how != "abstract":
        doc.notes.append("no Abstract heading found; front matter = "
                         + ("first-page text before the first section heading" if how == "introduction"
                            else "the whole first page"))
    front_extra: list[str] = []
    doc.sections = _sections(rest, front_extra)
    if not doc.sections:
        raise ValueError("no body text found after the front matter")

    title_lines = _title_lines(front, body_size)
    meta_title = metadata.get("title") or ""
    doc.title = meta_title.strip() if _usable_meta_title(meta_title) else " ".join(line.text for line in title_lines)
    front_authors = _front_authors(front, title_lines)
    for author in _authors_from_metadata(metadata.get("author") or "") + front_authors:
        key = (author.surname.lower(), author.given.lower())
        if key not in {(a.surname.lower(), a.given.lower()) for a in doc.authors}:
            doc.authors.append(author)
    if front_authors:
        doc.notes.append(f"{len(front_authors)} author name(s) guessed heuristically from front matter")

    front_text = "\n".join([line.text for line in front] + front_extra)
    doc.front_matter = "\n".join(filter(None, [front_text, "\n".join(_unique(running)), "\n".join(rotated)]))
    whole = "\n".join([all_text, "\n".join(rotated), "\n".join(s.text for s in doc.sections)])
    doc.emails = _unique([e.rstrip(".") for e in EMAIL_RE.findall(whole)])
    doc.orcids = _unique(ORCID_RE.findall(whole))
    doc.affiliations = _unique(_affiliation_lines([line.text for line in front] + front_extra))
    doc.institutions = _unique(institution_candidates(whole))
    doc.year = _guess_year(doc.front_matter)
    if m := _DOI_RE.search(doc.front_matter):
        doc.doi = m.group(1).rstrip(".,;)")
    doc.journal = _guess_journal(running) or _guess_journal(masthead)
    if running:
        doc.notes.append(f"removed {len(running)} running header/footer/page-number line(s)")
    return doc
