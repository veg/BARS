"""Generate galaxy/bars_review/test-data/synthetic.pdf: a fictional paper for the BARS tests.

Usage: python tests/bars/make_synthetic_pdf.py   (needs pymupdf; output is byte-for-byte deterministic)

Every person, institution, journal, URL and identifier in it is invented. The layout mimics a
journal PDF: running header with the journal name, page numbers, superscript affiliation markers,
an Abstract, numbered-free section headings, a line-break hyphen, Data availability with a GitHub
URL, Acknowledgements, an inline "Competing interests:" declaration and References.
"""

from __future__ import annotations

from pathlib import Path

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

OUT = Path(__file__).resolve().parents[2] / "galaxy" / "bars_review" / "test-data" / "synthetic.pdf"
PAGE_W, PAGE_H = 595, 842
MARGIN = 64
HEADER = "Journal of Marsupial Genomics | Volume 7 | 2019"
TITLE = "Convergent evolution of burrowing genes in wombats revealed by comparative genomics"
FIXED_DATE = "D:20190614000000Z"


class Writer:
    """Minimal top-to-bottom text layout with word wrap, page breaks, header and page numbers."""

    def __init__(self, doc: pymupdf.Document) -> None:
        self.doc = doc
        self.page = None
        self.y = 0.0
        self.new_page()

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        number = self.doc.page_count
        self.page.insert_text((MARGIN, 40), HEADER, fontsize=8, fontname="helv")
        self.page.insert_text((PAGE_W / 2 - 3, PAGE_H - 36), str(number), fontsize=8, fontname="helv")
        self.y = 80.0

    def _room(self, height: float) -> None:
        if self.y + height > PAGE_H - 70:
            self.new_page()

    def paragraph(self, text: str, size: float = 10, font: str = "helv", gap: float = 6) -> None:
        """Wrap text to the column width; explicit newlines force a line break."""
        width = PAGE_W - 2 * MARGIN
        for chunk in text.split("\n"):
            line = ""
            for word in chunk.split():
                candidate = f"{line} {word}".strip()
                if pymupdf.get_text_length(candidate, fontname=font, fontsize=size) > width and line:
                    self._line(line, size, font)
                    line = word
                else:
                    line = candidate
            if line:
                self._line(line, size, font)
        self.y += gap

    def _line(self, text: str, size: float, font: str) -> None:
        self._room(size * 1.4)
        self.page.insert_text((MARGIN, self.y + size), text, fontsize=size, fontname=font)
        self.y += size * 1.4

    def runs(self, runs: list[tuple[str, bool]], size: float = 11, gap: float = 4) -> None:
        """One line made of (text, is_superscript) runs."""
        self._room(size * 1.4)
        x = MARGIN
        for text, sup in runs:
            run_size = size * 0.65 if sup else size
            offset = -size * 0.35 if sup else 0
            self.page.insert_text((x, self.y + size + offset), text, fontsize=run_size, fontname="helv")
            x += pymupdf.get_text_length(text, fontname="helv", fontsize=run_size)
        self.y += size * 1.4 + gap

    def heading(self, text: str) -> None:
        self._room(40)
        self.y += 6
        self.paragraph(text, size=12, font="hebo", gap=2)


