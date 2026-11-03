"""Tests for bars.leakcheck: fail-closed detection, severities, placeholder masking."""

import pytest

from bars import scrub as scrub_module
from bars.leakcheck import blocking, check
from bars.model import Author, Document, Leak, Section
from bars.scrub import HEADER, PLACEHOLDERS, scrub


def make_doc(*sections: Section, **fields) -> Document:
    values = dict(
        source_id="PMC7654321", source_type="jats",
        title="A fictional study of quokka genomes in the wild",
        authors=[Author("Quokkason", "Alice"), Author("Numbat", "Bertrand"), Author("Brown", "Carol"),
                 Author("Müller", "Jürgen"), Author("Zorb", "Dana"), Author("Li", "Wei")],
        affiliations=["Department of Marsupial Genomics, University of Wombatia, Australia"],
        institutions=["University of Wombatia"],
        emails=["alice@quokka-lab.org"], orcids=["0000-0002-1825-0097"],
        journal="Journal of Marsupial Biology", publisher="Wombat Press", year=2019,
        doi="10.1234/jmb.2019.42", pmid="31234567", pmcid="PMC7654321",
    )
    values.update(fields)
    return Document(sections=list(sections), **values)


def kinds(leaks: list[Leak]) -> set[tuple[str, str]]:
    return {(leak.kind, leak.severity) for leak in leaks}


RICH_TEXT = (
    "Quokkason et al. (2015) and Numbat and colleagues; Alice Quokkason, A. Quokkason, Quokkason A, "
    "Brown et al 2016, C. Brown, Müller, Muller, Zorb (2012), Li et al. Approved by the University "
    "of Wombatia ethics board. Mail ALICE@QUOKKA-LAB.ORG or bob [at] quokka-lab [dot] org; see "
    "quokka-lab.org. ORCID https://orcid.org/0000-0002-1825-0097. Code (github.com/quokkalab/x), "
    "history https://usegalaxy.org/u/aquokka/h/run, [wf](https://workflowhub.eu/workflows/1), "
    "data 10.5281/zenodo.1234567 and doi:10.1371/journal.pone.0123456. PMC7654321, PMID: 31234567. "
    "Published in the Journal of Marsupial Biology by Wombat Press with the Quokka Lab in 2021.\n"
    "| Quokkason et al | 0.5 |\n"
    "*Correspondence: alice@quokka-lab.org"
)


def test_scrubbed_output_passes_and_raw_text_blocks():
    doc = make_doc(Section("Results", RICH_TEXT))
    scrubbed = scrub(doc, ["Quokka Lab"]).text
    assert not blocking(check(scrubbed, doc, ["Quokka Lab"]))
    raw = check(RICH_TEXT, doc, ["Quokka Lab"])
    assert blocking(raw)
    assert {kind for kind, severity in kinds(raw) if severity == "block"} >= {
        "author_name", "citation", "surname", "institution", "email", "domain", "orcid", "url",
        "doi", "pmcid", "pmid", "journal", "term"}


def test_check_is_independent_of_scrub(monkeypatch):
    """Even a scrubber whose rules all vanished cannot get identifiers past the check."""
    monkeypatch.setattr(scrub_module, "_rules", lambda *args: [])
    doc = make_doc(Section("Results", RICH_TEXT))
    assert blocking(check(scrub(doc).text, doc, ["Quokka Lab"]))


