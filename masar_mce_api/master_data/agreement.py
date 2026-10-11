"""Supplier Agreement (Blanket Order) receive API record processing.

Agreement payload: order_no (MCE agreement number, duplicate key), supplier
(code), custom_supplier_agreement_type, custom_pricing_type, from_date,
to_date, company, order_date, custom_title, terms, custom_receipt_allowance_check,
custom_receipt_allowance, custom_submit_after_inspection, custom_other_terms
[{tcs_terms, terms}], custom_penalties (never created), items [...].

Item row payload: item_code, qty, rate, custom_markup_percentage,
custom_selling_price, custom_purchase_price_after_tax,
custom_selling_price_after_tax, custom_suggest_accounting_price,
custom_inspection_is_required, custom_quality_inspection_quantity,
terms_and_conditions.

The agreement is inserted as Draft (workflow initial state) and never
submitted here: submission belongs to masar_mce's dual-entry pricing
verification / approval workflow.

Rules enforced in code: this API never creates Items, Suppliers or
penalties - custom_penalties is never written.
"""

from datetime import datetime

import erpnext
import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from masar_mce_api import pricing
from masar_mce_api.master_data.common import (
	CREATE,
	Rejected,
	action,
	cache,
	cached_exists,
	clean,
	get_settings,
	remember,
)
from masar_mce_api.utils import resolve_supplier_by_code
from masar_mce.utils import get_tax_rates_bulk

LOCAL_ZONE = "Local Zone"
WARN = "Warn in Terms and Conditions"
IGNORE = "Ignore"

