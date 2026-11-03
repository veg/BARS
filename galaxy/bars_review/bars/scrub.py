"""De-identify a parsed manuscript into the markdown sent to the blinded reviewer.

Implements PROTOCOL.md §4.2 (header/identifier stripping, section and banner filtering,
repository URL and self-citation/surname redaction, post-t0 years) plus the fixes for
spond/gemini-reviews#20-#22: typed placeholders for every URL (Galaxy, Zenodo, figshare,
WorkflowHub, ...), DOIs, ORCIDs, emails, email domains, institutions and the venue.

Rules run in one fixed order over a list of text segments. Each inserted placeholder becomes a
frozen segment that later rules never see, so nothing is ever redacted inside a placeholder.
Matching runs on a 1:1 ASCII-folded copy of each segment (same length, same offsets), so
'Müller' and 'Muller' are found alike while the original characters are what gets emitted.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence

from .model import DROP_KINDS, Author, Document, ScrubResult, Section

HEADER = "# [REDACTED FOR BLINDED REVIEW]"

EMAIL = "[EMAIL_REDACTED]"
ORCID = "[ORCID_REDACTED]"
CODE_REPO_URL = "[CODE_REPO_URL]"
GALAXY_HISTORY_URL = "[GALAXY_HISTORY_URL]"
GALAXY_WORKFLOW_URL = "[GALAXY_WORKFLOW_URL]"
GALAXY_URL = "[GALAXY_URL]"
WORKFLOW_REGISTRY_URL = "[WORKFLOW_REGISTRY_URL]"
DATA_ARCHIVE_URL = "[DATA_ARCHIVE_URL]"
PUBLIC_DATABASE_URL = "[PUBLIC_DATABASE_URL]"
DATA_ARCHIVE_DOI = "[DATA_ARCHIVE_DOI]"
DOI = "[DOI_REDACTED]"
URL = "[URL_REDACTED]"
ID = "[ID_REDACTED]"
AUTHOR = "[AUTHOR_REDACTED]"
INSTITUTION = "[INSTITUTION_REDACTED]"
DOMAIN = "[DOMAIN_REDACTED]"
JOURNAL = "[JOURNAL_REDACTED]"
TERM = "[REDACTED_TERM]"
YEAR = "[POST_T0_YEAR_REDACTED]"
METADATA = "[METADATA_REDACTED]"  # whole correspondence / copyright / received-accepted lines
TITLE = "[TITLE_REDACTED]"  # the paper's own full title quoted in the text (e.g. supplement captions)

PLACEHOLDERS = (
    "[REDACTED FOR BLINDED REVIEW]", EMAIL, ORCID, CODE_REPO_URL, GALAXY_HISTORY_URL,
    GALAXY_WORKFLOW_URL, GALAXY_URL, WORKFLOW_REGISTRY_URL, DATA_ARCHIVE_URL, PUBLIC_DATABASE_URL,
    DATA_ARCHIVE_DOI, DOI, URL, ID, AUTHOR, INSTITUTION, DOMAIN, JOURNAL, TERM, YEAR, METADATA, TITLE,
)

# --- shared word lists and host tables (also used by leakcheck.py) -------------------------

# Surnames that are also ordinary words or ubiquitous eponyms (Wright-Fisher, Smith-Waterman,
# Hardy-Weinberg, Kaplan-Meier): never redacted when standing alone, only in names/citations.
COMMON_WORDS = frozenset("""
able ash bank banks barber bass bell berry bird bishop black blank bloom bond booth branch brand
bridge bridges bright brook brooks brown bush butler camp cannon carpenter carter case cash chance
chase church clay close cook cooper cotton crane cross crow dale day dean diamond drake duke early
east english fair faith field fields fish fisher fletcher ford forest foster fox frank free freeman
french frost fuller gates gill glass gold golden good grace grant gray green grey guy hall hand
hardy harper hart hay heath hill hills holly hope horn house hunt hunter jewel joy keen king knight
lake lamb land lane law light little long lord love low mark marsh marshall mason may mills miller
moon morning moss noble north oak page palmer park parker peace pike pine plant pool pope porter
post potter power price pride quick rain ray read reed rice rich ring rivers rock rose rush sage
salt sands savage scales sharp shepherd short silver singer small smart snow south sparks spring
stark steel stern stone storm strong summer swan sweet swift temple thorn tower trout turner wade
walker wall ward ware warren waters watt weaver well wells west white wild will winter wise wolf
wood woods worth wright young
bayes cox crick gauss hamilton kaplan mann markov meier moran pearson poisson shannon simpson
smith southern northern western eastern spearman student tukey watson waterman weinberg welch
whitney wilcoxon
nature science cell genetics evolution development genome genes blood brain heart neuron immunity
""".split())

# Multi-word affiliation parts that are places, not institutions; never redacted.
NON_INSTITUTION_PHRASES = frozenset(p.lower() for p in (
    "United States", "United States of America", "United Kingdom", "New Zealand", "South Africa",
    "South Korea", "Republic of Korea", "Czech Republic", "Saudi Arabia", "Hong Kong", "Costa Rica",
    "Puerto Rico", "Sri Lanka", "The Netherlands", "United Arab Emirates", "Russian Federation",
    "Viet Nam", "People's Republic of China", "P. R. China", "PR China", "P.R. China",
))

# Words that make a phrase an organisation name ('University of X'); lowercase, 'universit' is a prefix.
INSTITUTION_KEYWORDS = frozenset(
    "universit institute institut instituto istituto college hospital center centre laboratory "
    "laboratories school department faculty academy foundation clinic museum observatory polytechnic "
    "division unit".split())
# Leading words of sub-units whose initials are not acronyms anyone uses ('Department of X' -> DX).
SUBUNIT_WORDS = frozenset("department division faculty school section unit program programme laboratory "
                          "lab chair group".split())
# Acronyms of shared resources cited as science (NIH 3T3, EMBL-EBI ENA, NCBI SRA, BDSC stocks): never
# taken for the authors' institution, whether they come from an email domain or an affiliation.
RESOURCE_ACRONYMS = frozenset("nih ncbi ebi embl cdc who nasa noaa usda fda epa ensembl sanger ddbj "
                              "uniprot ucsc nlm nci nhgri niaid nigms nhlbi nimh ninds nichd nibib nei "
                              "jgi bgi bdsc cgc atcc dsmz".split())
# Email-domain labels that are also common scientific abbreviations (the UGA codon, LSU rRNA, c-KIT):
# not redacted by scrub; the leak check still warns about them.
SCIENCE_ACRONYMS = frozenset("uga uaa uag aug lsu ssu uva uvb uvc cam kit pku unc imp sun".split())
# Descriptive words that do not make a shortened organisation name specific ('National Institute').
GENERIC_ORG_WORDS = frozenset(
    "national federal state central general medical research international royal public regional "
    "provincial municipal joint advanced applied higher technical technological agricultural veterinary "
    "health sciences science biomedical clinical genome genomics biology molecular cancer academic "
    "graduate children's childrens women's new north south east west northern southern eastern western "
    "american european asian african".split())
NAME_FUNCTION_WORDS = frozenset("of for and the de du des di der für del da in on at y e et & d".split())

# TLDs accepted for bare hosts without scheme or www (short ones that collide with file
# extensions or English words, e.g. .py .pl .pt .se .es .it .in, are left out).
BARE_HOST_TLDS = (
    "com", "org", "net", "edu", "gov", "int", "mil", "io", "info", "bio", "eu", "uk", "de", "fr",
    "ch", "nl", "jp", "cn", "au", "ca", "dk", "fi", "br", "kr", "nz", "il", "sg", "za", "ru", "cz",
    "ie", "hu", "gr", "tw", "hk", "mx",
)
# Further TLDs accepted only when a path follows (unimi.it/lab, uu.se/x).
PATH_ONLY_TLDS = ("it", "es", "se", "be", "at", "no", "pt", "pl", "in", "us", "ai")
# TLDs accepted for an all-lowercase bare host without a path ('wombatia.it', 'quokkalab.science',
# 'bit.ly'); capitalised forms are run-together sentences ('the end.It was'), not hosts. File
# extensions and code suffixes (.pt .pl .py .sh .in .id) stay out.
LOWERCASE_HOST_TLDS = (
    "it", "es", "se", "be", "at", "no", "us", "ai", "co", "ly", "me", "tv", "lu", "si", "sk", "ro", "ee",
    "lv", "lt", "hr", "bg", "ua", "tr", "cl", "pe", "uy", "ec", "ng", "ke", "th", "vn", "pk", "lk", "ir",
    "sa", "ae", "qa", "app", "science", "tech", "tools", "cloud", "xyz", "online", "site", "lab", "academy",
    "software", "studio",
)
# First parts of dotted code names that end like a host ('scipy.io', 'tf.io'): modules, not links.
CODE_NAMESPACES = frozenset("scipy skimage numpy np pandas pd torch tf sklearn keras jax matplotlib plt".split())
# Units and counted things after a number: '2048 bp' or '2031 genes' is a quantity, not a year.
COUNT_WORDS = frozenset("""
bp kb kbp mb mbp gb gbp nt aa da kda bases nucleotides residues genes snps snvs indels variants
mutations sites loci alleles markers reads contigs scaffolds sequences genomes transcripts proteins
peptides isolates strains samples specimens individuals participants patients subjects cases controls
animals mice rats cells colonies plants trees seeds species genera taxa families haplotypes cohorts
replicates iterations permutations bootstraps simulations generations trials runs steps epochs features
windows regions peaks clusters copies points pixels voxels words records studies km cm mm um nm kg mg ug
ng ml ul rpm
""".split())

CODE_HOST_LABELS = ("github", "gitlab", "bitbucket", "codeberg", "sourceforge", "gitee",
                    "githubusercontent")
REGISTRY_HOSTS = ("workflowhub.eu", "dockstore.org", "nf-co.re")
ARCHIVE_HOSTS = ("zenodo.org", "figshare.com", "datadryad.org", "osf.io", "data.mendeley.com")
DATABASE_HOSTS = ("ncbi.nlm.nih.gov", "ncbi.nih.gov", "ebi.ac.uk", "ddbj.nig.ac.jp", "rcsb.org",
                  "wwpdb.org", "pdbe.org", "pdbj.org", "uniprot.org", "gisaid.org", "ensembl.org",
                  "genome.ucsc.edu", "identifiers.org")
GALAXY_HOSTS = ("galaxyproject.org",)  # plus any host label containing "galaxy"
DATA_DOI_PREFIXES = ("10.5281", "10.6084", "10.5061", "10.17605", "10.17632", "10.7910")

# --- folding --------------------------------------------------------------------------------

_FOLD_SPECIAL = {
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "ı": "i",
    "\u2010": "-", "\u2011": "-", "\u2019": "'", "\u02bc": "'",
}
_ASCII_MULTI = {"ß": "ss", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "þ": "th", "Þ": "Th"}
_GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}


class _FoldTable(dict):
    """str.translate table: each character -> one ASCII-folded character (or itself)."""

    def __missing__(self, code: int) -> str:
        ch = chr(code)
        base = _FOLD_SPECIAL.get(ch)
        if base is None:
            base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            if len(base) != 1:
                base = ch
        self[code] = base
        return base


_FOLD_TABLE = _FoldTable()


def _fold(text: str) -> str:
    """Strip diacritics character by character; the result has the same length as `text`."""
    return text.translate(_FOLD_TABLE)


def _ascii(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    return _fold("".join(_ASCII_MULTI.get(c, c) for c in name))


def _variants(name: str) -> list[str]:
    """Folded spellings of a name: as given, fully ASCII, German transliteration (ü -> ue)."""
    name = unicodedata.normalize("NFC", " ".join(name.split()))
    german = "".join(_GERMAN.get(c, c) for c in name)
    return list(dict.fromkeys([_fold(name), _ascii(name), _ascii(german)]))


# --- regex building blocks ------------------------------------------------------------------

_LB = r"(?<![^\W_])"  # not preceded by a letter or digit
_RB = r"(?![^\W_])"  # not followed by a letter or digit
_RB_LOOSE = r"(?![^\W\d_])"  # not followed by a letter (allows footnote digits: Quokkason1)
_UP = "[A-Z]"
_WS = r"(?:[ \t]*\n[ \t]*|[ \t]+)"  # whitespace inside one paragraph (at most one line break)


def _rx(text: str) -> str:
    """Regex for a folded literal with flexible whitespace, hyphens and optional periods."""
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            if not out or out[-1] != _WS:
                out.append(_WS)
        elif ch in "-–":
            out.append("[-–]")
        elif ch == ".":
            out.append(r"\.?")
        elif ch == "'":
            out.append("['’]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _alt(sources: Iterable[str]) -> str:
    """Non-capturing alternation, longest first, deterministic."""
    uniq = sorted(set(s for s in sources if s), key=lambda s: (-len(s), s))
    return "(?:" + "|".join(uniq) + ")" if uniq else ""


def _name_rx(names: Iterable[str]) -> str:
    return _alt(_rx(v) for n in names for v in _variants(n))


# --- segments -------------------------------------------------------------------------------

Segment = tuple[str, bool]  # (text, frozen)
Replacer = Callable[[re.Match[str]], "tuple[str, int] | None"]  # -> (placeholder, chars given back)
Rule = tuple[re.Pattern[str], Replacer]


def _fixed(token: str) -> Replacer:
    return lambda m: (token, 0)


def _push(out: list[Segment], text: str) -> None:
    if not text:
        return
    if out and not out[-1][1]:
        out[-1] = (out[-1][0] + text, False)
    else:
        out.append((text, False))


def _apply(segments: list[Segment], rule: Rule, counts: dict[str, int]) -> list[Segment]:
    pattern, replace = rule
    out: list[Segment] = []
    for text, frozen in segments:
        if frozen:
            out.append((text, True))
            continue
        pos = 0
        for m in pattern.finditer(_fold(text)):
            hit = replace(m)
            if hit is None:
                continue
            token, give_back = hit
            end = m.end() - give_back
            if end <= m.start():
                continue
            _push(out, text[pos:m.start()])
            out.append((token, True))
            counts[token] = counts.get(token, 0) + 1
            pos = end
        _push(out, text[pos:])
    return out


# --- identifiers: emails, ORCIDs, URLs, DOIs, PubMed ids --------------------------------------

# Whole correspondence/date/copyright lines. Markdown headings never match ('### Correspondence
# analysis'); 'correspondence' needs a label form or an address, and date labels need a date.
_METADATA_LINE = re.compile(
    r"^(?!#+[ \t])[ \t]*[*†‡§¶#>\d]*[ \t]*(?:"
    r"correspondence(?:[ \t]*:|[ \t]+(?:to|should|concerning|regarding|address|and[ \t]+requests)\b"
    r"|(?=[^\n]*@))"
    r"|corresponding\s+authors?\b|to\s+whom\s+(?:all\s+)?correspondence\b"
    r"|(?:e-?mail|citation|academic\s+editor|handling\s+editor|editor)\s*:"
    r"|(?:received|accepted|published(?:\s+online)?|revised)\s*:\s*"
    r"(?=\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d)"
    r"|copyright\b|©|cite\s+this\s+article\b"
    r")[^\n]{0,400}$",
    re.IGNORECASE | re.MULTILINE,
)
_EMAIL = re.compile(
    r"(?:mailto:)?[a-z0-9][a-z0-9._%+\-]*@[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)*\.[a-z]{2,}(?![a-z\-])",  # a footnote digit may follow
    re.IGNORECASE,
)
_EMAIL_OBFUSCATED = re.compile(
    r"[a-z0-9][a-z0-9._%+\-]*\s*[\[({<]\s*at\s*[\])}>]\s*[a-z0-9\-]+"
    r"(?:(?:\.|\s*[\[({<]\s*dot\s*[\])}>]\s*)[a-z0-9\-]+)+",
    re.IGNORECASE,
)
_ORCID = re.compile(
    r"(?:(?:https?://)?(?:www\.)?orcid\.org/\s*)?(?<![\d-])\d{4}-\d{4}-\d{4}-\d{3}[\dX](?![\d-])",
    re.IGNORECASE,
)

_URL_CHARS = r"[^\s<>\"'`\[\]{}|\\^]"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


def _tld_alt(tlds: Iterable[str]) -> str:
    return "|".join(sorted(tlds, key=lambda t: (-len(t), t)))


_URL = re.compile(
    rf"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]*://{_URL_CHARS}*"
    rf"|(?<![\w.@/-])www\d{{0,3}}\.{_LABEL}(?:\.{_LABEL})*(?:[/?#:]{_URL_CHARS}*)?"
    rf"|(?<![\w.@/:-])(?=[a-z0-9-]*[a-z])(?:{_LABEL}\.)+(?:{_tld_alt(BARE_HOST_TLDS)})(?![\w-])"
    rf"(?:[/?#:]{_URL_CHARS}*)?"
    rf"|(?<![\w.@/:-])(?=[a-z0-9-]*[a-z])(?:{_LABEL}\.)+(?:{_tld_alt(PATH_ONLY_TLDS)})"
    rf"/{_URL_CHARS}+"
    rf"|(?<![\w.@/:-])(?-i:(?=[a-z0-9-]*[a-z])(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+"
    rf"(?:{_tld_alt(LOWERCASE_HOST_TLDS)}))(?![\w-])(?:[/?#:]{_URL_CHARS}*)?"
    rf"|(?<![\w.@/:-])(?:usegalaxy(?:\.{_LABEL})+|nf-co\.re)(?![\w-])(?:[/?#:]{_URL_CHARS}*)?",
    re.IGNORECASE,
)
_DOI = re.compile(r"(?<![\w/.\-])10\.\d{4,9}/[^\s\"'<>|\[\]{}`]+")
_PMCID = re.compile(r"(?<![A-Za-z0-9])PMC\s?\d+(?!\d)")
_PMID = re.compile(r"(?<![a-z0-9])(?:PMID|PubMed\s*ID)\s*:?\s*\d+(?!\d)", re.IGNORECASE)
_ARXIV = re.compile(r"(?<![\w/])arxiv\s*:\s*(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?",
                    re.IGNORECASE)

_HOST_PATH = re.compile(r"^(?:[a-z][a-z0-9+.\-]*://)?(?:[^/?#@\s]*@)?([^/?#:\s]*)(?::\d*)?(.*)$",
                        re.IGNORECASE | re.DOTALL)
_GALAXY_HISTORY = re.compile(r"/u/[^/]+/h/|/published/history|/histories/view|/history/view",
                             re.IGNORECASE)
_GALAXY_WORKFLOW = re.compile(r"/u/[^/]+/w/|/published/workflow|/workflows/run", re.IGNORECASE)
_GALAXY_OTHER = re.compile(r"/datasets/[0-9a-f]{16}|/u/[^/]+/[pdv]/|/published/page",
                           re.IGNORECASE)

_TRAILING = ".,;:!?'\"*_"


def _give_back(s: str) -> int:
    """Number of trailing punctuation characters that belong to the prose, not the URL/DOI."""
    end = len(s)
    while end:
        c = s[end - 1]
        if c in _TRAILING or (c == ")" and s.count("(", 0, end) < s.count(")", 0, end)):
            end -= 1
        else:
            break
    return len(s) - end


def _host_in(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _classify_doi(doi: str) -> str:
    return DATA_ARCHIVE_DOI if doi.split("/", 1)[0] in DATA_DOI_PREFIXES else DOI


def classify_url(url: str, email_domains: Iterable[str] = ()) -> str:
    """Placeholder for a URL (scheme optional); bare email domains map to [DOMAIN_REDACTED]."""
    m = _HOST_PATH.match(url)
    host, path = (m.group(1).lower().rstrip("."), m.group(2)) if m else ("", "")
    labels = host.split(".")
    if _host_in(host, ("doi.org",)):
        return _classify_doi(path.lstrip("/"))
    if _host_in(host, ("orcid.org",)):
        return ORCID
    if any(label in CODE_HOST_LABELS for label in labels):
        return CODE_REPO_URL
    if _host_in(host, REGISTRY_HOSTS):
        return WORKFLOW_REGISTRY_URL
    if _host_in(host, ARCHIVE_HOSTS) or any("dataverse" in label for label in labels):
        return DATA_ARCHIVE_URL
    if _host_in(host, DATABASE_HOSTS):
        return PUBLIC_DATABASE_URL
    galaxy_host = _host_in(host, GALAXY_HOSTS) or any("galaxy" in label for label in labels)
    if _GALAXY_HISTORY.search(path):
        return GALAXY_HISTORY_URL
    if _GALAXY_WORKFLOW.search(path):
        return GALAXY_WORKFLOW_URL
    if galaxy_host or _GALAXY_OTHER.search(path):
        return GALAXY_URL
    if ("://" not in url and not host.startswith("www") and not path.strip("/")
            and _host_in(host, email_domains)):
        return DOMAIN
    return URL


def code_name(text: str) -> bool:
    """'scipy.io', 'tf.io': a module path that looks like a bare host (no scheme, www or path)."""
    m = re.fullmatch(r"([a-z0-9]+)\.[a-z]+", text, re.IGNORECASE)
    return bool(m) and m.group(1).lower() in CODE_NAMESPACES


def _url_replacer(domains: Sequence[str]) -> Replacer:
    def replace(m: re.Match[str]) -> tuple[str, int] | None:
        n = _give_back(m.group())
        url = m.group()[: len(m.group()) - n]
        return None if code_name(url) else (classify_url(url, domains), n)
    return replace


def _replace_doi(m: re.Match[str]) -> tuple[str, int]:
    n = _give_back(m.group())
    return _classify_doi(m.group()[: len(m.group()) - n]), n


_SECOND_LEVEL = frozenset({"ac", "co", "edu", "gov", "org", "net", "com", "nhs", "res"})  # ac.uk, edu.au


def _email_domains(doc: Document) -> list[str]:
    """Email domains of the document plus their registrable parts (bio.psu.edu -> psu.edu)."""
    found = list(doc.emails)
    for text in [doc.front_matter, *doc.affiliations, *(s.text for s in doc.sections)]:
        found.extend(_EMAIL.findall(_fold(text)))
    domains: list[str] = []
    for email in found:
        domain = email.rsplit("@", 1)[-1].strip().strip(".").lower()
        labels = domain.split(".")
        if len(labels) < 2:
            continue
        keep = 3 if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
        domains.append(domain)
        if len(labels) > keep:
            domains.append(".".join(labels[-keep:]))
    return sorted(set(domains), key=lambda d: (-len(d), d))


# --- author names -----------------------------------------------------------------------------

_PARTICLES = frozenset(
    "van von de der den del della dei di da do dos das du la le lo ter ten te zu zur vom al el "
    "bin ibn".split())
_CITE_CONTEXT = (
    rf"(?=\s+(?i:et\.?\s*al){_RB}"
    rf"|\s+(?:(?i:and)|&)\s+(?:[A-Z]|(?i:colleagues|co-?workers|co-?authors){_RB})"
    r"|,?\s*\(?\s*(?:1[89]|20)\d\d[a-f]?(?!\d)"
    r"|\s*\[\d"
    rf"|['’]s{_RB}"  # possessive: "Wang's pipeline"
    rf"|\s+(?i:lab|laboratory|group){_RB}"
    rf"|[ \t]+{_UP}{{1,3}}[,;.](?:\s|$))"  # reference-list form with any initials: 'Wang H,' 'Wang HY.'
)
_YEAR_REF = r"(?:1[89]|20)\d\d[a-f]?(?!\d)"
# Surnames that are ordinary words (May, Summer, Rice, Brown): only unmistakable citation forms, so
# that 'in May 2018' and 'Rice and Arabidopsis' stay.
_COMMON_CITE_CONTEXT = (
    rf"(?=\s+(?i:et\.?\s*al){_RB}"
    r"|\s*\[\d"
    rf"|['’]s\s+(?i:lab|laboratory|group|team){_RB}"
    rf"|\s+(?i:lab|laboratory|group){_RB}"
    rf"|,?\s*\(\s*{_YEAR_REF}|,\s*{_YEAR_REF}"  # 'Brown (2012)', '(Brown, 2012)'
    rf"|\s+(?:(?i:and)|&)\s+(?:(?i:colleagues|co-?workers|co-?authors){_RB}"
    rf"|{_UP}[\w'’-]+(?:\s+(?i:et\.?\s*al)\.?)?,?\s*\(?\s*{_YEAR_REF})"  # 'Brown and Smith (2010)'
    rf"|[ \t]+{_UP}{{1,3}}[,;.](?:\s|$))"
)
_PAREN_YEAR_CONTEXT = rf"(?=[ \t]+{_YEAR_REF})"  # after '(' or '; ': '(Brown 2012)'
_ET_AL_CONTEXT = rf"(?=\s+(?i:et\.?\s*al){_RB})"


def _surnames(author: Author) -> list[str]:
    """Surname plus its core without leading particles ('van der Berg' -> 'Berg')."""
    surname = " ".join((author.surname or author.given).split())
    if len(surname) < 2:
        return []
    tokens = surname.split()
    i = 0
    while i < len(tokens) - 1 and tokens[i].lower() in _PARTICLES:
        i += 1
    if not i:
        return [surname]
    return [surname, surname[0].upper() + surname[1:], " ".join(tokens[i:])]  # + 'Van der Berg'


def _surname_parts(author: Author, lowers: set[str]) -> list[str]:
    """Citation forms of a compound personal surname: 'García Márquez' -> 'García-Márquez',
    'García', 'Márquez' (parts of >= 3 letters that are not common words; consortia are not split)."""
    tokens = [t for t in author.surname.split() if t.lower() not in _PARTICLES]
    if not author.given or len(tokens) < 2:
        return []
    joined = ["-".join(tokens)] if len(tokens) == 2 else []
    return joined + [t for t in (tokens[0], tokens[-1]) if len(t) >= 3 and not _is_common(t, lowers)]


def _given_parts(author: Author) -> tuple[list[str], list[str]]:
    """(full given-name tokens, initials) of an author; 'Jean-Luc' -> (['Jean-Luc'], ['J', 'L'])."""
    given = author.given if author.surname else ""
    full: list[str] = []
    initials: list[str] = []
    for token in re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", given):
        if len(token) == 1 or (token.isupper() and len(token) <= 3):
            initials.extend(token)
        else:
            full.append(token)
            initials.extend(p[0] for p in re.split(r"['’\-]", token) if p)
    return full, [_ascii(i)[:1].upper() for i in initials if _ascii(i)[:1].isalpha()]


def _lower_words(doc: Document) -> set[str]:
    """Words occurring in lowercase anywhere in the sections (URL/email-like tokens skipped)."""
    words: set[str] = set()
    for section in doc.sections:
        for token in _ascii(section.title + "\n" + section.text).split():
            if any(c in token for c in "@/:_") or re.search(r"[^\W\d_]\.[^\W\d_]", token):
                continue
            for word in re.findall(r"[^\W\d_]+(?:['\-][^\W\d_]+)*", token):
                if word[0].islower():
                    words.add(word)
                    words.update(word.split("-"))
    return words


def _is_common(name: str, lowers: set[str]) -> bool:
    """True for a single word (particles aside) that is ordinary English or used in lowercase.

    Multi-word or hyphenated names ('Brown-Young', consortium names) are never common.
    """
    parts = [p for p in re.split(r"[\s\-–]+", _ascii(name).lower()) if p and p not in _PARTICLES]
    return len(parts) == 1 and (parts[0] in COMMON_WORDS or parts[0] in lowers)


def _full_name_forms(given_full: list[str], initials: list[str], surname_rx: str) -> list[str]:
    if not initials:
        return []
    ini = re.escape(initials[0])
    dotted = rf"{ini}\.(?:[ \t]?[-–]?[ \t]?{_UP}\.)*"
    compact = rf"{ini}(?:[-–]?{_UP}){{1,3}}"
    sep = rf"(?:,{_WS}?|{_WS})"  # between surname and trailing initials/given name
    before = [dotted + _WS + "?", compact + _WS]
    if initials[0] not in "AI":  # bare 'A'/'I' are usually the article/pronoun
        before.append(ini + _WS)
    after = [
        sep + dotted,
        sep + compact + _RB,
        sep + ini + rf"(?=[ \t]*(?:[,;:)\]]|$)|\s+(?:(?i:et\.?\s*al|and)|&){_RB}"
        r"|\s*\(?(?:1[89]|20)\d\d(?!\d))",
    ]
    if given_full:
        first = _name_rx(given_full[:1])
        middle = _alt([_rx(v) for t in given_full[1:] for v in _variants(t)]
                      + [rf"{_UP}\.?", rf"{_UP}[a-z]+"])
        given = rf"{first}(?:{_WS}{middle}){{0,2}}"
        before.append(given + _WS)
        after += [rf",{_WS}?{given}{_RB}", rf"{_WS}{first}{_RB}"]
    return [_LB + _alt(before) + surname_rx + _RB_LOOSE, _LB + surname_rx + _alt(after)]


def _present(name: str, haystack: str) -> bool:
    """Cheap prefilter: can any spelling of `name` occur in the folded text at all?"""
    return any(re.match(r"[^\W\d_]*", v).group() in haystack for v in _variants(name))


def _author_patterns(authors: Sequence[Author], lowers: set[str],
                     haystack: str) -> tuple[list[str], list[str], list[str]]:
    """Regex sources for (full names, citation forms, standalone surnames) of all authors.

    Only names whose spelling occurs in `haystack` get patterns (keeps 1000-author papers fast).
    """
    full_forms: list[str] = []
    citations: list[str] = []
    standalone: list[str] = []
    for author in authors:
        given_full, initials = _given_parts(author)
        names = _surnames(author) + _surname_parts(author, lowers)
        for upper in (False, True):
            styled = [n.upper() if upper else n for n in names]
            styled = [n for n in styled if _present(n, haystack)]
            if not styled:
                continue
            given_styled = [g.upper() if upper else g for g in given_full]
            full_forms += _full_name_forms(given_styled, initials, _name_rx(styled))
            for name in styled:
                name_rx = _name_rx([name])
                common = _is_common(name, lowers)
                if len(name) == 2 or (upper and len(name) < 5):  # 'LEE', 'ALI' are also acronyms
                    citations.append(_LB + name_rx + _ET_AL_CONTEXT)
                elif common:
                    citations += [_LB + name_rx + _COMMON_CITE_CONTEXT,
                                  r"(?:(?<=\()|(?<=;[ \t]))" + name_rx + _PAREN_YEAR_CONTEXT]
                elif len(name) >= 3:
                    citations.append(_LB + name_rx + _CITE_CONTEXT)
                if len(name) >= 5 and not common:
                    standalone.append(_LB + name_rx + _RB_LOOSE)
    return full_forms, citations, standalone


# --- institutions, venue, terms ---------------------------------------------------------------

def _in_affiliations(name: str, doc: Document) -> bool:
    """True when an institution occurs in the affiliations or front matter, not only in the body."""
    key = " ".join(name.split()).lower()
    return any(key in " ".join(text.split()).lower() for text in (*doc.affiliations, doc.front_matter))


def _institution_phrases(doc: Document, affiliated_only: bool = False) -> list[str]:
    """Institutions plus organisation parts (>= 2 words) of affiliation strings.

    With `affiliated_only`, institutions found only in the body (PDF harvests from the whole text,
    e.g. 'Bloomington Drosophila Stock Center') are left out.
    """
    candidates = [i for i in doc.institutions if not affiliated_only or _in_affiliations(i, doc)]
    for affiliation in doc.affiliations:
        candidates.extend(re.split(r"[,;\n]", affiliation))
    phrases: set[str] = set()
    for candidate in candidates:
        words = re.sub(r"^[\W\d_]+", "", candidate).split()
        while words and not words[0][:1].isupper():
            words.pop(0)  # affiliation labels such as 'a' or 'and'
        while words and not re.search(r"[^\W\d_]", words[-1]):
            words.pop()  # postcodes
        phrase = " ".join(words).strip(" .")
        if (sum(bool(re.search(r"[^\W\d_]", w)) for w in phrase.split()) >= 2
                and phrase.lower() not in NON_INSTITUTION_PHRASES):
            phrases.add(phrase)
    return sorted(phrases, key=lambda p: (-len(p), p))


def _org_word(word: str) -> str:
    return _ascii(word).lower().strip(".,;:'’")


def _is_org_keyword(word: str) -> bool:
    w = _org_word(word)
    return w in INSTITUTION_KEYWORDS or w.startswith("universit")


def _leading_names(phrase: str) -> list[str]:
    """Shorter names an organisation goes by, cut from the front of its full name.

    'University of Michigan Medical School' -> 'University of Michigan', ...; a cut must hold an
    organisation keyword and a specific capitalised word, so 'National Institute' is never one.
    """
    words = phrase.split()
    names = []
    for k in range(2, len(words)):
        head = words[:k]
        specific = [w for w in head if w[:1].isupper() and not _is_org_keyword(w)
                    and _org_word(w) not in GENERIC_ORG_WORDS | NAME_FUNCTION_WORDS]
        if head[-1][:1].isupper() and specific and any(_is_org_keyword(w) for w in head):
            names.append(" ".join(head).rstrip(",;:"))
    return names


def _institution_acronyms(phrases: Sequence[str], domains: Sequence[str]) -> list[str]:
    """Upper-case acronyms (3-8 letters) of the authors' organisations.

    Initials of full names (Cold Spring Harbor Laboratory -> CSHL) and email-domain labels
    (unil.ch -> UNIL), except names of shared resources (NCBI, EBI) and, for domain labels,
    scientific abbreviations (uga.edu -> UGA, the stop codon).
    """
    found: set[str] = set()
    for phrase in phrases:
        words = re.findall(r"[^\W\d_]+", _ascii(phrase))
        if not words or words[0].lower() in SUBUNIT_WORDS:
            continue
        initials = "".join(w[0] for w in words if w[0].isupper() and w.lower() not in NAME_FUNCTION_WORDS)
        if 3 <= len(initials) <= 6 and initials.lower() not in RESOURCE_ACRONYMS:
            found.add(initials)
    for domain in domains:
        labels = domain.split(".")
        keep = 3 if len(labels[-1]) == 2 and len(labels) > 2 and labels[-2] in _SECOND_LEVEL else 2
        label = labels[-keep] if len(labels) >= keep else ""
        if (label.isalpha() and 3 <= len(label) <= 8
                and label not in RESOURCE_ACRONYMS and label not in SCIENCE_ACRONYMS):
            found.add(label.upper())
    return sorted(found, key=lambda a: (-len(a), a))


_INVERTIBLE = r"University|Institute|College|Academy"


def _other_forms(phrase: str) -> list[str]:
    """Other ways an organisation is written: 'Wombatia University' and 'Univ. of Wombatia' for
    'University of Wombatia', 'Pasteur Institute' for 'Institut Pasteur', 'MPI for Evolutionary
    Anthropology' for 'Max Planck Institute for Evolutionary Anthropology'."""
    forms = []
    if m := re.fullmatch(rf"({_INVERTIBLE}) of ((?:[A-Z][\w'’-]*)(?: [A-Z][\w'’-]*)?)", phrase):
        keyword, place = m.groups()
        forms.append(f"{place} {keyword}")
        if keyword == "University":
            forms.append(f"Univ. of {place}")
    if m := re.fullmatch(r"Institut ([A-Z][\w'’-]+(?: [A-Z][\w'’-]+)?)", phrase):
        forms.append(f"{m.group(1)} Institute")
    if m := re.fullmatch(r"((?:[A-Z][\w'’-]* ){2,4}(?:Institute|Institut|Center|Centre|Laboratory)) ((?:for|of) .+)",
                         phrase):
        initials = "".join(w[0] for w in m.group(1).split())
        forms.append(f"{initials} {m.group(2)}")
    return forms


