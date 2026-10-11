"""Supplier receive API record processing.

Payload per record:
	supplier_code (-> Supplier.name via masar_mce's autoname), supplier_name,
	country, supplier_group, custom_national_id, tax_id, custom_territory,
	territory_days (optional, else API Settings default),
	supplier_primary_address: {address_line1, address_line2, city, country}
		or the name of an existing Address,
	supplier_primary_contact: {mobile_no, email_id}
		or the name of an existing Contact

An existing supplier_code is rejected, never updated.
"""

import json

import frappe
from frappe import _
from frappe.contacts.doctype.address.address import get_address_display
from frappe.utils import cint, validate_email_address

from masar_mce_api.master_data import resolvers
from masar_mce_api.master_data.common import (
	CREATE,
	Planned,
	Rejected,
	action,
	cached_exists,
	clean,
	get_settings,
)
from masar_mce_api.utils import resolve_supplier_by_code


def create_minimal_supplier(code, supplier_name):
	"""Used by the Item API when its Missing Supplier setting is Create:
	only code + name are known, so the Supplier's other mandatory fields
	(country, tax ID, national ID, address, mobile) are deliberately
	skipped. The Supplier must be completed manually in ERPNext."""
	settings = get_settings()
	doc = frappe.new_doc("Supplier")
	doc.supplier_name = supplier_name
	doc.custom_supplier_code = code
	doc.supplier_type = settings.default_supplier_type or "Company"
	doc.supplier_group = settings.minimal_supplier_group
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	resolvers.remember_supplier(code, doc.name)
	return doc.name


def _object(raw, label, reasons):
	"""A nested object may arrive as a dict or as a JSON string; any other
	non-empty string is treated as the name of an existing record."""
	if isinstance(raw, str):
		stripped = raw.strip()
		if stripped.startswith("{"):
			try:
				raw = json.loads(stripped)
			except ValueError:
				reasons.append(_("{0} must be valid JSON").format(label))
				return None, None
		else:
			return None, stripped or None
	if isinstance(raw, dict):
		return raw, None
	if raw not in (None, ""):
		reasons.append(_("{0} must be an object").format(label))
	return None, None


def _required_id(ctx, raw, field, setting, label):
	"""National ID / Tax ID are mandatory on Supplier (masar_mce) and are
	never generated, so a missing value is rejected under either setting;
	the setting only changes the wording."""
	value = clean(raw.get(field))
	if value:
		return value
	if action(setting) == CREATE:
		ctx.reject(_("{0} is missing and cannot be generated ({1} is mandatory on Supplier)").format(field, label))
	else:
		ctx.reject(_("{0} is required").format(field))
	return None


def _resolve_address(ctx, raw, supplier_country):
	data, existing = _object(raw.get("supplier_primary_address"), "supplier_primary_address", ctx.reasons)
	if existing:
		name = cached_exists("Address", existing)
		if not name:
			ctx.reject(_("Primary Address does not exist: {0}").format(existing))
			return None
		_check_country_match(ctx, frappe.db.get_value("Address", name, "country"), supplier_country)
		return ("existing", name)
	if data is None:
		ctx.reject(_("supplier_primary_address is required"))
		return None
	if action("supplier_address_action") != CREATE:
		ctx.reject(_("Primary Address does not exist and creating it is disabled (Primary Address = Stop)"))
		return None

	address = {f: clean(data.get(f)) for f in ("address_line1", "address_line2", "city", "country")}
	for f in ("address_line1", "city", "country"):
		if not address[f]:
			ctx.reject(_("supplier_primary_address.{0} is required").format(f))
	if address["country"]:
		# name, Planned (same plan as the Supplier's country when equal) or None
		address["country"] = resolvers.resolve_country(ctx, address["country"])
		_check_country_match(ctx, address["country"], supplier_country)
	return ("new", address)


def _country_label(value):
	return value.key[1] if isinstance(value, Planned) else value


def _check_country_match(ctx, address_country, supplier_country):
	"""Never silently replace one country with the other."""
	if not address_country or not supplier_country:
		return
	if _country_label(address_country).lower() != _country_label(supplier_country).lower():
		ctx.reject(
			_("Address country {0} does not match Supplier country {1}").format(
				_country_label(address_country), _country_label(supplier_country)
			)
		)


