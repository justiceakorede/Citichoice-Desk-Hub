import ast
import random
from decimal import Decimal
from pathlib import Path

import pytest

from app import money

# every currency active in the WiseCP installation, as pasted by the operator
WISECP_ACTIVE = "USD EUR AUD XOF NOK BRL GBP XAF CAD CLP CNY COP NZD DOP EGP GHS HKD INR JPY KES CHF MXN NGN PLN SGD ZAR SEK".split()


def test_every_active_wisecp_currency_has_an_iso_exponent():
    for code in WISECP_ACTIVE:
        assert money.iso_table()[code]["iso_status"] == "iso", code
        assert money.exponent_of(code) in (0, 2, 3), code


@pytest.mark.parametrize("code,exp", [("NGN", 2), ("USD", 2), ("JPY", 0), ("CLP", 0), ("XOF", 0), ("XAF", 0),
                                      ("KWD", 3), ("BHD", 3), ("IQD", 3), ("ALL", 2), ("LAK", 2), ("CLF", 4)])
def test_exponents_follow_iso_4217_not_cldr(code, exp):
    assert money.exponent_of(code) == exp


def test_metals_and_funds_cannot_hold_money():
    for code in ("XAU", "XDR", "XXX"):
        with pytest.raises(money.MoneyError):
            money.exponent_of(code)


def test_wisecp_only_codes_are_flagged_not_trusted():
    for code in ("GGP", "IMP", "JEP", "HRK", "ZWL"):
        assert money.iso_table()[code]["iso_status"] == "not_in_current_iso"


def test_major_to_micro_is_exact_and_refuses_floats_and_excess_precision():
    assert money.major_to_micro("0.0067") == 6_700
    assert money.major_to_micro(Decimal("128265.1802")) == 128_265_180_200      # a WiseCP four-decimal total, exactly
    assert money.major_to_micro(-5) == -5_000_000
    with pytest.raises(TypeError):
        money.major_to_micro(0.1)
    with pytest.raises(money.MoneyError):
        money.major_to_micro("0.0000001")


@pytest.mark.parametrize("amount,code,ok", [
    ("100.12", "NGN", True), ("100.123", "NGN", False), ("1000", "JPY", True), ("1000.5", "JPY", False),
    ("1.234", "KWD", True), ("1.2345", "KWD", False), ("0.01", "USD", True), ("0.001", "USD", False)])
def test_external_amounts_must_fit_the_currencys_own_minor_unit(amount, code, ok):
    if ok:
        assert money.input_to_micro(amount, code) == money.major_to_micro(amount)
    else:
        with pytest.raises(money.MoneyError):
            money.input_to_micro(amount, code)


def test_minor_unit_conversions_for_zero_two_and_three_decimal_currencies():
    assert money.micro_to_minor(1_234_000_000, "JPY") == 1234                  # no minor unit: minor == major
    assert money.micro_to_minor(123_456_789, "NGN") == 12346                   # 123.456789 -> 12345.6789 kobo -> 12346
    assert money.micro_to_minor(1_234_567, "KWD") == 1235                      # 1.234567 -> 1234.567 fils -> 1235
    assert money.minor_to_micro(12346, "NGN") == 123_460_000
    assert money.minor_to_micro(1235, "KWD") == 1_235_000
    assert money.micro_to_minor(2_500, "NGN") == 0                             # half-even at the .25 boundary below half
    assert money.micro_to_minor(5_000, "NGN") == 0 and money.micro_to_minor(15_000, "NGN") == 2   # banker's rounding


def test_display_rounds_once_and_shows_the_currencys_own_decimals():
    assert money.format_micro(1_234_500_000, "NGN") == "NGN 1,234.50"
    assert money.format_micro(1_234_500_000, "JPY") == "JPY 1,235"
    assert money.format_micro(-5_000, "KWD") == "-KWD 0.005"
    assert money.format_micro(6_700, "USD") == "USD 0.01"


def test_roundtrip_property_over_random_amounts():
    rnd = random.Random(7)
    for _ in range(5000):
        micro = rnd.randint(-10 ** 15, 10 ** 15)
        assert money.major_to_micro(money.micro_to_decimal(micro)) == micro


def test_scaling_never_loses_digits_even_at_extreme_magnitude():
    huge = 9_000_000_000_000_000_000                       # near BIGINT max
    assert money.scale(huge, Decimal("1.0000000001")) == huge + 900_000_000
    assert money.scale(1, Decimal("1325.7182")) == 1326


def test_sub_cent_messages_are_not_distorted_by_per_message_rounding():
    """The reason micro-units exist. 100,000 messages at $0.006901 each."""
    per = money.major_to_micro("0.006901")
    total = per * 100_000
    assert money.format_micro(total, "USD") == "USD 690.10"
    per_cent_rounded = money.micro_to_minor(per, "USD", rounding=__import__("decimal").ROUND_CEILING) * 100_000
    assert per_cent_rounded == 100_000          # cent-rounding would bill $1,000.00: 45% too much


def test_money_code_never_uses_floats():
    for f in ("app/money.py", "app/services/pricing.py", "app/services/wallet.py", "app/routers/internal.py", "app/services/tenants.py", "app/routers/admin.py"):
        tree = ast.parse(Path(f).read_text())
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "float"), f
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), (f, node.value)
