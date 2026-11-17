# mspbots-forms-mcp

MCP server for the **MSPbots Forms API** — lets an Agent build forms, publish them, manage share links, and read back/analyze responses.

> **Naming note:** this is not a third-party vendor integration. It wraps an **internal MSPbots App API** (form builder + response collection feature — the underlying API's own paths/fields still say "survey", e.g. `/surveys`, `surveyVersionId`; the tools here present it to agents as "form" instead) described in the API contract attached to [PRD-15818](https://app.clickup.com/t/2280862/PRD-15818). "MSP" in the header names below refers to the MSPbots Agent Platform itself, not an external MSP tool vendor. This follows the same pattern as `ticketqa-mcp` (PRD-14991) — an internal-App wrapper, not a `vendor-mcp-template` SOP project.

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## ✅ Build status: read tools verified against real PROD data (2026-08-07)

This server was built from the API contract Leo Yang pasted into the PRD-15818 comment — **the actual backend repository was not read**. It's since been tested end-to-end (real MCP handshake + tool call, not just curl) against the real production deployment:

- **Real host found by trial**: the contract didn't say where this API is deployed. A working browser request (captured from the actual `app-forms` UI) revealed it: `https://agent.mspbots.ai/apps/app-forms/api/...` — a `/apps/<name>/api/...` prefix the contract never mentioned.
- **UPDATE 2026-08-26**: `app-forms` now also has a real INT deployment. A direct request to `agentint.mspbots.ai/apps/app-forms/api/surveys` returns real 200 data (previously 404'd — see git history for the original "PROD-only" finding, which was accurate as of 2026-08-07/08-24 but is no longer current). A real `mspbots_forms_form_list` call through the gateway with an INT tenant now returns that same INT-only data, not PROD data — confirmed the gateway's `X-MSP-Host` routing for `mspbots-forms` now correctly follows environment rather than being hardcoded to PROD. **Practical effect**: write-tool testing against INT is now safe (touches real INT data, not PROD) — re-verify this isn't stale again before assuming it, the same way this note itself corrected a stale assumption.
- **Undocumented tenant requirement, confirmed by isolation test**: the contract claims tenant is derived purely from the JWT ("tenantId is taken from the token, never from request parameters"). Live testing proved that's only true past the app-routing gateway — without an `X_Tenant_ID` **cookie** (not a header!), the same token/path 404s ("App not found"); adding just that cookie (nothing else changed) turns it into 200. Same class of gap `ticketqa-mcp` hit (there it was an undocumented header; here it's a cookie).
- **Verified working with real data**: `mspbots_forms_form_list`, `mspbots_forms_form_get` (with `include=questions`), `mspbots_forms_response_summary`, `mspbots_forms_share_list` — all returned real, correctly-shaped data for an actual PROD form (`Untitled survey`, id `9030b62e-4000-4e61-ae98-2b7285c86ea6` — that's the real record's own title, from before this repo's tool-facing terminology was changed to "form").
- **Not yet tested**: any write tool (`mspbots_forms_form_create/update/publish/delete/quick_publish`, `mspbots_forms_share_create/update/delete`) — deliberately not exercised against PROD without explicit sign-off, since they'd create/mutate/delete real data. `mspbots_forms_response_list` also untested (the one test form has 0 responses).
- The 3 referenced design docs (`survey-share-design.md`, `route-a-design.md`, `mcp-design.md`) and the real `service/lib/question-kinds.ts` source were still not read — the question-kind table below remains transcribed from the comment, not source-verified. The `text`/`textarea` kinds that were tested match, though.

## Overview

Implements the [Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP transport) and exposes **16 tools**: the **14** specified in the API contract, covering three areas — **building/editing forms**, **managing how they're shared**, and **reading back what respondents submitted** — plus **2** added for PRD-21397 to **transfer a form definition across tenants** via an export/import JSON file. An agent should reach for this MCP for requests like:

- "Build me a quick customer-satisfaction form and get me a link to send out" → `mspbots_forms_form_quick_publish`
- "I need to tweak the wording on question 3 of the NPS form" → `mspbots_forms_form_update`
- "How many people finished the NPS form, what's the average score?" → `mspbots_forms_response_summary`
- "Pause this share link for a bit, we're getting too many responses right now" → `mspbots_forms_share_update` with `action="pause"`
- "Show me the actual open-text comments people left, not just the stats" → `mspbots_forms_response_list`
- "This form's done — delete it and everything tied to it" → `mspbots_forms_form_delete`
- "Copy this form into the client's tenant exactly as-is" → `mspbots_forms_form_export` then `mspbots_forms_form_import`

| Category | Tool | Annotations |
|---|---|---|
| Form | `mspbots_forms_form_list`, `mspbots_forms_form_get` | readOnly |
| | `mspbots_forms_form_create`, `mspbots_forms_form_publish` | — |
| | `mspbots_forms_form_update` | idempotent |
| | `mspbots_forms_form_delete` | **destructive**, requires `confirm=true` |
| | `mspbots_forms_form_quick_publish` | composite: create + publish + share in one call |
| Share | `mspbots_forms_share_list`, `mspbots_forms_share_get` | readOnly |
| | `mspbots_forms_share_create` | — |
| | `mspbots_forms_share_update` | idempotent; `action` param (`pause`/`resume`/`close`/`rotate`) consolidates 4 lifecycle operations |
| | `mspbots_forms_share_delete` | **destructive**, requires `confirm=true` (responses preserved) |
| Response | `mspbots_forms_response_summary` | readOnly — call this first for analysis |
| | `mspbots_forms_response_list` | readOnly — columnar format, only when individual answers are needed |
| Transfer | `mspbots_forms_form_export` | — produces a versioned JSON file |
| | `mspbots_forms_form_import` | — creates a new unpublished draft; data-source/tenant-scoped fields are imported unbound and flagged in a report (PRD-21397) |

### Access control (PRD-21397)

In the connector's per-tool allowlist, allow `mspbots_forms_form_export` and `mspbots_forms_form_import` for the **workspace admin** role only, and leave them denied for every other role (deny-by-default already applies). This server performs no role check of its own: it receives no user role, so the allowlist is the only gate.

Per the contract, the public respondent-facing endpoints (`/api/public/*` — unlock, save draft, submit) are **deliberately not exposed as tools**: an agent shouldn't be able to submit a form on an anonymous respondent's behalf.

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The server starts on `http://localhost:8080`.

### Local (uv)

```bash
uv sync
python -m mspbots_forms_mcp
```

## Health Check

```bash
curl http://localhost:8080/health
# {"status": "ok"}
```

No credentials are required for the health endpoint.

## Authentication parameters (Authentication)

Every request to `/mcp` must include the following HTTP headers:

| Header | Type | Required | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-API-Key` | string | Yes | None | None (free text, JWT) | Access credential issued by the Agent Platform (platform JWT). This service forwards it as-is as `Authorization: Bearer <token>` on the downstream request. | `X-API-Key: eyJhbGciOiJFZERTQSJ9...` |
| `X-MSP-Host` | string | Yes | None | None (free text, base URL) | Host where the Forms API runs. This service appends `/apps/app-forms/api/<endpoint>` to get the full request URL (the real path, **confirmed by live testing**; the contract document only lists the bare `/api/<endpoint>` and does not mention the `/apps/app-forms` prefix). **Updated 2026-08-26**: the earlier note "the App is deployed only on PROD and the gateway always forwards to PROD" is outdated; live testing shows `app-forms` now also has a real INT deployment, and requests through the gateway are routed correctly by environment (INT tenants get INT data) instead of always being forwarded to PROD. | `X-MSP-Host: https://agent.mspbots.ai` |
| `X-MSP-Tenant-Id` | string | Yes | None | None (free text, UUID) | Tenant ID. The contract document says the tenant is resolved entirely from the token and needs no extra input; **live testing proved this only holds for the business layer**: the APISIX app-routing gateway returns 404 ("App not found") before it gets the tenant information, so this value is required. This service forwards it as the **`X_Tenant_ID` cookie** on the downstream request (not a header; this was only discovered by live testing and the contract does not mention it at all). | `X-MSP-Tenant-Id: e9f794fe-a6b4-4f35-bd2f-fcd19c5cc308` |

> ⏳ **Transition-period compatibility (since 2026-09-23)**: `X-API-Key` replaces the former `X-MSP-Token`. Tenant credentials written before the rename
> still exist in the registry under the old name and are injected as-is by the gateway, so the middleware falls back to reading
> `X-MSP-Token` when `X-API-Key` is absent; when both are present, `X-API-Key` wins. Once every tenant's credentials have been re-saved under the new name, delete
> that fallback in `server.py` and `tests/test_middleware.py::test_legacy_token_header_is_still_accepted`.
>
> `X-MSP-Tenant-Id` is **not affected by this rename**; it is still required and still forwarded downstream.

Missing any of the three headers returns `401 Unauthorized`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_HTTP_PORT` | `8080` | Listening port |
| `MCP_HTTP_HOST` | `0.0.0.0` | Listening host |

## MCP Endpoint

```
POST http://localhost:8080/mcp
```

Connect your MCP client with:
- Transport: `http` (Streamable HTTP)
- Headers: `X-API-Key`, `X-MSP-Host`, `X-MSP-Tenant-Id` (all required)

## Question DSL (for `mspbots_forms_form_create` / `mspbots_forms_form_update`)

Each question is a dict: `kind` (required), `title` (required), `required` (bool), plus kind-specific fields. Question `name`s (`q1`, `q2`, ...) are auto-assigned by the server in definition order.

| `kind` | SurveyJS type | Extra fields |
|---|---|---|
| `text` | `text` | `placeholder` |
| `textarea` | `comment` | `placeholder`, `rows` |
| `radio` | `radiogroup` | `choices` (required) |
| `checkbox` | `checkbox` | `choices` (required) |
| `dropdown` | `dropdown` | `choices` (required), `placeholder` |
| `rating` | `rating` | `steps`, `style` (`numbers`/`stars`/`smileys`), `lowLabel`, `highLabel` |
| `boolean` | `boolean` | `yesLabel`, `noLabel` |
| `date` | `text` + `inputType:date` | `earliest`, `latest` |

`showIf`: `{"question": "<earlier question name>", "operator": "is"/"isNot"/"greaterThan"/"atLeast"/"lessThan"/"atMost"/"contains"/"answered"/"empty", "value": ...}` — `question` can only reference an **earlier** question (server rejects forward/circular refs with `422`).

Multi-page forms use `pages: [{title?, questions: [...]}]` instead of a flat `questions` list.

## Test Example

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <token>" \
  -H "X-MSP-Host: https://agent.mspbots.ai" \
  -H "X-MSP-Tenant-Id: <tenant-id>" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "mspbots_forms_form_create",
      "arguments": {
        "title": "Customer satisfaction",
        "questions": [
          {"kind": "rating", "title": "Overall satisfaction", "required": true, "steps": 5, "lowLabel": "Poor", "highLabel": "Great"},
          {"kind": "radio", "title": "How did you hear about us?", "choices": ["Website", "Friend", "Ad"]}
        ]
      }
    }
  }'
```

## API Reference

- Source spec: API contract pasted into a comment on [PRD-15818](https://app.clickup.com/t/2280862/PRD-15818) by Leo Yang
- Parent program: [PRD-14513 — MSPbots MCP gateway](https://app.clickup.com/t/2280862/PRD-14513)

## Known Gaps / Implementation Notes

- **Verified 2026-08-07 against real PROD data**: `mspbots_forms_form_list`, `mspbots_forms_form_get` (incl. `include=questions`), `mspbots_forms_response_summary`, `mspbots_forms_share_list` all returned correctly-shaped real data for a live PROD form. The real host (`https://agent.mspbots.ai/apps/app-forms/api/...`) and the undocumented `X-MSP-Tenant-Id` → `X_Tenant_ID` cookie requirement (see Auth table above) were both discovered this way, not from the contract doc.
- **Write tools are still untested**: `mspbots_forms_form_create/update/publish/delete/quick_publish` and `mspbots_forms_share_create/update/delete` have not been called — deliberately avoided mutating real PROD data without explicit sign-off. `mspbots_forms_response_list` is also untested (the one available test form has 0 responses to list). Test these against a disposable form before trusting them.
- `mspbots_forms_form_quick_publish` is a composite tool (3 chained API calls: create → publish → share-create) with **no rollback** — if the second or third call fails, the form and/or its published version will already exist. This is a client-side convenience, not an atomic server-side operation.
- `mspbots_forms_share_update`'s `action="rotate"` calls a genuinely different endpoint (`POST /api/shares/:shareId/token`) than the other three actions (`PATCH /api/shares/:shareId` with a `status` field) — this was inferred from the contract's description ("merge the four lifecycle actions into one tool") rather than the contract giving an explicit combined-tool spec; the underlying two endpoints themselves are separately documented and that part is solid.
- The `question-kinds` mapping table above was transcribed from the ClickUp comment's rendered markdown table, not read from the actual `service/lib/question-kinds.ts` source file the comment says is authoritative. The `text`/`textarea` kinds were confirmed correct against a real form; the other 6 kinds (`radio`/`checkbox`/`dropdown`/`rating`/`boolean`/`date`) are still unverified.
- `mspbots_forms_form_update`'s interaction between `definition` and `questions`/`pages` when both are given is unspecified in the contract — this implementation includes both in the request body if both are passed, which may or may not be what the real server expects. Avoid combining them until confirmed.
- **`app-forms` App deployment (UPDATED 2026-08-26)**: this used to be PROD-only (a direct request to `agentint.mspbots.ai/apps/app-forms/api/...` 404'd, and the gateway hardcoded `X-MSP-Host` to PROD's `agent.mspbots.ai` regardless of environment — confirmed live 2026-08-24). That's no longer the case: `app-forms` now has a real INT deployment, and a real `mspbots_forms_form_list` call through the gateway with an INT tenant returns real INT data, not PROD data (confirmed live 2026-08-26). Don't assume either state without re-checking — this exact assumption already went stale once.
- **Terminology note**: tool names/descriptions/params say "form" (e.g. `form_id`, `mspbots_forms_form_create`), matching the "MSPbots Forms" product name — but the underlying REST API's own paths and JSON fields still say "survey" (`/surveys`, `surveyVersionId`) and are left as-is, since those are the real backend's wire contract, not something this MCP controls.
