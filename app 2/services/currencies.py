from sqlalchemy.orm import Session

from .. import money
from ..models import Currency


def seed_currencies(db: Session, enabled: tuple[str, ...] = ()) -> int:
    """Loads the vendored ISO 4217 table. Billing stays OFF for every currency unless named in `enabled`."""
    n = 0
    for code, r in money.iso_table().items():
        c = db.get(Currency, code)
        if not c:
            db.add(Currency(code=code, numeric=r["numeric"], name=r["name"], exponent=r["exponent"],
                            iso_status=r["iso_status"], billing_enabled=code in enabled))
            n += 1
    db.flush()
    return n
