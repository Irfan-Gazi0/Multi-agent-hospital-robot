"""
behaviors.py
------------
All custom py_trees Behaviour subclasses for the RHA Controller Agent.

Priority tree (left = highest priority):
  P1 — Infrastructure Emergency  : CheckFacilityEmergency  / HaltAndAlertNurse
  P2 — Security / Access Control : CheckAccessLevel         / InsufficientAccessMessage
  P3 — Task Plan Validation      : CheckInvalidTaskPlan     / InvalidPlanMessage
  P4 — Medication Safety (5 Rights): CheckMedicationSafety  / MedicationSafetyAlert
  P5 — Ambiguity / Contradiction : CheckAmbiguousCommand    / AmbiguityResolutionRequest
  P6 — HRII Task Execution       : ExecuteHRIITask

Design rules enforced throughout:
  • Every Behaviour calls self.attach_blackboard_client() in __init__, never in update().
  • Condition behaviours return SUCCESS when a problem IS detected (triggering the
    paired action in the Sequence) and FAILURE when the path is clear (falling through
    to the next priority branch in the Selector).
  • Action behaviours always return SUCCESS — if a safety or validation action is
    selected, it executes unconditionally.
"""

import json
import logging

import py_trees
from py_trees.common import Status

from medication_db import validate_medication

logger = logging.getLogger("RHA.Behaviors")

# ── Allowed skill namespaces and their mandatory params ──────────────────────
VALID_SKILL_PREFIXES = ("robot/", "facility/")

REQUIRED_SKILL_PARAMS: dict[str, list[str]] = {
    "robot/navigate": ["destination"],
    "robot/pick":     ["item"],
    "robot/place":    ["item"],
    "facility/door":  ["action", "room"],
    "facility/light": ["action", "room"],
}

# Absolute single-dose ceiling applied before the formulary maximum check.
ABSOLUTE_MAX_SINGLE_DOSE_MG = 10_000

# Keys whose presence in a robot/pick step's params marks it as medication.
_MEDICATION_PARAM_KEYS = {"medication_type", "dose_mg"}


