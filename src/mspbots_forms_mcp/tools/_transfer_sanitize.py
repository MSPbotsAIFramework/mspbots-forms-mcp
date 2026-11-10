"""Pure-function helpers for cross-tenant form export/import (PRD-21397).

Isolated from transfer.py (the tool-registration module) so these can be
unit-tested without any FormsAPIClient/network dependency — same split as
_common.py's NO_TOKEN constant, just for logic instead of a literal.
"""

import copy
import re
from typing import Any

from .._json import error_envelope
from ._survey_walk import normalize_choice_source

# Export envelope format version. Bump this and add a new validate/transform
# path when the envelope shape changes (e.g. a future version carrying
# share/theme settings) — see Tech design SS5 "Backward compatibility".
EXPORT_SCHEMA_VERSION = 1

# Keys that mark a static choice option as tenant-scoped (PRD-21397 ticket:
# tenantId, haloClientId, rewstOrgId — e.g. Ginko's client_org options).
# Extend this set here for future integrations (Open questions, design doc).
TENANT_SCOPED_CHOICE_KEYS = {"tenantId", "haloClientId", "rewstOrgId"}

# Form-record keys that must never reach the import PATCH body even under
# the "copy the whole record" strategy (review round 2, must-fix #2):
#   - "status": the base record's draft/published/archived field (confirmed
#     to exist by forms.py's own status-filtered form_list and shares.py's
#     parallel status state machine) -- copying it from a published source
#     form would auto-publish the freshly created (currently-draft) form,
#     contradicting the ticket's "unpublished" requirement.
# Beyond that literal, the real field names for owner/sharing/publish-state
# metadata are unverified (forms.py:1-6's own disclaimer: built from a
# pasted API contract, never called against a real deployment) -- so rather
# than guess more literals and risk missing a variant, anything whose name
# looks owner/sharing/publish-shaped is excluded by substring match. Revisit
# once TC2 against a real form shows the actual key names.
_NON_TRANSFERABLE_SETTING_KEYS = frozenset({"id", "definition", "questions", "versions", "status"})
_NON_TRANSFERABLE_SETTING_PATTERN = re.compile(r"owner|shar|publish", re.IGNORECASE)


def is_transferable_setting(key: str) -> bool:
    """True if `key` is safe to carry from the source form record onto the
    import PATCH body (transfer.py's mspbots_forms_form_import)."""
    return key not in _NON_TRANSFERABLE_SETTING_KEYS and not _NON_TRANSFERABLE_SETTING_PATTERN.search(
        key
    )


