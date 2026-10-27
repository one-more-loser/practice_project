from sqlalchemy import select

from cs_agent.db.business_tables import products


def test_product_detail_is_independent_of_catalog_first_page(environment):
    client, engine, clock, *_ = environment
    with engine.begin() as connection:
        template = dict(
            connection.execute(select(products).where(products.c.product_id == "product_cup"))
            .mappings()
            .one()
        )
        for number in range(51):
            row = {
                **template,
                "product_id": f"zz_extra_{number:02}",
                "created_at": clock.now(),
                "updated_at": clock.now(),
                "source_kind": "derived",
                "source_revision": "synthetic-query-boundary-test",
            }
            connection.execute(products.insert().values(**row))
    result = client.get("/api/v1/products/zz_extra_50")
    assert result.status_code == 200, result.text
    assert result.json()["data"]["product_id"] == "zz_extra_50"
