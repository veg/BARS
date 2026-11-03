"""Fail-closed leak check of scrubbed manuscript text before it is sent to the reviewer.

Every identifier is re-derived from the Document here, with patterns written independently of
scrub.py (only its word lists, host tables and placeholder tokens are shared), so a gap in the
scrubber is not silently mirrored by the check. Placeholder tokens are blanked before scanning,
so text inside them is never flagged. Severity "block" keeps the paper from the reviewer;
"warn" is only reported.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from functools import lru_cache

from .model import Author, Document, Leak
from .scrub import (ARCHIVE_HOSTS, BARE_HOST_TLDS, CODE_NAMESPACES, COMMON_WORDS, COUNT_WORDS, DATABASE_HOSTS,
                    GENERIC_ORG_WORDS, INSTITUTION_KEYWORDS, LOWERCASE_HOST_TLDS, NAME_FUNCTION_WORDS,
                    NON_INSTITUTION_PHRASES, PATH_ONLY_TLDS, PLACEHOLDERS, REGISTRY_HOSTS, RESOURCE_ACRONYMS,
                    SUBUNIT_WORDS)

BLOCK = "block"
WARN = "warn"
_RANK = {BLOCK: 0, WARN: 1}

Hit = tuple[int, int, str, str]  # start, end, kind, severity

_WS = r"(?:[ \t]*\n[ \t]*|[ \t]+)"  # whitespace inside one paragraph
_LB = r"(?<![^\W_])"  # not preceded by a letter or digit
_RB = r"(?![^\W_])"  # not followed by a letter or digit
_RB_LOOSE = r"(?![^\W\d_])"  # not followed by a letter

# --- folding (independent of scrub._fold; keeps offsets 1:1) -----------------------------------

_SPECIAL = {"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
            "ı": "i", "\u2010": "-", "\u2011": "-", "\u2019": "'", "\u02bc": "'"}
_MULTI = {"ß": "ss", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "þ": "th", "Þ": "Th"}
_GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}


@lru_cache(maxsize=4096)
def _fold_char(ch: str) -> str:
    if ch.isascii():
        return ch
    if ch in _SPECIAL:
        return _SPECIAL[ch]
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return base if len(base) == 1 else ch


def _fold(text: str) -> str:
    return "".join(map(_fold_char, text))


def _plain(text: str) -> str:
    """Fully ASCII-folded form (may change length; used for names, never for offsets)."""
    return _fold("".join(_MULTI.get(c, c) for c in unicodedata.normalize("NFC", text)))


def _spellings(name: str) -> set[str]:
    name = unicodedata.normalize("NFC", " ".join(name.split()))
    german = "".join(_GERMAN.get(c, c) for c in name)
    return {_fold(name), _plain(name), _plain(german)}


def _lit(text: str) -> str:
    """Regex for a folded literal with flexible whitespace, hyphens and apostrophes."""
    out = re.escape(text)
    out = re.sub(r"(?:\\\s)+|\s+", lambda m: _WS, out)
    return out.replace(r"\-", "[-–]").replace("'", "['’]")


def _alt(sources: Iterable[str]) -> str:
    uniq = sorted({s for s in sources if s}, key=lambda s: (-len(s), s))
    return "(?:" + "|".join(uniq) + ")" if uniq else ""


# --- document-derived identifiers -------------------------------------------------------------

_PARTICLES = frozenset(
    "van von de der den del della dei di da do dos das du la le lo ter ten te zu zur vom al el "
    "bin ibn".split())
_SECOND_LEVEL = frozenset({"ac", "co", "edu", "gov", "org", "net", "com", "nhs", "res"})  # ac.uk, edu.au
_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}", re.IGNORECASE)


def _doc_lowercase_words(doc: Document) -> set[str]:
    words: set[str] = set()
    for section in doc.sections:
        for token in _plain(f"{section.title}\n{section.text}").split():
            if re.search(r"[@/:_]|[^\W\d_]\.[^\W\d_]", token):
                continue  # URLs, emails, file names
            for word in re.findall(r"[^\W\d_]+(?:['\-][^\W\d_]+)*", token):
                if word[0].islower():
                    words.add(word)
                    words.update(word.split("-"))
    return words


def _common(name: str, lowers: set[str]) -> bool:
    """Single ordinary word (particles aside); multi-word and hyphenated names never are."""
    parts = [p for p in re.split(r"[\s\-–]+", _plain(name).lower()) if p and p not in _PARTICLES]
    return len(parts) == 1 and (parts[0] in COMMON_WORDS or parts[0] in lowers)


def _author_surnames(author: Author, lowers: set[str]) -> list[str]:
    surname = " ".join((author.surname or author.given).split())
    if len(surname) < 2:
        return []
    tokens = surname.split()
    names = [surname]
    if tokens[0].lower() in _PARTICLES and len(tokens) > 1:
        names.append(surname[:1].upper() + surname[1:])
        core = [t for t in tokens if t.lower() not in _PARTICLES]
        if core:
            names.append(" ".join(tokens[tokens.index(core[0]):]))
    # Compound personal surnames are cited by one part or hyphenated ('Márquez', 'Garcia-Marquez').
    core = [t for t in tokens if t.lower() not in _PARTICLES]
    if author.given and len(core) >= 2:
        if len(core) == 2:
            names.append("-".join(core))
        names += [t for t in (core[0], core[-1]) if len(t) >= 3 and not _common(t, lowers)]
    return names


def _author_given(author: Author) -> tuple[list[str], str]:
    """(full given-name tokens, first initial) of an author."""
    if not author.surname:
        return [], ""
    tokens = re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", author.given)
    full = [t for t in tokens if len(t) > 1 and not (t.isupper() and len(t) <= 3)]
    initial = _plain(tokens[0])[:1].upper() if tokens else ""
    return full, initial if initial.isalpha() else ""


def _affiliated(name: str, doc: Document) -> bool:
    """The institution is named in the affiliations or front matter (not only in the body text)."""
    key = " ".join(name.split()).lower()
    return any(key in " ".join(t.split()).lower() for t in (*doc.affiliations, doc.front_matter))


def _institutions(doc: Document, affiliated_only: bool = False) -> list[str]:
    raw = [i for i in doc.institutions if not affiliated_only or _affiliated(i, doc)]
    for affiliation in doc.affiliations:
        raw += re.split(r"[,;\n]", affiliation)
    phrases = set()
    for item in raw:
        words = re.sub(r"^[\W\d_]+", "", item).split()
        while words and not words[0][0].isupper():
            words = words[1:]
        while words and not any(c.isalpha() for c in words[-1]):
            words = words[:-1]
        phrase = " ".join(words).strip(" .")
        if (len([w for w in words if any(c.isalpha() for c in w)]) >= 2
                and phrase.lower() not in NON_INSTITUTION_PHRASES):
            phrases.add(phrase)
    return sorted(phrases)


def _org_key(word: str) -> str:
    return _plain(word).lower().strip(".,;:'’")


def _shortened(phrase: str) -> set[str]:
    """Front cuts of an organisation name that still name it ('University of Michigan' from
    'University of Michigan Medical School'): an org keyword plus a specific capitalised word."""
    words = phrase.split()
    found = set()
    for n in range(2, len(words)):
        head = words[:n]
        keys = [_org_key(w) for w in head]
        is_org = [k in INSTITUTION_KEYWORDS or k.startswith("universit") for k in keys]
        specific = any(w[:1].isupper() and not org and k not in GENERIC_ORG_WORDS | NAME_FUNCTION_WORDS
                       for w, k, org in zip(head, keys, is_org, strict=True))
        has_org = any(is_org)
        if head[-1][:1].isupper() and has_org and specific:
            found.add(" ".join(head).rstrip(",;:"))
    return found


def _distinctive_words(phrases: Iterable[str], lowers: set[str]) -> set[str]:
    """Specific words of organisation names ('Wombatia', 'Planck', 'Pasteur'), which also give the
    institution away when it is reworded ('Wombatia University IRB', 'the Pasteur Institute')."""
    found = set()
    for phrase in phrases:
        words = re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", phrase)
        keys = [_org_key(w) for w in words]
        if not any(k in INSTITUTION_KEYWORDS or k.startswith("universit") for k in keys):
            continue  # a place ('Wombat City'), not an organisation name
        for word, key in zip(words, keys, strict=True):
            if (word[0].isupper() and len(word) >= 4 and key not in INSTITUTION_KEYWORDS
                    and not key.startswith("universit") and key not in GENERIC_ORG_WORDS | NAME_FUNCTION_WORDS
                    and not _common(word, lowers)
                    and not all(part.lower() in RESOURCE_ACRONYMS for part in re.split(r"[-–]", word))):
                found.add(word)
    return found


def _acronyms(phrases: Iterable[str], domains: Iterable[str]) -> set[str]:
    """Initials of organisation names (CSHL) and email-domain labels (UNIL), 3-8 letters; shared
    resources (NCBI, EBI) are science, not the authors' institution."""
    found = set()
    for phrase in phrases:
        words = re.findall(r"[^\W\d_]+", _plain(phrase))
        if words and words[0].lower() not in SUBUNIT_WORDS:
            initials = "".join(w[0] for w in words if w[0].isupper() and w.lower() not in NAME_FUNCTION_WORDS)
            if 3 <= len(initials) <= 6 and initials.lower() not in RESOURCE_ACRONYMS:
                found.add(initials)
    for domain in domains:
        labels = domain.split(".")
        keep = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
        label = labels[-keep]
        if label.isalpha() and 3 <= len(label) <= 8 and label not in RESOURCE_ACRONYMS:
            found.add(label.upper())
    return found


