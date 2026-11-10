"""Transfer tool tests (PRD-21397): strip_tenant_specific, version
validation (pure functions, no network), and the export/import tool pair
driven end-to-end against a fake FormsAPIClient (no real HTTP).

Maps to docs/tickets/PRD-21397.ac.json:
  TC1 -> test_export_then_import_roundtrip_is_field_for_field_identical
  TC2 -> test_import_carries_over_sibling_form_settings (code-reachable part;
         the real-account round trip is still manual-only, see below)
  TC4 -> test_choices_by_url_field_is_stripped_and_reported
  TC5 -> test_tenant_scoped_static_choices_are_stripped_and_reported
  TC6 -> test_ordinary_static_choices_are_not_over_stripped
  TC7 -> test_import_refuses_unsupported_export_version,
         test_import_refuses_missing_export_version
  TC8 -> test_import_returns_form_id_and_report
  TC9 -> test_tenant_a_identifiers_do_not_survive_unflagged
  TC11 (dev, review round 2 must-fix #1) ->
         test_choices_source_shaped_tenant_scoped_choices_are_stripped_and_reported
  TC12 (dev, review round 2 must-fix #2) ->
         test_import_excludes_status_and_sharing_shaped_keys_from_patch_body
  TC13 (rework r2, COV1/AC9) ->
         test_import_overrides_title_inside_definition_as_well_as_outer_patch_body
  TC14 (rework r2, COV2) ->
         test_choices_source_without_tenant_key_match_is_still_treated_as_data_source_bound
  TC15 (rework r2, FIND1) -> test_form_export_does_not_truncate_oversized_form_payload
  TC16 (rework r2, FIND1) -> test_form_import_refuses_truncated_or_incomplete_export_payload
(TC2/TC3's real-account verification against "requires Microsoft 365
 sign-in" is still manual-only, see the ac.json "untested" entries and the
 Self-test section of docs/tickets/PRD-21397.md.)
"""

import copy
import json

import pytest

from mspbots_forms_mcp.tools._transfer_sanitize import (
    EXPORT_SCHEMA_VERSION,
    TransferError,
    is_transferable_setting,
    strip_tenant_specific,
    validate_export_version,
)
from mspbots_forms_mcp.tools.transfer import register as register_transfer


# ---------------------------------------------------------------------------
# strip_tenant_specific / validate_export_version — pure functions, no network
# ---------------------------------------------------------------------------


def test_choices_by_url_field_is_stripped_and_reported():
    definition = {
        "pages": [
            {
                "elements": [
                    {
                        "type": "dropdown",
                        "name": "q1",
                        "title": "Client org",
                        "choicesByUrl": {"url": "https://example/api/orgs"},
                        "choices": ["placeholder-will-be-cleared"],
                    }
                ]
            }
        ]
    }
    stripped, report = strip_tenant_specific(definition)
    field = stripped["pages"][0]["elements"][0]
    assert "choicesByUrl" not in field
    assert field["choices"] == []
    assert report == [{"field": "q1", "reason": "data-source-bound (choicesByUrl)"}]


def test_tenant_scoped_static_choices_are_stripped_and_reported():
    definition = {
        "pages": [
            {
                "elements": [
                    {
                        "type": "dropdown",
                        "name": "client_org",
                        "title": "Client org",
                        "choices": [
                            {"value": "a", "text": "Acme", "tenantId": "t-123"},
                            {"value": "b", "text": "Beta", "tenantId": "t-456"},
                        ],
                    }
                ]
            }
        ]
    }
    stripped, report = strip_tenant_specific(definition)
    field = stripped["pages"][0]["elements"][0]
    assert field["choices"] == []
    assert len(report) == 1
    assert report[0]["field"] == "client_org"
    assert "tenantId" in report[0]["reason"]


