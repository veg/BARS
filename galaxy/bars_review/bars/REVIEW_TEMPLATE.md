# Scientific Peer Review Template & Diagnostic Guide

This guide provides a standardized, fast-turnaround peer review protocol, 1–10 quantitative scoring rubric, and Markdown template for quantitative biology, evolutionary genomics, statistical modeling, bioinformatics, and epidemiology manuscripts.

The goal is to produce **human-scale, direct, technically rigorous reviews** that avoid filler while auditing methodology, mathematical and statistical validity, biological plausibility, and reproducibility.

---

## PART 0: DE-IDENTIFICATION & $t_0$ ISOLATION PROTOCOL (ANTI-PRESTIGE-HALO MANDATE)

Visible author names, institutional affiliations, and elite journal labels (*Nature*, *Science*, *Cell*, *PNAS*, *Nature Medicine*) can induce a prestige halo that softens critiques of methodologically flawed papers, while megajournal or preprint labels can induce a negative penalty (see [`CALIBRATION.md`](CALIBRATION.md) for empirical calibration tables and operating characteristics).

To eliminate prestige and venue bias while ensuring objective evaluation strictly on technical merits:
1. **Pre-Audit De-Identification:** Strip the manuscript Title, Author Names, Affiliations, Correspondence headers, Journal Name, Publication Year, DOI, and PMCID/PMID before running the 7-Pillar Audit (`[REDACTED FOR BLINDED REVIEW]`).
2. **$t_0$ Information Isolation:** Evaluate the manuscript strictly on its internal mathematical, statistical, methodological, biological, and table/data merits as published at $t_0$. Do not query external commentary, PubPeer threads, retraction notices, or subsequent citing papers during the audit.
3. **Post-Audit Header Restoration:** Re-attach the bibliographic metadata (`Title`, `Authors`, `Journal`, `DOI`/`PMCID`) only after the sub-scores, overall score, and 3-section critique have been locked.

---

## PART 1: THE 7-PILLAR RAPID AUDIT CHECKLIST

Before writing, audit the manuscript against these seven diagnostic pillars:

1. **Scope, Framing & Causal Claims:** Do the title and abstract claim causal drivers, mechanistic proof, or broad generality when the analysis only performs descriptive clustering, observational correlation, or a narrow case study?
2. **Data Provenance, Sampling Bias & Leakage:** Are there ascertainment biases, temporal/spatial skew, unacknowledged filtering rules, contamination/sequencing artifacts, lookahead bias, or post-hoc circularity (selecting hypotheses or thresholds after inspecting the data)?
3. **Model Specification & Internal Consistency:** Do the mathematical, statistical, or evolutionary models match the data-generating process? Are core assumptions (conditional independence, stationarity, panmixia, parameter identifiability, prior vs. likelihood separation) violated or mutually contradictory across sections?
4. **Statistical Support, Power & Uncertainty Propagation:** Are $p$-values, Bayes factors, confidence/credible intervals, or model selection criteria (AIC/BIC) properly calibrated? Is uncertainty propagated across all stages of multi-step pipelines? Do the authors conflate failing to reject a null hypothesis (due to low power or small sample size) with evidence for the null?
5. **Baselines, Controls & Symmetric Hypothesis Testing:** Are competing models or hypotheses evaluated under symmetric constraints, matched sample sizes, and realistic null simulations? Do Tables and Supplements actually support the model chosen in the main text?
6. **Biological & Physical Plausibility:** Do inferred rates, effect sizes, and mechanistic narratives respect known biophysical, molecular, evolutionary, or epidemiological constraints?
7. **Reproducibility, Data & Code Completeness & Technical Sanity Checks:** Are raw data and executable code available? Audit equations, check that numbers in the main text match tables and supplementary text, and scan for broken figure/table cross-references, mislabeled axes, and truncated or inaccurate citations.
   * **Ethical / Consent-Mandated Controlled-Access Repositories Receive Full Credit:** Human genomic/clinical cohorts deposited in recognized controlled-access repositories (`dbGaP`, `EGA`, `SFARI Base`, `All of Us Researcher Workbench`, `UK Biobank`, `AnVIL`, `GISAID`) or governed by Indigenous data sovereignty / CARE / MTA frameworks receive **full Data Availability credit** when accession numbers or formal access procedures are documented. Never penalize a paper for ethical/legal privacy protections on human participants.
   * **Published Versioned Pipelines vs. Novel Unreleased Software:** Distinguish novel computational algorithms/software (which require open source code) from standard genomic/bioinformatic analyses that chain published, versioned community tools (`verkko`, `hifiasm`, `minimap2`, `HyPhy`, `GATK`, `Seurat`), which should not be penalized as "irreproducible" when tool versions and parameters are documented.

---

## PART 2: PROSE & STYLE RULES (ANTI-LLM TROPE MANDATE)

