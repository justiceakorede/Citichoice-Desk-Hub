"""Template rendering and notification settings. Pure functions. NOT YET RUN in the build sandbox."""
import pytest

from app.services import notify as nf


def test_html_values_are_escaped_but_html_placeholders_are_not():
    out = nf.render("<p>{{business}}</p>{{advice_html}}", {"business": "A&B <x>", "advice_html": "<i>ok</i>"}, html=True)
    assert out == "<p>A&amp;B &lt;x&gt;</p><i>ok</i>"


def test_plain_text_is_not_escaped_and_unknown_placeholders_stay_visible():
    assert nf.render("{{business}} {{nope}}", {"business": "A&B"}) == "A&B {{nope}}"


def test_unknown_placeholders_are_found():
    assert nf.unknown_placeholders("{{business}} {{typo}}", "{{cost}}") == {"typo"}


def test_default_templates_only_use_known_placeholders():
    for key, tpl in nf.DEFAULTS.items():
        assert not nf.unknown_placeholders(tpl["email_subject"], tpl["email_html"], tpl["email_text"], *tpl["wa_params"]), key


def test_render_all_fills_whatsapp_wording_and_variables():
    tpl = dict(nf.DEFAULTS["report"], wa_template="weekly_report", wa_language="en")
    r = nf.render_all(tpl, nf.sample_report_ctx())
    assert r["wa_params"][0] == "Sample Business" and "Sample Business" in r["wa_wording"] and "{{" not in r["wa_wording"]
    assert "<table" in r["html"] and "Buy WhatsApp credit" in r["html"] and "{{" not in r["html"]


def test_set_conf_validates_low_balance_settings():
    class T:
        notify = None
    t = T()
    nf.set_conf(t, low_threshold_usd=10, low_repeat_hours=12)
    assert t.notify["low_threshold_usd"] == 10 and t.notify["low_repeat_hours"] == 12
    with pytest.raises(ValueError):
        nf.set_conf(t, low_threshold_usd=-1)
    with pytest.raises(ValueError):
        nf.set_conf(t, low_repeat_hours=999)
