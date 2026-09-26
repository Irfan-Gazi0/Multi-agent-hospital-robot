"""
server.py
---------
HTTP bridge that exposes the RHA Controller Behavior Tree to external
clients (Unity simulation, dashboards, smoke tests).

Endpoints
~~~~~~~~~
POST /plan      Run a single tick of the BT against a supplied scene state.
GET  /health    Liveness probe. Returns the BT root name + node count.
GET  /          Returns the OpenAPI-style schema as plain JSON.

Request body to /plan
~~~~~~~~~~~~~~~~~~~~~
{
  "user_role":          "Doctor",       # or "Nurse" / "Patient" / ...
  "task_name":          "fetch_medication",
  "patient_id":         "P001",
  "facility_emergency": false,
  "llm_task_json":      "<JSON string>",   # optional, pre-built plan
  "command":            "Fetch ..."        # optional, NL → LLM (needs API key)
}

If `llm_task_json` is supplied it is used verbatim. If `command` is supplied
and no `llm_task_json` is set, the LLM agent is called to generate a plan.
At least one of the two must be present.

Response
~~~~~~~~
{
  "winner":   "ExecuteHRIITask",
  "priority": "P6",
  "blocked":  false,
  "reason":   "",
  "task_validation_error": "",
  "medication_safety_violation": {},
  "ambiguity_flags": [],
  "steps":    [...],          # only when winner == ExecuteHRIITask
  "log":      ["...", "..."]  # captured logging output for this tick
}

Run
~~~
    python3 server.py
    # → http://0.0.0.0:5005

The server is single-threaded on purpose: one BT instance, one blackboard,
sequential ticks. This matches how a real RHA controller would operate.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import py_trees
from flask import Flask, jsonify, request
from flask_cors import CORS

from agents.access_agent import resolve_access, resolve_task_access
from blackboard_setup import initialise_blackboard
from controller_tree import create_consensus_tree

# ── App + CORS (Unity Editor uses arbitrary origins during play) ─────────────
app = Flask(__name__)
CORS(app)

# ── Single shared BT instance, blackboard writer kept alive at module scope ──
_BB_WRITER = initialise_blackboard()
_TREE = create_consensus_tree()
_TREE.setup(timeout=15)

# Map a winning child name → priority label for friendlier responses.
_PRIORITY_LABEL: dict[str, str] = {
    "P1_Infrastructure_Emergency": "P1",
    "P2_Security_Override":        "P2",
    "P3_Task_Plan_Validation":     "P3",
    "P4_Medication_Safety":        "P4",
    "P5_Ambiguity_Detection":      "P5",
    "ExecuteHRIITask":             "P6",
}


# ── Logging capture: each tick collects framework logs into a buffer ─────────
class _BufferLogHandler(logging.Handler):
    """Captures emitted log records into a list for one /plan call."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.buffer: list[str] = []
        self.setFormatter(
            logging.Formatter("%(levelname)-8s %(name)s | %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append(self.format(record))


_root_logger = logging.getLogger("RHA")
_root_logger.setLevel(logging.DEBUG)
# Also keep the human-readable stdout stream for terminal users.
if not _root_logger.handlers:
    _stream = logging.StreamHandler()
    _stream.setFormatter(
        logging.Formatter("%(levelname)-8s %(name)s | %(message)s")
    )
    _root_logger.addHandler(_stream)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_winner(root: py_trees.behaviour.Behaviour) -> tuple[str, str]:
    """
    Returns (branch_name, priority_label) for the first SUCCESS child of the
    root selector, or ("(none)", "-") if every branch returned FAILURE.
    """
    for child in root.children:
        if child.status == py_trees.common.Status.SUCCESS:
            return child.name, _PRIORITY_LABEL.get(child.name, "?")
    return "(none)", "-"


def _winning_action_name(root: py_trees.behaviour.Behaviour) -> str:
    """For sequence branches, the last child is the action that fired."""
    for child in root.children:
        if child.status == py_trees.common.Status.SUCCESS:
            sub = getattr(child, "children", [])
            return sub[-1].__class__.__name__ if sub else child.__class__.__name__
    return ""


def _set_blackboard(**kwargs: Any) -> None:
    for key, value in kwargs.items():
        setattr(_BB_WRITER, key, value)


def _level(explicit: int | None, fallback) -> int:
    """Explicit 1-3 wins, else the looked-up level, else 1."""
    if isinstance(explicit, int) and 1 <= explicit <= 3:
        return explicit
    return fallback() if fallback else 1


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root_schema():
    return jsonify({
        "service": "RHA Controller HTTP bridge",
        "endpoints": {
            "GET  /health": "Liveness probe.",
            "POST /plan":   "Run one BT tick. Body: scene state + plan/command.",
        },
        "priority_branches": list(_PRIORITY_LABEL.keys()),
    })


@app.route("/health", methods=["GET"])
def health():
    root = _TREE.root
    return jsonify({
        "ok": True,
        "root": root.name,
        "branches": [c.name for c in root.children],
    })


@app.route("/plan", methods=["POST"])
def plan():
    payload = request.get_json(silent=True) or {}

    # ── Resolve scene state ──────────────────────────────────────────────
    user_role           = payload.get("user_role")
    user_access_level   = payload.get("user_access_level")
    task_name           = payload.get("task_name")
    required_task_access = payload.get("required_task_access")
    patient_id          = str(payload.get("patient_id", "") or "")
    facility_emergency  = bool(payload.get("facility_emergency", False))
    llm_task_json       = payload.get("llm_task_json")
    command             = payload.get("command")
    scene_state         = payload.get("scene_state")  # dict from Unity SceneObserver

    user_lvl = _level(user_access_level,
                      user_role and (lambda: resolve_access(user_role)[0]))
    req_lvl  = _level(required_task_access,
                      task_name and (lambda: resolve_task_access(task_name)))

    # ── Resolve task plan (explicit JSON wins; else call LLM) ────────────
    if not llm_task_json and command:
        try:
            from agents.llm_agent import call_llm
            scene_hint = {
                "patient_id": patient_id,
                "user_role":  user_role or f"L{user_lvl}",
            }
            if isinstance(scene_state, dict):
                scene_hint["world"] = scene_state
            llm_task_json = call_llm(command, json.dumps(scene_hint))
        except Exception as exc:  # broad: missing key, network, parse, etc.
            return jsonify({
                "error":  "LLM call failed",
                "detail": str(exc),
            }), 502

    if llm_task_json is None:
        llm_task_json = ""

    # ── Capture this tick's log output ────────────────────────────────────
    buffered = _BufferLogHandler()
    _root_logger.addHandler(buffered)
    try:
        _set_blackboard(
            facility_emergency_detected=facility_emergency,
            user_access_level=user_lvl,
            required_task_access=req_lvl,
            patient_id=patient_id,
            llm_task_json=llm_task_json,
            task_validation_error="",
            medication_safety_violation={},
            ambiguity_flags=[],
        )
        _TREE.tick()
    finally:
        _root_logger.removeHandler(buffered)

    # ── Build response ────────────────────────────────────────────────────
    root = _TREE.root
    winner_branch, priority = _find_winner(root)
    winner_action = _winning_action_name(root)

    # Read back the per-branch error keys via a fresh reader client.
    reader = py_trees.blackboard.Client(name="HTTPResponseReader")
    for key in (
        "task_validation_error",
        "medication_safety_violation",
        "ambiguity_flags",
    ):
        reader.register_key(key=key, access=py_trees.common.Access.READ)

    blocked = priority in {"P1", "P2", "P3", "P4", "P5"}
    reason  = ""
    if priority == "P1":
        reason = "Facility emergency — task halted."
    elif priority == "P2":
        reason = (
            f"Access denied: user level {user_lvl} < required {req_lvl}."
        )
    elif priority == "P3":
        reason = reader.task_validation_error
    elif priority == "P4":
        reason = (reader.medication_safety_violation or {}).get(
            "reason", "Medication safety violation."
        )
    elif priority == "P5":
        reason = "Ambiguous plan: " + " | ".join(reader.ambiguity_flags)

    parsed_all_steps: list[dict] = []
    if llm_task_json:
        try:
            parsed_all_steps = json.loads(llm_task_json).get("steps", [])
        except json.JSONDecodeError:
            pass
    steps = parsed_all_steps if priority == "P6" else []

    return jsonify({
        "winner":                       winner_action or winner_branch,
        "branch":                       winner_branch,
        "priority":                     priority,
        "blocked":                      blocked,
        "reason":                       reason,
        "task_validation_error":        reader.task_validation_error,
        "medication_safety_violation":  reader.medication_safety_violation,
        "ambiguity_flags":              list(reader.ambiguity_flags),
        "steps":                        steps,             # only when approved
        "evaluated_steps":              parsed_all_steps,  # always, for visualisation
        "llm_task_json":                llm_task_json,
        "log":                          buffered.buffer,
    })


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═" * 60)
    print("  RHA Controller HTTP bridge — listening on 0.0.0.0:5005")
    print("  Test:  curl http://localhost:5005/health")
    print("═" * 60 + "\n")
    # Werkzeug calls socket.getfqdn() on bind, which DNS-resolves the
    # hostname and stalls on machines without a configured FQDN. Patch
    # it to a fast no-op so binding to 0.0.0.0 is instant.
    import socket
    socket.getfqdn = lambda host="": host or "localhost"
    # threaded=False keeps the BT/blackboard single-writer.
    app.run(host="0.0.0.0", port=5005, threaded=False, debug=False)
