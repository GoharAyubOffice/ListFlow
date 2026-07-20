"""Listflow local GUI (Streamlit) — the import/preview/edit screen.

Launch with `listflow gui` (wraps `streamlit run` on this file). Single-user,
localhost only. All heavy lifting is delegated to the tested pipeline/publisher —
this file is presentation plus a thin publish wrapper.
"""

import logging
from decimal import Decimal

import streamlit as st

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers


def _load_settings():
    from listflow.config import load_settings

    return load_settings()


def _run_preview(url: str, *, margin, variant, headed: bool):
    """Live extract + prepare; returns Prepared or raises with a friendly message."""
    from listflow.pipeline import prepare

    return prepare(
        url,
        settings=st.session_state["settings"],
        margin=margin,
        variant=variant or None,
        headed=headed,
    )


def _ebay_client():
    from listflow.ebay.auth import EbayAuth
    from listflow.ebay.client import EbayClient

    settings = st.session_state["settings"]
    return EbayClient(settings, EbayAuth(settings))


def _publish(prepared, *, publish: bool, category_id: str | None,
             existing_offer_id: str | None = None):
    """Same flow as the CLI's _run_publish, returning (result, status).

    existing_offer_id makes the offer step an update instead of a create, so
    re-running for the same SKU never trips eBay's one-offer-per-SKU rule.
    """
    from listflow.ebay.publisher import Publisher, make_sku
    from listflow.storage import Tracker

    settings = st.session_state["settings"]
    product, pricing = prepared.product, prepared.pricing
    sku = make_sku(product.source_id)
    with Tracker.open() as tracker:
        tracker.start(
            sku=sku,
            platform=prepared.platform.value,
            source_url=str(product.source_url),
            source_id=product.source_id,
            title_ebay=product.title_ebay,
            cost=product.base_cost,
            sell_price=pricing.sell_price,
            margin_actual=pricing.margin_actual,
        )
        publisher = Publisher(_ebay_client(), settings, on_step=tracker.mark_step)
        try:
            result = publisher.publish(
                product, pricing, publish=publish, category_id=category_id or None,
                existing_offer_id=existing_offer_id,
            )
        except Exception as exc:
            tracker.finish(sku, status="failed", notes=str(exc))
            raise
        status = "published" if result.listing_id else "draft"
        tracker.finish(
            sku, status=status, offer_id=result.offer_id, listing_id=result.listing_id
        )
    return result, status


def _publish_variations(prepared, *, publish: bool, force: bool):
    """Publish all in-stock variants as one multi-variation listing."""
    from listflow.ebay.publisher import Publisher, make_sku
    from listflow.storage import Tracker

    settings = st.session_state["settings"]
    product = prepared.product
    group_key = make_sku(product.source_id)
    variant_pricings = [(o.variant, o.pricing) for o in prepared.variant_offers]
    cheapest = min(prepared.variant_offers, key=lambda o: o.pricing.sell_price)
    with Tracker.open() as tracker:
        tracker.start(
            sku=group_key, platform=prepared.platform.value,
            source_url=str(product.source_url), source_id=product.source_id,
            title_ebay=product.title_ebay, cost=product.base_cost,
            sell_price=cheapest.pricing.sell_price,
            margin_actual=cheapest.pricing.margin_actual,
        )
        publisher = Publisher(_ebay_client(), settings, on_step=tracker.mark_step)
        try:
            result = publisher.publish_variations(
                product, variant_pricings, publish=publish,
                category_id=st.session_state.get("opt_category", "") or None,
                force_below_floor=force,
            )
        except Exception as exc:
            tracker.finish(group_key, status="failed", notes=str(exc))
            raise
        status = "published" if result.listing_id else "draft"
        tracker.finish(group_key, status=status, offer_id=result.offer_id,
                       listing_id=result.listing_id)
    return result, status


def _publish_existing_draft(sku: str, offer_id: str) -> str | None:
    """One API call: publish an already-created draft offer; returns the listingId."""
    from listflow.storage import Tracker

    response = _ebay_client().post(f"/sell/inventory/v1/offer/{offer_id}/publish")
    listing_id = response.json().get("listingId")
    with Tracker.open() as tracker:
        tracker.finish(sku, status="published", offer_id=offer_id, listing_id=listing_id)
    return listing_id


def _delete_listing(sku: str) -> dict:
    """End + delete a listing/draft, then mark it killed in the tracker."""
    from listflow.ebay.publisher import Publisher
    from listflow.storage import Tracker

    settings = st.session_state["settings"]
    publisher = Publisher(_ebay_client(), settings)
    summary = publisher.delete_listing(sku)
    with Tracker.open() as tracker:
        if tracker.get(sku):
            tracker.set_status(sku, "killed", notes="deleted via GUI")
    return summary


