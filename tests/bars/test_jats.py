"""Tests for bars.jats on the synthetic fixture and on small JATS snippets for edge cases."""

from __future__ import annotations

import pytest

from bars.jats import classify_title, institution_candidates, parse_jats
from bars.model import KINDS, Document

NS = 'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:mml="http://www.w3.org/1998/Math/MathML"'


def article(body: str = "", back: str = "", meta: str = "", front: str = "", root_extra: str = "") -> bytes:
    """A minimal JATS article around the given fragments."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<article {NS} article-type="research-article">
  <front>{front}<article-meta><title-group><article-title>T</article-title></title-group>{meta}</article-meta></front>
  <body>{body}</body>
  <back>{back}</back>{root_extra}
</article>""".encode()


def by_title(doc: Document, title: str):
    matches = [s for s in doc.sections if s.title == title]
    assert matches, f"no section {title!r} in {[s.title for s in doc.sections]}"
    return matches[0]


@pytest.fixture
def doc(test_data) -> Document:
    return parse_jats((test_data / "synthetic_jats.xml").read_bytes(), "synthetic_jats.xml")


# --- synthetic fixture: metadata ----------------------------------------------------------------

def test_fixture_metadata(doc):
    assert doc.source_id == "synthetic_jats.xml" and doc.source_type == "jats"
    assert doc.title.startswith("Convergent evolution of burrowing genes in Vombatus and other marsupials")
    assert [(a.surname, a.given, a.heuristic) for a in doc.authors] == [
        ("Quokkason", "Alice", False), ("Numbat", "Bertrand", False)]  # the editor is not an author
    assert doc.journal == "Journal of Marsupial Genomics"
    assert doc.publisher == "Outback Science Press"
    assert doc.year == 2019  # earliest pub-date; the 2020 pmc-release date is ignored
    assert (doc.doi, doc.pmid, doc.pmcid) == ("10.5555/jmg.2019.0042", "99999042", "PMC9999042")
    assert doc.emails == ["alice@quokka-lab.org"]
    assert doc.orcids == ["0000-0002-1825-0097"]
    assert doc.front_matter == ""


def test_fixture_affiliations_and_institutions(doc):
    assert "Department of Marsupial Genomics, University of Wombatia, Burrow City, Australia" in doc.affiliations
    assert "Tasmanian Institute of Devil Studies, Hobart, Australia" in doc.affiliations
    assert "Bilby Bioinformatics Centre, Alice Springs, Australia" in doc.affiliations  # present-address note
    assert not any(a.startswith("1") for a in doc.affiliations)  # <label> dropped
    for name in ("Department of Marsupial Genomics", "University of Wombatia", "Tasmanian Institute of Devil Studies"):
        assert name in doc.institutions


# --- synthetic fixture: sections ----------------------------------------------------------------

def test_fixture_section_order_and_kinds(doc):
    assert [(s.kind, s.title, s.level) for s in doc.sections] == [
        ("abstract", "Abstract", 2),
        ("abstract", "Author summary", 2),
        ("availability", "Data Availability", 2),
        ("body", "Introduction", 2),
        ("body", "Materials and methods", 2),
        ("body", "Sampling and ethics", 3),
        ("body", "Selection analyses", 3),
        ("body", "Model comparison", 4),
        ("body", "Results", 2),
        ("body", "Discussion", 2),
        ("ack", "Acknowledgments", 2),
        ("contrib", "Author contributions", 2),
        ("funding", "Funding", 2),
        ("coi", "Competing interests", 2),
        ("refs", "References", 2),
    ]
    assert all(s.kind in KINDS for s in doc.sections)
    assert doc.notes == ["skipped abstract (abstract-type=teaser)"]


def test_front_matter_identifiers_are_not_rendered(doc):
    text = "\n".join(s.title + "\n" + s.text for s in doc.sections)
    for leaked in ("Corresponding author", "Cedric", "Platypodes", "Monotreme", "Bilby Bioinformatics",
                   "© 2019", "Creative Commons", "10.5555/jmg", "jmg.0042", "Teaser text", "credit.niso.org",
                   "Outback Science Press", "PMC9999042"):
        assert leaked not in text, leaked


