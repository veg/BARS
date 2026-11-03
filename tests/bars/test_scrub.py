"""Tests for bars.scrub: typed placeholders, name/venue redaction and content preservation."""

import pytest

from bars.model import Author, Document, Section
from bars.scrub import (AUTHOR, CODE_REPO_URL, DATA_ARCHIVE_DOI, DATA_ARCHIVE_URL, DOI, DOMAIN,
                        EMAIL, GALAXY_HISTORY_URL, GALAXY_URL, GALAXY_WORKFLOW_URL, HEADER, ID,
                        INSTITUTION, JOURNAL, METADATA, ORCID, PUBLIC_DATABASE_URL, TERM, TITLE, URL,
                        WORKFLOW_REGISTRY_URL, YEAR, classify_url, scrub)

AUTHORS = [
    Author("Quokkason", "Alice"),
    Author("Numbat", "Bertrand"),
    Author("Brown", "Carol"),
    Author("Müller", "Jürgen"),
    Author("Picard", "Jean-Luc"),
    Author("van der Berg", "Hendrik"),
    Author("Zorb", "Dana"),
    Author("Li", "Wei"),
]


def make_doc(*sections: Section, **fields) -> Document:
    values = dict(
        source_id="PMC7654321", source_type="jats",
        title="A fictional study of quokka genomes in the wild",
        authors=list(AUTHORS),
        affiliations=["1 Department of Marsupial Genomics, University of Wombatia, Wombat City 2600, "
                      "Australia", "Institute of Things, Springfield, United States"],
        institutions=["University of Wombatia"],
        emails=["alice@quokka-lab.org", "bnumbat@bio.psu.edu"],
        journal="Journal of Marsupial Biology", publisher="Wombat Press", year=2019,
        doi="10.1234/jmb.2019.42", pmid="31234567", pmcid="PMC7654321",
    )
    values.update(fields)
    return Document(sections=list(sections), **values)


def body(text: str, extra_terms=(), **fields) -> str:
    """Scrub one body section and return only its scrubbed text."""
    result = scrub(make_doc(Section("Results", text), **fields), extra_terms)
    prefix = HEADER + "\n\n## Results\n\n"
    assert result.text.startswith(prefix)
    return result.text[len(prefix):].rstrip("\n")


# --- document structure -------------------------------------------------------------------

def test_header_sections_and_metadata_never_emitted():
    doc = make_doc(
        Section("", "We sequenced quokkas.", "abstract"),
        Section("Methods", "Reads were mapped.", "body", 2),
        Section("Alignment", "We used minimap2.", "body", 3),
        Section("Data availability", "All data are in the supplement.", "availability"),
        Section("Acknowledgements", "We thank Gerald Wombleton.", "ack"),
        Section("Funding", "Grant W-1.", "funding"),
        Section("Author contributions", "AQ wrote it.", "contrib"),
        Section("Competing interests", "None.", "coi"),
        Section("References", "1. Quokkason A. Title. J Marsupial Biol. 2015.", "refs"),
        Section("Retraction Note", "This article has been retracted.", "back"),
        Section("Correction for multiple testing", "Bonferroni.", "body", 2),
    )
    result = scrub(doc)
    assert result.text == (
        "# [REDACTED FOR BLINDED REVIEW]\n\n## Abstract\n\nWe sequenced quokkas.\n\n"
        "## Methods\n\nReads were mapped.\n\n### Alignment\n\nWe used minimap2.\n\n"
        "## Data availability\n\nAll data are in the supplement.\n\n"
        "## Correction for multiple testing\n\nBonferroni.\n"
    )
    assert result.dropped_sections == [
        "ack: Acknowledgements", "funding: Funding", "contrib: Author contributions",
        "coi: Competing interests", "refs: References", "notice: Retraction Note",
    ]
    assert result.counts == {}
    for secret in ("fictional study", "Marsupial Biology", "Wombat", "2019", "10.1234", "31234567",
                   "7654321", "Quokkason", "Wombleton"):
        assert secret not in result.text


def test_heading_level_is_clamped():
    result = scrub(make_doc(Section("Deep", "x", "body", 9), Section("Top", "y", "body", 1)))
    assert "\n###### Deep\n\nx\n\n## Top\n\ny\n" in result.text


def test_empty_document_has_header_only():
    assert scrub(make_doc()).text == HEADER + "\n"