def _is_medication_pick(params: dict) -> bool:
    """True if a robot/pick step is dispensing medication."""
    return (
        params.get("item", "").lower().strip() == "medication"
        or bool(_MEDICATION_PARAM_KEYS & params.keys())
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY 1 — Infrastructure Emergency
# ═══════════════════════════════════════════════════════════════════════════════

class CheckFacilityEmergency(py_trees.behaviour.Behaviour):
    """
    Condition: Reads the facility sensor feed from the blackboard.
    Returns SUCCESS  if a critical ward event (e.g. patient fall) is active.
    Returns FAILURE  if the environment is clear — tree falls through to P2.
    """

    def __init__(self, name: str = "CheckFacilityEmergency"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P1_EmergencyReader")
        self.bb.register_key(
            key="facility_emergency_detected",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        if self.bb.facility_emergency_detected:
            logger.debug("CheckFacilityEmergency: emergency flag is SET")
            return Status.SUCCESS
        return Status.FAILURE


class HaltAndAlertNurse(py_trees.behaviour.Behaviour):
    """
    Action (P1): Immediately halts any active task and pages the nursing
    station. In a real deployment this writes to the CAN bus stop register
    and triggers the hospital paging system.
    Always returns SUCCESS.
    """

    def __init__(self, name: str = "HaltAndAlertNurse"):
        super().__init__(name=name)

    def update(self) -> Status:
        logger.critical(
            "[FACILITY_EMERGENCY] Critical ward event detected. "
            "Halting all active tasks. Alerting nursing station — PRIORITY OVERRIDE."
        )
        return Status.SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY 2 — Security / Access Control
# ═══════════════════════════════════════════════════════════════════════════════

class CheckAccessLevel(py_trees.behaviour.Behaviour):
    """
    Condition: Compares the authenticated user's access level (1–3) against
    the minimum level required for the requested task.
      Level 1 = Patient
      Level 2 = Nurse
      Level 3 = Doctor / Pharmacist
    Returns SUCCESS  if user level < required level (access denied).
    Returns FAILURE  if user is authorised (falls through to P3).
    """

    def __init__(self, name: str = "CheckAccessLevel"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P2_AccessReader")
        self.bb.register_key(
            key="user_access_level",
            access=py_trees.common.Access.READ,
        )
        self.bb.register_key(
            key="required_task_access",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        user_lvl = self.bb.user_access_level
        req_lvl  = self.bb.required_task_access
        if user_lvl < req_lvl:
            logger.debug(
                "CheckAccessLevel: user=%d required=%d → access denied",
                user_lvl, req_lvl,
            )
            return Status.SUCCESS
        return Status.FAILURE


class InsufficientAccessMessage(py_trees.behaviour.Behaviour):
    """
    Action (P2): Informs the requesting entity that their access level is
    insufficient for the attempted task. Logs at ERROR level.
    Always returns SUCCESS.
    """

    def __init__(self, name: str = "InsufficientAccessMessage"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P2_AccessMsgReader")
        self.bb.register_key(
            key="user_access_level",
            access=py_trees.common.Access.READ,
        )
        self.bb.register_key(
            key="required_task_access",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        user_lvl  = self.bb.user_access_level
        req_lvl   = self.bb.required_task_access
        role_map  = {1: "Patient", 2: "Nurse", 3: "Doctor/Pharmacist"}
        user_role = role_map.get(user_lvl, f"Unknown(L{user_lvl})")
        req_role  = role_map.get(req_lvl,  f"Unknown(L{req_lvl})")
        logger.error(
            "[ACCESS_DENIED] User role '%s' (Level %d) is insufficient for a task "
            "requiring '%s' (Level %d). Request blocked.",
            user_role, user_lvl, req_role, req_lvl,
        )
        return Status.SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY 3 — Task Plan Validation
# ═══════════════════════════════════════════════════════════════════════════════

class CheckInvalidTaskPlan(py_trees.behaviour.Behaviour):
    """
    Condition: Validates the LLM-generated JSON task plan for structural
    correctness before any clinical or execution logic runs. Catches:
      • Empty or missing plan
      • Malformed / unparseable JSON
      • Steps missing the 'skill' or 'params' keys
      • Unknown skill namespaces (not robot/ or facility/)
      • Missing required parameters for known skills

    On detecting any problem: writes a human-readable reason to
    task_validation_error and returns SUCCESS (problem found → action fires).
    If the plan is structurally valid: returns FAILURE (fall through to P4).
    """

    def __init__(self, name: str = "CheckInvalidTaskPlan"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P3_PlanValidator")
        self.bb.register_key(
            key="llm_task_json",
            access=py_trees.common.Access.READ,
        )
        self.bb.register_key(
            key="task_validation_error",
            access=py_trees.common.Access.WRITE,
        )

    def _fail_with(self, reason: str) -> Status:
        """Write an error reason and signal problem detected."""
        self.bb.task_validation_error = reason
        logger.debug("CheckInvalidTaskPlan: %s", reason)
        return Status.SUCCESS

    def update(self) -> Status:
        raw = self.bb.llm_task_json

        # ── Empty plan ─────────────────────────────────────────────────
        if not raw or not raw.strip():
            return self._fail_with("Plan is empty. No task JSON received from LLM.")

        # ── JSON parse ─────────────────────────────────────────────────
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._fail_with(f"Malformed JSON — {exc}")

        # ── Steps list must exist and be non-empty ─────────────────────
        steps = plan.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            return self._fail_with(
                "Plan JSON is missing a non-empty 'steps' list."
            )

        # ── Validate each step ─────────────────────────────────────────
        for i, step in enumerate(steps):
            skill  = step.get("skill", "")
            params = step.get("params", {})

            # Skill key must exist
            if not skill:
                return self._fail_with(
                    f"Step {i+1} is missing the 'skill' field."
                )

            # Skill must be in the allowlist of known, registered skills
            if skill not in REQUIRED_SKILL_PARAMS:
                return self._fail_with(
                    f"Step {i+1} uses unknown skill: '{skill}'. "
                    f"Registered skills: {list(REQUIRED_SKILL_PARAMS.keys())}."
                )

            # Params must be a non-empty dict
            if not isinstance(params, dict) or len(params) == 0:
                return self._fail_with(
                    f"Step {i+1} ('{skill}') has empty or missing 'params'."
                )

            # Check mandatory params for known skill types
            required = REQUIRED_SKILL_PARAMS.get(skill, [])
            for req_param in required:
                if req_param not in params:
                    return self._fail_with(
                        f"Step {i+1} ('{skill}') is missing required "
                        f"parameter '{req_param}'."
                    )

            # Canonical rule: medication dispensing steps must use item="medication"
            # so P4's formulary check is guaranteed to evaluate them.
            if skill == "robot/pick" and _is_medication_pick(params):
                if params.get("item", "").lower().strip() != "medication":
                    bad_keys = sorted(_MEDICATION_PARAM_KEYS & params.keys())
                    return self._fail_with(
                        f"Step {i+1}: robot/pick has medication parameters "
                        f"({', '.join(bad_keys)}) but item='{params.get('item')}'. "
                        "All drug dispensing steps must use item='medication'."
                    )

        # ── All checks passed ─────────────────────────────────────────
        self.bb.task_validation_error = ""
        return Status.FAILURE


class InvalidPlanMessage(py_trees.behaviour.Behaviour):
    """
    Action (P3): Logs the validation failure reason and queues the LLM
    agent for re-generation of the task plan.
    Always returns SUCCESS.
    """

    def __init__(self, name: str = "InvalidPlanMessage"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P3_InvalidPlanMsg")
        self.bb.register_key(
            key="task_validation_error",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        logger.error(
            "[INVALID_PLAN] %s  →  Queuing LLM task re-generation.",
            self.bb.task_validation_error,
        )
        return Status.SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY 4 — Medication Safety (The Five Rights)
# ═══════════════════════════════════════════════════════════════════════════════

class CheckMedicationSafety(py_trees.behaviour.Behaviour):
    """
    Condition: Scans the task plan for robot/pick steps that involve
    administering medication and validates them against the patient's
    formulary record. Implements checks for:
      Right Patient  — task patient_id matches the active blackboard patient_id
      Right Drug     — drug is in the patient's prescribed medication list
      Right Dose     — dose_mg does not exceed the formulary maximum
      Allergy Check  — drug does not appear on the patient's allergy list

    If the plan contains no medication steps this check is a no-op (FAILURE).
    On any clinical violation: writes a structured dict to
    medication_safety_violation and returns SUCCESS.
    If all medication steps are safe: returns FAILURE (fall through to P5).
    """

    def __init__(self, name: str = "CheckMedicationSafety"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P4_MedSafetyChecker")
        self.bb.register_key(
            key="llm_task_json",
            access=py_trees.common.Access.READ,
        )
        self.bb.register_key(
            key="patient_id",
            access=py_trees.common.Access.READ,
        )
        self.bb.register_key(
            key="medication_safety_violation",
            access=py_trees.common.Access.WRITE,
        )

    def _violation(self, v_type: str, drug: str, reason: str) -> Status:
        self.bb.medication_safety_violation = {
            "type":   v_type,
            "drug":   drug,
            "reason": reason,
        }
        logger.debug("CheckMedicationSafety: violation=%s drug=%s", v_type, drug)
        return Status.SUCCESS

    def update(self) -> Status:
        raw        = self.bb.llm_task_json
        bb_patient = self.bb.patient_id   # authoritative source from access control

        # ── If the plan didn't pass P3 this key may be empty, but
        #    P3 would have caught it first. Defensive guard:
        if not raw or not raw.strip():
            return Status.FAILURE

        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            return Status.FAILURE   # P3 should have caught this first

        steps = plan.get("steps", [])

        # ── Right Patient: compare JSON patient_id to blackboard patient_id
        json_patient = plan.get("patient_id", "").strip()
        if json_patient and bb_patient and json_patient != bb_patient:
            return self._violation(
                v_type="WRONG_PATIENT",
                drug="N/A",
                reason=(
                    f"Task JSON specifies patient '{json_patient}' but the "
                    f"authenticated patient context is '{bb_patient}'. "
                    "Possible patient ID mismatch — task blocked."
                ),
            )

        # Use the blackboard patient_id as ground truth for drug checks.
        # If no patient is set, we can't safely verify — block the task.
        effective_patient = bb_patient or json_patient
        if not effective_patient:
            # No medication step can be validated without a patient ID.
            has_med_step = any(
                s.get("skill") == "robot/pick"
                and _is_medication_pick(s.get("params", {}))
                for s in steps
            )
            if has_med_step:
                return self._violation(
                    v_type="MISSING_PATIENT_ID",
                    drug="N/A",
                    reason="Medication step detected but no patient_id is set "
                           "on the blackboard. Cannot verify safety.",
                )
            return Status.FAILURE

        # ── Check each robot/pick medication step ─────────────────────
        found_med_step = False
        for i, step in enumerate(steps):
            if step.get("skill") != "robot/pick":
                continue
            params = step.get("params", {})
            if not _is_medication_pick(params):
                continue

            found_med_step = True
            raw_item = params.get("item", "").lower().strip()
            drug = params.get("medication_type", "").lower().strip()
            if not drug and raw_item != "medication":
                drug = raw_item
            dose_mg = params.get("dose_mg", 0)

            if not drug:
                return self._violation(
                    v_type="MISSING_MEDICATION_TYPE",
                    drug="(unknown)",
                    reason=f"Step {i+1}: robot/pick is a medication step but "
                           "no drug name could be resolved from 'medication_type' "
                           "or 'item'. Cannot validate safely.",
                )

            if not isinstance(dose_mg, (int, float)) or dose_mg <= 0:
                return self._violation(
                    v_type="INVALID_DOSE",
                    drug=drug,
                    reason=(
                        f"Step {i+1}: dose_mg must be a positive number, "
                        f"got '{dose_mg}'. Task blocked."
                    ),
                )

            # Ceiling is independent of the formulary maximum — catches LLM dose hallucinations.
            if dose_mg > ABSOLUTE_MAX_SINGLE_DOSE_MG:
                return self._violation(
                    v_type="UNSAFE_DOSE",
                    drug=drug,
                    reason=(
                        f"Step {i+1}: {dose_mg}mg of '{drug}' exceeds the "
                        f"absolute single-dose ceiling of {ABSOLUTE_MAX_SINGLE_DOSE_MG}mg. "
                        "Clinical review required before any dispensing."
                    ),
                )

            # Query the formulary
            safe, reason = validate_medication(effective_patient, drug, dose_mg)
            if not safe:
                return self._violation(
                    v_type="FORMULARY_VIOLATION",
                    drug=drug,
                    reason=reason,
                )

        # ── All medication steps passed ────────────────────────────────
        self.bb.medication_safety_violation = {}
        return Status.FAILURE


class MedicationSafetyAlert(py_trees.behaviour.Behaviour):
    """
    Action (P4): Blocks the task and issues a critical pharmacist alert
    detailing the specific clinical violation detected by CheckMedicationSafety.
    Always returns SUCCESS.
    """

    def __init__(self, name: str = "MedicationSafetyAlert"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P4_MedSafetyAlert")
        self.bb.register_key(
            key="medication_safety_violation",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        v = self.bb.medication_safety_violation
        logger.critical(
            "[MEDICATION_SAFETY] CRITICAL — %s | Drug: '%s' | %s | "
            "Task blocked. Alerting pharmacist.",
            v.get("type",   "UNKNOWN_VIOLATION"),
            v.get("drug",   "unknown"),
            v.get("reason", "No reason provided"),
        )
        return Status.SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY 5 — Ambiguity / Contradiction Detection
# ═══════════════════════════════════════════════════════════════════════════════

class CheckAmbiguousCommand(py_trees.behaviour.Behaviour):
    """
    Condition: Inspects the task plan for logical problems that do not
    constitute safety violations but would cause undefined or incorrect
    robot behaviour. Detects:
      • Contradictory facility/door commands (same room opened then closed)
      • Missing required parameters for known skill types
      • Ambiguous location targets (e.g. "Room" without a room number)

    On detecting any issue: writes a list of plain-English flag strings to
    ambiguity_flags and returns SUCCESS.
    If no issues found: returns FAILURE (fall through to P6 execution).
    """

    # Room numbers are expected to contain at least one digit, e.g. "Room 2",
    # "Ward B Room 4", "Pharmacy" (fixed-name locations are also acceptable).
    KNOWN_FIXED_LOCATIONS = {"pharmacy", "icu", "reception", "nurses station"}

    def __init__(self, name: str = "CheckAmbiguousCommand"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P5_AmbiguityChecker")
        self.bb.register_key(
            key="llm_task_json",
            access=py_trees.common.Access.READ,
        )
        self.bb.register_key(
            key="ambiguity_flags",
            access=py_trees.common.Access.WRITE,
        )

    @staticmethod
    def _is_ambiguous_location(location: str) -> bool:
        """Returns True if a location string has no digit and is not a known fixed name."""
        loc = location.lower().strip()
        if any(c.isdigit() for c in loc):
            return False
        if loc in CheckAmbiguousCommand.KNOWN_FIXED_LOCATIONS:
            return False
        # A generic word like "Room" or "Ward" with no number is ambiguous.
        generic_keywords = {"room", "ward", "bay", "corridor", "floor"}
        return any(kw in loc for kw in generic_keywords)

    def update(self) -> Status:
        raw = self.bb.llm_task_json
        if not raw or not raw.strip():
            return Status.FAILURE
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            return Status.FAILURE   # P3 would have caught this; defensive guard

        steps = plan.get("steps", [])
        flags: list[str] = []

        # Track door states per room to detect open/close contradictions
        door_actions: dict[str, list[str]] = {}  # room → [actions]

        for i, step in enumerate(steps):
            skill  = step.get("skill", "")
            params = step.get("params", {})
            step_label = f"Step {i+1} ('{skill}')"

            # ── Check required params ──────────────────────────────────
            required = REQUIRED_SKILL_PARAMS.get(skill, [])
            for req in required:
                if req not in params:
                    flags.append(
                        f"{step_label}: missing required parameter '{req}'."
                    )

            # ── Check for ambiguous location strings ───────────────────
            for loc_key in ("destination", "room"):
                value = params.get(loc_key, "")
                if value and self._is_ambiguous_location(str(value)):
                    flags.append(
                        f"{step_label}: location '{value}' is ambiguous "
                        f"(no room number). Specify e.g. 'Room 4' or 'Ward B Room 2'."
                    )

            # ── Track door open/close per room ─────────────────────────
            if skill == "facility/door":
                room   = params.get("room", "").lower().strip()
                action = params.get("action", "").lower().strip()
                if room:
                    door_actions.setdefault(room, []).append(action)

        # ── Detect door open-then-close contradictions ─────────────────
        for room, actions in door_actions.items():
            if "open" in actions and "close" in actions:
                flags.append(
                    f"Contradictory door commands for '{room}': the plan both "
                    "opens and closes this door. Clarify intended final state."
                )

        if flags:
            self.bb.ambiguity_flags = flags
            logger.debug(
                "CheckAmbiguousCommand: %d issue(s) found: %s", len(flags), flags
            )
            return Status.SUCCESS

        self.bb.ambiguity_flags = []
        return Status.FAILURE


class AmbiguityResolutionRequest(py_trees.behaviour.Behaviour):
    """
    Action (P5): Pauses execution and requests operator clarification,
    listing every ambiguity or contradiction detected in the plan.
    Always returns SUCCESS.
    """

    def __init__(self, name: str = "AmbiguityResolutionRequest"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P5_AmbiguityMsg")
        self.bb.register_key(
            key="ambiguity_flags",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        flags = self.bb.ambiguity_flags
        n     = len(flags)
        items = "\n  ".join(f"[{j+1}] {f}" for j, f in enumerate(flags))
        logger.warning(
            "[AMBIGUOUS_CMD] %d issue(s) detected in task plan. "
            "Execution paused. Requesting operator clarification:\n  %s",
            n, items,
        )
        return Status.SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY 6 — HRII Task Execution
# ═══════════════════════════════════════════════════════════════════════════════

class ExecuteHRIITask(py_trees.behaviour.Behaviour):
    """
    Action (P6): Reached only when all five guard branches above have
    returned FAILURE (environment clear, access granted, plan valid,
    medication safe, no ambiguity). Parses the JSON execution plan and
    dispatches each step to the appropriate subsystem, distinguishing
    between:
      [ROBOT SKILL]    — commands sent to the RHA motor controller
      [FACILITY SKILL] — commands sent to the smart-building BMS / IoT layer

    Returns SUCCESS if the plan is executed.
    Returns FAILURE  if the plan is empty (should not happen after P3, but
                     defensive guard in case of a direct test setup).
    """

    def __init__(self, name: str = "ExecuteHRIITask"):
        super().__init__(name=name)
        self.bb = self.attach_blackboard_client(name="P6_TaskExecutor")
        self.bb.register_key(
            key="llm_task_json",
            access=py_trees.common.Access.READ,
        )

    def update(self) -> Status:
        raw = self.bb.llm_task_json
        if not raw or not raw.strip():
            return Status.FAILURE

        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            return Status.FAILURE

        steps     = plan.get("steps", [])
        task_name = plan.get("task", "Unnamed Task")

        if not steps:
            return Status.FAILURE

        logger.info(
            "[NOMINAL OPERATION] Executing HRII task: '%s' (%d steps)",
            task_name, len(steps),
        )

        for step in steps:
            skill  = step.get("skill", "")
            params = step.get("params", {})

            if skill.startswith("robot/"):
                logger.info(
                    "  [ROBOT SKILL]    %s  →  %s", skill, params
                )
            elif skill.startswith("facility/"):
                logger.info(
                    "  [FACILITY SKILL] %s  →  %s", skill, params
                )
            else:
                # Should not be reached after P3 validation, but log it.
                logger.warning(
                    "  [UNKNOWN SKILL]  %s  →  %s  (skipped)", skill, params
                )

        logger.info("[NOMINAL OPERATION] Task '%s' dispatch complete.", task_name)
        return Status.SUCCESS
