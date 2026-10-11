"""Inbound master-data APIs (Item, Supplier; Supplier Agreement later).

runner.py     - shared receive flow: API Log, enable switch, batch parsing,
                per-record savepoint + MCE Master Data Record, response.
common.py     - per-request settings/cache, RecordContext (planned creates).
resolvers.py  - Create/Stop resolution of each master-data dependency.
item.py       - Item record processing.
supplier.py   - Supplier record processing (full + minimal).

Adding a new API = a process(raw, ctx) function plus one entry in
runner.APIS and its enable/Create-Stop fields in API Settings.
"""
