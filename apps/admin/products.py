import re
from flask import (
    render_template,
    redirect,
    flash,
    request,
    abort,
    url_for,
    current_app as app,
)
from flask_login import current_user
from flask_mail import Message

from sqlalchemy.sql.functions import func
from sqlalchemy import not_

from main import db, mail
from models.user import User
from models.product import (
    ProductGroup,
    Price,
    PriceTier,
    Product,
    ProductView,
    ProductViewProduct,
    random_voucher,
    Voucher,
)
from models.purchase import Purchase, PurchaseTransfer

from models import event_year
from . import admin
from .forms import (
    EditProductForm,
    NewProductForm,
    NewProductGroupForm,
    EditProductGroupForm,
    CopyProductGroupForm,
    NewPriceTierForm,
    EditPriceTierForm,
    ModifyPriceTierForm,
    NewProductViewForm,
    EditProductViewForm,
    AddProductViewProductForm,
    NewVoucherForm,
    EditVoucherForm,
    BulkVoucherEmailForm,
)


def get_user_purchases(query):
    return (
        query.join(Purchase)
        .join(Purchase.owner)
        .filter(Purchase.is_paid_for)
        .with_entities(User, func.count("*"))
        .group_by(User)
        .order_by(User.id)
    )


@admin.route("/products")
def products():
    root_groups = (
        ProductGroup.query.filter_by(parent_id=None).order_by(ProductGroup.id).all()
    )
    return render_template("admin/products/overview.html", root_groups=root_groups)


@admin.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def edit_product(product_id):
    form = EditProductForm()

    product = Product.query.get_or_404(product_id)
    if form.validate_on_submit():
        app.logger.info("%s editing product %s", current_user.name, product_id)
        form.update_product(product)
        db.session.commit()
        return redirect(url_for(".product_details", product_id=product_id))

    form.init_with_product(product)
    return render_template(
        "admin/products/edit-product.html", product=product, form=form
    )


@admin.route(
    "/products/group/<int:parent_id>/new",
    defaults={"copy_id": None},
    methods=["GET", "POST"],
)
@admin.route(
    "/products/<int:copy_id>/clone",
    defaults={"parent_id": None},
    methods=["GET", "POST"],
)
def new_product(copy_id, parent_id):
    if parent_id:
        parent = ProductGroup.query.get_or_404(parent_id)
    else:
        parent = Product.query.get(copy_id).parent

    form = NewProductForm()

    if form.validate_on_submit():
        product = Product(
            parent=parent,
            name=form.name.data,
            display_name=form.display_name.data,
            expires=form.expires.data or None,
            capacity_max=form.capacity_max.data or None,
            description=form.description.data or None,
        )
        app.logger.info("%s adding new Product %s", current_user.name, product)
        db.session.add(product)
        db.session.commit()
        flash("Your new ticket product has been created")
        return redirect(url_for(".product_details", product_id=product.id))

    if copy_id:
        form.init_with_product(Product.query.get(copy_id))

    return render_template(
        "admin/products/new-product.html", parent=parent, product_id=copy_id, form=form
    )


@admin.route("/products/<int:product_id>")
def product_details(product_id):
    product = Product.query.get_or_404(product_id)
    user_purchases = get_user_purchases(
        PriceTier.query.filter_by(product_id=product_id)
    )

    return render_template(
        "admin/products/product-details.html",
        product=product,
        user_purchases=user_purchases,
    )


@admin.route("/products/<int:product_id>/new-tier", methods=["GET", "POST"])
def new_price_tier(product_id):
    form = NewPriceTierForm()
    product = Product.query.get_or_404(product_id)

    if form.validate_on_submit():
        pt = PriceTier(form.name.data, personal_limit=form.personal_limit.data)
        pt.prices = [
            Price("GBP", form.price_gbp.data),
            Price("EUR", form.price_eur.data),
        ]

        # Only activate this price tier if it's the first one added.
        pt.active = len(product.price_tiers) == 0
        product.price_tiers.append(pt)
        db.session.commit()
        return redirect(url_for(".price_tier_details", tier_id=pt.id))

    return render_template(
        "admin/products/price-tier-new.html", product=product, form=form
    )


@admin.route("/products/price-tiers/<int:tier_id>")
def price_tier_details(tier_id):
    tier = PriceTier.query.get_or_404(tier_id)
    form = ModifyPriceTierForm()
    user_purchases = get_user_purchases(PriceTier.query.filter_by(id=tier.id))
    return render_template(
        "admin/products/price-tier-details.html",
        tier=tier,
        form=form,
        user_purchases=user_purchases,
    )