def test_choices_source_shaped_tenant_scoped_choices_are_stripped_and_reported():
    """TC11 (dev, review round 2 must-fix #1). The real client_org question
    (sibling ticket PRD-21379, test_form_get_field_id_handles_choices_source_shape,
    form f7528a78) stores its options under choicesSource.items with the
    tenant-scoped keys nested inside each item's "attrs" dict, not as a
    top-level "choices" list and not flat on the item -- the shape round
    1/2's own TC5/TC6 fixtures never exercised. This must be caught and
    stripped the same as the flat-choices case, with choicesSource dropped
    entirely (not left behind half-normalized)."""
    definition = {
        "pages": [
            {
                "elements": [
                    {
                        "type": "dropdown",
                        "name": "client_org",
                        "title": "Client Organization",
                        "choicesSource": {
                            "items": [
                                {
                                    "value": "a",
                                    "text": "Acme",
                                    "attrs": {
                                        "tenantId": "t-123",
                                        "haloClientId": "halo-123",
                                        "rewstOrgId": "rewst-123",
                                    },
                                },
                                {
                                    "value": "b",
                                    "text": "Beta",
                                    "attrs": {"haloClientId": "halo-456"},
                                },
                            ],
                            "passAttributes": ["tenantId", "haloClientId", "rewstOrgId"],
                        },
                    }
                ]
            }
        ]
    }
    stripped, report = strip_tenant_specific(definition)
    field = stripped["pages"][0]["elements"][0]
    assert "choicesSource" not in field
    assert field["choices"] == []
    assert len(report) == 1
    assert report[0]["field"] == "client_org"
    assert "tenantId" in report[0]["reason"]
    assert "haloClientId" in report[0]["reason"]
    rendered = json.dumps(stripped)
    assert "t-123" not in rendered
    assert "halo-123" not in rendered
    assert "halo-456" not in rendered


def test_choices_source_without_tenant_key_match_is_still_treated_as_data_source_bound():
    """TC14 (rework r2, COV2). Supersedes round 2's
    test_choices_source_shaped_ordinary_choices_are_not_over_stripped, which
    asserted the opposite premise: that a choicesSource question with no
    tenant-scoped key match should be left untouched. QA-NIGHT found that
    premise wrong on a real INT form -- Forms also uses choicesSource for a
    live/external ("mcp"/"api") binding that carries no TENANT_SCOPED_CHOICE_KEYS
    match but is still not portable, static data. Any choicesSource-shaped
    question is now treated as data-source-bound unconditionally, the same
    wholesale-clear treatment choicesByUrl already gets, regardless of
    whether any item happens to carry a tenant-scoped key."""
    definition = {
        "pages": [
            {
                "elements": [
                    {
                        "type": "dropdown",
                        "name": "priority",
                        "title": "Priority",
                        "choicesSource": {
                            "items": [{"value": "low", "text": "Low"}, {"value": "high", "text": "High"}],
                            "passAttributes": ["text"],
                        },
                    }
                ]
            }
        ]
    }
    stripped, report = strip_tenant_specific(definition)
    field = stripped["pages"][0]["elements"][0]
    assert "choicesSource" not in field
    assert field["choices"] == []
    assert len(report) == 1
    assert report[0]["field"] == "priority"
    assert report[0]["reason"] == "data-source-bound (choicesSource)"


def test_ordinary_static_choices_are_not_over_stripped():
    definition = {
        "pages": [
            {
                "elements": [
                    {
                        "type": "radio",
                        "name": "q2",
                        "title": "Favorite color",
                        "choices": ["Red", "Green", "Blue"],
                    }
                ]
            }
        ]
    }
    stripped, report = strip_tenant_specific(definition)
    field = stripped["pages"][0]["elements"][0]
    assert field["choices"] == ["Red", "Green", "Blue"]
    assert report == []


def test_strip_does_not_mutate_the_input():
    definition = {
        "pages": [{"elements": [{"name": "q1", "choicesByUrl": {"url": "x"}, "choices": ["a"]}]}]
    }
    original = copy.deepcopy(definition)
    strip_tenant_specific(definition)
    assert definition == original


def test_import_refuses_unsupported_export_version():
    with pytest.raises(TransferError) as exc_info:
        validate_export_version({"export_version": 2, "form": {}})
    assert "2" in exc_info.value.message
    assert exc_info.value.code == "invalid_argument"


def test_import_refuses_missing_export_version():
    with pytest.raises(TransferError) as exc_info:
        validate_export_version({"form": {}})
    assert "None" in exc_info.value.message or "none" in exc_info.value.message.lower()


def test_tenant_a_identifiers_do_not_survive_unflagged():
    tenant_a_ids = {"tenantId": "A-ONLY-111", "haloClientId": "A-ONLY-222", "rewstOrgId": "A-ONLY-333"}
    definition = {
        "pages": [
            {
                "elements": [
                    {
                        "name": "client_org",
                        "choices": [
                            {"value": "x", "text": "X", **tenant_a_ids},
                        ],
                    },
                    {
                        "name": "plain",
                        "choices": ["unrelated-string-choice"],
                    },
                ]
            }
        ]
    }
    stripped, report = strip_tenant_specific(definition)
    rendered = json.dumps(stripped)
    for value in tenant_a_ids.values():
        assert value not in rendered
    assert any(r["field"] == "client_org" for r in report)


