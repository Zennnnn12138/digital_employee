from app.parser import rule_parse


def test_budget_write_extracts_campaign_and_amount():
    parsed = rule_parse("把店铺 SHOP-1001 广告计划 C-501 的日预算调整到 360，原因是大促放量")
    writes = [item for item in parsed.actions if item["kind"] == "write"]
    assert parsed.shop_id == "SHOP-1001"
    assert parsed.roles == ["ads"]
    assert writes[0]["tool"] == "adjust_ad_budget"
    assert writes[0]["params"]["new_daily_budget"] == 360
    assert not parsed.missing_fields


def test_refund_clause_is_not_a_write():
    parsed = rule_parse("看看 SHOP-1001 订单 O-9003 适用的退款条款，先不要退款")
    assert "aftersales" in parsed.roles
    assert "fulfillment" not in parsed.roles
    assert all(item["kind"] == "read" for item in parsed.actions)


def test_missing_shop_asks_for_clarification():
    parsed = rule_parse("帮我看看最近的广告消耗")
    assert "shop_id" in parsed.missing_fields
    assert parsed.actions == []
