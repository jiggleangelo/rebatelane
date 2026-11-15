# rebatelane

Prototype: Prepare expiring rebate credits for heavy API and agent users - rebatelane

**Use it free, no account:** https://app-rebatelane.manykiln.com/  
**Try the demo (made-up data):** https://try-rebatelane.manykiln.com/  
**Product page:** https://rebatelane.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 97 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# Rebate Rail MVP

The original offline demo and the persistent rebatelane app prepare promotional credits. Neither charges anyone. The usable app never calls Stripe, sends messages, or makes AI or other outbound requests.

## Original demo

Run `python3 demo.py`. Its made-up vendor, FakeStripe, fault injection, roles, vault and inbox remain unchanged. It loses state on restart.

## Usable app

Run with Python 3.12 on a local Linux filesystem:

```sh
DATA_DIR=/path/to/persistent-data \
PUBLIC_BASE_URL=http://127.0.0.1:8080 \
APP_HOST=127.0.0.1 PORT=8080 python3 app.py
```

Hosted installations set an HTTPS `PUBLIC_BASE_URL` and `APP_HTTPS_TERMINATED=true`. `GET /healthz` returns `ok`.

`APP_DATA_CAP_MB` defaults to 200 and limits file bytes across the entire private DATA_DIR, including creation budgets and temporary replacement snapshots. Each workspace also has a 200 MB maximum. Writes reserve room for both the old and new snapshots and fail without replacing saved state when capacity is insufficient. This deliberately leaves space unavailable for ordinary data so atomic replacement remains bounded. Existing data already above a newly lowered cap remains readable and deletable; further writes are refused. Filesystem metadata and allocation overhead require additional volume headroom. Do not share DATA_DIR with other applications. Process and filesystem locks serialize writes, deletion and hourly retention sweeping.

A workspace URL is its only write key and cannot be recovered. Only token hashes are persisted. A separately rotated link grants read-only access. Full JSON export includes the hash-chained audit log. Delete removes the workspace immediately. Workspaces are deleted after 90 days without use; reading a workspace refreshes its retention time. The retention notice appears on every HTML page.

HTTP workspace creation has a persistent global budget of 60 per hour and a per-client budget of 10 per hour. Direct connections use the socket peer address. If a reverse proxy is used, set `APP_TRUSTED_PROXY_IPS` to its exact comma-separated IP addresses and configure that proxy to overwrite `X-Real-IP` with a single verified client IP. Forwarded headers from other peers are ignored. Requests have a 15-second absolute deadline, including headers and body, with 32 total workers and at most eight workers per socket peer. A shared reverse proxy therefore has eight upstream slots; configure its own connection limits and request buffering appropriately.

Usage CSV requires `account_id`, `period`, `calls`, and either `spend` or `spend_cents`. Optional columns are `billing_customer_id`, `top_fingerprint_share`, `peak_calls_per_min`, `error_rate`, `prior_period_calls`, and `account_age_days`. Missing account age holds an otherwise eligible row for review whenever the minimum age is positive. Other missing signals are visibly reported as not evaluated. Each input is limited to 1 MB and 5,000 rows, with one period per run and 24 runs per workspace.

Grants input accepts a grant array, CSV, a Stripe list response, multiple list-response JSON documents separated by whitespace, or an array of list-response pages. Multi-page imports share the same 1 MB and 5,000-grant limits. Include every page in order, ending with `has_more:false`; a page with `has_more:true` alone cannot establish absence. Only ID, customer, monetary amount, currency, expiry and reference survive parsing.

Enter the export's actual capture time with a timezone, or Unix seconds, and confirm all relevant customers and pages are included without filters or omissions. For multiple pages, use the time the first page was captured. The app validates the timestamp but cannot independently verify the owner's statement or the export's provenance. Upload arrival time never substitutes for capture time. Missing timestamps, missing completeness confirmation, pagination and dropped rows freeze absent references. Correcting an incomplete export automatically clears only freezes caused solely by that incomplete export. Unknown submissions and billing conflicts retain their own resolution requirements.

After a submission with unknown outcome, absence authorizes a same-reference retry verdict only when a complete export was captured at or after its submission mark. Explicit Unfreeze is still required before downloading it. An account frozen in any run is blocked from downloads in every run in that workspace. Previously downloaded files cannot be revoked; obtain a fresh download before applying credits.

Downloads contain Stripe form parameter names, including `amount[type]`, `amount[monetary][value]`, `amount[monetary][currency]`, `applicability_config[scope][price_type]`, `category`, `customer`, `expires_at`, and `metadata[rr_ref]`. The `Idempotency-Key` field must be sent as an HTTP header; send the other fields as form parameters. The JSON envelope's `notice` and `data` fields are not Stripe parameters. CSV starts with an explanatory comment. Users apply downloads through their own tooling; this app sends nothing. Mark attempts immediately and use fresh complete billing evidence before retrying. A stable idempotency key is not proof that Stripe still retains its deduplication record.

The repository also contains `rebate_rail/connectors/stripe_http.py`, an HTTP connector for Stripe's credit-grant API. It is not imported by `app.py` or `demo.py`, and it has never been run against Stripe: its tests use a fake transport only. It refuses to construct unless `REBATE_RAIL_LIVE=1` is set, a restricted key is supplied, and the jurisdiction is in `CLEARED_JURISDICTIONS`, which is an empty set in this code, so it stays disabled unless someone edits the code. The app and the original demo use no real payment connector. Treat the connector as unreviewed example code, not as something ready to charge or credit anyone.

No AI model is used. Rules, references, reconciliation and audit verification are deterministic. Sample data and rule defaults are labelled made-up examples. Never enter personal information, payment details or API keys.

Run the full suite with:

```sh
python3 -m unittest discover -s tests
```
