# tests/test_convert_shopify_export.py
"""
scripts/convert_shopify_export.py -- exercised against small,
Shopify-export-shaped synthetic fixtures (not the real client file, which
is run separately/manually) covering the actual edge cases a real export
has: multi-row products grouped by Handle, fields only populated on a
group's first row, variant rows carrying stock/SKU independently, a
product's image landing on a non-first row, draft/archived products, and
missing sku/image/stock.
"""
import csv

from scripts.convert_shopify_export import convert

_FIELDNAMES = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Variant SKU", "Variant Price",
    "Variant Inventory Qty", "Image Src", "Product Category", "Status",
]


def _write_csv(path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in _FIELDNAMES})


def _row(handle, **kwargs):
    return {"Handle": handle, **kwargs}


def test_simple_single_row_product(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("widget", Title="Widget", Body="ignored-key-not-real",
             **{"Body (HTML)": "<p>A fine widget.</p>", "Variant SKU": "SKU-1", "Variant Price": "199.00",
                "Variant Inventory Qty": "10", "Image Src": "https://example.com/w.png",
                "Product Category": "Home & Garden > Widgets", "Status": "active", "Vendor": "Acme"}),
    ])

    stats = convert(str(input_csv), str(output_csv))
    assert stats["converted"] == 1
    assert stats["skipped_not_active"] == 0

    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Widget"
    assert row["price"] == "199.00"
    assert row["description"] == "A fine widget."
    assert row["image_url"] == "https://example.com/w.png"
    assert row["sku"] == "SKU-1"
    assert row["stock_quantity"] == "10"
    assert row["category"] == "Widgets"  # last breadcrumb segment only


def test_multi_variant_product_sums_stock_and_takes_first_row_fields(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("shirt", Title="T-Shirt", **{"Body (HTML)": "<p>Cotton tee</p>", "Variant SKU": "SHIRT-S",
             "Variant Price": "599.00", "Variant Inventory Qty": "5", "Image Src": "https://example.com/shirt.png",
             "Product Category": "Apparel > Shirts", "Status": "active"}),
        # Later variant rows: Title/Body/Category blank (Shopify convention), own SKU/price/stock.
        _row("shirt", **{"Variant SKU": "SHIRT-M", "Variant Price": "599.00", "Variant Inventory Qty": "8"}),
        _row("shirt", **{"Variant SKU": "SHIRT-L", "Variant Price": "599.00", "Variant Inventory Qty": "12"}),
    ])

    stats = convert(str(input_csv), str(output_csv))
    assert stats["converted"] == 1

    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    row = rows[0]
    assert row["name"] == "T-Shirt"
    assert row["sku"] == "SHIRT-S"  # first row's SKU, not summed/combined
    assert row["stock_quantity"] == "25"  # 5 + 8 + 12 summed across all variants


def test_image_on_a_later_row_is_still_found(tmp_path):
    """A product's variant rows can precede the row carrying its main
    image -- the first row overall may have no Image Src at all."""
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("mug", Title="Mug", **{"Body (HTML)": "Ceramic mug", "Variant SKU": "MUG-1",
             "Variant Price": "249.00", "Variant Inventory Qty": "3", "Status": "active"}),  # no Image Src here
        _row("mug", **{"Image Src": "https://example.com/mug.png"}),
    ])

    stats = convert(str(input_csv), str(output_csv))
    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    assert rows[0]["image_url"] == "https://example.com/mug.png"
    assert stats["missing_image"] == 0


def test_draft_and_archived_products_skipped(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("draft-item", Title="Draft Item", **{"Variant Price": "99.00", "Status": "draft"}),
        _row("archived-item", Title="Archived Item", **{"Variant Price": "99.00", "Status": "archived"}),
        _row("live-item", Title="Live Item", **{"Variant Price": "99.00", "Status": "active"}),
    ])

    stats = convert(str(input_csv), str(output_csv))
    assert stats["skipped_not_active"] == 2
    assert stats["converted"] == 1

    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    assert [r["name"] for r in rows] == ["Live Item"]


def test_missing_sku_and_stock_reported_not_errored(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("mystery", Title="Mystery Item", **{"Variant Price": "10.00", "Status": "active"}),  # no SKU, no stock, no image
    ])

    stats = convert(str(input_csv), str(output_csv))
    assert stats["converted"] == 1
    assert stats["missing_sku"] == 1
    assert stats["missing_image"] == 1
    assert stats["missing_stock"] == 1

    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    assert rows[0]["sku"] == ""
    assert rows[0]["stock_quantity"] == "0"


def test_product_missing_name_or_price_skipped(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("no-price", Title="No Price Item", **{"Status": "active"}),  # Variant Price blank
        _row("no-title", **{"Variant Price": "50.00", "Status": "active"}),  # Title blank
        _row("fine", Title="Fine Item", **{"Variant Price": "20.00", "Status": "active"}),
    ])

    stats = convert(str(input_csv), str(output_csv))
    assert stats["skipped_missing_name_or_price"] == 2
    assert stats["converted"] == 1


def test_row_with_no_handle_is_skipped_not_crashed(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("", Title="Orphan Row", **{"Variant Price": "10.00", "Status": "active"}),
        _row("real-product", Title="Real Product", **{"Variant Price": "20.00", "Status": "active"}),
    ])

    stats = convert(str(input_csv), str(output_csv))
    assert stats["total_products_in_export"] == 1  # the blank-Handle row never formed a group
    assert stats["converted"] == 1


def test_html_description_stripped_and_whitespace_collapsed(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("book", Title="Book", **{
            "Body (HTML)": "<p>Great read.</p>\n<ul><li>Hardcover</li><li>300 pages</li></ul>",
            "Variant Price": "499.00", "Status": "active",
        }),
    ])

    convert(str(input_csv), str(output_csv))
    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    description = rows[0]["description"]
    assert "<" not in description and ">" not in description
    assert "Great read." in description
    assert "Hardcover" in description


def test_category_without_breadcrumb_separator_kept_as_is(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("item", Title="Item", **{"Variant Price": "10.00", "Status": "active", "Product Category": "Toys"}),
    ])

    convert(str(input_csv), str(output_csv))
    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    assert rows[0]["category"] == "Toys"


def test_output_columns_match_wizard_import_format(tmp_path):
    input_csv = tmp_path / "shopify.csv"
    output_csv = tmp_path / "commerce.csv"
    _write_csv(input_csv, [
        _row("item", Title="Item", **{"Variant Price": "10.00", "Status": "active"}),
    ])

    convert(str(input_csv), str(output_csv))
    with open(output_csv, encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == ["name", "price", "description", "image_url", "sku", "stock_quantity", "category"]
