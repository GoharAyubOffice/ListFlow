"""Cost -> sell price engine: margin, eBay fees, .99 rounding (spec §4.2).

Implemented in Phase 1. All money is Decimal — never float. Pure logic — no I/O.

Formula (spec §4.2):
    sell_price = (cost + fixed_fee) / (1 - fvf_rate - target_margin)
    then round UP to the nearest x.99
    passes_floor = margin_actual >= 0.20  (test-and-kill floor)
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from listflow.models import PricingResult

DEFAULT_MARGIN = Decimal("0.20")
DEFAULT_FVF_RATE = Decimal("0.128")  # eBay final value fee estimate
DEFAULT_FIXED_FEE = Decimal("0.30")  # per-order fixed fee, GBP
MARGIN_FLOOR = Decimal("0.20")  # test-and-kill floor — below this the CLI refuses

_PENNY = Decimal("0.01")
_BASIS_POINT = Decimal("0.0001")


def _round_up_to_psych_99(value: Decimal) -> Decimal:
    """Smallest price >= value that ends in .99 (10.00 -> 10.99, 12.99 -> 12.99)."""
    candidate = value.to_integral_value(rounding=ROUND_FLOOR) + Decimal("0.99")
    if candidate < value:
        candidate += 1
    return candidate


def price(
    cost: Decimal,
    *,
    margin: Decimal = DEFAULT_MARGIN,
    fvf_rate: Decimal = DEFAULT_FVF_RATE,
    fixed_fee: Decimal = DEFAULT_FIXED_FEE,
    floor: Decimal = MARGIN_FLOOR,
) -> PricingResult:
    """Price a product for eBay. Decimal in, Decimal out — floats raise TypeError."""
    for name, value in (
        ("cost", cost),
        ("margin", margin),
        ("fvf_rate", fvf_rate),
        ("fixed_fee", fixed_fee),
        ("floor", floor),
    ):
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")

    if cost <= 0:
        raise ValueError(f"cost must be positive, got {cost}")
    if margin < 0 or fvf_rate < 0 or fixed_fee < 0 or floor < 0:
        raise ValueError("margin, fvf_rate, fixed_fee and floor must all be >= 0")
    denominator = 1 - fvf_rate - margin
    if denominator <= 0:
        raise ValueError(
            f"fvf_rate + margin must stay below 1, got {fvf_rate} + {margin}"
        )

    sell_price = _round_up_to_psych_99((cost + fixed_fee) / denominator)
    ebay_fees_est = (sell_price * fvf_rate + fixed_fee).quantize(_PENNY, ROUND_HALF_UP)
    net_profit_est = (sell_price - cost - ebay_fees_est).quantize(_PENNY, ROUND_HALF_UP)
    margin_actual = (net_profit_est / sell_price).quantize(_BASIS_POINT, ROUND_HALF_UP)

    return PricingResult(
        cost=cost,
        ebay_fees_est=ebay_fees_est,
        target_margin=margin,
        sell_price=sell_price,
        net_profit_est=net_profit_est,
        margin_actual=margin_actual,
        passes_floor=margin_actual >= floor,
    )
