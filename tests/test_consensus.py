"""
tests/test_consensus.py
-----------------------
pytest suite for the RHA Controller Agent Behavior Tree.

Each test:
  1. Creates a fresh tree instance (new Behaviour objects, new bb clients)
  2. Sets blackboard values directly via a standalone writer client
  3. Ticks the tree once
  4. Asserts the root status AND the status of specific child branches

Key assertion idiom — proving priority override:
  py_trees.common.Status.INVALID means a node was NEVER evaluated during
  this tick. Asserting INVALID on a lower-priority branch proves the
  Selector stopped at a higher-priority branch.

  children[0] = P1_Infrastructure_Emergency
  children[1] = P2_Security_Override
  children[2] = P3_Task_Plan_Validation
  children[3] = P4_Medication_Safety
  children[4] = P5_Ambiguity_Detection
  children[5] = ExecuteHRIITask
"""

import json
import sys
import os

import pytest
import py_trees
from py_trees.common import Status

# Allow imports from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from controller_tree import create_consensus_tree

# ── Shared JSON fixtures ───────────────────────────────────────────────────────

VALID_JSON = json.dumps({
    "task": "Fetch Medication",
    "patient_id": "P001",
    "steps": [
        {"skill": "robot/navigate", "params": {"destination": "Pharmacy"}},
        {"skill": "facility/door",  "params": {"action": "open",  "room": "Pharmacy"}},
        {"skill": "robot/pick",     "params": {
            "item": "medication",
            "medication_type": "metformin",
            "dose_mg": 500,
        }},
        # Robot does not close the pharmacy door — staff responsibility
        {"skill": "robot/navigate", "params": {"destination": "Ward B Room 2"}},
        {"skill": "robot/place",    "params": {"item": "medication", "patient_id": "P001"}},
    ],
})

NON_MEDICATION_JSON = json.dumps({
    "task": "Deliver Supplies",
    "steps": [
        {"skill": "robot/navigate", "params": {"destination": "Ward B Room 4"}},
        {"skill": "robot/pick",     "params": {"item": "linen bundle"}},
        {"skill": "robot/place",    "params": {"item": "linen bundle"}},
    ],
})


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_tree():
    """
    Creates a fresh tree + standalone writer client for each test.
    The writer initialises all 8 blackboard keys to safe defaults.
    Clears blackboard values on teardown.
    """
    py_trees.blackboard.Blackboard.enable_activity_stream(maximum_size=100)

    # Standalone writer client — unique name prevents collisions across tests
    writer = py_trees.blackboard.Client(name=f"TestWriter_{id(object())}")
    for key in [
        "facility_emergency_detected",
        "user_access_level",
        "required_task_access",
        "llm_task_json",
        "patient_id",
        "task_validation_error",
        "medication_safety_violation",
        "ambiguity_flags",
    ]:
        writer.register_key(key=key, access=py_trees.common.Access.WRITE)

    # Safe defaults
    writer.facility_emergency_detected = False
    writer.user_access_level            = 3
    writer.required_task_access         = 3
    writer.llm_task_json                = ""
    writer.patient_id                   = ""
    writer.task_validation_error        = ""
    writer.medication_safety_violation  = {}
    writer.ambiguity_flags              = []

    tree = create_consensus_tree()
    tree.setup(timeout=15)

    yield tree, writer

    py_trees.blackboard.Blackboard.clear()


# ── Test cases ─────────────────────────────────────────────────────────────────

class TestPriority1_InfrastructureEmergency:

    def test_emergency_halts_everything(self, fresh_tree):
        """
        When facility_emergency_detected=True, P1 fires regardless of all
        other conditions. P2–P6 must be INVALID (never evaluated).
        """
        tree, writer = fresh_tree
        writer.facility_emergency_detected = True
        writer.user_access_level            = 3
        writer.required_task_access         = 3
        writer.patient_id                   = "P001"
        writer.llm_task_json                = VALID_JSON

        tree.tick()

        root = tree.root
        assert root.status == Status.SUCCESS
        assert root.children[0].status == Status.SUCCESS   # P1 won
        assert root.children[1].status == Status.INVALID   # P2 never evaluated
        assert root.children[2].status == Status.INVALID   # P3 never evaluated
        assert root.children[3].status == Status.INVALID   # P4 never evaluated
        assert root.children[4].status == Status.INVALID   # P5 never evaluated
        assert root.children[5].status == Status.INVALID   # P6 never evaluated

    def test_emergency_overrides_access_denial(self, fresh_tree):
        """
        Emergency (P1) beats access denial (P2) — highest priority wins.
        """
        tree, writer = fresh_tree
        writer.facility_emergency_detected = True
        writer.user_access_level            = 1   # Patient — would normally be denied
        writer.required_task_access         = 3
        writer.llm_task_json                = VALID_JSON

        tree.tick()

        root = tree.root
        assert root.children[0].status == Status.SUCCESS   # P1 won
        assert root.children[1].status == Status.INVALID   # P2 never reached