def test_custom_meta_data_availability_keeps_link_targets(doc):
    text = by_title(doc, "Data Availability").text
    assert "github.com/quokkalab/marsupial-pipeline" in text
    assert "our Galaxy history (https://usegalaxy.org/u/aquokka/h/marsupial-run)" in text
    assert "(https://workflowhub.eu/workflows/4242)" in text
    assert "https://workflowhub.eu/workflows/4242 (https://" not in text  # href == text: not repeated
    assert "10.5281/zenodo.1234567" in text
    assert "PRJNA999042" in text
    assert "pmc-prop" not in "".join(s.title for s in doc.sections)


def test_body_text_inline_markup_and_tails(doc):
    intro = by_title(doc, "Introduction").text
    assert "Quokkason et al. (2015) [1] described burrowing-associated" in intro
    assert "Numbat and colleagues [2] later proposed" in intro
    assert "under strong selection[3]." in intro  # superscript citation markers
    assert "(dN/dS)" in intro
    assert "\n\n" in intro  # two paragraphs
    methods = by_title(doc, "Sampling and ethics").text
    assert "University of Wombatia ethics board" in methods
    assert "alice@quokka-lab.org" in methods
    assert "In 2021 we resequenced" in by_title(doc, "Results").text


def test_ext_link_with_different_text_keeps_href(doc):
    assert "our lab website (https://www.quokka-lab.org/protocols)." in by_title(doc, "Selection analyses").text


def test_formulas_and_lists(doc):
    text = by_title(doc, "Selection analyses").text
    assert "ω = (d_N)/(d_S)" in text
    assert r"$$ L(\theta) = \prod_{s} \sum_{k} p_k \, P(D_s \mid \omega_k) $$ (1)" in text
    assert "documentclass" not in text
    assert "- BUSTED for gene-wide episodic selection;\n- MEME" in text


def test_table_rendering(doc):
    text = by_title(doc, "Results").text
    assert "**Table 1.** Genes under selection on burrowing lineages. Counts of codons" in text
    assert ("| Gene | Selected sites / MEME | Selected sites / FEL |\n"
            "| --- | --- | --- |\n"
            "| BRW1 | 12 | 8 |\n"
            "| DIG2 | 5 | 3 |\n"
            "| DIG2 | 4 | 2 |\n"
            "| CLAW3 \\| variant | 0 | 1 |") in text
    assert "Sites with p ≤ 0.1." in text


def test_figure_and_supplement(doc):
    assert ("**Figure 1.** Phylogeny of the 42 marsupials. Burrowing lineages are shown in bold; "
            "branches under selection in red.") in by_title(doc, "Results").text
    assert ("**S1 Table.** Per-codon test statistics for all 1,204 genes. (supplementary file not provided)"
            in by_title(doc, "Discussion").text)


def test_back_matter(doc):
    assert "Wombat Research Council" in by_title(doc, "Funding").text
    assert "no competing interests" in by_title(doc, "Competing interests").text
    refs = by_title(doc, "References").text
    assert refs.startswith("- 1 Quokkason A, Wallaby D. Burrowing behaviour genes")
    assert refs.count("\n- ") == 2


# --- edge cases ------------------------------------------------------------------------------------

def test_whitespace_tails_and_entities():
    xml = article('<sec><title>  Odd\n  <bold>title</bold> </title><p>Alpha   <italic>beta</italic>\n'
                  '   gamma<xref ref-type="bibr">1</xref>, delta&nbsp;epsilon&#x2264;5 x<sup>2</sup> '
                  'CO<sub>2</sub>.<break/>Next​ line.</p></sec>')
    s = parse_jats(xml, "x").sections[0]
    assert s.title == "Odd title"
    assert s.text == "Alpha beta gamma1, delta epsilon≤5 x^2 CO2. Next line."


def test_entities_are_never_expanded():
    xml = (b'<!DOCTYPE article [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;">'
           b'<!ENTITY ext SYSTEM "file:///etc/passwd">]>'
           b'<article><front><article-meta><title-group><article-title>T</article-title></title-group>'
           b'</article-meta></front><body><sec><title>S</title><p>x&b;y&ext;z</p></sec></body></article>')
    assert parse_jats(xml, "x").sections[0].text == "xyz"


