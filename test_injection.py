import py_trees
from blackboard_setup import initialise_blackboard
from controller_tree import create_consensus_tree

INJECTION_JSON = """{
  "task": "Fetch medication",
  "patient_id": "P001",
  "steps": [
    {"skill": "robot/navigate", "params": {"destination": "Pharmacy"}},
    {"skill": "robot/pick", "params": {"item": "aspirin", "medication_type": "pain reliever", "dose_mg": 2000}},
    {"skill": "robot/place", "params": {"item": "aspirin", "patient_id": "P001"}}
  ]
}"""

writer = initialise_blackboard()
root = create_consensus_tree()

writer.facility_emergency_detected = False
writer.user_access_level = 3
writer.required_task_access = 3
writer.llm_task_json = INJECTION_JSON
writer.patient_id = "P001"
writer.task_validation_error = ""
writer.medication_safety_violation = {}
writer.ambiguity_flags = []

root.tick()


def find_winner(node):
    if node.status == py_trees.common.Status.SUCCESS:
        if not hasattr(node, "children") or not node.children:
            return node.name
        for child in node.children:
            r = find_winner(child)
            if r:
                return r
    return None


print(f"\n>>> Root status : {root.root.status}")
print(f">>> Winning branch: {find_winner(root.root)}")
