"""Parse JATS XML (Europe PMC / PMC) into a bars Document.

Front matter is harvested for identifiers (title, authors, affiliations, emails, ORCIDs, journal,
year, ids) but never rendered; abstracts, availability statements (wherever the publisher put them:
body, back, author-notes, front `notes` or PLoS `custom-meta`), body and back matter become
classified Sections in reading order. Link targets are kept as `text (href)` so that they can be
scrubbed later instead of silently disappearing.
"""

from __future__ import annotations

import html.entities
import re
from collections.abc import Callable, Iterable

from lxml import etree

from .model import Author, Document, Section

MAX_TABLE_ROWS = 200
MAX_SPAN = 50  # cap on rowspan/colspan values from untrusted XML

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ORCID_RE = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")
ORCID_URL_RE = re.compile(r"orcid\.org/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.I)

# ---------------------------------------------------------------------------------------------
# Section title classification (shared with pdf.py)
# ---------------------------------------------------------------------------------------------

_KIND_RULES = (
    ("refs", r"references?|bibliography|literature cited|works cited|cited literature|references and notes"
             r"|notes and references|reference list|references cited"),
    ("ack", r"acknowle?dge?ments?|acknowledge?ments? and thanks|thanks"),
    ("funding", r"funding|funding (?:sources?|information|support|details|disclosure)|sources? of funding"
                r"|financial (?:support|disclosures?)|grant (?:support|information)|funders?"
                r"|role of (?:the )?funding sources?|supported by"),
    ("contrib", r"authors?'?s?'? contributions?|contributions? of (?:the )?authors?|author(?:ship)? contributions?"
                r"|contributors(?:hip)?|contributorship|contributor roles|author roles|authors'? roles"
                r"|credit(?: authorship)?(?: contributions?)?(?: author)?|author information|authors'? information"
                r"|contributor information|equal contribution|participating researchers"),
    ("coi", r"(?:(?:declaration|statement|disclosure)s? of )?(?:potential |possible )?"
            r"(?:competing|conflicting|conflicts? of)(?: financial)? interests?(?: disclosures?| declarations?)?"
            r"|declarations? of interests?|(?:financial )?disclosures?|duality of interests?|coi"),
)
_KIND_RES = tuple((kind, re.compile(rf"(?:{pattern})")) for kind, pattern in _KIND_RULES)
_AVAILABILITY_RE = re.compile(
    r"^availability\b|^(?:data|code|software|materials?|resources?|reagents?|sequences?|scripts?)\b"
    r"(?:.*\b(?:availab\w*|accessib\w*|deposition|deposited|sharing|access)\b)"
    r"|^accession (?:numbers?|codes?)$|^database accessions?$|^(?:web )?resources$|^urls?$"
    r"|^(?:data and code|code and data|data and software|software and data)$"
)
_ATTR_KINDS = {
    "con": "contrib", "contrib": "contrib", "equal": "contrib", "participating-researchers": "contrib",
    "contrib-info": "contrib",  # Europe PMC "Contributor Information": corresponding authors and emails
    "conflict": "coi", "coi": "coi", "competing-interests": "coi",
    "financial-disclosure": "funding", "supported-by": "funding", "funding-information": "funding",
    "data-availability": "availability", "data_availability": "availability", "availability": "availability",
    "ack": "ack", "acknowledgment": "ack", "acknowledgement": "ack",
}
# Europe PMC-generated secs that only repeat other content (the supplementary file list).
_SKIP_SEC_TYPES = frozenset({"associated-data"})
_DEFAULT_TITLES = {
    "availability": "Data availability", "ack": "Acknowledgments", "funding": "Funding",
    "contrib": "Author contributions", "coi": "Competing interests", "refs": "References", "back": "Notes",
}


def normalise_title(title: str) -> str:
    """Lower-case a heading and strip numbering, punctuation and a trailing 'statement'."""
    t = title.lower().replace("\u2019", "'").replace("\u2018", "'").replace("&", " and ")
    t = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[ivx]+[.)]|[a-z][.)])\s+", "", t)
    t = re.sub(r"(?<=\w)[-_](?=\w)", " ", t)  # attribute style: "financial-disclosure"
    t = re.sub(r"\s+", " ", t).strip(" \t:.;,-\u2013\u2014*")
    return re.sub(r" statements?$", "", t)


def classify_title(title: str) -> str | None:
    """Map a section title (or sec-type/fn-type style label) to a special section kind, else None."""
    t = normalise_title(title)
    if not t or len(t.split()) > 15:  # a sentence, not a heading
        return None
    kind = _classify_normalised(t)
    if kind or not re.search(r",|;| and |/", t):
        return kind
    parts = [p for p in re.split(r"\s*(?:,|;|\band\b|/)\s*", t) if p]
    kinds = [_classify_normalised(p) for p in parts]
    if "availability" in kinds:
        return "availability"
    if kinds and all(kinds):
        return kinds[0]
    return None


def _classify_normalised(t: str) -> str | None:
    if _AVAILABILITY_RE.search(t):
        return "availability"
    for kind, regex in _KIND_RES:
        if regex.fullmatch(t):
            return kind
    return None