@pytest.mark.parametrize("data", [b"", b"not xml", b"<article><unclosed></article>", b"<html><body/></html>"])
def test_invalid_input_raises_value_error(data):
    with pytest.raises(ValueError):
        parse_jats(data, "bad")


def test_article_inside_wrapper():
    xml = b"<pmc-articleset>" + article("<sec><title>Intro</title><p>Text.</p></sec>").split(b"?>", 1)[1] + b"</pmc-articleset>"
    assert [s.title for s in parse_jats(xml, "x").sections] == ["Intro"]


def test_namespaced_hrefs_and_uri():
    xml = article('<sec><title>Code</title><p>See <ext-link ext-link-type="uri" xlink:href="https://gitlab.com/a/b">'
                  'the repository</ext-link>, <uri xlink:href="https://zenodo.org/record/1">https://zenodo.org/record/1</uri>'
                  ' and <ext-link xlink:href="https://github.com/o/r/"> github.com/o/r </ext-link> or mail '
                  '<email xlink:href="mailto:x@y.org">x@y.org</email>.</p></sec>')
    text = parse_jats(xml, "x").sections[0].text
    assert text == ("See the repository (https://gitlab.com/a/b), https://zenodo.org/record/1 and "
                    "https://github.com/o/r/ or mail x@y.org.")  # a bare-host link text gets its scheme back


def test_bare_host_link_text_gets_its_scheme_back():
    xml = article('<sec><title>S</title><p>Run at <ext-link xlink:href="https://wombatia.it">wombatia.it</ext-link>, '
                  '<ext-link xlink:href="https://www.quokkalab.science/">quokkalab.science</ext-link>, '
                  '<ext-link xlink:href="http://bit.ly/3QkLab">bit.ly/3QkLab</ext-link> and '
                  '<ext-link xlink:href="https://doi.org/10.1/x">10.1/x</ext-link>.</p></sec>')
    assert parse_jats(xml, "x").sections[0].text == (
        "Run at https://wombatia.it, https://www.quokkalab.science/, http://bit.ly/3QkLab and 10.1/x.")


def test_local_file_hrefs_are_not_appended():
    xml = article('<sec><title>S</title><p>See <inline-supplementary-material xlink:href="pone.0275623.s001.xlsx">'
                  'S1 Table</inline-supplementary-material> and <ext-link xlink:href="#sec2">Methods</ext-link>.</p>'
                  '</sec>').replace(b"<article-title>T</article-title>", b"<article-title>A<break/>B</article-title>")
    doc = parse_jats(xml, "x")
    assert doc.sections[0].text == "See S1 Table and Methods."
    assert doc.title == "A B"


def test_abstract_types():
    meta = ('<abstract><sec><title>Background</title><p>B.</p></sec><sec><title>Results</title><p>R.</p></sec></abstract>'
            '<abstract abstract-type="graphical"><p>G.</p></abstract>'
            '<abstract abstract-type="executive-summary"><title>eLife digest</title><p>D.</p></abstract>'
            '<trans-abstract><p>Résumé.</p></trans-abstract>')
    doc = parse_jats(article(meta=meta), "x")
    assert [(s.kind, s.title, s.level, s.text) for s in doc.sections] == [
        ("abstract", "Abstract", 2, ""), ("abstract", "Background", 3, "B."), ("abstract", "Results", 3, "R."),
        ("abstract", "eLife digest", 2, "D.")]
    assert doc.notes == ["skipped abstract (abstract-type=graphical)"]


def test_plos_front_notes_and_custom_meta_are_deduplicated():
    da = ('Code at <ext-link ext-link-type="uri" xlink:href="https://github.com/veg/RASCL">'
          'https://github.com/veg/RASCL</ext-link>.')
    front = f'<notes><title>Data Availability</title><p>{da}</p></notes>'
    meta = (f'<custom-meta-group><custom-meta><meta-name>pmc-status-live</meta-name><meta-value>yes</meta-value>'
            f'</custom-meta><custom-meta id="data-availability"><meta-name>Data Availability</meta-name>'
            f'<meta-value>{da}</meta-value></custom-meta></custom-meta-group>')
    doc = parse_jats(article(meta=meta, front=""), "x")
    assert [(s.kind, s.text) for s in doc.sections] == [("availability", "Code at https://github.com/veg/RASCL.")]
    xml = article(meta=meta).replace(b"</article-meta></front>", b"</article-meta>" + front.encode() + b"</front>")
    assert [s.kind for s in parse_jats(xml, "x").sections] == ["availability"]


