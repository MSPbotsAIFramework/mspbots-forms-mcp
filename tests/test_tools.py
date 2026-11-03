"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
FormsAPIError, independent of any real HTTP request. The
mspbots_forms_form_get(field_id=...) tests below (PRD-21379) also avoid
real HTTP by monkeypatching FormsAPIClient.get and driving the tool
in-process via mcp.call_tool(), with per-request credentials supplied
through the same _gateway_creds_var contextvar test_middleware.py uses.
"""

import json

import pytest

from mspbots_forms_mcp.api_client import FormsAPIClient, FormsAPIError
from mspbots_forms_mcp.config import Settings
from mspbots_forms_mcp.server import create_mcp_server, _gateway_creds_var
from mspbots_forms_mcp.tools._survey_walk import find_question_by_name

# required params + annotation hints expected for each of the 14 tools.
# Tool names carry the mspbots_forms_ prefix added in a prior rename pass —
# any signature change here is a breaking contract change (SOP §13).
EXPECTED_TOOLS = {
    "mspbots_forms_form_list": (set(), {"readOnlyHint": True}),
    "mspbots_forms_form_get": ({"form_id"}, {"readOnlyHint": True}),
    "mspbots_forms_form_create": ({"title"}, {}),
    "mspbots_forms_form_update": ({"form_id"}, {"idempotentHint": True}),
    "mspbots_forms_form_publish": ({"form_id"}, {}),
    "mspbots_forms_form_delete": ({"form_id", "confirm"}, {"destructiveHint": True}),
    "mspbots_forms_form_quick_publish": ({"title"}, {}),
    "mspbots_forms_share_list": ({"form_id"}, {"readOnlyHint": True}),
    "mspbots_forms_share_get": ({"share_id"}, {"readOnlyHint": True}),
    "mspbots_forms_share_create": ({"form_id"}, {}),
    "mspbots_forms_share_update": ({"share_id"}, {"idempotentHint": True}),
    "mspbots_forms_share_delete": ({"share_id", "confirm"}, {"destructiveHint": True}),
    "mspbots_forms_response_summary": ({"form_id"}, {"readOnlyHint": True}),
    "mspbots_forms_response_list": ({"form_id"}, {"readOnlyHint": True}),
    "mspbots_forms_form_export": ({"form_id"}, {}),
    "mspbots_forms_form_import": ({"export_payload"}, {}),
}

# mspbots_forms_form_create/_update genuinely need their long DSL
# explanation (question `kind` values, showIf semantics) to let an agent
# construct valid nested arguments — an earlier blind usability test found
# that explanation was the most valuable part of these docstrings. They are
# intentionally exempt from the <=500 char budget (SOP §2.2 is "should", not
# "must") rather than gutted to fit.
_DESCRIPTION_LENGTH_EXEMPT = {"mspbots_forms_form_create", "mspbots_forms_form_update"}


@pytest.mark.asyncio
async def test_tools_list_snapshot():
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOLS), f"unexpected tool set: {names}"
    assert len(tools) == 16

    by_name = {t.name: t for t in tools}
    for name, (expected_required, expected_annotations) in EXPECTED_TOOLS.items():
        tool = by_name[name]
        required = set(tool.inputSchema.get("required", []))
        assert required == expected_required, f"{name}: required={required}"

        ann = tool.annotations
        for hint, value in expected_annotations.items():
            actual = getattr(ann, hint, None) if ann is not None else None
            assert actual == value, f"{name}: {hint}={actual}, expected {value}"
        if not expected_annotations:
            # No semantic hint expected (pure create/publish action) — but
            # still confirm none of the hints were set by accident.
            if ann is not None:
                assert not ann.readOnlyHint
                assert not ann.destructiveHint
                assert not ann.idempotentHint

        first_line = (tool.description or "").strip().splitlines()[0]
        assert len(first_line) <= 100, f"{name}: first line too long: {first_line!r}"

        if name not in _DESCRIPTION_LENGTH_EXEMPT:
            assert len(tool.description or "") <= 500, f"{name}: description too long"


@pytest.mark.asyncio
async def test_service_instructions_present_and_bounded():
    mcp = create_mcp_server(Settings())
    assert mcp.instructions
    assert len(mcp.instructions) <= 1500


@pytest.mark.parametrize(
    "status_code,expected_code,expected_retryable",
    [
        (0, "upstream_error", True),
        (400, "invalid_argument", False),
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (422, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (503, "upstream_error", True),
    ],
)
def test_error_envelope_mapping(status_code, expected_code, expected_retryable):
    err = FormsAPIError(status_code, "some_domain_code", "boom")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retryable"] is expected_retryable
    assert "boom" in envelope["error"]["message"]


def test_error_envelope_without_domain_code():
    err = FormsAPIError(404, None, "not there")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == "not_found"
    assert envelope["error"]["message"] == "not there"


# ---------------------------------------------------------------------------
# PRD-21379: mspbots_forms_form_get(field_id=...) — single-question filter
# that returns full choice metadata (tenantId/haloClientId/rewstOrgId)
# without the whole-form response's size cap dropping it.
# ---------------------------------------------------------------------------


def _client_org_definition(num_choices: int = 1, pad: str = "") -> dict:
    """A minimal SurveyJS definition with one dropdown question, client_org,
    nested one level inside a "panel" element (exercises the recursive walk)."""
    choices = [
        {
            "value": f"choice_{i:04d}",
            "text": f"Choice label {i:04d}{pad}",
            "tenantId": f"tenant-{i:04d}",
            "haloClientId": f"halo-{i:04d}",
            "rewstOrgId": f"rewst-{i:04d}",
        }
        for i in range(num_choices)
    ]
    return {
        "pages": [
            {
                "elements": [
                    {
                        "type": "panel",
                        "name": "org_panel",
                        "elements": [
                            {
                                "type": "dropdown",
                                "name": "client_org",
                                "title": "Client Organization",
                                "choices": choices,
                            }
                        ],
                    }
                ]
            }
        ]
    }


async def _call_form_get(monkeypatch, fake_get, arguments, tenant_id="tenant-abc"):
    """Drive mspbots_forms_form_get through the real FastMCP dispatch path
    (not by calling the undecorated function), with FormsAPIClient.get
    monkeypatched so no real HTTP is made. Returns the decoded JSON payload.
    """
    monkeypatch.setattr(FormsAPIClient, "get", fake_get)
    mcp = create_mcp_server(Settings())
    token = _gateway_creds_var.set(("tok", "https://agent.mspbots.ai", tenant_id))
    try:
        _, structured = await mcp.call_tool("mspbots_forms_form_get", arguments)
    finally:
        _gateway_creds_var.reset(token)
    return json.loads(structured["result"])


@pytest.mark.asyncio
async def test_form_get_field_id_returns_full_choice_metadata(monkeypatch):
    """TC1 / AC1."""
    captured_params = {}

    async def fake_get(self, path, params=None):
        captured_params.update(params or {})
        return {"id": "f7528a78", "definition": _client_org_definition(num_choices=3)}

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f7528a78", "field_id": "client_org"}
    )

    assert payload["name"] == "client_org"
    assert payload["form_id"] == "f7528a78"
    assert payload["field_id"] == "client_org"
    assert len(payload["choices"]) == 3
    for choice in payload["choices"]:
        assert choice["tenantId"] and choice["haloClientId"] and choice["rewstOrgId"]
    assert payload.get("truncated") is None
    # field_id must force a definition fetch even though the caller passed no include.
    assert captured_params["include"] == "definition"


@pytest.mark.asyncio
async def test_form_get_field_id_truncates_oversized_choices_gracefully(monkeypatch):
    """TC2 / AC2: a question whose own choices list alone exceeds the
    20,000-char budget must still come back as valid JSON with every
    surviving choice's properties intact, not silently discarded."""

    async def fake_get(self, path, params=None):
        # ~150 bytes/choice * 300 choices comfortably exceeds MAX_CHARS (20_000).
        return {
            "id": "f7528a78",
            "definition": _client_org_definition(num_choices=300, pad=" padding text"),
        }

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f7528a78", "field_id": "client_org"}
    )

    assert payload["truncated"] is True
    assert payload["original_count"] == 300
    assert payload["truncated_field"] == "choices"
    assert 0 < len(payload["choices"]) < 300
    for choice in payload["choices"]:
        assert choice["tenantId"] and choice["haloClientId"] and choice["rewstOrgId"]
    assert len(json.dumps(payload, separators=(",", ":"))) <= 20_000