def _attr_kind(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    return _ATTR_KINDS.get(v) or classify_title(re.sub(r"[-_|]+", " ", v))


_INSTITUTION_KW = (r"University|Universit[éäày]|Universidad|Universidade|Università|Institute|Institut|Instituto"
                   r"|College|Hospital|Center|Centre|Laboratory|Laboratories|School|Department|Faculty|Academy"
                   r"|Foundation|Clinic|Museum|Observatory|Polytechnic|Consortium|Division")
_CAP_WORD = r"[A-Z](?:[\w'\u2019&-]|\.(?=\w))*"  # internal dots only ("U.S"), never a sentence end
_INSTITUTION_RUN_RE = re.compile(
    rf"\b{_CAP_WORD}(?:[ \t]+(?:(?:of|for|and|the|de|du|des|di|der|für|del|da|&|in|on)[ \t]+)*{_CAP_WORD})*"
)
_INSTITUTION_KW_RE = re.compile(rf"\b(?:{_INSTITUTION_KW})\b")


def institution_candidates(text: str) -> list[str]:
    """Capitalised phrases of >= 2 words containing an institution keyword (e.g. 'University of X')."""
    found: list[str] = []
    for match in _INSTITUTION_RUN_RE.finditer(text):
        phrase = match.group(0)
        parts = re.split(r"\s+(?:and|&)\s+", phrase)
        # "Department of Ecology and Evolution" is one name; "X and the University of Y" is two.
        whole = len(parts) == 1 or (bool(_INSTITUTION_KW_RE.search(parts[0]))
                                    and not re.search(r"\s(?:and|&)\s+the\s", phrase))
        for candidate in ([phrase] if whole else []) + (parts if len(parts) > 1 else []):
            candidate = re.sub(r"^(?:(?:The|the|A|An|At|In|From|By|We|Our)\s+)+", "", candidate).strip(" .,&-")
            candidate = re.sub(r"\s+(?:of|for|and|the|de|du|des|di|der|für|del|da|&|in|on)$", "", candidate)
            if (len(candidate.split()) >= 2 and _INSTITUTION_KW_RE.search(candidate)
                    and candidate not in found):
                found.append(candidate)
    return found


# ---------------------------------------------------------------------------------------------
# XML loading helpers
# ---------------------------------------------------------------------------------------------

_XML_BUILTIN_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

# Container elements rendered as their own Sections when they appear inside a section.
_STRUCT_TAGS = frozenset({"sec", "ref-list", "fn-group", "glossary", "app", "app-group", "ack", "notes", "bio"})
# Elements rendered as separate markdown blocks rather than inline text.
_BLOCK_TAGS = frozenset({
    "p", "list", "def-list", "disp-formula", "disp-formula-group", "fig", "fig-group", "table-wrap",
    "table-wrap-group", "table", "array", "supplementary-material", "boxed-text", "disp-quote", "statement",
    "preformat", "code", "speech", "verse-group", "chem-struct-wrap", "sec", "ref-list", "fn-group", "fn",
    "glossary", "app", "app-group", "ack", "notes", "bio", "title", "caption", "media", "graphic",
    "alternatives", "address",
})
_SKIP_TAGS = frozenset({
    "object-id", "graphic", "inline-graphic", "media", "alt-text", "long-desc", "permissions", "sec-meta",
    "kwd-group", "processing-meta", "annotation", "annotation-xml", "private-char", "glyph-data",
})
_LINK_TAGS = frozenset({"ext-link", "uri", "a", "self-uri", "inline-supplementary-material", "related-object"})
_MAIN_ABSTRACT_TYPES = frozenset({"", "abstract", "main", "structured", "summary", "author-summary",
                                  "executive-summary", "plain-language-summary", "lay-summary"})


def _parse_xml(xml_bytes: bytes) -> etree._Element:
    """Parse untrusted XML without entity expansion, DTD loading or network access."""
    def numeric(match: re.Match) -> bytes:
        name = match.group(1).decode("ascii")
        if name in _XML_BUILTIN_ENTITIES or name not in html.entities.name2codepoint:
            return match.group(0)
        return b"&#%d;" % html.entities.name2codepoint[name]

    # HTML named entities (e.g. &nbsp;) are undefined without the DTD; turn them into char refs.
    data = re.sub(rb"&([A-Za-z][A-Za-z0-9]*);", numeric, xml_bytes)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, load_dtd=False,
                             dtd_validation=False, remove_comments=True, remove_pis=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"invalid JATS XML: {exc}") from exc
    if root is None:
        raise ValueError("invalid JATS XML: empty document")
    _strip_namespaces(root)
    if root.tag != "article":
        article = next(root.iter("article"), None)
        if article is None:
            raise ValueError(f"not a JATS article (root element <{root.tag}>)")
        root = article
    return root


def _strip_namespaces(root: etree._Element) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for name in [n for n in el.attrib if "}" in n]:
            value = el.attrib.pop(name)
            el.attrib[name.split("}", 1)[1]] = value
    etree.cleanup_namespaces(root)


