"""
user_memory.py — Persistent memory for Finance Advisor
Stores user profiles and analysis history as JSON.
Structure is intentionally SQLite-compatible for future migration.
"""

import json
import os
from datetime import datetime

MEMORY_FILE = os.path.join(os.path.dirname(__file__), '..', 'memory', 'user_profile.json')

def _load_raw() -> dict:
    """Load the raw JSON file. Returns empty structure if file doesn't exist."""
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    if not os.path.exists(MEMORY_FILE):
        return {"profile": {}, "analyses": []}
    with open(MEMORY_FILE, 'r') as f:
        return json.load(f)

def _save_raw(data: dict):
    """Write the full data structure back to disk."""
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def save_profile(answers: dict):
    """
    Save or update the user's profile from their intake question answers.
    answers: dict of {question_id: answer_value}
    """
    data = _load_raw()
    data["profile"].update(answers)
    data["profile"]["last_updated"] = datetime.now().isoformat()
    _save_raw(data)

def load_profile() -> dict:
    """Return the stored user profile, or empty dict if none exists."""
    return _load_raw().get("profile", {})

def save_analysis(result: dict, file_names: list[str]):
    """
    Append a completed analysis to history with timestamp and source files.
    result: the full analysis dict from analysis_agent
    file_names: list of CSV filenames that were uploaded
    """
    data = _load_raw()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "source_files": file_names,
        "result": result
    }
    data["analyses"].append(entry)
    # Keep last 12 analyses to avoid unbounded growth
    data["analyses"] = data["analyses"][-12:]
    _save_raw(data)

def load_analyses() -> list[dict]:
    """Return list of past analyses, most recent first."""
    return list(reversed(_load_raw().get("analyses", [])))

def has_profile() -> bool:
    """Quick check — does a profile exist yet?"""
    profile = load_profile()
    return bool(profile and len(profile) > 1)  # more than just last_updated

def get_profile_context() -> str:
    """
    Returns a plain-text summary of the user profile for injecting into prompts.
    This is what gets passed to analysis_agent to personalize the analysis.
    """
    profile = load_profile()
    if not profile:
        return ""

    lines = ["USER PROFILE (from previous sessions):"]
    skip = {"last_updated"}
    for key, value in profile.items():
        if key in skip:
            continue
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            lines.append(f"  {label}: {', '.join(value)}")
        else:
            lines.append(f"  {label}: {value}")
    return "\n".join(lines)
