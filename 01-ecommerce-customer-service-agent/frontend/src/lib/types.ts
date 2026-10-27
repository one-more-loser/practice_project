export interface SessionView {
  subject_id: string
  store_id: string
  active_role: string
  roles: string[]
  auth_version: number
  csrf_token: string
  capabilities?: string[]
  capability_revision?: number
}
export interface Sku {
  sku_id: string
  specifications: Record<string, string>
  price_minor: string
  available_qty: number
  status: string
  version: number
  price_version: number
  inventory_version?: number
  stock_qty?: number
  reserved_qty?: number
}
export interface Product {
  product_id: string
  name: string
  description: string
  category: string
  image_slug: string
  status: string
  version: number
  skus: Sku[]
}
export interface CartItem {
  item_id: string
  sku_id: string
  name: string
  image_slug: string
  qty: number
  selected: boolean
  version: number
  price_minor: string
  available_qty: number
  specifications: Record<string, string>
}
export interface Cart {
  cart_id: string
  version: number
  items: CartItem[]
}
export interface Operation {
  operation_id: string
  business_ref: string
  version: number
  quote_id?: string
  balance_minor?: string
}
export interface Quote {
  quote_id: string
  version: number
  payable_minor: string
  goods_minor: string
  discount_minor: string
  freight_minor: string
  expires_at: string
  lines: { product_name: string; qty: number; unit_price_minor: string }[]
}
export interface Proposal {
  confirmation_id: string
  summary: {
    action: string
    resource_ref: string
    payable_minor?: string
    goods_minor?: string
    original_freight_minor?: string
    notice?: string
    display_no?: string
    shipping?: { address_masked: string; phone_masked: string }
    previous_shipping?: { address_masked: string; phone_masked: string }
    items?: { name?: string; product_name?: string; qty: number; goods_minor?: string }[]
  }
  expires_at: string
}
export interface OrderItem {
  order_item_id: string
  product_name: string
  specifications: Record<string, string>
  ordered_qty: number
  unit_price_minor: string
  allocated_minor: string
  paid_goods_minor: string
}
export interface Order {
  order_id: string
  display_no: string
  trade_status: string
  fulfillment_status: string
  goods_minor: string
  discount_minor: string
  freight_minor: string
  payable_minor: string
  paid_minor: string
  refunded_minor: string
  version: number
  change_count: number
  created_at: string
  payment_expires_at: string
  received_at: string | null
  items: OrderItem[]
  shipping: Record<string, string>
  shipments: {
    shipment_id: string
    version: number
    carrier: string
    tracking_no: string
    status: string
    events: { description: string; occurred_at: string }[]
  }[]
  allowed_actions: Record<string, boolean>
  cancellation_requires_eligibility_review: boolean
}
export interface Wallet {
  balance_minor: string
  currency: string
  version: number
}