* **Direct and proportional:** Keep total length proportional to the manuscript's substance. State strengths and flaws plainly without theatrical adjectives (*"groundbreaking," "tour de force," "pivotal," "crucial," "compelling"*).
* **No LLM filler or syntactic cliches:** Avoid *"delve into," "tapestry," "landscape," "it is worth noting," "importantly," "not only... but also," "serves as a testament," "crucially," "landmark,"* and formulaic compliment sandwiches.
* **Concrete and verifiable:** Anchor every critique to specific sections, equations, tables, figures, or parameter values in the manuscript. Verify any absence claim ("not reported") against the full text, figure legends, and Data/Code Availability sections before stating it.

---

## PART 3: THE 1–10 QUANTITATIVE SCORING RUBRIC (STRUCTURED QUANTITATIVE SCORECARD)

Every reviewed manuscript is evaluated on four diagnostic sub-scores mapped to the 7 Pillars, reporting both the **Total Composite Score (`/ 10.0`)** and the **Scientific Core Score (`/ 8.0` = `Rigor + Claims + Biology`, excluding `Reproducibility`)** so that historical papers (1990s/2000s) and controlled-access resource papers are never confounded by modern open-code norms:

1. **Methodological & Statistical Rigor (0.0 – 3.5 pts | Pillars 3, 4, 5):** Model specification, technical validation of the primary deliverable, internal consistency, calibration of $p$-values/Bayes factors/AIC, uncertainty propagation across multi-step pipelines, and symmetric baseline/null controls.
   * **Primary Deliverable vs. Exploratory Add-On Weighting (Resource / Assembly / Benchmark Papers):** For foundational resource, genome assembly (e.g., T2T, pangenome), or benchmark manuscripts, `Methodological Rigor (/ 3.5)` must be anchored on the technical validation of the primary deliverable (e.g., assembly QV, switch/hamming error, orthogonal platform concordance, BAC/optical validation). Secondary exploratory vignettes (e.g., illustrative PheWAS or pilot organoid assays) must be critiqued under `Claims vs. Evidence (/ 2.5)` proportional to their centrality to the headline claim, and must not cap the overall score of a technically rigorous primary resource below `7.0`.
2. **Claim-to-Evidence Alignment (0.0 – 2.5 pts | Pillars 1, 5):** Correspondence between title/abstract/discussion claims and what was formally tested; proportion of core claims that are *Well Supported* vs. *Fragile/Overstated/Methodologically Flawed*.
3. **Data Integrity & Biological Plausibility (0.0 – 2.0 pts | Pillars 2, 6):** Data provenance, handling of ascertainment/sampling skew, absence of leakage or post-hoc circularity, contamination controls, and adherence to biophysical, molecular, and epidemiological constraints.
4. **Reproducibility & Data Integrity (0.0 – 2.0 pts | Pillar 7):** Availability of raw data and executable code, mathematical accuracy of equations, numerical consistency between main text and tables/supplements, and clean figures/references.
   * **Ethical / Consent-Mandated Controlled-Access Repositories Receive Full Credit:** Human genomic/clinical cohorts deposited in recognized controlled-access repositories (`dbGaP`, `EGA`, `SFARI Base`, `All of Us Researcher Workbench`, `UK Biobank`, `AnVIL`, `GISAID`) or governed by Indigenous data sovereignty / CARE / MTA frameworks receive **full Data Availability credit** when accession numbers or formal access procedures are documented. Never penalize a paper for ethical/legal privacy protections on human participants.

### Composite Score Metrics
* **Total Composite Score (`/ 10.0`):** `Rigor (/ 3.5) + Claims (/ 2.5) + Biology (/ 2.0) + Reproducibility (/ 2.0)`
* **Scientific Core Score (`/ 8.0`):** `Rigor (/ 3.5) + Claims (/ 2.5) + Biology (/ 2.0)` (evaluates purely scientific, methodological, and biological validity independent of archival code/data repository norms).

### Calibration Tiers
| Score | Tier | Diagnostic Interpretation |
| :---: | :--- | :--- |
| **9.0 – 10.0** | **Exemplary** | Sound model specification or primary technical validation, calibrated uncertainty, symmetric controls, compliant open or controlled-access data/code, and claims strictly bounded by statistical support. |
| **7.0 – 8.9** | **Strong / Sound** | Solid methodological, resource, or empirical contribution; minor modeling caveats, unpropagated intermediate uncertainty, or secondary exploratory framing overreach. |
| **5.0 – 6.9** | **Mixed / Salvageable** | Useful dataset or core method, but undermined by 1–2 major pillar violations in the primary analysis (e.g., circular conditioning, uncalibrated tests, or missing baselines) requiring re-analysis. |
| **3.0 – 4.9** | **Flawed / Overstated** | Core conclusions rest on statistical artifacts, severe ascertainment/contamination bias, or biologically implausible assumptions. |
| **1.0 – 2.9** | **Fundamentally Invalid** | Pervasive technical failures, unaddressed leakage/contamination, or claims directly contradicted by the manuscript's own tables. |

