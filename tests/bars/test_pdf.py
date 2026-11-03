"""Tests for bars.pdf on the committed synthetic PDF and on small PDFs generated on the fly."""

from __future__ import annotations

import pytest

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

import make_synthetic_pdf
from bars import pdf
from bars.model import Author, Document
from bars.pdf import join_lines, parse_pdf

W, H = 595, 842


def text_item(x: float, y: float, text: str, size: float = 10, font: str = "helv") -> tuple:
    return x, y, text, size, font


def make_pdf(pages: list[list[tuple]], metadata: dict | None = None) -> bytes:
    """A PDF with the given (x, y, text, size, font) items on each page."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page(width=W, height=H)
        for x, y, text, size, font in items:
            page.insert_text((x, y), text, fontsize=size, fontname=font)
    if metadata:
        doc.set_metadata(metadata)
    data = doc.tobytes()
    doc.close()
    return data


def by_title(doc: Document, title: str):
    matches = [s for s in doc.sections if s.title == title]
    assert matches, f"no section {title!r} in {[s.title for s in doc.sections]}"
    return matches[0]


def all_text(doc: Document) -> str:
    return "\n".join(s.title + "\n" + s.text for s in doc.sections)


@pytest.fixture
def doc(test_data) -> Document:
    return parse_pdf((test_data / "synthetic.pdf").read_bytes(), "synthetic.pdf")


# --- committed synthetic fixture -----------------------------------------------------------------

def test_fixture_metadata(doc):
    assert doc.source_id == "synthetic.pdf" and doc.source_type == "pdf"
    assert doc.title == make_synthetic_pdf.TITLE
    assert [(a.surname, a.given) for a in doc.authors] == [("Quokkason", "Alice"), ("Numbat", "Bertrand")]
    assert doc.emails == ["alice@quokka-lab.org"]
    assert doc.orcids == ["0000-0002-1825-0097"]
    assert doc.affiliations == ["Department of Marsupial Genomics, University of Wombatia, Burrow City, Australia",
                                "Tasmanian Institute of Devil Studies, Hobart, Australia"]
    for name in ("Department of Marsupial Genomics", "University of Wombatia", "Tasmanian Institute of Devil Studies"):
        assert name in doc.institutions
    assert doc.journal == "Journal of Marsupial Genomics"
    assert doc.year == 2019
    assert (doc.doi, doc.pmid, doc.pmcid, doc.publisher) == ("", "", "", "")


def test_fixture_sections(doc):
    assert [(s.kind, s.title) for s in doc.sections] == [
        ("abstract", "Abstract"), ("body", "Introduction"), ("body", "Materials and methods"),
        ("body", "Results"), ("body", "Discussion"), ("availability", "Data availability"),
        ("ack", "Acknowledgements"), ("coi", "Competing interests"), ("refs", "References")]
    assert by_title(doc, "Competing interests").text == "The authors declare no competing interests."
    assert all(s.level == 2 for s in doc.sections)


def test_fixture_front_matter_is_set_aside(doc):
    for expected in ("Alice Quokkason", "alice@quokka-lab.org", "Published: 14 June 2019",
                     "Journal of Marsupial Genomics | Volume 7 | 2019", make_synthetic_pdf.TITLE.split()[0]):
        assert expected in doc.front_matter
    text = all_text(doc)
    for leaked in ("Alice Quokkason", "Bertrand Numbat", "alice@quokka-lab.org", "Published:", "ORCID",
                   "Volume 7", "Tasmanian Institute", "Burrow City"):
        assert leaked not in text, leaked
    assert not any(line.strip().isdigit() for line in text.splitlines())  # page numbers removed


def test_fixture_body_text(doc):
    assert "Quokkason et al. (2015) described" in by_title(doc, "Introduction").text
    assert "Numbat and colleagues later proposed" in by_title(doc, "Introduction").text
    methods = by_title(doc, "Materials and methods").text
    assert "maximum-likelihood phylogenetic tree" in methods  # line-break hyphen removed
    assert "University of Wombatia ethics board" in methods
    assert methods.count("\n\n") == 1  # two paragraphs, wrapped lines rejoined
    assert "In 2021 we resequenced" in by_title(doc, "Discussion").text
    availability = by_title(doc, "Data availability").text
    for url in ("https://github.com/quokkalab/marsupial-pipeline", "https://usegalaxy.org/u/aquokka/h/marsupial-run",
                "https://doi.org/10.5281/zenodo.1234567"):
        assert url in availability


def test_generator_matches_committed_fixture(test_data):
    rebuilt = parse_pdf(make_synthetic_pdf.build_pdf(), "rebuilt")
    committed = parse_pdf((test_data / "synthetic.pdf").read_bytes(), "rebuilt")
    assert rebuilt == committed


def test_heuristic_authors_without_pdf_metadata():
    doc = parse_pdf(make_synthetic_pdf.build_pdf(metadata_author=False), "x")
    assert doc.authors == [Author("Quokkason", "Alice", heuristic=True), Author("Numbat", "Bertrand", heuristic=True)]
    assert "2 author name(s) guessed heuristically from front matter" in doc.notes


# --- generated layouts ---------------------------------------------------------------------------

def test_two_column_reading_order():
    left, right = 50, 320
    page = [text_item(140, 60, "A full-width title spanning the page gutter", 16, "hebo"),
            text_item(left, 120, "Abstract", 12, "hebo"),
            text_item(left, 140, "Left column first paragraph line one"),
            text_item(left, 152, "continues on the left column."),
            text_item(left, 200, "Introduction", 12, "hebo"),
            text_item(left, 220, "Left column second paragraph."),
            text_item(right, 120, "Right column paragraph that comes later."),
            text_item(right, 160, "Results", 12, "hebo"),
            text_item(right, 180, "Right column results text.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.title, s.text) for s in doc.sections] == [
        ("Abstract", "Left column first paragraph line one continues on the left column."),
        ("Introduction", "Left column second paragraph.\n\nRight column paragraph that comes later."),
        ("Results", "Right column results text.")]
    assert "A full-width title" in doc.front_matter


def test_inline_declarations_and_metadata_blocks_on_first_page():
    page = [text_item(50, 60, "A study of digging", 18, "hebo"),
            text_item(50, 90, "Alice Quokkason, Bertrand Numbat"),
            text_item(50, 130, "Abstract", 12, "hebo"),
            text_item(50, 150, "First abstract paragraph without a full stop"),
            text_item(50, 190, "Funding: The Wombat Trust funded this work."),
            text_item(50, 230, "and its continuation after the sidebar."),
            text_item(50, 270, "Citation: Quokkason A, Numbat B (2019) A study of digging. J Dig 3: e1."),
            text_item(50, 310, "Data Availability: Code is at https://github.com/q/dig."),
            text_item(50, 350, "Introduction", 12, "hebo"),
            text_item(50, 370, "Body text.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.kind, s.title, s.text) for s in doc.sections] == [
        ("abstract", "Abstract", "First abstract paragraph without a full stop and its continuation after the sidebar."),
        ("funding", "Funding", "The Wombat Trust funded this work."),
        ("availability", "Data Availability", "Code is at https://github.com/q/dig."),
        ("body", "Introduction", "Body text.")]
    assert "Citation: Quokkason A" in doc.front_matter
    assert doc.year == 2019 or doc.year is None
    assert [(a.surname, a.heuristic) for a in doc.authors] == [("Quokkason", True), ("Numbat", True)]


def test_metadata_labels_need_a_colon_or_date():
    page = [text_item(50, 60, "Abstract", 12, "hebo"),
            text_item(50, 80, "Published estimates of burrow depth vary widely."),
            text_item(50, 120, "Received 3 May 2018; Accepted 20 May 2019; Published: 14 June 2019"),
            text_item(50, 160, "Correspondence to Alice Quokkason at the burrow."),
            text_item(50, 200, "Introduction", 12, "hebo"),
            text_item(50, 220, "Received wisdom says otherwise.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.title, s.text) for s in doc.sections] == [
        ("Abstract", "Published estimates of burrow depth vary widely."),
        ("Introduction", "Received wisdom says otherwise.")]
    assert "Received 3 May 2018" in doc.front_matter and "Correspondence to Alice" in doc.front_matter
    assert doc.year == 2019


def test_no_abstract_front_matter_ends_at_introduction():
    page = [text_item(50, 60, "Short communication title", 18, "hebo"),
            text_item(50, 90, "Alice Quokkason", 11),
            text_item(50, 110, "Department of Marsupial Genomics, University of Wombatia"),
            text_item(50, 150, "1. Introduction", 12, "hebo"),
            text_item(50, 170, "Wombats dig."),
            text_item(50, 210, "2.1 Results", 12, "hebo"),
            text_item(50, 230, "They dig a lot.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.title, s.level, s.text) for s in doc.sections] == [
        ("1. Introduction", 2, "Wombats dig."), ("2.1 Results", 3, "They dig a lot.")]
    assert doc.title == "Short communication title"
    assert doc.authors == [Author("Quokkason", "Alice", heuristic=True)]  # lone name right below the title
    assert doc.affiliations == ["Department of Marsupial Genomics, University of Wombatia"]
    assert doc.notes[0] == "no Abstract heading found; front matter = first-page text before the first section heading"


def test_no_headings_first_page_is_front_matter():
    doc = parse_pdf(make_pdf([[text_item(50, 60, "Cover page by Alice Quokkason")],
                              [text_item(50, 60, "Plain text on the second page.")]]), "x")
    assert [(s.title, s.kind, s.text) for s in doc.sections] == [("Main text", "body", "Plain text on the second page.")]
    assert "Cover page" in doc.front_matter
    assert doc.notes[0] == "no Abstract heading found; front matter = the whole first page"


def test_running_headers_and_page_numbers_removed():
    pages = []
    for n in range(1, 5):
        header = "Quokkason et al." if n % 2 else "Wombat burrowing genes"
        items = [text_item(50, 30, f"{header}    Page {n} of 4", 8),
                 text_item(50, 60, "PLOS ONE | https://doi.org/10.5555/x.1 | June 2019"),
                 text_item(290, 820, str(n), 8)]
        if n == 1:
            items += [text_item(50, 100, "Abstract", 12, "hebo"), text_item(50, 120, "Abstract text.")]
        items += [text_item(50, 400, f"Body sentence on page {n}.")]
        pages.append(items)
    doc = parse_pdf(make_pdf(pages), "x")
    text = all_text(doc)
    assert "Page" not in text and "PLOS" not in text and "Quokkason" not in text
    assert doc.sections[0].text.split("\n\n") == ["Abstract text."] + [f"Body sentence on page {n}." for n in range(1, 5)]
    assert doc.journal == "PLOS ONE"
    assert doc.doi == "10.5555/x.1"
    assert "Quokkason et al." in doc.front_matter
    assert any(n.startswith("removed ") for n in doc.notes)


def test_metadata_title_and_junk_author():
    page = [text_item(50, 60, "Abstract", 12, "hebo"), text_item(50, 80, "Text.")]
    doc = parse_pdf(make_pdf([page], {"title": "Microsoft Word - draft_v3.docx", "author": "Office User"}), "x")
    assert doc.title == "" and doc.authors == []
    doc = parse_pdf(make_pdf([page], {"title": "A proper article title here", "author": "Alice Quokkason; B. Numbat"}), "x")
    assert doc.title == "A proper article title here"
    assert doc.authors == [Author("Quokkason", "Alice"), Author("Numbat", "B.")]


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.4 truncated"])
def test_unreadable_pdf_raises_value_error(data):
    with pytest.raises(ValueError):
        parse_pdf(data, "bad")


def test_pdf_without_text_raises_value_error():
    with pytest.raises(ValueError, match="no extractable text"):
        parse_pdf(make_pdf([[]]), "scan")


@pytest.mark.parametrize("lines, joined", [
    (["the phylo-", "genetic tree"], "the phylogenetic tree"),
    (["COVID-", "19 cases"], "COVID-19 cases"),
    (["see https://example.org/long-", "path/file for"], "see https://example.org/long-path/file for"),
    (["at https://github.com/quokkalab/", "marsupial-pipeline."], "at https://github.com/quokkalab/marsupial-pipeline."),
    (["soft­", "hyphen"], "softhyphen"),
    (["a dash -", "then text"], "a dash - then text"),
    (["  wrapped  ", "", "line "], "wrapped line"),
])
def test_join_lines(lines, joined):
    assert join_lines(lines) == joined


# --- front matter, headings and blocks found in publisher layouts ----------------------------------

def article_page(author_line: str, *body: str) -> list[tuple]:
    """First page: title, author line, Abstract, Introduction with the given body lines."""
    items = [text_item(60, 80, "Convergent evolution of burrowing genes in wombats", 18, "hebo"),
             text_item(60, 110, author_line, 11),
             text_item(60, 150, "Abstract", 12, "hebo"),
             text_item(60, 170, "Burrowing has evolved repeatedly in marsupials and we test many genes."),
             text_item(60, 200, "Introduction", 12, "hebo")]
    return items + [text_item(60, 220 + 16 * i, line) for i, line in enumerate(body)]


@pytest.mark.parametrize("line, expected", [
    ("Alice Quokkason1 · Bertrand Numbat2", [("Quokkason", "Alice"), ("Numbat", "Bertrand")]),
    ("Alice Quokkason1 | Bertrand Numbat2 | Wei Zhang1",
     [("Quokkason", "Alice"), ("Numbat", "Bertrand"), ("Zhang", "Wei")]),
    ("Alice Quokkason, PhD, and Bertrand Numbat, MD", [("Quokkason", "Alice"), ("Numbat", "Bertrand")]),
    ("Alice Quokkason MD PhD1, Bertrand Numbat MSc2", [("Quokkason", "Alice"), ("Numbat", "Bertrand")]),
    ("ALICE QUOKKASON, BERTRAND NUMBAT AND WEI LI",
     [("Quokkason", "Alice"), ("Numbat", "Bertrand"), ("Li", "Wei")]),
    ("Ronald McDonald1, Ann DeWitt2 & José Pérez1",
     [("McDonald", "Ronald"), ("DeWitt", "Ann"), ("Pérez", "José")]),
])
def test_author_line_formats(line, expected):
    doc = parse_pdf(make_pdf([article_page(line, "Wombats dig burrows.")]), "x")
    assert [(a.surname, a.given) for a in doc.authors] == expected
    assert all(a.heuristic for a in doc.authors)


@pytest.mark.parametrize("line, expected", [
    ("Michał Kowalski1, Jiří Novák2 and Łukasz Wiśniewski1",
     [("Kowalski", "Michał"), ("Novák", "Jiří"), ("Wiśniewski", "Łukasz")]),
    ("Ştefan Popescu1 · Nguyễn Văn An2", [("Popescu", "Ştefan"), ("An", "Nguyễn Văn")]),
])
def test_author_names_in_any_latin_script(line, expected):
    # base-14 PDF fonts cannot encode these letters, so the line parser is tested directly
    found = pdf._names_in_line(pdf._Line(text=line, plain=line, page=0, block=0, size=11), allow_single=False)
    assert [(a.surname, a.given) for a in found] == expected


def test_title_starting_with_summary_is_not_the_abstract_heading():
    page = [text_item(60, 80, "Summary Statistics Imputation Improves Fine-Mapping", 18, "hebo"),
            text_item(60, 110, "Alice Quokkason1*, Bertrand Numbat2 and Wei Zhang1", 11),
            text_item(60, 130, "1 Department of Marsupial Genomics, University of Wombatia, Wombat City", 9),
            text_item(60, 170, "Abstract", 12, "hebo"),
            text_item(60, 190, "Fine-mapping uses summary statistics from 42 cohorts."),
            text_item(60, 220, "Introduction", 12, "hebo"),
            text_item(60, 240, "Quokkason et al. (2015) proposed the imputation approach.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.kind, s.title) for s in doc.sections] == [("abstract", "Abstract"), ("body", "Introduction")]
    assert [a.surname for a in doc.authors] == ["Quokkason", "Numbat", "Zhang"]
    assert doc.title == "Summary Statistics Imputation Improves Fine-Mapping"
    assert "Wombat City" in doc.front_matter and "Wombat City" not in all_text(doc)


def test_alternating_running_heads_are_removed_and_name_the_journal():
    pages = [article_page("Alice Quokkason1, Bertrand Numbat2", "Wombats dig burrows.")]
    for n in range(1, 11):
        head = "A. Quokkason et al." if n % 2 else "Journal of Marsupial Genomics 7 (2019) 123–135"
        pages.append([text_item(60, 40, head, 8), text_item(60, 400, f"Body text on page {n + 1} {'x' * n}."),
                      text_item(290, 820, str(123 + n), 8)])
    doc = parse_pdf(make_pdf(pages), "x")
    text = all_text(doc)
    assert "Marsupial Genomics" not in text and "Quokkason et al." not in text
    assert all(f"Body text on page {n + 1}" in text for n in range(1, 11))
    assert doc.journal == "Journal of Marsupial Genomics"


def test_masthead_names_the_journal_when_no_head_repeats():
    page = [text_item(60, 40, "Journal of Marsupial Genomics 7 (2019) 123–135", 8)] + \
        article_page("Alice Quokkason1, Bertrand Numbat2", "Wombats dig burrows.")
    assert parse_pdf(make_pdf([page]), "x").journal == "Journal of Marsupial Genomics"


def make_box_pdf(boxes: list[tuple[float, str, float]]) -> bytes:
    """One page of text boxes (top y, text, font size); each box is one pymupdf block."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    for y, text, size in boxes:
        assert page.insert_textbox(pymupdf.Rect(60, y, 535, y + 60), text, fontsize=size) >= 0
    data = doc.tobytes()
    doc.close()
    return data


