"""Money handling. All persisted amounts are integers in MICRO-UNITS: 1 unit = 10^-6 of one MAJOR unit of the
row's own currency (so 1 NGN = 1_000_000 and 1 JPY = 1_000_000, whatever the currency's ISO exponent).

Why not ISO minor units: a USD message can cost $0.0067, below one cent. Rounding every message to a cent distorts
totals by tens of percent. Micro-units keep WiseCP's four-decimal amounts and Meta's rates exact, stay integers (so
balances, constraints and atomic updates stay simple), and round only at the edges: where an external system needs
minor units, and when a statement is displayed.

Floats never touch money. Anything that arrives as a float is rejected."""
import json
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from functools import lru_cache
from pathlib import Path

MICRO = 10 ** 6
_BIG = 60            # decimal digits of working precision: micro (19 digits) x rate (up to 24) never loses a digit


class MoneyError(ValueError):
    pass


@lru_cache
def iso_table() -> dict[str, dict]:
    doc = json.loads((Path(__file__).parent / "data" / "iso4217.json").read_text())
    return {r["code"]: r for r in doc["currencies"]}


def iso_source() -> str:
    return json.loads((Path(__file__).parent / "data" / "iso4217.json").read_text())["source"]


def exponent_of(code: str) -> int:
    row = iso_table().get(code.upper())
    if not row or row["exponent"] is None:
        raise MoneyError(f"{code} has no ISO minor-unit exponent and cannot hold money")
    return row["exponent"]


def _dec(value) -> Decimal:
    if isinstance(value, float):
        raise TypeError("floats are not allowed for money; pass a Decimal or a string")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def major_to_micro(amount) -> int:
    """Exact. Refuses anything that does not fit in micro-units without rounding."""
    d = _dec(amount)
    with localcontext() as ctx:
        ctx.prec = _BIG
        m = d * MICRO
        if m != m.to_integral_value():
            raise MoneyError("more than 6 decimal places")
        return int(m)


def input_to_micro(amount, code: str) -> int:
    """For amounts typed or sent by a person or another system: no finer than the currency's own minor unit."""
    d = _dec(amount)
    exp = exponent_of(code)
    if d != d.quantize(Decimal(1).scaleb(-exp)):
        raise MoneyError(f"{code} has {exp} decimal place(s)")
    return major_to_micro(d)


def dmul(*factors) -> Decimal:
    """Multiply Decimals without losing digits to the default 28-digit context."""
    with localcontext() as ctx:
        ctx.prec = _BIG
        out = Decimal(1)
        for f in factors:
            out *= _dec(f)
        return out


def micro_to_decimal(micro: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _BIG
        return Decimal(micro) / MICRO


def micro_to_minor(micro: int, code: str, rounding=ROUND_HALF_EVEN) -> int:
    exp = exponent_of(code)
    with localcontext() as ctx:
        ctx.prec = _BIG
        return int((Decimal(micro) / Decimal(10) ** (6 - exp)).to_integral_value(rounding=rounding))


def minor_to_micro(minor: int, code: str) -> int:
    return int(minor) * 10 ** (6 - exponent_of(code))


def scale(micro: int, factor, rounding=ROUND_HALF_EVEN) -> int:
    """Multiply an amount by a rate or markup, rounding once, to the nearest micro-unit."""
    f = _dec(factor)
    with localcontext() as ctx:
        ctx.prec = _BIG
        return int((Decimal(micro) * f).to_integral_value(rounding=rounding))


def format_micro(micro: int, code: str) -> str:
    """Display only: rounds half-up to the currency's own decimals, e.g. 'NGN 1,234.50' or 'JPY 1,235'."""
    exp = exponent_of(code)
    with localcontext() as ctx:
        ctx.prec = _BIG
        d = micro_to_decimal(abs(micro)).quantize(Decimal(1).scaleb(-exp), rounding=ROUND_HALF_UP)
    return f"{'-' if micro < 0 else ''}{code} {d:,.{exp}f}"
