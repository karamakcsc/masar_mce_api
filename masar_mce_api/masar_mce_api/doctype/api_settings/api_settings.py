# Copyright (c) 2026, KCSC and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document


class APISettings(Document):
	def validate(self):
		if self.headers:
			try:
				json.loads(self.headers)
			except ValueError:
				frappe.throw(_("Headers must be a valid JSON object"))
