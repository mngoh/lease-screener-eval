"""
Standalone scorer for the Lease Screener accuracy eval.

Lease Screener's pipeline is closed source; this scorer is not. It re-scores the
saved output of every eval run (eval/results.json, eval/baseline/results.json and
eval/runs/*/results.json) against the answer key, with no model calls, no lease
PDFs and no app code, and renders README.md from the result.

Matching: a reported flag hits a labeled clause when at least half of the shorter
text's distinct word trigrams occur in the longer one (trigram containment, 0.5).
Matching is one-to-one, best pair first. A flag that hits no clear label but
matches an arguable one (eval/ground_truth_arguable.csv) is unscored; every other
flag is a false alarm.

Two inputs are read as recorded at run time because computing them needs files
that stay private: whether a missed clause appears in the app's parsed lease text
(needs the lease PDFs and the app's parser), and how many labeled statutes appear
in the law reference the model was given (needs the curated reference). Without
eval/labels.json, the false-alarm triage is read as recorded too.

Usage, Python 3.10+, standard library only:
    python score.py            # scores for every run
    python score.py --check    # verify against every results file and README.md; exit 1 on any difference
    python score.py --readme   # rewrite README.md from the results files
"""

import argparse
import csv
import functools
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "eval"
GROUND_TRUTH = EVAL / "ground_truth.csv"
GROUND_TRUTH_ARGUABLE = EVAL / "ground_truth_arguable.csv"
LABELS_JSON = EVAL / "labels.json"
ADJUDICATION_JSON = EVAL / "adjudication.json"
README = ROOT / "README.md"
THRESHOLD = 0.5

# USD per million tokens (input, output), Anthropic API list prices on 2026-10-04, matched by model-id prefix.
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-5": (5.0, 25.0)}

# Pipeline stage that lost each missed clause, and the bucket it rolls up to.
STAGES = {
    "extraction": "retrieval",  # not in the parsed lease text, so the model never saw it
    "generator": "judgment",    # in the text, but the generator did not propose it
    "parse": "judgment",        # proposed in a format the flag parser drops
    "cap": "judgment",          # proposed, but dropped before judging (past the flag cap)
    "judge": "judgment",        # proposed, then rejected by the judge
    "dedup": "judgment",        # approved, then removed as a duplicate
    "merged": "judgment",       # quoted inside a flag already matched to another label
}

RUNS = [  # (label, results file), in the order of the run-by-run table
    ("Baseline: app as first measured (Haiku generator)", "baseline"),
    ("Baseline code, Opus generator", "runs/opus_baseline"),
    ("Fix A: curated statute reference", "runs/fixA_law"),
    ("Fixes A+B: VA attorney-fee rule removed", "runs/fixAB_va_fees"),
    ("Fixes A+B+C: statute-driven checklist, cap 10", "runs/fixABC_checklist"),
    ("A+B+C plus prompt consistency fixes, Haiku generator", "runs/final_haiku"),
    ("D: the same with the Opus generator", "runs/opus_judge_v1"),
    ("E: D plus the rewritten judge", "runs/opus_judge_v2"),
    ("Experiment, not shipped: E with a 20-flag cap in one pass", "runs/cap20"),
    ("Shipped: E with two parallel generator passes", "."),
    ("Control: Opus 5 alone, lease PDF uploaded, no app", "runs/chat_opus"),
]
EXTRA_RUNS = ["runs/two_pass"]  # the shipped configuration's live run, used for its timings


def results_path(key: str) -> Path:
    return EVAL / key / "results.json"


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+")
_PAGE_MARKER_RE = re.compile(r"\[Page \d+\]")


@functools.lru_cache(maxsize=4096)
def _tokens(text: str) -> tuple:
    """Lowercased NFKC word tokens with their character spans, skipping [Page N] markers."""
    markers = [m.span() for m in _PAGE_MARKER_RE.finditer(text)]
    out, mi = [], 0
    for m in _WORD_RE.finditer(text):
        while mi < len(markers) and markers[mi][1] <= m.start():
            mi += 1
        if mi < len(markers) and markers[mi][0] <= m.start():
            continue
        out.append((unicodedata.normalize("NFKC", m.group()).lower(), m.start(), m.end()))
    return tuple(out)


