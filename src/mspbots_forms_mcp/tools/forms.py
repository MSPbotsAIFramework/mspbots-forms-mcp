"""Form (form definition) tools.

⚠️ UNVERIFIED — built entirely from the API contract pasted into ClickUp
PRD-15818 (per Leo Yang's comment), not by reading the actual backend repo.
Endpoint paths/params/response shapes below match that doc; none have been
called against a real deployment. See README Known Gaps.

Note: the underlying REST API's own paths/fields still say "survey"
(`/surveys`, `surveyVersionId`, ...) — those are the real backend's wire
contract and are left untouched. Only the agent-facing tool names,
parameters, and descriptions here say "form".
"""

from collections.abc import Callable
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_choices_page, dump_json_capped, error_envelope
from ..api_client import FormsAPIClient, FormsAPIError
from ._common import NO_TOKEN
from ._survey_walk import find_question_by_name, normalize_choice_source

# Hard safety ceiling on top of the underlying API's own documented cap
# (default 20, max 100) — we clamp to the tighter of the two (100).
_MAX_LIMIT = 100


def register(mcp: FastMCP, client_factory: Callable[[], FormsAPIClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbots_forms_form_list(
        status: Annotated[
            str | None, Field(description='Optional filter — "draft", "published", or "archived".')
        ] = None,
        q: Annotated[str | None, Field(description="Optional fuzzy title match.")] = None,
        cursor: Annotated[
            str | None,
            Field(description="Optional pagination cursor from a previous response's nextCursor."),
        ] = None,
        limit: Annotated[
            int | None, Field(description="Optional page size (default 20, max 100 — server clamps).")
        ] = None,
    ) -> str:
        """List forms (form summaries) for the current tenant.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        if limit is not None:
            limit = min(limit, _MAX_LIMIT)
        params = {"status": status, "q": q, "cursor": cursor, "limit": limit}
        try:
            result = await client.get("/surveys", params=params)
            return dump_json_capped(result)
        except FormsAPIError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbots_forms_form_get(
        form_id: Annotated[str, Field(description="Required form ID.")],
        include: Annotated[
            list[str] | None,
            Field(
                description=(
                    'Optional list of extra sections to include: "definition" '
                    '(full SurveyJS JSON), "questions" (compact question list — '
                    "name/kind/title/required/showIf, much smaller than the full "
                    'definition), "versions" (published version list).'
                )
            ),
        ] = None,
        field_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional — return only the question named field_id "
                    '(SurveyJS "name", e.g. "client_org") with every choice '
                    'fully intact (e.g. tenantId). "questions" mode never '
                    "carries choice properties, and the whole-form response "
                    "can be truncated before reaching them; this bypasses "
                    "both. Forces an internal definition fetch."
                )
            ),
        ] = None,
        choices_offset: Annotated[
            int,
            Field(
                description=(
                    "Optional — with field_id, the index of the first choice "
                    "to return (default 0). Use the next_choices_offset the "
                    "previous call returned to read the rest of a question "
                    "whose options don't fit one response."
                ),
                ge=0,
            ),
        ] = 0,
        choices_limit: Annotated[
            int | None,
            Field(
                description=(
                    "Optional — with field_id, the maximum number of choices "
                    "to return in this call. Fewer may come back if they "
                    "don't fit the response budget; choices_returned and "
                    "next_choices_offset always say what actually happened."
                ),
                ge=1,
            ),
        ] = None,
    ) -> str:
        """Get a form's details.

        By default only the base record is returned — no definition,
        questions, or versions — since the full definition can be several KB.

        include=["questions"] never carries choice-level custom properties.
        To read one question's full choice metadata without risking
        truncation, pass field_id instead of include.

        A question with many options can exceed one response's budget; the
        field_id response then reports choices_offset / choices_returned /
        choices_total / next_choices_offset. Call again with
        choices_offset=next_choices_offset until it comes back null to read
        every option (PRD-21379).
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        include_list = list(include) if include else []
        if field_id is not None and "definition" not in include_list:
            include_list.append("definition")
        params = {"include": ",".join(include_list) if include_list else None}
        try:
            result = await client.get(f"/surveys/{form_id}", params=params)
        except FormsAPIError as e:
            return e.to_envelope()
        if field_id is None:
            return dump_json_capped(result)
        definition = result.get("definition") if isinstance(result, dict) else None
        question = (
            find_question_by_name(definition, field_id)
            if isinstance(definition, dict)
            else None
        )
        if question is None:
            return error_envelope(
                "not_found", f"No question named {field_id!r} in form {form_id!r}", False
            )
        # PRD-21379 rework r2 (AC7): a question's choices may live under a
        # "choicesSource" dict ({"items": [...]}) instead of a top-level
        # "choices" list — normalize so dump_json_capped's truncation (which
        # only scans top-level list values) can see and cap the option list
        # instead of discarding it entirely.
        question = normalize_choice_source(question)
        choices = question.get("choices")
        if not isinstance(choices, list):
            return dump_json_capped({"form_id": form_id, "field_id": field_id, **question})
        # PRD-21379 rework r3: one response cannot hold every option of a
        # large question (185 options ~= 51KB against a 20,000-char budget),
        # and silently returning the first screenful drops the rest beyond
        # reach. Page instead, so repeated calls can read all of them.
        base = {k: v for k, v in question.items() if k != "choices"}
        window = choices[choices_offset:]
        if choices_limit is not None:
            window = window[:choices_limit]
        return dump_choices_page(
            {"form_id": form_id, "field_id": field_id, **base},
            window,
            choices_offset,
            len(choices),
        )

    @mcp.tool()
    async def mspbots_forms_form_create(
        title: Annotated[str, Field(description="Required form title.")],
        questions: Annotated[
            list[dict] | None,
            Field(
                description=(
                    "Optional flat list of question dicts (see above). Use "
                    "this OR pages, not both — pages takes precedence if both given."
                )
            ),
        ] = None,
        pages: Annotated[
            list[dict] | None,
            Field(
                description=(
                    'Optional list of {"title": str?, "questions": [...]} for a '
                    "multi-page form. Takes precedence over questions if both given."
                )
            ),
        ] = None,
        show_progress_bar: Annotated[
            bool | None, Field(description="Optional, whether to show a progress bar.")
        ] = None,
    ) -> str:
        """Create a new form (draft, unpublished).

        Uses the friendly question DSL, not raw SurveyJS JSON — the server
        translates via a kind-mapping table. Supported `kind` values: "text",
        "textarea", "radio", "checkbox", "dropdown", "rating", "boolean",
        "date". Each question dict: kind (required), title (required),
        required (bool), plus kind-specific fields — "radio"/"checkbox"/
        "dropdown" need choices (list[str]); "rating" takes steps/style
        ("numbers"/"stars"/"smileys")/lowLabel/highLabel; "boolean" takes
        yesLabel/noLabel; "date" takes earliest/latest; "text"/"textarea"
        take placeholder (and "textarea" also rows). Optional showIf:
        {"question": "<earlier question's generated name>", "operator":
        "is"/"isNot"/"greaterThan"/"atLeast"/"lessThan"/"atMost"/"contains"/
        "answered"/"empty", "value": ...} — showIf.question can only
        reference an earlier question (server rejects forward/circular refs
        with 422). Question names (q1, q2, ...) are auto-assigned in order.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"title": title}
        if show_progress_bar is not None:
            body["showProgressBar"] = show_progress_bar
        if pages is not None:
            body["pages"] = pages
        elif questions is not None:
            body["questions"] = questions
        try:
            result = await client.post("/surveys", json_body=body)
            return dump_json_capped(result)
        except FormsAPIError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbots_forms_form_update(
        form_id: Annotated[str, Field(description="Required form ID.")],
        title: Annotated[str | None, Field(description="Optional new title.")] = None,
        show_progress_bar: Annotated[
            bool | None, Field(description="Optional new progress-bar setting.")
        ] = None,
        questions: Annotated[
            list[dict] | None,
            Field(
                description=(
                    "Optional flat question list (DSL) — FULL REPLACE of the "
                    "current question set, not a patch. To add/edit/remove a "
                    "single question, first call mspbots_forms_form_get(form_id, "
                    "include=['questions']) to fetch the current list, apply your "
                    "change, and pass the complete resulting array here — any "
                    "question you omit is permanently removed."
                )
            ),
        ] = None,
        pages: Annotated[
            list[dict] | None,
            Field(
                description=(
                    "Optional multi-page question list (DSL) — FULL REPLACE of "
                    "the current question set (same fetch-then-merge caveat as "
                    "questions; see its description). Takes precedence over "
                    "questions if both given."
                )
            ),
        ] = None,
        definition: Annotated[
            dict | None,
            Field(
                description=(
                    "Optional raw SurveyJS JSON, for edits not expressible "
                    "via the DSL. Mutually exclusive with questions/pages in "
                    "practice — the API contract doesn't specify what happens if "
                    "given together, so avoid combining them."
                )
            ),
        ] = None,
    ) -> str:
        """Update a form's draft. Only affects the working draft — published
        versions are immutable snapshots unaffected by this call.

        Use this to edit a form that already exists (e.g. one created via
        mspbots_forms_form_create); for spinning up a brand-new form and getting it
        live in one step, use mspbots_forms_form_quick_publish instead.

        Give either questions/pages (friendly DSL, full replace of the
        question set — same rules as mspbots_forms_form_create) OR definition (raw
        SurveyJS JSON, for advanced edits the DSL can't express — e.g.
        custom validators or choicesByUrl). If definition is given, it's a
        shallow merge — properties the DSL/Builder don't recognize are left
        untouched, never dropped.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {}
        if title is not None:
            body["title"] = title
        if show_progress_bar is not None:
            body["showProgressBar"] = show_progress_bar
        if pages is not None:
            body["pages"] = pages
        elif questions is not None:
            body["questions"] = questions
        if definition is not None:
            body["definition"] = definition
        try:
            result = await client.patch(f"/surveys/{form_id}", json_body=body)
            return dump_json_capped(result)
        except FormsAPIError as e:
            return e.to_envelope()

    @mcp.tool()
    async def mspbots_forms_form_publish(
        form_id: Annotated[str, Field(description="Required form ID to publish.")],
    ) -> str:
        """Publish a form — creates an immutable version snapshot of its
        current draft. Share links bind to this version, so answers stay
        paired with the question set that was live when they were collected,
        even if the draft changes afterward.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.post(f"/surveys/{form_id}/versions")
            return dump_json_capped(result)
        except FormsAPIError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbots_forms_form_delete(
        form_id: Annotated[str, Field(description="Required form ID to delete.")],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Required — must be set to true to proceed. This tool "
                    "refuses to call the API if confirm is not explicitly true."
                )
            ),
        ],
    ) -> str:
        """Delete a form — permanently removes it along with all its
        published versions, share links, AND collected responses.

        Only set confirm=true on an explicit, unambiguous delete request
        ("delete it", "delete form X for good") — vague retirement talk
        ("we don't need it anymore") should prompt a confirmation question
        instead. To just stop new submissions, use
        mspbots_forms_share_update (action="pause"/"close") instead.

        ⚠️ DESTRUCTIVE. Requires confirm=true.
        """
        if not confirm:
            return error_envelope(
                "invalid_argument", "Destructive operation requires confirm=true", False
            )
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.delete(f"/surveys/{form_id}")
            return dump_json_capped(result)
        except FormsAPIError as e:
            return e.to_envelope()

    @mcp.tool()
    async def mspbots_forms_form_quick_publish(
        title: Annotated[str, Field(description="Required form title.")],
        questions: Annotated[
            list[dict] | None,
            Field(description="Optional flat question list (DSL) — see mspbots_forms_form_create."),
        ] = None,
        pages: Annotated[
            list[dict] | None,
            Field(description="Optional multi-page question list (DSL) — see mspbots_forms_form_create."),
        ] = None,
        show_progress_bar: Annotated[
            bool | None, Field(description="Optional progress-bar setting.")
        ] = None,
        audience: Annotated[
            Literal["public", "workspace"],
            Field(
                description=(
                    'Share audience for the auto-created link — "public" or '
                    '"workspace" only (default "public"). For "passcode" or '
                    '"personal", call mspbots_forms_share_create afterward instead '
                    "to set the required passcode/recipient field — this tool "
                    "doesn't accept those."
                )
            ),
        ] = "public",
    ) -> str:
        """Create a form, publish it, and create a share link, in one call.

        Chains create + publish + share-create — no single backing
        endpoint, so a partial failure is NOT rolled back (form/version
        may already exist even if share creation fails).

        Only for brand-new forms — to edit an existing one, use
        mspbots_forms_form_update instead.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        create_body: dict = {"title": title}
        if show_progress_bar is not None:
            create_body["showProgressBar"] = show_progress_bar
        if pages is not None:
            create_body["pages"] = pages
        elif questions is not None:
            create_body["questions"] = questions
        try:
            form = await client.post("/surveys", json_body=create_body)
            form_id = form.get("id") if isinstance(form, dict) else None
            if not form_id:
                return error_envelope(
                    "upstream_error",
                    f"Form created but no id in response: {dump_json_capped(form)}",
                    False,
                )
            version = await client.post(f"/surveys/{form_id}/versions")
            share = await client.post(
                f"/surveys/{form_id}/shares", json_body={"audience": audience}
            )
            return dump_json_capped({"form": form, "version": version, "share": share})
        except FormsAPIError as e:
            return e.to_envelope()