# --- URLs, DOIs and other identifiers ------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Code at github.com/quokkalab/marsupial-pipeline.", f"Code at {CODE_REPO_URL}."),
    ("(https://github.com/quokkalab/x)", f"({CODE_REPO_URL})"),
    ("<https://gitlab.com/q/x>", f"<{CODE_REPO_URL}>"),
    ("[repo](https://bitbucket.org/q/x)", f"[repo]({CODE_REPO_URL})"),
    ("see https://codeberg.org/q/x, and", f"see {CODE_REPO_URL}, and"),
    ("**https://GitHub.com/Q/X**", f"**{CODE_REPO_URL}**"),
    ("| github.com/q/x | 0.5 |", f"| {CODE_REPO_URL} | 0.5 |"),
    ("site quokkalab.github.io/docs;", f"site {CODE_REPO_URL};"),
    ("sourceforge.net/projects/x and gitee.com/a/b", f"{CODE_REPO_URL} and {CODE_REPO_URL}"),
    ("https://usegalaxy.org/u/aquokka/h/marsupial-run;", f"{GALAXY_HISTORY_URL};"),
    ("usegalaxy.eu/u/aquokka/w/wf", GALAXY_WORKFLOW_URL),
    ("https://usegalaxy.org.au/published/workflow?id=abc1", GALAXY_WORKFLOW_URL),
    ("https://usegalaxy.be/histories/view?id=1", GALAXY_HISTORY_URL),
    ("https://usegalaxy.org/published/history?id=f2db41e1fa331b3e", GALAXY_HISTORY_URL),
    ("https://usegalaxy.org/workflows/run?id=1", GALAXY_WORKFLOW_URL),
    ("https://galaxy.example.edu/", GALAXY_URL),
    ("(usegalaxy.fr)", f"({GALAXY_URL})"),
    ("https://galaxyproject.org/news", GALAXY_URL),
    ("https://myserver.example.org/u/bob/h/run1", GALAXY_HISTORY_URL),
    ("https://my.server.org/datasets/f2db41e1fa331b3e/display", GALAXY_URL),
    ("https://workflowhub.eu/workflows/123?version=2", WORKFLOW_REGISTRY_URL),
    ("https://dockstore.org/workflows/x and nf-co.re/rnaseq",
     f"{WORKFLOW_REGISTRY_URL} and {WORKFLOW_REGISTRY_URL}"),
    ("https://zenodo.org/records/123", DATA_ARCHIVE_URL),
    ("figshare.com/articles/x", DATA_ARCHIVE_URL),
    ("https://datadryad.org/stash/dataset/doi:10.5061/dryad.x", DATA_ARCHIVE_URL),
    ("https://osf.io/abcd/", DATA_ARCHIVE_URL),
    ("https://dataverse.harvard.edu/dataset.xhtml?persistentId=x", DATA_ARCHIVE_URL),
    ("https://data.mendeley.com/datasets/x", DATA_ARCHIVE_URL),
    ("https://www.ncbi.nlm.nih.gov/bioproject/PRJNA123456", PUBLIC_DATABASE_URL),
    ("https://www.ebi.ac.uk/ena/browser/view/PRJEB1", PUBLIC_DATABASE_URL),
    ("https://www.rcsb.org/structure/1ABC", PUBLIC_DATABASE_URL),
    ("https://www.uniprot.org/uniprot/P12345", PUBLIC_DATABASE_URL),
    ("https://gisaid.org/x", PUBLIC_DATABASE_URL),
    ("https://doi.org/10.5281/zenodo.1234567.", f"{DATA_ARCHIVE_DOI}."),
    ("https://dx.doi.org/10.1371/journal.pone.0123456)", f"{DOI})"),
    ("doi.org/10.6084/m9.figshare.1", DATA_ARCHIVE_DOI),
    ("doi:10.6084/m9.figshare.123", f"doi:{DATA_ARCHIVE_DOI}"),
    ("(10.17605/OSF.IO/ABCDE)", f"({DATA_ARCHIVE_DOI})"),
    ("Dryad 10.5061/dryad.abc12;", f"Dryad {DATA_ARCHIVE_DOI};"),
    ("10.1016/S0140-6736(20)30183-5.", f"{DOI}."),
    ("[10.1093/nar/gkz1](https://doi.org/10.1093/nar/gkz1)", f"[{DOI}]({DOI})"),
    ("https://www.quokkalab.org/people", URL),
    ("visit www.quokkalab.org.", f"visit {URL}."),
    ("ftp://ftp.example.org/pub/x", URL),
    ("s3://bucket/key", URL),
    ("https://en.wikipedia.org/wiki/Quokka_(animal)).", f"{URL})."),
    ("see unimi.it/lab/page", f"see {URL}"),
    ("http://127.0.0.1:8080/x", URL),
])
def test_urls_and_dois(text, expected):
    assert body(text) == expected


