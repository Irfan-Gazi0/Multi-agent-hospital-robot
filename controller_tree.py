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

    branches = [
        ("P1_Infrastructure_Emergency", CheckFacilityEmergency, HaltAndAlertNurse),
        ("P2_Security_Override",        CheckAccessLevel,       InsufficientAccessMessage),
        ("P3_Task_Plan_Validation",     CheckInvalidTaskPlan,   InvalidPlanMessage),
        ("P4_Medication_Safety",        CheckMedicationSafety,  MedicationSafetyAlert),
        ("P5_Ambiguity_Detection",      CheckAmbiguousCommand,  AmbiguityResolutionRequest),
    ]
    children = []
    for name, cond, action in branches:
        seq = py_trees.composites.Sequence(name=name, memory=False)
        seq.add_children([cond(), action()])
        children.append(seq)

    # Root: Priority Selector (left = highest priority).
    # memory=False re-evaluates from P1 on every tick.
    root = py_trees.composites.Selector(name="RHA_Controller", memory=False)
    root.add_children(children + [ExecuteHRIITask()])

    tree = py_trees.trees.BehaviourTree(root=root)

    # Print ASCII routing diagram on startup
    print("\n" + "═" * 60)
    print("  RHA CONTROLLER — Behavior Tree Routing Hierarchy")
    print("  (left branch = highest priority)")
    print("═" * 60)
    print(py_trees.display.ascii_tree(root))
    print("═" * 60 + "\n")

    return tree
