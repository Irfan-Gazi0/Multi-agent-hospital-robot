"""
agents/access_agent.py
----------------------
Mock Access Control agent.

In a real deployment this would query the hospital IAM system (e.g. Active
Directory / OAuth2 token introspection) to resolve a bearer token or badge
scan to a role and access level.

Access levels:
  1 — Patient (read-only; no task dispatch)
  2 — Nurse   (basic robot tasks: navigate, carry)
  3 — Doctor / Pharmacist (full access: medication, diagnostics)
"""

import logging
import threading

logger = logging.getLogger("RHA.AccessAgent")

# Role registry: maps role name → (access_level, description)
ROLE_REGISTRY: dict[str, tuple[int, str]] = {
    "patient":     (1, "Patient — read-only access"),
    "nurse":       (2, "Nurse — basic task access"),
    "doctor":      (3, "Doctor — full clinical access"),
    "pharmacist":  (3, "Pharmacist — full clinical access"),
    "technician":  (2, "Technician — basic task access"),
    "admin":       (3, "Administrator — full system access"),
}

# Task registry: maps task name → minimum required access level
TASK_REGISTRY: dict[str, int] = {
    "fetch_medication":     3,
    "administer_injection": 3,
    "patient_transport":    2,
    "deliver_supplies":     2,
    "navigate":             1,
    "room_light_control":   1,
}


def resolve_access(role: str) -> tuple[int, str]:
    """
    Returns (access_level, description) for a given role string.
    Defaults to (1, 'Unknown role') if the role is not registered.
    """
    role_key = role.lower().strip()
    level, desc = ROLE_REGISTRY.get(role_key, (1, f"Unknown role '{role}'"))
    logger.info("AccessAgent: role='%s' → level=%d (%s)", role, level, desc)
    return level, desc


def resolve_task_access(task_name: str) -> int:
    """
    Returns the minimum access level required for the named task.
    Defaults to 3 (most restrictive) for unknown tasks.
    """
    task_key = task_name.lower().strip().replace(" ", "_")
    level = TASK_REGISTRY.get(task_key, 3)
    logger.info(
        "AccessAgent: task='%s' → required_level=%d", task_name, level
    )
    return level


def set_user_access(
    agent_state: dict,
    state_lock: threading.Lock,
    role: str,
    task_name: str,
) -> None:
    """
    Resolves role → level and task → required level, then writes both to
    the shared agent_state dict. Called directly in test scenarios.
    """
    user_level, _ = resolve_access(role)
    req_level      = resolve_task_access(task_name)
    with state_lock:
        agent_state["user_access_level"]   = user_level
        agent_state["required_task_access"] = req_level