def _venue_names(doc: Document) -> list[str]:
    names = {re.sub(r"^the\s+", "", " ".join(n.split()), flags=re.IGNORECASE)
             for n in (doc.journal, doc.publisher)}
    return sorted((n for n in names if len(n) >= 3), key=lambda n: (-len(n), n))


def _venue_rx(name: str) -> str:
    parts = ["(?:&|and)" if w.lower() in {"&", "and", "&amp;"} else _name_rx([w]) for w in name.split()]
    return _WS.join(parts)


def _has_upper(m: re.Match[str]) -> tuple[str, int] | None:
    return (JOURNAL, 0) if any(c.isupper() for c in m.group()) else None


# Publisher boilerplate that names the venue: 'Supplementary data are available at Bioinformatics online.'
_ONLINE_VENUE = re.compile(
    r"(?:(?<=[Aa]vailable at )|(?<=[Aa]vailable from )|(?<=[Aa]vailable on )|(?<=[Aa]ccessible at ))"
    r"(?:[Tt]he )?[A-Z][\w&|:.\-]*(?: (?:(?:of|and|for|in|&) )?[A-Z][\w&|:.\-]*){0,6}(?= [Oo]nline\b)"
)


_NOT_LICENSE = r"(?![ \t]+[Ll]icen[cs]e)"  # 'MIT license' names a license, not the institute