def test_url_classifier_directly():
    assert classify_url("github.com/a/b") == CODE_REPO_URL
    assert classify_url("https://quokka-lab.org", ["quokka-lab.org"]) == URL
    assert classify_url("lab.quokka-lab.org", ["quokka-lab.org"]) == DOMAIN
    assert classify_url("www.quokka-lab.org", ["quokka-lab.org"]) == URL


def test_file_names_and_dotted_code_are_not_urls():
    text = "Run pipeline.py, model.pt and run.sh; call scipy.stats and data.table; e.g. i.e. U.S."
    assert body(text) == text


@pytest.mark.parametrize("text, expected", [
    ("write to alice@quokka-lab.org.", f"write to {EMAIL}."),
    ("write to ALICE@QUOKKA-LAB.ORG today", f"write to {EMAIL} today"),
    ("[Alice](mailto:alice@quokka-lab.org)", f"[Alice]({EMAIL})"),
    ("(bob.smith+lab@sub.uni-wombatia.edu.au)", f"({EMAIL})"),
    ("bob [at] quokka-lab [dot] org. Next", f"{EMAIL}. Next"),
    ("carol(at)uni-wombatia.edu.au, ok", f"{EMAIL}, ok"),
    ("ORCID 0000-0002-1825-0097.", f"ORCID {ORCID}."),
    ("https://orcid.org/0000-0002-1825-009X", ORCID),
    ("[iD](https://orcid.org/0000-0002-1825-0097)", f"[iD]({ORCID})"),
    ("orcid.org/0000-0002-1825-0097", ORCID),
    ("PMC7654321 and PMC 12345", f"{ID} and {ID}"),
    ("PMID: 31234567; PMID 999; PubMed ID 42", f"{ID}; {ID}; {ID}"),
    ("record 31234567.", f"record {ID}."),
    ("arXiv:1905.01234v2 and arXiv: hep-th/9901001.", f"{ID} and {ID}."),
])
def test_emails_orcids_and_pubmed_ids(text, expected):
    assert body(text) == expected


def test_scientific_content_survives():
    text = ("Reads were deposited under PRJNA123456 (SRR1234567, SRR2021123), GSE2034 and ERR123456; "
            "structures 1ABC; MN908947.3. We studied BRCA1, TP53, Lin28a and LiCl in Homo sapiens, "
            "Escherichia coli K-12, E. coli and Notch signalling. Mean 3.14159 ± 0.002 (n = 2048, "
            "2048 × 2048 px, 2050% increase), p < 0.001; $y = \\beta_0 + \\beta_1 x^2$; 10.5 % of "
            "1,234,567 reads; chr7:117,559,590; 3.2025; v2.20.1; Brown adipose tissue and young "
            "adults; the Wright-Fisher model, Smith-Waterman, Hardy-Weinberg and Kaplan-Meier.")
    assert body(text) == text


# --- author names -------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Alice Quokkason", "Alice B. Quokkason", "A. Quokkason", "A.Quokkason", "A. B. Quokkason",
    "AB Quokkason", "Quokkason A.", "Quokkason, A.", "Quokkason, Alice", "Quokkason AB",
    "QUOKKASON", "ALICE QUOKKASON", "Quokkason", "Bertrand Numbat", "B. Numbat", "Numbat",
    "J.-L. Picard", "J-L Picard", "JL Picard", "Jean-Luc Picard", "Picard, J.-L.", "Picard",
    "Jürgen Müller", "J. Müller", "Müller", "Muller", "Mueller", "MÜLLER",
    "Hendrik van der Berg", "van der Berg", "H. van der Berg",
    "Carol Brown", "C. Brown", "Brown, C.", "Wei Li", "W. Li",
])
def test_author_name_forms_fully_redacted(text):
    assert body(f"x {text} y") == f"x {AUTHOR} y"