def _client_org_choices_source_definition(num_items: int = 185, num_missing_tenant: int = 37) -> dict:
    """A SurveyJS definition whose client_org question carries its options
    via a "choicesSource" static-choice-source dict (a nested "items" list)
    instead of a top-level "choices" list -- the shape the real client_org
    question on form f7528a78 actually uses (PRD-21379 rework r2, dev
    feedback comment 90170266253375), as opposed to the flat "choices" list
    _client_org_definition() above mocks (which round 1's tests never
    caught not matching reality).

    The first `num_missing_tenant` items omit "tenantId" from their attrs,
    mirroring the real form where 37 of 185 options genuinely have no
    tenantId in the source data (r2-amended AC5/TC5).
    """
    items = []
    for i in range(num_items):
        attrs = {"haloClientId": f"halo-{i:04d}", "rewstOrgId": f"rewst-{i:04d}"}
        if i >= num_missing_tenant:
            attrs["tenantId"] = f"tenant-{i:04d}"
        items.append({"value": f"choice_{i:04d}", "text": f"Choice label {i:04d}", "attrs": attrs})
    return {
        "pages": [
            {
                "elements": [
                    {
                        "type": "dropdown",
                        "name": "client_org",
                        "title": "Client Organization",
                        "choicesSource": {
                            "items": items,
                            "passAttributes": ["rewstOrgId", "haloClientId", "tenantId"],
                        },
                    }
                ]
            }
        ]
    }