def _listing_url(listing_id: str) -> str:
    env = st.session_state["settings"].ebay_env
    base = "sandbox.ebay.com" if env == "sandbox" else "ebay.co.uk"
    return f"https://www.{base}/itm/{listing_id}"


def _flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


def _apply_edits(prepared) -> list[str]:
    """Copy the edit widgets back onto the product; return a list of problems."""
    from listflow.content import (
        EBAY_TITLE_LIMIT,
        ForbiddenTokenError,
        validate_forbidden,
    )

    product = prepared.product
    problems: list[str] = []

    title = st.session_state.get("edit_title", "").strip()
    if not title:
        problems.append("Title is empty.")
    elif len(title) > EBAY_TITLE_LIMIT:
        problems.append(f"Title is {len(title)} chars — the eBay limit is {EBAY_TITLE_LIMIT}.")
    else:
        product.title_ebay = title

    description = st.session_state.get("edit_desc", "").strip()
    if not description:
        problems.append("Description is empty.")
    else:
        product.description_html = description

    selected = [
        asset
        for i, asset in enumerate(st.session_state["all_images"])
        if st.session_state.get(f"img_{i}", True)
    ]
    if not selected:
        problems.append("Select at least one image.")
    else:
        product.images = selected

    if not problems:
        try:
            validate_forbidden(product)
        except ForbiddenTokenError as exc:
            problems.append(f"Forbidden supplier token: {exc}")
    return problems


# ---------------------------------------------------------------- rendering


def _render_price(pricing) -> None:
    floor_ok = pricing.passes_floor
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Source cost", f"£{pricing.cost}")
    col2.metric("Sell price", f"£{pricing.sell_price}")
    col3.metric("Net profit (est)", f"£{pricing.net_profit_est}")
    col4.metric(
        "Margin",
        f"{Decimal(pricing.margin_actual) * 100:.2f}%",
        delta="above floor" if floor_ok else "BELOW 20% FLOOR",
        delta_color="normal" if floor_ok else "inverse",
    )
    if not floor_ok:
        st.error(
            "Margin is below the 20% floor — importing is blocked unless you tick "
            "'Force below-floor import' in the sidebar."
        )


