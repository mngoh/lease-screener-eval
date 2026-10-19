# Lease Screener eval

> **Flags 76% of risky clauses on a 4-lease test set, with 32% false alarms.** F1 72% vs. 31% for Claude Opus 5 given the same leases; 9 false alarms vs. 51.

## Head to head with Claude Opus 5 alone

| Setup | Flags | Hits | False alarms | On arguable (unscored) | Precision | Recall | F1 | False alarms reviewed and judged lawful | Est. cost per lease |
|---|---|---|---|---|---|---|---|---|---|
| Lease Screener as shipped (two Opus generator passes, Sonnet judge) | 63 | 19 | 9 | 35 | 68% | 76% | 72% | 8 of 9 | $2.29 |
| Previous version (one Opus generator pass, same judge) | 32 | 13 | 4 | 15 | 76% | 52% | 62% | 4 of 4 | $1.32 |
| Opus 5 alone: lease PDF uploaded, one plain request | 110 | 14 | 51 | 45 | 22% | 56% | 31% | 50 of 51 | $1.28 |

- **The app finds more and is wrong less.** It caught 19 of 25 labeled clauses against Opus alone's 14, with 9 false alarms against 51. Opus alone gives a tenant about 28 flags per lease to sort through; the app about 16, every one quoting law that is in the reference it was given, where the model alone cites from memory.
- **Opus alone's false alarms are real false alarms, not gaps in the answer key.** Every false alarm the labelers had not already considered, from every setup, was judged again blind, without the system's name or reasoning (`eval/LABELING.md`, `eval/adjudication.json`). None was a clear violation; the ones judged arguable are now unscored. Of Opus alone's 51 false alarms, 20 are clauses the labelers had judged lawful, such as Virginia jury waivers and contracted attorney fees, which the VRLTA permits, and 30 were judged lawful or out of scope in the blind review; 1 could not be matched to a reviewed clause.

The control sends each lease PDF to `claude-opus-5` with one request and nothing else: "This is my apartment lease in Washington, DC. Which clauses in it conflict with Washington, DC landlord-tenant law? For each one, quote the clause exactly as it is written in the lease and cite the specific statute or regulation section it conflicts with." Virginia leases name Virginia instead. No statute reference, checklist, judge or flag cap (`eval/chat_baseline.py`).

Lease Screener reads a residential lease PDF and flags clauses that may conflict with Washington, DC
or Virginia tenant law. Two Claude Opus generator passes, one per half of the lease, propose candidate
flags against the statute text, and a Claude Sonnet judge checks each candidate before it reaches the
report.

**The pipeline is closed source. The eval is open.** This repo has the answer key, the labeling rubric,
the blind adjudication of disputed flags, the saved output of every run, the control script, and a
standalone scorer that reproduces every number on this page from those files.

## Reproduce

```bash
python score.py --check
```

Python 3.10+, standard library only. `score.py` re-matches every saved flag to the answer key,
recomputes every metric, miss attribution, citation and cost figure, and exits nonzero if any of them
differs from what the results files recorded or from this README (`python score.py --readme`
regenerates it). Two inputs are read as recorded, because computing them needs files that stay
private: whether a missed clause appears in the app's parsed lease text (the lease PDFs and the app's
parser), and how many labeled statutes appear in the law reference the model was given (the curated
reference).

## Measured accuracy

Measured 2026-10-05 on 4 leases with 25 labeled risky clauses, before and after the fixes described below. Before: recall 8%, precision 33%, false-alarm rate 67%. After: recall 76%, precision 68%, false-alarm rate 32%.

| Run | Set | Leases | Labeled clauses | Flags | Hits | Misses | False alarms | Precision | Recall | F1 | False-alarm rate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Before | Overall | 4 | 25 | 12 | 2 | 23 | 4 | 33% | 8% | 13% | 67% |
| Before | DC | 2 | 10 | 6 | 1 | 9 | 1 | 50% | 10% | 17% | 50% |
| Before | VA | 2 | 15 | 6 | 1 | 14 | 3 | 25% | 7% | 11% | 75% |
| After | Overall | 4 | 25 | 63 | 19 | 6 | 9 | 68% | 76% | 72% | 32% |
| After | DC | 2 | 10 | 33 | 8 | 2 | 3 | 73% | 80% | 76% | 27% |
| After | VA | 2 | 15 | 30 | 11 | 4 | 6 | 65% | 73% | 69% | 35% |

- **Citation accuracy:** 19 of 19 correctly flagged clauses also cite the labeled statute (100%). Flagged and correctly cited: 19 of 25 labeled clauses (76%).
- **Unscored flags:** 35 flags landed on clauses labeled arguable (reasonable readers could disagree). They count as neither hits nor false alarms.
- **Recall ceiling:** the report shows at most 20 flags per lease, so the best possible recall on this set is 100%.
- **Grounding:** 63 of 63 reported flags quote law text that is actually in the law reference the model was given, and 17 of 25 labeled statutes appear in that reference at all.

