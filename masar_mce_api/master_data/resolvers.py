"""Create/Stop resolution of master-data dependencies.

Each resolver returns the existing record's name, or a Planned create when
the setting is Create, or records a rejection reason on ctx when it is Stop.
Nothing is written here; ctx.apply_plans() runs the creates once the whole
record has been validated.
"""

import frappe
from frappe import _
from frappe.utils import flt

from masar_mce_api.master_data.common import CREATE, action, cache, cached_exists, get_settings, remember
from masar_mce_api.utils import _lookup_cache, resolve_supplier_by_code


def _resolve_simple(ctx, doctype, value, setting, label, create_fn):
	existing = cached_exists(doctype, value)
	if existing:
		return existing
	if action(setting) == CREATE:
		return ctx.plan((doctype, value), doctype, create_fn)
	ctx.reject(_("{0} does not exist: {1}").format(label, value))
	return None


def _insert(doc, doctype):
	doc.insert(ignore_permissions=True)
	remember(doctype, doc.name)
	return doc.name


# ---------------------------------------------------------------------------
# Item dependencies
# ---------------------------------------------------------------------------

def resolve_item_group(ctx, value):
	def create():
		return _insert(
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": value,
					"parent_item_group": get_settings().default_parent_item_group or "All Item Groups",
					"is_group": 0,
				}
			),
			"Item Group",
		)

	return _resolve_simple(ctx, "Item Group", value, "item_group_action", _("Item Group"), create)


def resolve_uom(ctx, value):
	def create():
		return _insert(frappe.get_doc({"doctype": "UOM", "uom_name": value}), "UOM")

	return _resolve_simple(ctx, "UOM", value, "item_stock_uom_action", _("Stock UOM"), create)


def barcode_owner(barcode):
	key = ("barcode", barcode)
	store = cache()
	if key not in store:
		store[key] = frappe.db.get_value("Item Barcode", {"barcode": barcode, "parenttype": "Item"}, "parent")
	return store[key]


def resolve_item_supplier(ctx, code, supplier_name):
	"""Supplier on an Item. Create builds a minimal Supplier (code + name
	only, mandatory fields skipped) - see supplier.create_minimal_supplier."""
	existing = resolve_supplier_by_code(code)
	if existing:
		return existing
	if action("item_supplier_action") != CREATE:
		ctx.reject(_("Supplier does not exist: {0}").format(code))
		return None
	if not supplier_name:
		ctx.reject(_("supplier_name is required to create missing Supplier {0}").format(code))
		return None

	from masar_mce_api.master_data.supplier import create_minimal_supplier

	return ctx.plan(("Supplier", code), _("Supplier (minimal)"), lambda: create_minimal_supplier(code, supplier_name))


# ---------------------------------------------------------------------------
# Item Tax Template by (Tax Category, rate)
# ---------------------------------------------------------------------------

def _tax_templates():
	"""{template: {"company", "rates": set}} for enabled templates, loaded
	once per request. The rate lives in Item Tax Template Detail.tax_rate;
	templates carry no Tax Category (that is set on the Item's tax row)."""
	store = cache()
	if "tax_templates" not in store:
		templates = {}
		rows = frappe.db.sql(
			"""
			SELECT t.name, t.company, d.tax_rate
			FROM `tabItem Tax Template` t
			INNER JOIN `tabItem Tax Template Detail` d
				ON d.parent = t.name AND d.parenttype = 'Item Tax Template'
			WHERE t.disabled = 0
			""",
			as_dict=True,
		)
		for row in rows:
			entry = templates.setdefault(row.name, {"company": row.company, "rates": set()})
			entry["rates"].add(flt(row.tax_rate, 4))
		store["tax_templates"] = templates
	return store["tax_templates"]


def _tax_mapping():
	store = cache()
	if "tax_mapping" not in store:
		store["tax_mapping"] = {
			(row.tax_category, flt(row.tax_rate, 4)): row.item_tax_template
			for row in get_settings().get("tax_rate_mapping") or []
		}
	return store["tax_mapping"]


def _template_title(rate):
	return f"ضريبة {rate:g}%"


def resolve_tax_template(ctx, tax_category, rate, setting):
	"""1. API Settings > Tax Rate Mapping for (category, rate)
	2. the single enabled template whose rate(s) are exactly {rate}
	3. several matches -> rejected as ambiguous (map it in settings)
	4. none -> Create / Stop"""
	mapped = _tax_mapping().get((tax_category, rate))
	if mapped:
		return mapped

	settings = get_settings()
	company = settings.tax_template_company
	matches = sorted(
		name
		for name, t in _tax_templates().items()
		if t["rates"] == {rate} and (not company or t["company"] == company)
	)
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		ctx.reject(
			_(
				"{0} tax rate {1} matches several Item Tax Templates ({2}); map it in API Settings > Tax Rate Mapping"
			).format(tax_category, f"{rate:g}", ", ".join(matches))
		)
		return None

	if action(setting) != CREATE:
		ctx.reject(_("{0} tax template for rate {1} does not exist.").format(tax_category, f"{rate:g}"))
		return None
	if not company or not settings.tax_template_account:
		ctx.reject(
			_(
				"{0} tax template for rate {1} does not exist and cannot be created: set Tax Template Company and Tax Template Account in API Settings"
			).format(tax_category, f"{rate:g}")
		)
		return None

	title = _template_title(rate)
	expected_name = f"{title} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if cached_exists("Item Tax Template", expected_name):
		ctx.reject(
			_(
				"{0} tax template for rate {1} cannot be created: Item Tax Template {2} already exists with a different rate or is disabled"
			).format(tax_category, f"{rate:g}", expected_name)
		)
		return None

	def create():
		doc = frappe.get_doc(
			{
				"doctype": "Item Tax Template",
				"title": title,
				"company": company,
				"taxes": [{"tax_type": settings.tax_template_account, "tax_rate": rate}],
			}
		)
		doc.insert(ignore_permissions=True)
		_tax_templates()[doc.name] = {"company": company, "rates": {rate}}
		return doc.name

	# Keyed by rate, not category: a 3% Local and 3% Free share one template.
	return ctx.plan(("Item Tax Template", rate), _("Item Tax Template"), create)


# ---------------------------------------------------------------------------
# Supplier dependencies
# ---------------------------------------------------------------------------

def resolve_country(ctx, value):
	def create():
		return _insert(frappe.get_doc({"doctype": "Country", "country_name": value}), "Country")

	return _resolve_simple(ctx, "Country", value, "supplier_country_action", _("Country"), create)


def resolve_supplier_group(ctx, value):
	def create():
		return _insert(
			frappe.get_doc(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": value,
					"parent_supplier_group": get_settings().default_parent_supplier_group or "All Supplier Groups",
				}
			),
			"Supplier Group",
		)

	return _resolve_simple(ctx, "Supplier Group", value, "supplier_group_action", _("Supplier Group"), create)


def resolve_territory(ctx, value):
	def create():
		return _insert(
			frappe.get_doc(
				{
					"doctype": "Territory",
					"territory_name": value,
					"parent_territory": get_settings().default_parent_territory or "All Territories",
					"is_group": 0,
				}
			),
			"Territory",
		)

	return _resolve_simple(ctx, "Territory", value, "supplier_territory_action", _("Territory"), create)


def remember_supplier(code, name):
	_lookup_cache()["supplier"][code] = name
