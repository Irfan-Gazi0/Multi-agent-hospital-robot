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

    # ── Priority 1 key ────────────────────────────────────────────────
    writer.register_key(
        key="facility_emergency_detected",
        access=py_trees.common.Access.WRITE,
    )

    # ── Priority 2 keys ───────────────────────────────────────────────
    writer.register_key(
        key="user_access_level",
        access=py_trees.common.Access.WRITE,
    )
    writer.register_key(
        key="required_task_access",
        access=py_trees.common.Access.WRITE,
    )

    # ── Priority 3–6 shared task key ─────────────────────────────────
    writer.register_key(
        key="llm_task_json",
        access=py_trees.common.Access.WRITE,
    )

    # ── Priority 4 medication context ────────────────────────────────
    writer.register_key(
        key="patient_id",
        access=py_trees.common.Access.WRITE,
    )

    # ── Error detail keys (written by condition checks, read by actions) ──
    writer.register_key(
        key="task_validation_error",
        access=py_trees.common.Access.WRITE,
    )
    writer.register_key(
        key="medication_safety_violation",
        access=py_trees.common.Access.WRITE,
    )
    writer.register_key(
        key="ambiguity_flags",
        access=py_trees.common.Access.WRITE,
    )

    # ── Write safe defaults ────────────────────────────────────────────
    writer.facility_emergency_detected = False
    writer.user_access_level = 1          # minimum access until authenticated
    writer.required_task_access = 1
    writer.llm_task_json = ""
    writer.patient_id = ""
    writer.task_validation_error = ""
    writer.medication_safety_violation = {}
    writer.ambiguity_flags = []

    return writer
