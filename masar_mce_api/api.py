import json

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime

from masar_mce_api.utils import mask_headers

SUCCESS_STATUS_CODES = (200, 201, 202)
DEFAULT_MAX_BATCH_SIZE = 100


def _get_max_batch_size():
    """API Settings.max_batch_size overrides the default when set to a
    positive number; 0/blank (e.g. a not-yet-configured settings doc) falls
    back to DEFAULT_MAX_BATCH_SIZE rather than rejecting every batch."""
    configured = cint(frappe.db.get_single_value("API Settings", "max_batch_size"))
    return configured if configured > 0 else DEFAULT_MAX_BATCH_SIZE


def get_api_settings():
    settings = frappe.get_single("API Settings")
    if not settings.enabled:
        frappe.throw("API Settings is disabled. Enable it before making this request.")
    headers = json.loads(settings.headers) if settings.headers else {}
    return settings.url, headers, settings.request_method, settings.timeout


def insert_pos_item(payload_local_zone, payload_free_zone, transactional=False):
    if payload_free_zone and payload_local_zone:
        url, headers, method, timeout = get_api_settings()
        if not url or not headers or not method or not timeout:
            
            frappe.throw("API Settings are not properly configured. Please check the settings.", alert=True, indicator="red")
            return
        free_zone_response = _send_outgoing_request("insert_pos_item", url, method, headers, payload_free_zone, timeout, transactional)
        local_zone_response = _send_outgoing_request("insert_pos_item", url, method, headers, payload_local_zone, timeout, transactional)
        if (
            free_zone_response is not None
            and local_zone_response is not None
            and free_zone_response.status_code in SUCCESS_STATUS_CODES
            and local_zone_response.status_code in SUCCESS_STATUS_CODES
        ):
            frappe.msgprint("Item Successfully inserted", alert=True, indicator="green")
        else:
            free_text = free_zone_response.text if free_zone_response is not None else _("request failed")
            local_text = local_zone_response.text if local_zone_response is not None else _("request failed")
            frappe.throw(f"Error in inserting item:<br>Local Zone response: {local_text}<br>Free Zone response: {free_text}")


def _send_outgoing_request(api_name, url, method, headers, payload, timeout, transactional=False):
    """POST payload to the external system and log the call, masking
    credentials in headers.

    transactional=False: the log is committed immediately.
    transactional=True: the caller's transaction is not committed, so a failed
    call can roll back the calling document. If a rollback happens, the log is
    saved again afterwards so the failed call is still recorded."""
    started_at = now_datetime()
    body = json.dumps(payload, default=str)

    log = frappe.new_doc("MCE API Log")
    log.api_name = api_name
    log.api_type = "Send"
    log.http_method = method
    log.endpoint = url
    log.request_headers = json.dumps(mask_headers(headers))
    log.request_payload = body
    log.request_timestamp = started_at
    log.integration_user = frappe.session.user

    response = None
    try:
        response = requests.request(method, url, headers=headers, data=body, timeout=timeout)
        log.status_code = response.status_code
        log.response_payload = response.text
        log.application_status = "Success" if response.status_code in SUCCESS_STATUS_CODES else "Failed"
    except requests.RequestException as e:
        log.application_status = "Error"
        log.error_message = str(e)

    log.response_timestamp = now_datetime()
    log.processing_duration = (log.response_timestamp - started_at).total_seconds()

    if transactional:
        log_data = log.as_dict()  # taken before insert, so it has no name yet
        log.insert(ignore_permissions=True)
        frappe.db.after_rollback.add(lambda data=log_data: _save_log_after_rollback(data))
    else:
        log.insert(ignore_permissions=True)
        frappe.db.commit()
    return response


def _save_log_after_rollback(log_data):
    """Runs after the caller's transaction was rolled back, in a fresh transaction."""
    frappe.get_doc(log_data).insert(ignore_permissions=True)
    frappe.db.commit()

# ---------------------------------------------------------------------------
# Inbound inventory transaction API
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def process_inventory_transaction():
    """Receive up to DEFAULT_MAX_BATCH_SIZE inventory transactions
    synchronously (never enqueued). Every transaction is always saved as an
    MCE Inventory Transaction - Accepted or Rejected - regardless of whether
    other rows in the same batch fail. See masar_mce_api.validators and the
    doctype's own validate() for the per-type business rules, and
    _save_transaction below for why one row's failure can never roll back or
    block another."""
    request_id = frappe.generate_hash(length=10)
    started_at = now_datetime()
    log = _start_receive_log(request_id, started_at)

    if not frappe.db.get_single_value("API Settings", "receivable_enabled"):
        return _finish_with_error(
            log, started_at, 503, _("Inventory transaction processing is temporarily disabled.")
        )

    try:
        transactions = _parse_transactions(frappe.local.form_dict)
    except frappe.ValidationError as e:
        return _finish_with_error(log, started_at, 400, str(e))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "process_inventory_transaction: malformed request")
        return _finish_with_error(log, started_at, 400, _("Malformed request body"))

    results = []
    accepted = 0
    rejected = 0

    for raw in transactions:
        result = _save_transaction(raw, log.name, request_id)
        results.append(result)
        if result.get("status") == "Accepted":
            accepted += 1
        else:
            rejected += 1

    response = {
        "success": True,
        "total": len(transactions),
        "accepted": accepted,
        "rejected": rejected,
        "transactions": results,
    }
    _finish_log(
        log,
        started_at,
        response,
        "Success" if rejected == 0 else "Partial Success",
        len(transactions),
        accepted,
        rejected,
    )
    return response


