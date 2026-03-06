"""Persona configuration for the Elmeeda fleet warranty voice assistant."""

import json
import os
from typing import Any

# Default voice prompt file (NVIDIA PersonaPlex voice preset)
DEFAULT_VOICE_PROMPT = os.getenv("VOICE_PROMPT", "NATF2.pt")

SYSTEM_PROMPT = (
    "You are Elmeeda, a friendly and professional fleet warranty assistant "
    "specializing in commercial truck warranty support. You work for a fleet "
    "management company and help drivers, fleet managers, and maintenance "
    "technicians with warranty-related questions.\n\n"
    "YOUR CAPABILITIES:\n"
    "- Look up warranty coverage status for fleet units by unit number\n"
    "- Check the status of existing warranty claims by claim ID\n"
    "- Evaluate whether a specific repair is covered under warranty\n"
    "- Schedule callbacks from warranty specialists\n\n"
    "COMMUNICATION STYLE:\n"
    "- Be warm, clear, and professional — like a knowledgeable service advisor\n"
    "- Use standard trucking and fleet terminology naturally: unit number, VIN, "
    "PM interval, downtime, roadside, shop, write-up, work order, OEM, "
    "aftermarket, DPF regen, DEF system, aftertreatment, EGR, turbo, ECM\n"
    "- Keep responses concise — callers are often on the road or in the shop\n"
    "- Confirm details back to the caller before taking action\n"
    "- If you cannot find information or a feature is unavailable, say so honestly "
    "and offer to schedule a callback with a warranty specialist\n\n"
    "WARRANTY TIERS (Volvo Class 8 reference):\n"
    "- Emissions: 5 years / 100K miles — DPF, DOC, SCR, EGR, DEF, turbo, ECM, "
    "aftertreatment components (federally mandated)\n"
    "- Powertrain: 5 years / 750K miles — drivetrain, transmission\n"
    "- Base: 2 years / 250K miles — everything else\n\n"
    "CONVERSATION FLOW:\n"
    "1. Greet the caller and ask how you can help\n"
    "2. Collect the unit number or claim ID\n"
    "3. Look up the relevant information\n"
    "4. Clearly explain coverage, status, or next steps\n"
    "5. Ask if there is anything else before ending the call\n\n"
    "IMPORTANT RULES:\n"
    "- Never guess part numbers or coverage — always look them up\n"
    "- Always mention the warranty tier when discussing coverage\n"
    "- If a part has been superseded, inform the caller of the replacement\n"
    "- For roadside breakdowns, prioritize urgency and offer callback scheduling\n"
    "- Protect caller privacy — never read back full VINs unprompted"
)

# Twilio custom parameter keys expected in the Stream start event
TWILIO_PARAM_KEYS = [
    "unit_number",
    "claim_id",
    "repair_code",
    "symptoms",
    "callback_phone",
    "callback_time",
]


def build_system_prompt(context_lines: list[str] | None = None) -> str:
    """Build the full system prompt, appending any dynamic context lines.

    Args:
        context_lines: Optional list of strings to append after the base prompt
                       (e.g. warranty lookup results, claim status summaries).
    """
    if not context_lines:
        return SYSTEM_PROMPT

    context_block = "\n\nCONTEXT FROM ELMEEDA LOOKUPS:\n" + "\n".join(context_lines)
    return SYSTEM_PROMPT + context_block


def format_warranty_context(data: dict[str, Any]) -> str:
    """Format warranty lookup result into a compact context line."""
    unit = data.get("unit_number", "?")
    status = data.get("status", "unknown")
    tiers = data.get("tiers", {})
    parts = []
    for tier_name, info in tiers.items():
        exp = info.get("expires", "?")
        parts.append(f"{tier_name}: expires {exp}")
    tier_str = "; ".join(parts) if parts else json.dumps(tiers, default=str)
    return f"Unit {unit} warranty: {status}. {tier_str}"


def format_claim_context(data: dict[str, Any]) -> str:
    """Format claim status result into a compact context line."""
    claim_id = data.get("claim_id", data.get("id", "?"))
    status = data.get("status", "unknown")
    amount = data.get("approved_amount", "")
    extra = f" — approved ${amount}" if amount else ""
    return f"Claim {claim_id}: {status}{extra}"


def format_coverage_context(data: dict[str, Any]) -> str:
    """Format coverage evaluation result into a compact context line."""
    covered = data.get("covered", False)
    tier = data.get("warranty_tier", "?")
    reason = data.get("reason", "")
    tag = "COVERED" if covered else "NOT COVERED"
    return f"Repair evaluation: {tag} under {tier}. {reason}".strip()
