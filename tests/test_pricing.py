"""Table-driven pricing tests: floor, below-floor, .99 rounding, fee edges,
zero/negative guard (spec §9.1). Phase 1 — written before pricing.py logic.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from listflow.models import PricingResult, Product
from listflow.pricing import MARGIN_FLOOR, price

D = Decimal

# Expected values hand-computed from the spec §4.2 formula:
#   raw = (cost + fixed_fee) / (1 - fvf_rate - margin), rounded UP to x.99
#   fees = sell * fvf_rate + fixed_fee (penny, HALF_UP)
#   net = sell - cost - fees (penny, HALF_UP); margin_actual = net / sell (4dp, HALF_UP)
# (id, cost, margin, fvf, fixed, sell, fees, net, margin_actual, passes_floor)
TABLE = [
    ("typical", "10.00", "0.20", "0.128", "0.30", "15.99", "2.35", "3.64", "0.2276", True),
    ("exact-99", "8.42928", "0.20", "0.128", "0.30", "12.99", "1.96", "2.60", "0.2002", True),
    ("bump-up", "8.43", "0.20", "0.128", "0.30", "13.99", "2.09", "3.47", "0.2480", True),
    ("mid-band", "6.756", "0.20", "0.128", "0.30", "10.99", "1.71", "2.52", "0.2293", True),
    ("floor-fail", "10.00", "0.10", "0.128", "0.30", "13.99", "2.09", "1.90", "0.1358", False),
    ("tiny-cost", "0.01", "0.20", "0.128", "0.30", "0.99", "0.43", "0.55", "0.5556", True),
    ("no-fees", "10.00", "0.20", "0", "0", "12.99", "0.00", "2.99", "0.2302", True),
]


@pytest.mark.parametrize(
    ("cost", "margin", "fvf", "fixed", "sell", "fees", "net", "margin_actual", "passes"),
    [row[1:] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_pricing_table(cost, margin, fvf, fixed, sell, fees, net, margin_actual, passes):
    result = price(D(cost), margin=D(margin), fvf_rate=D(fvf), fixed_fee=D(fixed))
    assert result.sell_price == D(sell)
    assert result.ebay_fees_est == D(fees)
    assert result.net_profit_est == D(net)
    assert result.margin_actual == D(margin_actual)
    assert result.passes_floor is passes
    assert result.cost == D(cost)
    assert result.target_margin == D(margin)


@pytest.mark.parametrize("cost", ["0.05", "1.00", "2.37", "19.99", "149.50", "999.99"])
def test_sell_price_always_ends_in_99_and_never_rounds_down(cost):
    result = price(D(cost))
    assert result.sell_price % 1 == D("0.99")
    raw = (D(cost) + D("0.30")) / (1 - D("0.128") - D("0.20"))
    assert result.sell_price >= raw


def test_all_money_fields_are_decimal():
    result = price(D("7.20"))
    fields = (
        "cost",
        "ebay_fees_est",
        "target_margin",
        "sell_price",
        "net_profit_est",
        "margin_actual",
    )
    for field in fields:
        assert isinstance(getattr(result, field), Decimal), field


def test_margin_floor_constant_is_20_percent():
    assert D("0.20") == MARGIN_FLOOR


@pytest.mark.parametrize("bad_cost", ["0", "-1", "-0.01"])
def test_zero_or_negative_cost_rejected(bad_cost):
    with pytest.raises(ValueError, match="cost"):
        price(D(bad_cost))


def test_float_arguments_rejected():
    with pytest.raises(TypeError):
        price(9.99)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        price(D("5"), margin=0.25)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        price(D("5"), fvf_rate=0.128)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        price(D("5"), fixed_fee=0.3)  # type: ignore[arg-type]


def test_fee_plus_margin_must_leave_headroom():
    with pytest.raises(ValueError):
        price(D("10"), margin=D("0.9"), fvf_rate=D("0.128"))
    with pytest.raises(ValueError):
        price(D("10"), margin=D("1"), fvf_rate=D("0"))


def test_negative_rates_rejected():
    with pytest.raises(ValueError):
        price(D("10"), margin=D("-0.1"))
    with pytest.raises(ValueError):
        price(D("10"), fvf_rate=D("-0.01"))
    with pytest.raises(ValueError):
        price(D("10"), fixed_fee=D("-0.30"))


def _product_kwargs() -> dict:
    return {
        "source_platform": "amazon",
        "source_url": "https://www.amazon.co.uk/dp/B000000000",
        "source_id": "B000000000",
        "title_raw": "Pet Hair Remover Brush",
        "description_html": "<p>Removes pet hair fast.</p>",
        "bullet_points": ["Easy to clean"],
        "images": [{"source_url": "https://img.example.com/1.jpg", "width": 800, "height": 800}],
        "variants": [],
        "base_cost": D("3.50"),
        "currency": "GBP",
        "item_specifics": {"Colour": "Blue"},
        "extracted_at": datetime.now(UTC),
    }


def test_product_rejects_float_money():
    with pytest.raises(ValidationError):
        Product(**{**_product_kwargs(), "base_cost": 3.5})


def test_pricing_result_rejects_float_money():
    with pytest.raises(ValidationError):
        PricingResult(
            cost=1.5,
            ebay_fees_est=D("1.00"),
            target_margin=D("0.20"),
            sell_price=D("9.99"),
            net_profit_est=D("1.00"),
            margin_actual=D("0.20"),
            passes_floor=True,
        )