class TransferError(Exception):
    """Mirrors FormsAPIError's to_envelope() shape for transfer-specific
    validation failures that happen before any network call is made."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)

    def to_envelope(self) -> str:
        return error_envelope(self.code, self.message, self.retryable)


def validate_export_version(payload: dict) -> None:
    """Raise TransferError if payload's export_version isn't one this
    server supports. Must be called before any create/patch call — a half
    import (shell form created, then refused) is worse than refusing up
    front (AC6)."""
    version = payload.get("export_version")
    if version != EXPORT_SCHEMA_VERSION:
        raise TransferError(
            "invalid_argument",
            f"Unsupported export_version: {version!r}. This server supports: {EXPORT_SCHEMA_VERSION}.",
            retryable=False,
        )


def validate_form_payload(payload: dict) -> None:
    """Raise TransferError if payload's "form" field is missing, empty, or
    the envelope itself carries truncated/omitted_fields markers (rework
    r2, FIND1: dump_json_capped's generic cap used to shed the whole "form"
    field for an oversized form, and import proceeded anyway, creating an
    empty "Untitled form" with no error). Must be called before any
    create/patch call, same "never half-import" principle AC6 already
    states for an unsupported export_version (validate_export_version)."""
    if payload.get("truncated") or payload.get("omitted_fields"):
        raise TransferError(
            "invalid_argument",
            "export_payload is truncated/incomplete (omitted: "
            f"{payload.get('omitted_fields') or ['form']}). Re-export "
            "without truncation and try again.",
            retryable=False,
        )
    form = payload.get("form")
    if not isinstance(form, dict) or not form:
        raise TransferError(
            "invalid_argument",
            "export_payload.form is missing or empty.",
            retryable=False,
        )


def _field_label(node: dict) -> str:
    """Best-effort human-readable name for a report entry. SurveyJS-shaped
    question dicts carry "name" (the generated q1/q2 id) and/or "title" (the
    friendly label) — prefer name since it's stable and always present on a
    real question node; fall back to title, then a generic placeholder."""
    return node.get("name") or node.get("title") or "<unnamed field>"


def strip_tenant_specific(definition: dict) -> tuple[dict, list[dict]]:
    """Recursively walk a form definition tree (generic walk, not assuming
    a fixed pages[].elements[] shape — the SurveyJS shape this server
    produces is itself unverified, see Tech design SS Option analysis),
    removing anything tenant-specific and returning a report of what was
    removed.

    Three cases, each independently detected on every dict in the tree:
      - a dict with "choicesByUrl" -> that key is dropped and "choices" (if
        present) is cleared; reported as "data-source-bound (choicesByUrl)".
      - a dict with "choicesSource" -> that key is dropped and "choices" (if
        present) is cleared; reported as "data-source-bound (choicesSource)".
        Unconditional, regardless of whether any item carries a
        tenant-scoped key (rework r2, COV2): choicesSource is also Forms'
        wire shape for a live/external ("mcp"/"api") binding with no fixed
        item shape, and by construction anything using choicesSource
        instead of a plain "choices" list is sourced from somewhere outside
        the static definition -- not portable, static data, the same
        category choicesByUrl already gets flagged for.
      - a dict with a plain, non-sourced top-level "choices" list whose
        items carry any key in TENANT_SCOPED_CHOICE_KEYS -> the options are
        cleared; reported as "static tenant-scoped options (<matched
        keys>)". Only reached when neither of the two data-source cases
        above already matched.

    Returns a deep copy — the caller's original definition is never mutated.
    """
    working = copy.deepcopy(definition)
    report: list[dict] = []
    _walk(working, report)
    return working, report


def _choice_keys(choice: dict) -> set[str]:
    """Keys that could mark `choice` as tenant-scoped: the choice's own
    top-level keys (the shape this module originally assumed, still used by
    this ticket's own TC5/TC6 fixtures), plus a nested "attrs" dict's keys
    — the real shape of Ginko's client_org question (choicesSource.items[*]
    .attrs), live-tested and confirmed on sibling ticket PRD-21379
    (test_form_get_field_id_handles_choices_source_shape)."""
    keys = set(choice.keys())
    attrs = choice.get("attrs")
    if isinstance(attrs, dict):
        keys |= attrs.keys()
    return keys


def _walk(node: Any, report: list[dict]) -> None:
    if isinstance(node, dict):
        if "choicesByUrl" in node:
            node.pop("choicesByUrl", None)
            node["choices"] = []
            report.append(
                {"field": _field_label(node), "reason": "data-source-bound (choicesByUrl)"}
            )

        elif "choicesSource" in node:
            # Unconditional (rework r2, COV2) -- see strip_tenant_specific's
            # docstring: any choicesSource-shaped question is
            # data-source-bound by construction, the same wholesale-clear
            # treatment as choicesByUrl above, regardless of whether any
            # item carries a tenant-scoped key. Still surface the matched
            # key names in the reason when present (TC11, round 2) so the
            # import report keeps naming exactly what was tenant-scoped,
            # not just that the question was data-source-bound.
            candidate = normalize_choice_source(node)
            matched_keys: set[str] = set()
            items = candidate.get("choices")
            if isinstance(items, list):
                for choice in items:
                    if isinstance(choice, dict):
                        matched_keys |= TENANT_SCOPED_CHOICE_KEYS & _choice_keys(choice)

            node.pop("choicesSource", None)
            node["choices"] = []
            reason = "data-source-bound (choicesSource)"
            if matched_keys:
                reason += f"; tenant-scoped: {', '.join(sorted(matched_keys))}"
            report.append({"field": _field_label(node), "reason": reason})

        else:
            # A plain, non-sourced static-choice question's options live
            # under a top-level "choices" list. Only cleared when an item
            # carries a tenant-scoped key (e.g. Ginko's client_org options
            # before choicesSource adoption); an ordinary static list (TC6)
            # is left untouched.
            choices = node.get("choices")
            if isinstance(choices, list):
                matched_keys: set[str] = set()
                for choice in choices:
                    if isinstance(choice, dict):
                        matched_keys |= TENANT_SCOPED_CHOICE_KEYS & _choice_keys(choice)
                if matched_keys:
                    node["choices"] = []
                    report.append(
                        {
                            "field": _field_label(node),
                            "reason": f"static tenant-scoped options ({', '.join(sorted(matched_keys))})",
                        }
                    )

        for value in node.values():
            _walk(value, report)

    elif isinstance(node, list):
        for item in node:
            _walk(item, report)
