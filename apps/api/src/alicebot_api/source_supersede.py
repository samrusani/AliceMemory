"""Opt-in source replacement policy. No schema or capture identity changes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
import unicodedata

from alicebot_api.vnext_brain import SENSITIVITY_RANK
from alicebot_api.vnext_project_scope import source_project_scope

UNSUPPORTED_SUPERSEDE = "Source replacement is supported only by the SQLite Markdown importer"


@dataclass(frozen=True, slots=True)
class SupersedePolicy:
    mode: str = "off"
    allow_looser_classification: bool = False
    dry_run: bool = False

    def __post_init__(self):
        if self.mode not in {"off", "by_path"}:
            raise ValueError("Unknown source replacement policy")

    @classmethod
    def off(cls):
        return cls()

    @classmethod
    def by_path(cls, *, allow_looser_classification=False, dry_run=False):
        return cls("by_path", allow_looser_classification, dry_run)


def eligible_source(row):
    metadata = row.get("metadata_json") or {}
    return (row.get("source_type") == "markdown"
            and row.get("connector_name") == "markdown_folder"
            and bool(row.get("raw_path"))
            and metadata.get("generated_by") != "agent"
            and not metadata.get("agent_id"))


def classification_refusal(matches, *, domain, sensitivity, project_scope, allow_looser=False):
    from alicebot_api.vnext_memory_commit import SENSITIVE_DOMAINS
    for row in matches:
        if tuple(sorted(source_project_scope(row))) != tuple(sorted(project_scope)):
            return "different_project_scope"
    for row in matches:
        prior_domain = str(row.get("domain") or "unknown")
        prior_sensitivity = str(row.get("sensitivity") or "unknown")
        looser_domain = domain != prior_domain and (
            prior_domain in SENSITIVE_DOMAINS or prior_domain != "unknown")
        looser_sensitivity = SENSITIVITY_RANK[sensitivity] < SENSITIVITY_RANK[prior_sensitivity]
        if not allow_looser and (looser_domain or looser_sensitivity):
            return "looser_classification"
    return None


def printed_source_label(value):
    from alicebot_api.vnext_capture import _printed_label
    text = str(value or "")
    if PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute():
        text = PureWindowsPath(text).name if "\\" in text else PurePosixPath(text).name
    text = _printed_label(text)
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
        for char in text
    )