def _resolve_contact(ctx, raw):
	data, existing = _object(raw.get("supplier_primary_contact"), "supplier_primary_contact", ctx.reasons)
	if existing:
		name = cached_exists("Contact", existing)
		if not name:
			ctx.reject(_("Primary Contact does not exist: {0}").format(existing))
			return None
		if not frappe.db.get_value("Contact", name, "mobile_no"):
			ctx.reject(_("Primary Contact {0} has no Mobile No (mandatory on Supplier)").format(name))
		return ("existing", name)
	if data is None:
		ctx.reject(_("supplier_primary_contact is required (Mobile No is mandatory on Supplier)"))
		return None
	if action("supplier_contact_action") != CREATE:
		ctx.reject(_("Primary Contact does not exist and creating it is disabled (Primary Contact = Stop)"))
		return None

	contact = {"mobile_no": clean(data.get("mobile_no")), "email_id": clean(data.get("email_id"))}
	if not contact["mobile_no"]:
		ctx.reject(_("supplier_primary_contact.mobile_no is required"))
	if contact["email_id"] and not validate_email_address(contact["email_id"]):
		ctx.reject(_("supplier_primary_contact.email_id is not a valid email: {0}").format(contact["email_id"]))
	return ("new", contact)


def _link(doc, supplier_name):
	if not any(l.link_doctype == "Supplier" and l.link_name == supplier_name for l in doc.links):
		doc.append("links", {"link_doctype": "Supplier", "link_name": supplier_name})


def process(raw, ctx):
	code = clean(raw.get("supplier_code"))
	if not code:
		raise Rejected(_("supplier_code is required"))
	if resolve_supplier_by_code(code):
		raise Rejected(_("Supplier already exists: {0}").format(code))

	supplier_name = clean(raw.get("supplier_name"))
	if not supplier_name:
		ctx.reject(_("supplier_name is required"))

	country = clean(raw.get("country"))
	if not country:
		ctx.reject(_("country is required"))
	else:
		country = resolvers.resolve_country(ctx, country)

	supplier_group = clean(raw.get("supplier_group"))
	if supplier_group:
		supplier_group = resolvers.resolve_supplier_group(ctx, supplier_group)

	national_id = _required_id(ctx, raw, "custom_national_id", "supplier_national_id_action", _("National ID"))
	tax_id = _required_id(ctx, raw, "tax_id", "supplier_tax_id_action", _("Tax ID"))

	address = _resolve_address(ctx, raw, country)
	contact = _resolve_contact(ctx, raw)

	territory = clean(raw.get("custom_territory"))
	territory_days = None
	if territory:
		territory = resolvers.resolve_territory(ctx, territory)
		days_raw = raw.get("territory_days")
		territory_days = cint(days_raw) if clean(days_raw) else cint(get_settings().default_territory_days)
		if territory_days <= 0:
			ctx.reject(
				_(
					"territory_days must be a positive number (or set Default Territory Number of Days in API Settings)"
				)
			)

	ctx.apply_plans()  # raises Rejected with every reason collected above

	country = ctx.value(country)

	# supplier_primary_address / mobile_no are mandatory at Supplier insert,
	# so ERPNext's own on_update creation would run too late: the Address and
	# Contact are inserted first, unlinked (Frappe loads the linked party when
	# saving a link), and linked to the Supplier right after it exists. All of
	# it runs inside the record's savepoint.
	address_doc = _get_address(address, supplier_name, ctx)
	contact_doc = _get_contact(contact, supplier_name)

	supplier = frappe.new_doc("Supplier")
	supplier.supplier_name = supplier_name
	supplier.custom_supplier_code = code
	supplier.country = country
	supplier.supplier_group = ctx.value(supplier_group) if supplier_group else None
	supplier.supplier_type = get_settings().default_supplier_type or "Company"
	supplier.custom_national_id = national_id
	supplier.tax_id = tax_id
	supplier.supplier_primary_address = address_doc.name
	supplier.primary_address = get_address_display(address_doc.as_dict())
	supplier.supplier_primary_contact = contact_doc.name
	if territory:
		supplier.append("custom_territories", {"territory": ctx.value(territory), "number_of_days": territory_days})
	supplier.insert(ignore_permissions=True)

	if supplier.name != code:
		# autoname is owned by masar_mce; the links above assume name == code.
		frappe.throw(_("Supplier was named {0} instead of supplier_code {1}").format(supplier.name, code))

	for doc in (address_doc, contact_doc):
		_link(doc, supplier.name)
		doc.save(ignore_permissions=True)

	resolvers.remember_supplier(code, supplier.name)
	ctx.supplier = supplier.name
	return supplier


def _get_address(address, supplier_name, ctx):
	kind, value = address
	if kind == "existing":
		doc = frappe.get_doc("Address", value)
	else:
		value = {**value, "country": ctx.value(value["country"])}
		doc = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": supplier_name,
				"address_type": "Billing",
				"is_primary_address": 1,
				**value,
			}
		).insert(ignore_permissions=True)
	return doc


def _get_contact(contact, supplier_name):
	kind, value = contact
	if kind == "existing":
		doc = frappe.get_doc("Contact", value)
	else:
		doc = frappe.get_doc({"doctype": "Contact", "first_name": supplier_name, "is_primary_contact": 1})
		doc.add_phone(value["mobile_no"], is_primary_mobile_no=True)
		if value["email_id"]:
			doc.add_email(value["email_id"], is_primary=True)
		doc.insert(ignore_permissions=True)
	return doc
