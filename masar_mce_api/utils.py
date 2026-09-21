import frappe
from frappe import _


@frappe.whitelist()
def get_tax_for_item(item_code=None, category='Local Zone'):
    if not item_code or not category:
        return 0
    if not frappe.db.exists('Item', item_code):
        return 0
    item_doc = frappe.get_doc('Item', item_code)
    tax_rate = get_tax_from_taxes(item_doc.taxes, category)
    if tax_rate is not None:
        return tax_rate
    group_doc = frappe.get_doc('Item Group', item_doc.item_group)
    tax_rate = get_tax_from_taxes(group_doc.taxes, category)
    if tax_rate is not None:
        return tax_rate
    return 0


def get_item_barcode(item_code):
    if not item_code:
        return None
    barcode = frappe.db.get_value(
        "Item Barcode",
        {"parent": item_code},
        "barcode"
    )
    if barcode:
        return barcode
    return None

def get_tax_from_taxes(taxes, category):
    """Return tax rate from a taxes table (item or item group)."""
    if not taxes:
        return None
    for tax in taxes:
        if tax.tax_category == category and tax.item_tax_template:
            tax_rate = frappe.db.get_value(
                "Item Tax Template Detail",
                {"parent": tax.item_tax_template},
                "tax_rate"
            )
            return tax_rate / 100 or 0

    return None


def get_standard_price_list_b_s_sfz():
    buying = frappe.db.get_values(
        "Price List",
        {'enabled' : 1 , 'buying' : 1},
        "name", as_dict=False
    )
    selling = frappe.db.get_values(
        "Price List",
        {'enabled' : 1 , 'selling' : 1 , 'custom_free_zone' : 0 },
        "name", as_dict=False
    )
    selling_free_zone = frappe.db.get_values(
        "Price List",
        {'enabled' : 1 , 'selling' : 1 , 'custom_free_zone' : 1 },
        "name", as_dict=False
    )
    if len(buying) != 1:
        frappe.throw(_(
            "There must be exactly one enabled Buying Price List. "
            "Found {0}".format(len(buying))
        ))
    if len(selling) != 1:
        frappe.throw(_(
            "There must be exactly one enabled Selling Price List. "
            "Found {0}".format(len(selling))
        ))
    if len(selling_free_zone) != 1:
        frappe.throw(_(
            "There must be exactly one enabled Selling Free Zone Price List. "
            "Found {0}".format(len(selling_free_zone))
        ))
    return buying[0][0], selling[0][0], selling_free_zone[0][0]
def get_item_price(item_code):
    if not item_code:
        return None, None
    buying , selling  , selling_free_zone = get_standard_price_list_b_s_sfz()
    local_zone_rate = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": selling},
        "price_list_rate"
    )

    free_zone_rate = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": selling_free_zone},
        "price_list_rate"
    )
    if local_zone_rate and free_zone_rate:
        return local_zone_rate, free_zone_rate
    return None, None


# ---------------------------------------------------------------------------
# Inbound inventory transaction API - master-data resolution, memoised per
# request so a batch of up to 100 rows doesn't re-query the same
# Warehouse/Supplier/Item/Blanket Order repeatedly.
# ---------------------------------------------------------------------------

def _lookup_cache():
    if not hasattr(frappe.local, "mce_lookup_cache"):
        frappe.local.mce_lookup_cache = {"warehouse": {}, "supplier": {}, "item": {}, "agreement": {}}
    return frappe.local.mce_lookup_cache


def resolve_warehouse_by_code(code, expected_type=None):
    """Resolve an external warehouse code (Warehouse.custom_warehouse_no) to
    {name, warehouse_type, company}.

    custom_warehouse_no is NOT unique across warehouse_type in live data
    (e.g. code "1110" matches both a مركزي and a مستودع warehouse), so when
    the caller knows which role/type it needs, that must be part of the
    lookup filter itself - resolving by code alone and checking the type
    afterwards can silently pick the wrong warehouse. If nothing matches
    with the expected type, fall back to an unfiltered lookup purely so the
    caller can report the *actual* type in a clear rejection reason."""
    cache = _lookup_cache()["warehouse"]
    key = (code, expected_type)
    if key not in cache:
        filters = {"custom_warehouse_no": code, "disabled": 0}
        if expected_type:
            filters["warehouse_type"] = expected_type
        result = frappe.db.get_value("Warehouse", filters, ["name", "warehouse_type", "company"], as_dict=True)
        if not result and expected_type:
            result = frappe.db.get_value(
                "Warehouse",
                {"custom_warehouse_no": code, "disabled": 0},
                ["name", "warehouse_type", "company"],
                as_dict=True,
            )
        cache[key] = result
    return cache[key]