def locate(excerpt: str, text: str, window="auto"):
    """Share of the excerpt's distinct word trigrams that occur inside one window of `text`.
    The window is twice the excerpt's length plus 10 words ("auto") or the whole text (None).
    Returns (score, start, end) with character offsets into `text`."""
    ex = [t for t, _, _ in _tokens(excerpt)]
    toks = _tokens(text)
    if not ex or not toks:
        return 0.0, None, None
    n = min(3, len(ex))
    grams = {tuple(ex[i:i + n]) for i in range(len(ex) - n + 1)}
    words = [t for t, _, _ in toks]
    hits = [(i, tuple(words[i:i + n])) for i in range(len(words) - n + 1) if tuple(words[i:i + n]) in grams]
    if not hits:
        return 0.0, None, None
    span = len(words) if window is None else (2 * len(ex) + 10 if window == "auto" else window)
    counts, best, lo = Counter(), (0, 0, 0), 0
    for hi, (pos, g) in enumerate(hits):
        counts[g] += 1
        while pos - hits[lo][0] > span:
            old = hits[lo][1]
            counts[old] -= 1
            if not counts[old]:
                del counts[old]
            lo += 1
        if len(counts) > best[0]:
            best = (len(counts), lo, hi)
    found, lo, hi = best
    return found / len(grams), toks[hits[lo][0]][1], toks[hits[hi][0] + n - 1][2]


def similarity(a: str, b: str) -> float:
    """How much of the shorter text appears in the longer one (0 to 1)."""
    if len(_tokens(a)) > len(_tokens(b)):
        a, b = b, a
    return locate(a, b, window=None)[0]


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

_DCMR_RE = re.compile(r"DCMR\W{0,6}(?:sec(?:tion)?\.?\s*)?(\d+(?:\.\d+)*)", re.IGNORECASE)
_CODE_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?-\d{2,5}(?:\.\d+)*)")


def citation_keys(text: str) -> set:
    """14 DCMR 304.3 -> ('dcmr', '304.3'); D.C. Code 42-3505.51 -> ('code', '42-3505.51'); Va. Code 55.1-1204(A) -> ('code', '55.1-1204')."""
    text = text or ""
    return {("dcmr", m) for m in _DCMR_RE.findall(text)} | {("code", m) for m in _CODE_RE.findall(text)}


def _same_section(a: tuple, b: tuple) -> bool:
    """Same section, or one is a subsection of the other (304 matches 304.3; 304.1 does not)."""
    return a[0] == b[0] and (a[1] == b[1] or a[1].startswith(b[1] + ".") or b[1].startswith(a[1] + "."))


def citation_correct(gt_citation: str, flag: dict):
    """True or False, or None when the label's citation has no section number to score."""
    gt = citation_keys(gt_citation)
    if not gt:
        return None
    cited = citation_keys(flag["source"]) | citation_keys(flag["law"])
    return any(_same_section(g, c) for g in gt for c in cited)


# ---------------------------------------------------------------------------
# Answer key
# ---------------------------------------------------------------------------

def load_ground_truth(path: Path) -> dict:
    """{lease_file: {"jurisdiction", "rows"}}. Rows keep their CSV line number."""
    if not path.is_file():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    leases = {}
    for n, row in enumerate(rows, start=2):
        lease_file = (row["lease_file"] or "").strip()
        if not lease_file:
            continue
        lease = leases.setdefault(lease_file, {"jurisdiction": row["jurisdiction"].strip().upper(), "rows": []})
        excerpt = (row["clause_excerpt"] or "").strip()
        if excerpt and excerpt.upper() != "NONE":
            lease["rows"].append({"line": n, "clause_excerpt": excerpt, "issue": (row["issue"] or "").strip(),
                                  "statute_citation": (row["statute_citation"] or "").strip()})
    return leases


# ---------------------------------------------------------------------------
# Scoring one run
# ---------------------------------------------------------------------------

def classify_miss(row: dict, run: dict, lease_text_score) -> str:
    """The pipeline stage where a labeled clause was lost."""
    if lease_text_score is not None and lease_text_score < THRESHOLD:
        return "extraction"
    matched = [(s, c) for c in run["candidates"] if (s := similarity(row["clause_excerpt"], c["match_text"])) >= THRESHOLD]
    if not matched:
        return "parse" if locate(row["clause_excerpt"], run["generator_raw"])[0] >= THRESHOLD else "generator"
    _, c = max(matched, key=lambda item: (item[1]["in_report"], item[1]["approved"], item[1]["judged"], item[0]))
    if c["in_report"]:
        return "merged"
    if c["approved"]:
        return "dedup"
    return "judge" if c["judged"] else "cap"