### Where the misses happen

0 of 6 misses never reached the model (the PDF parser lost the clause). 6 reached the model and were not flagged. By pipeline stage:

| Stage | Misses | What happened |
|---|---|---|
| extraction | 0 | Not in the parsed lease text, so the model never saw it |
| generator | 6 | In the text, but the generator did not propose it |
| parse | 0 | Proposed in a format the flag parser drops |
| cap | 0 | Proposed after the first 20 flags, so never judged |
| judge | 0 | Proposed, then rejected by the Sonnet judge |
| dedup | 0 | Approved, then removed as a duplicate |
| merged | 0 | Quoted inside a flag already matched to another clause |

## Run by run

Each row is one full run of the eval on the same 4 leases and 25 labeled clauses, all scored against the current answer key. Numbers come from `eval/baseline/results.json`, `eval/runs/*/results.json` and `eval/results.json`.

| Run | Flags | Hits | False alarms | On arguable | Precision | Recall | F1 | False-alarm rate | Matched flags citing the labeled statute |
|---|---|---|---|---|---|---|---|---|---|
| Baseline: app as first measured (Haiku generator) | 12 | 2 | 4 | 6 | 33% | 8% | 13% | 67% | 1 of 2 |
| Baseline code, Opus generator | 19 | 6 | 6 | 7 | 50% | 24% | 32% | 50% | 3 of 6 |
| Fix A: curated statute reference | 13 | 2 | 5 | 6 | 29% | 8% | 12% | 71% | 2 of 2 |
| Fixes A+B: VA attorney-fee rule removed | 12 | 2 | 4 | 6 | 33% | 8% | 13% | 67% | 2 of 2 |
| Fixes A+B+C: statute-driven checklist, cap 10 | 22 | 7 | 7 | 8 | 50% | 28% | 36% | 50% | 7 of 7 |
| A+B+C plus prompt consistency fixes, Haiku generator | 24 | 7 | 7 | 10 | 50% | 28% | 36% | 50% | 7 of 7 |
| D: the same with the Opus generator | 34 | 10 | 9 | 15 | 53% | 40% | 45% | 47% | 10 of 10 |
| E: D plus the rewritten judge | 32 | 13 | 4 | 15 | 76% | 52% | 62% | 24% | 13 of 13 |
| Experiment, not shipped: E with a 20-flag cap in one pass | 65 | 18 | 12 | 35 | 60% | 72% | 65% | 40% | 17 of 18 |
| Shipped: E with two parallel generator passes | 63 | 19 | 9 | 35 | 68% | 76% | 72% | 32% | 19 of 19 |
| Control: Opus 5 alone, lease PDF uploaded, no app | 110 | 14 | 51 | 45 | 22% | 56% | 31% | 78% | 12 of 14 |

## What the eval found

- **The models were not given the law.** The reference text sent to the generator and judge was the first N
  characters of each source file. For Virginia that was the definitions section of the VRLTA and nothing
  else. For DC the generator's slice contained no 14 DCMR section, and the live D.C. Code fetch returned a
  table of contents. The prompts told the models to quote the reference verbatim and cite specific sections,
  so most baseline flags quoted law that was not in front of the model. Fix A loads the VRLTA sections on
  lease terms, duties and remedies, all of 14 DCMR Chapter 3, and a saved excerpt of the relevant D.C. Code
  sections. It fixed citations and grounding but not recall.
- **The VA prompt ordered a false alarm.** It said to always flag tenant-paid attorney fees. VRLTA
  § 55.1-1245(H) allows reasonable attorney fees when the lease provides for them, and § 55.1-1208(A)(4)
  bars them only "except as provided in this chapter". Fix B removed the rule.
- **The generator filled a quota.** It produced exactly five flags on every lease, almost all from the first
  three checklist items (maintenance liability waivers, "as is" clauses, attorney fees), and never proposed
  the labeled clauses outside those categories. Fix C rewrites the checklist around the clause types the
  loaded statutes regulate and raises the cap from five to ten. This is where most of the recall gain came from.
- **A hardcoded citation did not exist.** The DC prompt cited D.C. Code § 42-3504.07 for "as is" clauses;
  that section returns a 404 on code.dccouncil.gov. It now cites 14 DCMR § 304.1 and § 301.1.
