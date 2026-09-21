import frappe
from frappe import _ , db , get_doc
from masar_mce_api.utils import get_item_barcode
from masar_mce_api.api import insert_pos_item

def on_submit(self , method): 
    create_pos_item(self)
    
def create_pos_item(self):
    sa_doc = get_doc("Blanket Order", self.blanket_order)
    if sa_doc.docstatus == 1 and sa_doc.custom_status == "Active":
        items_local_zone = []
        items_free_zone = []
        supplier_code = db.get_value("Supplier", self.supplier, "custom_supplier_code")
        for item in self.items:
            is_disabled = db.get_value("Item", item.item_code, "disabled")
            is_disabled_sales = db.get_value("Item", item.item_code, "is_sales_item")
            if is_disabled == 1 or is_disabled_sales == 0:
                is_disabled = 1
            else:
                is_disabled = 0
            items_local_zone.append({
                "ITEMNO": item.item_code,
                "BARCODE": get_item_barcode(item.item_code),
                "ITEMSHORTNAME": item.item_name,
                "ITEMTAX":item.local_tax_rate,
                "ITEMPRICE": item.local_sp,
                "ITEMSTOP": is_disabled,
                "TRN_TYPE_PRICE": 1
            })
            items_free_zone.append({
                "ITEMNO": item.item_code,
                "BARCODE": get_item_barcode(item.item_code),
                "ITEMSHORTNAME": item.item_name,
                "ITEMTAX":item.free_tax_rate,
                "ITEMPRICE": item.free_sp,
                "ITEMSTOP": is_disabled,
                "TRN_TYPE_PRICE": 8
            })
            
        payload_local_zone = {
            "AGREEMENT_NO": self.blanket_order,
            "ITEMS": items_local_zone,
            "COMP_CODE": supplier_code if supplier_code else "",
            "AGR_STDATE": sa_doc.from_date,
            "AGR_ENDATE": sa_doc.to_date,
        }
        payload_free_zone = {
            "AGREEMENT_NO": self.blanket_order,
            "ITEMS": items_free_zone,
            "COMP_CODE": supplier_code if supplier_code else "",
            "AGR_STDATE": sa_doc.from_date,
            "AGR_ENDATE": sa_doc.to_date,
        }
        insert_pos_item(payload_local_zone, payload_free_zone)