def _parse_transactions(form_dict):
    return _parse_records(form_dict, "transactions")


def _parse_records(form_dict, key):
    """Shared by every receive API: form_dict[key] must be a non-empty list
    of objects (or its JSON string), no longer than the max batch size."""
    records = form_dict.get(key)
    if records is None:
        frappe.throw(_("{0} is required").format(key))
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except ValueError:
            frappe.throw(_("{0} must be valid JSON").format(key))
    if not isinstance(records, list):
        frappe.throw(_("{0} must be a list").format(key))
    if not records:
        frappe.throw(_("{0} must contain at least one record").format(key))
    max_batch_size = _get_max_batch_size()
    if len(records) > max_batch_size:
        frappe.throw(_("A maximum of {0} {1} is allowed per request").format(max_batch_size, key))
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            frappe.throw(_("{0}[{1}] must be an object").format(key, i))
    return records


def _save_transaction(raw, log_name, request_id):
    """Always returns a per-transaction result dict. The insert (which runs
    MCEInventoryTransaction.validate() -> Accepted/Rejected) is committed on
    its own as soon as it succeeds, so an unexpected error on transaction #20
    can never roll back the transactions already saved before it; if this
    specific row can't even be saved, it's still reported (never silently
    dropped) and logged for investigation."""
    items = raw.get("items")
    if isinstance(items, dict):
        items = [items]
    elif not isinstance(items, list):
        items = []

    doc = frappe.new_doc("MCE Inventory Transaction")
    doc.transaction_type = raw.get("transaction_type")
    doc.voucher_id = raw.get("voucher_id")
    doc.remarks = raw.get("remarks")
    doc.posting_datetime_raw = raw.get("posting_datetime")
    doc.supplier_raw = raw.get("supplier")
    doc.source_warehouse_no = raw.get("source_warehouse")
    doc.target_warehouse_no = raw.get("target_warehouse")
    doc.api_log = log_name
    doc.request_id = request_id
    doc.raw_payload = json.dumps(raw, default=str)

    for item in items:
        if not isinstance(item, dict):
            continue
        doc.append(
            "items",
            {
                "item_code": item.get("item_code"),
                "qty": item.get("qty"),
                "rate": item.get("rate"),
                "amount": item.get("amount"),
                "supplier_agreement_raw": item.get("supplier_agreement"),
            },
        )

    try:
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "MCE Inventory Transaction: unexpected error")
        return {
            "voucher_id": raw.get("voucher_id"),
            "status": "Rejected",
            "record": None,
            "reason": _("Unexpected system error while saving this transaction. It has been logged for investigation."),
        }

    return {
        "voucher_id": doc.voucher_id,
        "status": doc.validation_status,
        "record": doc.name,
        "reason": doc.rejection_reason,
    }


# ---------------------------------------------------------------------------
# Inbound master-data APIs (Item, Supplier, Supplier Agreement). Each is enabled independently in
# API Settings > Master Data (Incoming); see masar_mce_api.master_data.
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def process_item():
    from masar_mce_api.master_data.runner import receive

    return receive("item")


@frappe.whitelist(methods=["POST"])
def process_supplier():
    from masar_mce_api.master_data.runner import receive

    return receive("supplier")


@frappe.whitelist(methods=["POST"])
def process_supplier_agreement():
    from masar_mce_api.master_data.runner import receive

    return receive("agreement")


def _start_receive_log(request_id, started_at, api_name="process_inventory_transaction"):
    log = frappe.new_doc("MCE API Log")
    log.api_name = api_name
    log.api_type = "Receive"
    log.request_id = request_id
    log.http_method = "POST"
    log.endpoint = _get_endpoint(api_name)
    log.request_headers = json.dumps(_get_request_headers_masked())
    log.request_payload = json.dumps(frappe.local.form_dict, default=str)
    log.request_timestamp = started_at
    log.integration_user = frappe.session.user
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log


def _finish_log(log, started_at, response_payload, application_status, total, accepted, rejected):
    finished_at = now_datetime()
    log.db_set("response_payload", json.dumps(response_payload, default=str))
    log.db_set("response_timestamp", finished_at)
    log.db_set("processing_duration", (finished_at - started_at).total_seconds())
    log.db_set("application_status", application_status)
    log.db_set("total_records", total)
    log.db_set("accepted_records", accepted)
    log.db_set("rejected_records", rejected)
    frappe.db.commit()


def _finish_with_error(log, started_at, http_status, message):
    """Reached when the request itself is malformed (not a per-transaction
    validation failure) - the log still records the failure and the response
    stays structured, never a bare traceback."""
    finished_at = now_datetime()
    log.db_set("application_status", "Failed")
    log.db_set("error_message", message)
    log.db_set("response_timestamp", finished_at)
    log.db_set("processing_duration", (finished_at - started_at).total_seconds())
    frappe.db.commit()
    frappe.local.response.http_status_code = http_status
    return {"success": False, "message": message}


def _get_endpoint(api_name="process_inventory_transaction"):
    try:
        return frappe.local.request.path
    except Exception:
        return f"/api/method/masar_mce_api.api.{api_name}"


def _get_request_headers_masked():
    try:
        return mask_headers(dict(frappe.local.request.headers))
    except Exception:
        return {}
