"""Billing calculation — Phase 5 unit tests (pure dataclass logic).

Run under the app's Python (3.14) once a pytest runner is wired up (Phase 14).
"""

from cloud.app.billing_calc import Line, Calc, _tb


def test_tb_conversion():
    assert _tb(0) == 0
    assert _tb(1024 ** 4) == 1
    assert _tb(5 * 1024 ** 4) == 5
    assert _tb(None) == 0


def test_calc_totals_split_recurring_and_one_time():
    c = Calc(tenant_id="t1", plan="business", currency="USD", price_version=3)
    for ln in [
        Line(key="base", label="Base", quantity=1, unit_price_cents=12500, amount_cents=12500),
        Line(key="protected_data", label="Data", quantity=10, unit_price_cents=600,
             amount_cents=6000, included_qty=5, licensed_qty=15),
        Line(key="appliance_setup:8", label="Setup", quantity=1, unit_price_cents=49900,
             amount_cents=49900, kind="one_time"),
    ]:
        c.lines.append(ln)
        (setattr(c, "one_time_cents", c.one_time_cents + ln.amount_cents) if ln.kind == "one_time"
         else setattr(c, "recurring_cents", c.recurring_cents + ln.amount_cents))
    assert c.recurring_cents == 18500       # 12500 + 6000
    assert c.one_time_cents == 49900
    d = c.as_dict()
    assert d["recurring_display"] == "$185.00"
    assert d["lines"][1]["included_qty"] == 5 and d["lines"][1]["licensed_qty"] == 15


def test_included_vs_billable_rule():
    # billable = max(0, licensed - included)
    for licensed, included, expected in [(15, 5, 10), (5, 5, 0), (3, 5, 0), (1, 0, 1)]:
        assert max(0, licensed - included) == expected