*(Note: Empirical calibration outcome tables and operating characteristics across the $N=200$ double-blinded benchmark and companion studies live in [`CALIBRATION.md`](CALIBRATION.md) and are intentionally excluded from this prompt template so that reviews are generated strictly from evaluation instructions without post-calibration anchor numbers.)*

---

## PART 4: THE 3-SECTION CONCISE REVIEW TEMPLATE

```markdown
# Review of [MANUSCRIPT_ID]

**Manuscript Title:** [INSERT FULL MANUSCRIPT TITLE]  
**Authors:** [INSERT AUTHOR LIST]  
**Journal / Preprint:** [INSERT JOURNAL OR PREPRINT ID]  
**Overall Score:** **[X.X] / 10.0** ([Tier Name]) | **Scientific Core Score:** **[X.X] / 8.0**  
* **Score Breakdown:** Rigor: [X.X]/3.5 | Claims: [X.X]/2.5 | Biology: [X.X]/2.0 | Reproducibility: [X.X]/2.0

---

### 1. SUMMARY AND OVERALL IMPACT STATEMENT

[Paragraph 1: Concise summary of what was done. State the core scientific question, dataset size and provenance, mathematical/computational methods, and main quantitative findings. Note the availability of raw data and reproducible code.]

[Paragraph 2: Clear bottom-line evaluation. Acknowledge solid empirical or methodological contributions, then state the primary statistical, modeling, or biological limitations that affect the main conclusions. Conclude with a practical assessment of what is required to resolve them (e.g., whether issues can be addressed through computational re-analysis, formal uncertainty propagation, and reframing without new data generation).]

---

### 2. SPECIFIC CONCLUSIONS AND ASSESSMENT OF SUPPORT

* **[Key Claim 1]: [Well Supported / Moderately Supported / Fragile / Overstated / Methodologically Flawed].**  
  [Briefly explain why: cite the specific evidence, model assumptions, sensitivity analyses, or mathematical/biological caveats.]

* **[Key Claim 2]: [Well Supported / Moderately Supported / Fragile / Overstated / Methodologically Flawed].**  
  [Briefly explain why: e.g., consistency across datasets/methods, or sensitivity to sampling bias, unmodeled confounders, or low statistical power.]

* **[Key Claim 3]: [Well Supported / Moderately Supported / Fragile / Overstated / Methodologically Flawed].**  
  [Briefly explain why: e.g., asymmetric hypothesis testing, unpropagated uncertainty, circular conditioning, or conflict with supplementary tables.]

* **[Key Claim 4]: [Well Supported / Moderately Supported / Fragile / Overstated / Methodologically Flawed].**  
  [Briefly explain why: e.g., biological plausibility, missing baseline comparisons, or contradiction between sections.]

---

### 3. CONSTRUCTIVE AND DOABLE SUGGESTIONS FOR IMPROVEMENT

#### 1. [Actionable Improvement 1 — e.g., Correct Model Specification or Conditioning]
* **WHY:** [Why the current formulation, assumption, or test is statistically or biologically flawed.]
* **WHAT:** [What specific change is needed in the analysis, claims, or figures.]
* **HOW:** [Exact, doable implementation step using the authors' existing data and codebase.]

#### 2. [Actionable Improvement 2 — e.g., Propagate Uncertainty or Calibrate Power/FPR]
* **WHY:** [Why treating intermediate estimates as exact or relying on underpowered tests distorts the conclusion.]
* **WHAT:** [What sensitivity analysis, null simulation, or variance propagation should be added.]
* **HOW:** [Concrete procedure to compute and report the updated intervals, Bayes factors, or $p$-values.]

#### 3. [Actionable Improvement 3 — e.g., Control for Sampling Bias or Confounders]
* **WHY:** [How ascertainment, filtering, or sample-size mismatch biases the current result.]
* **WHAT:** [What subsampling, stratification, or baseline comparison is needed.]
* **HOW:** [Step-by-step description of how to run the control and update the relevant figure/table.]

#### 4. [Actionable Improvement 4 — e.g., Align Scope and Claims with Empirical Support]
* **WHY:** [Where the title, abstract, or discussion overreaches the quantitative evidence.]
* **WHAT:** [Which claims should be tempered or reframed.]
* **HOW:** [Specific textual revisions to bring the narrative in line with the supported results.]

#### 5. [Actionable Improvement 5 — Technical, Mathematical, and Editorial Cleanup]
* **WHY:** [Inconsistencies between text and tables, equation typos, and broken references undermine reader confidence.]
* **WHAT & HOW:** [Itemized list of exact page/line fixes for equations, table values, figure cross-references, and bibliography entries.]
```