@admin.route("/products/price-tiers/<int:tier_id>", methods=["POST"])
def price_tier_modify(tier_id):
    form = ModifyPriceTierForm()
    tier = PriceTier.query.get_or_404(tier_id)

    if form.validate_on_submit():
        if form.delete.data and tier.unused:
            db.session.delete(tier)
            db.session.commit()
            flash("Price tier deleted")
            return redirect(url_for(".product_details", product_id=tier.product_id))

        if form.activate.data:
            for t in tier.parent.price_tiers:
                t.active = False

            tier.active = True
            db.session.commit()
            flash("Price tier activated")
            return redirect(url_for(".price_tier_details", tier_id=tier.id))

        if form.deactivate.data:
            tier.active = False
            db.session.commit()
            flash("Price tier deactivated")
            return redirect(url_for(".price_tier_details", tier_id=tier.id))

    return abort(401)


@admin.route("/products/price-tiers/<int:tier_id>/edit", methods=["GET", "POST"])
def price_tier_edit(tier_id):
    tier = PriceTier.query.get_or_404(tier_id)
    form = EditPriceTierForm(obj=tier)

    if form.validate_on_submit():
        tier.name = form.name.data
        tier.personal_limit = form.personal_limit.data

        # Update prices but only if price tier has no purchases.
        price_gbp = tier.get_price("GBP")
        price_eur = tier.get_price("EUR")

        if tier.purchase_count == 0:
            if form.price_gbp.data != price_gbp.value:
                db.session.delete(price_gbp)
                tier.prices.append(Price("GBP", form.price_gbp.data))

            if form.price_eur.data != price_eur.value:
                db.session.delete(price_eur)
                tier.prices.append(Price("EUR", form.price_eur.data))

        db.session.commit()

        return redirect(url_for(".price_tier_details", tier_id=tier.id))

    form.price_gbp.data = tier.get_price("GBP").value
    form.price_eur.data = tier.get_price("EUR").value
    return render_template("admin/products/price-tier-edit.html", tier=tier, form=form)


@admin.route("/products/group/<int:group_id>")
def product_group_details(group_id):
    group = ProductGroup.query.get_or_404(group_id)

    def get_all_child_groups(group):
        return [group] + [g for c in group.children for g in get_all_child_groups(c)]

    group_ids = [g.id for g in get_all_child_groups(group)]
    price_tiers = ProductGroup.query.filter(ProductGroup.id.in_(group_ids)).join(
        Product, PriceTier
    )
    user_purchases = get_user_purchases(price_tiers)
    return render_template(
        "admin/products/product-group-details.html",
        group=group,
        user_purchases=user_purchases,
    )


@admin.route("/products/group/new", methods=["GET", "POST"])
def product_group_new():
    if request.args.get("parent"):
        parent = ProductGroup.query.get_or_404(request.args.get("parent"))
    else:
        parent = None

    form = NewProductGroupForm()

    if form.validate_on_submit():
        pg = ProductGroup(
            form.type.data,
            parent,
            parent.id if parent else None,
            name=form.name.data,
            capacity_max=form.capacity_max.data,
            expires=form.expires.data,
        )
        app.logger.info("%s adding new ProductGroup %s", current_user.name, pg)
        db.session.add(pg)
        db.session.commit()
        flash("ProductGroup created")
        return redirect(url_for(".product_group_details", group_id=pg.id))

    return render_template(
        "admin/products/product-group-edit.html", method="new", parent=parent, form=form
    )


@admin.route("/products/group/<int:group_id>/edit", methods=["GET", "POST"])
def product_group_edit(group_id):
    group = ProductGroup.query.get_or_404(group_id)
    form = EditProductGroupForm()
    if form.validate_on_submit():
        group = form.update_pg(group)
        db.session.add(group)
        db.session.commit()
        flash("ProductGroup updated")
        return redirect(url_for(".product_group_details", group_id=group.id))

    form.init_with_pg(group)

    return render_template(
        "admin/products/product-group-edit.html", method="edit", group=group, form=form
    )


@admin.route("/products/group/<int:group_id>/copy", methods=["GET", "POST"])
def product_group_copy(group_id):
    group = ProductGroup.query.get_or_404(group_id)
    form = CopyProductGroupForm()

    if group.children:
        # No recursive copying
        abort(404)

    if request.method != "POST":
        form.name.data = group.name + " (copy)"

    if group.capacity_max is None:
        del form.capacity_max_required

    else:
        del form.capacity_max

    if form.validate_on_submit():
        capacity_max = None
        if group.capacity_max is not None:
            capacity_max = form.capacity_max_required.data
            group.capacity_max -= capacity_max

        new_group = ProductGroup(
            type=group.type,
            name=form.name.data,
            capacity_max=capacity_max,
            expires=form.expires.data,
        )
        for product in group.products:
            new_product = Product(
                name=product.name,
                display_name=product.display_name,
                description=product.description,
            )
            new_group.products.append(new_product)
            for pt in product.price_tiers:
                if not pt.active and not form.include_inactive.data:
                    continue

                new_pt = PriceTier(
                    name=pt.name, personal_limit=pt.personal_limit, active=pt.active
                )
                new_product.price_tiers.append(new_pt)
                for price in pt.prices:
                    new_price = Price(price.currency, price.value)
                    new_pt.prices.append(new_price)

        new_group.parent = group.parent
        db.session.add(new_group)
        db.session.commit()
        flash("ProductGroup copied")
        return redirect(url_for(".product_group_details", group_id=new_group.id))

    return render_template(
        "admin/products/product-group-copy.html", group=group, form=form
    )


