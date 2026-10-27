from cs_agent.catalog.service import CatalogService


def test_public_catalog_uses_business_price_and_omits_internal_fields(business):
    db, _, _, _ = business
    with db.transaction() as session:
        rows = CatalogService().read(session)
    cup = next(row for row in rows if row["product_id"] == "product_cup")
    assert cup["skus"][0]["price_minor"] == "4200" or cup["skus"][0]["price_minor"] == "4000"
    assert {sku["sku_id"]: sku["available_qty"] for sku in cup["skus"]} == {
        "sku_cup_ivory": 10,
        "sku_cup_coral": 5,
    }
    assert all("cost_minor" not in sku for sku in cup["skus"])
    assert "supplier_ref" not in cup
