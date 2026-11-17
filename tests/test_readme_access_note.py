"""TC17 (PRD-21397): README tells the deploying admin to allow the transfer tools for workspace admins only."""

from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def test_readme_has_admin_only_allowlist_note():
    text = README.read_text(encoding="utf-8")
    start = text.index("### Access control")
    section = text[start : text.index("\n#", start + 1)].lower()
    assert "mspbots_forms_form_export" in section
    assert "mspbots_forms_form_import" in section
    assert "workspace admin" in section
    assert "allowlist" in section
    assert "no role check" in section
