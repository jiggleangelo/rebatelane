from .models import Account, EarnRule, UsageAggregate, Vendor

def build(vault):
    vendor = Vendor("vendor_sample", "Example Vector API (sample)", "billing-approver",
                    "exception-owner", vault.put("owner@example.invalid"))
    accounts = {}
    aggregates = []
    profiles = [
        (130000, 500000, .12, 900, .02, 110000, 180),
        (180000, 800000, .18, 1200, .03, 150000, 210),
        (260000, 1500000, .09, 2200, .01, 230000, 365),
        (400000, 3000000, .20, 3000, .04, 350000, 400),
        (80000, 400000, .10, 700, .02, 70000, 200),
        (160000, 900000, .82, 1000, .02, 140000, 200),
        (250000, 1200000, .20, 7000, .05, 40000, 200),
        (140000, 600000, .12, 800, .02, 100000, 12),
    ]
    for index, values in enumerate(profiles, 1):
        ident = f"acct_{index:02d}"
        accounts[ident] = Account(ident, f"cus_{index:02d}", f"agent-fleet-{index:02d}")
        aggregates.append(UsageAggregate(ident, "2026-09", *values))
    return vendor, accounts, aggregates, EarnRule(100000, 200, 20000, 90, 30)
