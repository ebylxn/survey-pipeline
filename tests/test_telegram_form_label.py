# -*- coding: utf-8 -*-
"""Submission messages must name the survey form they belong to."""

import pytest

from services.admin_moderator import render_dossier_message
from services.telegram_ui import form_label, submission_form_line


@pytest.fixture
def registry():
    from core.state import FORM_REGISTRY

    saved = dict(FORM_REGISTRY)
    FORM_REGISTRY.clear()
    try:
        yield FORM_REGISTRY
    finally:
        FORM_REGISTRY.clear()
        FORM_REGISTRY.update(saved)


def _schema(form_id, title):
    from services.form_engine import FormSchema

    return FormSchema(form_id=form_id, title=title, questions=[])


def test_form_label_uses_the_registered_title_and_id(registry):
    registry["camp_x"] = _schema("camp_x", "Khảo sát tháng 9")
    assert submission_form_line("camp_x") == "• <b>Biểu mẫu:</b> <b>Khảo sát tháng 9</b> (<code>camp_x</code>)"


def test_form_label_falls_back_to_the_id_and_escapes_html(registry):
    assert form_label("unknown_form") == "<code>unknown_form</code>"
    assert form_label('bad"><b>id') == '<code>bad"&gt;&lt;b&gt;id</code>'
    assert submission_form_line(None) == ""


def test_dossier_message_names_the_form(registry):
    registry["camp_x"] = _schema("camp_x", "Khảo sát tháng 9")
    text = render_dossier_message({
        "submission_id": "VN-1",
        "form_id": "camp_x",
        "status": "APPROVED",
        "gross_income": 1000,
        "net_income": 900,
        "trips_count": 3,
    })
    assert "Biểu mẫu:" in text and "Khảo sát tháng 9" in text and "<code>camp_x</code>" in text