def _ws(text: str) -> str:
    """Collapse whitespace (incl. NBSP/thin spaces) and drop zero-width characters."""
    text = re.sub(r"[\u200b-\u200d\u2060\ufeff\u00ad]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _tag(node) -> str:
    return node.tag if isinstance(node.tag, str) else ""


def _plain(el: etree._Element | None, skip: Iterable[str] = ("xref", "fn", "label")) -> str:
    """Whitespace-normalised text of an element, ignoring `skip` children (tails are kept)."""
    if el is None:
        return ""
    skip = frozenset(skip) | _SKIP_TAGS

    def walk(node, root: bool = False) -> str:
        if isinstance(node, etree._Entity):
            return _entity_text(node)
        if not isinstance(node.tag, str) or (node.tag in skip and not root):
            return ""
        if node.tag == "break":
            return " "
        parts = [node.text or ""]
        for child in node:
            parts.append(walk(child))
            parts.append(child.tail or "")
        return " ".join(parts) if node.tag in _BLOCK_TAGS else "".join(parts)

    return _fix_spacing(_ws(walk(el, root=True)))


def _spaced(el: etree._Element | None, skip: Iterable[str] = ("label", "institution-id")) -> str:
    """Text with a space between every text node (for structured data like affiliations and refs)."""
    if el is None:
        return ""
    skip = frozenset(skip) | _SKIP_TAGS
    parts: list[str] = []

    def walk(node, root: bool = False) -> None:
        if not isinstance(node.tag, str) or (node.tag in skip and not root):
            return
        parts.append(node.text or "")
        for child in node:
            walk(child)
            parts.append(child.tail or "")

    walk(el, root=True)
    return _fix_spacing(_ws(" ".join(parts)))


def _fix_spacing(text: str) -> str:
    text = re.sub(r"\s+([,.;:)\]])", r"\1", text)
    return re.sub(r"([(\[])\s+", r"\1", text)


def _entity_text(node) -> str:
    name = (node.text or "").strip("&;")
    code = html.entities.name2codepoint.get(name)
    return chr(code) if code else ""


def _clean_tex(tex: str) -> str:
    """Extract the math from PMC-style tex-math (which often wraps a whole LaTeX document)."""
    if m := re.search(r"\\begin\{document\}(.*?)\\end\{document\}", tex, re.S):
        tex = m.group(1)
    tex = tex.strip()
    while len(tex) > 1 and tex[0] == "$" and tex[-1] == "$":
        tex = tex[1:-1].strip()
    return _ws(tex)


# ---------------------------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------------------------

class _Renderer:
    """Turns JATS block/inline content into markdown; collects ingest notes."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.figures = 0
        self.tables = 0

    # -- mixed content ------------------------------------------------------------------------

    def mixed(self, el: etree._Element, skip: Iterable[str] = (),
              on_struct: Callable[[etree._Element], None] | None = None,
              out: list[str] | None = None) -> list[str]:
        """Render children: inline runs become paragraphs, block children their own blocks.

        Structural children (nested sections) go to `on_struct` instead; blocks are appended to
        `out` as they are produced, so the callback sees everything that precedes it.
        """
        skip = frozenset(skip)
        blocks: list[str] = out if out is not None else []
        buf: list[str] = [el.text or ""]

        def flush() -> None:
            text = _ws("".join(buf))
            buf.clear()
            if text:
                blocks.append(text)

        for child in el:
            tag = _tag(child)
            if tag and tag in skip:
                pass
            elif on_struct is not None and tag in _STRUCT_TAGS:
                flush()
                on_struct(child)
            elif tag in _BLOCK_TAGS:
                flush()
                blocks.extend(self.block(child))
            else:
                buf.append(self.inline(child))
            buf.append(child.tail or "")
        flush()
        return blocks

    def inline(self, node) -> str:
        """Inline text of a node (without its tail)."""
        if isinstance(node, etree._Entity):
            return _entity_text(node)
        tag = _tag(node)
        if not tag or tag in _SKIP_TAGS:
            return ""
        if tag in _LINK_TAGS:
            return self._link(node)
        if tag == "sup":
            return self._sup(node)
        if tag == "inline-formula":
            return self._formula_text(node, inline=True)
        if tag in ("tex-math", "math"):
            return self._formula_text(node, inline=True)
        if tag == "break":
            return " "
        if tag == "fn":
            return " (" + " ".join(self.mixed(node, skip={"label"})) + ") "
        if tag in _BLOCK_TAGS:
            return " " + " ".join(self.block(node)) + " "
        return self._inline_children(node)

    def _inline_children(self, node) -> str:
        parts = [node.text or ""]
        for child in node:
            parts.append(self.inline(child))
            parts.append(child.tail or "")
        return "".join(parts)

    def _link(self, node) -> str:
        text = _ws(self._inline_children(node))
        href = (node.get("href") or "").strip()
        if not href or _LOCAL_HREF_RE.match(href):
            return text
        if _href_in_text(href, text):
            # 'wombatia.it' linking to https://wombatia.it: emit the full URL so it is seen as a link
            return href if _bare_form(text) == _bare_form(href) and "://" in href else text
        return f"{text} ({href})" if text else href

    def _sup(self, node) -> str:
        inner = _ws(self._inline_children(node))
        if not inner:
            return ""
        children = [c for c in node if _tag(c)]
        if children and all(c.tag == "xref" for c in children):
            return f"[{inner}]"  # citation markers
        return f"^{inner}" if " " not in inner else f"^({inner})"

    # -- blocks -------------------------------------------------------------------------------

    def block(self, el: etree._Element) -> list[str]:
        tag = el.tag
        if tag in ("p", "fn", "address", "caption"):
            return self.mixed(el, skip={"label"})
        if tag == "list":
            return self._list(el)
        if tag == "def-list":
            return self._def_list(el)
        if tag == "disp-formula":
            return [self._formula_text(el, inline=False)]
        if tag in ("fig", "chem-struct-wrap"):
            return [self._figure(el)]
        if tag == "table-wrap":
            return self._table_wrap(el)
        if tag in ("table", "array"):
            return self._table_blocks(el, label="")
        if tag == "supplementary-material":
            return [self._supplement(el)]
        if tag in ("fig-group", "table-wrap-group", "disp-formula-group"):
            caption = self._caption(el)
            blocks = [caption] if caption else []
            for child in el:
                if _tag(child) in _BLOCK_TAGS and child.tag != "caption":
                    blocks.extend(self.block(child))
            return blocks
        if tag == "disp-quote":
            return ["\n".join("> " + line for line in b.splitlines()) for b in self.mixed(el, skip={"label"})]
        if tag in ("preformat", "code"):
            return ["```\n" + "".join(el.itertext()).strip("\n") + "\n```"]
        if tag == "alternatives":
            return self._alternatives(el)
        if tag in ("graphic", "media"):
            return []
        if tag == "title":
            text = _plain(el)
            return [f"**{text}**"] if text else []
        if tag == "ref-list":
            return self._refs(el)
        # sec, boxed-text, statement, notes, app, ack, glossary, bio, speech, verse-group, fn-group ...
        heading = " ".join(p for p in (_plain(el.find("label")), _plain(el.find("title"))) if p)
        blocks = [f"**{heading}**"] if heading else []
        return blocks + self.mixed(el, skip={"label", "title"})

    def _alternatives(self, el: etree._Element) -> list[str]:
        for wanted in ("table", "tex-math", "math"):
            found = el.find(wanted)
            if found is not None:
                if wanted == "table":
                    return self._table_blocks(found, label="")
                return [self._formula_text(found, inline=False)]
        return self.mixed(el)

    def _list(self, el: etree._Element) -> list[str]:
        ordered = (el.get("list-type") or "").lower() in (
            "order", "ordered", "number", "alpha-lower", "alpha-upper", "roman-lower", "roman-upper")
        lines: list[str] = []
        title = _plain(el.find("title"))
        n = 0
        for item in el:
            if _tag(item) != "list-item":
                continue
            n += 1
            label = _plain(item.find("label"))
            marker = f"{n}." if ordered and not label else "-"
            blocks = self.mixed(item, skip={"label"})
            if not blocks:
                continue
            first = f"{marker} {label + ' ' if label else ''}{blocks[0]}"
            lines.append(first.replace("\n", "\n   "))
            lines.extend("   " + b.replace("\n", "\n   ") for b in blocks[1:])
        out = [f"**{title}**"] if title else []
        return out + (["\n".join(lines)] if lines else [])

    def _def_list(self, el: etree._Element) -> list[str]:
        lines = []
        for item in el.iter("def-item"):
            term = _plain(item.find("term"))
            definition = " ".join(" ".join(self.mixed(d)) for d in item.findall("def"))
            lines.append(f"- {term}: {definition}" if term else f"- {definition}")
        title = _plain(el.find("title"))
        return ([f"**{title}**"] if title else []) + (["\n".join(lines)] if lines else [])

    def _formula_text(self, el: etree._Element, inline: bool) -> str:
        tex = el if el.tag == "tex-math" else el.find(".//tex-math")
        if tex is not None and "".join(tex.itertext()).strip():
            body = _clean_tex("".join(tex.itertext()))
            text = f"${body}$" if inline else f"$$ {body} $$"
        else:
            math = el if el.tag == "math" else el.find(".//math")
            text = _ws(_mathml(math)) if math is not None else _ws(self._inline_children(el))
        label = _plain(el.find("label")) if el.tag == "disp-formula" else ""
        return f"{text} {label}".strip()

    def _caption(self, el: etree._Element) -> str:
        cap = el.find("caption")
        parts = []
        if cap is not None:
            title_el = cap.find("title")
            title = _fix_spacing(_ws(self._inline_children(title_el))) if title_el is not None else ""
            if title:
                parts.append(title if title[-1] in ".!?:" else title + ".")
            parts.extend(" ".join(self.mixed(c)) for c in cap if _tag(c) not in ("title", ""))
        parts.extend(" ".join(self.mixed(p)) for p in el.findall("p"))
        parts.extend(_plain(a) for a in el.findall("attrib"))
        return _ws(" ".join(p for p in parts if p))

    def _figure(self, el: etree._Element) -> str:
        self.figures += 1
        label = _plain(el.find("label"))
        if number := _label_number(label, r"fig(?:ure)?s?"):
            name = f"Figure {number}"
        else:
            name = label.rstrip(".:") or f"Figure {self.figures}"
        return _ws(f"**{name}.** {self._caption(el)}")

    def _table_wrap(self, el: etree._Element) -> list[str]:
        self.tables += 1
        label = _plain(el.find("label"))
        number = _label_number(label, r"tables?")
        name = f"Table {number}" if number else (label.rstrip(".:") if label else f"Table {self.tables}")
        blocks = [_ws(f"**{name}.** {self._caption(el)}")]
        tables = [t for t in el.iter("table")]
        for table in tables:
            blocks.extend(self._table_blocks(table, label=name))
        if not tables:
            blocks.append("(table content not available as text)")
        for foot in el.findall("table-wrap-foot"):
            blocks.extend(self.mixed(foot, skip={"title"}))
        return blocks

    def _table_blocks(self, table: etree._Element, label: str) -> list[str]:
        rows = _expand_rows(table, self._cell_text)
        if not rows:
            return []
        head = [cells for cells, is_head in rows if is_head]
        body = [cells for cells, is_head in rows if not is_head]
        if not head:
            head, body = [body[0]], body[1:]
        width = max(len(cells) for cells, _ in rows)
        header = []
        for i in range(width):
            parts: list[str] = []
            for cells in head:
                value = cells[i] if i < len(cells) else ""
                if value and value not in parts:
                    parts.append(value)
            header.append(" / ".join(parts))
        truncated = len(body) - MAX_TABLE_ROWS
        if truncated > 0:
            self.notes.append(f"{label or 'table'}: truncated to {MAX_TABLE_ROWS} of {len(body)} rows")
            body = body[:MAX_TABLE_ROWS]
        lines = [_md_row(header, width), "| " + " | ".join(["---"] * width) + " |"]
        lines.extend(_md_row(cells, width) for cells in body)
        out = ["\n".join(lines)]
        if truncated > 0:
            out.append(f"(Table truncated: first {MAX_TABLE_ROWS} of {MAX_TABLE_ROWS + truncated} rows shown.)")
        return out

    def _cell_text(self, cell: etree._Element) -> str:
        return _ws(" ".join(self.mixed(cell))).replace("|", "\\|")

    def _supplement(self, el: etree._Element) -> str:
        label = _plain(el.find("label")).rstrip(".:") or "Supplementary material"
        # BMC/Springer put the caption inside the <media> child.
        caption = self._caption(el) or " ".join(filter(None, (self._caption(m) for m in el.findall("media"))))
        return _ws(f"**{label}.** {caption} (supplementary file not provided)")

    def _refs(self, el: etree._Element) -> list[str]:
        lines = []
        for ref in el:
            tag = _tag(ref)
            if tag == "ref":
                label = _plain(ref.find("label"))
                text = " ".join(_spaced(c) for c in ref if _tag(c) not in ("label", ""))
                lines.append(_ws(f"- {label} {text}" if label else f"- {text}"))
            elif tag == "ref-list":
                lines.extend(self._refs(ref))
            elif tag == "p":
                lines.extend(self.mixed(ref))
        return ["\n".join(lines)] if lines else []


# In-document anchors and packaged file names (e.g. "pone.0275623.s001.xlsx", which would leak the
# article number) are not link targets worth keeping.
_LOCAL_HREF_RE = re.compile(
    r"^#|^[\w.-]+\.(?:xlsx?|csv|tsv|docx?|pptx?|pdf|zip|gz|tar|txt|tiff?|jpe?g|png|gif|eps|svg|mp4|mov|avi"
    r"|xml|json|r|py)$", re.I)


def _href_in_text(href: str, text: str) -> bool:
    """True when the link target is already visible in the link text (ignoring scheme/www/mailto)."""
    low = text.lower()
    h = re.sub(r"^mailto:", "", href.strip(), flags=re.I)
    candidates = {h, re.sub(r"^[a-z][a-z0-9+.-]*://", "", h, flags=re.I)}
    candidates |= {re.sub(r"^www\.", "", c, flags=re.I) for c in candidates}
    candidates |= {re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", c, flags=re.I) for c in candidates}
    return any(c.rstrip("/").lower() in low for c in candidates if c.rstrip("/"))


def _bare_form(url: str) -> str:
    """A link without scheme, 'www.' and trailing slash, lower-cased ('https://www.x.org/' -> 'x.org')."""
    return re.sub(r"^(?:[a-z][a-z0-9+.-]*://)?(?:www\.)?", "", url.strip(), flags=re.I).rstrip("/").lower()


def _label_number(label: str, word: str) -> str:
    m = re.match(rf"^\s*{word}\.?\s*(\S.*?)[.:]?\s*$", label, re.I)
    return m.group(1) if m else ""


def _md_row(cells: list[str], width: int) -> str:
    padded = list(cells) + [""] * (width - len(cells))
    return "| " + " | ".join(padded) + " |"


def _span(value: str | None) -> int:
    try:
        return min(max(int(value or 1), 1), MAX_SPAN)
    except ValueError:
        return 1


def _expand_rows(table: etree._Element,
                 cell_text: Callable[[etree._Element], str]) -> list[tuple[list[str], bool]]:
    """Flatten a table into rows of cell strings, repeating rowspans and header colspans."""
    raw: list[tuple[etree._Element, bool]] = []
    for part in table:
        tag = _tag(part)
        if tag == "tr":
            raw.append((part, False))
        elif tag in ("thead", "tbody", "tfoot"):
            raw.extend((tr, tag == "thead") for tr in part if _tag(tr) == "tr")
    if raw and not any(is_head for _, is_head in raw):
        # No <thead>: treat leading all-<th> rows as the header.
        for i, (tr, _) in enumerate(raw):
            cells = [c for c in tr if _tag(c) in ("td", "th")]
            if not cells or any(c.tag != "th" for c in cells):
                break
            raw[i] = (tr, True)
    rows: list[tuple[list[str], bool]] = []
    pending: dict[int, tuple[str, int]] = {}  # column -> (text, rows still to fill)
    for tr, is_head in raw:
        row: dict[int, str] = {}
        for col, (text, left) in list(pending.items()):
            row[col] = text
            if left <= 1:
                del pending[col]
            else:
                pending[col] = (text, left - 1)
        col = 0
        for cell in tr:
            if _tag(cell) not in ("td", "th"):
                continue
            while col in row:
                col += 1
            text, rowspan, colspan = cell_text(cell), _span(cell.get("rowspan")), _span(cell.get("colspan"))
            for k in range(colspan):
                row[col + k] = text if (k == 0 or is_head) else ""
                if rowspan > 1:
                    pending[col + k] = (row[col + k], rowspan - 1)
            col += colspan
        if row:
            rows.append(([row.get(i, "") for i in range(max(row) + 1)], is_head))
    return rows


_SPACED_OPERATORS = frozenset("=<>+\u2264\u2265\u2248\u00b1\u2212\u00d7")
# Over-script accents rendered as functions: x with a hat over it -> hat(x).
_ACCENTS = {"^": "hat", "\u02c6": "hat", "\u0302": "hat", "~": "tilde", "\u02dc": "tilde", "\u0303": "tilde",
            "\u00af": "bar", "\u203e": "bar", "\u0304": "bar", "_": "bar", "\u02d9": "dot", "\u0307": "dot",
            "\u2192": "vec", "\u20d7": "vec"}
# Large operators take their limits as scripts; a space after them keeps the operand apart.
_LARGE_OPERATORS = frozenset("\u2211 \u220f \u2210 \u222b \u222c \u222d \u222e \u22c3 \u22c2 \u2a01 \u2a02 "
                             "lim max min sup inf argmax argmin".split())


def _mathml(el: etree._Element) -> str:
    """Rough linear text for MathML (fractions, scripts, roots, fences, tables); enough for a reviewer."""
    tag = _tag(el)
    if not tag or tag in _SKIP_TAGS:
        return ""
    kids = [c for c in el if _tag(c)]
    sub = [_mathml(k) for k in kids]

    def group(text: str) -> str:
        text = text.strip()
        return text if len(text) <= 1 else f"({text})"

    def scripts(pairs: list[str]) -> str:
        """'_a^b' for (sub, sup) pairs of mmultiscripts; empty slots (<none/>) are skipped."""
        out = ""
        for low, high in zip(pairs[::2], pairs[1::2], strict=False):
            out += (f"_{group(low)}" if low.strip() else "") + (f"^{group(high)}" if high.strip() else "")
        return out

    if tag in ("mi", "mn", "mtext", "ms"):
        return el.text or "".join(el.itertext())
    if tag == "mo":
        op = (el.text or "").strip()
        return f" {op} " if op in _SPACED_OPERATORS else op
    if tag == "mspace":
        return " "
    if tag == "mfrac" and len(sub) == 2:
        return f"{group(sub[0])}/{group(sub[1])}"
    if tag == "mfenced":  # deprecated but common (Springer/BMC): grouping brackets must survive
        # Children are arguments split by commas, except that publishers fence whole expressions
        # without an <mrow> ('(a - b)'); those hold operators and get no separators.
        default = "" if any(_tag(k) == "mo" for k in kids) else ","
        separators = re.sub(r"\s+", "", el.get("separators", default))
        inner = sub[0] if sub else ""
        for i, part in enumerate(sub[1:]):
            inner += (separators[min(i, len(separators) - 1)] if separators else "") + part
        if len(kids) == 1 and _tag(kids[0]) == "mtable":
            inner = inner[1:-1]  # the fence replaces the matrix brackets
        return f"{el.get('open', '(')}{inner}{el.get('close', ')')}"
    if tag in ("mover", "munderover") and sub and sub[-1].strip() in _ACCENTS:
        accented = f"{_ACCENTS[sub[-1].strip()]}({sub[0].strip()})"
        return accented if tag == "mover" else f"{accented}_{group(sub[1])}"
    space = " " if sub and sub[0].strip() in _LARGE_OPERATORS else ""
    if tag in ("msub", "munder") and len(sub) == 2:
        return f"{sub[0]}_{group(sub[1])}{space}"
    if tag in ("msup", "mover") and len(sub) == 2:
        return f"{sub[0]}^{group(sub[1])}"
    if tag in ("msubsup", "munderover") and len(sub) == 3:
        return f"{sub[0]}_{group(sub[1])}^{group(sub[2])}{space}"
    if tag == "mmultiscripts" and sub:
        tags = [_tag(k) for k in kids]
        cut = tags.index("mprescripts") if "mprescripts" in tags else len(sub)
        return scripts(sub[cut + 1:]) + sub[0] + scripts(sub[1:cut])
    if tag == "mtable":
        rows = [", ".join(cell.strip() for cell in _mathml_row(row)) for row in kids
                if _tag(row) in ("mtr", "mlabeledtr")]
        return "[" + "; ".join(rows) + "]"
    if tag == "msqrt":
        return f"sqrt({''.join(sub).strip()})"
    if tag == "mroot" and len(sub) == 2:
        return f"root{group(sub[1].strip())}({sub[0].strip()})"
    if tag == "semantics":
        return sub[0] if sub else ""
    return "".join(sub)


def _mathml_row(row: etree._Element) -> list[str]:
    """Cells of an mtr/mlabeledtr (the equation label of a labelled row is left out)."""
    cells = [c for c in row if _tag(c) == "mtd"]
    if _tag(row) == "mlabeledtr":
        cells = cells[1:]
    return [_mathml(c) for c in cells]


# ---------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------

class _Sectioner:
    """Builds classified Sections (in reading order) from front, body and back."""

    def __init__(self, renderer: _Renderer, affiliations: list[str]) -> None:
        self.r = renderer
        self.affiliations = affiliations  # receives present-address notes

    def tree(self, el: etree._Element, inherited: str, level: int, default_title: str = "") -> list[Section]:
        """A sec-like element and its nested sections, depth-first in reading order."""
        if (el.get("sec-type") or "").lower() in _SKIP_SEC_TYPES:
            return []
        title = self.title_of(el)
        kind = self._kind(el, title) or inherited
        blocks: list[str] = []
        children: list[Section] = []

        def on_struct(child: etree._Element) -> None:
            if child.tag == "fn-group":
                children.extend(self.fn_group(child, kind, level + 1))
            elif child.tag in ("sec", "notes", "app") and not self.title_of(child) and not self._kind(child, ""):
                # Untitled, unclassified subsection: splice its content into the parent.
                self.r.mixed(child, skip={"title", "label", "sec-meta"}, on_struct=on_struct, out=blocks)
            else:
                children.extend(self.tree(child, kind, level + 1))

        if el.tag == "ref-list":
            blocks.extend(self.r.block(el))
        else:
            self.r.mixed(el, skip={"title", "label", "sec-meta"}, on_struct=on_struct, out=blocks)
        text = "\n\n".join(b for b in blocks if b)
        if not title and (text or children):
            special = kind not in ("body", "back", "abstract")
            title = (_DEFAULT_TITLES.get(kind, "") if special else "") or default_title
        if not (text or (title and children)):
            return children
        return [Section(title=title, text=text, kind=kind, level=min(level, 6)), *children]

    def fn_group(self, el: etree._Element, inherited: str, level: int) -> list[Section]:
        """Footnote groups: one Section if titled, else one per classified note (+ one for the rest)."""
        title = self.title_of(el)
        if title:
            return self.tree(el, inherited, level)
        sections: list[Section] = []
        rest: list[str] = []
        for fn in el:
            if _tag(fn) != "fn":
                continue
            section = self.note(fn, level)
            if section is not None:
                sections.append(section)
            elif not self.harvest_only(fn):
                rest.extend(self.r.mixed(fn, skip={"label", "title"}))
        if rest:
            sections.append(Section(title=_DEFAULT_TITLES["back"], text="\n\n".join(rest),
                                    kind=inherited, level=level))
        return sections

    def note(self, fn: etree._Element, level: int = 2) -> Section | None:
        """A classified footnote (COI, funding, contributions, availability) as a Section, else None."""
        lead = _lead_text(fn)
        kind = _attr_kind(fn.get("fn-type")) or classify_title(self.title_of(fn) or "") or (
            classify_title(lead) if lead else None)
        if not kind:
            return None
        text = "\n\n".join(self.r.mixed(fn, skip={"label", "title"}))
        if not text:
            return None
        title = self.title_of(fn) or (lead if lead and classify_title(lead) == kind else _DEFAULT_TITLES[kind])
        return Section(title=title, text=text, kind=kind, level=level)

    def harvest_only(self, fn: etree._Element) -> bool:
        """Notes that only carry identity (addresses, correspondence) are harvested, not rendered."""
        fn_type = (fn.get("fn-type") or "").lower()
        address = fn_type in ("present-address", "current-aff") or bool(
            re.match(r"(?:current|present) address", _lead_text(fn), re.I))
        if address and (text := _plain(fn)):
            self.affiliations.append(re.sub(r"^(?:current|present) address:?\s*", "", text, flags=re.I))
        # Publisher boilerplate ("Springer Nature remains neutral ...") names the venue's publisher.
        publisher_note = bool(re.match(r"publisher['’]?s?['’]? note", _lead_text(fn), re.I))
        return address or publisher_note or fn_type in ("deceased", "corresp")

    def title_of(self, el: etree._Element) -> str:
        title_el = el.find("title")
        title = _plain(title_el, skip=("fn",)) if title_el is not None else ""
        label = _plain(el.find("label")) if el.tag in ("sec", "app") else ""
        return f"{label} {title}".strip() if title else ""

    @staticmethod
    def _kind(el: etree._Element, title: str) -> str | None:
        tag = el.tag
        if tag == "ack":
            return "ack"
        if tag == "ref-list":
            return "refs"
        if tag == "bio":
            return "contrib"
        attr = el.get("sec-type") or el.get("notes-type") or el.get("content-type") or el.get("fn-type")
        return _attr_kind(attr) or (classify_title(title) if title else None)


def _lead_text(fn: etree._Element) -> str:
    """A note's lead-in label: leading bold/italic text, or text before the first colon."""
    p = fn.find("p")
    p = p if p is not None else fn
    first = next((c for c in p if _tag(c)), None)
    if first is not None and not (p.text or "").strip() and first.tag in ("bold", "italic", "sc", "title"):
        lead = _plain(first)
    else:
        m = re.match(r"^\s*([^:.]{3,60})[:.]", _plain(p))
        lead = m.group(1) if m else ""
    return lead.strip(" :.")


# ---------------------------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------------------------

def _authors(meta: etree._Element) -> list[Author]:
    authors: list[Author] = []
    for contrib in meta.iter("contrib"):
        if (contrib.get("contrib-type") or "author").lower() != "author":
            continue
        authors.extend(_author_from_name(name) for name in _name_elements(contrib))
        collab = contrib.find("collab")
        if collab is not None:
            text = _plain(collab, skip=("contrib-group", "xref", "fn", "label"))
            if text:
                authors.append(Author(surname=text))
    for group in meta.iter("contrib-group"):
        for behalf in group.findall("on-behalf-of"):
            if text := _plain(behalf):
                name = re.sub(r"^(?:for|on behalf of)\s+(?:the\s+)?", "", text, flags=re.I)
                authors.append(Author(surname=name))
    notes = meta.find("author-notes")
    if notes is not None:
        authors.extend(_author_from_name(n) for n in notes.iter("name", "string-name"))
    return _dedupe_authors(authors)


def _name_elements(contrib: etree._Element) -> list[etree._Element]:
    """The contributor's name, or every form in <name-alternatives> (native script and Latin)."""
    for child in contrib:
        if _tag(child) in ("name", "string-name"):
            return [child]
        if _tag(child) == "name-alternatives":
            return [c for c in child if _tag(c) in ("name", "string-name")]
    return []


def _author_from_name(name: etree._Element) -> Author:
    surname = _plain(name.find("surname"))
    given = _plain(name.find("given-names"))
    if not surname:  # unstructured <string-name>
        text = _plain(name)
        if "," in text:
            surname, given = (p.strip() for p in text.split(",", 1))
        else:
            *rest, surname = text.split() or [""]
            given = " ".join(rest)
    return Author(surname=surname, given=given)


def _dedupe_authors(authors: list[Author]) -> list[Author]:
    seen: set[tuple[str, str]] = set()
    out = []
    for a in authors:
        key = (a.surname.lower(), a.given.lower())
        if a.surname and key not in seen:
            seen.add(key)
            out.append(a)
    return out


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _year(meta: etree._Element) -> int | None:
    years = []
    for date in meta.findall("pub-date"):
        text = date.findtext("year") or date.get("iso-8601-date") or ""
        if m := re.search(r"\b(1[89]\d\d|2\d\d\d)\b", text):
            years.append(int(m.group(1)))
    return min(years) if years else None


def _ids(meta: etree._Element) -> tuple[str, str, str]:
    doi = pmid = pmcid = ""
    for el in meta.findall("article-id"):
        kind, value = (el.get("pub-id-type") or "").lower(), _ws(el.text or "")
        if kind == "doi" and not doi:
            doi = value
        elif kind == "pmid" and not pmid:
            pmid = value
        elif kind in ("pmc", "pmcid") and not pmcid and value:
            pmcid = value.upper() if value.upper().startswith("PMC") else f"PMC{value}"
    return doi, pmid, pmcid


def _harvest_contacts(root: etree._Element) -> tuple[list[str], list[str]]:
    """Every email and ORCID anywhere in the article: elements, link targets and free text."""
    hrefs = [v for el in root.iter() if _tag(el) for k, v in el.attrib.items() if k == "href"]
    text = " ".join(root.itertext()) + " " + " ".join(hrefs)
    emails = [_ws("".join(e.itertext())) for e in root.iter("email")]
    emails += [m.rstrip(".") for m in EMAIL_RE.findall(text)]
    orcids = [ORCID_RE.search(c.text or "").group(0) for c in root.iter("contrib-id")
              if (c.get("contrib-id-type") or "").lower() == "orcid" and ORCID_RE.search(c.text or "")]
    orcids += ORCID_URL_RE.findall(text)
    return _unique(emails), _unique(orcids)


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------

def parse_jats(xml_bytes: bytes, source_id: str) -> Document:
    """Parse JATS XML into a Document with harvested identifiers and classified Sections."""
    root = _parse_xml(xml_bytes)
    front = root.find("front")
    meta = front.find("article-meta") if front is not None else None
    if meta is None:
        meta = etree.Element("article-meta")
    doc = Document(source_id=source_id, source_type="jats")

    title_group = meta.find("title-group")
    if title_group is not None:
        doc.title = ": ".join(p for p in (_plain(title_group.find("article-title")),
                                          _plain(title_group.find("subtitle"))) if p)
    doc.authors = _authors(meta)
    doc.affiliations = _unique(_spaced(aff) for aff in meta.iter("aff"))
    doc.emails, doc.orcids = _harvest_contacts(root)
    journal_meta = front.find("journal-meta") if front is not None else None
    if journal_meta is not None:
        doc.journal = _plain(next(journal_meta.iter("journal-title"), None))
        doc.publisher = _plain(next(journal_meta.iter("publisher-name"), None))
    doc.year = _year(meta)
    doc.doi, doc.pmid, doc.pmcid = _ids(meta)

    renderer = _Renderer()
    sectioner = _Sectioner(renderer, doc.affiliations)
    sections = _abstract_sections(meta, sectioner, doc.notes)
    sections += _front_sections(front, meta, sectioner, renderer)
    body = root.find("body")
    if body is not None:
        sections += _body_sections(body, sectioner)
    else:
        doc.notes.append("no <body> in JATS: abstract/metadata-only record")
    floats = root.find("floats-group")
    if floats is not None:
        sections += sectioner.tree(floats, "body", 2, default_title="Figures and tables")
    back = root.find("back")
    if back is not None:
        sections += _back_sections(back, sectioner)
    doc.sections = _dedupe_sections(sections)
    doc.affiliations = _unique(doc.affiliations)
    institutions = [_plain(i) for i in meta.iter("institution")
                    if not any(a.tag == "funding-group" for a in i.iterancestors())]
    for aff in doc.affiliations:
        institutions.extend(institution_candidates(aff))
    doc.institutions = _unique(institutions)
    doc.notes.extend(renderer.notes)
    if not doc.title and not doc.sections:
        raise ValueError("JATS article has neither a title nor any text")
    return doc


def _abstract_sections(meta: etree._Element, sectioner: _Sectioner, notes: list[str]) -> list[Section]:
    sections: list[Section] = []
    for abstract in meta.findall("abstract"):
        abstract_type = (abstract.get("abstract-type") or "").lower()
        if abstract_type not in _MAIN_ABSTRACT_TYPES:
            notes.append(f"skipped abstract (abstract-type={abstract_type})")
            continue
        default = "Abstract" if abstract_type in ("", "abstract", "main", "structured") else "Summary"
        found = sectioner.tree(abstract, "abstract", 2, default_title=default)
        for s in found:
            s.kind = "abstract"
        if found and not found[0].title:
            found[0].title = default
        sections += found
    return sections


def _front_sections(front: etree._Element | None, meta: etree._Element, sectioner: _Sectioner,
                    renderer: _Renderer) -> list[Section]:
    """Declarations hidden in front matter: author-notes, funding-group, notes, custom-meta."""
    sections: list[Section] = []
    author_notes = meta.find("author-notes")
    if author_notes is not None:
        for fn in author_notes.iter("fn"):
            section = sectioner.note(fn)
            if section is not None:
                sections.append(section)
            else:
                sectioner.harvest_only(fn)
    for group in meta.findall("funding-group"):
        statement = [_ws(" ".join(renderer.mixed(s))) for s in group.iter("funding-statement")]
        text = "\n\n".join(s for s in statement if s) or _spaced(group, skip=("institution-id",))
        if text:
            sections.append(Section(title="Funding", text=text, kind="funding"))
    notes_elements = [n for parent in (front, meta) if parent is not None for n in parent.findall("notes")]
    for notes in notes_elements:
        found = sectioner.tree(notes, "", 2)
        sections += [s for s in found if s.kind]
    for custom in meta.iter("custom-meta"):
        name = _plain(custom.find("meta-name"))
        kind = classify_title(name) or ("availability" if "data availability" in name.lower() else None)
        value = custom.find("meta-value")
        if kind and value is not None:
            text = "\n\n".join(renderer.mixed(value))
            if text:
                sections.append(Section(title=name, text=text, kind=kind))
    return sections


def _body_sections(body: etree._Element, sectioner: _Sectioner) -> list[Section]:
    """Top-level body secs; loose paragraphs outside any sec become "Main text" sections."""
    sections: list[Section] = []
    loose: list[str] = []

    def flush_loose() -> None:
        if loose:
            sections.append(Section(title="Main text", text="\n\n".join(loose), kind="body"))
            loose.clear()

    def on_struct(child: etree._Element) -> None:
        flush_loose()
        if child.tag == "fn-group":
            sections.extend(sectioner.fn_group(child, "body", 2))
        else:
            sections.extend(sectioner.tree(child, "body", 2, default_title="Main text"))

    sectioner.r.mixed(body, skip={"sec-meta", "title", "label"}, on_struct=on_struct, out=loose)
    flush_loose()
    return sections


_BACK_TITLES = {"ack": "Acknowledgments", "ref-list": "References", "glossary": "Glossary",
                "app-group": "Appendix", "app": "Appendix", "bio": "Author biographies"}


def _back_sections(back: etree._Element, sectioner: _Sectioner) -> list[Section]:
    sections: list[Section] = []
    for child in back:
        tag = _tag(child)
        if not tag or tag in ("title", "label"):
            continue
        if tag == "fn-group":
            sections += sectioner.fn_group(child, "back", 2)
        elif tag == "app-group" and not sectioner.title_of(child):
            for app in child:
                if _tag(app):
                    sections += sectioner.tree(app, "back", 2, default_title="Appendix")
        elif tag in _STRUCT_TAGS:
            sections += sectioner.tree(child, "back", 2, default_title=_BACK_TITLES.get(tag, "Notes"))
        else:
            blocks = sectioner.r.block(child) if tag in _BLOCK_TAGS else [_ws(sectioner.r.inline(child))]
            if any(blocks):
                sections.append(Section(title="Notes", text="\n\n".join(b for b in blocks if b), kind="back"))
    return sections


def _dedupe_sections(sections: list[Section]) -> list[Section]:
    """Drop repeated declarations (e.g. PLoS Data Availability in both front notes and custom-meta)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for s in sections:
        key = (s.kind, _ws(s.text).lower())
        if s.kind not in ("body", "abstract") and s.text and key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