class TestPriority2_SecurityOverride:

    def test_patient_denied_doctor_task(self, fresh_tree):
        """
        Patient (L1) requesting a Doctor-only (L3) task → P2 fires.
        P3–P6 must be INVALID.
        """
        tree, writer = fresh_tree
        writer.facility_emergency_detected = False
        writer.user_access_level            = 1   # Patient
        writer.required_task_access         = 3   # Doctor-only task
        writer.llm_task_json                = VALID_JSON

        tree.tick()

        root = tree.root
        assert root.status == Status.SUCCESS
        assert root.children[0].status == Status.FAILURE   # P1: no emergency
        assert root.children[1].status == Status.SUCCESS   # P2 won
        assert root.children[2].status == Status.INVALID   # P3 never evaluated
        assert root.children[5].status == Status.INVALID   # P6 never evaluated

    def test_equal_access_level_is_allowed(self, fresh_tree):
        """
        user_access_level == required_task_access → P2 should NOT fire
        (CheckAccessLevel returns FAILURE, falls through to P3).
        """
        tree, writer = fresh_tree
        writer.facility_emergency_detected = False
        writer.user_access_level            = 2   # Nurse
        writer.required_task_access         = 2   # Nurse-level task
        writer.patient_id                   = ""
        writer.llm_task_json                = NON_MEDICATION_JSON

        tree.tick()

        assert tree.root.children[1].status == Status.FAILURE  # P2 did NOT fire


class TestPriority3_TaskPlanValidation:

    def test_malformed_json_blocked(self, fresh_tree):
        """Unparseable JSON is caught by P3; P4–P6 are INVALID."""
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.llm_task_json        = '{"task": "Bad", "steps": [{not valid json}]'

        tree.tick()

        root = tree.root
        assert root.status == Status.SUCCESS
        assert root.children[2].status == Status.SUCCESS   # P3 fired
        assert root.children[3].status == Status.INVALID   # P4 never reached
        assert root.children[5].status == Status.INVALID   # P6 never reached

    def test_unknown_skill_blocked(self, fresh_tree):
        """
        A plan containing an unknown skill namespace (robot/teleport)
        must be blocked by P3.
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.llm_task_json        = json.dumps({
            "task": "Emergency Transport",
            "steps": [
                {"skill": "robot/teleport", "params": {"destination": "ICU"}},
            ],
        })

        tree.tick()

        assert tree.root.children[2].status == Status.SUCCESS   # P3 fired
        assert tree.root.children[5].status == Status.INVALID   # P6 never reached

    def test_missing_required_param_blocked(self, fresh_tree):
        """
        robot/navigate without a 'destination' param must be caught by P3.
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.llm_task_json        = json.dumps({
            "task": "Navigate",
            "steps": [
                {"skill": "robot/navigate", "params": {"speed": "slow"}},  # missing 'destination'
            ],
        })

        tree.tick()

        assert tree.root.children[2].status == Status.SUCCESS   # P3 fired

    def test_empty_plan_blocked(self, fresh_tree):
        """An empty llm_task_json string must be caught by P3."""
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.llm_task_json        = ""

        tree.tick()

        assert tree.root.children[2].status == Status.SUCCESS   # P3 fired


