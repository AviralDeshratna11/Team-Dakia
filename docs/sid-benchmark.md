# Sourcing Desk benchmark for `sid`

This report records the offline CLI benchmark against `sid` commit `73a757cee7479e4ed2d5a69f19ad2231daa196b3`. It is a historical result for that revision. [Tested commit](https://github.com/AviralDeshratna11/Team-Dakia/commit/73a757cee7479e4ed2d5a69f19ad2231daa196b3)

## Results

| Run | Result |
|---|---|
| Full offline suite | 23 passed, 1 skipped, 4 expected failures |
| Focused benchmark with expected failures enforced | 9 passed, 4 failed |
| Combined statement and branch coverage for `desk` | 70% |

The benchmark exercised the seven top-level CLI commands `run`, `show`, `orders`, `approve`, `wallet`, `pack`, and `rfq`. Its checks included quote extraction and citations, stock and delivery boundaries, shipping consolidation, approval holds, checkout recovery, mock RFQ replies, and pack approval.

## Findings at the tested revision

| Finding | Observed behavior | Expected behavior |
|---|---|---|
| Duplicate wallet debit | A retry after interruption between the wallet debit and mock intent confirmation debits the same intent twice. | Exactly one ledger debit settles the order. |
| Negative unit price | A negative quote qualifies for selection and produces a negative mock charge. | A negative price is rejected before an order or payment is created. |
| Stock below minimum order quantity | The stock check uses the requested quantity even when the supplier's minimum requires buying more. | Available stock covers the actual purchase quantity. |
| Unknown arrival date | A quote without an arrival estimate qualifies despite an explicit deadline. | An unknown arrival prevents automatic purchase when a deadline is set. |

## Status on `main`

The four findings are fixed: in `912984e` (Aviral), and the archived cases now run as a regression suite, `tests/test_benchmark_sid.py`, with no expected-failure markers (13 passed). The interruption case patches `MockGateway._save` with its current signature, `_save(intent, db)`. The archived file's monkeypatch targets the old method, so run as archived it fails with a `TypeError`, not a double debit; the behaviour itself is fixed.

A review after those fixes found more gaps of the same kind, now fixed with tests:

| Gap | Fix |
|---|---|
| A ₹0 price counted as valid (`price >= 0`), and ₹0 orders and payments were allowed | Prices must be above zero; orders and payment intents refuse ₹0 |
| A later run that picks another vendor for a line creates a second order and pays for the line twice | One live order per request line: a started or paid order is finished, never duplicated; unpaid ones become `SUPERSEDED` |
| Cached quotes never age, so an old price can be bought automatically | Quotes older than the pack's `max_quote_age_hours` (72) go to founder review |
| A quote with no source URL or observation time can be bought automatically | Founder review |
| For a vendor not in the pack, arrival comes from a default transit time but counts as known | Marked `assumed`; with a deadline, it goes to founder review. RFQ vendors can confirm a delivery time. |
| The agent's reported seller domain is never checked against the pages it fetched | Flagged for founder review |
| Two runs started in the same second share a trace folder | Run IDs include microseconds |

## Reproduce

Open the tested `sid` revision in a separate checkout. Copy [the archived benchmark cases](sid-benchmark-cases.py) into that checkout as `tests/test_benchmark_sid.py`, then run from its root:

```powershell
uv sync --locked
$env:PYTHONUTF8 = "1"
$env:PYTHON_DOTENV_DISABLED = "1"
$env:DESK_PAYMENT_PROVIDER = "mock"
uv run --locked pytest -q -rxs
uv run --locked pytest -q --runxfail tests/test_benchmark_sid.py
```

The second command counts the four expected failures as failures. The archived cases depend on the `sid` CLI, its test fixtures, and its category packs.

## Limits

Paid data purchasing was deferred, so that test was skipped. The run used Python 3.12 on Windows, synthetic supplier records, temporary SQLite databases, a mock wallet, and a mock mailbox. Live supplier accuracy, Vertex model quality, production latency, and real payment settlement remain unmeasured.