def evaluate_lease(run: dict, rows: list, arguable: list, recorded: dict) -> dict:
    flags = [c for c in run["candidates"] if c["in_report"]]
    pairs = sorted(((similarity(r["clause_excerpt"], f["match_text"]), ri, fi)
                    for ri, r in enumerate(rows) for fi, f in enumerate(flags)), key=lambda p: -p[0])
    row_to_flag, flag_to_row = {}, {}
    for score, ri, fi in pairs:
        if score < THRESHOLD:
            break
        if ri in row_to_flag or fi in flag_to_row:
            continue
        row_to_flag[ri], flag_to_row[fi] = fi, ri

    hits = [{"gt": rows[ri], "citation_correct": citation_correct(rows[ri]["statute_citation"], flags[fi])}
            for ri, fi in sorted(row_to_flag.items())]
    misses = []
    for ri, r in enumerate(rows):
        if ri not in row_to_flag:
            in_text = recorded["lease_text_score"].get((run["lease_file"], r["clause_excerpt"]))
            misses.append({"gt": r, "stage": classify_miss(r, run, in_text), "lease_text_recorded": in_text is not None})

    false_alarms, on_arguable = [], 0
    for fi, f in enumerate(flags):
        if fi in flag_to_row:
            continue
        if any(similarity(a["clause_excerpt"], f["match_text"]) >= THRESHOLD for a in arguable):
            on_arguable += 1
            continue
        closest = max((s for s, _, fj in pairs if fj == fi), default=0.0)
        false_alarms.append({"flag": f, "closest_gt_score": closest})
    return {"lease_file": run["lease_file"], "jurisdiction": run["jurisdiction"], "gt_clauses": len(rows),
            "flags": len(flags), "tp": len(hits), "fp": len(false_alarms), "fn": len(misses),
            "on_arguable": on_arguable, "hits": hits, "misses": misses, "false_alarms": false_alarms}


def _ratio(num: int, den: int):
    return round(num / den, 4) if den else None


def scores(evals: list) -> dict:
    tp, fp, fn = (sum(e[k] for e in evals) for k in ("tp", "fp", "fn"))
    on_arguable = sum(e["on_arguable"] for e in evals)
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = None if precision is None or recall is None else (
        round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0)
    return {"leases": len(evals), "gt_clauses": tp + fn, "flags": tp + fp + on_arguable, "tp": tp, "fp": fp,
            "fn": fn, "on_arguable": on_arguable, "precision": precision, "recall": recall, "f1": f1,
            "false_alarm_rate": _ratio(fp, tp + fp)}


def citation_scores(evals: list) -> dict:
    hits = [h for e in evals for h in e["hits"]]
    scored = [h for h in hits if h["citation_correct"] is not None]
    correct = sum(h["citation_correct"] for h in scored)
    with_citation = sum(1 for e in evals for r in [h["gt"] for h in e["hits"]] + [m["gt"] for m in e["misses"]]
                        if citation_keys(r["statute_citation"]))
    return {"matched_flags_scored": len(scored), "correct": correct, "accuracy_of_matched": _ratio(correct, len(scored)),
            "gt_clauses_with_citation": with_citation, "flagged_and_correctly_cited": _ratio(correct, with_citation)}


def miss_breakdown(evals: list) -> dict:
    stages = Counter(m["stage"] for e in evals for m in e["misses"])
    buckets = Counter()
    for stage, n in stages.items():
        buckets[STAGES[stage]] += n
    return {"by_stage": {s: stages.get(s, 0) for s in STAGES},
            "by_bucket": {b: buckets.get(b, 0) for b in ("retrieval", "judgment")}}


def false_alarm_triage(evals: list, recorded: dict) -> dict:
    """Sort false alarms four ways: a second flag on a clause another flag already matched, a clause the
    labelers checked and judged lawful (labels.json), a clause the blind adjudication judged lawful or out
    of scope (adjudication.json), or a clause nobody has reviewed. Only the last can hold issues the
    answer key missed."""
    reviewed = None
    if LABELS_JSON.is_file():
        reviewed = {d["lease_file"]: d.get("reviewed_not_labeled", []) for d in json.loads(LABELS_JSON.read_text("utf-8"))}
    adjudicated = {}
    for a in json.loads(ADJUDICATION_JSON.read_text("utf-8")):
        if a["verdict"] in ("lawful", "out_of_scope"):
            adjudicated.setdefault(a["lease_file"], []).append(a["flagged_text"])
    counts = Counter()
    for e in evals:
        for fa in e["false_alarms"]:
            clause = fa["flag"]["clause"] or ""
            if reviewed is None:
                kind = recorded["triage"].get((e["lease_file"], fa["flag"]["index"]))
            elif fa["closest_gt_score"] >= THRESHOLD:
                kind = "repeat_flag_on_labeled_clause"
            elif any(locate(r["excerpt_start"], clause, window=None)[0] >= 0.5 for r in reviewed.get(e["lease_file"], [])):
                kind = "reviewed_and_judged_lawful"
            elif any(similarity(t, clause) >= 0.5 for t in adjudicated.get(e["lease_file"], []) if t):
                kind = "adjudicated_not_a_violation"
            else:
                kind = "never_reviewed"
            counts[kind] += 1
    return {k: counts.get(k, 0) for k in ("repeat_flag_on_labeled_clause", "reviewed_and_judged_lawful",
                                          "adjudicated_not_a_violation", "never_reviewed")}


