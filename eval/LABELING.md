# How the ground truth was made

This file documents the answer key the accuracy eval scores the app against. Read it before quoting any number from eval/results.json.

## Who labeled it

- The labels were drafted by Claude (model claude-opus-5-5) on 2026-10-04: one labeling agent per lease, working from the rubric below. A second Claude pass then checked every excerpt and statute quote mechanically and reconciled identical clauses across leases (changes listed below).
- No lawyer and no human has reviewed the labels yet. Until someone does, the eval measures agreement with a careful model-written answer key, not with legal ground truth.
- The app under test also uses Claude models (Haiku 4.5 and Sonnet 4.6). The labelers never saw the app's prompts, code or output, and the labels were committed before the pipeline ran on these leases. The reviewing pass had read the app's prompts while building the eval, but no app output on these leases existed when the labels were frozen.

## Files

- `ground_truth.csv`: 25 clauses labeled clear. These define recall: the app should flag each one.
- `ground_truth_arguable.csv`: 125 clauses where reasonable readers could disagree, including the clauses added in the adjudication round below. They are not part of recall, and a flag on one of them is reported but not scored.
- `labels.json`: the full labeler output: page, statute quote, reasoning and confidence for every label, reviewer notes, and for each lease the provisions that were checked and not labeled.

## Test set

| Lease | Jurisdiction | Clear | Arguable | Checked, not labeled |
|---|---|---|---|---|
| DC-1 | DC | 6 | 35 | 59 |
| DC-2 | DC | 4 | 31 | 52 |
| VA-1 | VA | 8 | 20 | 42 |
| VA-2 | VA | 7 | 39 | 58 |

The four leases are real signed leases and are not published. Their file names are replaced with DC-1, DC-2, VA-1 and VA-2 in every file here, and rent and deposit amounts in quoted text with placeholders. The test folder holds seven PDFs but only four distinct leases: two are byte-identical copies of DC-1 and VA-2, and one is a three-page excerpt of DC-2. The duplicates are left out so no clause is counted twice. Pages 71 to 92 of DC-1 are a voter registration packet with no lease terms.

## Law sources

- Virginia: the Virginia Residential Landlord and Tenant Act PDF in the repo (input_data/VA_Data/VA_Residential_Landlord_and_Tenant_Act.pdf), §§ 55.1-1200 to 55.1-1259, read in full by section.
- DC: 14 DCMR Chapter 3 from the repo (input_data/DC_Data/DCHousingRegs.pdf), and D.C. Code sections fetched from code.dccouncil.gov on 2026-10-04: § 42-3201, § 42-3202, § 42-3203, § 42-3204, § 42-3205, § 42-3206, § 42-3207, § 42-3208, § 42-3209, § 42-3210, § 42-3501.03, § 42-3505.01, § 42-3505.02, § 42-3505.06, § 42-3505.07, § 42-3505.10, § 42-3505.11, § 42-3505.31, § 42-3505.51, § 42-3505.52, § 42-3505.53, § 42-3505.54, § 42-3505.55, § 42-3509.02, § 8-241.03, § 8-241.04.
- The DC Tenant Bill of Rights and OTA resource PDFs were used only to find statutes, never cited.

## Rubric

Goal: list every clause in the lease that, on its face, conflicts with the tenant law of the
lease's jurisdiction (DC or Virginia) as written in the law texts provided. These labels are the
answer key the app is scored against, so precision matters as much as coverage.

### Label a clause only when all three hold

1. A specific section in the provided law texts prohibits, voids, caps or requires something,
   and the clause does what the section prohibits, exceeds the cap, falls short of the
   requirement, or waives a right the section says cannot be waived.
2. The conflict is visible from the lease itself (including filled-in amounts and dates). If
   legality depends on facts not in the lease (rent-control status, subsidy status, the unit's
   actual condition), do not label it.
3. You can quote the section text that establishes the conflict, verbatim, from the law files.

Judge the lease as if it were presented for signing on 2026-10-04, against the law in force on
that date. Provisions of law that take effect later (for example D.C. Code § 42-3505.11, which
starts January 1, 2027) do not count.

### Do not label

- Clauses that are one-sided, unusual, expensive or unfavorable, but lawful.
- Clauses that restate or track the law.
- Blank or unfilled form fields.
- Conflicts that need more than one inferential step from the section text.
- Explanatory material (the DC Tenant Bill of Rights and OTA resource files are explainers, not
  law; you may use them to find the right statute, but cite the statute or regulation itself).

### Decision rules (apply these the same way in every lease)

- A liability waiver or limitation that expressly preserves the landlord's liability for its own
  negligence (or for conduct the statute protects) does not conflict with a negligence-based
  prohibition. If it expressly exempts negligence and adds only a generic phrase such as "to the
  extent permitted by law" or "unless otherwise required by law", label it with confidence
  "arguable", not "clear".
- Identical text that appears more than once is labeled once. Different clauses that raise the
  same issue (for example a liability waiver in the lease body and a different one in the
  parking addendum) are separate labels.
- One paragraph with two separate conflicting provisions gets two labels with two excerpts.
- Addenda, community policies and riders are part of the lease.
- Confidence "clear": a careful lawyer reading the clause next to the quoted section would agree
  it conflicts. "arguable": reasonable readers could disagree (savings language, ambiguous
  scope, fact-dependent). Only "clear" labels go into the answer key; "arguable" ones are kept
  for human review.