@admin.route("/transfers")
def purchase_transfers():
    transfer_logs = PurchaseTransfer.query.all()
    return render_template(
        "admin/products/purchase-transfers.html", transfers=transfer_logs
    )


@admin.route("/tees")
def tees():
    purchases = (
        ProductGroup.query.filter_by(type="tees")
        .join(Product, Purchase, Purchase.owner)
        .group_by(User.id, Product.id)
        .with_entities(User, Product, func.count(Purchase.id))
        .filter(Purchase.is_paid_for == True)  # noqa: E712
        .order_by(User.name, Product.name)
    )

    return render_template("admin/products/tee-purchases.html", purchases=purchases)


@admin.route("/product_views")
def product_views():
    view_counts = (
        ProductView.query.outerjoin(ProductView.product_view_products)
        .with_entities(ProductView, func.count(ProductViewProduct.view_id))
        .group_by(ProductView)
        .order_by(ProductView.id)
        .all()
    )
    return render_template("admin/products/views.html", view_counts=view_counts)


@admin.route("/product_views/new", methods=["GET", "POST"])
def product_view_new():
    form = NewProductViewForm()

    if form.validate_on_submit():
        view = ProductView(
            type=form.type.data,
            name=form.name.data,
            cfp_accepted_only=form.cfp_accepted_only.data,
            vouchers_only=form.vouchers_only.data,
        )
        app.logger.info("Adding new ProductView %s", view.name)
        db.session.add(view)
        db.session.commit()
        flash("ProductView created")
        return redirect(url_for(".product_view", view_id=view.id))

    return render_template("admin/products/view-new.html", form=form)


@admin.route("/product_views/<int:view_id>", methods=["GET", "POST"])
def product_view(view_id):
    view = ProductView.query.get_or_404(view_id)

    form = EditProductViewForm(obj=view)
    if request.method != "POST":
        # Empty form - populate pvps
        for pvp in view.product_view_products:
            form.pvps.append_entry()
            f = form.pvps[-1]
            f.product_id.data = pvp.product_id

            f.order.data = pvp.order

    pvp_dict = {pvp.product_id: pvp for pvp in view.product_view_products}
    for f in form.pvps:
        pvp = pvp_dict[f.product_id.data]
        pvp._field = f

    if form.validate_on_submit():
        if form.update.data:
            view.name = form.name.data
            view.type = form.type.data
            view.cfp_accepted_only = form.cfp_accepted_only.data
            view.vouchers_only = form.vouchers_only.data

            for f in form.pvps:
                pvp_dict[f.product_id.data].order = f.order.data

        else:
            for f in form.pvps:
                if f.delete.data:
                    pvp = pvp_dict[f.product_id.data]
                    db.session.delete(pvp)

        db.session.commit()

    active_vouchers = Voucher.query.filter_by(view=view).filter(
        not_(Voucher.expiry.isnot(None) & (Voucher.expiry < func.now()))
        & not_(Voucher.is_used)
    )
    stats = {
        "active": active_vouchers.count(),
        "total_tickets": active_vouchers.with_entities(
            func.sum(Voucher.tickets_remaining)
        ).scalar(),
    }

    return render_template(
        "admin/products/view-edit.html", view=view, form=form, voucher_stats=stats
    )


@admin.route("/product_views/<int:view_id>/add", methods=["GET", "POST"])
@admin.route("/product_views/<int:view_id>/add/<int:group_id>", methods=["GET", "POST"])
@admin.route(
    "/product_views/<int:view_id>/add/<int:group_id>/<int:product_id>",
    methods=["GET", "POST"],
)
def product_view_add(view_id, group_id=None, product_id=None):
    view = ProductView.query.get_or_404(view_id)
    form = AddProductViewProductForm()

    root_groups = (
        ProductGroup.query.filter_by(parent_id=None).order_by(ProductGroup.id).all()
    )

    if product_id is not None:
        product = Product.query.get(product_id)
    else:
        product = None
        del form.add_product

    if group_id is not None:
        group = ProductGroup.query.get(group_id)
    else:
        group = None
        del form.add_all_products

    if form.validate_on_submit():
        if form.add_all_products.data:
            for product in group.products:
                ProductViewProduct(view, product)
            db.session.commit()

        elif form.add_product.data:
            ProductViewProduct(view, product)
            db.session.commit()

        return redirect(url_for(".product_view", view_id=view.id))

    return render_template(
        "admin/products/view-add-products.html",
        view=view,
        form=form,
        root_groups=root_groups,
        add_group=group,
        add_product=product,
    )


