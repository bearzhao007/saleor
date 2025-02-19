from typing import TYPE_CHECKING, Optional

import graphene
from promise import Promise

from ...checkout import calculations, models
from ...checkout.base_calculations import (
    calculate_undiscounted_base_line_total_price,
    calculate_undiscounted_base_line_unit_price,
)
from ...checkout.calculations import fetch_checkout_data
from ...checkout.utils import get_valid_collection_points_for_checkout
from ...core.taxes import zero_money, zero_taxed_money
from ...core.utils.lazyobjects import unwrap_lazy
from ...permission.enums import (
    AccountPermissions,
    CheckoutPermissions,
    PaymentPermissions,
)
from ...shipping.interface import ShippingMethodData
from ...tax.utils import get_display_gross_prices
from ...warehouse import models as warehouse_models
from ...warehouse.reservations import is_reservation_enabled
from ..account.dataloaders import AddressByIdLoader
from ..account.utils import check_is_owner_or_has_one_of_perms
from ..channel import ChannelContext
from ..channel.types import Channel
from ..checkout.dataloaders import ChannelByCheckoutLineIDLoader, ChannelByIdLoader
from ..core import ResolveInfo
from ..core.connection import CountableConnection
from ..core.descriptions import (
    ADDED_IN_31,
    ADDED_IN_34,
    ADDED_IN_35,
    ADDED_IN_38,
    ADDED_IN_39,
    ADDED_IN_313,
    DEPRECATED_IN_3X_FIELD,
    PREVIEW_FEATURE,
)
from ..core.doc_category import DOC_CATEGORY_CHECKOUT, DOC_CATEGORY_PAYMENTS
from ..core.enums import LanguageCodeEnum
from ..core.scalars import UUID
from ..core.tracing import traced_resolver
from ..core.types import BaseObjectType, ModelObjectType, Money, NonNullList, TaxedMoney
from ..core.utils import str_to_enum
from ..decorators import one_of_permissions_required
from ..giftcard.types import GiftCard
from ..meta import resolvers as MetaResolvers
from ..meta.types import ObjectWithMetadata, _filter_metadata
from ..payment.types import TransactionItem
from ..plugins.dataloaders import (
    get_plugin_manager_promise,
    plugin_manager_promise_callback,
)
from ..product.dataloaders import (
    ProductTypeByProductIdLoader,
    ProductTypeByVariantIdLoader,
    ProductVariantByIdLoader,
)
from ..shipping.types import ShippingMethod
from ..site.dataloaders import load_site_callback
from ..tax.dataloaders import (
    TaxConfigurationByChannelId,
    TaxConfigurationPerCountryByTaxConfigurationIDLoader,
)
from ..utils import get_user_or_app_from_context
from ..warehouse.dataloaders import StocksReservationsByCheckoutTokenLoader
from ..warehouse.types import Warehouse
from .dataloaders import (
    CheckoutByTokenLoader,
    CheckoutInfoByCheckoutTokenLoader,
    CheckoutLinesByCheckoutTokenLoader,
    CheckoutLinesInfoByCheckoutTokenLoader,
    CheckoutMetadataByCheckoutIdLoader,
    TransactionItemsByCheckoutIDLoader,
)
from .enums import CheckoutAuthorizeStatusEnum, CheckoutChargeStatusEnum
from .utils import prevent_sync_event_circular_query

if TYPE_CHECKING:
    from ...account.models import Address
    from ...checkout.fetch import CheckoutInfo, CheckoutLineInfo
    from ...plugins.manager import PluginsManager


def get_dataloaders_for_fetching_checkout_data(
    root: models.Checkout, info: ResolveInfo
) -> tuple[
    Optional[Promise["Address"]],
    Promise[list["CheckoutLineInfo"]],
    Promise["CheckoutInfo"],
    Promise["PluginsManager"],
]:
    address_id = root.shipping_address_id or root.billing_address_id
    address = AddressByIdLoader(info.context).load(address_id) if address_id else None
    lines = CheckoutLinesInfoByCheckoutTokenLoader(info.context).load(root.token)
    checkout_info = CheckoutInfoByCheckoutTokenLoader(info.context).load(root.token)
    manager = get_plugin_manager_promise(info.context)
    return address, lines, checkout_info, manager


