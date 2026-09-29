import frappe
from masar_mce_api.utils import get_item_barcode, get_tax_for_item, get_item_price
from masar_mce_api.api import insert_pos_item

def on_update(self, method):
    if self.is_fixed_asset == 1:
        return
    update_pos_item(self)


def update_pos_item(self):
    active_sa = None
    if self.custom_latest_sa:
        active_sa = self.custom_latest_sa
    else:
        existing_sp = frappe.db.sql("""
            SELECT tbo.name
            FROM `tabBlanket Order Item` tboi
            INNER JOIN `tabBlanket Order` tbo ON tboi.parent = tbo.name
            WHERE tboi.item_code = %s
              AND tbo.docstatus = 1
              AND tbo.custom_status = 'Active'
            GROUP BY tbo.name
        """, (self.name,), as_dict=True)

        if existing_sp:
            active_sa = existing_sp[0].name

    if not active_sa:
        return

    sa_doc = frappe.get_doc("Blanket Order", active_sa)

    supplier_code = frappe.db.get_value(
        "Supplier",
        sa_doc.supplier,
        "custom_supplier_code"
    )

    local_zone_tax = get_tax_for_item(self.name, "Local Zone")
    free_zone_tax = get_tax_for_item(self.name, "Free Zone")

    selling_local_zone, selling_free_zone = get_item_price(self.name)

    item_stop = 1 if (self.disabled == 1 or self.is_sales_item == 0) else 0

    payload_local_zone = {
        "AGREEMENT_NO": active_sa,
        "COMP_CODE": supplier_code,
        "AGR_STDATE": sa_doc.from_date,
        "AGR_ENDATE": sa_doc.to_date,
        "ITEMS": [
            {
                "ITEMNO": self.name,
                "BARCODE": get_item_barcode(self.name),
                "ITEMSHORTNAME": self.item_name,
                "ITEMTAX": local_zone_tax * 100,
                "ITEMPRICE": selling_local_zone,
                "ITEMSTOP": item_stop,
                "TRN_TYPE_PRICE": 1
            }
        ]
    }

    payload_free_zone = {
        "AGREEMENT_NO": active_sa,
        "COMP_CODE": supplier_code,
        "AGR_STDATE": sa_doc.from_date,
        "AGR_ENDATE": sa_doc.to_date,
        "ITEMS": [
            {
                "ITEMNO": self.name,
                "BARCODE": get_item_barcode(self.name),
                "ITEMSHORTNAME": self.item_name,
                "ITEMTAX": free_zone_tax * 100,
                "ITEMPRICE": selling_free_zone,
                "ITEMSTOP": item_stop,
                "TRN_TYPE_PRICE": 8
            }
        ]
    }

    insert_pos_item(payload_local_zone, payload_free_zone)