@pytest.mark.parametrize("text, expected", [
    ("Quokkason et al. (2015) showed", f"{AUTHOR} et al. (2015) showed"),
    ("(Quokkason et al 2015)", f"({AUTHOR} et al 2015)"),
    ("Numbat and colleagues found", f"{AUTHOR} and colleagues found"),
    ("Brown et al. reported", f"{AUTHOR} et al. reported"),
    ("Brown and Smith (2010)", f"{AUTHOR} and Smith (2010)"),
    ("as Brown (2012) argued", f"as {AUTHOR} (2012) argued"),
    ("(Brown, 2012)", f"({AUTHOR}, 2012)"),
    ("Brown [12] showed", f"{AUTHOR} [12] showed"),
    ("the Brown lab and Brown's group", f"the {AUTHOR} lab and {AUTHOR}'s group"),
    ("Zorb et al. and Zorb (2012)", f"{AUTHOR} et al. and {AUTHOR} (2012)"),
    ("Li et al. used LiCl", f"{AUTHOR} et al. used LiCl"),
    ("Van der Berg et al. and Berg et al.", f"{AUTHOR} et al. and {AUTHOR} et al."),
    ("Quokkason A, Numbat B (2015)", f"{AUTHOR}, {AUTHOR} (2015)"),
    ("Quokkason’s pipeline and Quokkason1", f"{AUTHOR}’s pipeline and {AUTHOR}1"),
    ("| Quokkason et al. | 2015 |", f"| {AUTHOR} et al. | 2015 |"),
    ("_Quokkason_ wrote", f"_{AUTHOR}_ wrote"),
    ("QUOKKASON ET AL. (2015)", f"{AUTHOR} ET AL. (2015)"),
    # reference-list style with someone else's initials (dataset citations in availability sections)
    ("Gajria B, Zorb H, Brunk PB. 2007. ToxoDB.", f"Gajria B, {AUTHOR} H, Brunk PB. 2007. ToxoDB."),
    ("Treatman C, Zorb HY.", f"Treatman C, {AUTHOR} HY."),
    ("the Zorb A domain and Zorb HQ5 cells", "the Zorb A domain and Zorb HQ5 cells"),
])
def test_citation_forms(text, expected):
    assert body(text) == expected


def test_common_and_short_surnames_survive_as_words():
    text = "Brown fat, a Brown rat and Field plots; Zorb was kept; Li-ion and Li+ ions."
    assert body(text) == text  # Brown is common; Zorb/Li are below the standalone length


def test_names_do_not_span_paragraphs():
    text = "Fat was measured in Brown\n\nC. elegans strains; Alice\n\nQuokkason-like."
    assert body(text) == f"Fat was measured in Brown\n\nC. elegans strains; Alice\n\n{AUTHOR}-like."


def test_bare_article_is_not_taken_for_an_initial():
    assert body("A Quokkason-like model") == f"A {AUTHOR}-like model"


def test_lowercase_use_in_document_makes_surname_common():
    doc = make_doc(Section("Intro", "Numbat populations declined. The numbat is a marsupial. "
                                    "Numbat et al. disagree."))
    text = scrub(doc).text
    assert "Numbat populations declined" in text
    assert f"{AUTHOR} et al. disagree" in text


def test_lowercase_use_inside_urls_and_emails_does_not_count():
    doc = make_doc(Section("Intro", "Quokkason built it: see quokkason@x.org and x.org/quokkason."))
    assert scrub(doc).text.count(AUTHOR) == 1


def test_consortium_and_hyphenated_names_are_never_common_words():
    doc = make_doc(Section("Intro", "The quokka genome consortium idea: Quokka Genome Consortium "
                                    "data and Brown-Young plots; brown and young."),
                   authors=[Author("Quokka Genome Consortium"), Author("Brown-Young", "Ann")])
    assert f"idea: {AUTHOR} data and {AUTHOR} plots; brown and young." in scrub(doc).text


def test_ascii_metadata_matches_accented_text():
    out = body("Grégoire Etienne and Étienne", authors=[Author("Etienne", "Gregoire")])
    assert out == f"{AUTHOR} and {AUTHOR}"


def test_heuristic_authors_are_redacted_too():
    out = body("Wombleton (2001) and Gerald Wombleton",
               authors=[Author("Wombleton", "Gerald", heuristic=True)])
    assert out == f"{AUTHOR} (2001) and {AUTHOR}"


# --- institutions, domains, venue, terms ---------------------------------------------------

