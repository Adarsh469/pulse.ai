"""
server.py — FastAPI server for ClinicalTriage-Env
===================================================
Exposes the OpenEnv endpoints required by the competition spec:
  POST /reset  → initialise episode, return observation
  POST /step   → process one action, return observation + reward
  GET  /state  → return current internal state
  GET  /health → liveness probe for Hugging Face Spaces

Compatible with Hugging Face Spaces on port 7860.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from env import ClinicalTriageEnv, TriageAction, ResetResult, StepResult

FRONTEND_DIR = Path(__file__).parent / "frontend"

app = FastAPI(
    title="ClinicalTriage-Env API",
    description=(
        "Emergency Department triage simulation environment. "
        "Implements the OpenEnv step()/reset()/state() interface."
    ),
    version="1.0.0",
)

# Allow cross-origin requests (required for HF Spaces)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory session store (single-process, single-user)
# ---------------------------------------------------------------------------

_sessions: Dict[str, ClinicalTriageEnv] = {}
_DEFAULT_TASK = os.getenv("DEFAULT_TASK", "task1_esi_assignment")


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------

class ResetRequest(BaseModel):
    task_id: Optional[str] = None
    seed: Optional[int] = None


class StepRequest(BaseModel):
    action_type: str
    esi_level: Optional[int] = None
    queue_order: Optional[list] = None
    question_topic: Optional[str] = None
    session_id: Optional[str] = "default"


class ResetRequestWithSession(ResetRequest):
    session_id: Optional[str] = "default"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe for Hugging Face Spaces."""
    return {"status": "ok", "env": "clinical-triage-env", "version": "1.0.0"}


@app.post("/reset")
def reset(request: ResetRequestWithSession = None) -> Dict[str, Any]:
    """
    Reset the environment.
    Body: { "task_id": "task1_esi_assignment" | "task2_queue_priority" | "task3_ambiguous_triage",
             "seed": <int|null>, "session_id": "default" }
    """
    if request is None:
        request = ResetRequestWithSession()

    task_id = request.task_id or _DEFAULT_TASK
    session_id = request.session_id or "default"

    valid_tasks = {
        "task1_esi_assignment",
        "task2_queue_priority",
        "task3_ambiguous_triage",
    }
    if task_id not in valid_tasks:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown task_id '{task_id}'. Valid: {sorted(valid_tasks)}",
        )

    env = ClinicalTriageEnv(task_id=task_id, seed=request.seed)
    _sessions[session_id] = env

    result: ResetResult = env.reset()
    return result.model_dump()


