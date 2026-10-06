from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from app.models import CommissionType, ProviderCommissionRate, ProviderCommissionTier
from app.schemas import CommissionTiersSet
from app.services.commission_service import calculate_commission, commission_for, find_tier

D = Decimal
PCT, FLAT, BOTH = CommissionType.PERCENTAGE, CommissionType.FLAT, CommissionType.PERCENTAGE_PLUS_FLAT


def tier(lo, hi, fee="0", pct="0", flat="0"):
    return NS(
        min_amount=D(lo),
        max_amount=None if hi is None else D(hi),
        provider_fee=D(fee),
        percentage=D(pct),
        flat_fee=D(flat),
    )


def tiered(*tiers):
    return NS(tiers=list(tiers), commission_type=PCT, percentage=D("0"), flat_fee=D("0"))


# Provider fee per band; we keep 20% of it (+ a flat 0.10 on the top band).
SCHEDULE = tiered(
    tier("1", "100", fee="1.00", pct="0.20"),
    tier("100.01", "500", fee="2.50", pct="0.20"),
    tier("500.01", None, fee="5.00", pct="0.20", flat="0.10"),
)


def test_commission_is_share_of_the_provider_fee():
    assert commission_for(SCHEDULE, D("50")) == D("0.20")    # 1.00 * 20%
    assert commission_for(SCHEDULE, D("250")) == D("0.50")   # 2.50 * 20%


def test_share_of_fee_plus_flat():
    assert commission_for(SCHEDULE, D("1000")) == D("1.10")  # 5.00 * 20% + 0.10


def test_flat_only_and_share_only_bands():
    flat_only = tiered(tier("0", None, fee="3.00", pct="0", flat="0.40"))
    assert commission_for(flat_only, D("10")) == D("0.40")
    share_only = tiered(tier("0", None, fee="3.00", pct="0.5"))
    assert commission_for(share_only, D("10")) == D("1.50")


def test_commission_does_not_depend_on_where_in_the_band_the_amount_is():
    assert commission_for(SCHEDULE, D("1")) == commission_for(SCHEDULE, D("100")) == D("0.20")


def test_both_ends_of_a_band_are_inclusive():
    assert commission_for(SCHEDULE, D("1.00")) == D("0.20")      # min
    assert commission_for(SCHEDULE, D("100.00")) == D("0.20")    # max
    assert commission_for(SCHEDULE, D("100.01")) == D("0.50")    # next band's min
    assert commission_for(SCHEDULE, D("500.00")) == D("0.50")
    assert commission_for(SCHEDULE, D("500.01")) == D("1.10")


def test_min_amount_need_not_be_zero_amounts_below_it_earn_nothing():
    assert commission_for(SCHEDULE, D("0.50")) is None
    assert commission_for(SCHEDULE, D("0.99")) is None


def test_gap_between_bands_earns_nothing():
    gappy = tiered(tier("1", "100", fee="1", pct="0.2"), tier("200", None, fee="2", pct="0.2"))
    assert commission_for(gappy, D("150")) is None
    assert commission_for(gappy, D("200")) == D("0.40")


def test_amount_above_a_capped_top_band_earns_nothing():
    capped = tiered(tier("1", "100", fee="1.00", pct="0.2"))
    assert commission_for(capped, D("100")) == D("0.20")
    assert commission_for(capped, D("100.01")) is None
    # the legacy wrapper maps "no band" to zero rather than raising
    assert calculate_commission(capped, D("5000")) == D("0.00")


def test_find_tier_returns_the_band_object():
    assert find_tier(SCHEDULE.tiers, D("300")) is SCHEDULE.tiers[1]
    assert find_tier(SCHEDULE.tiers, D("0")) is None


def test_plain_rate_without_tiers_behaves_as_before():
    plain = NS(tiers=[], commission_type=BOTH, percentage=D("0.02"), flat_fee=D("1.50"))
    assert commission_for(plain, D("100")) == D("3.50")
    pct = NS(tiers=[], commission_type=PCT, percentage=D("0.015"), flat_fee=D("0"))
    assert commission_for(pct, D("20")) == D("0.30")


def test_rounding_is_half_up():
    s = tiered(tier("0", None, fee="0.50", pct="0.01"))   # 0.005
    assert commission_for(s, D("10")) == D("0.01")        # half-even would give 0.00
    s2 = tiered(tier("0", None, fee="0.25", pct="0.01"))  # 0.0025
    assert commission_for(s2, D("10")) == D("0.00")


def test_orm_models_accept_the_tier_set():
    # the real models, not stand-ins: guards the relationship wiring itself
    rate = ProviderCommissionRate(
        commission_type=PCT,
        percentage=D("0"),
        flat_fee=D("0"),
        tiers=[
            ProviderCommissionTier(min_amount=D("1"), max_amount=D("100"), provider_fee=D("2"),
                                   commission_type=BOTH, percentage=D("0.5"), flat_fee=D("0")),
            ProviderCommissionTier(min_amount=D("100.01"), max_amount=None, provider_fee=D("4"),
                                   commission_type=BOTH, percentage=D("0.5"), flat_fee=D("0.10")),
        ],
    )
    assert commission_for(rate, D("99")) == D("1.00")
    assert commission_for(rate, D("100.01")) == D("2.10")


# ---- validation of an edited schedule -------------------------------------

def body(*rows):
    return {"tiers": [
        {"min_amount": lo, "max_amount": hi, "provider_fee": "1", "percentage": "0.2"} for lo, hi in rows
    ]}


def test_valid_schedule_is_accepted_and_sorted():
    m = CommissionTiersSet(**body(("100.01", None), ("1", "100")))
    assert [t.min_amount for t in m.tiers] == [D("1"), D("100.01")]


def test_first_band_does_not_have_to_start_at_zero():
    CommissionTiersSet(**body(("10", "50")))


def test_gap_is_allowed():
    CommissionTiersSet(**body(("1", "100"), ("150", None)))


def test_single_amount_band_is_allowed():
    CommissionTiersSet(**body(("100", "100")))


def test_overlap_is_rejected():
    with pytest.raises(ValidationError, match="overlap"):
        CommissionTiersSet(**body(("1", "200"), ("100", None)))


def test_touching_bands_are_an_overlap_because_both_ends_are_inclusive():
    with pytest.raises(ValidationError, match="overlap"):
        CommissionTiersSet(**body(("1", "100"), ("100", None)))


def test_open_ended_band_must_be_the_highest():
    with pytest.raises(ValidationError, match="only the highest"):
        CommissionTiersSet(**body(("1", None), ("100", None)))


def test_max_cannot_be_below_min():
    with pytest.raises(ValidationError, match="can't be below"):
        CommissionTiersSet(**body(("50", "10")))


def test_empty_schedule_rejected():
    with pytest.raises(ValidationError, match="at least one"):
        CommissionTiersSet(tiers=[])


def test_percentage_must_be_a_fraction():
    with pytest.raises(ValidationError, match="between 0 and 1"):
        CommissionTiersSet(tiers=[{"min_amount": "0", "provider_fee": "1", "percentage": "20"}])


def test_negative_amounts_rejected():
    with pytest.raises(ValidationError):
        CommissionTiersSet(tiers=[{"min_amount": "0", "provider_fee": "-1", "percentage": "0.2"}])
    with pytest.raises(ValidationError):
        CommissionTiersSet(tiers=[{"min_amount": "0", "provider_fee": "1", "flat_fee": "-0.1"}])
