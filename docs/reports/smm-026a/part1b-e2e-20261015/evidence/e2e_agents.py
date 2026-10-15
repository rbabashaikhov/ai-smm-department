"""SMM-026A part 1b E2E (after the R1-R6 quality fixes), phase A: the real LangGraph pipeline with real LLM calls.

Runs Strategist -> Copywriter -> Editor (-> revision loop) -> Publisher
(dry run: publish_live=False, nothing leaves the process except the OpenAI
calls). Streams node updates so every Editor decision is kept, and records
tokens and wall time per node.

usage: e2e_agents.py <scenario: normal|revision> <out.json> [publishing_strategy]
"""
from __future__ import annotations

import json
import os
import sys
import time
from copy import deepcopy

for forbidden in ("THREADS_ACCESS_TOKEN", "STORAGE_SSH_HOST", "AI_SMM_DATABASE_URL", "DATABASE_URL"):
    assert not os.environ.get(forbidden), f"{forbidden} must not be set for the E2E run"

from langchain_core.callbacks import get_usage_metadata_callback

from ai_smm.graph import build_graph

import ai_smm.agents.copywriter as _copywriter_mod

# Instrumentation only: record what the Copywriter's length pass did.
SHORTENINGS: list[dict] = []
_shorten = _copywriter_mod.shorten_text


def _recording_shorten(text, **kw):
    out = _shorten(text, **kw)
    if out != text or len(text) > _copywriter_mod.MAX_ITEM_LENGTH:
        SHORTENINGS.append({"before": len(text), "after": len(out)})
    return out


_copywriter_mod.shorten_text = _recording_shorten
from ai_smm.knowledge.loader import load_project_knowledge

PRICE_PER_M = {"gpt-4.1-mini": (0.40, 0.10, 1.60)}  # input, cached input, output USD / 1M


def cost(usage: dict) -> float:
    total = 0.0
    for model, u in usage.items():
        key = next((k for k in PRICE_PER_M if model.startswith(k)), None)
        if key is None:
            continue
        p_in, p_cached, p_out = PRICE_PER_M[key]
        cached = (u.get("input_token_details") or {}).get("cache_read", 0) or 0
        total += (u["input_tokens"] - cached) * p_in / 1e6 + cached * p_cached / 1e6
        total += u["output_tokens"] * p_out / 1e6
    return total


def flat(usage: dict) -> dict:
    out = {"input": 0, "output": 0, "cached": 0}
    for u in usage.values():
        out["input"] += u.get("input_tokens", 0)
        out["output"] += u.get("output_tokens", 0)
        out["cached"] += (u.get("input_token_details") or {}).get("cache_read", 0) or 0
    return out


def main() -> int:
    scenario, out_path = sys.argv[1], sys.argv[2]
    knowledge = load_project_knowledge("ai-catalog-consultant")
    state = {
        "project_id": "ai-catalog-consultant",
        "knowledge": knowledge,
        "revision_count": 0,
        "max_revisions": int(os.environ.get("E2E_MAX_REVISIONS", "3")),
        "errors": [],
        "publish_live": False,
        "force_bad_draft": scenario == "revision",
    }
    if len(sys.argv) > 3:
        state["publishing_strategy"] = sys.argv[3]

    steps = []
    final: dict = {}
    t_start = time.monotonic()

    crash = None
    with get_usage_metadata_callback() as cb:
        prev = {}
        sh_prev = 0
        t_prev = time.monotonic()
        stream = build_graph().stream(state, stream_mode="updates")
        while True:
            try:
                update = next(stream)
            except StopIteration:
                break
            except Exception as exc:  # keep what happened before the crash
                crash = f"{type(exc).__name__}: {exc}"
                break
            now = time.monotonic()
            usage_now = deepcopy(cb.usage_metadata)
            delta = {}
            for model, u in usage_now.items():
                p = prev.get(model, {})
                delta[model] = {
                    "input_tokens": u["input_tokens"] - p.get("input_tokens", 0),
                    "output_tokens": u["output_tokens"] - p.get("output_tokens", 0),
                    "input_token_details": {
                        "cache_read": (u.get("input_token_details") or {}).get("cache_read", 0)
                        - (p.get("input_token_details") or {}).get("cache_read", 0)
                    },
                }
            prev = usage_now
            for node, output in update.items():
                steps.append(
                    {
                        "node": node,
                        "seconds": round(now - t_prev, 2),
                        "models": sorted(k for k, v in delta.items() if v["input_tokens"]),
                        "tokens": flat(delta),
                        "cost_usd": round(cost(delta), 6),
                        "shortenings": SHORTENINGS[sh_prev:] if node == "copywriter" else [],
                        "output": output,
                    }
                )
                if node == "copywriter":
                    sh_prev = len(SHORTENINGS)
                final.update(output or {})
            t_prev = now
        total_usage = deepcopy(cb.usage_metadata)

    report = {
        "scenario": scenario,
        "shortenings": SHORTENINGS,
        "requested_strategy": state.get("publishing_strategy", "(agent chooses)"),
        "seconds_total": round(time.monotonic() - t_start, 2),
        "usage_by_model": total_usage,
        "tokens_total": flat(total_usage),
        "cost_usd_total": round(cost(total_usage), 6),
        "crash": crash,
        "steps": steps,
        "final": {k: v for k, v in final.items() if k != "knowledge"},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(json.dumps({k: report[k] for k in ("crash", "scenario", "seconds_total", "tokens_total", "cost_usd_total")}))
    for s in steps:
        o = s["output"] or {}
        extra = ""
        if s["node"] == "editor":
            extra = f" approved={o.get('editor_approved')} score={o.get('editor_score')} narrative={o.get('editor_narrative_score')} issues={len(o.get('editor_issues') or [])}"
        if s["node"] == "copywriter":
            o_d = (o.get("draft") or {}).get("publications") or []
            extra = f" lens={[len(i['text']) for p in o_d for i in p['items']]} shortened={s['shortenings']}"
        if s["node"] == "publisher":
            extra = f" status={(o.get('publication') or {}).get('status')}"
        print(f"  {s['node']:<20} {s['seconds']:>6}s tok={s['tokens']} ${s['cost_usd']}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