def test_institutions_and_places():
    out = body("Approved by the University of Wombatia ethics board (UNIVERSITY OF WOMBATIA); "
               "staff of the Department of Marsupial Genomics, Institute of Things; "
               "samples from the United States and Australia.")
    assert out == (f"Approved by the {INSTITUTION} ethics board ({INSTITUTION}); staff of the "
                   f"{INSTITUTION}, {INSTITUTION}; samples from the United States and Australia.")


def test_shortened_institution_names():
    # 'University of Michigan' must go when the affiliation says 'University of Michigan Medical School'.
    out = body("Approved by the University of Wombatia committee and the Max Quokka Institute; samples "
               "from the National Institute of Standards and the Wombatia Medical School.",
               affiliations=["University of Wombatia Medical School, Wombat City, Australia",
                             "Max Quokka Institute for Marsupial Biology, Perth",
                             "National Institute of Marsupial Health, Australia"], institutions=[])
    assert out == (f"Approved by the {INSTITUTION} committee and the {INSTITUTION}; samples from the "
                   "National Institute of Standards and the Wombatia Medical School.")


def test_institution_acronyms_from_names_and_email_domains():
    out = body("Sequenced at CQHL and UNWOM; NIH 3T3 cells; EMBL-EBI; cqhl and CQHLX stay.",
               affiliations=["Cold Quokka Harbor Laboratory, Wombat City", "Department of Things, Perth"],
               institutions=[], emails=["x@unwom.edu.au", "y@nih.gov", "z@ebi.ac.uk"])
    assert out == f"Sequenced at {INSTITUTION} and {INSTITUTION}; NIH 3T3 cells; EMBL-EBI; cqhl and CQHLX stay."
    out = body("Built at MIT; released under the MIT license.", emails=["a@mit.edu"])
    assert out == f"Built at {INSTITUTION}; released under the MIT license."


def test_footnote_like_affiliation_and_correspondence_lines():
    text = ("¹ University of Wombatia, Department of Marsupial Genomics\n"
            "*Correspondence: ALICE@QUOKKA-LAB.ORG (A.Q.)\n"
            "Received: 1 May 2019; Accepted: 2 June 2019\n"
            "© 2019 Quokkason et al. This is an open access article.\n"
            "Received signals were filtered.")
    assert body(text) == (f"¹ {INSTITUTION}, {INSTITUTION}\n{METADATA}\n{METADATA}\n{METADATA}\n"
                          "Received signals were filtered.")


def test_email_domains_as_text():
    out = body("Hosted at quokka-lab.org and psu.edu; https://quokka-lab.org/people.")
    assert out == f"Hosted at {DOMAIN} and {DOMAIN}; {URL}."


def test_journal_and_publisher():
    out = body("Unlike the Journal of Marsupial Biology (JOURNAL OF MARSUPIAL BIOLOGY) and "
               "Wombat Press, a journal of marsupial biology is lowercase prose.")
    assert out == f"Unlike the {JOURNAL} ({JOURNAL}) and {JOURNAL}, a journal of marsupial biology " \
                  "is lowercase prose."


def test_single_word_journal_only_when_not_a_common_word():
    assert body("As eLife noted", journal="eLife", publisher="") == f"As {JOURNAL} noted"
    text = "Nature of the data; Nature reviewed nature."
    assert body(text, journal="Nature", publisher="") == text


def test_publisher_boilerplate_naming_the_venue():
    out = body("Supplemental material available at G3 online. Supplementary data are available at "
               "Molecular Biology and Evolution online. Kits are available at the time of order online.")
    assert out == (f"Supplemental material available at {JOURNAL} online. Supplementary data are available "
                   f"at {JOURNAL} online. Kits are available at the time of order online.")


def test_full_title_quoted_in_text():
    out = body("Additional file 1. Supplementary Figures for A Fictional Study of Quokka-Genomes in the "
               "Wild. A fictional study of quokka genomes was planned.")
    assert out == f"Additional file 1. Supplementary Figures for {TITLE}. A fictional study of quokka genomes " \
                  "was planned."


def test_extra_terms_are_case_insensitive_whole_phrases():
    out = body("The Quokka Lab and the quokka   lab use QL-7 but quokka labs and xQL-7 stay.",
               extra_terms=["Quokka Lab", "QL-7", "a", " "])
    assert out == f"The {TERM} and the {TERM} use {TERM} but quokka labs and xQL-7 stay."