def _title_rule(title: str) -> Rule | None:
    """The paper's full title (>= 4 words) wherever it is quoted, punctuation and case aside."""
    words = re.findall(r"[^\W_]+", _fold(title))
    if len(words) < 4:
        return None
    body = r"[\W_]{1,6}".join(re.escape(w) for w in words)
    return re.compile(_LB + body + _RB, re.IGNORECASE), _fixed(TITLE)


def _phrase_rule(phrases: Sequence[str], token: str, flags: int = 0,
                 replace: Replacer | None = None) -> Rule | None:
    body = _name_rx(phrases)
    if not body:
        return None
    return re.compile(_LB + body + _RB, flags), replace or _fixed(token)


# --- years ------------------------------------------------------------------------------------

_YEAR_RX = re.compile(r"(?<![\w.,])((?:19|20)\d\d)(?:s|[a-f])?(?!\w)(?![.,]\d)")


_DATE_WORDS = frozenset("in since until till by during from after before around circa".split())


def _date_word(word: str) -> bool:
    """'since'/'until' anywhere, or a sentence-initial 'In'/'By'/'From': a year follows, not a count."""
    return word.lower() in ("since", "until", "till", "during") or (word[0].isupper() and word.lower() in _DATE_WORDS)


def _counted(text: str, start: int, end: int) -> bool:
    """True for '2048 bp' or 'in 2050 individuals' (a unit or counted noun follows), unless a date
    word precedes ('In 2021 cells were ...')."""
    following = re.match(r"[ \t-]*([^\W\d_]+)", text[end:])
    preceding = re.search(r"([^\W\d_]+)\W*$", text[:start])
    return bool(following) and following.group(1).lower() in COUNT_WORDS and not (
        preceding and _date_word(preceding.group(1)))