def _render_editor(prepared) -> None:
    from listflow.content import EBAY_TITLE_LIMIT

    product = prepared.product

    st.subheader("2 · Review & edit")
    _render_price(prepared.pricing)

    title = st.text_input(
        f"Listing title (max {EBAY_TITLE_LIMIT} chars)",
        key="edit_title",
        max_chars=EBAY_TITLE_LIMIT,
    )
    st.caption(f"{len(title)}/{EBAY_TITLE_LIMIT} characters")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Description (eBay-safe HTML)**")
        st.text_area(
            "Description HTML",
            key="edit_desc",
            height=260,
            label_visibility="collapsed",
        )
        with st.expander("Preview rendered description"):
            st.html(st.session_state.get("edit_desc", ""))
        if product.item_specifics:
            st.markdown("**Item specifics**")
            st.table(
                [{"Aspect": k, "Value": v} for k, v in product.item_specifics.items()]
            )
        if prepared.variant_offers:
            n = len(prepared.variant_offers)
            prices = sorted(o.pricing.sell_price for o in prepared.variant_offers)
            st.markdown(f"**Variants** — {n} in stock, £{prices[0]}–£{prices[-1]}")
            st.checkbox(
                f"List all {n} variants as one multi-variation listing "
                "(buyer picks on the listing)",
                key="opt_all_variants",
            )
            with st.expander("See variants"):
                st.table([
                    {
                        **o.variant.attributes,
                        "cost": f"£{o.variant.source_price}",
                        "sell": f"£{o.pricing.sell_price}",
                        "floor": "ok" if o.pricing.passes_floor else "below",
                    }
                    for o in prepared.variant_offers
                ])
        elif product.variants:
            st.info(f"{len(product.variants)} variants found, but none are in stock.")

    with right:
        st.markdown("**Images** — untick any you don't want")
        images = st.session_state["all_images"]
        cols_per_row = 3
        for start in range(0, len(images), cols_per_row):
            row = st.columns(cols_per_row)
            for offset, asset in enumerate(images[start : start + cols_per_row]):
                i = start + offset
                with row[offset]:
                    st.image(str(asset.source_url), width="stretch")
                    st.checkbox(f"Image {i + 1}", key=f"img_{i}")
        selected_count = sum(
            1 for i in range(len(images)) if st.session_state.get(f"img_{i}", True)
        )
        st.caption(f"{selected_count}/{len(images)} selected")

    st.divider()
    st.subheader("3 · Import to eBay")

    draft = st.session_state.get("draft_offer")
    if draft:
        st.success(f"Draft ready — offer **{draft['offer_id']}** (SKU {draft['sku']})")
        st.caption(
            "ℹ️ eBay's Seller Hub 'Drafts' tab only shows drafts made with eBay's own "
            "listing form — API-created drafts like this one never appear there "
            "(an eBay platform limitation). This draft is tracked here and in "
            "`listflow list`, and publishes with one click below."
        )
        b1, b2, _spacer = st.columns([1, 1, 2])
        if b1.button("🚀 Publish this draft", type="primary", width="stretch"):
            with st.spinner("Publishing the draft offer…"):
                try:
                    listing_id = _publish_existing_draft(draft["sku"], draft["offer_id"])
                except Exception as exc:
                    st.error(f"Publish failed: {exc}")
                    return
            st.session_state.pop("draft_offer", None)
            _flash(
                "success",
                f"PUBLISHED ✓ SKU {draft['sku']} — live listing: "
                f"{_listing_url(listing_id) if listing_id else '(no id returned)'}",
            )
            st.rerun()
        if b2.button("📝 Update draft with edits", width="stretch"):
            problems = _apply_edits(prepared)
            if problems:
                for problem in problems:
                    st.error(problem)
                return
            with st.spinner("Updating the draft offer…"):
                try:
                    result, _status = _publish(
                        prepared,
                        publish=False,
                        category_id=st.session_state.get("opt_category", ""),
                        existing_offer_id=draft["offer_id"],
                    )
                except Exception as exc:
                    st.error(f"Update failed: {exc}")
                    return
            _flash("success", f"Draft updated ✓ — offer {result.offer_id}")
            st.rerun()
        return

    all_variants = bool(st.session_state.get("opt_all_variants"))
    b1, b2, _spacer = st.columns([1, 1, 2])
    draft_label = "📝 Create variation draft" if all_variants else "📝 Create draft"
    publish_label = "🚀 Publish all variants" if all_variants else "🚀 Publish live"
    draft_clicked = b1.button(draft_label, type="secondary", width="stretch")
    publish_clicked = b2.button(publish_label, type="primary", width="stretch")

    if draft_clicked or publish_clicked:
        force = st.session_state.get("opt_force", False)
        if not all_variants and not prepared.pricing.passes_floor and not force:
            st.error("Below the 20% margin floor — tick 'Force below-floor import' to override.")
            return
        problems = _apply_edits(prepared)
        if problems:
            for problem in problems:
                st.error(problem)
            return
        label = "Publishing…" if publish_clicked else "Creating draft…"
        with st.spinner(label):
            try:
                if all_variants:
                    result, status = _publish_variations(
                        prepared, publish=publish_clicked, force=force
                    )
                else:
                    result, status = _publish(
                        prepared,
                        publish=publish_clicked,
                        category_id=st.session_state.get("opt_category", ""),
                    )
            except Exception as exc:
                st.error(f"Import failed: {exc}")
                st.info("State was saved — you can resume with `listflow retry <sku>`.")
                return
        if all_variants:
            if status == "published":
                _flash("success", f"PUBLISHED ✓ {result.sku} — {_listing_url(result.listing_id)}")
            else:
                _flash(
                    "success",
                    f"Variation DRAFT ✓ {result.sku} — click '🚀 Publish all variants' "
                    "to go live.",
                )
            st.rerun()
        elif status == "draft":
            st.session_state["draft_offer"] = {"sku": result.sku, "offer_id": result.offer_id}
            _flash("success", f"DRAFT ✓ SKU {result.sku} — offer {result.offer_id}")
            st.rerun()
        else:
            _flash(
                "success",
                f"PUBLISHED ✓ SKU {result.sku} — live listing: "
                f"{_listing_url(result.listing_id) if result.listing_id else result.offer_id}",
            )
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="Listflow", page_icon="📦", layout="wide")
    logging.basicConfig(level=logging.INFO)

    try:
        if "settings" not in st.session_state:
            st.session_state["settings"] = _load_settings()
    except Exception as exc:
        st.error(f"Configuration problem: {exc}")
        st.stop()
    settings = st.session_state["settings"]

    st.title("📦 Listflow")
    st.caption(
        f"AliExpress / Amazon → eBay · environment: **{settings.ebay_env}** · "
        f"marketplace {settings.marketplace_id}"
    )

    with st.sidebar:
        st.header("Options")
        margin_pct = st.slider(
            "Target margin %", min_value=5, max_value=60,
            value=int(settings.margin * 100), step=1,
        )
        st.text_input("Variant selector (optional)", key="opt_variant",
                      placeholder="Colour=Red,Size=XL")
        st.text_input("eBay category id (optional)", key="opt_category",
                      placeholder="auto-suggested if empty")
        st.checkbox("Visible browser (AliExpress)", key="opt_headed", value=True)
        st.checkbox("Force below-floor import", key="opt_force", value=False)
        st.divider()
        st.caption(
            "Amazon → fast postage policy · AliExpress → slow (10-day) policy. "
            "Prices are Decimal end-to-end; forbidden supplier tokens are stripped "
            "and validated."
        )

    if "flash" in st.session_state:
        kind, message = st.session_state.pop("flash")
        (st.success if kind == "success" else st.error)(message)

    tab_import, tab_listings = st.tabs(["📥 Import", "📋 My listings"])
    with tab_import:
        _render_import(margin_pct)
    with tab_listings:
        _render_listings()