def test_placeholders_are_never_rewritten_by_later_rules():
    out = body("github.com/q/x, a@b.org, https://zenodo.org/1 and 10.5281/zenodo.2025123",
               extra_terms=["CODE", "redacted", "URL", "zenodo", "2025"],
               authors=[Author("Redacted", "Url"), Author("Archive", "Data")])
    assert out == f"{CODE_REPO_URL}, {EMAIL}, {DATA_ARCHIVE_URL} and {DATA_ARCHIVE_DOI}"


# --- post-t0 years ------------------------------------------------------------------------

def test_post_t0_years():
    text = ("In 2021, 2019, the 2030s, 2025a; 1999; 2030.5; 12021; GSE2034; n = 2048; "
            "2048 × 2048 px; 2019/2020; 2019–2022; (2100).")
    assert body(text) == (f"In {YEAR}, 2019, the {YEAR}, {YEAR}; 1999; 2030.5; 12021; GSE2034; "
                          f"n = 2048; 2048 × 2048 px; 2019/{YEAR}; 2019–{YEAR}; (2100).")


def test_post_t0_number_filling_a_table_cell_is_a_value():
    text = "| MTG329 | 2049 | 46 |\n| x | In 2049 | 2049 cells |"
    assert body(text) == f"| MTG329 | 2049 | 46 |\n| x | In {YEAR} | 2049 cells |"


def test_years_untouched_without_t0():
    assert body("In 2021 and 2030.", year=None) == "In 2021 and 2030."


# --- result bookkeeping -------------------------------------------------------------------

def test_counts_and_determinism():
    doc = make_doc(Section("Data", "github.com/q/x and gitlab.com/q/y by Alice Quokkason in 2022; "
                                   "alice@quokka-lab.org.", "availability"))
    first, second = scrub(doc, ["Wombat"]), scrub(doc, ["Wombat"])
    assert first == second
    assert first.counts == {AUTHOR: 1, CODE_REPO_URL: 2, EMAIL: 1, YEAR: 1}
    assert list(first.counts) == sorted(first.counts)


# --- regressions: hosts, metadata lines, counts, acronyms, names, institutions ---------------------

@pytest.mark.parametrize("text, expected", [
    ("Run at wombatia.it and quokkalab.science.", f"Run at {URL} and {URL}."),
    ("Short link bit.ly/3QkLab, the quokka.app and bio.tools.", f"Short link {URL}, the {URL} and {URL}."),
    ("Sequenced at scilifelab.se with tools from bioinfo.unipd.it.", f"Sequenced at {URL} with tools from {URL}."),
    ("The data end.It was clear; see Fig.Se and E.coli.", "The data end.It was clear; see Fig.Se and E.coli."),
])
def test_lowercase_hosts_with_other_tlds(text, expected):
    assert body(text) == expected


def test_module_paths_are_not_urls():
    text = "Raw counts were loaded with scipy.io and skimage.io; tf.io.gfile and pandas.io.parquet in Python."
    assert body(text) == text
    assert body("Docs at scipy.io/docs") == f"Docs at {URL}"


def test_email_domain_mentioned_as_text_with_any_tld():
    assert body("the unimi.it cluster", emails=["alice@unimi.it"]) == f"the {DOMAIN} cluster"
    # .pt is no host TLD on its own (model.pt), so only the email-domain rule catches this one
    assert body("the quokka-lab.pt cluster", emails=["alice@quokka-lab.pt"]) == f"the {DOMAIN} cluster"


def test_email_followed_by_a_footnote_digit():
    assert body("Contact the lab at bob@wombat-uni.edu2 for samples.") == f"Contact the lab at {EMAIL}2 for samples."


@pytest.mark.parametrize("text", [
    "### Correspondence analysis",
    "Correspondence analysis (CA) of the codon usage table separated burrowing lineages.",
    "Accepted: reads with MAPQ >= 30 were retained.",
    "Received signals were filtered.",
])
def test_scientific_lines_are_not_metadata(text):
    assert body(text) == text


@pytest.mark.parametrize("text", [
    "Correspondence: Alice Quokkason", "Correspondence to Dr Quokkason", "Correspondence Alice (alice@x.org)",
    "Accepted: June 2, 2019", "Published online: 3 May 2019", "Editor: Jane Doe",
])
def test_metadata_lines_are_removed(text):
    assert body(text) == METADATA


