"""Reviewed Shopify object selections and customer-excluding result models.

These models project source responses before they can reach Agent context or
retained output. They do not register Tools or establish connection permission.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FIELD_POLICY_REVISION = "shopify-objects-v1"
PAGE_LIMIT = 50


class ShopifyObject(BaseModel):
    """Ignore unselected source fields at every modeled nesting level."""

    model_config = ConfigDict(extra="ignore", strict=True)


class Shop(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Shop/[0-9]+$")
    name: str
    currencyCode: str
    ianaTimezone: str


class Money(ShopifyObject):
    amount: str
    currencyCode: str

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError("Money must be a decimal string.") from None
        if not amount.is_finite():
            raise ValueError("Money must be finite.")
        return value


class MoneyBag(ShopifyObject):
    shopMoney: Money
    presentmentMoney: Money


class PageInfo(ShopifyObject):
    hasNextPage: bool
    endCursor: str | None = Field(max_length=2048)

    @model_validator(mode="after")
    def validate_continuation(self) -> PageInfo:
        if self.hasNextPage and not self.endCursor:
            raise ValueError("An unread page requires a continuation cursor.")
        return self


class ObjectPage[Node: ShopifyObject](ShopifyObject):
    nodes: list[Node] = Field(max_length=PAGE_LIMIT)
    pageInfo: PageInfo


class ProductReference(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/(Product|ProductVariant)/[0-9]+$")


class Variant(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/ProductVariant/[0-9]+$")
    title: str
    sku: str | None
    price: str


class Product(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Product/[0-9]+$")
    title: str
    handle: str
    status: str
    productType: str
    vendor: str
    descriptionHtml: str
    createdAt: str
    updatedAt: str
    variants: ObjectPage[Variant]


class OrderLine(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/LineItem/[0-9]+$")
    sku: str | None
    quantity: int
    currentQuantity: int
    product: ProductReference | None
    variant: ProductReference | None


class Order(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Order/[0-9]+$")
    name: str
    createdAt: str
    updatedAt: str
    processedAt: str
    test: bool
    cancelledAt: str | None
    displayFinancialStatus: str | None
    displayFulfillmentStatus: str
    currencyCode: str
    currentTotalPriceSet: MoneyBag
    currentTotalDiscountsSet: MoneyBag
    totalRefundedSet: MoneyBag
    lineItems: ObjectPage[OrderLine]


class ShopData(ShopifyObject):
    shop: Shop


class ProductData(ShopifyObject):
    product: Product | None


class ProductsData(ShopifyObject):
    products: ObjectPage[Product]


class OrderData(ShopifyObject):
    order: Order | None


class OrdersData(ShopifyObject):
    orders: ObjectPage[Order]


class Location(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Location/[0-9]+$")
    name: str
    isActive: bool


class InventoryQuantity(ShopifyObject):
    name: str
    quantity: int


class InventoryLevel(ShopifyObject):
    id: str
    location: Location
    quantities: list[InventoryQuantity] = Field(max_length=4)


class InventoryItem(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/InventoryItem/[0-9]+$")
    sku: str | None
    tracked: bool
    inventoryLevels: ObjectPage[InventoryLevel]


class Collection(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Collection/[0-9]+$")
    title: str
    handle: str
    descriptionHtml: str
    updatedAt: str


class Page(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Page/[0-9]+$")
    title: str
    handle: str
    body: str
    isPublished: bool
    createdAt: str
    updatedAt: str
    publishedAt: str | None


class Article(Page):
    id: str = Field(pattern=r"^gid://shopify/Article/[0-9]+$")


class ProductSEO(ShopifyObject):
    title: str | None
    description: str | None


class ProductContent(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Product/[0-9]+$")
    title: str
    descriptionHtml: str
    handle: str
    status: str
    updatedAt: str
    seo: ProductSEO


class ProductContentData(ShopifyObject):
    product: ProductContent | None


class PageContentData(ShopifyObject):
    page: Page | None


class InventoryData(ShopifyObject):
    inventoryItems: ObjectPage[InventoryItem]


class LocationsData(ShopifyObject):
    locations: ObjectPage[Location]


class CollectionsData(ShopifyObject):
    collections: ObjectPage[Collection]


class PagesData(ShopifyObject):
    pages: ObjectPage[Page]


class ArticlesData(ShopifyObject):
    articles: ObjectPage[Article]


class VariantsParent(ShopifyObject):
    id: str
    variants: ObjectPage[Variant]


class VariantsData(ShopifyObject):
    product: VariantsParent | None


class LinesParent(ShopifyObject):
    id: str
    lineItems: ObjectPage[OrderLine]


class LinesData(ShopifyObject):
    order: LinesParent | None


class LevelsParent(ShopifyObject):
    id: str
    inventoryLevels: ObjectPage[InventoryLevel]


class LevelsData(ShopifyObject):
    inventoryItem: LevelsParent | None


class CollectionParent(ShopifyObject):
    id: str
    products: ObjectPage[Product]


class CollectionProductsData(ShopifyObject):
    collection: CollectionParent | None


class OrderSnapshot(ShopifyObject):
    id: str = Field(pattern=r"^gid://shopify/Order/[0-9]+$")
    createdAt: str
    test: bool
    cancelledAt: str | None
    displayFinancialStatus: str | None
    displayFulfillmentStatus: str
    currentTotalPriceSet: MoneyBag
    currentTotalDiscountsSet: MoneyBag
    totalRefundedSet: MoneyBag


class OrderSnapshotsData(ShopifyObject):
    orders: ObjectPage[OrderSnapshot]


_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
_MONEY = "shopMoney { amount currencyCode } presentmentMoney { amount currencyCode }"
_PRODUCT_FIELDS = (
    """
    id title handle status productType vendor descriptionHtml createdAt updatedAt
    variants(first: $nestedFirst) {
        nodes { id title sku price }
"""
    + _PAGE_INFO
    + " }"
)
_ORDER_FIELDS = (
    """
    id name createdAt updatedAt processedAt test cancelledAt
    displayFinancialStatus displayFulfillmentStatus currencyCode
    currentTotalPriceSet { """
    + _MONEY
    + " } currentTotalDiscountsSet { "
    + _MONEY
    + " } totalRefundedSet { "
    + _MONEY
    + """ }
    lineItems(first: $nestedFirst) {
        nodes { id sku quantity currentQuantity product { id } variant { id } }
    """
    + _PAGE_INFO
    + " }"
)

# Query strings are server-owned; no operation accepts caller-authored GraphQL.
SHOP_QUERY = "query ShopContext { shop { id name currencyCode ianaTimezone } }"
PRODUCT_QUERY = "query Product($id: ID!, $nestedFirst: Int!) { product(id: $id) { " + _PRODUCT_FIELDS + " } }"
PRODUCTS_QUERY = (
    "query Products($first: Int!, $after: String, $nestedFirst: Int!) { products(first: $first, after: $after, sortKey: ID) { nodes { "
    + _PRODUCT_FIELDS
    + " } "
    + _PAGE_INFO
    + " } }"
)
ORDER_QUERY = "query Order($id: ID!, $nestedFirst: Int!) { order(id: $id) { " + _ORDER_FIELDS + " } }"
ORDERS_QUERY = (
    "query Orders($first: Int!, $after: String, $nestedFirst: Int!, $filter: String!) { "
    "orders(first: $first, after: $after, query: $filter, sortKey: CREATED_AT) { nodes { " + _ORDER_FIELDS + " } " + _PAGE_INFO + " } }"
)


def incomplete_pages(value: ShopifyObject) -> tuple[str, ...]:
    """Report selected connections that still have unread pages."""
    paths: list[str] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, ObjectPage) and node.pageInfo.hasNextPage:
            paths.append(path)
        if isinstance(node, ShopifyObject):
            for name in type(node).model_fields:
                visit(getattr(node, name), f"{path}.{name}" if path else name)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")

    visit(value, "")
    return tuple(paths)


# Closed object families and child connections. Aliases are never caller supplied.
_LEVEL_FIELDS = 'id location { id name isActive } quantities(names: ["available", "on_hand", "incoming", "committed"]) { name quantity }'
_CONTENT_FIELDS = "id title handle body isPublished createdAt updatedAt publishedAt"
_RESOURCE_FIELDS = {
    "inventory": (
        "inventoryItems",
        "id sku tracked inventoryLevels(first: $nestedFirst) { nodes { " + _LEVEL_FIELDS + " } " + _PAGE_INFO + " }",
        InventoryData,
        "read_inventory",
    ),
    "locations": ("locations", "id name isActive", LocationsData, ("read_locations", "read_inventory", "read_markets_home")),
    "collections": ("collections", "id title handle descriptionHtml updatedAt", CollectionsData, "read_products"),
    "pages": ("pages", _CONTENT_FIELDS, PagesData, ("read_content", "read_online_store_pages")),
    "articles": ("articles", _CONTENT_FIELDS, ArticlesData, ("read_content", "read_online_store_pages")),
}
RESOURCE_READS = {
    name: (
        "query Objects($first: Int!, $after: String"
        + (", $nestedFirst: Int!" if name == "inventory" else "")
        + ") { "
        + resource
        + "(first: $first, after: $after) { nodes { "
        + fields
        + " } "
        + _PAGE_INFO
        + " } }",
        projection,
        scope,
    )
    for name, (resource, fields, projection, scope) in _RESOURCE_FIELDS.items()
}
CHILD_READS = {
    "variants": ("product", "Product", "variants", "id title sku price", VariantsData, "read_products"),
    "line_items": ("order", "Order", "lineItems", "id sku quantity currentQuantity product { id } variant { id }", LinesData, "read_orders"),
    "inventory_levels": ("inventoryItem", "InventoryItem", "inventoryLevels", _LEVEL_FIELDS, LevelsData, "read_inventory"),
    "collection_products": ("collection", "Collection", "products", _PRODUCT_FIELDS, CollectionProductsData, "read_products"),
}
ORDER_SNAPSHOTS_QUERY = (
    "query OrderSnapshots($first: Int!, $after: String, $filter: String!) { "
    "orders(first: $first, after: $after, query: $filter, sortKey: CREATED_AT) { nodes { "
    "id createdAt test cancelledAt displayFinancialStatus displayFulfillmentStatus "
    "currentTotalPriceSet { " + _MONEY + " } currentTotalDiscountsSet { " + _MONEY + " } totalRefundedSet { " + _MONEY + " } } " + _PAGE_INFO + " } }"
)