@pytest.mark.asyncio
async def test_form_get_field_id_handles_choices_source_shape(monkeypatch):
    """TC7 / AC7 (rework r2 regression, source: dev feedback comment
    90170266253375). The real client_org question on form f7528a78 stores
    its 185 options under choicesSource.items, not a top-level choices
    list -- round 1's fix fell into dump_json_capped's "not list-shaped"
    branch for this shape and silently discarded all 185 options. This
    must come back gracefully truncated (AC2), never as the generic
    "Result too large ... not list-shaped" notice."""

    async def fake_get(self, path, params=None):
        return {
            "id": "f7528a78",
            "definition": _client_org_choices_source_definition(num_items=185, num_missing_tenant=37),
        }

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f7528a78", "field_id": "client_org"}
    )

    assert "note" not in payload  # never the generic not-list-shaped collapse
    assert "choices" in payload
    # Real-size payload (185 items, ~51KB in production) exceeds the
    # 20,000-char budget -- must be gracefully truncated, not emptied.
    assert payload["truncated"] is True
    assert payload["truncated_field"] == "choices"
    assert payload["original_count"] == 185
    assert 0 < len(payload["choices"]) < 185
    for item in payload["choices"]:
        assert item["attrs"]["haloClientId"] and item["attrs"]["rewstOrgId"]
        # tenantId is only required when the source data actually had one --
        # 37 of 185 genuinely don't (r2-amended AC5), and that's not a bug.
        if "tenantId" in item["attrs"]:
            assert item["attrs"]["tenantId"]
    assert len(json.dumps(payload, separators=(",", ":"))) <= 20_000


@pytest.mark.asyncio
async def test_form_get_field_id_choices_source_small_form_is_not_truncated(monkeypatch):
    """TC7 / AC7 complement: a choicesSource-shaped question small enough to
    fit under budget must come back with every option present intact and no
    truncated flag -- the choicesSource normalization itself is not lossy,
    only the pre-existing size cap (AC2) can ever drop entries."""

    async def fake_get(self, path, params=None):
        return {
            "id": "f1",
            "definition": _client_org_choices_source_definition(num_items=3, num_missing_tenant=1),
        }

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f1", "field_id": "client_org"}
    )

    assert payload.get("truncated") is None
    assert len(payload["choices"]) == 3
    assert "tenantId" not in payload["choices"][0]["attrs"]
    assert payload["choices"][1]["attrs"]["tenantId"]
    assert payload["choices"][2]["attrs"]["tenantId"]
    assert payload["choicesSourcePassAttributes"] == ["rewstOrgId", "haloClientId", "tenantId"]


@pytest.mark.asyncio
async def test_form_get_field_id_not_found_returns_structured_error(monkeypatch):
    """TC3 / AC3."""

    async def fake_get(self, path, params=None):
        return {"id": "f7528a78", "definition": _client_org_definition()}

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f7528a78", "field_id": "no_such_question"}
    )

    assert payload["error"]["code"] == "not_found"
    assert "no_such_question" in payload["error"]["message"]
    assert "f7528a78" in payload["error"]["message"]
    assert payload["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_form_get_without_field_id_is_unchanged(monkeypatch):
    """TC4 / AC4: existing callers that never pass field_id must see
    byte-identical behavior — same params sent upstream, same shape back."""
    mock_response = {"id": "f7528a78", "title": "Offboarding", "status": "published"}
    captured_params = {}

    async def fake_get(self, path, params=None):
        captured_params.update(params or {})
        return mock_response

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f7528a78", "include": ["versions"]}
    )

    assert payload == mock_response
    # No field_id given -> include is passed through exactly as the caller
    # gave it; "definition" is never force-added.
    assert captured_params["include"] == "versions"


