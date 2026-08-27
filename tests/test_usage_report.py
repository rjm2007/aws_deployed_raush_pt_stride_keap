from rpt_agent.usage_report import _cost, recipient_label


def test_recipient_label_masks_phone_and_is_stable():
    first = recipient_label("+15555550123", "report-secret")
    second = recipient_label("+15555550123", "report-secret")
    assert first == second
    assert first.startswith("Tester ***0123 (")
    assert "+15555550123" not in first


def test_provider_debits_render_as_positive_cost():
    assert str(_cost("-0.08320")) == "0.08320"
    assert _cost(None) is None