@admin.route("/product_views/<int:view_id>/voucher")
def product_view_voucher_list(view_id):
    view = ProductView.query.get_or_404(view_id)
    vouchers = Voucher.query.filter_by(view=view)

    if not request.args.get("used"):
        vouchers = vouchers.filter(not_(Voucher.is_used))

    if not request.args.get("expired"):
        vouchers = vouchers.filter(
            not_(Voucher.expiry.isnot(None) & (Voucher.expiry < func.now()))
        )

    return render_template(
        "admin/products/view-vouchers.html", view=view, vouchers=vouchers
    )


@admin.route("/product_views/<int:view_id>/voucher/<string:voucher_code>")
def product_view_voucher_detail(view_id, voucher_code):
    view = ProductView.query.get_or_404(view_id)
    voucher = Voucher.get_by_code(voucher_code)
    if voucher is None:
        abort(404)
    return render_template(
        "admin/products/view-voucher.html", view=view, voucher=voucher
    )


@admin.route("/product_views/<int:view_id>/voucher/add", methods=["GET", "POST"])
def product_view_add_voucher(view_id):
    view = ProductView.query.get_or_404(view_id)
    form = NewVoucherForm()

    if form.validate_on_submit():
        voucher = Voucher(
            view,
            code=form.voucher.data,
            expiry=form.expires.data,
            purchases_remaining=form.num_purchases.data,
            tickets_remaining=form.num_tickets.data,
        )
        db.session.commit()
        flash("Voucher successfully created")
        return redirect(
            url_for(
                ".product_view_voucher_detail",
                view_id=view_id,
                voucher_code=voucher.code,
            )
        )

    return render_template("admin/products/view-add-voucher.html", view=view, form=form)


@admin.route(
    "/product_views/<int:view_id>/voucher/<string:voucher_code>/edit",
    methods=["GET", "POST"],
)
def product_view_edit_voucher(view_id, voucher_code):
    view = ProductView.query.get_or_404(view_id)
    voucher = Voucher.get_by_code(voucher_code)
    if voucher is None:
        abort(404)

    form = EditVoucherForm()
    if form.validate_on_submit():
        form.update_voucher(voucher)
        db.session.commit()
        flash("Voucher updated")
        return redirect(
            url_for(
                ".product_view_voucher_detail",
                view_id=view_id,
                voucher_code=voucher_code,
            )
        )

    form.init_with_voucher(voucher)

    return render_template(
        "admin/products/view-edit-voucher.html", view=view, voucher=voucher, form=form
    )


@admin.route("/product_views/<int:view_id>/voucher/bulk_add", methods=["GET", "POST"])
def product_view_bulk_add_vouchers_by_email(view_id):
    view = ProductView.query.get_or_404(view_id)
    form = BulkVoucherEmailForm()

    if form.validate_on_submit():
        emails = set(e.strip() for e in re.split(r"[\s,]+", form.emails.data))

        if len(emails) > 250:
            flash("More than 250 emails provided. Please submit <250 at a time.")
            return redirect(
                url_for(".product_view_bulk_add_vouchers_by_email", view_id=view_id)
            )

        added = existing = 0

        for email in emails:
            if Voucher.query.filter_by(email=email).first():
                existing += 1
                continue

            voucher = Voucher(
                view,
                code=random_voucher(),
                email=email,
                expiry=form.expires.data,
                purchases_remaining=form.num_purchases.data,
                tickets_remaining=form.num_tickets.data,
            )

            msg = Message(
                f"Your voucher for Electromagnetic Field {event_year()}",
                sender=app.config["TICKETS_EMAIL"],
                recipients=[email],
            )

            msg.body = render_template("emails/volunteer-voucher.txt", voucher=voucher)

            app.logger.info("Emailing %s volunteer voucher: %s", email, voucher.code)
            db.session.commit()
            mail.send(msg)
            added += 1

        flash(f"{added} vouchers added, {existing} duplicates skipped.")

        return redirect(
            url_for(".product_view_bulk_add_vouchers_by_email", view_id=view_id)
        )

    return render_template(
        "admin/products/view-bulk-add-voucher.html", view=view, form=form
    )