@pytest.mark.asyncio
async def test_form_get_field_id_path_introduces_no_shared_state(monkeypatch):
    """TC6 / AC6: find_question_by_name is a pure function (no module-level
    or shared mutable state), and the field_id branch resolves its client
    the same per-request way as every other branch — two different tenants'
    concurrent-style calls never see each other's data."""
    import inspect

    from mspbots_forms_mcp.tools import _survey_walk

    # Pure-function check: no module-level mutable (non-callable, non-type)
    # globals that calls could accumulate state into.
    for attr_name, value in vars(_survey_walk).items():
        if attr_name.startswith("__"):
            continue
        assert inspect.isfunction(value) or inspect.ismodule(value) or isinstance(
            value, type
        ), f"unexpected module-level state: {attr_name}={value!r}"

    # Calling it twice with different inputs must not leak between calls.
    def_a = _client_org_definition(num_choices=1)
    def_b = {"pages": [{"elements": [{"type": "text", "name": "other_question"}]}]}
    first = find_question_by_name(def_a, "client_org")
    second = find_question_by_name(def_b, "client_org")
    assert first is not None and first["name"] == "client_org"
    assert second is None
    # Re-running the first lookup gives the same result — no mutation occurred.
    assert find_question_by_name(def_a, "client_org") == first

    # Per-request isolation: two different tenants, each with their own
    # definition, routed through the same field_id code path.
    async def fake_get(self, path, params=None):
        tenant = self._cookies()["X_Tenant_ID"]
        forms = {
            "tenant-A": _client_org_definition(num_choices=1),
            "tenant-B": {"pages": [{"elements": [{"type": "text", "name": "unrelated"}]}]},
        }
        return {"id": "f1", "definition": forms[tenant]}

    payload_a = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f1", "field_id": "client_org"}, tenant_id="tenant-A"
    )
    payload_b = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f1", "field_id": "client_org"}, tenant_id="tenant-B"
    )
    assert payload_a["name"] == "client_org"
    assert payload_b["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_form_get_field_id_pages_through_every_option(monkeypatch):
    """TC8 / AC8 (rework r3, source: INT test 2026-11-03). r2 stopped the
    185-option client_org question from collapsing, but one response only
    ever held 71 of them and there was no way to ask for the other 114 --
    the ticket's AC ("every option ... retained") still could not be met.
    Following next_choices_offset must now walk the whole option list, with
    every option's source attributes intact and no option seen twice."""

    async def fake_get(self, path, params=None):
        return {
            "id": "f7528a78",
            "definition": _client_org_choices_source_definition(num_items=185, num_missing_tenant=37),
        }

    seen = []
    offset = 0
    pages = 0
    while True:
        payload = await _call_form_get(
            monkeypatch,
            fake_get,
            {"form_id": "f7528a78", "field_id": "client_org", "choices_offset": offset},
        )
        pages += 1
        assert payload["choices_total"] == 185
        assert payload["choices_offset"] == offset
        assert payload["choices_returned"] == len(payload["choices"])
        assert payload["choices_returned"] > 0
        assert len(json.dumps(payload, separators=(",", ":"))) <= 20_000
        for item in payload["choices"]:
            assert item["attrs"]["haloClientId"] and item["attrs"]["rewstOrgId"]
            seen.append(item["value"])
        if payload["next_choices_offset"] is None:
            assert "truncated" not in payload
            break
        offset = payload["next_choices_offset"]
        assert pages < 20  # guard against a non-advancing page

    assert len(seen) == 185
    assert len(set(seen)) == 185


@pytest.mark.asyncio
async def test_form_get_field_id_choices_limit_caps_the_page(monkeypatch):
    """TC8 complement: choices_limit bounds a page even when more would
    fit, and the response still points at the rest."""

    async def fake_get(self, path, params=None):
        return {
            "id": "f7528a78",
            "definition": _client_org_choices_source_definition(num_items=185, num_missing_tenant=37),
        }

    payload = await _call_form_get(
        monkeypatch,
        fake_get,
        {"form_id": "f7528a78", "field_id": "client_org", "choices_limit": 5},
    )

    assert payload["choices_returned"] == 5
    assert payload["choices_total"] == 185
    assert payload["next_choices_offset"] == 5


@pytest.mark.asyncio
async def test_form_get_oversized_whole_form_keeps_the_base_record(monkeypatch):
    """TC9 / AC9 (rework r3, source: INT test 2026-11-03). The unfiltered
    call was never fixed: a whole-form response past the budget has no
    top-level list, so dump_json_capped discarded all of it and returned a
    116-char notice with no form data. The identifying fields must survive,
    and the response must name what was dropped so the caller can re-ask
    for it with field_id."""

    async def fake_get(self, path, params=None):
        return {
            "id": "f7528a78",
            "title": "[Ginko IT] Microsoft: User Offboarding",
            "status": "published",
            "questionCount": 41,
            "latestVersion": 4,
            "definition": _client_org_choices_source_definition(num_items=185, num_missing_tenant=37),
        }

    payload = await _call_form_get(
        monkeypatch, fake_get, {"form_id": "f7528a78", "include": ["definition"]}
    )

    assert payload["id"] == "f7528a78"
    assert payload["title"] == "[Ginko IT] Microsoft: User Offboarding"
    assert payload["questionCount"] == 41
    assert payload["latestVersion"] == 4
    assert payload["truncated"] is True
    assert payload["omitted_fields"] == ["definition"]
    assert len(json.dumps(payload, separators=(",", ":"))) <= 20_000