def _domains(doc: Document) -> list[str]:
    emails = list(doc.emails)
    for text in (doc.front_matter, *doc.affiliations, *(s.text for s in doc.sections)):
        emails += _EMAIL.findall(_fold(text))
    out = set()
    for email in emails:
        domain = email.rsplit("@", 1)[-1].strip(" .").lower()
        labels = domain.split(".")
        if len(labels) >= 2:
            out.add(domain)
            keep = 3 if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
            if len(labels) > keep:
                out.add(".".join(labels[-keep:]))
    return sorted(out)


# --- fixed patterns ---------------------------------------------------------------------------

_KNOWN_HOSTS = ("github.com", "github.io", "githubusercontent.com", "bitbucket.org",
                "codeberg.org", "sourceforge.net", "gitee.com", "galaxyproject.org",
                "doi.org", "orcid.org", *REGISTRY_HOSTS, *ARCHIVE_HOSTS, *DATABASE_HOSTS)
# Host prefixes need a lower-case label after the dot: 'gitlab.com', but not 'on GitLab. The'.
_KNOWN_PREFIXES = r"(?<![\w.])(?:gitlab|usegalaxy|dataverse)\.(?-i:[a-z0-9])"
_TLDS = "|".join(sorted(BARE_HOST_TLDS, key=len, reverse=True))
_PATH_TLDS = "|".join(sorted(PATH_ONLY_TLDS, key=len, reverse=True))
_LOWER_TLDS = "|".join(sorted(LOWERCASE_HOST_TLDS, key=len, reverse=True))