def test_author_notes_and_funding_group():
    meta = ('<author-notes><corresp id="c1">* E-mail: <email>a@b.edu</email> (AQ)</corresp>'
            '<fn fn-type="conflict"><p>The authors have declared that no competing interests exist.</p></fn>'
            '<fn fn-type="con"><p>Conceived the experiments: AQ.</p></fn>'
            '<fn fn-type="equal"><p>These authors contributed equally.</p></fn>'
            '<fn><p><bold>Data availability:</bold> All data are on figshare.</p></fn>'
            '<fn fn-type="other"><p>Deceased.</p></fn></author-notes>'
            '<funding-group><award-group><funding-source><institution-wrap><institution>Wombat Trust</institution>'
            '</institution-wrap></funding-source></award-group><funding-statement>Funded by the Wombat Trust.'
            '</funding-statement></funding-group>')
    doc = parse_jats(article(meta=meta), "x")
    assert [(s.kind, s.title) for s in doc.sections] == [
        ("coi", "Competing interests"), ("contrib", "Author contributions"), ("contrib", "Author contributions"),
        ("availability", "Data availability"), ("funding", "Funding")]
    assert doc.sections[-1].text == "Funded by the Wombat Trust."
    assert doc.emails == ["a@b.edu"]
    assert "Wombat Trust" not in doc.institutions  # funders are not affiliations


def test_elife_style_back_matter():
    back = ('<sec sec-type="additional-information"><title>Additional information</title>'
            '<fn-group content-type="competing-interest"><title>Competing interests</title>'
            '<fn fn-type="COI-statement"><p>No competing interests declared.</p></fn></fn-group>'
            '<fn-group content-type="author-contribution"><title>Author contributions</title>'
            '<fn fn-type="con"><p>AQ, Conceptualization.</p></fn></fn-group>'
            '<sec sec-type="ethics-statement"><title>Ethics</title><p>Approved.</p></sec></sec>'
            '<sec sec-type="data-availability"><title>Data availability</title><p>Sequencing data: PRJNA1.</p></sec>'
            '<app-group><app><title>Appendix 1</title><p>Extra.</p></app></app-group>'
            '<glossary><title>Abbreviations</title><def-list><def-item><term>BGM</term><def><p>Bayesian graphical model'
            '</p></def></def-item></def-list></glossary>')
    doc = parse_jats(article(back=back), "x")
    assert [(s.kind, s.title, s.level) for s in doc.sections] == [
        ("back", "Additional information", 2),  # heading only: its content is in the subsections
        ("coi", "Competing interests", 3), ("contrib", "Author contributions", 3), ("back", "Ethics", 3),
        ("availability", "Data availability", 2), ("back", "Appendix 1", 2), ("back", "Abbreviations", 2)]
    assert doc.sections[-1].text == "- BGM: Bayesian graphical model"


def test_back_fn_group_classified_by_lead_in():
    back = ('<fn-group><fn><p><bold>Funding.</bold> NSF grant 1.</p></fn>'
            '<fn><p>Code availability: see the supplement.</p></fn>'
            '<fn><p>Publisher\'s note: neutral.</p></fn>'
            '<fn><p>Reagents were a gift.</p></fn>'
            '<fn fn-type="present-address"><p>Present address: Koala College of Science, Perth.</p></fn></fn-group>')
    doc = parse_jats(article(back=back), "x")
    assert [(s.kind, s.title) for s in doc.sections] == [
        ("funding", "Funding"), ("availability", "Code availability"), ("back", "Notes")]
    assert doc.sections[-1].text == "Reagents were a gift."  # the publisher's note names the publisher
    assert "Koala College of Science, Perth." in doc.affiliations
    assert "Koala College of Science" in doc.institutions


