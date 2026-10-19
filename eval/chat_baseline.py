"""
Control experiment: the model alone, without the app.

For each labeled lease, send the PDF straight to the model with one plain request,
the way a tenant would in a chat window: no statute reference, no checklist, no
judge, no flag cap. The answer is returned through a small JSON schema so the
flags can be scored; the schema fixes the shape of the answer, not its content.

Scored with the same ground truth, matcher and metrics as run_eval.py. Requests
are cached in eval/cache/llm/ like every other model call in the eval.

Published as the record of the exact request behind eval/runs/chat_opus/. It
imports run_eval.py, the private harness that runs the app, and needs the lease
PDFs, so it does not run from this repo; score.py rescores its saved output.

Usage, from the repo root:
    .venv/bin/python eval/chat_baseline.py                     # claude-opus-5, results in eval/runs/chat_opus/
    .venv/bin/python eval/chat_baseline.py --model claude-sonnet-5 --tag chat_sonnet
    .venv/bin/python eval/chat_baseline.py --offline           # cache only
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval as E  # noqa: E402

PLACES = {"DC": "Washington, DC", "VA": "Virginia"}
PROMPT = (
    "This is my apartment lease in {place}. Which clauses in it conflict with {place} landlord-tenant law? "
    "For each one, quote the clause exactly as it is written in the lease and cite the specific statute or "
    "regulation section it conflicts with."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string", "description": "The clause, quoted exactly from the lease"},
                    "citation": {"type": "string", "description": "Statute or regulation section it conflicts with"},
                    "issue": {"type": "string", "description": "One sentence on the conflict"},
                },
                "required": ["clause", "citation", "issue"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["flags"],
    "additionalProperties": False,
}


def ask(client, model: str, lease_file: str, jurisdiction: str, offline: bool) -> dict:
    """One chat-style request per lease, cached under a hash of the full request."""
    pdf = (E.LEASE_DIR / lease_file).read_bytes()
    request = {
        "model": model,
        "max_tokens": 64000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf).decode("ascii"),
                }},
                {"type": "text", "text": PROMPT.format(place=PLACES[jurisdiction])},
            ],
        }],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()
    path = E.LLM_CACHE / f"{key}.json"
    if path.exists():
        return {**json.loads(path.read_text("utf-8")), "source": "cache"}
    if offline:
        raise RuntimeError(f"cache miss for {lease_file} in --offline mode")
    with client.messages.stream(**request) as stream:
        resp = stream.get_final_message()
    rec = {
        "role": "chat_baseline",
        "requested_model": model,
        "model": resp.model,
        "text": next((b.text for b in resp.content if b.type == "text"), ""),
        "stop_reason": resp.stop_reason,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    E.LLM_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), "utf-8")
    return {**rec, "source": "api"}


def to_run(lease_file: str, jurisdiction: str, answer: dict) -> dict:
    """Shape the answer like a run_eval pipeline run so evaluate_lease can score it.
    Every flag counts as reported: there is no judge and no cap."""
    from services.lease_parser import load_from_bytes

    flags = json.loads(answer["text"])["flags"] if answer["stop_reason"] != "refusal" and answer["text"] else []
    candidates = []
    for i, f in enumerate(flags):
        candidates.append({
            "index": i, "judged": True, "verdict": "no judge", "approved": True, "in_report": True,
            "block": json.dumps(f, ensure_ascii=False), "severity": None, "title": f["issue"],
            "clause": f["clause"], "law": "", "source": f["citation"], "why": f["issue"],
            "match_text": f["clause"],
        })
    path = E.LEASE_DIR / lease_file
    return {
        "lease_file": lease_file,
        "jurisdiction": jurisdiction,
        # The model read the PDF itself; the app's parsed text is kept only so a miss
        # can be checked against the lease text the same way run_eval.py does.
        "text": load_from_bytes(path.read_bytes(), path.name),
        "generator_raw": answer["text"],
        "stop_reason": answer["stop_reason"],
        "served_model": answer["model"],
        "usage": answer["usage"],
        "source": answer["source"],
        "candidates": candidates,
    }


def write_markdown(results: dict, path: Path) -> None:
    s, v, cit = results["scores"], results["versions"], results["citation"]
    lines = [
        f"# Control: {v['model']} alone, lease PDF uploaded",
        "",
        f"Run {v['run_at'][:10]}. Each lease PDF was sent to `{v['model']}` with this request and nothing else "
        "(no statute reference, no checklist, no judge, no flag cap):",
        "",
        f"> {PROMPT.format(place='Washington, DC')}",
        "",
        "Answers were returned through a JSON schema of {clause, citation, issue}. Scored with "
        "eval/ground_truth.csv and the matcher in eval/run_eval.py. Generated by eval/chat_baseline.py; "
        "do not edit by hand.",
        "",
        "| Set | Leases | Labeled clauses | Flags | Hits | Misses | False alarms | On arguable | Precision | Recall | F1 | False-alarm rate |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, sc in [("Overall", s["overall"])] + sorted(s["by_jurisdiction"].items()):
        lines.append(
            f"| {name} | {sc['leases']} | {sc['gt_clauses']} | {sc['flags']} | {sc['tp']} | {sc['fn']} | {sc['fp']} "
            f"| {sc['on_arguable']} | {E._pct(sc['precision'])} | {E._pct(sc['recall'])} | {E._pct(sc['f1'])} "
            f"| {E._pct(sc['false_alarm_rate'])} |"
        )
    lines += [
        "",
        f"Citation accuracy: {cit['correct']} of {cit['matched_flags_scored']} matched flags cite the labeled statute "
        f"({E._pct(cit['accuracy_of_matched'])}).",
        "",
        f"Tokens: {v['input_tokens']:,} input, {v['output_tokens']:,} output across {s['overall']['leases']} requests "
        f"({v['api_calls']} from the API this run, {v['cached_calls']} from cache).",
    ]
    for e, run in zip(results["leases"], results["pipeline_runs"]):
        lines += ["", f"## {e['lease_file']} ({e['jurisdiction']})", "",
                  f"{e['flags']} flags, stop reason `{run['stop_reason']}`. Hits {e['tp']}, misses {e['fn']}, "
                  f"false alarms {e['fp']}, on arguable clauses {e['on_arguable']}."]
        for title, rows in (
            ("Hits", [(h["gt"]["clause_excerpt"], h["flag"]["clause"], h["flag"]["source"]) for h in e["hits"]]),
            ("False alarms", [("", f["flag"]["clause"], f["flag"]["source"]) for f in e["false_alarms"]]),
            ("Flags on arguable clauses (not scored)",
             [(a["arguable"]["clause_excerpt"], a["flag"]["clause"], a["flag"]["source"]) for a in e["flags_on_arguable_clauses"]]),
        ):
            if rows:
                lines += ["", f"**{title}**", "", "| Labeled clause | Model's quote | Model cites |", "|---|---|---|"]
                lines += [f"| {E._cell(g)} | {E._cell(q)} | {E._cell(c, 120)} |" for g, q, c in rows]
        if e["misses"]:
            lines += ["", "**Misses**", "", "| CSV line | Labeled clause | Issue |", "|---|---|---|"]
            lines += [f"| {m['gt']['line']} | {E._cell(m['gt']['clause_excerpt'])} | {E._cell(m['gt']['issue'], 120)} |"
                      for m in e["misses"]]
    path.write_text("\n".join(lines) + "\n", "utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--tag", default="chat_opus")
    parser.add_argument("--threshold", type=float, default=E.DEFAULT_THRESHOLD)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    from dotenv import load_dotenv
    import anthropic

    load_dotenv(E.REPO / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY") and not args.offline:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env, or run with --offline.")
    client = anthropic.Anthropic() if not args.offline else None

    gt = E.load_ground_truth()
    arguable = E.load_ground_truth(E.GROUND_TRUTH_ARGUABLE, required=False)
    labeled = {k: v for k, v in gt.items() if v["rows"] or v["none"]}

    def one(item):
        lease_file, lease = item
        print(f"{lease_file}: asking {args.model} ...", flush=True)
        return to_run(lease_file, lease["jurisdiction"], ask(client, args.model, lease_file, lease["jurisdiction"], args.offline))

    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(one, labeled.items()))

    evals = []
    for run in runs:
        lease = labeled[run["lease_file"]]
        e = E.evaluate_lease(run, lease, arguable.get(run["lease_file"], {}).get("rows", []), args.threshold)
        evals.append(e)
        print(f"  {run['lease_file']}: stop {run['stop_reason']}, {e['flags']} flags, {e['tp']} hits, "
              f"{e['fn']} misses, {e['fp']} false alarms, {e['on_arguable']} on arguable clauses")

    out_dir = E.EVAL_DIR / "runs" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "versions": {
            "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "experiment": "model alone: lease PDF uploaded with one plain request, no app pipeline",
            "model": args.model,
            "served_models": sorted({r["served_model"] for r in runs}),
            "prompt": PROMPT,
            "output_schema": SCHEMA,
            "match_threshold": args.threshold,
            "input_tokens": sum(r["usage"]["input_tokens"] for r in runs),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in runs),
            "api_calls": sum(r["source"] == "api" for r in runs),
            "cached_calls": sum(r["source"] == "cache" for r in runs),
            "git_commit": E._git("rev-parse", "HEAD"),
            "estimated_cost": E.estimated_cost(
                {("chat", r["served_model"], k): sum(x["usage"][f"{k}_tokens"] for x in runs if x["served_model"] == r["served_model"])
                 for r in runs for k in ("input", "output")},
                len(runs),
            ),
        },
        "scores": {
            "overall": E.scores(evals),
            "by_jurisdiction": {
                j: E.scores([e for e in evals if e["jurisdiction"] == j]) for j in sorted({e["jurisdiction"] for e in evals})
            },
        },
        "citation": E.citation_scores(evals),
        "misses": E.miss_breakdown(evals),
        "false_alarm_triage": E.false_alarm_triage(evals, args.threshold),
        "leases": evals,
        "pipeline_runs": [{k: v for k, v in r.items() if k != "text"} for r in runs],
    }
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", "utf-8")
    write_markdown(results, out_dir / "results.md")
    o = results["scores"]["overall"]
    print(f"\nprecision {E._pct(o['precision'])}, recall {E._pct(o['recall'])}, F1 {E._pct(o['f1'])}, "
          f"false alarms {E._pct(o['false_alarm_rate'])}; {results['versions']['input_tokens']:,} input and "
          f"{results['versions']['output_tokens']:,} output tokens")
    print(f"wrote {out_dir / 'results.json'} and {out_dir / 'results.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