class GatewayConfigLine(BaseObjectType):
    field = graphene.String(required=True, description="Gateway config key.")
    value = graphene.String(description="Gateway config value for key.")

    class Meta:
        description = "Payment gateway client configuration key and value pair."
        doc_category = DOC_CATEGORY_PAYMENTS


class PaymentGateway(BaseObjectType):
    name = graphene.String(required=True, description="Payment gateway name.")
    id = graphene.ID(required=True, description="Payment gateway ID.")
    config = NonNullList(
        GatewayConfigLine,
        required=True,
        description="Payment gateway client configuration.",
    )
    currencies = NonNullList(
        graphene.String,
        required=True,
        description="Payment gateway supported currencies.",
    )

    class Meta:
        description = (
            "Available payment gateway backend with configuration "
            "necessary to setup client."
        )
        doc_category = DOC_CATEGORY_PAYMENTS


class CheckoutLine(ModelObjectType[models.CheckoutLine]):
    id = graphene.GlobalID(required=True, description="The ID of the checkout line.")
    variant = graphene.Field(
        "saleor.graphql.product.types.ProductVariant",
        required=True,
        description="The product variant from which the checkout line was created.",
    )
    quantity = graphene.Int(
        required=True,
        description="The quantity of product variant assigned to the checkout line.",
    )
    unit_price = graphene.Field(
        TaxedMoney,
        description="The unit price of the checkout line, with taxes and discounts.",
        required=True,
    )
    undiscounted_unit_price = graphene.Field(
        Money,
        description="The unit price of the checkout line, without discounts.",
        required=True,
    )
    total_price = graphene.Field(
        TaxedMoney,
        description="The sum of the checkout line price, taxes and discounts.",
        required=True,
    )
    undiscounted_total_price = graphene.Field(
        Money,
        description="The sum of the checkout line price, without discounts.",
        required=True,
    )
    requires_shipping = graphene.Boolean(
        description="Indicates whether the item need to be delivered.",
        required=True,
    )

    class Meta:
        description = "Represents an item in the checkout."
        interfaces = [graphene.relay.Node, ObjectWithMetadata]
        model = models.CheckoutLine
        metadata_since = ADDED_IN_35

    @staticmethod
    def resolve_variant(root: models.CheckoutLine, info: ResolveInfo):
        variant = ProductVariantByIdLoader(info.context).load(root.variant_id)
        channel = ChannelByCheckoutLineIDLoader(info.context).load(root.id)

        return Promise.all([variant, channel]).then(
            lambda data: ChannelContext(node=data[0], channel_slug=data[1].slug)
        )

    @staticmethod
    @prevent_sync_event_circular_query
    def resolve_unit_price(root, info: ResolveInfo):
        def with_checkout(data):
            checkout, manager = data
            checkout_info = CheckoutInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )
            lines = CheckoutLinesInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )

            def calculate_line_unit_price(data):
                (
                    checkout_info,
                    lines,
                ) = data
                for line_info in lines:
                    if line_info.line.pk == root.pk:
                        return calculations.checkout_line_unit_price(
                            manager=manager,
                            checkout_info=checkout_info,
                            lines=lines,
                            checkout_line_info=line_info,
                        )
                return None

            return Promise.all(
                [
                    checkout_info,
                    lines,
                ]
            ).then(calculate_line_unit_price)

        return Promise.all(
            [
                CheckoutByTokenLoader(info.context).load(root.checkout_id),
                get_plugin_manager_promise(info.context),
            ]
        ).then(with_checkout)

    @staticmethod
    def resolve_undiscounted_unit_price(root, info: ResolveInfo):
        def with_checkout(checkout):
            checkout_info = CheckoutInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )
            lines = CheckoutLinesInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )

            def calculate_undiscounted_unit_price(data):
                (
                    checkout_info,
                    lines,
                ) = data
                for line_info in lines:
                    if line_info.line.pk == root.pk:
                        return calculate_undiscounted_base_line_unit_price(
                            line_info, checkout_info.channel
                        )

                return None

            return Promise.all(
                [
                    checkout_info,
                    lines,
                ]
            ).then(calculate_undiscounted_unit_price)

        return (
            CheckoutByTokenLoader(info.context)
            .load(root.checkout_id)
            .then(with_checkout)
        )

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_total_price(root, info: ResolveInfo):
        def with_checkout(data):
            checkout, manager = data
            checkout_info = CheckoutInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )
            lines = CheckoutLinesInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )

            def calculate_line_total_price(data):
                (checkout_info, lines) = data
                for line_info in lines:
                    if line_info.line.pk == root.pk:
                        return calculations.checkout_line_total(
                            manager=manager,
                            checkout_info=checkout_info,
                            lines=lines,
                            checkout_line_info=line_info,
                        )
                return None

            return Promise.all([checkout_info, lines]).then(calculate_line_total_price)

        return Promise.all(
            [
                CheckoutByTokenLoader(info.context).load(root.checkout_id),
                get_plugin_manager_promise(info.context),
            ]
        ).then(with_checkout)

    @staticmethod
    def resolve_undiscounted_total_price(root, info: ResolveInfo):
        def with_checkout(checkout):
            checkout_info = CheckoutInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )
            lines = CheckoutLinesInfoByCheckoutTokenLoader(info.context).load(
                checkout.token
            )

            def calculate_undiscounted_total_price(data):
                (
                    checkout_info,
                    lines,
                ) = data
                for line_info in lines:
                    if line_info.line.pk == root.pk:
                        return calculate_undiscounted_base_line_total_price(
                            line_info, checkout_info.channel
                        )
                return None

            return Promise.all(
                [
                    checkout_info,
                    lines,
                ]
            ).then(calculate_undiscounted_total_price)

        return (
            CheckoutByTokenLoader(info.context)
            .load(root.checkout_id)
            .then(with_checkout)
        )

    @staticmethod
    def resolve_requires_shipping(root: models.CheckoutLine, info: ResolveInfo):
        def is_shipping_required(product_type):
            return product_type.is_shipping_required

        return (
            ProductTypeByVariantIdLoader(info.context)
            .load(root.variant_id)
            .then(is_shipping_required)
        )


class CheckoutLineCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_CHECKOUT
        node = CheckoutLine


class DeliveryMethod(graphene.Union):
    class Meta:
        description = (
            "Represents a delivery method chosen for the checkout. "
            '`Warehouse` type is used when checkout is marked as "click and collect" '
            "and `ShippingMethod` otherwise." + ADDED_IN_31
        )
        types = (Warehouse, ShippingMethod)

    @classmethod
    def resolve_type(cls, instance, info: ResolveInfo):
        if isinstance(instance, ShippingMethodData):
            return ShippingMethod
        if isinstance(instance, warehouse_models.Warehouse):
            return Warehouse

        return super(DeliveryMethod, cls).resolve_type(instance, info)


class Checkout(ModelObjectType[models.Checkout]):
    id = graphene.ID(required=True, description="The ID of the checkout.")
    created = graphene.DateTime(
        required=True, description="The date and time when the checkout was created."
    )
    updated_at = graphene.DateTime(
        required=True,
        description=("Time of last modification of the given checkout." + ADDED_IN_313),
    )
    last_change = graphene.DateTime(
        required=True,
        deprecation_reason=(f"{DEPRECATED_IN_3X_FIELD} Use `updatedAt` instead."),
    )
    user = graphene.Field(
        "saleor.graphql.account.types.User",
        description="The user assigned to the checkout.",
    )
    channel = graphene.Field(
        Channel,
        required=True,
        description="The channel for which checkout was created.",
    )
    billing_address = graphene.Field(
        "saleor.graphql.account.types.Address",
        description="The billing address of the checkout.",
    )
    shipping_address = graphene.Field(
        "saleor.graphql.account.types.Address",
        description="The shipping address of the checkout.",
    )
    note = graphene.String(required=True, description="The note for the checkout.")
    discount = graphene.Field(
        Money,
        description=(
            "The total discount applied to the checkout. "
            "Note: Only discount created via voucher are included in this field."
        ),
    )
    discount_name = graphene.String(
        description="The name of voucher assigned to the checkout."
    )
    translated_discount_name = graphene.String(
        description=(
            "Translation of the discountName field in the language "
            "set in Checkout.languageCode field."
            "Note: this field is set automatically when "
            "Checkout.languageCode is defined; otherwise it's null"
        )
    )
    voucher_code = graphene.String(
        description="The code of voucher assigned to the checkout."
    )
    available_shipping_methods = NonNullList(
        ShippingMethod,
        required=True,
        description="Shipping methods that can be used with this checkout.",
        deprecation_reason=(f"{DEPRECATED_IN_3X_FIELD} Use `shippingMethods` instead."),
    )
    shipping_methods = NonNullList(
        ShippingMethod,
        required=True,
        description="Shipping methods that can be used with this checkout.",
    )
    available_collection_points = NonNullList(
        Warehouse,
        required=True,
        description=(
            "Collection points that can be used for this order." + ADDED_IN_31
        ),
    )
    available_payment_gateways = NonNullList(
        PaymentGateway,
        description="List of available payment gateways.",
        required=True,
    )
    email = graphene.String(description="Email of a customer.", required=False)
    gift_cards = NonNullList(
        GiftCard,
        description="List of gift cards associated with this checkout.",
        required=True,
    )
    is_shipping_required = graphene.Boolean(
        description="Returns True, if checkout requires shipping.", required=True
    )
    quantity = graphene.Int(description="The number of items purchased.", required=True)
    stock_reservation_expires = graphene.DateTime(
        description=(
            "Date when oldest stock reservation for this checkout "
            "expires or null if no stock is reserved." + ADDED_IN_31
        ),
    )
    lines = NonNullList(
        CheckoutLine,
        description=(
            "A list of checkout lines, each containing information about "
            "an item in the checkout."
        ),
        required=True,
    )
    shipping_price = graphene.Field(
        TaxedMoney,
        description=(
            "The price of the shipping, with all the taxes included. Set to 0 when no "
            "delivery method is selected."
        ),
        required=True,
    )
    shipping_method = graphene.Field(
        ShippingMethod,
        description="The shipping method related with checkout.",
        deprecation_reason=(f"{DEPRECATED_IN_3X_FIELD} Use `deliveryMethod` instead."),
    )
    delivery_method = graphene.Field(
        DeliveryMethod,
        description=("The delivery method selected for this checkout." + ADDED_IN_31),
    )
    subtotal_price = graphene.Field(
        TaxedMoney,
        description="The price of the checkout before shipping, with taxes included.",
        required=True,
    )
    tax_exemption = graphene.Boolean(
        description=(
            "Returns True if checkout has to be exempt from taxes." + ADDED_IN_38
        ),
        required=True,
    )
    token = graphene.Field(UUID, description="The checkout's token.", required=True)
    total_price = graphene.Field(
        TaxedMoney,
        description=(
            "The sum of the the checkout line prices, with all the taxes,"
            "shipping costs, and discounts included."
        ),
        required=True,
    )

    total_balance = graphene.Field(
        Money,
        description=(
            "The difference between the paid and the checkout total amount."
            + ADDED_IN_313
            + PREVIEW_FEATURE
        ),
        required=True,
    )

    language_code = graphene.Field(
        LanguageCodeEnum, description="Checkout language code.", required=True
    )
    transactions = NonNullList(
        TransactionItem,
        description=(
            "List of transactions for the checkout. Requires one of the "
            "following permissions: MANAGE_CHECKOUTS, HANDLE_PAYMENTS."
            + ADDED_IN_34
            + PREVIEW_FEATURE
        ),
    )
    display_gross_prices = graphene.Boolean(
        description=(
            "Determines whether checkout prices should include taxes when displayed "
            "in a storefront." + ADDED_IN_39
        ),
        required=True,
    )
    authorize_status = CheckoutAuthorizeStatusEnum(
        description=(
            "The authorize status of the checkout." + ADDED_IN_313 + PREVIEW_FEATURE
        ),
        required=True,
    )
    charge_status = CheckoutChargeStatusEnum(
        description=(
            "The charge status of the checkout." + ADDED_IN_313 + PREVIEW_FEATURE
        ),
        required=True,
    )

    class Meta:
        description = "Checkout object."
        model = models.Checkout
        interfaces = [graphene.relay.Node, ObjectWithMetadata]

    @staticmethod
    def resolve_created(root: models.Checkout, _info: ResolveInfo):
        return root.created_at

    @staticmethod
    def resolve_channel(root: models.Checkout, info):
        return ChannelByIdLoader(info.context).load(root.channel_id)

    @staticmethod
    def resolve_id(root: models.Checkout, _info: ResolveInfo):
        return graphene.Node.to_global_id("Checkout", root.pk)

    @staticmethod
    def resolve_shipping_address(root: models.Checkout, info: ResolveInfo):
        if not root.shipping_address_id:
            return
        return AddressByIdLoader(info.context).load(root.shipping_address_id)

    @staticmethod
    def resolve_billing_address(root: models.Checkout, info: ResolveInfo):
        if not root.billing_address_id:
            return
        return AddressByIdLoader(info.context).load(root.billing_address_id)

    @staticmethod
    def resolve_user(root: models.Checkout, info: ResolveInfo):
        if not root.user_id:
            return None
        requestor = get_user_or_app_from_context(info.context)
        check_is_owner_or_has_one_of_perms(
            requestor, root.user, AccountPermissions.MANAGE_USERS
        )
        return root.user

    @staticmethod
    def resolve_email(root: models.Checkout, _info: ResolveInfo):
        return root.get_customer_email()

    @classmethod
    def resolve_shipping_method(cls, root: models.Checkout, info):
        def with_checkout_info(checkout_info):
            delivery_method = checkout_info.delivery_method_info.delivery_method
            if not delivery_method or not isinstance(
                delivery_method, ShippingMethodData
            ):
                return
            return delivery_method

        return (
            CheckoutInfoByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(with_checkout_info)
        )

    @classmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_shipping_methods(cls, root: models.Checkout, info: ResolveInfo):
        return (
            CheckoutInfoByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(lambda checkout_info: unwrap_lazy(checkout_info.all_shipping_methods))
        )

    @staticmethod
    def resolve_delivery_method(root: models.Checkout, info: ResolveInfo):
        return (
            CheckoutInfoByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(
                lambda checkout_info: checkout_info.delivery_method_info.delivery_method
            )
        )

    @staticmethod
    def resolve_quantity(root: models.Checkout, info: ResolveInfo):
        checkout_info = CheckoutLinesInfoByCheckoutTokenLoader(info.context).load(
            root.token
        )

        def calculate_quantity(lines):
            return sum([line_info.line.quantity for line_info in lines])

        return checkout_info.then(calculate_quantity)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_total_price(root: models.Checkout, info: ResolveInfo):
        def calculate_total_price(data):
            address, lines, checkout_info, manager = data
            taxed_total = calculations.calculate_checkout_total_with_gift_cards(
                manager=manager,
                checkout_info=checkout_info,
                lines=lines,
                address=address,
            )
            return max(taxed_total, zero_taxed_money(root.currency))

        dataloaders = list(get_dataloaders_for_fetching_checkout_data(root, info))
        return Promise.all(dataloaders).then(calculate_total_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_subtotal_price(root: models.Checkout, info: ResolveInfo):
        def calculate_subtotal_price(data):
            address, lines, checkout_info, manager = data
            return calculations.checkout_subtotal(
                manager=manager,
                checkout_info=checkout_info,
                lines=lines,
                address=address,
            )

        dataloaders = list(get_dataloaders_for_fetching_checkout_data(root, info))
        return Promise.all(dataloaders).then(calculate_subtotal_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_shipping_price(root: models.Checkout, info: ResolveInfo):
        def calculate_shipping_price(data):
            address, lines, checkout_info, manager = data
            return calculations.checkout_shipping_price(
                manager=manager,
                checkout_info=checkout_info,
                lines=lines,
                address=address,
            )

        dataloaders = list(get_dataloaders_for_fetching_checkout_data(root, info))
        return Promise.all(dataloaders).then(calculate_shipping_price)

    @staticmethod
    def resolve_lines(root: models.Checkout, info: ResolveInfo):
        return CheckoutLinesByCheckoutTokenLoader(info.context).load(root.token)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_available_shipping_methods(root: models.Checkout, info: ResolveInfo):
        return (
            CheckoutInfoByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(lambda checkout_info: checkout_info.valid_shipping_methods)
        )

    @staticmethod
    @traced_resolver
    def resolve_available_collection_points(root: models.Checkout, info: ResolveInfo):
        def get_available_collection_points(lines):
            return get_valid_collection_points_for_checkout(lines, root.channel_id)

        return (
            CheckoutLinesInfoByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(get_available_collection_points)
        )

    @staticmethod
    @prevent_sync_event_circular_query
    @plugin_manager_promise_callback
    def resolve_available_payment_gateways(
        root: models.Checkout, _info: ResolveInfo, manager
    ):
        return manager.list_payment_gateways(
            currency=root.currency, checkout=root, channel_slug=root.channel.slug
        )

    @staticmethod
    def resolve_gift_cards(root: models.Checkout, _info):
        return root.gift_cards.all()

    @staticmethod
    def resolve_is_shipping_required(root: models.Checkout, info: ResolveInfo):
        def is_shipping_required(lines):
            product_ids = [line_info.product.id for line_info in lines]

            def with_product_types(product_types):
                return any([pt.is_shipping_required for pt in product_types])

            return (
                ProductTypeByProductIdLoader(info.context)
                .load_many(product_ids)
                .then(with_product_types)
            )

        return (
            CheckoutLinesInfoByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(is_shipping_required)
        )

    @staticmethod
    def resolve_language_code(root, _info):
        return LanguageCodeEnum[str_to_enum(root.language_code)]

    @staticmethod
    @traced_resolver
    @load_site_callback
    def resolve_stock_reservation_expires(
        root: models.Checkout, info: ResolveInfo, site
    ):
        if not is_reservation_enabled(site.settings):
            return None

        def get_oldest_stock_reservation_expiration_date(reservations):
            if not reservations:
                return None

            return min(reservation.reserved_until for reservation in reservations)

        return (
            StocksReservationsByCheckoutTokenLoader(info.context)
            .load(root.token)
            .then(get_oldest_stock_reservation_expiration_date)
        )

    @staticmethod
    @one_of_permissions_required(
        [CheckoutPermissions.MANAGE_CHECKOUTS, PaymentPermissions.HANDLE_PAYMENTS]
    )
    def resolve_transactions(root: models.Checkout, info: ResolveInfo):
        return TransactionItemsByCheckoutIDLoader(info.context).load(root.pk)

    @staticmethod
    def resolve_display_gross_prices(root: models.Checkout, info: ResolveInfo):
        tax_config = TaxConfigurationByChannelId(info.context).load(root.channel_id)
        country_code = root.get_country()

        def load_tax_country_exceptions(tax_config):
            tax_configs_per_country = (
                TaxConfigurationPerCountryByTaxConfigurationIDLoader(info.context).load(
                    tax_config.id
                )
            )

            def calculate_display_gross_prices(tax_configs_per_country):
                tax_config_country = next(
                    (
                        tc
                        for tc in tax_configs_per_country
                        if tc.country.code == country_code
                    ),
                    None,
                )
                return get_display_gross_prices(tax_config, tax_config_country)

            return tax_configs_per_country.then(calculate_display_gross_prices)

        return tax_config.then(load_tax_country_exceptions)

    @staticmethod
    def resolve_metadata(root: models.Checkout, info):
        return (
            CheckoutMetadataByCheckoutIdLoader(info.context)
            .load(root.pk)
            .then(
                lambda metadata_storage: MetaResolvers.resolve_metadata(
                    metadata_storage.metadata
                )
                if metadata_storage
                else {}
            )
        )

    @staticmethod
    def resolve_metafield(root: models.Checkout, info, *, key: str):
        return (
            CheckoutMetadataByCheckoutIdLoader(info.context)
            .load(root.pk)
            .then(
                lambda metadata_storage: metadata_storage.metadata.get(key)
                if metadata_storage
                else {}
            )
        )

    @staticmethod
    def resolve_metafields(root: models.Checkout, info, *, keys=None):
        return (
            CheckoutMetadataByCheckoutIdLoader(info.context)
            .load(root.pk)
            .then(
                lambda metadata_storage: _filter_metadata(
                    metadata_storage.metadata, keys
                )
                if metadata_storage
                else {}
            )
        )

    @staticmethod
    def resolve_private_metadata(root: models.Checkout, info):
        return (
            CheckoutMetadataByCheckoutIdLoader(info.context)
            .load(root.pk)
            .then(
                lambda metadata_storage: MetaResolvers.resolve_private_metadata(
                    metadata_storage, info
                )
                if metadata_storage
                else {}
            )
        )

    @staticmethod
    def resolve_private_metafield(root: models.Checkout, info, *, key: str):
        def resolve_private_metafield_with_privilege_check(metadata_storage):
            MetaResolvers.check_private_metadata_privilege(metadata_storage, info)
            return metadata_storage.private_metadata.get(key)

        return (
            CheckoutMetadataByCheckoutIdLoader(info.context)
            .load(root.pk)
            .then(
                lambda metadata_storage: resolve_private_metafield_with_privilege_check(
                    metadata_storage
                )
                if metadata_storage
                else {}
            )
        )

    @staticmethod
    def resolve_private_metafields(root: models.Checkout, info, *, keys=None):
        def resolve_private_metafields_with_privilege(metadata_storage):
            MetaResolvers.check_private_metadata_privilege(metadata_storage, info)
            return _filter_metadata(metadata_storage.private_metadata, keys)

        return (
            CheckoutMetadataByCheckoutIdLoader(info.context)
            .load(root.pk)
            .then(
                lambda metadata_storage: resolve_private_metafields_with_privilege(
                    metadata_storage
                )
                if metadata_storage
                else {}
            )
        )

    @classmethod
    def resolve_type(cls, root: models.Checkout, _info):
        item_type, _ = MetaResolvers.resolve_object_with_metadata_type(
            root.metadata_storage
        )
        return item_type

    @classmethod
    def resolve_updated_at(cls, root: models.Checkout, _info):
        return root.last_change

    @classmethod
    def resolve_authorize_status(cls, root: models.Checkout, info):
        def _resolve_authorize_status(data):
            address, lines, checkout_info, manager, transactions = data
            fetch_checkout_data(
                checkout_info=checkout_info,
                manager=manager,
                lines=lines,
                address=address,
                checkout_transactions=transactions,
            )
            return checkout_info.checkout.authorize_status

        dataloaders = list(get_dataloaders_for_fetching_checkout_data(root, info))
        dataloaders.append(
            TransactionItemsByCheckoutIDLoader(info.context).load(root.pk)
        )
        return Promise.all(dataloaders).then(_resolve_authorize_status)

    @classmethod
    def resolve_charge_status(cls, root: models.Checkout, info):
        def _resolve_charge_status(data):
            address, lines, checkout_info, manager, transactions = data
            fetch_checkout_data(
                checkout_info=checkout_info,
                manager=manager,
                lines=lines,
                address=address,
                checkout_transactions=transactions,
            )
            return checkout_info.checkout.charge_status

        dataloaders = list(get_dataloaders_for_fetching_checkout_data(root, info))
        dataloaders.append(
            TransactionItemsByCheckoutIDLoader(info.context).load(root.pk)
        )
        return Promise.all(dataloaders).then(_resolve_charge_status)

    @classmethod
    def resolve_total_balance(cls, root: models.Checkout, info):
        def _calculate_total_balance_for_transactions(data):
            address, lines, checkout_info, manager, transactions = data
            taxed_total = calculations.calculate_checkout_total_with_gift_cards(
                manager=manager,
                checkout_info=checkout_info,
                lines=lines,
                address=address,
            )
            checkout_total = max(taxed_total, zero_taxed_money(root.currency))
            total_charged = zero_money(root.currency)
            for transaction in transactions:
                total_charged += transaction.amount_charged
                total_charged += transaction.amount_charge_pending
            return total_charged - checkout_total.gross

        dataloaders = list(get_dataloaders_for_fetching_checkout_data(root, info))
        dataloaders.append(
            TransactionItemsByCheckoutIDLoader(info.context).load(root.pk)
        )
        return Promise.all(dataloaders).then(_calculate_total_balance_for_transactions)


class CheckoutCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_CHECKOUT
        node = Checkout