def _year_replacer(t0: int) -> Replacer:
    def replace(m: re.Match[str]) -> tuple[str, int] | None:
        if int(m.group(1)) <= t0:
            return None
        before = m.string[: m.start()].rstrip()[-1:]
        after = m.string[m.end():].lstrip()[:1]
        if (before and before in "=<>≤≥±×*+^~" or after and after in "%±×"
                or _counted(m.string, m.start(), m.end())):
            return None  # a quantity such as n = 2048 or 2048 bp, not a calendar year
        if (m.string[: m.start()].rstrip(" \t")[-1:] == "|"
                and m.string[m.end():].lstrip(" \t")[:1] == "|"):
            return None  # a bare number filling a table cell is a value (quality 2049), not a date
        return YEAR, 0
    return replace


# --- assembly ---------------------------------------------------------------------------------

_NOTICE_TITLE = re.compile(
    r"^\s*(?:retraction(?:\s+(?:note|notice))?|notice\s+of\s+retraction|retracted(?:\s+article)?"
    r"|withdrawal|withdrawn|expression\s+of\s+concern|erratum|corrigendum)\s*:?\s*$",
    re.IGNORECASE,
)


def _rules(doc: Document, extra_terms: Sequence[str], text: str) -> list[Rule]:
    """Rules in application order; `text` is the rendered markdown (used only as a prefilter)."""
    lowers = _lower_words(doc)
    domains = _email_domains(doc)
    rules: list[Rule | None] = [
        (_METADATA_LINE, _fixed(METADATA)),
        (_EMAIL, _fixed(EMAIL)),
        (_EMAIL_OBFUSCATED, _fixed(EMAIL)),
        (_ORCID, _fixed(ORCID)),
        (_URL, _url_replacer(domains)),
        (_DOI, _replace_doi),
        (_PMCID, _fixed(ID)),
        (_PMID, _fixed(ID)),
        (_ARXIV, _fixed(ID)),
    ]
    own_ids = [re.sub(r"\D", "", i) for i in (doc.pmid, doc.pmcid)]
    own_ids = [i for i in own_ids if len(i) >= 6]
    if own_ids:
        rules.append((re.compile(rf"(?<![\w.]){_alt(own_ids)}(?!\d)"), _fixed(ID)))

    rules.append(_title_rule(doc.title))
    terms = [" ".join(t.split()) for t in extra_terms if len(t.strip()) >= 2]
    rules.append(_phrase_rule(terms, TERM, re.IGNORECASE))
    institutions = _institution_phrases(doc)
    names = sorted({*institutions, *(n for p in institutions for n in _leading_names(p)),
                    *(f for p in institutions for f in _other_forms(p))})
    rules.append(_phrase_rule(names + [p.upper() for p in names], INSTITUTION))
    acronyms = _institution_acronyms(_institution_phrases(doc, affiliated_only=True), domains)
    if acronyms:
        body = _alt(map(re.escape, acronyms))
        rules.append((re.compile(_LB + body + _RB + _NOT_LICENSE), _fixed(INSTITUTION)))

    multi = [n for n in _venue_names(doc) if len(n.split()) > 1]
    single = [n for n in _venue_names(doc) if len(n.split()) == 1 and not _is_common(n, lowers)]
    if multi:
        body = _alt(_venue_rx(n) for n in multi)
        rules.append((re.compile(_LB + body + _RB, re.IGNORECASE), _has_upper))
    rules.append(_phrase_rule(single + [n.upper() for n in single], JOURNAL))
    rules.append((_ONLINE_VENUE, _fixed(JOURNAL)))

    if domains:
        body = _alt(re.escape(d) for d in domains)
        rules.append((re.compile(rf"(?<![\w.@\-]){body}(?![\w\-])(?!\.[a-z0-9])", re.IGNORECASE),
                      _fixed(DOMAIN)))

    full_forms, citations, standalone = _author_patterns(doc.authors, lowers, _fold(text))
    for sources in (full_forms, citations, standalone):
        if sources:
            rules.append((re.compile(_alt(sources), re.MULTILINE), _fixed(AUTHOR)))

    if doc.year:
        rules.append((_YEAR_RX, _year_replacer(doc.year)))
    return [r for r in rules if r is not None]