def test_europe_pmc_generated_back_secs_in_body():
    # Europe PMC's reprocessed XML moves back matter into <body> and adds its own secs.
    body = ('<sec><title>Results</title><p>We found 3 loci.</p>'
            '<supplementary-material id="MOESM1"><media xlink:href="13059_MOESM1_ESM.pdf">'
            '<caption><p>Additional file 1. Supplementary Figures S1-S4.</p></caption></media>'
            '</supplementary-material></sec>'
            '<sec sec-type="fn-group"><title>Footnotes</title><fn-group><fn id="fn1">'
            '<p><bold>Publisher’s Note</bold></p><p>Wombat Nature remains neutral with regard to '
            'jurisdictional claims.</p></fn></fn-group></sec>'
            '<sec sec-type="contrib-info"><title>Contributor Information</title>'
            '<p>Alice Quokkason, Email: alice@quokka-lab.org</p>'
            '<p>Bertrand Numbat, University of Wombatia, Australia.</p></sec>'
            '<sec sec-type="associated-data"><title>Associated Data</title>'
            '<sec sec-type="supplementary-materials"><title>Supplementary Materials</title>'
            '<supplementary-material id="db_ds_1"><media xlink:href="x.pdf"/></supplementary-material>'
            '</sec></sec>')
    doc = parse_jats(article(body=body), "x")
    assert [(s.kind, s.title) for s in doc.sections] == [
        ("body", "Results"), ("contrib", "Contributor Information")]
    assert ("**Supplementary material.** Additional file 1. Supplementary Figures S1-S4. "
            "(supplementary file not provided)") in doc.sections[0].text
    assert "alice@quokka-lab.org" in doc.emails
    assert classify_title("Contributor Information") == "contrib"


def test_body_section_classification_and_untitled_secs():
    body = ('<p>Loose opening paragraph.</p>'
            '<sec><label>1.</label><title>Methods</title><p>M.</p><sec><p>Untitled sub.</p></sec>'
            '<sec sec-type="data-availability"><title>Software</title><p>On GitHub.</p></sec></sec>'
            '<sec><title>Acknowledgments</title><p>Thanks.</p></sec>')
    doc = parse_jats(article(body=body), "x")
    assert [(s.kind, s.title, s.level, s.text) for s in doc.sections] == [
        ("body", "Main text", 2, "Loose opening paragraph."),
        ("body", "1. Methods", 2, "M.\n\nUntitled sub."),
        ("availability", "Software", 3, "On GitHub."),
        ("ack", "Acknowledgments", 2, "Thanks.")]


def test_table_without_thead_colspan_and_truncation():
    rows = "".join(f"<tr><td>g{i}</td><td>{i}</td></tr>" for i in range(205))
    body = ('<sec><title>R</title><table-wrap><label>Table S1.</label><caption><p>Big.</p></caption><table>'
            '<tr><th>Gene</th><th>N</th></tr><tr><td colspan="2">Group A</td></tr>' + rows + '</table></table-wrap>'
            '<table-wrap><caption><title>Image only</title></caption><graphic xlink:href="t.png"/></table-wrap></sec>')
    doc = parse_jats(article(body=body), "x")
    text = doc.sections[0].text
    assert text.startswith("**Table S1.** Big.\n\n| Gene | N |\n| --- | --- |\n| Group A |  |\n| g0 | 0 |")
    assert "| g198 | 198 |" in text and "| g199 |" not in text
    assert "(Table truncated: first 200 of 206 rows shown.)" in text
    assert doc.notes == ["Table S1: truncated to 200 of 206 rows"]
    assert "**Table 2.** Image only.\n\n(table content not available as text)" in text