class TestPriority4_MedicationSafety:

    def test_wrong_medication_blocked(self, fresh_tree):
        """
        Aspirin is not in P001's prescription — P4 must fire; P5–P6 INVALID.
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.patient_id           = "P001"
        writer.llm_task_json        = json.dumps({
            "task": "Fetch Medication",
            "patient_id": "P001",
            "steps": [
                {"skill": "robot/pick", "params": {
                    "item": "medication",
                    "medication_type": "aspirin",  # P002's drug, not P001's
                    "dose_mg": 100,
                }},
                {"skill": "robot/place", "params": {"item": "medication"}},
            ],
        })

        tree.tick()

        root = tree.root
        assert root.status == Status.SUCCESS
        assert root.children[2].status == Status.FAILURE   # P3 passed
        assert root.children[3].status == Status.SUCCESS   # P4 fired
        assert root.children[4].status == Status.INVALID   # P5 never reached
        assert root.children[5].status == Status.INVALID   # P6 never reached

    def test_overdose_blocked(self, fresh_tree):
        """1000mg metformin exceeds P001's 500mg maximum → P4 fires."""
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.patient_id           = "P001"
        writer.llm_task_json        = json.dumps({
            "task": "Fetch Medication",
            "patient_id": "P001",
            "steps": [
                {"skill": "robot/pick", "params": {
                    "item": "medication",
                    "medication_type": "metformin",
                    "dose_mg": 1000,    # max is 500
                }},
            ],
        })

        tree.tick()

        assert tree.root.children[3].status == Status.SUCCESS   # P4 fired
        assert tree.root.children[5].status == Status.INVALID   # P6 never reached

    def test_wrong_patient_id_blocked(self, fresh_tree):
        """
        JSON specifies patient P002 but blackboard patient_id is P001 →
        patient ID mismatch → P4 fires.
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.patient_id           = "P001"   # authenticated patient
        writer.llm_task_json        = json.dumps({
            "task": "Fetch Medication",
            "patient_id": "P002",   # mismatch
            "steps": [
                {"skill": "robot/pick", "params": {
                    "item": "medication",
                    "medication_type": "aspirin",
                    "dose_mg": 100,
                }},
            ],
        })

        tree.tick()

        assert tree.root.children[3].status == Status.SUCCESS   # P4 fired

    def test_non_medication_task_passes_p4(self, fresh_tree):
        """
        A plan with no medication steps should pass P4 (FAILURE → fall through).
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.patient_id           = "P001"
        writer.llm_task_json        = NON_MEDICATION_JSON

        tree.tick()

        # P4 should NOT fire — no medication steps in the plan
        assert tree.root.children[3].status == Status.FAILURE


class TestPriority5_AmbiguityDetection:

    def test_door_contradiction_blocked(self, fresh_tree):
        """
        Plan that opens and closes the same door is contradictory → P5 fires.
        P6 must be INVALID.
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.patient_id           = ""
        writer.llm_task_json        = json.dumps({
            "task": "Enter Room",
            "steps": [
                {"skill": "facility/door", "params": {"action": "open",  "room": "Ward B Room 3"}},
                {"skill": "robot/navigate","params": {"destination": "Ward B Room 3"}},
                {"skill": "facility/door", "params": {"action": "close", "room": "Ward B Room 3"}},
            ],
        })

        tree.tick()

        root = tree.root
        assert root.status == Status.SUCCESS
        assert root.children[4].status == Status.SUCCESS   # P5 fired
        assert root.children[5].status == Status.INVALID   # P6 never reached


class TestPriority6_NominalExecution:

    def test_nominal_execution_all_guards_pass(self, fresh_tree):
        """
        All five guards return FAILURE (environment clear).
        ExecuteHRIITask (P6) must reach SUCCESS.
        """
        tree, writer = fresh_tree
        writer.facility_emergency_detected = False
        writer.user_access_level            = 3
        writer.required_task_access         = 3
        writer.patient_id                   = "P001"
        writer.llm_task_json                = VALID_JSON

        tree.tick()

        root = tree.root
        assert root.status == Status.SUCCESS
        # All guard branches must have returned FAILURE (clear path)
        assert root.children[0].status == Status.FAILURE   # P1 clear
        assert root.children[1].status == Status.FAILURE   # P2 clear
        assert root.children[2].status == Status.FAILURE   # P3 valid JSON
        assert root.children[3].status == Status.FAILURE   # P4 medication safe
        assert root.children[4].status == Status.FAILURE   # P5 no ambiguity
        assert root.children[5].status == Status.SUCCESS   # P6 executed

    def test_no_task_json_returns_root_failure(self, fresh_tree):
        """
        With no JSON available, P3 fires (empty plan).
        When there is genuinely nothing to do, the tree correctly blocks execution.
        """
        tree, writer = fresh_tree
        writer.user_access_level    = 3
        writer.required_task_access = 3
        writer.llm_task_json        = ""

        tree.tick()

        # P3 fires for the empty plan
        assert tree.root.children[2].status == Status.SUCCESS