### Excerpt rules

- Copy the excerpt verbatim from the lease text file (whitespace may differ), one or two
  sentences, about 15 to 70 words, covering the operative language that creates the conflict.
- Never include personal names, unit numbers, phone numbers or email addresses. Pick a span that
  avoids them.
- Do not cross a [Page N] marker.
- If the text file is garbled at that spot (columns interleaved), open the PDF page with the Read
  tool (pages parameter) and copy the excerpt as it reads on the page.

## Reviewer changes

Label numbers refer to each lease's order in labels.json.

- DC-1 label 3 (Waives habitability claims and rent offsets for mixed-use conditions): clear -> arguable. Same text as DC-2 label 23; whether outside noise or odors breach the housing code depends on facts, so arguable in both leases.
- DC-1 label 1 (Payments applied to unpaid charges, including late fees, before rent): clear -> arguable. The clause does not name late fees; the conflict with § 42-3505.31(c)(2) needs the step that unpaid obligations include late fees.
- DC-2 label 20 (Owner disclaims all liability for lost or damaged packages): arguable -> clear. Flat disclaimer of owner liability for a service on the property with no savings language, the same call as the DC-1 pool and controlled-access disclaimers.
- DC-2 label 2: added 14 DCMR § 304.4 as an acceptable citation, since the same sentence makes the tenant cover the owner's attorneys' fees (also arguable label 21).
- DC-2: added arguable label copied from DC-1 label 1 (Payments applied to unpaid charges, including late fees, before rent), same text on page 7. Same text appears in both DC leases; added so the two leases are labeled alike.
- DC-2: added arguable label copied from DC-1 label 12 (Tenant must indemnify owner for its staff's work at tenant request), same text on page 5. Same text appears in both DC leases; added so the two leases are labeled alike.
- DC-2: added arguable label copied from DC-1 label 15 (Drug default applies regardless of tenant's knowledge), same text on page 6. Same text appears in both DC leases; added so the two leases are labeled alike.
- DC-1: added arguable label copied from DC-2 label 7 (Insurance lapse called incurable breach, bypassing 30-day cure right), same text on page 2. Same text appears in both DC leases; added so the two leases are labeled alike.
- DC-1: added arguable label copied from DC-2 label 14 (Separate $20 monthly trash charge billed to tenant), same text on page 13. Same text appears in both DC leases; added so the two leases are labeled alike.
- VA-2 label 1 (Waives rent relief and fitness and habitability claims for mixed-use conditions): clear -> arguable. Same sentence as the DC mixed-use labels; whether outside noise or odors breach § 55.1-1220 depends on facts, so arguable in all three leases.
- VA-2: added arguable label for the crime disclaimer on page 7 (the labeler had listed it as checked, not labeled). The same crime disclaimer is arguable in the other three leases; added so a flag on it is not scored as a false alarm.

## Adjudication round, after the first scored runs

Labels added after a system has run can favor that system, so this round is reported separately.

- **What was judged:** every false alarm whose clause the labelers had never listed, from three setups: the app, the app with an Opus generator, and Opus 5 alone with the lease PDF. After merging flags on the same clause that left 43 distinct clauses (1 flagged by the app, 4 by the app with an Opus generator, 38 by Opus alone; a clause can be flagged by more than one).
- **How:** each clause went to a fresh labeling agent as text cut from the lease itself, with no system name, reasoning or citation, under the same rubric and law texts as the original labels. The adjudicators could not see the existing labels.
- **Two re-judged items:** the first excerpts were widened to sentence edges, and for two clauses (one DC, one VA) that pulled in a neighboring sentence, which the adjudicator judged instead. Both were re-judged on the flagged sentence; `re_judged_on_flagged_sentence` marks them in `adjudication.json`.
- **Result:** 0 clear, 11 arguable, 31 lawful, 1 out of scope (rests on law outside the VRLTA). 11 clauses were added to `ground_truth_arguable.csv`. No clear clause was added, so the clear set that defines recall is unchanged.
- **One disagreement between readers:** while judging the too-wide DC excerpt, the adjudicator called the existing arguable label "Animal rule violations trigger owner's attorney's fees remedy" clear under 14 DCMR § 304.4. The original labeler had judged it arguable because the Lease Contract it refers to has no fee clause. The label was left arguable.
- **Pooling bias:** only clauses some system flagged were re-examined. Clauses no system flagged and the labelers missed are still missing.

Full verdicts, with which setup flagged each clause, are in `adjudication.json`.

## Adjudication round 2, after the two-pass generator run

- **What was judged:** the false alarms of the two-pass generator run that nobody had reviewed. One was the fee table of a provision the labelers had already marked arguable, flagged with its dollar amounts; the table line was added as a second excerpt for that label. The other 5 went to fresh labeling agents under the same blind procedure as round 1, this time with the exact flagged sentence plus surrounding context for reading.
- **Result:** 0 clear, 4 arguable, 1 lawful, 0 out of scope; 4 added to the answer key.
- **Two matching artifacts, left as scored:** in the two-pass run and the cap-20 experiment, a flag on a labeled clause scored below the 0.5 match threshold because the flag and the label quote different sentences of the same provision: the controlled-access covenant not to sue in DC-1 (cap-20 run) and the security-system consent clause in VA-1 (two-pass run, score 0.40). The labels and the threshold were not changed after seeing these results, so each counts as one miss and one false alarm.