_FIXED: list[tuple[re.Pattern[str], str]] = [
    (_EMAIL, "email"),
    (re.compile(r"[a-z0-9._%+\-]+\s*[\[({<]\s*at\s*[\])}>]\s*[a-z0-9\-]+", re.IGNORECASE), "email"),
    (re.compile(r"(?<![\d-])\d{4}-\d{4}-\d{4}-\d{3}[\dX](?![\d-])", re.IGNORECASE), "orcid"),
    (re.compile(r"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]*://", re.IGNORECASE), "url"),
    (re.compile(r"(?<![\w.@/-])www\d{0,3}\.[a-z0-9]", re.IGNORECASE), "url"),
    (re.compile(rf"(?<![\w.@/:-])(?=[a-z0-9-]*[a-z])(?:[a-z0-9-]+\.)+(?:{_TLDS})(?![\w-])",
                re.IGNORECASE), "url"),
    (re.compile(rf"(?<![\w.@/:-])(?=[a-z0-9-]*[a-z])(?:[a-z0-9-]+\.)+(?:{_PATH_TLDS})/\S",
                re.IGNORECASE), "url"),
    (re.compile(rf"(?<![\w.@/:-])(?=[a-z0-9-]*[a-z])(?:[a-z0-9-]+\.)+(?:{_LOWER_TLDS})(?![\w-])"), "url"),
    (re.compile("|".join([*(re.escape(h) for h in _KNOWN_HOSTS), _KNOWN_PREFIXES]), re.IGNORECASE), "url"),
    (re.compile(r"(?<![\w/.\-])10\.\d{4,9}/\S"), "doi"),
    (re.compile(r"(?<![A-Za-z0-9])PMC\s?\d+"), "pmcid"),
    (re.compile(r"(?<![a-z0-9])(?:PMID|PubMed\s*ID)\s*:?\s*\d+", re.IGNORECASE), "pmid"),
    (re.compile(r"(?<![\w/])arxiv\s*:\s*[a-z\d]", re.IGNORECASE), "arxiv"),
]
# Host-like words with three or more labels and a two-letter TLD not handled above ('igc.gulbenkian.pt');
# file extensions ('data.tar.gz', 'hg38.chr1.fa') are not TLDs.
_FILE_EXTENSIONS = "gz|xz|bz|fa|fq|py|pl|sh|md|rs|js|ts|so|cc|mk|nf|db|gb|sv|rb|jl|hs|ml"
_HOST_LIKE = re.compile(rf"(?<![\w.@/:-])(?=[a-z0-9-]*[a-z])[a-z0-9-]+(?:\.[a-z0-9-]+)+"
                        rf"\.(?!(?:{_FILE_EXTENSIONS})(?![\w-]))[a-z]{{2}}(?![\w-])")
