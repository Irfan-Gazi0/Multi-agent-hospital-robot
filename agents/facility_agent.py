"""
agents/facility_agent.py
------------------------
Mock ward-level Facility Monitor agent.

In a real deployment this would subscribe to:
  • Camera-based fall-detection CV models
  • Door / bed-exit pressure sensors
  • Fire/smoke detector feeds
  • Code-blue panic buttons

Here we simulate a configurable event feed that writes the
facility_emergency_detected flag to a shared state dict. The
test_runner.py and live mode call set_emergency() directly to
inject scenarios.
"""

import logging
import threading
import time

logger = logging.getLogger("RHA.FacilityAgent")

# Structured event payload written on emergency
FALL_EVENT = {
    "event":    "patient_fall",
    "room":     "Room 2",
    "severity": "critical",
    "source":   "ward_camera_node_02",
}


def facility_agent_loop(
    agent_state: dict,
    state_lock: threading.Lock,
    stop_event: threading.Event,
    poll_interval_s: float = 0.25,
) -> None:
    """
    Background thread: polls simulated ward sensors at `poll_interval_s`
    and updates agent_state["facility_emergency_detected"].

    The emergency flag is sticky — once set it remains True until
    explicitly cleared by medical staff (call clear_emergency()).
    """
    logger.info("FacilityAgent: started (poll_interval=%.2fs)", poll_interval_s)
    while not stop_event.is_set():
        # In live mode the flag is set externally via set_emergency().
        # This loop just keeps the thread alive for the demo.
        stop_event.wait(timeout=poll_interval_s)
    logger.info("FacilityAgent: stopped")


def set_emergency(
    agent_state: dict,
    state_lock: threading.Lock,
    active: bool = True,
) -> None:
    """
    Inject or clear an emergency event. Called directly in test scenarios.
    """
    with state_lock:
        agent_state["facility_emergency_detected"] = active
        if active:
            agent_state["_facility_event"] = FALL_EVENT
            logger.warning(
                "FacilityAgent: [EVENT] %s in %s (severity=%s)",
                FALL_EVENT["event"], FALL_EVENT["room"], FALL_EVENT["severity"],
            )
        else:
            agent_state["_facility_event"] = {}