def per_lease_cost(estimated: dict | None, leases: int):
    """Recomputed from the token counts the run recorded, at PRICES."""
    if not estimated or not leases:
        return None
    total = 0.0
    for row in estimated["by_call_type"]:
        price = next((p for prefix, p in PRICES.items() if row["model"].startswith(prefix)), None)
        total += row["tokens"] / 1e6 * price[0 if row["kind"] == "input" else 1] if price else 0.0
    return round(total / leases, 2)


def score_run(path: Path, gt: dict, arguable: dict) -> dict:
    """Re-score one saved results file. Returns a dict shaped like the file's own summary fields."""
    saved = json.loads(path.read_text("utf-8"))
    recorded = {
        "lease_text_score": {(e["lease_file"], m["gt"]["clause_excerpt"]): m.get("lease_text_score")
                             for e in saved["leases"] for m in e["misses"]},
        "triage": {(e["lease_file"], fa["flag"]["index"]): fa.get("triage")
                   for e in saved["leases"] for fa in e["false_alarms"]},
    }
    runs = saved["pipeline_runs"]
    evals = [evaluate_lease(run, gt[run["lease_file"]]["rows"], arguable.get(run["lease_file"], {}).get("rows", []), recorded)
             for run in runs]
    v = saved["versions"]
    cap = v.get("max_report_flags")
    gt_total = sum(e["gt_clauses"] for e in evals)
    out = {
        "path": path,
        "saved": saved,
        "versions": v,
        "scores": {"overall": scores(evals),
                   "by_jurisdiction": {j: scores([e for e in evals if e["jurisdiction"] == j])
                                       for j in sorted({e["jurisdiction"] for e in evals})}},
        "leases": evals,
        "citation": citation_scores(evals),
        "misses": miss_breakdown(evals),
        "false_alarm_triage": false_alarm_triage(evals, recorded),
        "cost_per_lease": per_lease_cost(v.get("estimated_cost"), len(runs)),
        "slowest_generator_s": max(((r.get("generator_latency_s") or 0) for r in runs), default=0),
        "slowest_lease_s": max(((r.get("wall_seconds_this_run") or 0) for r in runs), default=0),
        "miss_text_not_recorded": sum(not m["lease_text_recorded"] for e in evals for m in e["misses"]),
    }
    if cap:
        out["recall_ceiling"] = _ratio(sum(min(e["gt_clauses"], cap) for e in evals), gt_total)
    if "grounding" in saved:
        flags = [c for r in runs for c in r["candidates"] if c["in_report"]]
        quoted = [c for c in flags if c.get("law_quote_score") is not None]
        out["grounding"] = {
            "flags_with_law_quote": len(quoted),
            "flags_law_quote_found_in_generator_reference": sum(c["law_quote_score"] >= THRESHOLD for c in quoted),
            "gt_citations": sum(1 for e in evals for r in [h["gt"] for h in e["hits"]] + [m["gt"] for m in e["misses"]]
                                if citation_keys(r["statute_citation"])),
            # needs the law reference the model was given, which is not public: as recorded
            "gt_citations_present_in_generator_reference": saved["grounding"]["gt_citations_present_in_generator_reference"],
        }
    return out


def score_all() -> dict:
    gt, arguable = load_ground_truth(GROUND_TRUTH), load_ground_truth(GROUND_TRUTH_ARGUABLE)
    keys = [k for _, k in RUNS] + EXTRA_RUNS
    return {k: score_run(results_path(k), gt, arguable) for k in keys if results_path(k).is_file()}


# ---------------------------------------------------------------------------
# Checking against the results files
# ---------------------------------------------------------------------------