@pytest.mark.parametrize("text, kind", [
    ("write to alice@quokka-lab.org", "email"),
    ("WRITE TO BOB@EXAMPLE.COM", "email"),
    ("bob [at] example [dot] com", "email"),
    ("iD 0000-0002-1825-0097", "orcid"),
    ("iD 0000000218250097", "orcid"),
    ("see https://example.com", "url"),
    ("see ftp://x", "url"),
    ("see www.example.com", "url"),
    ("see example.org/x", "url"),
    ("see github.com/q", "url"),
    ("see unimi.it/lab", "url"),
    ("mirror usegalaxy.be", "url"),
    ("ref 10.1371/journal.pone.0123456", "doi"),
    ("PMC123456", "pmcid"),
    ("PMID: 123", "pmid"),
    ("record 31234567", "pmid"),
    ("preprint arXiv:1905.01234", "arxiv"),
    ("by Alice Quokkason", "author_name"),
    ("by C. Brown", "author_name"),
    ("by Carol Brown", "author_name"),
    ("by Brown, Carol", "author_name"),
    ("by Wei Li", "author_name"),
    ("by ALICE QUOKKASON", "author_name"),
    ("Brown et al. showed", "citation"),
    ("Brown (2015) showed", "citation"),
    ("Li et al showed", "citation"),
    ("Quokkason", "surname"),
    ("QUOKKASON", "surname"),
    ("Zorb", "surname"),
    ("Muller and Mueller", "surname"),
    ("the University of Wombatia board", "institution"),
    ("Department of Marsupial Genomics", "institution"),
    ("quokka-lab.org", "domain"),
    ("JOURNAL OF MARSUPIAL BIOLOGY", "journal"),
    ("Wombat Press", "journal"),
])
def test_blocking_kinds(text, kind):
    leaks = check(f"{HEADER}\n\n{text}\n", make_doc())
    assert (kind, "block") in kinds(leaks)
    assert blocking(leaks)


def test_extra_terms_block_case_insensitively():
    leaks = check("the QUOKKA lab", make_doc(), ["Quokka Lab"])
    assert ("term", "block") in kinds(leaks)
    assert not check("the quokka labs", make_doc(), ["Quokka Lab"])


def test_single_word_journal_severity_follows_common_word_rule():
    assert ("journal", "block") in kinds(check("As eLife noted", make_doc(journal="eLife")))
    doc = make_doc(Section("Intro", "the nature of things"), journal="Nature", publisher="")
    assert kinds(check("Nature reviewed it", doc)) == {("journal", "warn")}


@pytest.mark.parametrize("text, kind", [
    ("Brown fat", "surname"),
    ("We report a fictional study of quokka genomes in captivity", "title"),
    ("*Correspondence: [EMAIL_REDACTED]", "correspondence"),
    ("the corresponding author", "correspondence"),
    ("In 2025 we resampled", "year"),
    ("Supplementary data are available at Bioinformatics online.", "journal"),
])
def test_warning_kinds(text, kind):
    leaks = check(text, make_doc())
    assert kinds(leaks) == {(kind, "warn")}
    assert not blocking(leaks)


def test_shortened_institution_blocks_and_acronyms_warn():
    doc = make_doc(affiliations=["University of Wombatia Medical School, Wombat City",
                                 "Cold Quokka Harbor Laboratory, Perth"],
                   institutions=[], emails=["x@unwom.edu.au", "y@nih.gov"])
    assert kinds(check("approved by the University of Wombatia committee", doc)) == {("institution", "block")}
    assert kinds(check("sequenced at CQHL and UNWOM", doc)) == {("institution", "warn")}
    assert not check("NIH 3T3 cells and the National Institute of Standards", doc)


def test_masked_venue_and_table_cell_numbers_are_clean():
    text = "Data are available at [JOURNAL_REDACTED] online.\n| MTG329 | 2049 | 46 |"
    assert not check(text, make_doc())


def test_heuristic_author_names_block_but_bare_tokens_warn():
    doc = make_doc(authors=[Author("Wombleton", "Gerald", heuristic=True), Author("Wang", "Wei", heuristic=True)])
    leaks = check("Gerald Wombleton and Wombleton (2001); Gerald again", doc)
    assert {("author_name", "block"), ("citation", "block"), ("heuristic_author", "warn")} <= kinds(leaks)
    assert kinds(check("We reused Wang's pipeline; Wombleton noted it", doc)) == {("surname", "warn")}
    # the same possessive is redacted by scrub, so the PDF path does not leak it
    scrubbed = scrub(make_doc(Section("Results", "We reused Wang's pipeline."), authors=doc.authors)).text
    assert "Wang" not in scrubbed and not check(scrubbed, doc)