def _render(sections: Iterable[Section]) -> str:
    parts: list[str] = []
    for section in sections:
        title = section.title.strip() or ("Abstract" if section.kind == "abstract" else "")
        if title:
            parts.append("#" * max(2, min(6, section.level)) + " " + " ".join(title.split()))
        if section.text.strip():
            parts.append(section.text.strip())
    return "\n\n".join(parts)


def scrub(doc: Document, extra_terms: Sequence[str] = ()) -> ScrubResult:
    """De-identify `doc` into reviewer markdown.

    Title, authors, affiliations, journal, year, DOI and PubMed ids are never emitted; sections
    of DROP_KINDS and retraction/erratum notices are dropped; identifiers left in the remaining
    text become typed placeholders (counted in ScrubResult.counts). Extra terms shorter than two
    characters are ignored.
    """
    kept: list[Section] = []
    dropped: list[str] = []
    for section in doc.sections:
        if section.kind in DROP_KINDS:
            dropped.append(f"{section.kind}: {section.title.strip()}")
        elif _NOTICE_TITLE.match(section.title):
            dropped.append(f"notice: {section.title.strip()}")
        else:
            kept.append(section)

    counts: dict[str, int] = {}
    rendered = _render(kept)
    segments: list[Segment] = []
    _push(segments, rendered)
    for rule in _rules(doc, extra_terms, rendered):
        segments = _apply(segments, rule, counts)
    body = "".join(text for text, _ in segments)
    text = HEADER + "\n\n" + body + "\n" if body else HEADER + "\n"
    return ScrubResult(text=text, counts=dict(sorted(counts.items())), dropped_sections=dropped)
