"""
test_runner.py
--------------
Deterministic 8-scenario demonstration of the RHA Controller Agent.

Each scenario directly sets blackboard values then ticks the tree once,
printing the result. No real hardware or API calls are made — this is a
pure logic verification of the 6-priority Behavior Tree routing.

Run:
    python test_runner.py

Scenarios:
  A — Nominal Execution    : all guards pass → ExecuteHRIITask fires
  B — Access Denied        : Patient requests Doctor-only task
  C — Emergency Override   : Patient fall stops everything
  D — Invalid JSON         : LLM returned malformed output
  E — Unknown Skill        : LLM hallucinated a non-existent skill
  F — Wrong Medication     : Drug not in patient's prescription list
  G — Overdose             : Dose exceeds formulary maximum
  H — Contradictory Plan   : Plan opens and closes the same door
"""

import json
import logging
import sys

import py_trees

from blackboard_setup import initialise_blackboard
from controller_tree import create_consensus_tree

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s  %(name)s  |  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("RHA.TestRunner")

# ── Reusable JSON fixtures ────────────────────────────────────────────────────

VALID_MEDICATION_JSON = json.dumps({
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
        {"skill": "facility/light", "params": {"action": "on",    "room": "Ward B Room 2"}},
        {"skill": "robot/place",    "params": {"item": "medication", "patient_id": "P001"}},
    ],
})

WRONG_MEDICATION_JSON = json.dumps({
    "task": "Fetch Medication",
    "patient_id": "P001",
    "steps": [
        {"skill": "robot/navigate", "params": {"destination": "Pharmacy"}},
        # aspirin belongs to P002 — P001 is not prescribed aspirin
        {"skill": "robot/pick", "params": {
            "item": "medication",
            "medication_type": "aspirin",
            "dose_mg": 100,
        }},
        {"skill": "robot/place", "params": {"item": "medication", "patient_id": "P001"}},
    ],
})

OVERDOSE_JSON = json.dumps({
    "task": "Fetch Medication",
    "patient_id": "P001",
    "steps": [
        {"skill": "robot/pick", "params": {
            "item": "medication",
            "medication_type": "metformin",
            "dose_mg": 1000,   # max for P001 is 500 mg
        }},
        {"skill": "robot/place", "params": {"item": "medication", "patient_id": "P001"}},
    ],
})

CONTRADICTION_JSON = json.dumps({
    "task": "Enter and Secure Room",
    "steps": [
        {"skill": "robot/navigate",  "params": {"destination": "Ward B Room 3"}},
        {"skill": "facility/door",   "params": {"action": "open",  "room": "Ward B Room 3"}},
        {"skill": "robot/pick",      "params": {"item": "supply kit"}},
        # LLM also closes the same door in the same plan — contradiction
        {"skill": "facility/door",   "params": {"action": "close", "room": "Ward B Room 3"}},
    ],
})