def build_pdf(metadata_author: bool = True) -> bytes:
    doc = pymupdf.open()
    w = Writer(doc)
    w.paragraph(TITLE, size=16, font="hebo", gap=8)
    w.runs([("Alice Quokkason", False), ("1,*", True), (", Bertrand Numbat", False), ("1,2", True)])
    w.runs([("1", True), (" Department of Marsupial Genomics, University of Wombatia, Burrow City, Australia", False)],
           size=9, gap=0)
    w.runs([("2", True), (" Tasmanian Institute of Devil Studies, Hobart, Australia", False)], size=9, gap=0)
    w.paragraph("* Corresponding author: alice@quokka-lab.org", size=9, gap=0)
    w.paragraph("ORCID: https://orcid.org/0000-0002-1825-0097", size=9, gap=0)
    w.paragraph("Published: 14 June 2019", size=9, gap=10)

    w.heading("Abstract")
    w.paragraph(
        "Burrowing has evolved repeatedly in marsupials, yet the genetic basis of digging behaviour is "
        "poorly understood. We compared 42 marsupial genomes and tested 1,204 genes for episodic "
        "positive selection on burrowing lineages. Three genes (BRW1, DIG2 and CLAW3) showed "
        "convergent signatures of selection, and site-level models identified 25 codons under "
        "diversifying selection. Our results suggest that a small set of developmental genes "
        "underlies repeated transitions to a fossorial lifestyle.")
    w.heading("Introduction")
    w.paragraph(
        "Fossorial mammals share skeletal and behavioural adaptations for digging. Quokkason et al. "
        "(2015) described burrowing-associated expression changes in wombat forelimbs, and Numbat and "
        "colleagues later proposed that claw morphology evolves under strong selection. Whether the "
        "same genes are repeatedly targeted by selection across independent burrowing lineages "
        "remains an open question that comparative genomics can address.")
    w.paragraph(
        "Here we assemble a dataset of 42 genomes spanning four independent origins of burrowing and "
        "apply branch-site models of codon evolution to test for convergent episodic selection.")

    w.new_page()
    w.heading("Materials and methods")
    w.paragraph(
        "All sampling procedures were approved by the University of Wombatia ethics board (protocol "
        "UW-17-42). Coding sequences were aligned with a codon-aware aligner and a maximum-likelihood "
        "phylo-\ngenetic tree was inferred under a GTR+G model. Burrowing lineages were labelled as "
        "foreground branches.")
    w.paragraph(
        "Gene-wide episodic selection was tested with a branch-site random-effects model, and "
        "individual codons were tested with a mixed-effects model of episodic diversification. "
        "P values were corrected for multiple testing with the Holm procedure.")
    w.heading("Results")
    w.paragraph(
        "Of 1,204 genes tested, 31 showed evidence of episodic positive selection on at least one "
        "burrowing lineage (corrected p < 0.05). Three genes were selected on all four burrowing "
        "lineages (Table 1).")
    w.paragraph("Table 1. Genes under selection on burrowing lineages. BRW1: 12 sites; DIG2: 9 sites; "
                "CLAW3: 4 sites.", size=9)
    w.paragraph("Figure 1. Phylogeny of the 42 marsupials; burrowing lineages are shown in bold.", size=9)

    w.new_page()
    w.heading("Discussion")
    w.paragraph(
        "Convergent selection on BRW1, DIG2 and CLAW3 suggests a shared developmental route to "
        "fossoriality. In 2021 we resequenced two additional wombat populations and recovered the "
        "same selected sites, although these data were not part of the analyses reported here.")
    w.heading("Data availability")
    w.paragraph(
        "All code is available at https://github.com/quokkalab/marsupial-pipeline and the complete "
        "analysis history at https://usegalaxy.org/u/aquokka/h/marsupial-run. Raw reads are archived "
        "at Zenodo (https://doi.org/10.5281/zenodo.1234567).")
    w.heading("Acknowledgements")
    w.paragraph("We thank Dingo Wallaby and the University of Wombatia sequencing core for support.")
    w.paragraph("Competing interests: The authors declare no competing interests.")
    w.heading("References")
    w.paragraph("1. Quokkason A, Dingo W. Burrowing behaviour genes in marsupials. J Marsupial Genomics. "
                "2015;3:1-10.", size=9, gap=2)
    w.paragraph("2. Numbat B, Quokkason A. Claw morphology and digging performance. Outback Zool. "
                "2017;12:44-51.", size=9, gap=2)

    metadata = {"title": TITLE, "author": "Alice Quokkason, Bertrand Numbat" if metadata_author else "",
                "subject": "", "keywords": "", "creator": "make_synthetic_pdf.py", "producer": "PyMuPDF",
                "creationDate": FIXED_DATE, "modDate": FIXED_DATE, "trapped": ""}
    doc.set_metadata(metadata)
    data = doc.tobytes(garbage=4, deflate=True, no_new_id=True)
    doc.close()
    return data


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_pdf())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