_CORRESPONDENCE = re.compile(
    r"^(?!#+[ \t])[ \t*†‡§¶#>\d]*(?:correspondence(?:[ \t]*:|[ \t]+(?:to|should|concerning|regarding)\b"
    r"|(?=[^\n]*@))|corresponding\b|to\s+whom\b)|\bcorresponding\s+authors?\b",
    re.IGNORECASE | re.MULTILINE)
_YEAR = re.compile(r"(?<![\w.,])((?:19|20)\d\d)(?:s|[a-f])?(?!\w)(?![.,]\d)")
_DATE_WORDS = frozenset("in since until till by during from after before around circa".split())
_ET_AL = r"\s+(?i:et\.?\s*al)" + _RB
_CITATION = (rf"(?={_ET_AL}|\s+(?:(?i:and)|&)\s+(?:[A-Z]|(?i:colleagues|co-?workers|co-?authors))"
             r"|,?\s*\(?\s*(?:1[89]|20)\d\d(?!\d)|\s*\[\d|['’]s\s+(?i:lab|laboratory|group|team)\b"
             r"|\s+(?i:lab|laboratory|group)\b)")
# Surnames that are ordinary words (May, Rice): only forms that cannot be a date or a phrase.
_REF_YEAR = r"(?:1[89]|20)\d\d(?!\d)"
_COMMON_CITATION = (rf"(?={_ET_AL}|\s*\[\d|['’]s\s+(?i:lab|laboratory|group|team)\b|\s+(?i:lab|laboratory|group)\b"
                    rf"|,?\s*\(\s*{_REF_YEAR}|,\s*{_REF_YEAR}"
                    rf"|\s+(?:(?i:and)|&)\s+(?:(?i:colleagues|co-?workers|co-?authors)\b"
                    rf"|[A-Z][\w'’-]+(?:\s+(?i:et\.?\s*al)\.?)?,?\s*\(?\s*{_REF_YEAR}))")