UNKNOWN_SKILL_JSON = json.dumps({
    "task": "Emergency Transport",
    "steps": [
        # LLM hallucinated a non-existent skill
        {"skill": "robot/teleport", "params": {"destination": "ICU"}},
    ],
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _set_blackboard(writer, **kwargs):
    """Write key-value pairs directly onto the blackboard via the writer client."""
    for key, value in kwargs.items():
        setattr(writer, key, value)


def _print_scenario_header(label: str, description: str):
    print("\n" + "─" * 60)
    print(f"  SCENARIO {label}: {description}")
    print("─" * 60)


def _print_result(tree: py_trees.trees.BehaviourTree):
    root   = tree.root
    status = root.status
    winner = _find_winner(root)
    print(f"  ▶ Root status : {status.name}")
    print(f"  ▶ Winning branch: {winner}")


def _find_winner(root) -> str:
    """Returns the name of the first child branch that reported SUCCESS."""
    for child in root.children:
        if child.status == py_trees.common.Status.SUCCESS:
            # Composite nodes have children; leaf nodes do not
            sub_children = getattr(child, "children", [])
            if sub_children:
                return f"{child.name}  →  {sub_children[-1].__class__.__name__}"
            return child.__class__.__name__
    if root.status == py_trees.common.Status.FAILURE:
        return "(none — root FAILURE: nothing to do)"
    return "(unknown)"


# ── Scenarios ──────────────────────────────────────────────────────────────────

def run_scenario_a(writer, tree):
    """Nominal: Doctor requests medication fetch — all guards pass."""
    _print_scenario_header("A", "Nominal Execution — Doctor fetches P001 medication")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=3,           # Doctor
        required_task_access=3,        # Fetch Medication requires L3
        patient_id="P001",
        llm_task_json=VALID_MEDICATION_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_b(writer, tree):
    """Access Denied: Patient (L1) attempts a Doctor-only (L3) task."""
    _print_scenario_header("B", "Access Denied — Patient requests medication fetch")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=1,           # Patient
        required_task_access=3,        # Fetch Medication requires L3
        patient_id="P001",
        llm_task_json=VALID_MEDICATION_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_c(writer, tree):
    """Emergency: Patient fall overrides even an authorised Doctor request."""
    _print_scenario_header("C", "Infrastructure Emergency — Patient fall in Room 2")
    _set_blackboard(
        writer,
        facility_emergency_detected=True,   # FALL DETECTED
        user_access_level=3,
        required_task_access=3,
        patient_id="P001",
        llm_task_json=VALID_MEDICATION_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_d(writer, tree):
    """Invalid JSON: LLM returned unparseable output."""
    _print_scenario_header("D", "Invalid JSON — LLM returned malformed output")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=3,
        required_task_access=3,
        patient_id="P001",
        llm_task_json='{"task": "Fetch Med", "steps": [{bad json}]',  # malformed
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_e(writer, tree):
    """Unknown Skill: LLM hallucinated a non-existent robot/teleport skill."""
    _print_scenario_header("E", "Unknown Skill — LLM hallucinated 'robot/teleport'")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=3,
        required_task_access=3,
        patient_id="P001",
        llm_task_json=UNKNOWN_SKILL_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_f(writer, tree):
    """Wrong Medication: LLM planned aspirin for P001 (not prescribed)."""
    _print_scenario_header("F", "Wrong Medication — aspirin not in P001's prescription")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=3,
        required_task_access=3,
        patient_id="P001",
        llm_task_json=WRONG_MEDICATION_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_g(writer, tree):
    """Overdose: LLM planned 1000mg metformin; P001's max is 500mg."""
    _print_scenario_header("G", "Overdose — 1000mg metformin vs 500mg max for P001")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=3,
        required_task_access=3,
        patient_id="P001",
        llm_task_json=OVERDOSE_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


def run_scenario_h(writer, tree):
    """Contradiction: plan opens then closes the same door."""
    _print_scenario_header("H", "Contradictory Plan — opens and closes Ward B Room 3 door")
    _set_blackboard(
        writer,
        facility_emergency_detected=False,
        user_access_level=3,
        required_task_access=3,
        patient_id="",
        llm_task_json=CONTRADICTION_JSON,
        task_validation_error="",
        medication_safety_violation={},
        ambiguity_flags=[],
    )
    tree.tick()
    _print_result(tree)


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═" * 60)
    print("  RHA CONTROLLER AGENT — HRII Framework Demo")
    print("  8-Scenario Deterministic Behavior Tree Test")
    print("═" * 60)

    # Initialise blackboard (keep writer in scope for the full run)
    _bb_writer = initialise_blackboard()

    # Build and display the tree
    tree = create_consensus_tree()
    tree.setup(timeout=15)

    # Run all 8 scenarios in sequence
    run_scenario_a(_bb_writer, tree)
    run_scenario_b(_bb_writer, tree)
    run_scenario_c(_bb_writer, tree)
    run_scenario_d(_bb_writer, tree)
    run_scenario_e(_bb_writer, tree)
    run_scenario_f(_bb_writer, tree)
    run_scenario_g(_bb_writer, tree)
    run_scenario_h(_bb_writer, tree)

    print("\n" + "═" * 60)
    print("  All 8 scenarios complete.")
    print("═" * 60 + "\n")