@app.post("/step")
def step(request: StepRequest) -> Dict[str, Any]:
    """
    Take one environment step.
    Body: { "action_type": "...", "esi_level": 3, "session_id": "default" }
    """
    session_id = request.session_id or "default"
    env = _sessions.get(session_id)
    if env is None:
        raise HTTPException(
            status_code=400,
            detail=f"No active session '{session_id}'. Call /reset first.",
        )

    try:
        action = TriageAction(
            action_type=request.action_type,
            esi_level=request.esi_level,
            queue_order=request.queue_order,
            question_topic=request.question_topic,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        result: StepResult = env.step(action)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Merge info dict into the response so the frontend can use true_esi, etc.
    data = result.model_dump()
    data["info"] = result.info
    return data


@app.get("/state")
def state(session_id: str = "default") -> Dict[str, Any]:
    """Return current internal environment state."""
    env = _sessions.get(session_id)
    if env is None:
        raise HTTPException(
            status_code=400,
            detail=f"No active session '{session_id}'. Call /reset first.",
        )
    return env.state()


@app.get("/tasks")
def list_tasks() -> Dict[str, Any]:
    """List available tasks."""
    return {
        "tasks": [
            {
                "id": "task1_esi_assignment",
                "name": "Single Patient ESI Assignment",
                "difficulty": "easy",
                "description": "Assign ESI level (1-5) to a single patient.",
            },
            {
                "id": "task2_queue_priority",
                "name": "Patient Queue Prioritization",
                "difficulty": "medium",
                "description": "Order 5 patients from most to least urgent.",
            },
            {
                "id": "task3_ambiguous_triage",
                "name": "Ambiguous Triage with Hidden History",
                "difficulty": "hard",
                "description": (
                    "Uncover hidden medications/allergies via ask_question, "
                    "then assign ESI. Contraindication bonus available."
                ),
            },
        ]
    }



# ---------------------------------------------------------------------------
# Clinical explanation endpoint
# ---------------------------------------------------------------------------

ESI_CLINICAL_GUIDE = {
    1: {"label": "Immediate (Resuscitation)", "color": "#ff2d55",
        "description": "Life-threatening — requires immediate physician intervention.",
        "indicators": ["cardiac arrest", "respiratory failure", "active hemorrhage"]},
    2: {"label": "Emergent (High Risk)", "color": "#ff6b00",
        "description": "High-risk, should NOT wait. Rapid evaluation needed within minutes.",
        "indicators": ["severe chest pain", "suspected stroke", "high-risk medications"]},
    3: {"label": "Urgent (Needs Resources)", "color": "#ffd60a",
        "description": "Stable but requires multiple diagnostic resources (labs, imaging).",
        "indicators": ["moderate pain", "fever with complex symptoms"]},
    4: {"label": "Less Urgent", "color": "#30d158",
        "description": "Stable. Requires one resource. Can be seen in fast-track.",
        "indicators": ["minor injury", "simple prescription"]},
    5: {"label": "Non-Urgent", "color": "#636366",
        "description": "No acute distress. Could be managed in primary care.",
        "indicators": ["routine check", "cold symptoms"]},
}

_CRITICAL_SYMS = {"chest pain", "shortness of breath", "blurred vision", "altered mental status", "active hemorrhage"}
_HIGH_SYMS = {"severe headache", "syncope", "difficulty breathing", "severe pain"}


@app.get("/explain")
def explain(session_id: str = "default") -> Dict[str, Any]:
    """Generate rich clinical explanation for the completed episode."""
    env = _sessions.get(session_id)
    if env is None:
        raise HTTPException(status_code=400, detail=f"No session '{session_id}'.")

    result: Dict[str, Any] = {"task_id": env.task_id}

    if env.task_id == "task1_esi_assignment" and env._current_case:
        case = env._current_case
        true_esi = case.true_esi
        guide = ESI_CLINICAL_GUIDE[true_esi]
        reasoning = [
            f"Patient: {case.age}yo {case.gender} — {', '.join(case.symptoms)}.",
            f"Risk level: {case.risk_level.upper()} | Red flags: {', '.join(case.red_flags) if case.red_flags else 'none detected'}.",
        ]
        for rf in case.red_flags:
            if rf in _CRITICAL_SYMS:
                reasoning.append(f"⚠ '{rf}' is a critical red flag → pushes toward ESI 1 or 2.")
        reasoning.append(f"Correct ESI = {true_esi} ({guide['label']}): {guide['description']}")
        result.update({"true_esi": true_esi, "esi_label": guide["label"], "esi_color": guide["color"],
                       "reasoning": reasoning, "red_flags": case.red_flags})

    elif env.task_id == "task2_queue_priority" and env._queue_cases:
        true_sorted = sorted(env._queue_cases, key=lambda c: c.true_esi)
        correct_order, reasoning = [], ["Patients must be sorted by ESI (1 = most urgent first)."]
        for i, c in enumerate(true_sorted, 1):
            correct_order.append({
                "rank": i, "case_id": c.case_id, "age": c.age, "gender": c.gender,
                "symptoms": c.symptoms, "true_esi": c.true_esi,
                "esi_label": ESI_CLINICAL_GUIDE[c.true_esi]["label"],
                "esi_color": ESI_CLINICAL_GUIDE[c.true_esi]["color"],
                "reasoning": f"ESI {c.true_esi}: risk={c.risk_level}" + (f", red flags: {', '.join(c.red_flags)}" if c.red_flags else ", no critical flags"),
            })
            reasoning.append(f"#{i} {c.case_id} → ESI {c.true_esi} ({ESI_CLINICAL_GUIDE[c.true_esi]['label']})")
        result["correct_order"] = correct_order
        result["reasoning"] = reasoning

    elif env.task_id == "task3_ambiguous_triage" and env._current_case and env._hidden_history:
        case = env._current_case
        hidden = env._hidden_history
        true_esi = case.true_esi
        guide = ESI_CLINICAL_GUIDE[true_esi]
        reasoning = [
            f"Patient: {case.age}yo {case.gender} — {', '.join(case.symptoms)}.",
            f"Hidden medications: {', '.join(case.hidden_medications)}.",
            f"Key contraindication: {hidden.get('contraindication', 'none')}.",
            hidden.get("contraindication_summary", ""),
            f"Correct ESI = {true_esi} ({guide['label']}).",
            "✓ Contraindication correctly identified — bonus awarded." if env._contraindication_identified
            else "✗ Contraindication missed — ask about medications first to maximise score.",
        ]
        result.update({
            "true_esi": true_esi, "esi_label": guide["label"], "esi_color": guide["color"],
            "contraindication_identified": env._contraindication_identified,
            "hidden_medications": case.hidden_medications, "hidden_allergies": case.hidden_allergies,
            "contraindication": hidden.get("contraindication"),
            "contraindication_summary": hidden.get("contraindication_summary", ""),
            "revealed_topics": list(env._revealed_topics), "reasoning": reasoning,
        })

    return result


# ---------------------------------------------------------------------------
# Learning from users
# ---------------------------------------------------------------------------

import json
import logging
import time as _time

# Configure structured logging for production
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Persist feedback inside the Docker named volume (feedback_data:/app/feedback_data)
FEEDBACK_DATA_DIR = Path(__file__).parent / "feedback_data"
FEEDBACK_DATA_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_FILE = FEEDBACK_DATA_DIR / "feedback_log.jsonl"


class FeedbackRequest(BaseModel):
    case_id: str
    task_id: str
    symptoms: list
    human_esi: Optional[int] = None
    true_esi: Optional[int] = None
    reward: Optional[float] = None
    session_id: Optional[str] = "default"


@app.post("/feedback")
def submit_feedback(req: FeedbackRequest) -> Dict[str, Any]:
    """Store a human triage decision for agent learning."""
    record = {
        "ts": _time.time(),
        "case_id": req.case_id,
        "task_id": req.task_id,
        "symptoms": req.symptoms,
        "human_esi": req.human_esi,
        "true_esi": req.true_esi,
        "reward": req.reward,
    }
    with open(FEEDBACK_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    return {"status": "recorded", "total": _count_feedback()}


def _count_feedback() -> int:
    if not FEEDBACK_FILE.exists():
        return 0
    with open(FEEDBACK_FILE) as f:
        return sum(1 for _ in f)


@app.get("/learned_heuristics")
def learned_heuristics() -> Dict[str, Any]:
    """
    Aggregate human decisions into symptom-level ESI heuristics.
    Returns: { symptom: { avg_esi, count, correct_rate } }
    """
    if not FEEDBACK_FILE.exists():
        return {"heuristics": {}, "total_feedback": 0}

    from collections import defaultdict
    sym_data: Dict[str, list] = defaultdict(list)
    total = 0

    with open(FEEDBACK_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if rec.get("human_esi") and rec.get("task_id") == "task1_esi_assignment":
                for sym in rec.get("symptoms", []):
                    sym_data[sym].append({
                        "esi": rec["human_esi"],
                        "correct": rec.get("reward", 0) >= 0.8,
                    })

    heuristics = {}
    for sym, entries in sym_data.items():
        avg_esi = sum(e["esi"] for e in entries) / len(entries)
        correct_rate = sum(1 for e in entries if e["correct"]) / len(entries)
        heuristics[sym] = {
            "avg_esi": round(avg_esi, 2),
            "count": len(entries),
            "correct_rate": round(correct_rate, 2),
        }

    return {"heuristics": heuristics, "total_feedback": total}


@app.get("/feedback/stats")
def feedback_stats() -> Dict[str, Any]:
    """Summary stats for the learning dashboard."""
    total = _count_feedback()
    if total == 0:
        return {"total": 0, "avg_reward": None, "tasks": {}}

    from collections import defaultdict
    tasks: Dict[str, list] = defaultdict(list)
    rewards = []

    if FEEDBACK_FILE.exists():
        with open(FEEDBACK_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("reward") is not None:
                    rewards.append(rec["reward"])
                    tasks[rec.get("task_id", "unknown")].append(rec["reward"])

    return {
        "total": total,
        "avg_reward": round(sum(rewards) / len(rewards), 3) if rewards else None,
        "tasks": {k: {"count": len(v), "avg_reward": round(sum(v) / len(v), 3)} for k, v in tasks.items()},
    }



@app.get("/", include_in_schema=False)
def serve_index():
    """Serve the frontend dashboard."""
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"status": "ok", "message": "ClinicalTriage-Env API — frontend not found"})

# Mount static assets (CSS, JS, images) under /static
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

