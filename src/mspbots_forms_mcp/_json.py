"""Token-economical JSON serialization for tool return values.

Compact (no indent), non-ASCII-preserving, and size-capped so a single
tool call can never blow past the ~20,000-char budget an agent's context
can reasonably absorb.
"""

import json
from typing import Any

MAX_CHARS = 20_000


def _compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def dump_json_capped(data: Any, max_chars: int = MAX_CHARS) -> str:
    """Serialize data compactly, truncating the largest list field (or the
    top-level list) if the result would exceed max_chars, rather than ever
    returning an unbounded blob.
    """
    s = _compact(data)
    if len(s) <= max_chars:
        return s

    if isinstance(data, list):
        return _truncate_list(data, max_chars, wrap_key="items")

    if isinstance(data, dict):
        list_keys = [k for k, v in data.items() if isinstance(v, list)]
        if list_keys:
            key = max(list_keys, key=lambda k: len(_compact(data[k])))
            items = data[key]
            lo, hi = 0, len(items)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                candidate = dict(data)
                candidate[key] = items[:mid]
                candidate["truncated"] = True
                candidate["truncated_field"] = key
                candidate["original_count"] = len(items)
                if len(_compact(candidate)) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            candidate = dict(data)
            candidate[key] = items[:lo]
            candidate["truncated"] = True
            candidate["truncated_field"] = key
            candidate["original_count"] = len(items)
            return _compact(candidate)

    # Not list-shaped and still too big. Dropping the whole payload loses
    # every usable field with it (PRD-21379: a 62KB form response collapsed
    # to a 116-char notice carrying no form data at all), so shed the
    # largest values one at a time and keep whatever still fits.
    if isinstance(data, dict):
        return _shed_largest_values(data, max_chars, len(s))

    return _compact(
        {
            "truncated": True,
            "note": f"Result too large ({len(s)} chars) to return in full and not list-shaped; narrow your query.",
        }
    )


def _shed_largest_values(data: dict, max_chars: int, original_chars: int) -> str:
    """Drop the biggest values until the rest fits, naming what was dropped.

    Keys are shed largest-first so the small scalar fields that identify the
    record (id, title, status, counts) survive — those are what a caller
    needs to narrow the next query.
    """
    kept = dict(data)
    omitted: list[str] = []
    by_size = sorted(kept, key=lambda k: len(_compact(kept[k])), reverse=True)
    for key in by_size:
        del kept[key]
        omitted.append(key)
        candidate = dict(kept)
        candidate["truncated"] = True
        candidate["omitted_fields"] = omitted
        candidate["note"] = (
            f"Result too large ({original_chars} chars); the fields in "
            "omitted_fields were dropped. Re-request them one at a time "
            "(e.g. mspbots_forms_form_get's field_id) to get them in full."
        )
        out = _compact(candidate)
        if len(out) <= max_chars:
            return out

    return _compact(
        {
            "truncated": True,
            "omitted_fields": omitted,
            "note": f"Result too large ({original_chars} chars) to return in full; narrow your query.",
        }
    )


def dump_choices_page(base: dict, choices: list, offset: int, total: int, max_chars: int = MAX_CHARS) -> str:
    """Serialize `base` plus as many of `choices` as fit, with paging metadata.

    `choices` is the already-offset (and already limited) slice; `total` is
    the option count of the whole question. Unlike dump_json_capped's generic
    list truncation, this always reports where the returned window starts and
    where to resume, so a caller can page through every option of an
    oversized question instead of silently seeing only the first screenful
    (PRD-21379: 185 options, only 71 of which fit one 20,000-char response).
    """
    lo, hi = 0, len(choices)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_compact(_choices_page(base, choices, offset, total, mid))) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    page = _choices_page(base, choices, offset, total, lo)
    if lo == 0 and choices:
        page["note"] = (
            "Not even one option fits the response budget; the option at "
            f"offset {offset} is too large to return."
        )
    return _compact(page)


def _choices_page(base: dict, choices: list, offset: int, total: int, count: int) -> dict:
    next_offset = offset + count
    incomplete = next_offset < total
    page = {
        **base,
        "choices": choices[:count],
        "choices_offset": offset,
        "choices_returned": count,
        "choices_total": total,
        "next_choices_offset": next_offset if incomplete else None,
    }
    if incomplete:
        # Keep the pre-paging truncation keys so callers written against the
        # PRD-21379 r2 response shape keep working.
        page["truncated"] = True
        page["truncated_field"] = "choices"
        page["original_count"] = total
    return page


def _truncate_list(items: list, max_chars: int, wrap_key: str) -> str:
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = {wrap_key: items[:mid], "truncated": True, "original_count": len(items)}
        if len(_compact(candidate)) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    return _compact({wrap_key: items[:lo], "truncated": True, "original_count": len(items)})


def error_envelope(code: str, message: str, retryable: bool) -> str:
    return _compact({"error": {"code": code, "message": message, "retryable": retryable}})
