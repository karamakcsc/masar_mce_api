# Copyright (c) 2026, KCSC and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from masar_mce_api.utils import get_active_supplier_agreement, resolve_purchase_order_for_agreement_item
from masar_mce_api.validators import run_validation


def _auto_submit_enabled():
	return bool(frappe.db.get_single_value("API Settings", "auto_submit_created_documents"))


class MCEInventoryTransaction(Document):
	def validate(self):
		# Business-rule failures must never raise here - a Rejected transaction
		# still has to be saved (see process_inventory_transaction). Only a
		# genuinely broken document (framework-level) should raise.
		run_validation(self)

	def before_submit(self):
		if self.validation_status != "Accepted":
			frappe.throw(_("Only Accepted transactions can be submitted."))

	def on_submit(self):
		if self.transaction_type == "G1G":
			self._create_purchase_receipt()
		else:
			self._create_stock_entry()
		self.db_set("processed_on", now_datetime(), update_modified=False)
		self.db_set("processed_by", frappe.session.user, update_modified=False)

	def _create_stock_entry(self):
		"""F1 / G1A / H1 / H3: a سند تدوير (Material Transfer) Stock Entry
		between the two already-validated warehouses. custom_viewed_by_warehouse
		is only set when we are about to auto-submit - masar_mce's own Stock
		Entry.on_submit hook otherwise blocks this stock_entry_type, but a
		Draft left for manual review should still go through the normal
		warehouse-manager approval step."""
		company = frappe.db.get_value("Warehouse", self.source_warehouse, "company")
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "سند تدوير"
		se.purpose = "Material Transfer"
		se.company = company
		se.posting_date = self.posting_date
		se.posting_time = self.posting_time
		se.set_posting_time = 1
		for row in self.items:
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": row.qty,
					"basic_rate": row.rate,
					"s_warehouse": self.source_warehouse,
					"t_warehouse": self.target_warehouse,
				},
			)
		if _auto_submit_enabled():
			se.custom_viewed_by_warehouse = 1
		se.insert(ignore_permissions=True)
		if _auto_submit_enabled():
			se.submit()
		self.db_set("created_document_type", "Stock Entry", update_modified=False)
		self.db_set("created_document_name", se.name, update_modified=False)

	def _create_purchase_receipt(self):
		"""G1G: receipt of goods from a Supplier into target_warehouse, with no
		source warehouse. Each row must be linked to a real open Purchase Order
		Item (via the Item's active Supplier Agreement) so masar_mce's own
		Purchase Receipt.before_insert::set_purchase_order_rate() picks up the
		agreed rate instead of zeroing it."""
		company = frappe.db.get_value("Warehouse", self.target_warehouse, "company")
		pr = frappe.new_doc("Purchase Receipt")
		pr.supplier = self.supplier
		pr.company = company
		pr.posting_date = self.posting_date
		pr.posting_time = self.posting_time
		pr.set_posting_time = 1
		pr.custom_request_date = self.posting_date
		pr.set_warehouse = self.target_warehouse
		for row in self.items:
			agreement = get_active_supplier_agreement(row.item_code)
			po_item = resolve_purchase_order_for_agreement_item(row.item_code, agreement, self.supplier)
			if not po_item:
				frappe.throw(
					_("No open Purchase Order found for item {0} under the Supplier Agreement at submit time").format(
						row.item_code
					)
				)
			pr.append(
				"items",
				{
					"item_code": row.item_code,
					"received_qty": row.qty,
					"qty": row.qty,
					"rate": row.rate,
					"warehouse": self.target_warehouse,
					"purchase_order": po_item.purchase_order,
					"purchase_order_item": po_item.purchase_order_item,
				},
			)
		pr.insert(ignore_permissions=True)
		if _auto_submit_enabled():
			pr.submit()
		self.db_set("created_document_type", "Purchase Receipt", update_modified=False)
		self.db_set("created_document_name", pr.name, update_modified=False)
