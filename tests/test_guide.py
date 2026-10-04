"""The in-app user guide: every topic valid, links resolve, role filtering, generated sections, search."""

import re

import flet as ft

from databridge.config import Settings
from databridge.core.auth import PERMISSIONS
from databridge.services import guide
from databridge.ui.app import App

NAV_KEYS = {k for k, *_ in App.NAV}


def test_topics_are_valid_and_links_resolve():
    all_topics = guide.topics("admin")
    assert len(all_topics) >= 15
    slugs = {t.slug for t in all_topics}
    for t in all_topics:
        assert t.title and t.body.startswith("# "), t.slug
        assert t.permission in PERMISSIONS, (t.slug, t.permission)
        assert hasattr(ft.Icons, t.icon), (t.slug, t.icon)
        for kind, target in re.findall(r"\]\((guide|app):([\w-]+)\)", t.body):
            assert target in (slugs if kind == "guide" else NAV_KEYS), (t.slug, kind, target)
        # guide placeholders ({{functions}}, {{settings}}, {{api_base}}, {{api_host}}) must all be filled in;
        # other {{...}} are documentation of DataBridge's own templates (e.g. {{today}} in REST requests)
        assert not re.search(r"\{\{(functions|settings|api_base|api_host)\}\}", guide.render(t)), t.slug


def test_every_page_has_help_and_roles_see_their_topics():
    for key in NAV_KEYS - {"docs"}:
        assert guide.topic_for_page(key, "admin"), f"no guide topic for page {key}"
    viewer = {t.slug for t in guide.topics("viewer")}
    designer = {t.slug for t in guide.topics("designer")}
    admin = {t.slug for t in guide.topics("admin")}
    assert viewer < designer < admin
    assert "getting-started" in viewer and "mapping-studio" in viewer
    assert "admin-company-llm" not in designer and "ai-workflows" not in viewer and "connections" not in viewer


def test_links_a_role_cannot_open_become_text():
    start = guide.get("getting-started", "viewer")
    as_viewer, as_admin = guide.render(start, "viewer"), guide.render(start, "admin")
    assert "(guide:connections)" in as_admin and "(guide:connections)" not in as_viewer
    assert "connection" in as_viewer  # the words stay


def test_generated_reference_sections():
    from databridge.engine.formula import FUNCTIONS

    formulas = guide.render(guide.get("formulas", "viewer"))
    for name in FUNCTIONS:
        assert f"`{name}`" in formulas
    settings_topic = guide.render(guide.get("admin-settings", "admin"))
    for name in Settings.model_fields:
        assert f"DATABRIDGE_{name.upper()}" in settings_topic
        assert guide.SETTING_HELP.get(name), f"describe setting {name} in guide.SETTING_HELP"


def test_search():
    hits = [t.slug for t, _ in guide.search("reject report", "designer")]
    assert hits and "runs" in hits
    assert guide.search("company key gateway", "designer")[0][0].slug == "company-models"
    assert not guide.search("config.yaml upload", "viewer")  # admin topic hidden from viewers
    assert guide.search("", "admin") == []


def test_guide_images_exist():
    import re

    from databridge.services import guide
    from databridge.ui.web_assets import GUIDE_IMAGES

    refs = [ref for t in guide._all() for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", t.body)]
    assert refs, "the architecture topic should have diagrams"
    for ref in refs:
        assert ref.startswith("guide/") and (GUIDE_IMAGES / ref.split("/", 1)[1]).is_file(), ref