def test_counts_after_t0_are_not_years():
    text = ("We amplified a 2048 bp fragment and found 2031 genes and 2087 SNPs in 2050 individuals; "
            "In 2021 cells were thawed; the 2040s climate; samples from 2030.")
    assert body(text) == ("We amplified a 2048 bp fragment and found 2031 genes and 2087 SNPs in 2050 individuals; "
                          f"In {YEAR} cells were thawed; the {YEAR} climate; samples from {YEAR}.")


def test_resource_and_science_acronyms_are_not_institutions():
    affiliations = ["National Center for Biotechnology Information, National Library of Medicine, Bethesda",
                    "European Molecular Biology Laboratory, European Bioinformatics Institute, Hinxton"]
    text = ("Reads from NCBI SRA and EMBL-EBI ENA; NIH 3T3 cells; the UGA stop codon and LSU rRNA; "
            "approved at the National Center for Biotechnology Information.")
    out = body(text, affiliations=affiliations, institutions=[],
               emails=["a@uga.edu", "b@lsu.edu", "c@ncbi.nlm.nih.gov"])
    assert out == ("Reads from NCBI SRA and EMBL-EBI ENA; NIH 3T3 cells; the UGA stop codon and LSU rRNA; "
                   f"approved at the {INSTITUTION}.")


def test_pdf_body_institutions_give_no_acronyms():
    # PDF institutions are harvested from the whole text; only affiliated ones yield acronyms
    doc = make_doc(Section("Methods", "Stocks came from the Bloomington Drosophila Stock Center (BDSC); "
                                      "BDSC #5905 and the CQHL core."),
                   affiliations=["Cold Quokka Harbor Laboratory, Wombat City"],
                   institutions=["Bloomington Drosophila Stock Center", "Cold Quokka Harbor Laboratory"],
                   source_type="pdf")
    text = scrub(doc).text
    assert f"the {INSTITUTION} (BDSC); BDSC #5905 and the {INSTITUTION} core." in text


def test_compound_surnames_are_cited_by_parts():
    out = body("García Márquez et al. (2017), Márquez et al. (2017), Garcia-Marquez et al. 2017 and Garcia (2016)",
               authors=[Author("García Márquez", "Juan")])
    assert out == f"{AUTHOR} et al. (2017), {AUTHOR} et al. (2017), {AUTHOR} et al. 2017 and {AUTHOR} (2016)"


def test_common_word_surnames_keep_dates_and_phrases():
    authors = [Author("May", "Ann"), Author("Summer", "Bo"), Author("Rice", "Cy"), Author("Brown", "Di")]
    text = ("Collected in May 2018, in Summer 2017 and Winter 2018; Rice and Arabidopsis genomes; "
            "May et al. (2015), (Rice 2012), Summer and Smith (2010) and Brown (2012).")
    assert body(text, authors=authors) == (
        "Collected in May 2018, in Summer 2017 and Winter 2018; Rice and Arabidopsis genomes; "
        f"{AUTHOR} et al. (2015), ({AUTHOR} 2012), {AUTHOR} and Smith (2010) and {AUTHOR} (2012).")


def test_short_upper_case_surnames_are_acronyms_unless_cited():
    authors = [Author("Ali", "Sara"), Author("Mao", "Jun"), Author("Lee", "Min")]
    text = "Mice with ALI had more MAO-A; the LEE and SPI-1 islands; LEE et al. disagree."
    assert body(text, authors=authors) == ("Mice with ALI had more MAO-A; the LEE and SPI-1 islands; "
                                           f"{AUTHOR} et al. disagree.")


def test_possessive_surnames_are_redacted():
    out = body("We reused Zorb's pipeline and Picard’s filters; Young's modulus.",
               authors=[Author("Zorb", "Dana"), Author("Picard", "Jean"), Author("Young", "Amy")])
    assert out == f"We reused {AUTHOR}'s pipeline and {AUTHOR}’s filters; Young's modulus."


def test_reworded_institution_names():
    out = body("The Wombatia University IRB and the Univ. of Wombatia; the Pasteur Institute; "
               "MPI for Evolutionary Anthropology staff.",
               affiliations=["University of Wombatia, Wombat City", "Institut Pasteur, Paris",
                             "Max Planck Institute for Evolutionary Anthropology, Leipzig"], institutions=[])
    assert out == f"The {INSTITUTION} IRB and the {INSTITUTION}; the {INSTITUTION}; {INSTITUTION} staff."
