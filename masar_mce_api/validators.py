"""Reusable validators for the inbound inventory-transaction API.

Each low-level validator returns an error string (or None on success) rather
than raising, so a batch of up to 100 transactions can keep going after a
single row fails. run_validation() is the orchestrator, called from
MCEInventoryTransaction.validate() - it always sets validation_status /
rejection_reason and never raises for a business-rule failure, since a
Rejected transaction still has to be saved.
"""

from datetime import datetime

import frappe
from frappe import _

from masar_mce_api.utils import (
	get_active_supplier_agreement,
	item_allowed_for_supplier,
	item_exists,
	resolve_purchase_order_for_agreement_item,
	resolve_supplier_by_code,
	resolve_warehouse_by_code,
)

TRANSACTION_TYPES = ("F1", "G1A", "G1G", "H1", "H3", "S1")

POSTING_DATETIME_FORMAT = "%d/%m/%Y %H:%M:%S"

# Warehouse role rules for the four Stock Entry ("سند تدوير") transaction
# types. G1G (Purchase Receipt) and S1 (Return to Supplier issue) are handled
# separately below: G1G has no source warehouse and no warehouse-type
# restriction on the target; S1 issues stock out of the source warehouse
# (any type) back to a supplier, so its target warehouse is optional.
WAREHOUSE_RULES = {
	"F1": {"source_type": "سوق", "target_type": "مستودع"},
	"G1A": {"source_type": "مستودع", "target_type": "سوق"},
	"H1": {"source_type": "سوق", "target_type": "سوق"},
	"H3": {"source_type": "مستودع", "target_type": "مستودع"},
}


def validate_transaction_type(transaction_type):
	if not transaction_type:
		return _("transaction_type is required")
	if transaction_type not in TRANSACTION_TYPES:
		return _("Invalid transaction type: {0}").format(transaction_type)
	return None


def parse_posting_datetime(raw):
	if not raw:
		return None, _("posting_datetime is required")
	try:
		return datetime.strptime(raw, POSTING_DATETIME_FORMAT), None
	except (ValueError, TypeError):
		return None, _("posting_datetime {0} must be in the format dd/mm/YYYY HH:MM:SS").format(raw)


def validate_voucher_id(voucher_id, doc=None):
	"""doc must be passed on every validation so re-validating an existing
	document (e.g. on submit, which re-runs validate()) excludes itself -
	and, since a later resend of the same voucher_id is deliberately saved
	as its own Rejected record rather than being dropped, only a row with an
	*earlier* creation time counts as the original: otherwise the first,
	legitimately-Accepted row would see its own later duplicate and start
	rejecting itself too."""
	if not voucher_id:
		return _("voucher_id is required")
	filters = {"voucher_id": voucher_id}
	if doc is not None:
		if doc.name:
			filters["name"] = ["!=", doc.name]
		if doc.creation:
			filters["creation"] = ["<", doc.creation]
	if frappe.db.exists("MCE Inventory Transaction", filters):
		return _("Duplicate transaction: voucher_id {0} has already been received").format(voucher_id)
	return None


def validate_warehouse_role(code, role_type, field_label):
	"""Resolve an external warehouse code and, if role_type is given, confirm
	it is a warehouse of that type. Returns (resolved_dict_or_None, error)."""
	if not code:
		return None, _("{0} is required").format(field_label)
	warehouse = resolve_warehouse_by_code(code, role_type)
	if not warehouse:
		return None, _("{0} {1} does not exist").format(field_label, code)
	if role_type and warehouse.warehouse_type != role_type:
		return None, _("{0} {1} must be of warehouse type '{2}', found '{3}'").format(
			field_label, code, role_type, warehouse.warehouse_type
		)
	return warehouse, None


def validate_supplier(code):
	if not code:
		return None, _("supplier is required")
	supplier = resolve_supplier_by_code(code)
	if not supplier:
		return None, _("Supplier {0} does not exist").format(code)
	return supplier, None


def validate_item_row(item_code, qty, rate, amount):
	if not item_code:
		return _("item_code is required")
	if not item_exists(item_code):
		return _("Item {0} does not exist").format(item_code)

	try:
		qty = float(qty)
	except (TypeError, ValueError):
		return _("qty is invalid for item {0}").format(item_code)
	if qty <= 0:
		return _("qty must be greater than zero for item {0}").format(item_code)

	if rate in (None, ""):
		return _("rate is required for item {0}").format(item_code)
	try:
		rate = float(rate)
	except (TypeError, ValueError):
		return _("rate is invalid for item {0}").format(item_code)
	if rate < 0:
		return _("rate must not be negative for item {0}").format(item_code)

	if amount not in (None, ""):
		try:
			amount = float(amount)
		except (TypeError, ValueError):
			return _("amount is invalid for item {0}").format(item_code)
		if amount < 0:
			return _("amount must not be negative for item {0}").format(item_code)

	return None


