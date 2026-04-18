"""
medication_db.py
----------------
Mock patient registry and formulary database for the RHA medication
safety checks. In a real deployment this would be a HL7 FHIR-compliant
EHR query; here we use a static dict to keep the demo self-contained.

Public interface:
    validate_medication(patient_id, drug, dose_mg) -> (bool, str)
    get_patient(patient_id)                        -> dict | None
"""

from typing import Optional, Tuple

# ── Mock patient registry ────────────────────────────────────────────────────
# Keys: patient_id (str)
# Values: name, allergies, prescribed_meds (allowed drugs), max_doses (mg)
PATIENT_DB: dict = {
    "P001": {
        "name": "John Smith",
        "allergies": ["penicillin", "sulfa"],
        "prescribed_meds": ["metformin", "lisinopril"],
        "max_doses": {
            "metformin": 500,    # mg per administration
            "lisinopril": 10,
        },
    },
    "P002": {
        "name": "Jane Doe",
        "allergies": [],
        "prescribed_meds": ["aspirin", "atorvastatin"],
        "max_doses": {
            "aspirin": 100,
            "atorvastatin": 40,
        },
    },
    "P003": {
        "name": "Robert Lee",
        "allergies": ["penicillin"],
        "prescribed_meds": ["amoxicillin", "ibuprofen"],
        "max_doses": {
            "amoxicillin": 500,
            "ibuprofen": 400,
        },
    },
}


def get_patient(patient_id: str) -> Optional[dict]:
    """Return patient record or None if not found."""
    return PATIENT_DB.get(patient_id)


def validate_medication(
    patient_id: str,
    drug: str,
    dose_mg: float,
) -> Tuple[bool, str]:
    """
    Check whether administering `dose_mg` mg of `drug` to `patient_id`
    is safe according to the formulary.

    Returns:
        (True,  "OK")                if safe
        (False, "<reason string>")   if unsafe — reason describes violation
    """
    drug = drug.lower().strip()

    # ── 1. Patient must exist in the registry ────────────────────────
    patient = PATIENT_DB.get(patient_id)
    if patient is None:
        return (
            False,
            f"Patient '{patient_id}' not found in patient registry. "
            "Cannot verify medication safety.",
        )

    name = patient["name"]

    # ── 2. Drug must not be on the allergy list ───────────────────────
    for allergen in patient["allergies"]:
        if allergen.lower() in drug or drug in allergen.lower():
            return (
                False,
                f"ALLERGY ALERT — Patient {patient_id} ({name}) is allergic "
                f"to '{allergen}'. Requested drug '{drug}' is contraindicated.",
            )

    # ── 3. Drug must be in the patient's prescribed medications ───────
    prescribed = [m.lower() for m in patient["prescribed_meds"]]
    if drug not in prescribed:
        return (
            False,
            f"Drug '{drug}' is NOT in patient {patient_id} ({name})'s "
            f"prescribed medication list {patient['prescribed_meds']}.",
        )

    # ── 4. Dose must not exceed formulary maximum ─────────────────────
    max_dose = patient["max_doses"].get(drug)
    if max_dose is not None and dose_mg > max_dose:
        return (
            False,
            f"Dose {dose_mg}mg of '{drug}' exceeds the maximum {max_dose}mg "
            f"for patient {patient_id} ({name}).",
        )

    return (True, "OK")
