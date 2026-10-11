"""Supplier Agreement (Blanket Order Item) pricing - the single Python
implementation of the calculation done by masar_mce's
custom/blanket_order/blanket_order.js field handlers.

Until this module existed the buying / selling / markup relationship lived
only in that client script; the server (masar_mce blanket_order.validate ->
calculate_amounts_and_total) only recomputes the after-tax prices and totals.
Any server-side caller (the Supplier Agreement receive API today, the form
later) must use calculate_row() so both always produce the same numbers.

t = the Item's Local Zone tax as a fraction (0.16), the same rate the form
(masar_mce.utils.get_tax_for_item) and calculate_amounts_and_total use.

Buying Price Basis  (inputs: rate + markup, or rate + selling price)
	purchase_after_tax = rate * (1 + t)
	selling            = purchase_after_tax * (1 + markup / 100)
	selling_after_tax  = selling * (1 + t)
	markup (when not given) = (selling - purchase_after_tax) / purchase_after_tax * 100

Selling Price Basis (inputs: selling + markup, or selling + rate)
	selling            = selling_after_tax / (1 + t)   when only selling_after_tax is given
	purchase_after_tax = selling - selling * markup / 100      ("markdown")
	rate               = purchase_after_tax / (1 + t)
	selling_after_tax  = selling * (1 + t)
	markup (when not given) = (selling - purchase_after_tax) / purchase_after_tax * 100

The Selling basis markdown form is the one used by the form's
custom_markup_percentage / custom_selling_price handlers, by the stored
agreements and by the Pricing Sheet's server calculation. (The form's
custom_pricing_type change handler divides by (1 + markup/100) instead - a
known inconsistency of that one handler, not followed here.)

Values are returned unrounded, as the form stores them; compare them with
values_differ(), which rounds both sides to the Blanket Order Item field's
precision using the system rounding method.
"""

import frappe
from frappe import _
from frappe.utils import flt

BUYING_BASIS = "Buying Price Basis"
SELLING_BASIS = "Selling Price Basis"

# Blanket Order Item fields the calculation reads/writes.
RATE = "rate"
PURCHASE_AFTER_TAX = "custom_purchase_price_after_tax"
MARKUP = "custom_markup_percentage"
SELLING = "custom_selling_price"
SELLING_AFTER_TAX = "custom_selling_price_after_tax"

PRICE_FIELDS = (RATE, PURCHASE_AFTER_TAX, MARKUP, SELLING, SELLING_AFTER_TAX)

LABELS = {
	RATE: "Buying Price",
	PURCHASE_AFTER_TAX: "Buying Price After Tax",
	MARKUP: "Markup %",
	SELLING: "Selling Price",
	SELLING_AFTER_TAX: "Selling Price After Tax",
}


class PricingInputError(ValueError):
	pass


def _markup_from(selling, purchase_after_tax):
	if not flt(purchase_after_tax):
		raise PricingInputError(_("markup cannot be derived: Buying Price After Tax is 0"))
	return (flt(selling) - flt(purchase_after_tax)) / flt(purchase_after_tax) * 100


def calculate_row(pricing_type, tax_rate, rate=None, markup=None, selling=None, selling_after_tax=None):
	"""Return {field: value} for PRICE_FIELDS from the inputs that drive the
	given pricing type. tax_rate is a fraction. Inputs not used by the
	pricing type are ignored (the caller compares them against the result).
	Raises PricingInputError when the driving inputs are missing."""
	t = flt(tax_rate)

	if pricing_type == BUYING_BASIS:
		if rate is None:
			raise PricingInputError(_("rate (Buying Price) is required for {0}").format(BUYING_BASIS))
		purchase_after_tax = flt(rate) * (1 + t)
		if markup is not None:
			selling = purchase_after_tax * (1 + flt(markup) / 100)
		elif selling is not None:
			markup = _markup_from(selling, purchase_after_tax)
		else:
			raise PricingInputError(
				_("custom_markup_percentage or custom_selling_price is required for {0}").format(BUYING_BASIS)
			)
		return {
			RATE: flt(rate),
			PURCHASE_AFTER_TAX: purchase_after_tax,
			MARKUP: flt(markup),
			SELLING: flt(selling),
			SELLING_AFTER_TAX: flt(selling) * (1 + t),
		}

	if pricing_type == SELLING_BASIS:
		if selling is None:
			if selling_after_tax is None:
				raise PricingInputError(
					_("custom_selling_price or custom_selling_price_after_tax is required for {0}").format(
						SELLING_BASIS
					)
				)
			selling = flt(selling_after_tax) / (1 + t)
		if markup is not None:
			purchase_after_tax = flt(selling) - flt(selling) * flt(markup) / 100
		elif rate is not None:
			purchase_after_tax = flt(rate) * (1 + t)
			markup = _markup_from(selling, purchase_after_tax)
		else:
			raise PricingInputError(
				_("custom_markup_percentage or rate is required for {0}").format(SELLING_BASIS)
			)
		return {
			RATE: purchase_after_tax / (1 + t),
			PURCHASE_AFTER_TAX: purchase_after_tax,
			MARKUP: flt(markup),
			SELLING: flt(selling),
			SELLING_AFTER_TAX: flt(selling) * (1 + t),
		}

	raise PricingInputError(_("Unsupported pricing type: {0}").format(pricing_type))


def field_precision(fieldname):
	"""Display precision of a Blanket Order Item field (what the user sees)."""
	if not hasattr(frappe.local, "mce_bo_item_precision"):
		frappe.local.mce_bo_item_precision = {}
	cache = frappe.local.mce_bo_item_precision
	if fieldname not in cache:
		cache[fieldname] = frappe.new_doc("Blanket Order Item").precision(fieldname) or 3
	return cache[fieldname]


def values_differ(fieldname, received, calculated):
	precision = field_precision(fieldname)
	return flt(received, precision) != flt(calculated, precision)


def display(fieldname, value):
	value = flt(value, field_precision(fieldname))
	text = f"{value:.{field_precision(fieldname)}f}".rstrip("0").rstrip(".")
	return f"{text}%" if fieldname == MARKUP else text
