# Stockflow

Offline event accounting. Reports call projection, which consults identity.
An event's identity must preserve both tenant and id as opaque strings.
Journal balances must equal a replay projection of journal event values.
Batches are atomic; even a failed stream cannot expose a partial ledger.

Module map:
- stockflow/__init__.py
- stockflow/api/__init__.py
- stockflow/api/accounts.py
- stockflow/api/append.py
- stockflow/api/export.py
- stockflow/api/health.py
- stockflow/api/report.py
- stockflow/api/status.py
- stockflow/api/tenants.py
- stockflow/diagnostics/__init__.py
- stockflow/diagnostics/empty.py
- stockflow/diagnostics/equality.py
- stockflow/diagnostics/negative.py
- stockflow/diagnostics/owners.py
- stockflow/diagnostics/replay.py
- stockflow/diagnostics/volume.py
- stockflow/domain/__init__.py
- stockflow/domain/accounts.py
- stockflow/domain/events.py
- stockflow/domain/identity.py
- stockflow/domain/projection.py
- stockflow/domain/signs.py
- stockflow/domain/tenants.py
- stockflow/domain/totals.py
- stockflow/imports/__init__.py
- stockflow/imports/batches.py
- stockflow/imports/columns.py
- stockflow/imports/count.py
- stockflow/imports/dedup.py
- stockflow/imports/fields.py
- stockflow/imports/merge.py
- stockflow/imports/preview.py
- stockflow/reporting/__init__.py
- stockflow/reporting/counts.py
- stockflow/reporting/csv.py
- stockflow/reporting/lookup.py
- stockflow/reporting/maximum.py
- stockflow/reporting/summary.py
- stockflow/reporting/text.py
- stockflow/reporting/zeros.py
- stockflow/storage/__init__.py
- stockflow/storage/balances.py
- stockflow/storage/decode.py
- stockflow/storage/journal.py
- stockflow/storage/jsonlines.py
- stockflow/storage/keys.py
- stockflow/storage/size.py
- stockflow/storage/snapshot.py
