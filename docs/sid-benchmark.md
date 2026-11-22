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