def test_front_matter_lines_warn_without_hiding_blocks():
    doc = make_doc(front_matter="Running head: quokka genomes of the wild west\n"
                                "Alice Quokkason and the other authors of this paper\nshort")
    leaks = check("Running head: quokka genomes of the wild west\n"
                  "Alice Quokkason and the other authors of this paper", doc)
    assert ("front_matter", "warn") in kinds(leaks)
    assert ("author_name", "block") in kinds(leaks)


def test_names_do_not_span_paragraphs():
    leaks = check("measured in Brown\n\nC. elegans strains", make_doc())
    assert kinds(leaks) == {("surname", "warn")}


def test_surname_used_as_lowercase_word_is_common():
    doc = make_doc(Section("Intro", "The numbat is a marsupial."))
    assert kinds(check("Numbat populations declined", doc)) == {("surname", "warn")}
    assert ("citation", "block") in kinds(check("Numbat et al. disagree", doc))


def test_placeholders_never_flagged():
    text = HEADER + "\n\n" + " ".join(PLACEHOLDERS) + "\n" + ",".join(PLACEHOLDERS)
    doc = make_doc(authors=[Author("Redacted", "Url"), Author("Archive", "Data")],
                   institutions=["Public Database"], journal="Code Repo")
    assert check(text, doc, ["redacted", "URL", "CODE", "DOI"]) == []


def test_scientific_content_is_clean():
    text = ("PRJNA123456, SRR2021123, GSE2034, BRCA1, Lin28a, LiCl, Homo sapiens, E. coli K-12; "
            "3.14 ± 0.2 (n = 1999), p < 0.001; $y = x^2$; pipeline.py, model.pt, scipy.stats; "
            "the Wright-Fisher model; one-to-one correspondence; In 2019 and 2003.")
    assert check(text, make_doc()) == []


def test_nested_hits_reported_once_with_context():
    text = "line one\n" + "x " * 40 + "by Alice Quokkason\n" + "y " * 40
    leaks = check(text, make_doc())
    assert len(leaks) == 1
    leak = leaks[0]
    assert (leak.kind, leak.severity, leak.match) == ("author_name", "block", "Alice Quokkason")
    assert "\n" not in leak.context and "Alice Quokkason" in leak.context
    assert len(leak.context) <= 120


def test_blocking_helper():
    assert not blocking([])
    assert not blocking([Leak("year", "warn", "2025", "")])
    assert blocking([Leak("year", "warn", "2025", ""), Leak("email", "block", "a@b.org", "")])


# --- regressions ---------------------------------------------------------------------------------

def test_pdf_without_author_names_is_blocked_unless_terms_are_given():
    doc = make_doc(authors=[], source_type="pdf")
    leaks = check("Plain text.", doc)
    assert [(leak.kind, leak.severity) for leak in leaks] == [("no_authors", "block")]
    assert not check("Plain text.", doc, ["Quokkason"])
    assert not check("Plain text.", make_doc(authors=[]))  # JATS lists its authors explicitly


@pytest.mark.parametrize("text", ["see wombatia.it", "at quokkalab.science", "go to bit.ly/3QkLab",
                                  "the quokka.app"])
def test_lowercase_hosts_with_other_tlds_block(text):
    assert ("url", "block") in kinds(check(text, make_doc()))


def test_host_like_words_with_more_labels_warn():
    assert kinds(check("strains from igc.gulbenkian.pt", make_doc())) == {("url", "warn")}
    assert check("reads in data.tar.gz aligned to hg38.chr1.fa", make_doc()) == []