- **The judge worked against the law in both directions.** Its decision tree said to always approve a
  liability waiver even when it expressly keeps the landlord liable for negligence, and an "as is" clause even
  when it excepts conditions the law requires fixed; 14 DCMR § 304.3 and VRLTA § 55.1-1208(A)(5) bar waivers
  of that liability, not clauses that preserve it. Its rejection rules (a "non-committal language" rule that
  fired on the title suffix the generator is required to write, a "concrete harm" gate, and "if uncertain,
  REJECT") discarded flags the judge itself described as violations. The rewritten judge approves a clause
  when a provision in the reference prohibits what it does, rejects clauses that track or preserve the law,
  and reads the lease around the clause instead of the first 8,000 characters.
- **Switching the generator to Opus (D) raised recall.** With the same prompts, statute reference and judge, recall went from 28% to 40% and precision from 50% to 53%, for an estimated $0.61 more per lease. If Opus declines a request, the Haiku generator answers instead.
- **Rewriting the judge (E) was the largest gain in precision.** On the same generator output, false alarms went from 9 to 4, labeled clauses lost at the judge from 4 to 1, precision from 53% to 76% and recall from 40% to 52%.
- **Two parallel generator passes were the largest gain in recall.** After E the generator returned exactly its ten-flag cap on every lease, and most misses were liability waivers inside addenda. A single pass with a 20-flag cap found them (recall 72%) but its generator took up to 140 seconds per lease and the whole pipeline up to 146, against the report route's 150-second timeout, so it was not shipped. Splitting each lease in half and reviewing the halves in parallel, ten flags each, raised recall from 52% to 76% and F1 from 62% to 72%, with precision going from 76% to 68%. The slowest generator pass took 81 seconds and the slowest lease 90 seconds end to end. Estimated cost went from $1.32 to $2.29 per lease.
- **Remaining misses are the generator.** 6 of 6 misses were in the lease text and never proposed, and each pass still returns its full ten candidates, so more passes or a larger per-pass budget is the next lever, with latency as the constraint.

## Limits of this measurement

- The labels were written by Claude, the app runs on Claude models, and no lawyer has reviewed the labels.
- The fixed checklist names the topics of the same statute sections the labels were derived from, so part of
  the gain is that alignment. Leases outside this set are the real test.
- Four leases and 25 clauses. One clause changes recall by four points. Each configuration was run once, so
  run-to-run variation is not measured.
- The checklist was refined once after the first fixed run: the DC move-out item gained the statute's own
  exception (a longer rent-increase notice), which both DC leases satisfy.
- Two flags on labeled clauses quote a different sentence of the same provision than the label does and score
  below the match threshold, so they count as a miss and a false alarm (`eval/LABELING.md`). The labels and
  threshold were not changed after seeing this, so recall and precision are slightly understated.
- The judge rewrite was measured on the same 25 labels it is scored on. Its rules come from the statute text,
  as the labels do, which is also why they agree; leases the app has not seen are the real test.
- The adjudication round re-examined only clauses some system flagged; clauses every system missed and the
  labelers missed are still missing from the answer key.

## Method

- Each run passed the labeled leases through the app's own report pipeline offline, with database, payment and email code stubbed out, and saved every generator candidate, judge decision and reported flag to its results file. That harness imports the app and stays closed source with it; `score.py` scores the saved output.
- The test leases are real signed leases and are not published. Their file names are replaced with DC-1, DC-2, VA-1 and VA-2 in every file here, and rent and deposit amounts in quoted text with placeholders such as `[rent]`; `python score.py --check` confirms the redaction changes no score.
- Ground truth is `eval/ground_truth.csv` (25 clauses labeled clear) plus `eval/ground_truth_arguable.csv` (125 clauses, unscored). The labels were drafted by Claude from the lease text and the statute text only, with no access to the app. The clear labels were frozen before the pipeline ran; arguable labels were added afterward in a blind adjudication of flagged clauses. No lawyer has reviewed them yet; see `eval/LABELING.md` for the rubric, sources, the adjudication rounds and every reviewer change.
- A flag counts as a hit when its quoted clause matches a labeled excerpt (word-trigram containment, threshold 0.5). Precision = hits / (hits + false alarms). Recall = hits / labeled clauses. False-alarm rate = false alarms / (hits + false alarms).
- Models: generator `claude-opus-5`, judge `claude-sonnet-4-6`. App commit `9c1b4a219a`. Full per-lease tables with clause text: `eval/results.md`.

## Files

| Path | What it is |
|---|---|
| `score.py` | Standalone scorer; checks and regenerates this README |
| `eval/ground_truth.csv` | The 25 clauses labeled clear; they define recall |
| `eval/ground_truth_arguable.csv` | 125 clauses reasonable readers could disagree on; flags on them are unscored |
| `eval/LABELING.md` | Rubric, law sources, reviewer changes, both adjudication rounds |
| `eval/adjudication.json` | Blind verdicts on false alarms no labeler had considered |
| `eval/chat_baseline.py` | The control: each lease PDF sent to Opus 5 with one plain request |
| `eval/results.json`, `eval/results.md` | The shipped configuration |
| `eval/baseline/results.json` | The app as first measured |
| `eval/runs/*/results.json` | Every other run above; `two_pass` is the shipped configuration's live run, used for its timings |
