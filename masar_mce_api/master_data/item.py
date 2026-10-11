"""Item receive API record processing.

Payload per record:
	item_code, item_name, description, is_stock_item, item_group, stock_uom,
	barcode, supplier (supplier code), supplier_name (only used to create a
	minimal Supplier), local_zone_tax_rate, free_zone_tax_rate

An existing item_code is rejected, never updated.
"""

import frappe
from frappe import _

from masar_mce_api.master_data import resolvers
from masar_mce_api.master_data.common import (
	CREATE,
	Rejected,
	action,
	cache,
	cached_exists,
	clean,
	parse_rate,
	remember,
)

LOCAL_ZONE = "Local Zone"
FREE_ZONE = "Free Zone"

TAX_FIELDS = (
	(LOCAL_ZONE, "local_zone_tax_rate", "item_local_zone_tax_action"),
	(FREE_ZONE, "free_zone_tax_rate", "item_free_zone_tax_action"),
)


def _parse_check(raw, label, reasons):
	if isinstance(raw, bool):
		return int(raw)
	value = clean(raw)
	if value is None:
		reasons.append(_("{0} is required").format(label))
		return None
	if value.lower() in ("1", "true", "yes"):
		return 1
	if value.lower() in ("0", "false", "no"):
		return 0
	reasons.append(_("{0} must be 0 or 1: {1}").format(label, raw))
	return None


def process(raw, ctx):
	item_code = clean(raw.get("item_code"))
	if not item_code:
		raise Rejected(_("item_code is required"))
	if cached_exists("Item", item_code):
		raise Rejected(_("Item already exists: {0}").format(item_code))

	item_name = clean(raw.get("item_name"))
	if not item_name:
		ctx.reject(_("item_name is required"))
	description = raw.get("description") if isinstance(raw.get("description"), str) else None
	is_stock_item = _parse_check(raw.get("is_stock_item"), "is_stock_item", ctx.reasons)

	item_group = clean(raw.get("item_group"))
	if not item_group:
		ctx.reject(_("item_group is required"))
	else:
		item_group = resolvers.resolve_item_group(ctx, item_group)

	stock_uom = clean(raw.get("stock_uom"))
	if not stock_uom:
		ctx.reject(_("stock_uom is required"))
	else:
		stock_uom = resolvers.resolve_uom(ctx, stock_uom)

	barcode = clean(raw.get("barcode"))
	if not barcode:
		if action("item_barcode_action") != CREATE:
			ctx.reject(_("barcode is required"))
	else:
		owner = resolvers.barcode_owner(barcode)
		if owner:
			ctx.reject(_("Barcode {0} already belongs to Item {1}").format(barcode, owner))

	supplier = clean(raw.get("supplier"))
	if supplier:
		supplier = resolvers.resolve_item_supplier(ctx, supplier, clean(raw.get("supplier_name")))

	taxes = []
	for tax_category, field, setting in TAX_FIELDS:
		rate = parse_rate(raw.get(field), field, ctx.reasons)
		if rate is None:
			continue
		template = resolvers.resolve_tax_template(ctx, tax_category, rate, setting)
		if template:
			taxes.append((tax_category, template))

	ctx.apply_plans()  # raises Rejected with every reason collected above

	item = frappe.new_doc("Item")
	item.item_code = item_code
	item.item_name = item_name
	item.is_stock_item = is_stock_item
	item.item_group = ctx.value(item_group)
	item.stock_uom = ctx.value(stock_uom)
	if description:
		item.description = description
	if barcode:
		item.append("barcodes", {"barcode": barcode})
	if supplier:
		item.append("supplier_items", {"supplier": ctx.value(supplier)})
	for tax_category, template in taxes:
		item.append("taxes", {"item_tax_template": ctx.value(template), "tax_category": tax_category})
	item.insert(ignore_permissions=True)

	remember("Item", item.name)
	if barcode:
		cache()[("barcode", barcode)] = item.name
	return item