def resolve_supplier_by_code(code):
    """Resolve an external supplier code to a Supplier name. Supplier.name and
    Supplier.custom_supplier_code are often the same value, so try a direct
    match first before falling back to the custom field."""
    cache = _lookup_cache()["supplier"]
    if code not in cache:
        if frappe.db.exists("Supplier", code):
            cache[code] = code
        else:
            cache[code] = frappe.db.get_value("Supplier", {"custom_supplier_code": code}, "name")
    return cache[code]


def item_exists(item_code):
    cache = _lookup_cache()["item"]
    if item_code not in cache:
        cache[item_code] = bool(frappe.db.exists("Item", item_code))
    return cache[item_code]


def get_active_supplier_agreement(item_code):
    """Return the Blanket Order (Supplier Agreement) currently active for an
    Item: Item.custom_latest_sa if set, otherwise the most recent submitted
    Blanket Order with custom_status = Active that lists this item. Mirrors
    masar_mce's own custom/Item/item.py::update_pos_item resolution."""
    cache = _lookup_cache()["agreement"]
    if item_code not in cache:
        latest_sa = frappe.db.get_value("Item", item_code, "custom_latest_sa")
        if not latest_sa:
            rows = frappe.db.sql(
                """
                SELECT tbo.name
                FROM `tabBlanket Order Item` tboi
                INNER JOIN `tabBlanket Order` tbo ON tboi.parent = tbo.name
                WHERE tboi.item_code = %s
                  AND tbo.docstatus = 1
                  AND tbo.custom_status = 'Active'
                GROUP BY tbo.name
                """,
                (item_code,),
                as_dict=True,
            )
            latest_sa = rows[0].name if rows else None
        cache[item_code] = latest_sa
    return cache[item_code]


def resolve_purchase_order_for_agreement_item(item_code, supplier_agreement, supplier=None):
    """G1G support: find an open Purchase Order Item for item_code placed
    against supplier_agreement, with qty still available to receive.

    Uses ERPNext's own standard Purchase Order Item.blanket_order field -
    confirmed against live data to be the field masar_mce's own on_submit
    hook (set_agreement_date) actually relies on (~1900+ populated rows).
    The custom_blanket_order_item field is declared but unused anywhere in
    masar_mce and populated on zero rows, so it is not the real linkage."""
    if not supplier_agreement:
        return None

    conditions = ["poi.item_code = %(item_code)s", "poi.blanket_order = %(agreement)s"]
    values = {"item_code": item_code, "agreement": supplier_agreement}
    if supplier:
        conditions.append("po.supplier = %(supplier)s")
        values["supplier"] = supplier

    rows = frappe.db.sql(
        f"""
        SELECT poi.name AS purchase_order_item, poi.parent AS purchase_order, poi.rate,
               (poi.qty - IFNULL(poi.received_qty, 0)) AS available_qty
        FROM `tabPurchase Order Item` poi
        INNER JOIN `tabPurchase Order` po ON po.name = poi.parent
        WHERE {" AND ".join(conditions)}
          AND po.docstatus = 1
          AND po.status NOT IN ('Closed', 'On Hold')
          AND (poi.qty - IFNULL(poi.received_qty, 0)) > 0
        ORDER BY po.transaction_date
        LIMIT 1
        """,
        values,
        as_dict=True,
    )
    return rows[0] if rows else None


def mask_headers(headers):
    """Never persist credentials in plain text - Authorization/API-key/token
    style headers get their value replaced, keeping only the auth scheme
    (e.g. 'Basic ********') when one is present."""
    if not headers:
        return {}
    sensitive = {"authorization", "api-key", "x-api-key", "token", "x-auth-token", "cookie", "password", "secret"}
    masked = {}
    for key, value in headers.items():
        if key.lower() in sensitive:
            if isinstance(value, str) and " " in value:
                scheme = value.split(" ", 1)[0]
                masked[key] = f"{scheme} ********"
            else:
                masked[key] = "********"
        else:
            masked[key] = value
    return masked