def _render_import(margin_pct: int) -> None:
    st.subheader("1 · Product URL")
    url_col, button_col = st.columns([5, 1])
    url = url_col.text_input(
        "Product URL", key="url", label_visibility="collapsed",
        placeholder="https://www.amazon.co.uk/dp/… or https://www.aliexpress.com/item/…",
    )
    preview_clicked = button_col.button("🔍 Preview", type="primary", width="stretch")

    if preview_clicked:
        if not url.strip():
            st.warning("Paste a product URL first.")
        else:
            for key in list(st.session_state):
                if key.startswith("img_") or key in (
                    "edit_title", "edit_desc", "done", "draft_offer", "opt_all_variants",
                ):
                    del st.session_state[key]
            with st.spinner("Extracting product (AliExpress opens a browser window)…"):
                try:
                    prepared = _run_preview(
                        url.strip(),
                        margin=Decimal(margin_pct) / 100,
                        variant=st.session_state.get("opt_variant", ""),
                        headed=st.session_state.get("opt_headed", True),
                    )
                except Exception as exc:
                    st.error(f"Extraction failed: {exc}")
                    prepared = None
            if prepared is not None:
                st.session_state["prepared"] = prepared
                st.session_state["all_images"] = list(prepared.product.images)
                st.session_state["edit_title"] = prepared.product.title_ebay
                st.session_state["edit_desc"] = prepared.product.description_html
                for i in range(len(prepared.product.images)):
                    st.session_state[f"img_{i}"] = True

    if "prepared" in st.session_state:
        _render_editor(st.session_state["prepared"])


_STATUS_BADGE = {
    "published": "🟢 live", "draft": "🟡 draft", "failed": "🔴 failed", "killed": "⚫ deleted",
}


def _render_listings() -> None:
    from listflow.storage import Tracker

    with Tracker.open() as tracker:
        rows = tracker.all()
    if not rows:
        st.info("No imports yet — list something from the Import tab.")
        return
    st.caption(
        f"{len(rows)} tracked import(s). Delete ends any live listing and removes the "
        "offer + inventory item(s) from eBay."
    )
    for row in rows:
        sku = row["ebay_sku"]
        status = row["status"]
        c1, c2, c3 = st.columns([5, 2, 2])
        title = row["title_ebay"] or "(untitled)"
        c1.markdown(
            f"**{title[:60]}**  \n`{sku}` · {row['source_platform']} · £{row['sell_price']}"
        )
        if row["ebay_listing_id"]:
            c2.markdown(f"[view listing]({_listing_url(row['ebay_listing_id'])})")
        c2.markdown(_STATUS_BADGE.get(status, status))
        if status != "killed":
            with c3.popover("🗑 Delete"):
                st.write(f"Delete **{sku}**?")
                if status == "published":
                    st.warning("This ends a LIVE listing.")
                if st.button("Yes, delete it", key=f"confirm_del_{sku}", type="primary"):
                    with st.spinner(f"Deleting {sku}…"):
                        try:
                            summary = _delete_listing(sku)
                        except Exception as exc:
                            st.error(f"Delete failed: {exc}")
                            return
                    _flash(
                        "success",
                        f"Deleted {sku} — offers {summary['offers_deleted']}, "
                        f"items {summary['items_deleted']}.",
                    )
                    st.rerun()
        st.divider()


if __name__ == "__main__":
    main()
