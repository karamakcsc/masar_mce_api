"""Shared receive flow for the master-data APIs - same principles as
process_inventory_transaction: synchronous (never enqueued), every request
logged in MCE API Log, every record saved with a result (here as an MCE
Master Data Record) and committed on its own, so one bad record can never
roll back or block another."""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime, strip_html

from masar_mce_api.api import _finish_log, _finish_with_error, _parse_records, _start_receive_log
from masar_mce_api.master_data import agreement, item, supplier
from masar_mce_api.master_data.common import Rejected, RecordContext, clean, reset_cache

APIS = {
	"item": frappe._dict(
		api_name="process_item",
		enabled_field="item_api_enabled",
		list_key="items",
		key_field="item_code",
		record_type="Item",
		label="Item API",
		process=item.process,
	),
	"supplier": frappe._dict(
		api_name="process_supplier",
		enabled_field="supplier_api_enabled",
		list_key="suppliers",
		key_field="supplier_code",
		record_type="Supplier",
		label="Supplier API",
		process=supplier.process,
	),
	"agreement": frappe._dict(
		api_name="process_supplier_agreement",
		enabled_field="agreement_api_enabled",
		list_key="agreements",
		key_field="order_no",
		record_type="Supplier Agreement",
		label="Supplier Agreement API",
		process=agreement.process,
	),
}

UNEXPECTED = "Unexpected system error while processing this record. It has been logged for investigation."


def receive(kind):
	api = APIS[kind]
	request_id = frappe.generate_hash(length=10)
	started_at = now_datetime()
	log = _start_receive_log(request_id, started_at, api.api_name)

	if not frappe.db.get_single_value("API Settings", api.enabled_field):
		return _finish_with_error(log, started_at, 503, _("{0} is disabled.").format(api.label))

	try:
		records = _parse_records(frappe.local.form_dict, api.list_key)
	except frappe.ValidationError as e:
		frappe.local.message_log = []
		return _finish_with_error(log, started_at, 400, str(e))
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"{api.api_name}: malformed request")
		return _finish_with_error(log, started_at, 400, _("Malformed request body"))

	try:
		reset_cache()
		results = [_process_record(api, raw, idx, log.name, request_id) for idx, raw in enumerate(records)]
	except Exception:
		# Only reachable if the per-record guard itself fails (e.g. lost DB
		# connection); records committed before this point stay committed.
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"{api.api_name}: unexpected error")
		response = _finish_with_error(log, started_at, 500, _("Unexpected system error"))
		log.db_set("application_status", "Error")
		frappe.db.commit()
		return response

	created = sum(1 for r in results if r["status"] == "Created")
	rejected = len(results) - created
	response = {
		"success": True,
		"total": len(results),
		"created": created,
		"rejected": rejected,
		"records": results,
	}
	if rejected == 0:
		status = "Success"
	elif created == 0:
		status = "Failed"
	else:
		status = "Partial Success"
	_finish_log(log, started_at, response, status, len(results), created, rejected)
	return response


def _process_record(api, raw, idx, log_name, request_id):
	key = clean(raw.get(api.key_field))
	ctx = RecordContext()
	savepoint = f"mce_md_{idx}"
	frappe.db.savepoint(savepoint)

	document = None
	try:
		document = api.process(raw, ctx)
		reasons = []
	except Rejected as e:
		reasons = e.reasons
	except frappe.ValidationError as e:
		# ERPNext's own validation of the Item/Supplier/master being created.
		reasons = [strip_html(str(e)) or e.__class__.__name__]
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"{api.api_name}: unexpected error ({key})")
		reasons = [_(UNEXPECTED)]

	if reasons:
		# Undo every master auto-created for this record - none of them is
		# kept for a record that was not created.
		frappe.db.rollback(save_point=savepoint)
		reset_cache()
		document = None
		ctx.auto_created = []
		if ctx.supplier and not frappe.db.exists("Supplier", ctx.supplier):
			ctx.supplier = None  # created by this record and rolled back
	frappe.local.message_log = []

	record = frappe.new_doc("MCE Master Data Record")
	record.record_type = api.record_type
	record.external_key = key
	record.status = "Rejected" if reasons else "Created"
	record.rejection_reason = " | ".join(reasons) or None
	record.auto_created = "\n".join(ctx.auto_created) or None
	record.warnings = "\n".join(ctx.warnings) or None
	record.supplier = ctx.supplier
	record.api_log = log_name
	record.request_id = request_id
	record.raw_payload = json.dumps(raw, default=str, ensure_ascii=False)
	if document:
		record.created_document_type = document.doctype
		record.created_document_name = document.name

	try:
		record.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		reset_cache()
		frappe.log_error(frappe.get_traceback(), f"{api.api_name}: could not save MCE Master Data Record ({key})")
		return {
			api.key_field: key,
			"status": "Rejected",
			"record": None,
			"document": None,
			"reason": _(UNEXPECTED),
			"auto_created": [],
			"warnings": [],
		}

	return {
		api.key_field: key,
		"status": record.status,
		"record": record.name,
		"document": record.created_document_name,
		"reason": record.rejection_reason,
		"auto_created": ctx.auto_created,
		"warnings": ctx.warnings,
		**ctx.extra,
	}
