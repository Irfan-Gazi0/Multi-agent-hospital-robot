"""
controller_tree.py
------------------
Assembles the RHA Controller Agent Behavior Tree.

Tree topology (Priority Selector — left child wins):

  RHA_Controller [Selector, memory=False]
  ├── P1_Infrastructure_Emergency [Sequence, memory=False]
  │   ├── CheckFacilityEmergency
  │   └── HaltAndAlertNurse
  ├── P2_Security_Override [Sequence, memory=False]
  │   ├── CheckAccessLevel
  │   └── InsufficientAccessMessage
  ├── P3_Task_Plan_Validation [Sequence, memory=False]
  │   ├── CheckInvalidTaskPlan
  │   └── InvalidPlanMessage
  ├── P4_Medication_Safety [Sequence, memory=False]
  │   ├── CheckMedicationSafety
  │   └── MedicationSafetyAlert
  ├── P5_Ambiguity_Detection [Sequence, memory=False]
  │   ├── CheckAmbiguousCommand
  │   └── AmbiguityResolutionRequest
  └── ExecuteHRIITask

memory=False on every node ensures the tree re-evaluates from P1 on
EVERY tick — a facility emergency can interrupt even an in-flight task.
"""

import py_trees

from behaviors import (
    CheckFacilityEmergency,
    HaltAndAlertNurse,
    CheckAccessLevel,
    InsufficientAccessMessage,
    CheckInvalidTaskPlan,
    InvalidPlanMessage,
    CheckMedicationSafety,
    MedicationSafetyAlert,
    CheckAmbiguousCommand,
    AmbiguityResolutionRequest,
    ExecuteHRIITask,
)


def create_consensus_tree() -> py_trees.trees.BehaviourTree:
    """
    Builds and returns the fully configured RHA Controller Behavior Tree.
    The ASCII representation is printed to stdout so the routing hierarchy
    is visible on startup.
    """

    # ── Priority 1: Infrastructure Emergency ─────────────────────────
    p1_infrastructure = py_trees.composites.Sequence(
        name="P1_Infrastructure_Emergency",
        memory=False,
    )
    p1_infrastructure.add_children([
        CheckFacilityEmergency(),
        HaltAndAlertNurse(),
    ])

    # ── Priority 2: Security / Access Control ─────────────────────────
    p2_security = py_trees.composites.Sequence(
        name="P2_Security_Override",
        memory=False,
    )
    p2_security.add_children([
        CheckAccessLevel(),
        InsufficientAccessMessage(),
    ])

    # ── Priority 3: Task Plan Validation ──────────────────────────────
    p3_validation = py_trees.composites.Sequence(
        name="P3_Task_Plan_Validation",
        memory=False,
    )
    p3_validation.add_children([
        CheckInvalidTaskPlan(),
        InvalidPlanMessage(),
    ])

    # ── Priority 4: Medication Safety ─────────────────────────────────
    p4_medication = py_trees.composites.Sequence(
        name="P4_Medication_Safety",
        memory=False,
    )
    p4_medication.add_children([
        CheckMedicationSafety(),
        MedicationSafetyAlert(),
    ])

    # ── Priority 5: Ambiguity / Contradiction Detection ───────────────
    p5_ambiguity = py_trees.composites.Sequence(
        name="P5_Ambiguity_Detection",
        memory=False,
    )
    p5_ambiguity.add_children([
        CheckAmbiguousCommand(),
        AmbiguityResolutionRequest(),
    ])

    # ── Priority 6: HRII Task Execution ───────────────────────────────
    p6_execution = ExecuteHRIITask()

    # ── Root: Priority Selector (left = highest priority) ─────────────
    root = py_trees.composites.Selector(
        name="RHA_Controller",
        memory=False,  # CRITICAL: re-evaluates from P1 on every tick
    )
    root.add_children([
        p1_infrastructure,
        p2_security,
        p3_validation,
        p4_medication,
        p5_ambiguity,
        p6_execution,
    ])

    tree = py_trees.trees.BehaviourTree(root=root)

    # Print ASCII routing diagram on startup
    print("\n" + "═" * 60)
    print("  RHA CONTROLLER — Behavior Tree Routing Hierarchy")
    print("  (left branch = highest priority)")
    print("═" * 60)
    try:
        # py_trees 2.x: ascii_tree returns a string
        print(py_trees.display.ascii_tree(root))
    except AttributeError:
        # Fallback for versions that expose print_ascii_tree directly
        py_trees.display.print_ascii_tree(root)
    print("═" * 60 + "\n")

    return tree