def check_run(r: dict) -> list:
    """Every difference between what this scorer computes and what the results file recorded."""
    s, diffs = r["saved"], []

    def cmp(name, mine, theirs):
        if mine != theirs:
            diffs.append(f"{name}: scorer {mine!r}, file {theirs!r}")

    for key in ("overall",):
        cmp("scores.overall", r["scores"]["overall"], s["scores"]["overall"])
    for j, sc in r["scores"]["by_jurisdiction"].items():
        cmp(f"scores.{j}", sc, s["scores"]["by_jurisdiction"][j])
    for mine, theirs in zip(r["leases"], s["leases"]):
        for k in ("gt_clauses", "flags", "tp", "fp", "fn", "on_arguable"):
            cmp(f"{mine['lease_file']}.{k}", mine[k], theirs[k])
        cmp(f"{mine['lease_file']}.miss stages", sorted((m["gt"]["line"], m["stage"]) for m in mine["misses"]),
            sorted((m["gt"]["line"], m["stage"]) for m in theirs["misses"]))
    cmp("citation", r["citation"], {k: s["citation"][k] for k in r["citation"]})
    cmp("misses.by_stage", r["misses"]["by_stage"], s["misses"]["by_stage"])
    cmp("misses.by_bucket", r["misses"]["by_bucket"], s["misses"]["by_bucket"])
    if "false_alarm_triage" in s:
        cmp("false_alarm_triage", r["false_alarm_triage"], s["false_alarm_triage"])
    if "recall_ceiling" in r:
        cmp("recall_ceiling", r["recall_ceiling"], s.get("recall_ceiling"))
    if "grounding" in r:
        cmp("grounding", r["grounding"], {k: s["grounding"][k] for k in r["grounding"]})
    est = s["versions"].get("estimated_cost")
    if est:
        cmp("cost per lease", r["cost_per_lease"], est["per_lease_usd"])
    return diffs


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

def pct(x) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


INTRO = """Lease Screener reads a residential lease PDF and flags clauses that may conflict with Washington, DC
or Virginia tenant law. Two Claude Opus generator passes, one per half of the lease, propose candidate
flags against the statute text, and a Claude Sonnet judge checks each candidate before it reaches the
report.

**The pipeline is closed source. The eval is open.** This repo has the answer key, the labeling rubric,
the blind adjudication of disputed flags, the saved output of every run, the control script, and a
standalone scorer that reproduces every number on this page from those files."""

REPRODUCE = """## Reproduce

```bash
python score.py --check
```

Python 3.10+, standard library only. `score.py` re-matches every saved flag to the answer key,
recomputes every metric, miss attribution, citation and cost figure, and exits nonzero if any of them
differs from what the results files recorded or from this README (`python score.py --readme`
regenerates it). Two inputs are read as recorded, because computing them needs files that stay
private: whether a missed clause appears in the app's parsed lease text (the lease PDFs and the app's
parser), and how many labeled statutes appear in the law reference the model was given (the curated
reference)."""

FINDINGS = """## What the eval found

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
  and reads the lease around the clause instead of the first 8,000 characters."""

LIMITS = """## Limits of this measurement

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
  labelers missed are still missing from the answer key."""

CONTROL_PROMPT = ("This is my apartment lease in Washington, DC. Which clauses in it conflict with Washington, DC "
                  "landlord-tenant law? For each one, quote the clause exactly as it is written in the lease and cite "
                  "the specific statute or regulation section it conflicts with.")


def _score_rows(label: str, r: dict) -> list:
    sets = [("Overall", r["scores"]["overall"])] + sorted(r["scores"]["by_jurisdiction"].items())
    return [f"| {label} | {name} | {s['leases']} | {s['gt_clauses']} | {s['flags']} | {s['tp']} | {s['fn']} | {s['fp']} "
            f"| {pct(s['precision'])} | {pct(s['recall'])} | {pct(s['f1'])} | {pct(s['false_alarm_rate'])} |"
            for name, s in sets]


