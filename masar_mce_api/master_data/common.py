import frappe
from frappe import _
from frappe.utils import flt

CREATE = "Create"
STOP = "Stop"


class Rejected(Exception):
	"""A business-rule rejection of one record. Raised before (or instead
	of) any write, carrying every reason found so the caller gets the full
	list of what is missing in one response."""

	def __init__(self, reasons):
		self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
		super().__init__(" | ".join(self.reasons))


def get_settings():
	"""API Settings, read once per request."""
	if not getattr(frappe.local, "mce_md_settings", None):
		frappe.local.mce_md_settings = frappe.get_single("API Settings")
	return frappe.local.mce_md_settings


def action(fieldname):
	return get_settings().get(fieldname) or STOP


def cache():
	if not hasattr(frappe.local, "mce_md_cache"):
		frappe.local.mce_md_cache = {}
	return frappe.local.mce_md_cache


def reset_cache():
	"""Called at the start of every request and whenever a record is rolled
	back, so a master created (then rolled back) by a failed record is never
	remembered as existing by the next one."""
	frappe.local.mce_md_cache = {}
	frappe.local.mce_md_settings = None
	if hasattr(frappe.local, "mce_lookup_cache"):
		del frappe.local.mce_lookup_cache


def cached_exists(doctype, name):
	"""Returns the stored name (case as saved in the DB) or None."""
	key = ("exists", doctype, name)
	store = cache()
	if key not in store:
		store[key] = frappe.db.exists(doctype, name) or None
	return store[key]


def remember(doctype, name):
	cache()[("exists", doctype, name)] = name


def clean(value):
	"""Normalise a scalar from JSON to a stripped string (numbers allowed,
	e.g. a code sent as 123), or None when blank."""
	if value is None or isinstance(value, (dict, list)):
		return None
	if isinstance(value, float) and value.is_integer():
		value = int(value)
	value = str(value).strip()
	return value or None


def parse_rate(raw, label, reasons):
	"""Optional tax rate in percent. Returns flt or None (not sent)."""
	if raw is None or (isinstance(raw, str) and not raw.strip()):
		return None
	try:
		rate = float(raw)
	except (TypeError, ValueError):
		reasons.append(_("{0} is invalid: {1}").format(label, raw))
		return None
	if rate < 0 or rate > 100:
		reasons.append(_("{0} must be between 0 and 100: {1}").format(label, raw))
		return None
	return flt(rate, 4)


class RecordContext:
	"""Collects, for one record, the rejection reasons and the master data to
	create. Nothing is written until every dependency has been resolved and
	no reason was found (apply_plans), so a Stop rejection never leaves a
	partially created set of masters behind."""

	def __init__(self):
		self.reasons = []
		self.plans = {}  # key -> (label, fn)
		self.created = {}  # key -> name returned by fn
		self.auto_created = []
		self.warnings = []  # data received but not used, kept on the record
		self.supplier = None  # Supplier the record belongs to, for the record
		self.extra = {}  # API-specific keys added to this record's response

	def reject(self, reason):
		self.reasons.append(reason)

	def plan(self, key, label, fn):
		"""Schedule a create; the same key planned twice (e.g. one country
		used by both the Supplier and its Address) is created once."""
		if key not in self.plans:
			self.plans[key] = (label, fn)
		return Planned(key)

	def raise_if_rejected(self):
		if self.reasons:
			raise Rejected(self.reasons)

	def apply_plans(self):
		self.raise_if_rejected()
		for key, (label, fn) in self.plans.items():
			name = fn()
			self.created[key] = name
			self.auto_created.append(f"{label}: {name}")

	def value(self, resolved):
		if isinstance(resolved, Planned):
			return self.created[resolved.key]
		return resolved


class Planned:
	__slots__ = ("key",)

	def __init__(self, key):
		self.key = key
