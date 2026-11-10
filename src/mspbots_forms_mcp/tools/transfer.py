"""Transfer tools: export a form's definition to a file, import it into
another tenant (PRD-21397).

A form built and tested in one tenant can be copied into another tenant's
Forms exactly, without hand-rebuilding it: export packages the full form
record (via the existing read path) into a versioned envelope; import walks
that envelope's definition, strips/flags anything tenant-specific (data-
source-bound choicesByUrl fields, static choices carrying tenant-scoped
identifiers), and recreates the form as a new, unpublished draft.

Both tools are pure composition of existing, already-tenant-isolated
primitives (GET/POST/PATCH /surveys) — see Tech design SS "Conclusion" in
docs/tickets/PRD-21397.md. Each tool call still carries exactly one
tenant's credentials (server.py's per-request contextvar); there is no step
where this server holds two tenants' credentials at once, so the admin/agent
carries the exported JSON across the export and import calls.
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import dump_json, dump_json_capped, error_envelope
from ..api_client import FormsAPIClient, FormsAPIError
from ._common import NO_TOKEN
from ._transfer_sanitize import (
    EXPORT_SCHEMA_VERSION,
    TransferError,
    is_transferable_setting,
    strip_tenant_specific,
    validate_export_version,
    validate_form_payload,
)


def register(mcp: FastMCP, client_factory: Callable[[], FormsAPIClient | None]) -> None:
    @mcp.tool()
    async def mspbots_forms_form_export(
        form_id: Annotated[str, Field(description="Required form ID to export.")],
    ) -> str:
        """Export a form's full definition to a portable JSON file, to copy
        it into another tenant via mspbots_forms_form_import.

        Carries the complete form record verbatim (definition, questions,
        versions) so the import matches field-for-field. Does NOT carry
        responses, the original form ID, owners/sharing, or trigger links.

        Export under the source tenant's credentials, then pass the JSON to
        mspbots_forms_form_import under the destination tenant's (e.g.
        after Switch Tenant) — never both at once.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            form = await client.get(
                f"/surveys/{form_id}", params={"include": "definition,questions,versions"}
            )
        except FormsAPIError as e:
            return e.to_envelope()
        # FIND1 (rework r2): dump_json_capped's generic cap sheds the whole
        # dict-shaped "form" field for an oversized form -- this envelope
        # is saved to a file for a later mspbots_forms_form_import, never
        # read as agent-facing conversation context, so the token-economy
        # cap every other tool in this server uses is the wrong default
        # here. See dump_json's docstring (_json.py).
        return dump_json(
            {
                "export_version": EXPORT_SCHEMA_VERSION,
                "source_form_id": form_id,
                "form": form,
            }
        )

    @mcp.tool()
    async def mspbots_forms_form_import(
        export_payload: Annotated[
            dict,
            Field(
                description=(
                    "Required — the object produced by mspbots_forms_form_export "
                    "(or an equivalent hand-built one with the same export_version "
                    "and form shape)."
                )
            ),
        ],
        title_suffix: Annotated[
            str,
            Field(
                description=(
                    'Optional suffix appended to the source title for the new '
                    'form\'s title (default " (imported)"). Exposed as a '
                    "parameter so a re-import after a naming collision can use a "
                    "different suffix without any code change."
                )
            ),
        ] = " (imported)",
    ) -> str:
        """Import a form definition exported by mspbots_forms_form_export,
        creating a new, unpublished draft form in the current tenant.

        Questions bound to a data source (choicesByUrl) or carrying a
        tenant-scoped identifier (e.g. tenantId, haloClientId, rewstOrgId)
        are imported UNBOUND and listed in the returned report for the
        admin to reconnect manually. Responses are never carried over.

        Refuses outright (no form created) if export_payload's
        export_version is unsupported — never a partial import.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            validate_export_version(export_payload)
            # FIND1 (rework r2): refuse a structurally incomplete "form"
            # (missing, empty, or carrying truncated/omitted_fields
            # markers) outright -- never silently create an empty
            # "Untitled form" from it.
            validate_form_payload(export_payload)
        except TransferError as e:
            return e.to_envelope()

        form = export_payload["form"]
        title = form.get("title") or "Untitled form"
        definition = form.get("definition") or {}
        new_title = f"{title}{title_suffix}"

        stripped_definition, report = strip_tenant_specific(definition)
        # COV1 (rework r2): the source definition's own top-level "title"
        # (a standard SurveyJS root-level property, independent of the
        # wrapping form record's "title") must also be overridden to the
        # suffixed new title -- otherwise this same PATCH call's nested
        # definition.title silently overrides the outer "title" key on the
        # backend, and the " (imported)" suffix reverts once persisted even
        # though this tool's own echoed response already said the right
        # title.
        if "title" in stripped_definition:
            stripped_definition["title"] = new_title

        # PRD-21397 AC2 (settings fidelity, e.g. showProgressBar, "requires
        # Microsoft 365 sign-in", thank-you message): `form` is the raw
        # upstream record (export deliberately keeps it verbatim -- Tech
        # design SS "Conclusion"), so every form-level setting it carries is
        # a sibling key, not nested inside `definition`. Build the PATCH body
        # from that whole record rather than enumerating settings by name,
        # excluding identity/content keys replaced elsewhere ("id",
        # "definition", "questions", "versions") plus anything
        # status/owner/sharing-shaped (review round 2, must-fix #2):
        # copying the source's "status" or a sharing/ownership field onto
        # the freshly created (currently-draft) form risks auto-publishing
        # it and/or carrying over metadata the ticket's non-goals explicitly
        # exclude ("owners/sharing" -- docs/tickets/PRD-21397.md:45) -- see
        # is_transferable_setting's docstring for why this is a pattern
        # match rather than more literal names.
        patch_body = {k: v for k, v in form.items() if is_transferable_setting(k)}
        patch_body["definition"] = stripped_definition
        patch_body["title"] = new_title

        try:
            created = await client.post("/surveys", json_body={"title": new_title})
            new_form_id = created.get("id") if isinstance(created, dict) else None
            if not new_form_id:
                return error_envelope(
                    "upstream_error",
                    f"Form created but no id in response: {dump_json_capped(created)}",
                    False,
                )
            await client.patch(f"/surveys/{new_form_id}", json_body=patch_body)
        except FormsAPIError as e:
            # If the patch (step 2) fails after the create (step 1)
            # succeeded, the shell form already exists in draft — same
            # accepted non-atomicity as mspbots_forms_form_quick_publish
            # (Tech design SS "Data & API contract", documented not silent).
            return e.to_envelope()

        return dump_json_capped({"form_id": new_form_id, "title": new_title, "report": report})