# 'Supplementary data are available at <Journal> online.' (publisher boilerplate naming the venue)
_ONLINE_VENUE = re.compile(r"\b(?:[Aa]vailable|[Aa]ccessible)\s+(?:at|from|on)\s+(?:[Tt]he\s+)?[A-Z][^\s.,;]*"
                           r"(?:\s+[^\s.,;]+){0,6}?\s+[Oo]nline\b")


def _quantity(text: str, start: int, end: int) -> bool:
    """'n = 2048', '50 %', '2048 bp', 'a total of 2031 genes': a number, not a calendar year."""
    before, after = text[:start].rstrip()[-1:], text[end:].lstrip()[:1]
    if (before and before in "=<>≤≥±×*+^~") or (after and after in "%±×"):
        return True
    unit = re.match(r"[ \t-]*([^\W\d_]+)", text[end:])
    prep = re.search(r"([^\W\d_]+)\W*$", text[:start])
    year_follows = bool(prep) and (prep.group(1).lower() in ("since", "until", "till", "during")
                                   or (prep.group(1)[0].isupper() and prep.group(1).lower() in _DATE_WORDS))
    return bool(unit) and unit.group(1).lower() in COUNT_WORDS and not year_follows


def _code_name(text: str, start: int, end: int) -> bool:
    """'scipy.io' and other module paths that look like a bare host."""
    m = re.fullmatch(r"([a-z0-9]+)\.[a-z]+", text[start:end], re.IGNORECASE)
    return bool(m) and m.group(1).lower() in CODE_NAMESPACES and text[end:end + 1] != "/"


def _table_cell(text: str, start: int, end: int) -> bool:
    """True when text[start:end] is the whole content of a markdown table cell."""
    return text[:start].rstrip(" \t")[-1:] == "|" and text[end:].lstrip(" \t")[:1] == "|"


def _placeholder_mask(text: str) -> str:
    pattern = "|".join(re.escape(p) for p in sorted(PLACEHOLDERS, key=len, reverse=True))
    return re.sub(pattern, lambda m: " " * len(m.group()), text)


# --- per-category pattern sources -------------------------------------------------------------

def _occurs(name: str, text: str) -> bool:
    """Prefilter: the leading letters of some spelling of `name` occur in `text`."""
    return any(re.match(r"[^\W\d_]*", v).group() in text for v in _spellings(name))