def head_to_head(res: dict) -> list:
    app, prev, chat = res["."], res["runs/opus_judge_v2"], res["runs/chat_opus"]
    ao, co, ct = app["scores"]["overall"], chat["scores"]["overall"], chat["false_alarm_triage"]
    lines = [
        "| Setup | Flags | Hits | False alarms | On arguable (unscored) | Precision | Recall | F1 | False alarms reviewed and judged lawful | Est. cost per lease |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, r in (("Lease Screener as shipped (two Opus generator passes, Sonnet judge)", app),
                     ("Previous version (one Opus generator pass, same judge)", prev),
                     ("Opus 5 alone: lease PDF uploaded, one plain request", chat)):
        o, t = r["scores"]["overall"], r["false_alarm_triage"]
        reviewed = t["reviewed_and_judged_lawful"] + t["adjudicated_not_a_violation"]
        lines.append(f"| {label} | {o['flags']} | {o['tp']} | {o['fp']} | {o['on_arguable']} | {pct(o['precision'])} "
                     f"| {pct(o['recall'])} | {pct(o['f1'])} | {reviewed} of {o['fp']} | ${r['cost_per_lease']:.2f} |")
    if ao["tp"] >= co["tp"]:
        lead = (f"- **The app finds more and is wrong less.** It caught {ao['tp']} of {ao['gt_clauses']} labeled "
                f"clauses against Opus alone's {co['tp']}, with {ao['fp']} false alarms against {co['fp']}.")
    else:
        lead = (f"- **Opus alone finds more and is wrong more.** It caught {co['tp']} of {co['gt_clauses']} labeled "
                f"clauses against the app's {ao['tp']}, with {co['fp']} false alarms against {ao['fp']}.")
    lines += [
        "",
        lead + f" Opus alone gives a tenant about {round(co['flags'] / co['leases'])} flags per lease to sort "
        f"through; the app about {round(ao['flags'] / ao['leases'])}, every one quoting law that is in the reference "
        "it was given, where the model alone cites from memory.",
        "- **Opus alone's false alarms are real false alarms, not gaps in the answer key.** Every false alarm the "
        "labelers had not already considered, from every setup, was judged again blind, without the system's name or "
        "reasoning (`eval/LABELING.md`, `eval/adjudication.json`). None was a clear violation; the ones judged "
        f"arguable are now unscored. Of Opus alone's {co['fp']} false alarms, {ct['reviewed_and_judged_lawful']} are "
        "clauses the labelers had judged lawful, such as Virginia jury waivers and contracted attorney fees, which the "
        f"VRLTA permits, and {ct['adjudicated_not_a_violation']} were judged lawful or out of scope in the blind review"
        + (f"; {ct['never_reviewed']} could not be matched to a reviewed clause." if ct["never_reviewed"] else "."),
        "",
        f"The control sends each lease PDF to `{chat['versions']['model']}` with one request and nothing else: "
        f"\"{CONTROL_PROMPT}\" Virginia leases name Virginia instead. No statute reference, checklist, judge or flag cap "
        "(`eval/chat_baseline.py`).",
    ]
    return lines


def dynamic_findings(res: dict) -> list:
    h, o1, o2, c20, tp, r = (res[k] for k in ("runs/final_haiku", "runs/opus_judge_v1", "runs/opus_judge_v2",
                                              "runs/cap20", "runs/two_pass", "."))
    ho, oo = h["scores"]["overall"], o1["scores"]["overall"]
    a, b = o1["scores"]["overall"], o2["scores"]["overall"]
    eo, co, ro = o2["scores"]["overall"], c20["scores"]["overall"], r["scores"]["overall"]
    misses = [m for e in r["leases"] for m in e["misses"]]
    return [
        f"- **Switching the generator to Opus (D) raised recall.** With the same prompts, statute reference and "
        f"judge, recall went from {pct(ho['recall'])} to {pct(oo['recall'])} and precision from "
        f"{pct(ho['precision'])} to {pct(oo['precision'])}, for an estimated "
        f"${o1['cost_per_lease'] - h['cost_per_lease']:.2f} more per lease. "
        "If Opus declines a request, the Haiku generator answers instead.",
        f"- **Rewriting the judge (E) was the largest gain in precision.** On the same generator output, false "
        f"alarms went from {a['fp']} to {b['fp']}, labeled clauses lost at the judge from "
        f"{o1['misses']['by_stage']['judge']} to {o2['misses']['by_stage']['judge']}, precision from "
        f"{pct(a['precision'])} to {pct(b['precision'])} and recall from {pct(a['recall'])} to {pct(b['recall'])}.",
        f"- **Two parallel generator passes were the largest gain in recall.** After E the generator returned "
        f"exactly its ten-flag cap on every lease, and most misses were liability waivers inside addenda. A single "
        f"pass with a 20-flag cap found them (recall {pct(co['recall'])}) but its generator took up to "
        f"{c20['slowest_generator_s']:.0f} seconds per lease and the whole pipeline up to "
        f"{c20['slowest_lease_s']:.0f}, against the report route's 150-second timeout, so it was "
        f"not shipped. Splitting each lease in half and reviewing the halves in parallel, ten flags each, raised "
        f"recall from {pct(eo['recall'])} to {pct(ro['recall'])} and F1 from {pct(eo['f1'])} to {pct(ro['f1'])}, "
        f"with precision going from {pct(eo['precision'])} to {pct(ro['precision'])}. The slowest generator pass "
        f"took {tp['slowest_generator_s']:.0f} seconds and the slowest lease "
        f"{tp['slowest_lease_s']:.0f} seconds end to end. Estimated cost went from "
        f"${o2['cost_per_lease']:.2f} to ${r['cost_per_lease']:.2f} per lease.",
        f"- **Remaining misses are the generator.** {sum(m['stage'] == 'generator' for m in misses)} of "
        f"{len(misses)} misses were in the lease text and never proposed, and each pass still returns its full ten "
        "candidates, so more passes or a larger per-pass budget is the next lever, with latency as the constraint.",
    ]