# Optional pass-through fields (copied as received when present).
AGREEMENT_OPTIONAL = {
	"custom_receipt_allowance_check": cint,
	"custom_receipt_allowance": flt,
	"custom_submit_after_inspection": cint,
}
ITEM_OPTIONAL = {
	"custom_suggest_accounting_price": flt,
	"custom_inspection_is_required": cint,
	"custom_quality_inspection_quantity": flt,
	"terms_and_conditions": str,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_date(raw, field, ctx):
	value = clean(raw)
	if not value:
		ctx.reject(_("{0} is required").format(field))
		return None
	for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
		try:
			return datetime.strptime(value, fmt).date()
		except ValueError:
			pass
	ctx.reject(_("{0} must be YYYY-MM-DD or dd/mm/YYYY: {1}").format(field, value))
	return None


def _number(raw, label, ctx, required=False):
	"""None when not sent; records a reason when sent but not numeric."""
	if raw is None or (isinstance(raw, str) and not raw.strip()):
		if required:
			ctx.reject(_("{0} is required").format(label))
		return None
	try:
		return float(raw)
	except (TypeError, ValueError):
		ctx.reject(_("{0} is invalid: {1}").format(label, raw))
		return None


def pricing_type_options():
	store = cache()
	if "pricing_type_options" not in store:
		options = frappe.get_meta("Blanket Order").get_field("custom_pricing_type").options or ""
		store["pricing_type_options"] = [o for o in options.split("\n") if o.strip()]
	return store["pricing_type_options"]


def _item_states(item_codes):
	"""{item_code: {"name", "disabled"} | None}, bulk-loaded and cached for
	the whole request so repeated Items are queried once."""
	store = cache()
	missing = [c for c in item_codes if ("item_state", c) not in store]
	if missing:
		found = {
			r.name: r
			for r in frappe.get_all(
				"Item", filters={"name": ["in", missing]}, fields=["name", "disabled"]
			)
		}
		lowered = {k.lower(): v for k, v in found.items()}
		for code in missing:
			store[("item_state", code)] = found.get(code) or lowered.get(code.lower())
	return {c: store[("item_state", c)] for c in item_codes}


def _local_tax_rates(item_codes):
	"""Local Zone tax fraction per Item via masar_mce's own
	get_tax_rates_bulk (the function blanket_order.validate uses), cached."""
	store = cache()
	missing = [c for c in item_codes if ("local_tax", c) not in store]
	if missing:
		rates = get_tax_rates_bulk(missing, categories=[LOCAL_ZONE])
		for code in missing:
			store[("local_tax", code)] = flt(rates.get((code, LOCAL_ZONE), 0))
	return {c: store[("local_tax", c)] for c in item_codes}


# ---------------------------------------------------------------------------
# agreement-level resolution
# ---------------------------------------------------------------------------

def _resolve_supplier(ctx, code):
	if not code:
		ctx.reject(_("supplier is required"))
		return None
	name = resolve_supplier_by_code(code)
	if not name:
		ctx.reject(_("Supplier does not exist: {0}").format(code))
		return None
	if frappe.get_cached_value("Supplier", name, "disabled"):
		ctx.reject(_("Supplier is disabled: {0}").format(name))
		return None
	ctx.supplier = name
	return name


def _resolve_agreement_type(ctx, value):
	if not value:
		ctx.reject(_("custom_supplier_agreement_type is required"))
		return None
	existing = cached_exists("Supplier Agreement Type", value)
	if existing:
		if frappe.get_cached_value("Supplier Agreement Type", existing, "disabled"):
			ctx.reject(_("Supplier Agreement Type is disabled: {0}").format(existing))
			return None
		return existing
	if action("agreement_type_action") != CREATE:
		ctx.reject(_("Supplier Agreement Type does not exist: {0}").format(value))
		return None

	def create():
		doc = frappe.get_doc({"doctype": "Supplier Agreement Type", "supplier_agreement_type": value})
		doc.insert(ignore_permissions=True)
		remember("Supplier Agreement Type", doc.name)
		return doc.name

	return ctx.plan(("Supplier Agreement Type", value), _("Supplier Agreement Type"), create)


def _resolve_pricing_type(ctx, value):
	allowed = pricing_type_options()
	if not value:
		ctx.reject(_("custom_pricing_type is required. Allowed values: {0}").format(", ".join(allowed)))
		return None
	if value not in allowed:
		ctx.reject(_("Invalid pricing type: {0}. Allowed values: {1}").format(value, ", ".join(allowed)))
		return None
	return value


def _resolve_company(ctx, value):
	company = value or get_settings().default_agreement_company or erpnext.get_default_company()
	if not company:
		ctx.reject(_("company is required (no Default Agreement Company configured)"))
		return None
	existing = cached_exists("Company", company)
	if not existing:
		ctx.reject(_("Company does not exist: {0}").format(company))
	return existing


def _resolve_other_terms(ctx, raw):
	"""custom_other_terms rows: Terms and Conditions link (tcs_terms, mandatory
	on the row) + the received text (terms), which is always kept as sent."""
	stop = action("agreement_other_terms_action") != CREATE
	if raw in (None, "", []):
		if stop:
			ctx.reject(_("custom_other_terms is required (Other Terms = Stop)"))
		return []
	if isinstance(raw, dict):
		raw = [raw]
	if not isinstance(raw, list):
		ctx.reject(_("custom_other_terms must be a list"))
		return []

	rows = []
	for idx, term in enumerate(raw, start=1):
		if not isinstance(term, dict):
			ctx.reject(_("custom_other_terms row {0} must be an object").format(idx))
			continue
		title = clean(term.get("tcs_terms"))
		text = term.get("terms") if isinstance(term.get("terms"), str) else None
		if not title:
			ctx.reject(_("custom_other_terms row {0}: tcs_terms is required").format(idx))
			continue
		existing = cached_exists("Terms and Conditions", title)
		if existing:
			rows.append({"tcs_terms": existing, "terms": text})
			continue
		if stop:
			ctx.reject(_("Terms and Conditions does not exist: {0}").format(title))
			continue

		def create(title=title, text=text):
			doc = frappe.get_doc(
				{"doctype": "Terms and Conditions", "title": title, "terms": text, "buying": 1}
			)
			doc.insert(ignore_permissions=True)
			remember("Terms and Conditions", doc.name)
			return doc.name

		rows.append({"tcs_terms": ctx.plan(("Terms and Conditions", title), _("Terms and Conditions"), create), "terms": text})
	return rows


def _check_penalties(ctx, raw):
	"""Penalties are NEVER created by this API: no Penalty, Penalty Type or
	custom_penalties row is built from the payload under any setting."""
	if raw in (None, "", [], {}):
		return
	if action("agreement_penalties_action") == IGNORE:
		ctx.warnings.append(_("Penalties were received and ignored (the API does not create penalties)"))
	else:
		ctx.reject(_("Penalties were received but the API cannot create penalties (Received Penalties = Stop)"))


def _check_duplicate(ctx, order_no):
	existing = frappe.db.get_value(
		"Blanket Order", {"order_no": order_no, "docstatus": ["<", 2]}, "name"
	)
	if existing:
		raise Rejected(_("Supplier Agreement already exists for order_no {0}: {1}").format(order_no, existing))


# ---------------------------------------------------------------------------
# items + pricing
# ---------------------------------------------------------------------------

def _parse_items(ctx, raw_items):
	if isinstance(raw_items, dict):
		raw_items = [raw_items]
	if not isinstance(raw_items, list) or not raw_items:
		ctx.reject(_("items must contain at least one row"))
		return []

	rows, seen = [], set()
	for idx, raw in enumerate(raw_items, start=1):
		if not isinstance(raw, dict):
			ctx.reject(_("Item row {0} must be an object").format(idx))
			continue
		code = clean(raw.get("item_code"))
		if not code:
			ctx.reject(_("Item row {0}: item_code is required").format(idx))
			continue
		if code in seen:
			ctx.reject(_("Item {0} appears more than once").format(code))
			continue
		seen.add(code)

		qty = _number(raw.get("qty"), _("Item {0}: qty").format(code), ctx, required=True)
		if qty is not None and qty <= 0:
			ctx.reject(_("Item {0}: qty must be greater than zero").format(code))
		received = {}
		for field in pricing.PRICE_FIELDS:
			received[field] = _number(raw.get(field), _("Item {0}: {1}").format(code, field), ctx)
		for field in (pricing.RATE, pricing.SELLING, pricing.SELLING_AFTER_TAX, pricing.PURCHASE_AFTER_TAX):
			if received[field] is not None and received[field] < 0:
				ctx.reject(_("Item {0}: {1} must not be negative").format(code, field))
		if received[pricing.MARKUP] is not None and received[pricing.MARKUP] < 0:
			ctx.reject(_("Item {0}: custom_markup_percentage must not be negative").format(code))

		extra = {}
		for field, cast in ITEM_OPTIONAL.items():
			if raw.get(field) not in (None, ""):
				extra[field] = cast(raw.get(field))
		rows.append(frappe._dict(idx=idx, item_code=code, qty=qty, received=received, extra=extra))
	return rows


def _filter_missing_items(ctx, rows):
	states = _item_states([r.item_code for r in rows])
	missing = [r.item_code for r in rows if not states[r.item_code]]
	disabled = [r.item_code for r in rows if states[r.item_code] and states[r.item_code].disabled]
	for code in disabled:
		ctx.reject(_("Item is disabled: {0}").format(code))

	if missing:
		if action("agreement_items_action") == IGNORE:
			ctx.warnings.append(_("Missing Items ignored (not created, left out of the agreement): {0}").format(", ".join(missing)))
			rows = [r for r in rows if r.item_code not in missing]
			if not rows:
				ctx.reject(_("None of the received Items exist; a Supplier Agreement needs at least one Item"))
		else:
			ctx.reject(_("Missing Items: {0}").format(", ".join(missing)))
	for r in rows:
		if states.get(r.item_code):
			r.item_code = states[r.item_code].name
	return rows


def _drivers(pricing_type, received):
	"""Inputs that drive the calculation for the pricing type; every other
	received price is compared against the calculated value."""
	if pricing_type == pricing.BUYING_BASIS:
		driving = [pricing.RATE]
		driving.append(pricing.MARKUP if received[pricing.MARKUP] is not None else pricing.SELLING)
	else:
		driving = [pricing.SELLING if received[pricing.SELLING] is not None else pricing.SELLING_AFTER_TAX]
		driving.append(pricing.MARKUP if received[pricing.MARKUP] is not None else pricing.RATE)
	return driving


def _calculate(ctx, pricing_type, rows):
	"""Run the shared calculation on every row and collect ALL differences."""
	taxes = _local_tax_rates([r.item_code for r in rows])
	differences = []
	for r in rows:
		received = r.received
		driving = _drivers(pricing_type, received)
		try:
			r.calculated = pricing.calculate_row(
				pricing_type,
				taxes[r.item_code],
				rate=received[pricing.RATE] if pricing.RATE in driving else None,
				markup=received[pricing.MARKUP] if pricing.MARKUP in driving else None,
				selling=received[pricing.SELLING] if pricing.SELLING in driving else None,
				selling_after_tax=received[pricing.SELLING_AFTER_TAX] if pricing.SELLING_AFTER_TAX in driving else None,
			)
		except pricing.PricingInputError as e:
			ctx.reject(_("Item {0}: {1}").format(r.item_code, e))
			r.calculated = None
			continue
		for field in pricing.PRICE_FIELDS:
			if field in driving or received[field] is None:
				continue
			if pricing.values_differ(field, received[field], r.calculated[field]):
				differences.append(
					{
						"item_code": r.item_code,
						"field": field,
						"label": pricing.LABELS[field],
						"received": flt(received[field], pricing.field_precision(field)),
						"calculated": flt(r.calculated[field], pricing.field_precision(field)),
					}
				)
	return differences


def _format_differences(differences):
	lines, current = [], None
	for d in differences:
		if d["item_code"] != current:
			current = d["item_code"]
			lines.append(f"Item {current}:")
		lines.append(
			f"  {d['label']}: received {pricing.display(d['field'], d['received'])}, "
			f"calculated {pricing.display(d['field'], d['calculated'])}"
		)
	return lines


def _warning_html(differences):
	parts = ["<p><strong>MCE API Pricing Warning:</strong></p>"]
	current = None
	for d in differences:
		if d["item_code"] != current:
			if current is not None:
				parts.append("</p>")
			current = d["item_code"]
			parts.append(f"<p>Item {frappe.utils.escape_html(current)}:<br>")
		parts.append(
			f"{d['label']} received from MCE: {pricing.display(d['field'], d['received'])} - "
			f"System calculated {d['label']}: {pricing.display(d['field'], d['calculated'])}<br>"
		)
	parts.append("</p><p>The system calculation was used.</p>")
	return "".join(parts)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def process(raw, ctx):
	order_no = clean(raw.get("order_no"))
	if not order_no:
		raise Rejected(_("order_no is required"))
	_check_duplicate(ctx, order_no)

	supplier = _resolve_supplier(ctx, clean(raw.get("supplier")))
	agreement_type = _resolve_agreement_type(ctx, clean(raw.get("custom_supplier_agreement_type")))
	pricing_type = _resolve_pricing_type(ctx, clean(raw.get("custom_pricing_type")))
	company = _resolve_company(ctx, clean(raw.get("company")))
	from_date = _parse_date(raw.get("from_date"), "from_date", ctx)
	to_date = _parse_date(raw.get("to_date"), "to_date", ctx)
	if from_date and to_date and from_date > to_date:
		ctx.reject(_("from_date {0} is after to_date {1}").format(from_date, to_date))
	order_date = _parse_date(raw.get("order_date"), "order_date", ctx) if clean(raw.get("order_date")) else None

	other_terms = _resolve_other_terms(ctx, raw.get("custom_other_terms"))
	_check_penalties(ctx, raw.get("custom_penalties"))

	rows = _parse_items(ctx, raw.get("items"))
	rows = _filter_missing_items(ctx, rows) if rows else rows

	differences = []
	if pricing_type and rows:
		differences = _calculate(ctx, pricing_type, rows)
	if differences:
		ctx.extra["pricing_differences"] = differences
		difference_action = get_settings().agreement_price_difference_action or "Stop"
		if difference_action not in (WARN, IGNORE):
			ctx.reject("Pricing calculation mismatch:\n" + "\n".join(_format_differences(differences)))
		else:
			ctx.warnings.append(
				_("Pricing differences ({0}) - system calculation used:").format(difference_action)
				+ "\n"
				+ "\n".join(_format_differences(differences))
			)

	ctx.apply_plans()  # raises Rejected with every reason collected above

	bo = frappe.new_doc("Blanket Order")
	bo.blanket_order_type = "Purchasing"
	bo.supplier = supplier
	bo.custom_title = clean(raw.get("custom_title")) or f"اتفاقية: {supplier}"  # same default as the form
	bo.custom_pricing_type = pricing_type
	bo.custom_supplier_agreement_type = ctx.value(agreement_type)
	bo.company = company
	bo.from_date = from_date
	bo.to_date = to_date
	bo.order_no = order_no
	if order_date:
		bo.order_date = order_date
	for field, cast in AGREEMENT_OPTIONAL.items():
		if raw.get(field) not in (None, ""):
			bo.set(field, cast(raw.get(field)))

	terms = raw.get("terms") if isinstance(raw.get("terms"), str) else ""
	if differences and get_settings().agreement_price_difference_action == WARN:
		# Received terms are kept as sent; the warning is appended below them.
		terms = (terms + "<br><br>" if terms else "") + _warning_html(differences)
	if terms:
		bo.terms = terms

	for term in other_terms:
		bo.append("custom_other_terms", {"tcs_terms": ctx.value(term["tcs_terms"]), "terms": term["terms"]})
	if other_terms:
		# A row sent without text gets the master's text, as the form does.
		for row in bo.custom_other_terms:
			if row.terms is None:
				row.terms = frappe.db.get_value("Terms and Conditions", row.tcs_terms, "terms")

	for r in rows:
		bo.append("items", {"item_code": r.item_code, "qty": r.qty, **r.calculated, **r.extra})

	bo.insert(ignore_permissions=True)
	return bo