@pytest.mark.parametrize("text", [
    "Raw data are deposited in the Harvard Dataverse. The pipeline is maintained on GitLab. "
    "Workflows were run on UseGalaxy.",
    "Loaded with scipy.io and skimage.io; tf.io.gfile.",
    "The data end.It was clear.",
])
def test_words_that_only_look_like_hosts_are_clean(text):
    assert check(text, make_doc()) == []


@pytest.mark.parametrize("text", ["code on gitlab.com/q/x", "mirror usegalaxy.eu", "dataverse.harvard.edu"])
def test_host_prefixes_still_block(text):
    assert ("url", "block") in kinds(check(text, make_doc()))


def test_short_upper_case_surnames_are_acronyms():
    doc = make_doc(authors=[Author("Ali", "Sara"), Author("Mao", "Jun"), Author("Lee", "Min")])
    assert check("Mice with ALI showed more MAO-A; the LEE and SPI-1 islands were intact.", doc) == []
    assert ("citation", "block") in kinds(check("LEE et al. disagree", doc))


def test_compound_surname_parts_block():
    doc = make_doc(authors=[Author("García Márquez", "Juan")])
    for text in ("Márquez et al. (2017)", "Garcia-Marquez et al. 2017", "as Garcia (2016) showed"):
        assert ("citation", "block") in kinds(check(text, doc)), text


def test_common_word_surnames_block_only_unmistakable_citations():
    doc = make_doc(authors=[Author("May", "Ann"), Author("Rice", "Cy")])
    assert not blocking(check("Collected in May 2018; Rice and Arabidopsis genomes.", doc))
    for text in ("May et al. (2015)", "May (2015)", "Rice and Smith (2010)", "(Rice, 2012)"):
        assert ("citation", "block") in kinds(check(text, doc)), text


def test_reworded_institution_names_warn():
    doc = make_doc(affiliations=["University of Wombatia, Wombat City", "Institut Pasteur, Paris"], institutions=[])
    assert kinds(check("The Wombatia University IRB approved it.", doc)) == {("institution", "warn")}
    assert kinds(check("Strains from the Pasteur Institute.", doc)) == {("institution", "warn")}
    assert check("A Wombat burrow.", doc) == []  # 'Wombat City' is a place, not an organisation name


def test_resource_acronyms_from_affiliations_are_not_flagged():
    doc = make_doc(affiliations=["National Center for Biotechnology Information, Bethesda",
                                 "European Molecular Biology Laboratory, European Bioinformatics Institute"],
                   institutions=[], emails=["x@ncbi.nlm.nih.gov"])
    assert check("Reads from NCBI SRA and EMBL-EBI ENA.", doc) == []


def test_lowercase_journal_words_are_prose():
    doc = make_doc(journal="Genome Biology", publisher="")
    assert check("advances in genome biology", doc) == []
    assert ("journal", "block") in kinds(check("published in Genome Biology", doc))


def test_mit_license_is_not_the_institute():
    doc = make_doc(emails=["a@mit.edu"])
    assert check("released under the MIT license", doc) == []
    assert kinds(check("built at MIT", doc)) == {("institution", "warn")}


@pytest.mark.parametrize("text", [
    "Contact the lab at bob@wombat-uni.edu2 for samples.",
    "Batch ID 1234-5678-9012-3456-7 was used.",
    "We ran n = 2048 permutations and a 2048 bp window over 2031 genes.",
    "The UGA stop codon and LSU rRNA; NIH 3T3 cells.",
    "### Correspondence analysis\n\nCorrespondence analysis separated the lineages.",
])
def test_scrub_and_check_agree(text):
    """What scrub keeps as content, the check must not flag (and what it redacts must be gone)."""
    doc = make_doc(Section("Results", text), emails=["alice@quokka-lab.org", "x@uga.edu", "y@lsu.edu"])
    leaks = check(scrub(doc).text, doc)
    assert not blocking(leaks)
    assert not any(leak.kind in ("year", "correspondence") for leak in leaks)
