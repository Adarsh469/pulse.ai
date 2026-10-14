"""
ClinicalTriage-Env — env.py
============================
OpenEnv-compliant Emergency Department triage simulation environment.

Tasks:
  task1_esi_assignment  (Easy)   - Assign ESI to a single patient
  task2_queue_priority  (Medium) - Rank 5 patients by urgency
  task3_ambiguous_triage (Hard)  - Uncover hidden history, then assign ESI

ESI Scale:
  1 = Immediate (Resuscitation)
  2 = Emergent  (High risk, should not wait)
  3 = Urgent    (Stable, multiple resources needed)
  4 = Less Urgent (Stable, one resource)
  5 = Non-Urgent  (Routine, no resources)
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATASET_PATH = Path(__file__).parent / "dataset" / "medical_triage_500.jsonl"

# Map dataset urgency_category + risk_level → ESI
def _compute_esi(urgency_category: str, risk_level: str, red_flags: List[str]) -> int:
    """Deterministic ESI from dataset fields.

    Mapping rationale (aligned with ESI v4 guidelines):
      immediate + high_risk_flags → ESI 1 (life-threatening)
      immediate (no flags, high)  → ESI 2 (emergent)
      urgent    + red_flags       → ESI 2 (elevated urgent)
      urgent    (no flags)        → ESI 3
      routine   + medium          → ESI 4
      routine   + low             → ESI 5
    """
    critical_flags = {"chest pain", "shortness of breath", "blurred vision"}
    has_critical = bool(set(red_flags) & critical_flags)

    if urgency_category == "immediate":
        if has_critical or risk_level == "high":
            return 1
        return 2
    elif urgency_category == "urgent":
        if has_critical:
            return 2
        if risk_level == "high":
            return 3
        return 3  # medium/low urgent → ESI 3
    else:  # routine
        if risk_level == "medium":
            return 4
        return 5


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class PatientSummary(BaseModel):
    """Public patient information given to the agent."""
    case_id: str
    age: int
    gender: str
    symptoms: List[str]
    symptom_count: int
    duration: str
    onset: str
    context: str
    # red_flags intentionally excluded for Task 3 until revealed


class FullPatientCase(BaseModel):
    """Full internal case (includes hidden fields)."""
    case_id: str
    age: int
    gender: str
    symptoms: List[str]
    symptom_count: int
    duration: str
    onset: str
    context: str
    risk_level: str
    red_flags: List[str]
    true_esi: int
    # Task 3 hidden fields
    hidden_medications: List[str] = Field(default_factory=list)
    hidden_allergies: List[str] = Field(default_factory=list)
    hidden_contraindication: Optional[str] = None


class TriageObservation(BaseModel):
    """Observation returned to the agent after reset() or step()."""
    task_id: str
    step: int
    patient: Optional[PatientSummary] = None          # Task 1 & 3
    queue: Optional[List[PatientSummary]] = None       # Task 2
    message: str = ""                                  # Free-text hint or clarification response
    clarification_budget: Optional[int] = None        # Remaining ask_question calls (Task 3)
    awaiting_final_esi: Optional[bool] = None         # Task 3: ready for final assignment


class TriageAction(BaseModel):
    """Action sent by the agent."""
    action_type: Literal["assign_esi", "reorder_queue", "ask_question"]
    esi_level: Optional[int] = Field(default=None, ge=1, le=5)       # assign_esi
    queue_order: Optional[List[str]] = None                            # reorder_queue: list of case_ids
    question_topic: Optional[Literal[
        "medications", "allergies", "past_medical_history", "current_symptoms"
    ]] = None                                                          # ask_question


class TriageReward(BaseModel):
    """Reward breakdown."""
    value: float = Field(ge=0.0, le=1.3)
    exact_match: bool = False
    partial_credit: bool = False
    contraindication_bonus: bool = False
    kendall_tau: Optional[float] = None
    details: str = ""


class StepResult(BaseModel):
    """Full result of env.step()."""
    observation: TriageObservation
    reward: float
    done: bool
    info: Dict[str, Any] = Field(default_factory=dict)


class ResetResult(BaseModel):
    """Return type of env.reset()."""
    observation: TriageObservation


# ---------------------------------------------------------------------------
# Hidden history bank for Task 3
# ---------------------------------------------------------------------------

_AMBIGUOUS_HISTORY_BANK = [
    {
        "medications": ["warfarin", "metformin"],
        "allergies": ["penicillin"],
        "contraindication": "warfarin",
        "contraindication_summary": (
            "Patient is on warfarin (anticoagulant). Combined with chest pain and "
            "shortness of breath, this elevates risk of pulmonary embolism — "
            "ESI should be escalated to 1."
        ),
    },
    {
        "medications": ["lisinopril", "aspirin"],
        "allergies": ["sulfa drugs"],
        "contraindication": "lisinopril",
        "contraindication_summary": (
            "Patient on lisinopril (ACE inhibitor) presenting with blurred vision "
            "and dizziness — hypertensive urgency cannot be excluded, ESI escalated to 2."
        ),
    },
    {
        "medications": ["insulin glargine", "metformin"],
        "allergies": ["latex"],
        "contraindication": "insulin glargine",
        "contraindication_summary": (
            "Diabetic patient on insulin with sudden onset vomiting and abdominal pain — "
            "diabetic ketoacidosis risk, ESI should be 2."
        ),
    },
    {
        "medications": ["clopidogrel", "atorvastatin"],
        "allergies": ["iodine contrast"],
        "contraindication": "clopidogrel",
        "contraindication_summary": (
            "Patient on clopidogrel (antiplatelet) with chest pain — ACS high suspicion, "
            "ESI must be 1."
        ),
    },
    {
        "medications": ["prednisone", "ibuprofen"],
        "allergies": ["aspirin"],
        "contraindication": "prednisone",
        "contraindication_summary": (
            "Long-term corticosteroid use can mask fever and signs of infection. "
            "Patient's actual severity may be higher — ESI escalated by 1."
        ),
    },
]


# ---------------------------------------------------------------------------
# Main Environment Class
# ---------------------------------------------------------------------------

class ClinicalTriageEnv:
    """
    OpenEnv-compliant Clinical Triage Environment.

    Usage:
        env = ClinicalTriageEnv("task1_esi_assignment")
        result = env.reset()
        step_result = env.step(TriageAction(action_type="assign_esi", esi_level=3))
    """

    VALID_TASKS = {
        "task1_esi_assignment",
        "task2_queue_priority",
        "task3_ambiguous_triage",
    }

    def __init__(self, task_id: str = "task1_esi_assignment", seed: Optional[int] = None):
        if task_id not in self.VALID_TASKS:
            raise ValueError(f"Unknown task: {task_id}. Choose from {self.VALID_TASKS}")
        self.task_id = task_id
        self.seed = seed
        self._rng = random.Random(seed)
        self._dataset: List[Dict] = self._load_dataset()

        # Episode state
        self._step_count: int = 0
        self._done: bool = False
        self._current_case: Optional[FullPatientCase] = None
        self._queue_cases: Optional[List[FullPatientCase]] = None
        self._hidden_history: Optional[Dict] = None
        self._revealed_topics: set = set()
        self._clarification_budget: int = 3
        self._contraindication_identified: bool = False
        self._total_reward: float = 0.0
        self._awaiting_final_esi: bool = False

    # ------------------------------------------------------------------
    # Dataset helpers
    # ------------------------------------------------------------------

    def _load_dataset(self) -> List[Dict]:
        if not DATASET_PATH.exists():
            raise FileNotFoundError(f"Dataset not found at {DATASET_PATH}")
        records = []
        with open(DATASET_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def _raw_to_full_case(self, raw: Dict) -> FullPatientCase:
        patient = raw["patient"]
        presentation = raw["presentation"]
        risk_assessment = raw["risk_assessment"]
        triage = raw["triage_classification"]

        red_flags = risk_assessment.get("red_flags", [])
        esi = _compute_esi(
            triage["urgency_category"],
            risk_assessment["risk_level"],
            red_flags,
        )
        return FullPatientCase(
            case_id=raw["case_id"],
            age=patient["age"],
            gender=patient["gender"],
            symptoms=presentation["symptoms"],
            symptom_count=presentation["symptom_count"],
            duration=presentation["duration"],
            onset=presentation["onset"],
            context=presentation["context"],
            risk_level=risk_assessment["risk_level"],
            red_flags=red_flags,
            true_esi=esi,
        )

    def _to_patient_summary(self, case: FullPatientCase) -> PatientSummary:
        return PatientSummary(
            case_id=case.case_id,
            age=case.age,
            gender=case.gender,
            symptoms=case.symptoms,
            symptom_count=case.symptom_count,
            duration=case.duration,
            onset=case.onset,
            context=case.context,
        )

    # ------------------------------------------------------------------
    # ESI reward helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _esi_reward(predicted: int, true: int) -> Tuple[float, bool, bool]:
        """Returns (reward, exact_match, partial_credit)."""
        if predicted == true:
            return 1.0, True, False
        if abs(predicted - true) == 1:
            return 0.4, False, True
        return 0.0, False, False

    # ------------------------------------------------------------------
    # Public OpenEnv API
    # ------------------------------------------------------------------

    def reset(self) -> ResetResult:
        """Reset the environment and return the initial observation."""
        self._step_count = 0
        self._done = False
        self._total_reward = 0.0
        self._revealed_topics = set()
        self._contraindication_identified = False
        self._awaiting_final_esi = False

        samples = self._rng.sample(self._dataset, k=min(10, len(self._dataset)))

        if self.task_id == "task1_esi_assignment":
            raw = self._rng.choice(samples)
            self._current_case = self._raw_to_full_case(raw)
            obs = TriageObservation(
                task_id=self.task_id,
                step=0,
                patient=self._to_patient_summary(self._current_case),
                message=(
                    "A patient has arrived at the Emergency Department. "
                    "Review the presentation and assign the appropriate ESI level (1–5). "
                    "ESI 1=Immediate, 2=Emergent, 3=Urgent, 4=Less Urgent, 5=Non-Urgent. "
                    "Use action_type='assign_esi' with esi_level=<1-5>."
                ),
            )

        elif self.task_id == "task2_queue_priority":
            # Pick 5 cases with diverse ESI levels for interesting ordering
            raw_list = self._rng.sample(self._dataset, k=min(20, len(self._dataset)))
            cases = [self._raw_to_full_case(r) for r in raw_list]
            # Ensure variety: pick one from each ESI tier where possible
            by_esi: Dict[int, List[FullPatientCase]] = {}
            for c in cases:
                by_esi.setdefault(c.true_esi, []).append(c)
            selected: List[FullPatientCase] = []
            for esi in sorted(by_esi.keys()):
                if len(selected) < 5 and by_esi[esi]:
                    selected.append(self._rng.choice(by_esi[esi]))
            chosen_ids = {c.case_id for c in selected}
            remaining = [c for c in cases if c.case_id not in chosen_ids]
            self._rng.shuffle(remaining)
            selected.extend(remaining[: 5 - len(selected)])
            self._queue_cases = selected[:5]
            self._rng.shuffle(self._queue_cases)  # shuffle presentation order

            summaries = [self._to_patient_summary(c) for c in self._queue_cases]
            obs = TriageObservation(
                task_id=self.task_id,
                step=0,
                queue=summaries,
                message=(
                    "Five patients have arrived simultaneously. "
                    "Order them from most urgent (first) to least urgent (last). "
                    "Use action_type='reorder_queue' with queue_order=[case_id_1, ..., case_id_5]. "
                    "ESI 1=most urgent, ESI 5=least urgent."
                ),
            )

        else:  # task3_ambiguous_triage
            self._clarification_budget = 3
            # Pick a case with known red_flags for ambiguity
            candidates = [
                r for r in samples
                if r["risk_assessment"].get("red_flags") and
                r["presentation"]["symptom_count"] >= 2
            ]
            if not candidates:
                candidates = samples
            raw = self._rng.choice(candidates)
            case = self._raw_to_full_case(raw)
            self._hidden_history = self._rng.choice(_AMBIGUOUS_HISTORY_BANK)
            case.hidden_medications = self._hidden_history["medications"]
            case.hidden_allergies = self._hidden_history["allergies"]
            case.hidden_contraindication = self._hidden_history["contraindication"]
            self._current_case = case

            obs = TriageObservation(
                task_id=self.task_id,
                step=0,
                patient=self._to_patient_summary(self._current_case),
                clarification_budget=self._clarification_budget,
                awaiting_final_esi=False,
                message=(
                    "A patient has arrived with an ambiguous presentation. "
                    "Their medication history and allergies are not immediately known. "
                    f"You have {self._clarification_budget} questions available. "
                    "Use action_type='ask_question' with question_topic in "
                    "['medications', 'allergies', 'past_medical_history', 'current_symptoms']. "
                    "When ready, use action_type='assign_esi' with esi_level=<1-5>."
                ),
            )

        return ResetResult(observation=obs)

    def step(self, action: TriageAction) -> StepResult:
        """Process one agent action and return updated observation + reward."""
        if self._done:
            raise RuntimeError("Episode is done. Call reset() to start a new episode.")

        self._step_count += 1
        info: Dict[str, Any] = {"step": self._step_count, "task_id": self.task_id}

        if self.task_id == "task1_esi_assignment":
            return self._step_task1(action, info)
        elif self.task_id == "task2_queue_priority":
            return self._step_task2(action, info)
        else:
            return self._step_task3(action, info)

    def state(self) -> Dict[str, Any]:
        """Return current environment state (for debugging / logging)."""
        base = {
            "task_id": self.task_id,
            "step": self._step_count,
            "done": self._done,
            "total_reward": self._total_reward,
        }
        if self._current_case:
            base["current_case_id"] = self._current_case.case_id
            base["true_esi"] = self._current_case.true_esi
        if self._queue_cases:
            base["queue_case_ids"] = [c.case_id for c in self._queue_cases]
            base["true_esi_order"] = [c.true_esi for c in self._queue_cases]
        if self.task_id == "task3_ambiguous_triage":
            base["clarification_budget"] = self._clarification_budget
            base["revealed_topics"] = list(self._revealed_topics)
            base["contraindication_identified"] = self._contraindication_identified
        return base

    # ------------------------------------------------------------------
    # Task-specific step handlers
    # ------------------------------------------------------------------

    def _step_task1(self, action: TriageAction, info: Dict) -> StepResult:
        if action.action_type != "assign_esi":
            obs = TriageObservation(
                task_id=self.task_id,
                step=self._step_count,
                patient=self._to_patient_summary(self._current_case),
                message="Invalid action. Use action_type='assign_esi' with esi_level=<1-5>.",
            )
            return StepResult(observation=obs, reward=0.0, done=False, info=info)

        predicted = action.esi_level
        true_esi = self._current_case.true_esi
        reward, exact, partial = self._esi_reward(predicted, true_esi)
        self._total_reward += reward
        self._done = True

        tr = TriageReward(
            value=reward,
            exact_match=exact,
            partial_credit=partial,
            details=(
                f"Predicted ESI={predicted}, True ESI={true_esi}. "
                f"{'Exact match.' if exact else 'Partial credit (±1).' if partial else 'Incorrect.'}"
            ),
        )
        info.update(tr.model_dump())

        obs = TriageObservation(
            task_id=self.task_id,
            step=self._step_count,
            message=tr.details,
        )
        return StepResult(observation=obs, reward=reward, done=True, info=info)

    def _step_task2(self, action: TriageAction, info: Dict) -> StepResult:
        if action.action_type != "reorder_queue":
            summaries = [self._to_patient_summary(c) for c in self._queue_cases]
            obs = TriageObservation(
                task_id=self.task_id,
                step=self._step_count,
                queue=summaries,
                message=(
                    "Invalid action. Use action_type='reorder_queue' "
                    "with queue_order=[case_id_1, ..., case_id_5]."
                ),
            )
            return StepResult(observation=obs, reward=0.0, done=False, info=info)

        # Build lookup: case_id → true_esi
        esi_by_id = {c.case_id: c.true_esi for c in self._queue_cases}
        predicted_order = action.queue_order or []

        # Validate all IDs present
        valid_ids = set(esi_by_id.keys())
        if not predicted_order or not set(predicted_order).issubset(valid_ids):
            obs = TriageObservation(
                task_id=self.task_id,
                step=self._step_count,
                queue=[self._to_patient_summary(c) for c in self._queue_cases],
                message=(
                    "Invalid queue_order. Provide all 5 case IDs: "
                    f"{sorted(valid_ids)}"
                ),
            )
            return StepResult(observation=obs, reward=0.0, done=False, info=info)

        # True order: sort by ESI ascending (ESI 1 = most urgent = position 1)
        true_sorted = sorted(self._queue_cases, key=lambda c: c.true_esi)
        true_ids = [c.case_id for c in true_sorted]

        # Build rank arrays for Kendall Tau
        # predicted_order: position 0 = most urgent
        true_rank = {cid: i for i, cid in enumerate(true_ids)}
        pred_rank = {cid: i for i, cid in enumerate(predicted_order)}

        n = len(predicted_order)
        concordant = 0
        discordant = 0
        total_pairs = n * (n - 1) // 2

        for i in range(n):
            for j in range(i + 1, n):
                ci, cj = predicted_order[i], predicted_order[j]
                pred_diff = pred_rank[ci] - pred_rank[cj]  # always negative since i<j
                true_diff = true_rank[ci] - true_rank[cj]
                if pred_diff * true_diff > 0:
                    concordant += 1
                elif pred_diff * true_diff < 0:
                    discordant += 1
                # ties count as neither

        tau = (concordant - discordant) / total_pairs if total_pairs > 0 else 0.0
        # Normalize tau from [-1, 1] to [0, 1]
        reward = (tau + 1.0) / 2.0
        reward = max(0.0, min(1.0, reward))
        self._total_reward += reward
        self._done = True

        tr = TriageReward(
            value=reward,
            kendall_tau=tau,
            details=(
                f"Kendall Tau={tau:.3f}, normalized reward={reward:.3f}. "
                f"True priority order: {true_ids}. "
                f"Your order: {predicted_order}."
            ),
        )
        info.update(tr.model_dump())

        obs = TriageObservation(
            task_id=self.task_id,
            step=self._step_count,
            message=tr.details,
        )
        return StepResult(observation=obs, reward=reward, done=True, info=info)

    def _step_task3(self, action: TriageAction, info: Dict) -> StepResult:
        case = self._current_case
        hidden = self._hidden_history

        # ── ask_question ──────────────────────────────────────────────
        if action.action_type == "ask_question":
            if self._clarification_budget <= 0:
                obs = TriageObservation(
                    task_id=self.task_id,
                    step=self._step_count,
                    patient=self._to_patient_summary(case),
                    clarification_budget=0,
                    awaiting_final_esi=True,
                    message=(
                        "No more clarification questions allowed. "
                        "Please assign an ESI level now."
                    ),
                )
                return StepResult(observation=obs, reward=0.0, done=False, info=info)

            topic = action.question_topic
            self._revealed_topics.add(topic)
            self._clarification_budget -= 1

            if topic == "medications":
                reply = (
                    f"Patient's current medications: {', '.join(hidden['medications'])}."
                )
                # Check if contraindication drug is in medications
                if hidden["contraindication"] in hidden["medications"]:
                    self._contraindication_identified = True
                    reply += (
                        f" NOTE: {hidden['contraindication'].upper()} flagged as "
                        f"clinically significant — review interaction carefully."
                    )
            elif topic == "allergies":
                reply = (
                    f"Known allergies: {', '.join(hidden['allergies'])}."
                    if hidden["allergies"]
                    else "No known drug allergies reported."
                )
            elif topic == "past_medical_history":
                reply = (
                    "Patient has a documented history consistent with chronic condition management. "
                    "Medical records indicate regular follow-ups with a specialist."
                )
            else:  # current_symptoms
                reply = (
                    f"Detailed symptom review: {', '.join(case.symptoms)}. "
                    f"Onset was {case.onset}, occurring {case.context}."
                )

            obs = TriageObservation(
                task_id=self.task_id,
                step=self._step_count,
                patient=self._to_patient_summary(case),
                clarification_budget=self._clarification_budget,
                awaiting_final_esi=self._clarification_budget == 0,
                message=reply,
            )
            return StepResult(observation=obs, reward=0.0, done=False, info=info)

        # ── assign_esi ────────────────────────────────────────────────
        elif action.action_type == "assign_esi":
            predicted = action.esi_level
            true_esi = case.true_esi

            base_reward, exact, partial = self._esi_reward(predicted, true_esi)

            # Contraindication bonus: +0.3 if identified via ask_question(medications)
            contra_bonus = 0.0
            if self._contraindication_identified:
                contra_bonus = 0.3

            raw_reward = base_reward + contra_bonus
            reward = min(1.0, raw_reward)  # cap for scoring purposes
            self._total_reward += reward
            self._done = True

            detail_parts = [
                f"Predicted ESI={predicted}, True ESI={true_esi}.",
                "Exact match." if exact else ("Partial credit (±1)." if partial else "Incorrect."),
            ]
            if contra_bonus > 0:
                detail_parts.append(
                    f"Contraindication bonus +{contra_bonus:.1f} awarded "
                    f"({hidden['contraindication']} identified)."
                )
            detail_parts.append(
                f"Raw reward={raw_reward:.2f}, capped reward={reward:.2f}."
            )

            tr = TriageReward(
                value=reward,
                exact_match=exact,
                partial_credit=partial,
                contraindication_bonus=contra_bonus > 0,
                details=" ".join(detail_parts),
            )
            info.update(tr.model_dump())
            info["contraindication_summary"] = hidden.get("contraindication_summary", "")

            obs = TriageObservation(
                task_id=self.task_id,
                step=self._step_count,
                message=tr.details,
            )
            return StepResult(observation=obs, reward=reward, done=True, info=info)

        else:
            obs = TriageObservation(
                task_id=self.task_id,
                step=self._step_count,
                patient=self._to_patient_summary(case),
                clarification_budget=self._clarification_budget,
                awaiting_final_esi=self._awaiting_final_esi,
                message=(
                    "Invalid action. Use action_type='ask_question' "
                    "or action_type='assign_esi'."
                ),
            )
            return StepResult(observation=obs, reward=0.0, done=False, info=info)
