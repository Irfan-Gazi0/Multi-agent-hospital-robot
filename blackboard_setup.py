"""
blackboard_setup.py
-------------------
Initialises the py_trees Blackboard with every key used by the RHA
Controller Agent and sets safe, neutral default values.

IMPORTANT: The returned client object MUST be held in scope by the
caller for the entire lifetime of the program. py_trees removes key
registrations when a client is garbage-collected, which would make
the blackboard keys invisible to the behaviours.

Usage:
    from blackboard_setup import initialise_blackboard
    _bb_writer = initialise_blackboard()   # keep reference alive
"""

import py_trees


def initialise_blackboard() -> py_trees.blackboard.Client:
    """
    Creates a writer client, registers all 8 shared keys, and writes
    safe default values. Returns the client — the caller must keep it.
    """
    # Enable the activity stream so we can inspect blackboard history
    # when debugging (no-op if already enabled).
    py_trees.blackboard.Blackboard.enable_activity_stream(maximum_size=500)

    writer = py_trees.blackboard.Client(name="Initialiser")

    defaults = {
        "facility_emergency_detected": False,
        "user_access_level": 1,  # minimum access until authenticated
        "required_task_access": 1,
        "llm_task_json": "",
        "patient_id": "",
        "task_validation_error": "",
        "medication_safety_violation": {},
        "ambiguity_flags": [],
    }
    for key, value in defaults.items():
        writer.register_key(key=key, access=py_trees.common.Access.WRITE)
        setattr(writer, key, value)

    return writer
