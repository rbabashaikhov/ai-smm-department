"""Drive one disposable local ideation/generation job with existing test fakes."""

from __future__ import annotations

import sys
from dataclasses import replace

from local_browser_safety import block_external_connections, local_settings

from ai_smm.generation_worker import GenerationWorker
from ai_smm.ideation_worker import IdeationWorker
from tests.test_generation_worker import FakeGraph, _good_result
from tests.test_ideation_worker import FakeLLM, _idea


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"ideation", "generation"}:
        raise SystemExit("usage: local_fake_workers.py ideation|generation")
    settings = local_settings()
    block_external_connections(settings)
    if sys.argv[1] == "ideation":
        worker = IdeationWorker(
            settings=replace(settings, ideation_worker_id="browser-fake-ideation"),
            llm_factory=lambda: FakeLLM({"ideas": [_idea(1), _idea(2), _idea(3)]}),
        )
    else:
        result = _good_result()
        result["draft"]["publications"] = result["draft"]["publications"][:1]
        result["content_plan"]["planned_total"] = 1
        worker = GenerationWorker(
            settings=replace(settings, generation_worker_id="browser-fake-generation"),
            graph_factory=lambda: FakeGraph(result),
            knowledge_loader=lambda project_id, **_options: {"project_id": project_id},
        )
    result = worker.process_one()
    print(result or "no-job")
    return 0 if result == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