def render(res: dict) -> str:
    r, base, chat = res["."], res["baseline"], res["runs/chat_opus"]
    v, o, bo, co = r["versions"], r["scores"]["overall"], base["scores"]["overall"], chat["scores"]["overall"]
    cit, gr, mb = r["citation"], r["grounding"], r["misses"]
    models = {m["role"]: m["requested"] for m in v["models"]}
    cap = v["max_report_flags"]
    n_arguable = sum(len(x["rows"]) for x in load_ground_truth(GROUND_TRUTH_ARGUABLE).values())
    stage_desc = {
        "extraction": "Not in the parsed lease text, so the model never saw it",
        "generator": "In the text, but the generator did not propose it",
        "parse": "Proposed in a format the flag parser drops",
        "cap": f"Proposed after the first {cap} flags, so never judged",
        "judge": "Proposed, then rejected by the Sonnet judge",
        "dedup": "Approved, then removed as a duplicate",
        "merged": "Quoted inside a flag already matched to another clause",
    }
    lines = [
        "# Lease Screener eval",
        "",
        f"> **Flags {pct(o['recall'])} of risky clauses on a {o['leases']}-lease test set, with "
        f"{pct(o['false_alarm_rate'])} false alarms.** F1 {pct(o['f1'])} vs. {pct(co['f1'])} for Claude Opus 5 "
        f"given the same leases; {o['fp']} false alarms vs. {co['fp']}.",
        "",
        "## Head to head with Claude Opus 5 alone",
        "",
    ] + head_to_head(res) + [
        "",
        INTRO,
        "",
        REPRODUCE,
        "",
        "## Measured accuracy",
        "",
        f"Measured {v['run_at'][:10]} on {o['leases']} leases with {o['gt_clauses']} labeled risky clauses, before and "
        f"after the fixes described below. Before: recall {pct(bo['recall'])}, precision {pct(bo['precision'])}, "
        f"false-alarm rate {pct(bo['false_alarm_rate'])}. After: recall {pct(o['recall'])}, precision "
        f"{pct(o['precision'])}, false-alarm rate {pct(o['false_alarm_rate'])}.",
        "",
        "| Run | Set | Leases | Labeled clauses | Flags | Hits | Misses | False alarms | Precision | Recall | F1 | False-alarm rate |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ] + _score_rows("Before", base) + _score_rows("After", r) + [
        "",
        f"- **Citation accuracy:** {cit['correct']} of {cit['matched_flags_scored']} correctly flagged clauses also cite "
        f"the labeled statute ({pct(cit['accuracy_of_matched'])}). Flagged and correctly cited: {cit['correct']} of "
        f"{cit['gt_clauses_with_citation']} labeled clauses ({pct(cit['flagged_and_correctly_cited'])}).",
        f"- **Unscored flags:** {o['on_arguable']} flags landed on clauses labeled arguable (reasonable readers could "
        "disagree). They count as neither hits nor false alarms.",
        f"- **Recall ceiling:** the report shows at most {cap} flags per lease, so the best possible "
        f"recall on this set is {pct(r['recall_ceiling'])}.",
        f"- **Grounding:** {gr['flags_law_quote_found_in_generator_reference']} of {gr['flags_with_law_quote']} reported "
        f"flags quote law text that is actually in the law reference the model was given, and "
        f"{gr['gt_citations_present_in_generator_reference']} of {gr['gt_citations']} labeled statutes appear in that "
        "reference at all.",
        "",
        "### Where the misses happen",
        "",
        f"{mb['by_bucket']['retrieval']} of {o['fn']} misses never reached the model (the PDF parser lost the clause). "
        f"{mb['by_bucket']['judgment']} reached the model and were not flagged. By pipeline stage:",
        "",
        "| Stage | Misses | What happened |",
        "|---|---|---|",
    ] + [f"| {s} | {n} | {stage_desc[s]} |" for s, n in mb["by_stage"].items()] + [
        "",
        "## Run by run",
        "",
        f"Each row is one full run of the eval on the same {o['leases']} leases and {o['gt_clauses']} labeled clauses, "
        "all scored against the current answer key. Numbers come from `eval/baseline/results.json`, "
        "`eval/runs/*/results.json` and `eval/results.json`.",
        "",
        "| Run | Flags | Hits | False alarms | On arguable | Precision | Recall | F1 | False-alarm rate | Matched flags citing the labeled statute |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ] + [
        f"| {label} | {x['flags']} | {x['tp']} | {x['fp']} | {x['on_arguable']} | {pct(x['precision'])} | {pct(x['recall'])} "
        f"| {pct(x['f1'])} | {pct(x['false_alarm_rate'])} | {res[k]['citation']['correct']} of "
        f"{res[k]['citation']['matched_flags_scored']} |"
        for label, k in RUNS if k in res for x in [res[k]["scores"]["overall"]]
    ] + [
        "",
        FINDINGS,
    ] + dynamic_findings(res) + [
        "",
        LIMITS,
        "",
        "## Method",
        "",
        "- Each run passed the labeled leases through the app's own report pipeline offline, with database, payment "
        "and email code stubbed out, and saved every generator candidate, judge decision and reported flag to its "
        "results file. That harness imports the app and stays closed source with it; `score.py` scores the saved output.",
        "- The test leases are real signed leases and are not published. Their file names are replaced with DC-1, DC-2, "
        "VA-1 and VA-2 in every file here, and rent and deposit amounts in quoted text with placeholders such as "
        "`[rent]`; `python score.py --check` confirms the redaction changes no score.",
        f"- Ground truth is `eval/ground_truth.csv` ({o['gt_clauses']} clauses labeled clear) plus "
        f"`eval/ground_truth_arguable.csv` ({n_arguable} clauses, unscored). The labels were drafted by Claude from the "
        "lease text and the statute text only, with no access to the app. The clear labels were frozen before the "
        "pipeline ran; arguable labels were added afterward in a blind adjudication of flagged clauses. No lawyer has "
        "reviewed them yet; see `eval/LABELING.md` for the rubric, sources, the adjudication rounds and every reviewer "
        "change.",
        f"- A flag counts as a hit when its quoted clause matches a labeled excerpt (word-trigram containment, "
        f"threshold {v['match_threshold']}). Precision = hits / (hits + false alarms). Recall = hits / labeled "
        "clauses. False-alarm rate = false alarms / (hits + false alarms).",
        f"- Models: generator `{models.get('generator')}`, judge `{models.get('judge')}`. App commit "
        f"`{v['git_commit'][:10]}`. Full per-lease tables with clause text: `eval/results.md`.",
        "",
        "## Files",
        "",
        "| Path | What it is |",
        "|---|---|",
        "| `score.py` | Standalone scorer; checks and regenerates this README |",
        f"| `eval/ground_truth.csv` | The {o['gt_clauses']} clauses labeled clear; they define recall |",
        f"| `eval/ground_truth_arguable.csv` | {n_arguable} clauses reasonable readers could disagree on; flags on them are unscored |",
        "| `eval/LABELING.md` | Rubric, law sources, reviewer changes, both adjudication rounds |",
        "| `eval/adjudication.json` | Blind verdicts on false alarms no labeler had considered |",
        "| `eval/chat_baseline.py` | The control: each lease PDF sent to Opus 5 with one plain request |",
        "| `eval/results.json`, `eval/results.md` | The shipped configuration |",
        "| `eval/baseline/results.json` | The app as first measured |",
        "| `eval/runs/*/results.json` | Every other run above; `two_pass` is the shipped configuration's live run, used for its timings |",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify against every results file and README.md")
    ap.add_argument("--readme", action="store_true", help="rewrite README.md from the results files")
    args = ap.parse_args()

    res = score_all()
    print(f"{'Run':<58} {'flags':>5} {'hits':>4} {'FA':>3} {'miss':>4} {'P':>4} {'R':>4} {'F1':>4} {'FA rate':>7}")
    for label, k in RUNS:
        if k in res:
            o = res[k]["scores"]["overall"]
            print(f"{label[:58]:<58} {o['flags']:>5} {o['tp']:>4} {o['fp']:>3} {o['fn']:>4} {pct(o['precision']):>4} "
                  f"{pct(o['recall']):>4} {pct(o['f1']):>4} {pct(o['false_alarm_rate']):>7}")
    mb = res["."]["misses"]["by_stage"]
    print("\nShipped misses by stage: " + ", ".join(f"{s} {n}" for s, n in mb.items()))
    if not LABELS_JSON.is_file():
        print("False-alarm triage read as recorded (eval/labels.json not present).")

    if args.readme:
        README.write_text(render(res), "utf-8", newline="\n")
        print(f"wrote {README.name}")
    if not args.check:
        return 0

    failed = False
    for k, r in res.items():
        diffs = check_run(r)
        failed |= bool(diffs)
        rel = r["path"].relative_to(ROOT).as_posix()
        print(f"{'OK  ' if not diffs else 'DIFF'} {rel}" + "".join(f"\n       {d}" for d in diffs))
    current = README.read_text("utf-8").replace("\r\n", "\n") if README.is_file() else ""
    if current != render(res):
        failed = True
        print("DIFF README.md does not match the results files; run `python score.py --readme`")
    else:
        print("OK   README.md")
    print("\nAll numbers reproduced." if not failed else "\nDifferences found.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
