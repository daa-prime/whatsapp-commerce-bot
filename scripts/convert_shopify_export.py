# scripts/convert_shopify_export.py
"""
Converts a raw Shopify product export CSV into the CSV format the
onboarding wizard's bulk product import expects
(admin/onboarding_wizard.py's _parse_csv_products: name, price,
description, image_url, sku, stock_quantity, category).

Standalone, one-off conversion utility -- deliberately NOT baked into the
wizard's core CSV importer, which stays format-agnostic (it only knows the
commerce-CSV shape, not any particular e-commerce platform's export shape).
Run this once per Shopify export, then upload the resulting file through
the wizard's or portal's normal CSV import.

Usage:
    python scripts/convert_shopify_export.py shopify_export.csv commerce_import.csv

Shopify's export has one row per (product, variant, image) combination,
grouped by `Handle` -- only the *first* row of each group has `Title`,
`Body (HTML)`, `Product Category` filled in; later rows repeat the Handle
but leave those columns blank (Shopify's own export convention, not a data
quality issue).

Conversion rules, and the two flagged product decisions:
  - name        <- Title (first non-blank in the group)
  - price       <- Variant Price (first non-blank in the group)
  - description <- Body (HTML), HTML tags stripped to plain text (stdlib
                   html.parser -- no bs4/lxml dependency needed or added)
  - image_url   <- Image Src (first row in the group that HAS one -- not
                   necessarily the group's first row overall, since variant
                   rows can precede the row carrying the product's main image)
  - sku         <- Variant SKU (first non-blank in the group; blank, not an
                   error, if no row in the group has one)
  - stock_quantity <- SUM of Variant Inventory Qty across every row in the
                   group, not just the first. FLAGGED: a product with
                   multiple size/color variants really has that many units
                   sellable in total once it collapses to one commerce-CSV
                   row with no variant concept of its own -- taking only the
                   first variant's count would understate real availability.
  - category    <- Product Category, keeping only the LAST ">"-separated
                   breadcrumb segment (e.g. "Home & Garden > Linens &
                   Bedding > Bedding > Quilts & Comforters" -> "Quilts &
                   Comforters"). FLAGGED: this app's only use of
                   products.category (core/commerce_flow.py's
                   _group_products_by_category, a WhatsApp list-message
                   section title) is capped around 24 chars by Meta -- the
                   full breadcrumb wouldn't render usefully there.
  - Only rows where Status == "active" (case-insensitive) are included --
    draft/archived products are skipped entirely.
  - A product missing a name or price after conversion is skipped (reported
    separately) rather than written as an unusable row -- the wizard's own
    importer requires both anyway.
"""
import csv
import sys
from html.parser import HTMLParser

_OUTPUT_FIELDNAMES = ["name", "price", "description", "image_url", "sku", "stock_quantity", "category"]


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _strip_html(html: str) -> str:
    if not html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html)
    # Collapse whitespace/newlines left behind by stripped block tags
    # (<p>, <br>, <li>, ...) -- Shopify's Body (HTML) is paragraph/list
    # markup, not preformatted text, so multiple blank lines aren't meaningful.
    return " ".join(stripper.get_text().split())


def _first_nonblank(rows: list[dict], key: str) -> str:
    for row in rows:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def _first_image(rows: list[dict]) -> str:
    return _first_nonblank(rows, "Image Src")


def _sum_stock(rows: list[dict]) -> int:
    total = 0
    for row in rows:
        raw = (row.get("Variant Inventory Qty") or "").strip()
        if not raw:
            continue
        try:
            total += int(float(raw))
        except ValueError:
            continue  # a malformed quantity contributes nothing rather than crashing the whole conversion
    return total


def _last_category_segment(full_category: str) -> str:
    if not full_category:
        return ""
    segments = [s.strip() for s in full_category.split(">")]
    return segments[-1] if segments and segments[-1] else full_category.strip()


def convert(input_path: str, output_path: str) -> dict:
    """Reads the Shopify export at input_path, writes the converted
    commerce-import CSV to output_path, and returns a stats dict describing
    what happened -- so a caller (main() below, or a test) can report
    exactly how much manual cleanup a merchant would need to do after
    conversion, not just "it worked"."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            handle = (row.get("Handle") or "").strip()
            if not handle:
                continue  # a row with no Handle can't be grouped to a product -- skip rather than crash
            if handle not in groups:
                groups[handle] = []
                order.append(handle)
            groups[handle].append(row)

    stats = {
        "total_products_in_export": len(order),
        "skipped_not_active": 0,
        "skipped_missing_name_or_price": 0,
        "converted": 0,
        "missing_image": 0,
        "missing_sku": 0,
        "missing_stock": 0,
    }

    out_rows = []
    for handle in order:
        rows = groups[handle]
        status = _first_nonblank(rows, "Status").lower()
        if status != "active":
            stats["skipped_not_active"] += 1
            continue

        name = _first_nonblank(rows, "Title")
        price = _first_nonblank(rows, "Variant Price")
        if not name or not price:
            stats["skipped_missing_name_or_price"] += 1
            continue

        description = _strip_html(_first_nonblank(rows, "Body (HTML)"))
        image_url = _first_image(rows)
        sku = _first_nonblank(rows, "Variant SKU")
        stock_quantity = _sum_stock(rows)
        category = _last_category_segment(_first_nonblank(rows, "Product Category"))

        if not image_url:
            stats["missing_image"] += 1
        if not sku:
            stats["missing_sku"] += 1
        if not stock_quantity:
            stats["missing_stock"] += 1

        out_rows.append({
            "name": name,
            "price": price,
            "description": description,
            "image_url": image_url,
            "sku": sku,
            "stock_quantity": stock_quantity,
            "category": category,
        })
        stats["converted"] += 1

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)

    return stats


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/convert_shopify_export.py <shopify_export.csv> <commerce_import.csv>")
        sys.exit(1)

    stats = convert(sys.argv[1], sys.argv[2])
    print(f"Products in export:          {stats['total_products_in_export']}")
    print(f"Skipped (not active):        {stats['skipped_not_active']}")
    print(f"Skipped (no name/price):     {stats['skipped_missing_name_or_price']}")
    print(f"Converted:                   {stats['converted']}")
    print(f"  missing image_url:         {stats['missing_image']}")
    print(f"  missing sku:               {stats['missing_sku']}")
    print(f"  missing/zero stock:        {stats['missing_stock']}")


if __name__ == "__main__":
    main()