def test_short_availability_block_with_email_is_a_section_not_metadata():
    data = make_box_pdf([(60, "Convergent evolution of burrowing genes in wombats", 18),
                         (110, "Alice Quokkason1, Bertrand Numbat2", 11),
                         (160, "Abstract", 12),
                         (200, "We compared 42 marsupial genomes and tested 1,204 genes.", 10),
                         (260, "Data Availability: Alignments are at https://github.com/quokkalab/burrow; raw "
                               "reads are available on request from alice@quokka-lab.org.", 9),
                         (330, "Introduction", 12),
                         (370, "Fossorial mammals share skeletal adaptations for digging.", 10)])
    doc = parse_pdf(data, "x")
    assert [(s.kind, s.title) for s in doc.sections] == [
        ("abstract", "Abstract"), ("availability", "Data Availability"), ("body", "Introduction")]
    assert "https://github.com/quokkalab/burrow" in by_title(doc, "Data Availability").text
    assert doc.emails == ["alice@quokka-lab.org"]


def test_heading_merged_into_an_email_block_is_not_swallowed():
    page = [text_item(60, 80, "Convergent evolution of burrowing genes in wombats", 18, "hebo"),
            text_item(60, 108, "Alice Quokkason1, Bertrand Numbat2", 11),
            text_item(60, 128, "Abstract", 12, "hebo"),
            text_item(60, 150, "We compared 42 marsupial genomes."),
            text_item(60, 168, "Data Availability: Alignments are at https://github.com/quokkalab/burrow; raw", 9),
            text_item(60, 184, "reads are available on request from alice@quokka-lab.org.", 9),
            text_item(60, 199, "Introduction", 12, "hebo"),
            text_item(60, 222, "Fossorial mammals share skeletal adaptations for digging.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.kind, s.title) for s in doc.sections] == [
        ("abstract", "Abstract"), ("availability", "Data Availability"), ("body", "Introduction")]
    assert by_title(doc, "Data Availability").text.endswith("on request from alice@quokka-lab.org.")


def test_table_column_named_reference_is_not_a_heading():
    page = article_page("Alice Quokkason1, Bertrand Numbat2", "Wombats dig burrows.")
    page += [text_item(60, 260, "Results", 12, "hebo"), text_item(60, 280, "Table 1. Genomes analysed.")]
    for k, row in enumerate([("Species", "Genome size", "Reference"), ("Vombatus ursinus", "3.2 Gb", "[12]")]):
        page += [text_item(x, 300 + 16 * k, cell) for x, cell in zip((60, 260, 420), row, strict=True)]
    page += [text_item(60, 350, "Thirty one genes showed episodic positive selection."),
             text_item(60, 380, "Discussion", 12, "hebo"), text_item(60, 400, "Selection is convergent.")]
    doc = parse_pdf(make_pdf([page]), "x")
    assert [(s.kind, s.title) for s in doc.sections][-2:] == [("body", "Results"), ("body", "Discussion")]
    assert "Thirty one genes" in by_title(doc, "Results").text


def test_affiliation_footnotes_below_the_abstract_go_to_front_matter():
    page = article_page("Alice Quokkason1*, Bertrand Numbat2", "Fossorial mammals share adaptations.")
    page += [text_item(60, 760, "1 Department of Marsupial Genomics, University of Wombatia, Wombat City, "
                                "Australia", 7),
             text_item(60, 772, "2 Institute of Fossorial Biology, Burrow Town, New Zealand", 7)]
    doc = parse_pdf(make_pdf([page]), "x")
    assert doc.affiliations == ["Department of Marsupial Genomics, University of Wombatia, Wombat City, Australia",
                                "Institute of Fossorial Biology, Burrow Town, New Zealand"]
    assert "Burrow Town" not in all_text(doc) and "Burrow Town" in doc.front_matter
    assert by_title(doc, "Introduction").text == "Fossorial mammals share adaptations."