def test_names_collab_and_orcid_links():
    meta = ('<contrib-group><contrib contrib-type="author"><name-alternatives><name><surname>Ng</surname>'
            '<given-names>Wei</given-names></name><string-name>吴伟</string-name></name-alternatives></contrib>'
            '<contrib><string-name>Quokkason, Alice</string-name></contrib>'
            '<contrib contrib-type="author"><string-name>Bertrand van Numbat</string-name>'
            '<ext-link xlink:href="https://orcid.org/0000-0001-2345-678X">ORCID</ext-link></contrib>'
            '<contrib contrib-type="author"><collab>Wombat Genome Consortium<contrib-group><contrib contrib-type="author">'
            '<name><surname>Bilby</surname><given-names>C</given-names></name></contrib></contrib-group></collab></contrib>'
            '<on-behalf-of>on behalf of the Marsupial Network</on-behalf-of></contrib-group>')
    doc = parse_jats(article(meta=meta), "x")
    assert [(a.surname, a.given) for a in doc.authors] == [
        ("Ng", "Wei"), ("吴伟", ""), ("Quokkason", "Alice"), ("Numbat", "Bertrand van"), ("Wombat Genome Consortium", ""),
        ("Bilby", "C"), ("Marsupial Network", "")]
    assert doc.orcids == ["0000-0001-2345-678X"]


def test_every_name_alternative_is_an_author():
    meta = ('<contrib-group><contrib contrib-type="author"><name-alternatives>'
            '<name name-style="eastern" xml:lang="zh"><surname>张</surname><given-names>伟</given-names></name>'
            '<name name-style="western"><surname>Zhangwei</surname><given-names>Liu</given-names></name>'
            '</name-alternatives></contrib><contrib contrib-type="author"><name-alternatives>'
            '<string-name xml:lang="ja">山田 太郎</string-name><name><surname>Yamada</surname>'
            '<given-names>Taro</given-names></name></name-alternatives></contrib></contrib-group>')
    doc = parse_jats(article(meta=meta), "x")
    assert [(a.surname, a.given) for a in doc.authors] == [
        ("张", "伟"), ("Zhangwei", "Liu"), ("太郎", "山田"), ("Yamada", "Taro")]


def test_sub_articles_and_floats_group():
    extra = ('<floats-group><fig><label>Figure 2</label><caption><p>Floating.</p></caption></fig></floats-group>'
             '<sub-article article-type="decision-letter"><front-stub><title-group><article-title>Decision letter'
             '</article-title></title-group></front-stub><body><p>Reviewer text; contact ed@journal.org.</p></body>'
             '</sub-article>')
    doc = parse_jats(article(body="<sec><title>A</title><p>a</p></sec>", root_extra=extra), "x")
    assert [(s.title, s.text) for s in doc.sections] == [("A", "a"), ("Figures and tables", "**Figure 2.** Floating.")]
    assert "ed@journal.org" in doc.emails  # harvested for scrubbing, never rendered


def test_mathml_only_formula_and_nested_list():
    body = ('<sec><title>M</title><disp-formula><mml:math><mml:msup><mml:mi>x</mml:mi><mml:mn>2</mml:mn></mml:msup>'
            '<mml:mo>+</mml:mo><mml:msqrt><mml:mi>y</mml:mi></mml:msqrt></mml:math><label>(2)</label></disp-formula>'
            '<list list-type="order"><list-item><p>One</p><list list-type="bullet"><list-item><p>a</p></list-item>'
            '</list></list-item><list-item><label>(ii)</label><p>Two</p></list-item></list></sec>')
    text = parse_jats(article(body=body), "x").sections[0].text
    assert text == "x^2 + sqrt(y) (2)\n\n1. One\n   - a\n- (ii) Two"