def validate_g1g_agreement(item_code, supplier):
	agreement = get_active_supplier_agreement(item_code)
	if not agreement:
		return _("No active Supplier Agreement found for item {0}").format(item_code)
	if not resolve_purchase_order_for_agreement_item(item_code, agreement, supplier):
		return _("No open Purchase Order found for item {0} under Supplier Agreement {1}").format(item_code, agreement)
	return None


def validate_s1_party_item(item_code, supplier):
	"""Mirrors masar_mce's Stock Entry.on_submit::validate_party_specific_item
	for 'إرجاع إلى المورد', so the row is rejected up front instead of the
	created Stock Entry failing later at submit."""
	if not item_allowed_for_supplier(item_code, supplier):
		return _("Item {0} is not allowed for Supplier {1} (no Party Specific Item)").format(item_code, supplier)
	return None


def _reject(doc, reasons):
	doc.validation_status = "Rejected"
	doc.rejection_reason = " | ".join(reasons)


def run_validation(doc):
	reasons = []

	type_error = validate_transaction_type(doc.transaction_type)
	if type_error:
		_reject(doc, [type_error])
		return

	voucher_error = validate_voucher_id(doc.voucher_id, doc)
	if voucher_error:
		reasons.append(voucher_error)

	posting_dt, dt_error = parse_posting_datetime(doc.posting_datetime_raw)
	if dt_error:
		reasons.append(dt_error)
	else:
		doc.posting_date = posting_dt.date()
		doc.posting_time = posting_dt.time()

	if doc.transaction_type == "G1G":
		if doc.source_warehouse_no:
			reasons.append(_("source_warehouse must not be provided for transaction type G1G"))

		target_warehouse, error = validate_warehouse_role(doc.target_warehouse_no, None, _("target_warehouse"))
		if error:
			reasons.append(error)
		elif target_warehouse:
			doc.target_warehouse = target_warehouse.name

		supplier, error = validate_supplier(doc.supplier_raw)
		if error:
			reasons.append(error)
		elif supplier:
			doc.supplier = supplier
	elif doc.transaction_type == "S1":
		source_warehouse, error = validate_warehouse_role(doc.source_warehouse_no, None, _("source_warehouse"))
		if error:
			reasons.append(error)
		elif source_warehouse:
			doc.source_warehouse = source_warehouse.name

		# Optional: a Material Issue has no target. When sent it must still be
		# a real warehouse, but it is only recorded here, never on the Stock Entry.
		if doc.target_warehouse_no:
			target_warehouse, error = validate_warehouse_role(doc.target_warehouse_no, None, _("target_warehouse"))
			if error:
				reasons.append(error)
			elif target_warehouse:
				doc.target_warehouse = target_warehouse.name

		supplier, error = validate_supplier(doc.supplier_raw)
		if error:
			reasons.append(error)
		elif supplier:
			doc.supplier = supplier
	else:
		rule = WAREHOUSE_RULES.get(doc.transaction_type)
		if rule:
			source_warehouse, error = validate_warehouse_role(
				doc.source_warehouse_no, rule["source_type"], _("source_warehouse")
			)
			if error:
				reasons.append(error)
			elif source_warehouse:
				doc.source_warehouse = source_warehouse.name

			target_warehouse, error = validate_warehouse_role(
				doc.target_warehouse_no, rule["target_type"], _("target_warehouse")
			)
			if error:
				reasons.append(error)
			elif target_warehouse:
				doc.target_warehouse = target_warehouse.name

	if not doc.items:
		reasons.append(_("At least one item is required"))

	for idx, row in enumerate(doc.items, start=1):
		row_error = validate_item_row(row.item_code, row.qty, row.rate, row.amount)
		if row_error:
			row.validation_error = row_error
			reasons.append(_("Item row {0}: {1}").format(idx, row_error))
		elif doc.transaction_type == "G1G" and doc.get("supplier"):
			agreement_error = validate_g1g_agreement(row.item_code, doc.supplier)
			if agreement_error:
				row.validation_error = agreement_error
				reasons.append(_("Item row {0}: {1}").format(idx, agreement_error))
		elif doc.transaction_type == "S1" and doc.get("supplier"):
			party_error = validate_s1_party_item(row.item_code, doc.supplier)
			if party_error:
				row.validation_error = party_error
				reasons.append(_("Item row {0}: {1}").format(idx, party_error))

	if reasons:
		_reject(doc, reasons)
	else:
		doc.validation_status = "Accepted"
		doc.rejection_reason = None
