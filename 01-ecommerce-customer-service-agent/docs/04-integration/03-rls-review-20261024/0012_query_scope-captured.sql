ALTER POLICY b2_query ON customer_order USING(customer_order.store_id=current_setting('cs.store',true)
          AND customer_order.data_batch_id=current_setting('cs.batch',true)
          AND customer_order.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=customer_order.data_batch_id
            AND b.generation=customer_order.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=customer_order.store_id AND s.status='active') AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer' AND created_at>=nullif(current_setting('cs.start',true),'')::timestamptz AND created_at<=nullif(current_setting('cs.end',true),'')::timestamptz);

ALTER POLICY b2_query ON order_item USING(order_item.store_id=current_setting('cs.store',true)
          AND order_item.data_batch_id=current_setting('cs.batch',true)
          AND order_item.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=order_item.data_batch_id
            AND b.generation=order_item.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=order_item.store_id AND s.status='active') AND order_id IN (SELECT order_id FROM customer_order) AND current_setting('cs.role',true)='customer');

ALTER POLICY b2_query ON order_shipment USING(order_shipment.store_id=current_setting('cs.store',true)
          AND order_shipment.data_batch_id=current_setting('cs.batch',true)
          AND order_shipment.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=order_shipment.data_batch_id
            AND b.generation=order_shipment.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=order_shipment.store_id AND s.status='active') AND order_id IN (SELECT order_id FROM customer_order) AND current_setting('cs.role',true)='customer');

ALTER POLICY b2_query ON wallet_account USING(wallet_account.store_id=current_setting('cs.store',true)
          AND wallet_account.data_batch_id=current_setting('cs.batch',true)
          AND wallet_account.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=wallet_account.data_batch_id
            AND b.generation=wallet_account.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=wallet_account.store_id AND s.status='active') AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer');

ALTER POLICY b2_query ON wallet_entry USING(wallet_entry.store_id=current_setting('cs.store',true)
          AND wallet_entry.data_batch_id=current_setting('cs.batch',true)
          AND wallet_entry.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=wallet_entry.data_batch_id
            AND b.generation=wallet_entry.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=wallet_entry.store_id AND s.status='active') AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer' AND occurred_at>=nullif(current_setting('cs.start',true),'')::timestamptz AND occurred_at<=nullif(current_setting('cs.end',true),'')::timestamptz);

ALTER POLICY b2_query ON product USING(product.store_id=current_setting('cs.store',true)
          AND product.data_batch_id=current_setting('cs.batch',true)
          AND product.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=product.data_batch_id
            AND b.generation=product.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=product.store_id AND s.status='active'));

ALTER POLICY b2_query ON sku USING(sku.store_id=current_setting('cs.store',true)
          AND sku.data_batch_id=current_setting('cs.batch',true)
          AND sku.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=sku.data_batch_id
            AND b.generation=sku.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=sku.store_id AND s.status='active'));

ALTER POLICY b2_query ON inventory USING(inventory.store_id=current_setting('cs.store',true)
          AND inventory.data_batch_id=current_setting('cs.batch',true)
          AND inventory.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=inventory.data_batch_id
            AND b.generation=inventory.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=inventory.store_id AND s.status='active'));