def _author_sources(doc: Document, lowers: set[str], text: str) -> dict[tuple[str, str], list[str]]:
    """Regex sources keyed by (kind, severity) for every author name form occurring in `text`."""
    sources: dict[tuple[str, str], list[str]] = defaultdict(list)
    for author in doc.authors:
        # Full names and citations of PDF (heuristic) authors block like any other; a bare surname
        # guessed from a PDF only warns.
        bare = WARN if author.heuristic else BLOCK
        full, initial = _author_given(author)
        for upper in (False, True):
            first = (full[0].upper() if upper else full[0]) if full else ""
            for name in _author_surnames(author, lowers):
                name = name.upper() if upper else name
                if not _occurs(name, text):
                    continue
                surname = _alt(_lit(v) for v in _spellings(name))
                forms = []
                if initial:
                    i = re.escape(initial)
                    sep = rf"(?:,{_WS}?|{_WS})"
                    forms += [rf"{i}\.(?:[ \t]?[-–]?[ \t]?[A-Z]\.)*{_WS}?{surname}{_RB_LOOSE}",
                              rf"{i}(?:[-–]?[A-Z]){{1,3}}{_WS}{surname}{_RB_LOOSE}",
                              rf"{surname}{sep}{i}\.",
                              rf"{surname}{sep}{i}(?:[-–]?[A-Z]){{1,3}}{_RB}",
                              rf"{surname}{sep}{i}"
                              r"(?=[ \t]*(?:[,;:)\]]|$)|\s*\(?(?:1[89]|20)\d\d(?!\d))"]
                    if initial not in "AI":
                        forms.append(rf"{i}{_WS}{surname}{_RB_LOOSE}")
                if first:
                    given = _alt(_lit(v) for v in _spellings(first))
                    forms += [rf"{given}(?:{_WS}(?:[A-Z]\.?|[A-Z][a-z]+)){{0,2}}{_WS}{surname}{_RB_LOOSE}",
                              rf"{surname},?{_WS}{given}{_RB}"]
                if forms:
                    sources[("author_name", BLOCK)].append(_LB + _alt(forms))
                common = _common(name, lowers)
                if len(name) == 2 or (upper and len(name) < 5):  # 'LEE', 'ALI', 'MAO-A' are acronyms
                    sources[("citation", BLOCK)].append(_LB + surname + f"(?={_ET_AL})")
                elif len(name) >= 3:
                    sources[("citation", BLOCK)].append(_LB + surname + (_COMMON_CITATION if common else _CITATION))
                    boundary = _RB_LOOSE if len(name) >= 5 else _RB
                    sources[("surname", WARN if common else bare)].append(_LB + surname + boundary)
        if author.heuristic:
            for token in full:
                if len(token) >= 3 and not _common(token, lowers) and _occurs(token, text):
                    for v in _spellings(token):
                        sources[("heuristic_author", WARN)].append(_LB + _lit(v) + _RB)
    return sources


def _document_sources(doc: Document, extra_terms: Sequence[str], lowers: set[str],
                      text: str) -> dict[tuple[str, str], list[str]]:
    sources = _author_sources(doc, lowers, text)
    institutions = _institutions(doc)
    for phrase in {*institutions, *(n for p in institutions for n in _shortened(p))}:
        for v in _spellings(phrase) | _spellings(phrase.upper()):
            sources[("institution", BLOCK)].append(_LB + _lit(v) + _RB)
    for word in _distinctive_words(institutions, lowers):
        for v in _spellings(word):
            sources[("institution", WARN)].append(_LB + _lit(v) + _RB)
    for acronym in _acronyms(_institutions(doc, affiliated_only=True), _domains(doc)):
        sources[("institution", WARN)].append(_LB + re.escape(acronym) + _RB + r"(?![ \t]+[Ll]icen[cs]e)")
    for domain in _domains(doc):
        sources[("domain", BLOCK)].append(rf"(?<![\w.@\-])(?i:{re.escape(domain)})(?![\w\-])")
    for term in extra_terms:
        if len(term.strip()) >= 2:
            for v in _spellings(term):
                sources[("term", BLOCK)].append(rf"{_LB}(?i:{_lit(v)}){_RB}")
    for venue in (doc.journal, doc.publisher):
        venue = re.sub(r"^the\s+", "", " ".join(venue.split()), flags=re.IGNORECASE)
        if len(venue) < 3:
            continue
        if len(venue.split()) > 1:
            words = [r"(?:&|and)" if w.lower() in {"&", "and"} else _alt(_lit(v) for v in _spellings(w))
                     for w in venue.split()]
            # case-insensitive; check() drops all-lowercase matches (plain prose)
            sources[("journal", BLOCK)].append(rf"{_LB}(?i:{_WS.join(words)}){_RB}")
        else:
            severity = WARN if _common(venue, lowers) else BLOCK
            for v in _spellings(venue) | _spellings(venue.upper()):
                sources[("journal", severity)].append(_LB + _lit(v) + _RB)
    title = re.findall(r"[^\W_]+", _fold(doc.title))
    size = min(6, len(title))
    if size >= 4:
        for i in range(len(title) - size + 1):
            window = r"[\W_]{1,6}".join(re.escape(w) for w in title[i:i + size])
            sources[("title", WARN)].append(rf"{_LB}(?i:{window}){_RB}")
    for line in doc.front_matter.splitlines()[:300]:
        words = line.split()
        if len(words) >= 5:
            sources[("front_matter", WARN)].append(_lit(_fold(" ".join(words))))
    ids = [re.sub(r"\D", "", i) for i in (doc.pmid, doc.pmcid)]
    for digits in ids:
        if len(digits) >= 6:
            sources[("pmid", BLOCK)].append(rf"(?<![\w.]){digits}(?!\d)")
    for orcid in doc.orcids:
        compact = re.sub(r"[^\dX]", "", orcid.upper())
        if len(compact) == 16:
            sources[("orcid", BLOCK)].append(rf"(?<!\d){compact}(?!\d)")
    return sources


