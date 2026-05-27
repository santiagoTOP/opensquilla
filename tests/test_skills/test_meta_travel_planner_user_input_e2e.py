"""End-to-end test: meta-travel-planner with user_input step (PR8 reference).

Validates the complete user_input lifecycle against a real bundled
meta-skill:

  trigger → pause at trip_collect → reply with form values → resume →
  downstream `trip_preferences` step sees inputs.collected.trip_collect.*

This is the canonical proof that PR1+PR2+PR3+PR4+PR8 work together as a
single feature.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.meta_run_writer import MetaRunWriter
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.plan_serde import to_jsonable
from opensquilla.skills.meta.types import MetaMatch, MetaResult


@pytest.fixture
def writer(tmp_path: Path) -> MetaRunWriter:
    db = tmp_path / "test.sqlite"
    backend = get_backend(f"sqlite:///{db}")
    backend.apply_migrations(read_migrations("migrations"))
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return MetaRunWriter(conn)


def _load_travel_planner_plan():
    bundled = Path("src/opensquilla/skills/bundled").resolve()
    loader = SkillLoader(bundled_dir=bundled)
    specs = [s for s in loader.load_all() if getattr(s, "kind", "") == "meta"]
    for s in specs:
        if s.name == "meta-travel-planner":
            plan = parse_meta_plan(s)
            assert plan is not None
            return plan
    raise AssertionError("meta-travel-planner not found in bundled")


async def _sv(*_a):
    return
    yield  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_travel_planner_pauses_at_trip_collect_then_resumes(writer):
    """The real bundled meta-travel-planner DAG:
      - pauses at the new trip_collect user_input step,
      - resumes when 4 form values are submitted,
      - downstream trip_preferences step sees inputs.collected.trip_collect.*
    """
    plan = _load_travel_planner_plan()

    # Sanity: confirm trip_collect is first and is kind=user_input.
    assert plan.steps[0].id == "trip_collect"
    assert plan.steps[0].kind == "user_input"

    inputs = {"user_message": "plan a 5-day trip to Tokyo", "collected": {}}
    snapshot_json = json.dumps(to_jsonable(plan), sort_keys=True, ensure_ascii=False)
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", plan.name, "d", snapshot_json, "soft_meta_invoke",
             "S1", "running", 0, json.dumps(inputs)),
        )
        writer._conn.commit()

    orch = MetaOrchestrator(
        agent_runner=None, skill_loader=None, dao=writer,  # type: ignore[arg-type]
    )

    # Capture what trip_preferences (the second step, llm_chat) receives.
    observed: dict[str, dict] = {}

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        # Capture the first downstream step's view of inputs.collected.
        if step.id == "trip_preferences":
            observed["trip_preferences"] = {
                "inputs_user_message": match_inputs.get("user_message"),
                "inputs_collected": match_inputs.get("collected"),
                "rendered_task": step.with_args.get("task", "")[:600],
                "outputs_trip_collect": outputs.get("trip_collect", ""),
            }
            yield _StepDone(text="contract-stub", status="ok")
        else:
            # All later steps are stubbed — we only need to prove the
            # collected fields reach the FIRST downstream step.
            yield _StepDone(text=f"{step.id}-stub", status="ok")

    # 1) First call should pause at trip_collect.
    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1", session_id="S1",
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )
    assert isinstance(paused, MetaResult)
    assert paused.paused is True
    assert paused.paused_payload is not None
    assert paused.paused_payload.step_id == "trip_collect"
    # The form has the 4 fields we expect.
    field_names = {f.name for f in paused.paused_payload.schema.fields}
    assert field_names == {"destination", "days", "party_size", "budget"}

    # 2) Resume with the form values.
    result = await orch.resume(
        run_id="r1", session_id="S1",
        filled_fields={
            "destination": "Tokyo",
            "days": 5,
            "party_size": 2,
            "budget": "mid",
        },
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )
    assert result.paused is False
    assert result.ok is True

    # 3) Verify trip_preferences saw the collected fields.
    tp = observed["trip_preferences"]
    assert tp["inputs_user_message"] == "plan a 5-day trip to Tokyo"
    collect = tp["inputs_collected"]["trip_collect"]
    assert collect["destination"] == "Tokyo"
    assert collect["days"] == 5
    assert collect["party_size"] == 2
    assert collect["budget"] == "mid"
    # outputs.trip_collect is the markdown summary.
    assert "destination: Tokyo (from user)" in tp["outputs_trip_collect"]
    assert "days: 5 (from user)" in tp["outputs_trip_collect"]

    # 4) Run is finalized to 'ok'.
    with writer._lock:
        row = writer._conn.execute(
            "SELECT status, ended_at_ms FROM meta_skill_runs WHERE run_id='r1'",
        ).fetchone()
    assert row["status"] == "ok"
    assert row["ended_at_ms"] is not None and row["ended_at_ms"] > 0


@pytest.mark.asyncio
async def test_travel_planner_cancel_keyword_terminates_run(writer):
    """User can bail out by replying with one of the cancel_keywords."""
    plan = _load_travel_planner_plan()
    inputs = {"user_message": "plan a trip", "collected": {}}
    snapshot_json = json.dumps(to_jsonable(plan), sort_keys=True, ensure_ascii=False)
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r2", plan.name, "d", snapshot_json, "soft_meta_invoke",
             "S2", "running", 0, json.dumps(inputs)),
        )
        writer._conn.commit()

    orch = MetaOrchestrator(
        agent_runner=None, skill_loader=None, dao=writer,  # type: ignore[arg-type]
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r2", session_id="S2",
            ):
                yield ev
            return
        yield _StepDone(text=f"{step.id}-stub", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r2", session_id="S2",
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )
    assert paused.paused is True

    # Simulate cancel by directly calling mark_cancelled with one of the
    # configured keywords' reason (meta_resolution would do this on a
    # real "取消" reply, but we don't have the full TurnRunner here).
    writer.mark_cancelled(run_id="r2", reason="user_cancel:取消")
    with writer._lock:
        row = writer._conn.execute(
            "SELECT status FROM meta_skill_runs WHERE run_id='r2'",
        ).fetchone()
    assert row["status"] == "cancelled"
