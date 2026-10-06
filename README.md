# PayPulse backend

FastAPI + PostgreSQL skeleton for a multi-provider mobile money aggregation POS
system. Merchants initiate a collection or withdrawal against any linked
provider (C-Pay, M-Pesa, EcoCash, MyWallet, Khetsi); the provider confirms via
callback; a receipt is issued on confirmation.

## What's here

```
app/
  main.py                 FastAPI app, router wiring
  config.py                Settings from environment (.env)
  database.py               Async SQLAlchemy engine/session
  models.py                  ORM models — merchants, users, providers,
                              transactions, callbacks, receipts, settlements
  schemas.py                  Pydantic request/response models
  dependencies.py               JWT auth, RBAC (require_roles), merchant scoping

  adapters/
    base.py                     BaseProviderAdapter interface — every provider
                                 implements this; nothing outside this folder
                                 branches on provider name
    mock_provider.py              Working in-memory adapter for dev/tests
    registry.py                    Maps adapter_key -> configured adapter instance
                                    (this is where you plug in real mpesa.py,
                                    ecocash.py, cpay.py, etc.)

  services/
    transaction_service.py         The state machine: initiate, callback
                                    processing, receipt issuance, timeout sweep

  routers/
    auth.py            POST /auth/login
    transactions.py    POST /transactions, /transactions/withdrawals,
                        GET /transactions, GET /transactions/{id},
                        POST /transactions/{id}/receipt
    callbacks.py        POST /callbacks/{provider_adapter_key}
    merchants.py         merchant onboarding, KYC approval, balances
    providers.py          admin kill switch per provider

  tasks/
    celery_app.py          Beat schedule: expiry sweep, pending-txn poll,
                            daily settlement snapshot
    expiry.py                Sweeps stale pending transactions
    reconciliation.py          Polls providers for lost callbacks; writes
                                immutable daily settlement rows

scripts/
  init_db.py                Local dev table creation (see below) — a real
                             script file, not an inline python -c snippet,
                             so it doesn't fall over on multi-line shell paste
```

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL etc.

# Postgres must be running and reachable at DATABASE_URL.
# 1. Confirm the actual binary exists and check what it is
ls -la /usr/local/opt/python@3.14/bin/

# 2. Clear out any half-created venv from earlier attempts
rm -rf .venv

# 3. Create the venv with that exact interpreter
/usr/local/opt/python@3.14/bin/python3.14 -m venv .venv

# 4. Activate it
source .venv/bin/activate

# 5. Confirm
python --version
which python
# This skeleton ships no Alembic migrations yet — for now:
python scripts/init_db.py

python -m uvicorn app.main:app --reload 
```

Celery worker + beat (separate processes, need Redis running):

```bash
celery -A app.tasks.celery_app worker --loglevel=info
celery -A app.tasks.celery_app beat --loglevel=info
```

Interactive API docs: `http://localhost:8000/docs`

## What's deliberately stubbed

This is a skeleton, not a finished system. Before going anywhere near real
money:

- **Real provider adapters.** Only `mock_provider.py` exists. Add one file per
  provider under `adapters/` (e.g. `adapters/mpesa.py`) implementing
  `BaseProviderAdapter`, register it in `adapters/registry.py`, and seed a row
  in the `providers` table with a matching `adapter_key`.
- **Alembic migrations.** `models.py` defines the schema; wire up
  `alembic init` and generate the first revision from it rather than using
  `create_all` outside of local dev.
- **MFA on login.** `User.mfa_secret` exists in the model and is checked for
  presence in `routers/auth.py` but not yet enforced — add TOTP verification
  before issuing the token.
- **Withdrawal approval workflow.** `settings.withdrawal_approval_threshold`
  is defined but not yet wired to a second-approver step in
  `routers/transactions.py`.
- **Object storage for KYC documents.** `MerchantKycDocument.file_reference`
  expects a path/key into S3-compatible storage — the upload endpoint itself
  isn't built yet.
- **React frontend.** Not included here — this is the API it talks to.
- **Rate limiting on the callback endpoint** and **replay-window checks**
  (rejecting a valid-signature callback that's implausibly old) are worth
  adding once you're integrating against real provider sandboxes.

## Design notes worth keeping in mind while extending this

- **Adapter isolation.** `transaction_service.py` never imports a specific
  provider — it only calls through `BaseProviderAdapter`. If you find
  yourself writing `if provider.name == "mpesa"` anywhere outside
  `adapters/`, that logic belongs in the adapter instead.
- **Callback trust.** Every inbound callback is logged to `callbacks_raw`
  *before* signature verification, and rejected callbacks still get logged
  (just not applied). Never skip this — it's your forensic trail.
- **Idempotency two ways.** `idempotency_key` protects against a merchant
  double-submitting a request; the `provider_reference` uniqueness protects
  against a provider double-sending a callback. Both matter independently.
- **Settlements are snapshots, not live queries.** `daily_settlements` rows
  are written once by the scheduled task and never recomputed — that's what
  makes them usable as an audit record.
