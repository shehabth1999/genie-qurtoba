"""Cash-SYS client management tools for AI Studio workflows."""
import random
from typing import Optional, Dict, Any

from modules.aistudio.tools import tool


def _generate_client_code() -> str:
    """8-digit numeric code, first digit never 0."""
    return str(random.randint(10_000_000, 99_999_999))


@tool(
    name="cash_sys_create_and_activate",
    display_name="Create Cash-SYS Client & Activate Trial",
    description=(
        "Use this tool to register a new client in the Cash-SYS platform and immediately "
        "activate their 14-day free trial in a single step. "
        "Before calling this tool, you MUST have collected ALL of the following: "
        "1) The client's full name, "
        "2) The company or shop name, "
        "3) An Egyptian mobile number — exactly 11 digits and must start with 01 (e.g. 01012345678). "
        "If the tool returns success=False, report the exact error to the user and ask them to "
        "provide corrected information before calling this tool again. "
        "If account creation fails the trial activation is skipped automatically. "
        "On success, returns the client ID, trial status, and login credentials "
        "(phone + password) — the password is only shown once, save it immediately. "
        "Do NOT call this tool more than once for the same client."
    ),
    category="cash_sys",
    requires_auth=True,
)
def cash_sys_create_and_activate(
    context,
    name: str,
    company: str,
    phone: str,
    district: Optional[str] = None,
    governorate: Optional[str] = None,
    address: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    from qurtoba.utils_sync import (
        create_cash_sys_client as _create,
        activate_cash_sys_trial as _activate,
    )

    base_data = {"name": name, "company": company, "phone": phone}
    if district:
        base_data["district"] = district
    if governorate:
        base_data["governorate"] = governorate
    if address:
        base_data["address"] = address
    if notes:
        base_data["notes"] = notes

    # Retry up to 3 times with a fresh code if the server rejects the code as duplicate.
    client_data, err = None, None
    for _ in range(3):
        try:
            client_data, err = _create({**base_data, "code": _generate_client_code()})
        except Exception as e:
            return {"success": False, "step": "create", "error": str(e)}

        if not err:
            break  # success

        # Only retry on code-collision errors; all other errors are fatal.
        err_lower = err.lower()
        if "code" not in err_lower and "unique" not in err_lower and "duplicate" not in err_lower:
            return {"success": False, "step": "create", "error": err}

    if err:
        return {"success": False, "step": "create", "error": err}

    client_id = client_data["id"]

    try:
        trial_data, err = _activate(client_id)
        if err:
            return {"success": False, "step": "activate", "error": err, "client_id": client_id}
    except Exception as e:
        return {"success": False, "step": "activate", "error": str(e), "client_id": client_id}

    result: Dict[str, Any] = {
        "success": True,
        "client_id": client_id,
        "trial_status": trial_data.get("trial_status"),
        "account_created": trial_data.get("account_created", False),
    }
    if trial_data.get("account_created"):
        result["phone"] = trial_data.get("phone")
        result["password"] = trial_data.get("password")
    return result