@pytest.mark.parametrize("math, expected", [
    # Springer/BMC fence whole expressions without <mrow>: the brackets carry the signs (RagTag's gap size)
    ("<mi>g</mi><mo>=</mo><mi>a</mi><mo>-</mo><mfenced><mi>b</mi><mo>+</mo><mi>c</mi></mfenced>", "g = a-(b + c)"),
    ("<mi>f</mi><mfenced><mi>x</mi><mi>y</mi></mfenced>", "f(x,y)"),
    ('<mfenced open="[" close="]" separators=";"><mi>a</mi><mi>b</mi></mfenced>', "[a;b]"),
    ("<mi>S</mi><mo>=</mo><munderover><mo>\u2211</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi>"
     "</munderover><msub><mi>x</mi><mi>i</mi></msub>", "S = \u2211_(i = 1)^n x_i"),
    ("<munder><mo>lim</mo><mrow><mi>n</mi><mo>\u2192</mo><mi>\u221e</mi></mrow></munder><msub><mi>a</mi><mi>n</mi>"
     "</msub>", "lim_(n\u2192\u221e) a_n"),
    ('<mi>Q</mi><mo>=</mo><mfenced open="[" close="]"><mtable><mtr><mtd><mn>1</mn></mtd><mtd><mn>2</mn></mtd></mtr>'
     "<mtr><mtd><mn>3</mn></mtd><mtd><mn>4</mn></mtd></mtr></mtable></mfenced>", "Q = [1, 2; 3, 4]"),
    ("<mover><mi>\u03b8</mi><mo>^</mo></mover><mo>=</mo><mn>0.5</mn>", "hat(\u03b8) = 0.5"),
    ("<mover><mi>x</mi><mi>y</mi></mover>", "x^y"),
    ("<mmultiscripts><mi>C</mi><mn>14</mn><none/><mprescripts/><none/><mn>6</mn></mmultiscripts>", "^6C_(14)"),
])
def test_mathml_structures_keep_their_meaning(math, expected):
    body = f"<sec><title>M</title><disp-formula><math>{math}</math></disp-formula></sec>"
    assert parse_jats(article(body=body), "x").sections[0].text == expected


def test_paragraph_with_embedded_blocks():
    body = ('<sec><title>R</title><p>Before <fig><label>Fig. 3</label><caption><title>Cap</title></caption></fig>'
            'after.</p><boxed-text><caption><title>Box 1</title></caption><p>Boxed.</p></boxed-text>'
            '<disp-quote><p>Quoted.</p></disp-quote><preformat>a  b\n c</preformat></sec>')
    text = parse_jats(article(body=body), "x").sections[0].text
    assert text.split("\n\n") == ["Before", "**Figure 3.** Cap.", "after.", "**Box 1**", "Boxed.", "> Quoted.",
                                  "```\na  b\n c\n```"]


def test_no_body_is_noted():
    doc = parse_jats(article().replace(b"<body></body>", b""), "x")
    assert doc.sections == [] and doc.notes == ["no <body> in JATS: abstract/metadata-only record"]


@pytest.mark.parametrize("title, kind", [
    ("Data Availability", "availability"), ("Data Availability Statement", "availability"),
    ("Availability and implementation", "availability"), ("Code availability", "availability"),
    ("Software availability", "availability"), ("Availability of data and materials", "availability"),
    ("Data and code availability", "availability"), ("Accession numbers", "availability"),
    ("Data accessibility", "availability"), ("Funding and data availability", "availability"),
    ("Acknowledgments", "ack"), ("ACKNOWLEDGEMENTS", "ack"), ("5. Acknowledgement", "ack"),
    ("Funding", "funding"), ("Funding Statement", "funding"), ("Financial Disclosure", "funding"),
    ("financial-disclosure", "funding"), ("Sources of funding", "funding"),
    ("Author contributions", "contrib"), ("Authors' contributions", "contrib"), ("Author’s contributions", "contrib"),
    ("CRediT authorship contribution statement", "contrib"), ("Contributors", "contrib"),
    ("Competing interests", "coi"), ("Conflict of interest", "coi"), ("Conflicts of Interest Statement", "coi"),
    ("Declaration of competing interest", "coi"), ("Disclosures", "coi"), ("COI-statement", "coi"),
    ("Acknowledgments and funding", "ack"), ("Competing interests & funding", "coi"),
    ("References", "refs"), ("Literature Cited", "refs"), ("Bibliography", "refs"),
    ("Introduction", None), ("Materials and methods", None), ("Results", None), ("Nutrient availability", None),
    ("Data analysis", None), ("Funding patterns in NIH grants", None), ("Ethics statement", None),
    ("Declarations", None), ("Acknowledgments and ethics", None), ("", None),
])
def test_classify_title(title, kind):
    assert classify_title(title) == kind


def test_institution_candidates():
    text = ("We thank Dingo Wallaby and the University of Wombatia sequencing core, the Department of Ecology "
            "and Evolutionary Biology, and the Broad Institute. Results from the Monotreme Research Station.")
    assert institution_candidates(text) == [
        "University of Wombatia", "Department of Ecology and Evolutionary Biology", "Department of Ecology",
        "Broad Institute"]