# ---------------------------------------------------------------------------
# Tool-level: mspbots_forms_form_export / mspbots_forms_form_import driven
# through FastMCP's in-process call_tool, against a fake client (no network)
# — same no-network style as test_tools.py/test_middleware.py.
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records calls and plays back canned responses — stands in for
    FormsAPIClient so these tests never touch the network."""

    def __init__(self, get_response=None, create_response=None, patch_response=None):
        self._get_response = get_response
        self._create_response = create_response
        self._patch_response = patch_response
        self.calls: list[tuple] = []

    async def get(self, path, params=None):
        self.calls.append(("get", path, params))
        return self._get_response

    async def post(self, path, json_body=None):
        self.calls.append(("post", path, json_body))
        return self._create_response

    async def patch(self, path, json_body):
        self.calls.append(("patch", path, json_body))
        return self._patch_response


def _make_mcp(client):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name="test-transfer")
    register_transfer(mcp, lambda: client)
    return mcp


SOURCE_FORM = {
    "id": "src-form-1",
    "title": "PRECICOM offboarding",
    "showProgressBar": True,
    "definition": {
        "pages": [
            {
                "name": "page1",
                "elements": [
                    {"type": "text", "name": "q1", "title": "Employee name"},
                    {"type": "radio", "name": "q2", "title": "Department", "choices": ["IT", "Sales"]},
                ],
            }
        ]
    },
}


@pytest.mark.asyncio
async def test_export_then_import_roundtrip_is_field_for_field_identical():
    # Export side: fake client returns the canned source form record.
    export_client = _FakeClient(get_response=SOURCE_FORM)
    export_mcp = _make_mcp(export_client)
    export_result = await export_mcp.call_tool("mspbots_forms_form_export", {"form_id": "src-form-1"})
    export_payload = json.loads(export_result[0][0].text)

    assert export_payload["export_version"] == EXPORT_SCHEMA_VERSION
    assert export_payload["form"] == SOURCE_FORM
    assert export_client.calls == [
        ("get", "/surveys/src-form-1", {"include": "definition,questions,versions"})
    ]

    # Import side: a fresh fake client (new tenant), fed the export payload.
    import_client = _FakeClient(
        create_response={"id": "new-form-9"}, patch_response={"id": "new-form-9"}
    )
    import_mcp = _make_mcp(import_client)
    import_result = await import_mcp.call_tool(
        "mspbots_forms_form_import", {"export_payload": export_payload}
    )
    import_payload = json.loads(import_result[0][0].text)

    assert import_payload["form_id"] == "new-form-9"
    assert import_payload["title"] == "PRECICOM offboarding (imported)"
    assert import_payload["report"] == []  # no data-source/tenant-scoped fields in this fixture

    # The definition handed to PATCH is field-for-field identical to the
    # source definition (no data-source/tenant-scoped choices to strip).
    patch_call = next(c for c in import_client.calls if c[0] == "patch")
    assert patch_call[2]["definition"] == SOURCE_FORM["definition"]
    # AC2 (settings fidelity): sibling form-level settings (showProgressBar
    # here) travel with the PATCH body, not just the definition.
    assert patch_call[2]["showProgressBar"] is True


@pytest.mark.asyncio
async def test_import_overrides_title_inside_definition_as_well_as_outer_patch_body():
    """TC13 (rework r2, COV1/AC9). Round 1's SOURCE_FORM fixture has no
    top-level "title" inside definition, so the collision this bug depends
    on never arose: the PATCHed definition's own "title" key must also be
    overridden to the suffixed new title, not left as the source's original
    title -- otherwise the backend re-derives the stored/displayed title
    from the nested definition and the " (imported)" suffix silently
    reverts once persisted, even though the tool's own echoed response
    already said the right title."""
    source_with_nested_title = copy.deepcopy(SOURCE_FORM)
    source_with_nested_title["definition"]["title"] = "PRECICOM offboarding"
    export_payload = {"export_version": EXPORT_SCHEMA_VERSION, "form": source_with_nested_title}

    import_client = _FakeClient(create_response={"id": "new-form-9"}, patch_response={"id": "new-form-9"})
    import_mcp = _make_mcp(import_client)
    import_result = await import_mcp.call_tool(
        "mspbots_forms_form_import", {"export_payload": export_payload}
    )
    import_payload = json.loads(import_result[0][0].text)

    patch_call = next(c for c in import_client.calls if c[0] == "patch")
    assert patch_call[2]["title"] == "PRECICOM offboarding (imported)"
    assert patch_call[2]["definition"]["title"] == "PRECICOM offboarding (imported)"
    assert import_payload["title"] == "PRECICOM offboarding (imported)"


@pytest.mark.asyncio
async def test_import_carries_over_sibling_form_settings():
    """AC2 regression (review round 1, must-fix #1): the exported `form`
    record's sibling settings -- anything alongside `definition`/`title`,
    e.g. showProgressBar, a hypothetical M365-sign-in-required flag, a
    thank-you message -- must reach the PATCH body verbatim, since the
    export deliberately keeps the raw record (Tech design SS "Conclusion"):
    this is what lets AC2 hold without enumerating every settings field by
    name. Identity (`id`) and non-setting projections (`questions`,
    `versions`) must NOT be copied over.
    """
    source_with_settings = copy.deepcopy(SOURCE_FORM)
    source_with_settings["requireMicrosoftSignIn"] = True
    source_with_settings["thankYouMessage"] = "Thanks for completing this form!"
    source_with_settings["questions"] = [{"name": "q1", "kind": "text"}]  # read-side projection
    source_with_settings["versions"] = [{"id": "v1", "publishedAt": "2026-01-01"}]
    # Review round 2, must-fix #2: a status/owner/sharing-shaped key on the
    # source record must NOT reach the PATCH body either (see
    # test_import_excludes_status_and_sharing_shaped_keys_from_patch_body
    # for the dedicated/exhaustive coverage of this).
    source_with_settings["status"] = "published"
    export_payload = {"export_version": EXPORT_SCHEMA_VERSION, "form": source_with_settings}

    import_client = _FakeClient(create_response={"id": "new-form-9"}, patch_response={"id": "new-form-9"})
    import_mcp = _make_mcp(import_client)
    await import_mcp.call_tool("mspbots_forms_form_import", {"export_payload": export_payload})

    patch_call = next(c for c in import_client.calls if c[0] == "patch")
    body = patch_call[2]
    assert body["showProgressBar"] is True
    assert body["requireMicrosoftSignIn"] is True
    assert body["thankYouMessage"] == "Thanks for completing this form!"
    assert "id" not in body
    assert "questions" not in body
    assert "versions" not in body
    assert "status" not in body
    # title/definition are still the import-side overrides, not copied raw.
    assert body["title"] == "PRECICOM offboarding (imported)"
    assert body["definition"] == source_with_settings["definition"]


def test_is_transferable_setting_excludes_status_owner_sharing_publish_shaped_keys():
    """TC12 (dev, review round 2 must-fix #2), pure-function half: the
    confirmed "status" field, plus any owner/sharing/publish-shaped key
    (exact real names unverified -- forms.py's own disclaimer), must never
    be treated as a safe setting to copy; an ordinary setting still is."""
    for blocked in [
        "id",
        "definition",
        "questions",
        "versions",
        "status",
        "ownerId",
        "owner",
        "sharedWith",
        "shareSettings",
        "publishedAt",
        "publishedVersionId",
        "isPublished",
    ]:
        assert is_transferable_setting(blocked) is False, blocked
    for allowed in ["showProgressBar", "requireMicrosoftSignIn", "thankYouMessage", "title"]:
        assert is_transferable_setting(allowed) is True, allowed


@pytest.mark.asyncio
async def test_import_excludes_status_and_sharing_shaped_keys_from_patch_body():
    """TC12 (dev, review round 2 must-fix #2), tool-level half: a source
    form record carrying "status": "published" plus an owner/sharing-shaped
    key must not auto-publish or carry ownership onto the freshly created
    (currently-draft) destination form -- both directly contradicted by the
    ticket's non-goals (docs/tickets/PRD-21397.md:45) and "unpublished"
    scope requirement (:39)."""
    source_with_state = copy.deepcopy(SOURCE_FORM)
    source_with_state["status"] = "published"
    source_with_state["ownerId"] = "user-should-not-transfer"
    source_with_state["sharedWith"] = ["team-x"]
    export_payload = {"export_version": EXPORT_SCHEMA_VERSION, "form": source_with_state}

    import_client = _FakeClient(create_response={"id": "new-form-9"}, patch_response={"id": "new-form-9"})
    import_mcp = _make_mcp(import_client)
    await import_mcp.call_tool("mspbots_forms_form_import", {"export_payload": export_payload})

    patch_call = next(c for c in import_client.calls if c[0] == "patch")
    body = patch_call[2]
    assert "status" not in body
    assert "ownerId" not in body
    assert "sharedWith" not in body
    # An ordinary sibling setting still travels (round-1's fix not regressed).
    assert body["showProgressBar"] is True


@pytest.mark.asyncio
async def test_import_returns_form_id_and_report():
    source_with_data_source_field = copy.deepcopy(SOURCE_FORM)
    source_with_data_source_field["definition"]["pages"][0]["elements"].append(
        {"type": "dropdown", "name": "q3", "title": "Org", "choicesByUrl": {"url": "x"}}
    )
    export_payload = {"export_version": EXPORT_SCHEMA_VERSION, "form": source_with_data_source_field}

    import_client = _FakeClient(create_response={"id": "new-form-9"}, patch_response={"id": "new-form-9"})
    import_mcp = _make_mcp(import_client)
    import_result = await import_mcp.call_tool(
        "mspbots_forms_form_import", {"export_payload": export_payload}
    )
    import_payload = json.loads(import_result[0][0].text)

    assert import_payload["form_id"] == "new-form-9"
    assert len(import_payload["report"]) == 1
    assert import_payload["report"][0]["field"] == "q3"


@pytest.mark.asyncio
async def test_import_refuses_unsupported_version_without_any_write_call():
    import_client = _FakeClient()
    import_mcp = _make_mcp(import_client)
    result = await import_mcp.call_tool(
        "mspbots_forms_form_import",
        {"export_payload": {"export_version": 2, "form": SOURCE_FORM}},
    )
    payload = json.loads(result[0][0].text)
    assert payload["error"]["code"] == "invalid_argument"
    assert "2" in payload["error"]["message"]
    assert import_client.calls == []  # no post/patch ever invoked


def _oversized_form(num_questions: int = 400) -> dict:
    """A synthetic form whose full JSON record is comfortably over
    dump_json_capped's 20,000-char budget (the Ginko/PRECICOM-sized real
    forms QA-NIGHT reproduced against), built from plain repeated
    questions -- no real tenant or INT needed (TC15/TC16 are pure
    export/import-shape tests)."""
    elements = [
        {
            "type": "text",
            "name": f"q{i}",
            "title": f"Question {i} -- a reasonably long title to pad the payload size",
            "description": "A reasonably long description field, repeated to pad size.",
        }
        for i in range(num_questions)
    ]
    return {
        "id": "src-form-big",
        "title": "Ginko onboarding",
        "showProgressBar": True,
        "definition": {"pages": [{"name": "page1", "elements": elements}]},
    }


@pytest.mark.asyncio
async def test_form_export_does_not_truncate_oversized_form_payload():
    """TC15 (rework r2, FIND1). dump_json_capped's generic 20,000-char cap
    sheds the whole dict-shaped "form" field for an oversized form (there is
    no list-shaped field for its truncation path to shrink instead), so
    mspbots_forms_form_export must not run its envelope through that
    generic cap: the returned "form" must be the complete, untruncated
    record, since export output is saved to a file for a later import, not
    read as agent-facing conversation context."""
    big_form = _oversized_form()
    assert len(json.dumps(big_form)) > 20_000  # exercise the real size class

    export_client = _FakeClient(get_response=big_form)
    export_mcp = _make_mcp(export_client)
    export_result = await export_mcp.call_tool(
        "mspbots_forms_form_export", {"form_id": "src-form-big"}
    )
    export_payload = json.loads(export_result[0][0].text)

    assert export_payload["form"] == big_form
    assert "truncated" not in export_payload
    assert "omitted_fields" not in export_payload


@pytest.mark.asyncio
async def test_form_import_refuses_truncated_or_incomplete_export_payload():
    """TC16 (rework r2, FIND1). An export_payload whose "form" is missing,
    empty, or marked truncated/omitted (e.g. one produced by the FIND1 bug
    before its fix) must be refused outright -- never silently imported as
    an empty "Untitled form" -- same "never half-import" principle AC6
    already states for an unsupported export_version."""
    import_client = _FakeClient()
    import_mcp = _make_mcp(import_client)

    bad_payloads = [
        {"export_version": EXPORT_SCHEMA_VERSION, "form": {}},
        {"export_version": EXPORT_SCHEMA_VERSION},
        {
            "export_version": EXPORT_SCHEMA_VERSION,
            "truncated": True,
            "omitted_fields": ["form"],
        },
    ]
    for payload in bad_payloads:
        result = await import_mcp.call_tool(
            "mspbots_forms_form_import", {"export_payload": payload}
        )
        parsed = json.loads(result[0][0].text)
        assert parsed["error"]["code"] == "invalid_argument", payload
        assert import_client.calls == []  # no post/patch ever invoked