# --- scanning ---------------------------------------------------------------------------------

def _find(pattern: re.Pattern[str], text: str, kind: str, severity: str) -> list[Hit]:
    return [(m.start(), m.end(), kind, severity) for m in pattern.finditer(text) if m.end() > m.start()]


def _dedupe(hits: Iterable[Hit]) -> list[Hit]:
    """Drop hits nested inside another hit of equal or higher severity."""
    kept: list[Hit] = []
    for hit in sorted(set(hits), key=lambda h: (_RANK[h[3]], h[0], -h[1], h[2])):
        if any(k[0] <= hit[0] and hit[1] <= k[1] and _RANK[k[3]] <= _RANK[hit[3]] for k in kept):
            continue
        kept.append(hit)
    return sorted(kept, key=lambda h: (h[0], h[1], _RANK[h[3]], h[2]))


def _context(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - 40):start] + text[start:end][:80] + text[end:end + 40]
    return " ".join(snippet.split())


def check(scrubbed_text: str, doc: Document, extra_terms: Sequence[str] = ()) -> list[Leak]:
    """Scan scrubbed text for anything that could identify the paper; see module docstring."""
    text = _fold(_placeholder_mask(scrubbed_text))
    hits: list[Hit] = []
    for pattern, kind in _FIXED:
        hits += [h for h in _find(pattern, text, kind, BLOCK) if not (kind == "url" and _code_name(text, *h[:2]))]
    hits += _find(_HOST_LIKE, text, "url", WARN)
    lowers = _doc_lowercase_words(doc)
    for (kind, severity), sources in _document_sources(doc, extra_terms, lowers, text).items():
        found = _find(re.compile(_alt(sources), re.MULTILINE), text, kind, severity)
        if kind == "journal":
            found = [h for h in found if any(c.isupper() for c in text[h[0]:h[1]])]
        hits += found
    hits += _find(_CORRESPONDENCE, text, "correspondence", WARN)
    hits += _find(_ONLINE_VENUE, text, "journal", WARN)
    if doc.year:
        hits += [(m.start(), m.end(), "year", WARN) for m in _YEAR.finditer(text)
                 if int(m.group(1)) > doc.year and not _table_cell(text, m.start(), m.end())
                 and not _quantity(text, m.start(), m.end())]
    leaks = [Leak(kind=kind, severity=severity, match=" ".join(scrubbed_text[s:e].split())[:200],
                  context=_context(scrubbed_text, s, e))
             for s, e, kind, severity in _dedupe(hits)]
    if doc.source_type == "pdf" and not doc.authors and not extra_terms:
        # Nothing to look for: a PDF whose author names could not be found is not checked blind.
        leaks.insert(0, Leak("no_authors", BLOCK, "", "no author names found in the PDF; list them as extra "
                                                      "terms to redact"))
    return leaks


def blocking(leaks: Iterable[Leak]) -> bool:
    """True when any leak must keep the paper from the reviewer."""
    return any(leak.severity == BLOCK for leak in leaks)
