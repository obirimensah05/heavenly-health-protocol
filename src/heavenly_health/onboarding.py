"""Device-first onboarding decision tree and its rendered configuration.

The question order is the product: device -> tracking app -> destination ->
agent -> where the agent runs -> schedule -> permissions. MCP, Docker, and
remote access are opt-in extras at the very end, never prerequisites.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOCAL_MCP_URL = "http://127.0.0.1:8791/mcp"

# Question A: what device do you use?
DEVICES: dict[str, str] = {
    "apple_watch": "Apple Watch / iPhone",
    "fitbit_pixel": "Fitbit / Pixel Watch",
    "garmin": "Garmin",
    "whoop": "WHOOP",
    "oura": "Oura Ring",
    "android_other": "Other Android wearable",
}

# Question B: which app is the source of truth? Keys map to source adapters.
# status: "implemented" routes proceed today; "spec" routes are recorded and
# the user is told honestly that the adapter is not built yet.
SOURCE_APPS: dict[str, tuple[str, str]] = {
    "apple_health": ("Apple Health (synced with the Health Auto Export app)", "implemented"),
    "google_health": ("Google Health API (Fitbit / Pixel Watch data)", "implemented"),
    "garmin": ("Garmin Connect (requires Garmin developer approval)", "implemented"),
    "whoop": ("WHOOP app", "implemented"),
    "oura": ("Oura app", "implemented"),
    "health_connect": ("Android Health Connect", "spec"),
}

# Devices suggest their natural source apps, most likely first.
DEVICE_SOURCE_APPS: dict[str, tuple[str, ...]] = {
    "apple_watch": ("apple_health",),
    "fitbit_pixel": ("google_health", "health_connect"),
    "garmin": ("garmin", "apple_health", "google_health"),
    "whoop": ("whoop", "apple_health", "google_health"),
    "oura": ("oura", "apple_health", "google_health"),
    "android_other": ("health_connect", "google_health"),
}

# Question C: where should the agent-readable copy live?
DESTINATIONS: dict[str, tuple[str, str]] = {
    "supabase": ("Your own Supabase project (free tier works)", "implemented"),
    "obsidian": ("Obsidian vault", "spec"),
    "local_sqlite": ("Local second brain / SQLite", "spec"),
    "google_drive": ("Google Drive", "spec"),
    "icloud_drive": ("iCloud Drive", "spec"),
}

# Question D: which agent should read your health data?
AGENTS: dict[str, str] = {
    "claude_code": "Claude Code",
    "claude": "Claude (app / web)",
    "chatgpt": "ChatGPT",
    "codex": "Codex",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "perplexity": "Perplexity",
    "other": "Another MCP-compatible agent",
}

# Question E: where does that agent run?
AGENT_LOCATIONS: dict[str, str] = {
    "local": "On this computer",
    "cloud": "In the cloud (hosted agent)",
    "both": "Both local and cloud",
}

FREQUENCIES: dict[str, str] = {
    "daily": "Every day",
    "every_3_days": "Every 3 days",
    "weekly": "Once a week",
    "custom": "Custom",
}

ARRIVALS: dict[str, str] = {
    "morning": "Morning briefing",
    "evening": "Evening reflection",
    "custom": "Custom time",
}

_SHARED_METRICS = (
    "steps",
    "heart_rate",
    "resting_heart_rate",
    "heart_rate_variability",
    "sleep_analysis",
    "walking_running_distance",
    "active_energy",
    "oxygen_saturation",
    "respiratory_rate",
    "vo2_max",
)

# Metrics offered per implemented source adapter. Unimplemented sources grant
# nothing until their adapter exists.
SOURCE_METRICS: dict[str, tuple[str, ...]] = {
    "apple_health": _SHARED_METRICS + ("body_mass",),
    "google_health": _SHARED_METRICS + ("body_mass",),
    "garmin": _SHARED_METRICS + ("stress_level", "body_battery"),
    "whoop": (
        "heart_rate_variability",
        "resting_heart_rate",
        "oxygen_saturation",
        "sleep_analysis",
        "respiratory_rate",
        "active_energy",
    ),
    "oura": (
        "steps",
        "active_energy",
        "sleep_analysis",
        "heart_rate_variability",
        "resting_heart_rate",
        "respiratory_rate",
        "oxygen_saturation",
    ),
}

_METRIC_ORDER = _SHARED_METRICS + ("body_mass", "stress_level", "body_battery")


@dataclass(frozen=True)
class OnboardingAnswers:
    """Non-secret answers collected by the guided setup."""

    devices: tuple[str, ...]
    source_app: str
    destination: str
    agent: str
    agent_location: str
    frequency: str
    arrival: str
    briefing_time: str
    timezone: str

    def metrics(self) -> tuple[str, ...]:
        return metrics_for_sources((self.source_app,))


def metrics_for_sources(sources: tuple[str, ...]) -> tuple[str, ...]:
    """Return the ordered metric allowlist granted by implemented sources."""
    granted = {metric for source in sources for metric in SOURCE_METRICS.get(source, ())}
    return tuple(metric for metric in _METRIC_ORDER if metric in granted)


def render_runtime_env(
    answers: OnboardingAnswers,
    supabase_url: str = "",
    supabase_service_role_key: str = "",
    apple_delivery_table: str = "",
) -> str:
    """Render the owner-only runtime environment file for these answers.

    Empty storage values keep Heavenly in status-only mode until the user adds
    their Supabase project; nothing else is required to finish onboarding.
    """
    if answers.source_app == "apple_health" and not apple_delivery_table:
        apple_delivery_table = ""
    allowlist = ",".join(answers.metrics())
    lines = [
        "# Written by `heavenly setup`. Owner-only. Never commit this file.",
        f"SUPABASE_URL={supabase_url}",
        f"SUPABASE_SERVICE_ROLE_KEY={supabase_service_role_key}",
        "HEAVENLY_HEALTH_TABLE=heavenly_health_events",
        "HEAVENLY_RAW_HEALTH_TABLE=heavenly_health_raw_events",
        f"HEAVENLY_ALLOWED_METRICS={allowlist}",
        f"HEAVENLY_APPLE_HEALTH_DELIVERY_TABLE={apple_delivery_table}",
        "HEAVENLY_MCP_HOST=127.0.0.1",
        "HEAVENLY_MCP_PORT=8791",
    ]
    return "\n".join(lines) + "\n"


def answers_payload(answers: OnboardingAnswers) -> dict[str, object]:
    """Return the JSON-safe, secret-free record of the onboarding answers."""
    return {
        "devices": list(answers.devices),
        "source_app": answers.source_app,
        "destination": answers.destination,
        "agent": answers.agent,
        "agent_location": answers.agent_location,
        "schedule": {
            "frequency": answers.frequency,
            "arrival": answers.arrival,
            "time": answers.briefing_time,
            "timezone": answers.timezone,
        },
        "metrics": list(answers.metrics()),
    }


def connect_instructions(agent: str, remote: bool = False) -> tuple[str, ...]:
    """Plain-language steps to point the chosen agent at Heavenly."""
    if remote:
        return (
            "Your agent runs in the cloud, so it needs a protected remote URL.",
            "That is the one advanced step: see docs/deployment.md for the",
            "Cloudflare-protected setup, then give your agent that URL.",
            f"(Agents on this computer can already use {LOCAL_MCP_URL}.)",
        )
    if agent == "claude_code":
        return (
            "Run this once:",
            f"  claude mcp add --transport http heavenly {LOCAL_MCP_URL}",
            "Then ask Claude Code about your health data.",
        )
    label = AGENTS.get(agent, "your agent")
    return (
        f"In {label}, add a new MCP server (HTTP) with this URL:",
        f"  {LOCAL_MCP_URL}",
        "No client ID or client secret is needed for a local connection.",
    )


def write_owner_only(path: Path, content: str) -> None:
    """Atomically write an owner-only (0600) regular file, refusing symlinks."""
    if path.is_symlink():
        raise OSError(f"{path} must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def save_answers(path: Path, answers: OnboardingAnswers) -> None:
    """Persist the secret-free onboarding answers next to the runtime config."""
    write_owner_only(path, json.dumps(answers_payload(answers), indent=2, sort_keys=True) + "\n")


def default_answers_path() -> Path:
    return Path.home() / ".config" / "heavenly" / "onboarding.json"
