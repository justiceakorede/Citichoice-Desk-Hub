"""currencies, micro-unit money, billing currency per tenant

Moves every stored amount from minor units (assumed 2 decimals) to micro-units of the major unit, and records an
explicit ISO 4217 currency wherever money is held. Safe by construction: it aborts, changing nothing (PostgreSQL DDL
is transactional), if any wallet balance disagrees with the sum of its ledger.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ISO_SOURCE = 'iso4217 package 1.16.20260101; WiseCP-only codes flagged not_in_current_iso with assumed exponent 2'
ISO_ROWS = [
        ('AED', '784', 'UAE Dirham', 2, 'iso'),
        ('AFN', '971', 'Afghani', 2, 'iso'),
        ('ALL', '008', 'Lek', 2, 'iso'),
        ('AMD', '051', 'Armenian Dram', 2, 'iso'),
        ('ANG', None, 'ANG', 2, 'not_in_current_iso'),
        ('AOA', '973', 'Kwanza', 2, 'iso'),
        ('ARS', '032', 'Argentine Peso', 2, 'iso'),
        ('AUD', '036', 'Australian Dollar', 2, 'iso'),
        ('AWG', '533', 'Aruban Florin', 2, 'iso'),
        ('AZN', '944', 'Azerbaijan Manat', 2, 'iso'),
        ('BAM', '977', 'Convertible Mark', 2, 'iso'),
        ('BBD', '052', 'Barbados Dollar', 2, 'iso'),
        ('BDT', '050', 'Taka', 2, 'iso'),
        ('BGN', None, 'BGN', 2, 'not_in_current_iso'),
        ('BHD', '048', 'Bahraini Dinar', 3, 'iso'),
        ('BIF', '108', 'Burundi Franc', 0, 'iso'),
        ('BMD', '060', 'Bermudian Dollar', 2, 'iso'),
        ('BND', '096', 'Brunei Dollar', 2, 'iso'),
        ('BOB', '068', 'Boliviano', 2, 'iso'),
        ('BOV', '984', 'Mvdol', 2, 'iso'),
        ('BRL', '986', 'Brazilian Real', 2, 'iso'),
        ('BSD', '044', 'Bahamian Dollar', 2, 'iso'),
        ('BTN', '064', 'Ngultrum', 2, 'iso'),
        ('BWP', '072', 'Pula', 2, 'iso'),
        ('BYN', '933', 'Belarusian Ruble', 2, 'iso'),
        ('BZD', '084', 'Belize Dollar', 2, 'iso'),
        ('CAD', '124', 'Canadian Dollar', 2, 'iso'),
        ('CDF', '976', 'Congolese Franc', 2, 'iso'),
        ('CHE', '947', 'WIR Euro', 2, 'iso'),
        ('CHF', '756', 'Swiss Franc', 2, 'iso'),
        ('CHW', '948', 'WIR Franc', 2, 'iso'),
        ('CLF', '990', 'Unidad de Fomento', 4, 'iso'),
        ('CLP', '152', 'Chilean Peso', 0, 'iso'),
        ('CNY', '156', 'Yuan Renminbi', 2, 'iso'),
        ('COP', '170', 'Colombian Peso', 2, 'iso'),
        ('COU', '970', 'Unidad de Valor Real', 2, 'iso'),
        ('CRC', '188', 'Costa Rican Colon', 2, 'iso'),
        ('CUC', None, 'CUC', 2, 'not_in_current_iso'),
        ('CUP', '192', 'Cuban Peso', 2, 'iso'),
        ('CVE', '132', 'Cabo Verde Escudo', 2, 'iso'),
        ('CZK', '203', 'Czech Koruna', 2, 'iso'),
        ('DJF', '262', 'Djibouti Franc', 0, 'iso'),
        ('DKK', '208', 'Danish Krone', 2, 'iso'),
        ('DOP', '214', 'Dominican Peso', 2, 'iso'),
        ('DZD', '012', 'Algerian Dinar', 2, 'iso'),
        ('EGP', '818', 'Egyptian Pound', 2, 'iso'),
        ('ERN', '232', 'Nakfa', 2, 'iso'),
        ('ETB', '230', 'Ethiopian Birr', 2, 'iso'),
        ('EUR', '978', 'Euro', 2, 'iso'),
        ('FJD', '242', 'Fiji Dollar', 2, 'iso'),
        ('FKP', '238', 'Falkland Islands Pound', 2, 'iso'),
        ('GBP', '826', 'Pound Sterling', 2, 'iso'),
        ('GEL', '981', 'Lari', 2, 'iso'),
        ('GGP', None, 'GGP', 2, 'not_in_current_iso'),
        ('GHS', '936', 'Ghana Cedi', 2, 'iso'),
        ('GIP', '292', 'Gibraltar Pound', 2, 'iso'),
        ('GMD', '270', 'Dalasi', 2, 'iso'),
        ('GNF', '324', 'Guinean Franc', 0, 'iso'),
        ('GTQ', '320', 'Quetzal', 2, 'iso'),
        ('GYD', '328', 'Guyana Dollar', 2, 'iso'),
        ('HKD', '344', 'Hong Kong Dollar', 2, 'iso'),
        ('HNL', '340', 'Lempira', 2, 'iso'),
        ('HRK', None, 'HRK', 2, 'not_in_current_iso'),
        ('HTG', '332', 'Gourde', 2, 'iso'),
        ('HUF', '348', 'Forint', 2, 'iso'),
        ('IDR', '360', 'Rupiah', 2, 'iso'),
        ('ILS', '376', 'New Israeli Sheqel', 2, 'iso'),
        ('IMP', None, 'IMP', 2, 'not_in_current_iso'),
        ('INR', '356', 'Indian Rupee', 2, 'iso'),
        ('IQD', '368', 'Iraqi Dinar', 3, 'iso'),
        ('IRR', '364', 'Iranian Rial', 2, 'iso'),
        ('ISK', '352', 'Iceland Krona', 0, 'iso'),
        ('JEP', None, 'JEP', 2, 'not_in_current_iso'),
        ('JMD', '388', 'Jamaican Dollar', 2, 'iso'),
        ('JOD', '400', 'Jordanian Dinar', 3, 'iso'),
        ('JPY', '392', 'Yen', 0, 'iso'),
        ('KES', '404', 'Kenyan Shilling', 2, 'iso'),
        ('KGS', '417', 'Som', 2, 'iso'),
        ('KHR', '116', 'Riel', 2, 'iso'),
        ('KMF', '174', 'Comorian Franc', 0, 'iso'),
        ('KPW', '408', 'North Korean Won', 2, 'iso'),
        ('KRW', '410', 'Won', 0, 'iso'),
        ('KWD', '414', 'Kuwaiti Dinar', 3, 'iso'),
        ('KYD', '136', 'Cayman Islands Dollar', 2, 'iso'),
        ('KZT', '398', 'Tenge', 2, 'iso'),
        ('LAK', '418', 'Lao Kip', 2, 'iso'),
        ('LBP', '422', 'Lebanese Pound', 2, 'iso'),
        ('LKR', '144', 'Sri Lanka Rupee', 2, 'iso'),
        ('LRD', '430', 'Liberian Dollar', 2, 'iso'),
        ('LSL', '426', 'Loti', 2, 'iso'),
        ('LTL', None, 'LTL', 2, 'not_in_current_iso'),
        ('LYD', '434', 'Libyan Dinar', 3, 'iso'),
        ('MAD', '504', 'Moroccan Dirham', 2, 'iso'),
        ('MDL', '498', 'Moldovan Leu', 2, 'iso'),
        ('MGA', '969', 'Malagasy Ariary', 2, 'iso'),
        ('MKD', '807', 'Denar', 2, 'iso'),
        ('MMK', '104', 'Kyat', 2, 'iso'),
        ('MNT', '496', 'Tugrik', 2, 'iso'),
        ('MOP', '446', 'Pataca', 2, 'iso'),
        ('MRU', '929', 'Ouguiya', 2, 'iso'),
        ('MUR', '480', 'Mauritius Rupee', 2, 'iso'),
        ('MVR', '462', 'Rufiyaa', 2, 'iso'),
        ('MWK', '454', 'Malawi Kwacha', 2, 'iso'),
        ('MXN', '484', 'Mexican Peso', 2, 'iso'),
        ('MXV', '979', 'Mexican Unidad de Inversion (UDI)', 2, 'iso'),
        ('MYR', '458', 'Malaysian Ringgit', 2, 'iso'),
        ('MZN', '943', 'Mozambique Metical', 2, 'iso'),
        ('NAD', '516', 'Namibia Dollar', 2, 'iso'),
        ('NGN', '566', 'Naira', 2, 'iso'),
        ('NIO', '558', 'Cordoba Oro', 2, 'iso'),
        ('NOK', '578', 'Norwegian Krone', 2, 'iso'),
        ('NPR', '524', 'Nepalese Rupee', 2, 'iso'),
        ('NZD', '554', 'New Zealand Dollar', 2, 'iso'),
        ('OMR', '512', 'Rial Omani', 3, 'iso'),
        ('PAB', '590', 'Balboa', 2, 'iso'),
        ('PEN', '604', 'Sol', 2, 'iso'),
        ('PGK', '598', 'Kina', 2, 'iso'),
        ('PHP', '608', 'Philippine Peso', 2, 'iso'),
        ('PKR', '586', 'Pakistan Rupee', 2, 'iso'),
        ('PLN', '985', 'Zloty', 2, 'iso'),
        ('PYG', '600', 'Guarani', 0, 'iso'),
        ('QAR', '634', 'Qatari Rial', 2, 'iso'),
        ('RON', '946', 'Romanian Leu', 2, 'iso'),
        ('RSD', '941', 'Serbian Dinar', 2, 'iso'),
        ('RUB', '643', 'Russian Ruble', 2, 'iso'),
        ('RWF', '646', 'Rwanda Franc', 0, 'iso'),
        ('SAR', '682', 'Saudi Riyal', 2, 'iso'),
        ('SBD', '090', 'Solomon Islands Dollar', 2, 'iso'),
        ('SCR', '690', 'Seychelles Rupee', 2, 'iso'),
        ('SDG', '938', 'Sudanese Pound', 2, 'iso'),
        ('SEK', '752', 'Swedish Krona', 2, 'iso'),
        ('SGD', '702', 'Singapore Dollar', 2, 'iso'),
        ('SHP', '654', 'Saint Helena Pound', 2, 'iso'),
        ('SLE', '925', 'Leone', 2, 'iso'),
        ('SLL', None, 'SLL', 2, 'not_in_current_iso'),
        ('SOS', '706', 'Somali Shilling', 2, 'iso'),
        ('SRD', '968', 'Surinam Dollar', 2, 'iso'),
        ('SSP', '728', 'South Sudanese Pound', 2, 'iso'),
        ('STN', '930', 'Dobra', 2, 'iso'),
        ('SVC', '222', 'El Salvador Colon', 2, 'iso'),
        ('SYP', '760', 'Syrian Pound', 2, 'iso'),
        ('SZL', '748', 'Lilangeni', 2, 'iso'),
        ('THB', '764', 'Baht', 2, 'iso'),
        ('TJS', '972', 'Somoni', 2, 'iso'),
        ('TMT', '934', 'Turkmenistan New Manat', 2, 'iso'),
        ('TND', '788', 'Tunisian Dinar', 3, 'iso'),
        ('TOP', '776', 'Pa’anga', 2, 'iso'),
        ('TRY', '949', 'Turkish Lira', 2, 'iso'),
        ('TTD', '780', 'Trinidad and Tobago Dollar', 2, 'iso'),
        ('TWD', '901', 'New Taiwan Dollar', 2, 'iso'),
        ('TZS', '834', 'Tanzanian Shilling', 2, 'iso'),
        ('UAH', '980', 'Hryvnia', 2, 'iso'),
        ('UGX', '800', 'Uganda Shilling', 0, 'iso'),
        ('USD', '840', 'US Dollar', 2, 'iso'),
        ('USN', '997', 'US Dollar (Next day)', 2, 'iso'),
        ('UYI', '940', 'Uruguay Peso en Unidades Indexadas (UI)', 0, 'iso'),
        ('UYU', '858', 'Peso Uruguayo', 2, 'iso'),
        ('UYW', '927', 'Unidad Previsional', 4, 'iso'),
        ('UZS', '860', 'Uzbekistan Sum', 2, 'iso'),
        ('VED', '926', 'Bolívar Soberano', 2, 'iso'),
        ('VEF', None, 'VEF', 2, 'not_in_current_iso'),
        ('VES', '928', 'Bolívar Soberano', 2, 'iso'),
        ('VND', '704', 'Dong', 0, 'iso'),
        ('VUV', '548', 'Vatu', 0, 'iso'),
        ('WST', '882', 'Tala', 2, 'iso'),
        ('XAD', '396', 'Arab Accounting Dinar', 2, 'iso'),
        ('XAF', '950', 'CFA Franc BEAC', 0, 'iso'),
        ('XAG', '961', 'Silver', None, 'iso'),
        ('XAU', '959', 'Gold', None, 'iso'),
        ('XBA', '955', 'Bond Markets Unit European Composite Unit (EURCO)', None, 'iso'),
        ('XBB', '956', 'Bond Markets Unit European Monetary Unit (E.M.U.-6)', None, 'iso'),
        ('XBC', '957', 'Bond Markets Unit European Unit of Account 9 (E.U.A.-9)', None, 'iso'),
        ('XBD', '958', 'Bond Markets Unit European Unit of Account 17 (E.U.A.-17)', None, 'iso'),
        ('XCD', '951', 'East Caribbean Dollar', 2, 'iso'),
        ('XCG', '532', 'Caribbean Guilder', 2, 'iso'),
        ('XDR', '960', 'SDR (Special Drawing Right)', None, 'iso'),
        ('XOF', '952', 'CFA Franc BCEAO', 0, 'iso'),
        ('XPD', '964', 'Palladium', None, 'iso'),
        ('XPF', '953', 'CFP Franc', 0, 'iso'),
        ('XPT', '962', 'Platinum', None, 'iso'),
        ('XSU', '994', 'Sucre', None, 'iso'),
        ('XTS', '963', 'Codes specifically reserved for testing purposes', None, 'iso'),
        ('XUA', '965', 'ADB Unit of Account', None, 'iso'),
        ('XXX', '999', 'The codes assigned for transactions where no currency is involved', None, 'iso'),
        ('YER', '886', 'Yemeni Rial', 2, 'iso'),
        ('YUN', None, 'YUN', 2, 'not_in_current_iso'),
        ('ZAR', '710', 'Rand', 2, 'iso'),
        ('ZMW', '967', 'Zambian Kwacha', 2, 'iso'),
        ('ZWG', '924', 'Zimbabwe Gold', 2, 'iso'),
        ('ZWL', None, 'ZWL', 2, 'not_in_current_iso'),
]


def _scale(col, cur_col="c.exponent"):
    return f"{col} * power(10::numeric, 6 - {cur_col})::bigint"


def upgrade() -> None:
    bind = op.get_bind()
    cur = op.create_table(
        "currencies",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("numeric", sa.String(3)), sa.Column("name", sa.String(100), nullable=False),
        sa.Column("exponent", sa.Integer()), sa.Column("iso_status", sa.String(24), nullable=False, server_default="iso"),
        sa.Column("billing_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("exponent is null or (exponent >= 0 and exponent <= 4)", name="ck_currency_exponent"),
        sa.CheckConstraint("iso_status in ('iso', 'not_in_current_iso')", name="ck_currency_status"))
    op.bulk_insert(cur, [dict(code=c, numeric=n, name=nm, exponent=e, iso_status=s, billing_enabled=False)
                         for c, n, nm, e, s in ISO_ROWS])

    bad = bind.execute(sa.text("""
        select distinct x.currency from (select currency from wallets union select currency from plans) x
        left join currencies c on c.code = x.currency where c.code is null or c.exponent is null""")).fetchall()
    if bad:
        raise RuntimeError(f"wallets/plans use currencies with no ISO exponent: {[r[0] for r in bad]}")
    op.execute("update currencies set billing_enabled = true where code in (select currency from wallets)")

    # ---- wallets
    op.add_column("wallets", sa.Column("balance_micro", sa.BigInteger()))
    op.execute(f"update wallets w set balance_micro = {_scale('w.balance_minor')} from currencies c where c.code = w.currency")
    op.alter_column("wallets", "balance_micro", nullable=False)
    op.create_unique_constraint("uq_wallet_tenant_currency", "wallets", ["tenant_id", "currency"])
    op.create_foreign_key("fk_wallets_currency", "wallets", "currencies", ["currency"], ["code"])

    # ---- ledger
    op.add_column("ledger_entries", sa.Column("currency", sa.String(3)))
    op.add_column("ledger_entries", sa.Column("amount_micro", sa.BigInteger()))
    op.add_column("ledger_entries", sa.Column("balance_after_micro", sa.BigInteger()))
    op.execute(f"""update ledger_entries l set currency = w.currency, amount_micro = {_scale('l.amount_minor')},
                   balance_after_micro = {_scale('l.balance_after')}
                   from wallets w join currencies c on c.code = w.currency where w.tenant_id = l.tenant_id""")
    drift = bind.execute(sa.text("""
        select w.tenant_id, w.balance_micro, coalesce(l.s, 0) from wallets w
        left join (select tenant_id, sum(amount_micro) s from ledger_entries group by tenant_id) l on l.tenant_id = w.tenant_id
        where coalesce(l.s, 0) <> w.balance_micro""")).fetchall()
    if drift:
        raise RuntimeError(f"ABORTED: wallet balance differs from ledger sum for tenants {[r[0] for r in drift]}; fix first")
    unset = bind.execute(sa.text("select count(*) from ledger_entries where currency is null or amount_micro is null")).scalar()
    if unset:
        raise RuntimeError(f"ABORTED: {unset} ledger entries could not be converted")
    for col in ("currency", "amount_micro", "balance_after_micro"):
        op.alter_column("ledger_entries", col, nullable=False)
    op.create_foreign_key("fk_ledger_wallet_currency", "ledger_entries", "wallets", ["tenant_id", "currency"], ["tenant_id", "currency"])

    # ---- usage records (the first amount still includes the FX buffer; corrected in a later step, see models)
    op.add_column("usage_records", sa.Column("customer_currency", sa.String(3)))
    op.add_column("usage_records", sa.Column("provider_currency", sa.String(3), nullable=False, server_default="USD"))
    op.alter_column("usage_records", "provider_currency", server_default=None)
    for col in ("meta_cost_incl_buffer_micro", "service_fee_micro", "charge_micro"):
        op.add_column("usage_records", sa.Column(col, sa.BigInteger()))
    op.execute(f"""update usage_records u set customer_currency = w.currency,
                   meta_cost_incl_buffer_micro = {_scale('u.meta_cost_minor')}, service_fee_micro = {_scale('u.service_fee_minor')},
                   charge_micro = {_scale('u.charge_minor')} from wallets w join currencies c on c.code = w.currency where w.tenant_id = u.tenant_id""")
    for col in ("customer_currency", "meta_cost_incl_buffer_micro", "service_fee_micro", "charge_micro"):
        op.alter_column("usage_records", col, nullable=False)
    op.create_foreign_key("fk_usage_customer_currency", "usage_records", "currencies", ["customer_currency"], ["code"])
    op.create_foreign_key("fk_usage_provider_currency", "usage_records", "currencies", ["provider_currency"], ["code"])

    # ---- plans (reporting prices only)
    op.add_column("plans", sa.Column("price_micro", sa.BigInteger()))
    op.execute(f"update plans p set price_micro = {_scale('p.price_minor')} from currencies c where c.code = p.currency and p.price_minor is not null")
    op.create_foreign_key("fk_plans_currency", "plans", "currencies", ["currency"], ["code"])

    # ---- tenants: billing currency follows the wallet; never silently reinterpreted
    op.add_column("tenants", sa.Column("billing_currency", sa.String(3)))
    op.execute("update tenants t set billing_currency = w.currency from wallets w where w.tenant_id = t.id")
    missing = bind.execute(sa.text("select count(*) from tenants where billing_currency is null")).scalar()
    if missing:
        raise RuntimeError(f"ABORTED: {missing} tenant(s) have no wallet")
    op.alter_column("tenants", "billing_currency", nullable=False)
    op.create_foreign_key("fk_tenants_billing_currency", "tenants", "currencies", ["billing_currency"], ["code"])

    for t, cols in (("wallets", ["balance_minor"]), ("ledger_entries", ["amount_minor", "balance_after"]),
                    ("usage_records", ["meta_cost_minor", "service_fee_minor", "charge_minor"]), ("plans", ["price_minor"])):
        for col in cols:
            op.drop_column(t, col)


def _unscale(col, cur_col="c.exponent"):
    return f"{col} / power(10::numeric, 6 - {cur_col})::bigint"


def downgrade() -> None:
    """Back to minor units. Refuses if any amount has more precision than its currency's minor unit,
    because that would silently lose money."""
    bind = op.get_bind()
    for table, cols, joinsql in (
            ("wallets", ["balance_micro"], "from currencies c where c.code = x.currency"),
            ("ledger_entries", ["amount_micro", "balance_after_micro"], "from currencies c where c.code = x.currency"),
            ("usage_records", ["meta_cost_incl_buffer_micro", "service_fee_micro", "charge_micro"], "from currencies c where c.code = x.customer_currency"),
            ("plans", ["price_micro"], "from currencies c where c.code = x.currency")):
        for col in cols:
            n = bind.execute(sa.text(f"select count(*) from {table} x join currencies c on c.code = {'x.customer_currency' if table == 'usage_records' else 'x.currency'} "
                                     f"where x.{col} is not null and mod(x.{col}, power(10::numeric, 6 - c.exponent)::bigint) <> 0")).scalar()
            if n:
                raise RuntimeError(f"cannot downgrade: {n} value(s) in {table}.{col} are finer than the currency's minor unit")
    op.add_column("wallets", sa.Column("balance_minor", sa.BigInteger()))
    op.execute(f"update wallets x set balance_minor = {_unscale('x.balance_micro')} from currencies c where c.code = x.currency")
    op.alter_column("wallets", "balance_minor", nullable=False)
    op.add_column("ledger_entries", sa.Column("amount_minor", sa.BigInteger()))
    op.add_column("ledger_entries", sa.Column("balance_after", sa.BigInteger()))
    op.execute(f"update ledger_entries x set amount_minor = {_unscale('x.amount_micro')}, balance_after = {_unscale('x.balance_after_micro')} from currencies c where c.code = x.currency")
    op.alter_column("ledger_entries", "amount_minor", nullable=False)
    op.alter_column("ledger_entries", "balance_after", nullable=False)
    for col in ("meta_cost_minor", "service_fee_minor", "charge_minor"):
        op.add_column("usage_records", sa.Column(col, sa.BigInteger()))
    op.execute(f"""update usage_records x set meta_cost_minor = {_unscale('x.meta_cost_incl_buffer_micro')},
                   service_fee_minor = {_unscale('x.service_fee_micro')}, charge_minor = {_unscale('x.charge_micro')}
                   from currencies c where c.code = x.customer_currency""")
    for col in ("meta_cost_minor", "service_fee_minor", "charge_minor"):
        op.alter_column("usage_records", col, nullable=False)
    op.add_column("plans", sa.Column("price_minor", sa.BigInteger()))
    op.execute(f"update plans x set price_minor = {_unscale('x.price_micro')} from currencies c where c.code = x.currency and x.price_micro is not null")

    op.drop_constraint("fk_tenants_billing_currency", "tenants", type_="foreignkey")
    op.drop_column("tenants", "billing_currency")
    op.drop_constraint("fk_plans_currency", "plans", type_="foreignkey")
    op.drop_column("plans", "price_micro")
    op.drop_constraint("fk_usage_provider_currency", "usage_records", type_="foreignkey")
    op.drop_constraint("fk_usage_customer_currency", "usage_records", type_="foreignkey")
    for col in ("charge_micro", "service_fee_micro", "meta_cost_incl_buffer_micro", "provider_currency", "customer_currency"):
        op.drop_column("usage_records", col)
    op.drop_constraint("fk_ledger_wallet_currency", "ledger_entries", type_="foreignkey")
    for col in ("balance_after_micro", "amount_micro", "currency"):
        op.drop_column("ledger_entries", col)
    op.drop_constraint("fk_wallets_currency", "wallets", type_="foreignkey")
    op.drop_constraint("uq_wallet_tenant_currency", "wallets", type_="unique")
    op.drop_column("wallets", "balance_micro")
    op.drop_table("currencies")
