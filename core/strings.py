# core/strings.py
"""
Fixed bot-facing UI strings, in English and Hindi -- every message
core/commerce_flow.py sends that isn't merchant-configured content (business
name, product names/descriptions, which are never auto-translated and stay
exactly as entered).

Lookup-based by design (a dict per key, not scattered inline if/else): every
send site does `t("some_key", lang, **kwargs)` instead of hardcoding a
literal string. Adding a third language later means adding one more entry
per key here, not touching core/commerce_flow.py at all.

`{unit}`/`{verb}` kwargs exist only for the English templates that need
English pluralization ("item"/"items", "was"/"were") -- Hindi doesn't
inflect the same way, so the Hindi templates simply don't reference those
placeholders. str.format() ignores unused kwargs, so passing them
unconditionally to every call is harmless.
"""

LANG_EN = "en"
LANG_HI = "hi"
DEFAULT_LANG = LANG_EN

LANGUAGE_NAMES = {LANG_EN: "English", LANG_HI: "हिन्दी"}

_STRINGS: dict[str, dict[str, str]] = {
    "welcome": {
        LANG_EN: "Hi! Welcome to {tenant_name}. What would you like to do?",
        LANG_HI: "नमस्ते! {tenant_name} में आपका स्वागत है। आप क्या करना चाहेंगे?",
    },
    "main_menu_button": {
        LANG_EN: "Main Menu",
        LANG_HI: "मुख्य मेनू",
    },
    "menu_shop_now": {
        LANG_EN: "Shop Now",
        LANG_HI: "अभी खरीदें",
    },
    "menu_my_orders": {
        LANG_EN: "My Orders",
        LANG_HI: "मेरे ऑर्डर",
    },
    "menu_track_order": {
        LANG_EN: "Track Order",
        LANG_HI: "ऑर्डर ट्रैक करें",
    },
    "menu_offers": {
        LANG_EN: "Offers",
        LANG_HI: "ऑफ़र",
    },
    "menu_account": {
        LANG_EN: "Account",
        LANG_HI: "खाता",
    },
    "menu_talk_to_us": {
        LANG_EN: "Talk to Us",
        LANG_HI: "हमसे बात करें",
    },
    "account_coming_soon": {
        LANG_EN: "Account management is coming soon!",
        LANG_HI: "खाता प्रबंधन जल्द ही आ रहा है!",
    },
    "talk_to_us_coming_soon": {
        LANG_EN: "Talk to Us is coming soon — for now, please reach out to us directly.",
        LANG_HI: "हमसे बात करने की सुविधा जल्द ही आ रही है — फ़िलहाल कृपया सीधे हमसे संपर्क करें।",
    },
    "please_choose": {
        LANG_EN: "Please choose an option from the list above",
        LANG_HI: "कृपया ऊपर दी गई सूची में से कोई विकल्प चुनें",
    },
    "no_products_available": {
        LANG_EN: "Sorry, we don't have any products available right now.",
        LANG_HI: "क्षमा करें, फ़िलहाल हमारे पास कोई उत्पाद उपलब्ध नहीं है।",
    },
    "browse_products_body": {
        LANG_EN: "Browse our products:",
        LANG_HI: "हमारे उत्पाद देखें:",
    },
    "view_products_button": {
        LANG_EN: "View Products",
        LANG_HI: "उत्पाद देखें",
    },
    "see_more_products": {
        LANG_EN: "See more products",
        LANG_HI: "और उत्पाद देखें",
    },
    "category_other": {
        LANG_EN: "Other",
        LANG_HI: "अन्य",
    },
    "category_more_section": {
        LANG_EN: "More",
        LANG_HI: "और",
    },
    "item_unavailable": {
        LANG_EN: "Sorry, that item is no longer available.",
        LANG_HI: "क्षमा करें, यह उत्पाद अब उपलब्ध नहीं है।",
    },
    "add_to_cart_button": {
        LANG_EN: "Add to Cart",
        LANG_HI: "कार्ट में जोड़ें",
    },
    "back_to_products_button": {
        LANG_EN: "Back to Products",
        LANG_HI: "उत्पादों पर वापस जाएं",
    },
    "out_of_stock": {
        LANG_EN: "Sorry, this item is out of stock.",
        LANG_HI: "क्षमा करें, यह उत्पाद स्टॉक में नहीं है।",
    },
    "only_n_left": {
        LANG_EN: "Sorry, only {n} left in stock.",
        LANG_HI: "क्षमा करें, स्टॉक में केवल {n} बचे हैं।",
    },
    "added_to_cart": {
        LANG_EN: "Added {product_name} to your cart.",
        LANG_HI: "{product_name} आपके कार्ट में जोड़ दिया गया।",
    },
    "cart_count_summary": {
        LANG_EN: "Cart ({count} {unit}) — {amount}",
        LANG_HI: "कार्ट ({count} आइटम) — {amount}",
    },
    "view_cart_button": {
        LANG_EN: "View Cart",
        LANG_HI: "कार्ट देखें",
    },
    "continue_shopping_button": {
        LANG_EN: "Continue Shopping",
        LANG_HI: "खरीदारी जारी रखें",
    },
    "cart_empty": {
        LANG_EN: "Your cart is empty.",
        LANG_HI: "आपका कार्ट खाली है।",
    },
    "your_cart_header": {
        LANG_EN: "Your Cart:",
        LANG_HI: "आपका कार्ट:",
    },
    "subtotal_label": {
        LANG_EN: "Subtotal: {amount}",
        LANG_HI: "उप-योग: {amount}",
    },
    "checkout_button": {
        LANG_EN: "Checkout",
        LANG_HI: "चेकआउट करें",
    },
    "your_recent_orders": {
        LANG_EN: "Your recent orders:",
        LANG_HI: "आपके हाल के ऑर्डर:",
    },
    "view_orders_button": {
        LANG_EN: "View Orders",
        LANG_HI: "ऑर्डर देखें",
    },
    "orders_section_title": {
        LANG_EN: "Orders",
        LANG_HI: "ऑर्डर",
    },
    "no_orders_yet": {
        LANG_EN: "You don't have any orders yet.",
        LANG_HI: "आपका अभी तक कोई ऑर्डर नहीं है।",
    },
    "order_number": {
        LANG_EN: "Order #{id}",
        LANG_HI: "ऑर्डर #{id}",
    },
    "order_status_label": {
        LANG_EN: "Status: {status}",
        LANG_HI: "स्थिति: {status}",
    },
    "order_placed_label": {
        LANG_EN: "Placed: {date}",
        LANG_HI: "दिनांक: {date}",
    },
    "order_total_label": {
        LANG_EN: "Total: {amount}",
        LANG_HI: "कुल: {amount}",
    },
    "order_not_found": {
        LANG_EN: "Something went wrong finding that order. Please start over.",
        LANG_HI: "उस ऑर्डर को खोजने में कुछ गड़बड़ हुई। कृपया फिर से शुरू करें।",
    },
    "cart_empty_checkout": {
        LANG_EN: "Your cart is empty. Send any message to start shopping again.",
        LANG_HI: "आपका कार्ट खाली है। खरीदारी फिर से शुरू करने के लिए कोई भी संदेश भेजें।",
    },
    "cart_items_unavailable": {
        LANG_EN: "Sorry, the items in your cart are no longer available. Please start shopping again.",
        LANG_HI: "क्षमा करें, आपके कार्ट में मौजूद उत्पाद अब उपलब्ध नहीं हैं। कृपया फिर से खरीदारी शुरू करें।",
    },
    "order_placed_confirmation": {
        LANG_EN: "Order #{id} placed!",
        LANG_HI: "ऑर्डर #{id} दर्ज हो गया!",
    },
    "items_removed_note": {
        LANG_EN: "Note: {names} went out of stock and {verb} removed from your order.",
        LANG_HI: "नोट: {names} स्टॉक में नहीं रहे और आपके ऑर्डर से हटा दिए गए।",
    },
    "payment_coming_next": {
        LANG_EN: "Payment integration coming next — we'll follow up with a payment link shortly.",
        LANG_HI: "भुगतान की सुविधा जल्द जुड़ेगी — हम शीघ्र ही भुगतान लिंक भेजेंगे।",
    },
    "payment_link_failed": {
        LANG_EN: "We couldn't generate a payment link right now — we'll follow up shortly.",
        LANG_HI: "हम अभी भुगतान लिंक नहीं बना सके — हम जल्द ही संपर्क करेंगे।",
    },
    "pay_here": {
        LANG_EN: "Pay here to confirm your order: {url}",
        LANG_HI: "अपना ऑर्डर पक्का करने के लिए यहां भुगतान करें: {url}",
    },
    "native_order_unavailable": {
        LANG_EN: "Sorry, we couldn't process your order — none of the items are available right now.",
        LANG_HI: "क्षमा करें, हम आपका ऑर्डर पूरा नहीं कर सके — फ़िलहाल कोई भी उत्पाद उपलब्ध नहीं है।",
    },
    "payment_received": {
        LANG_EN: "Payment received! Your order #{id} is confirmed.",
        LANG_HI: "भुगतान प्राप्त हुआ! आपका ऑर्डर #{id} पक्का हो गया है।",
    },
    "payment_failed_retry": {
        LANG_EN: "Your payment for order #{id} didn't go through. You can try again here: {url}",
        LANG_HI: "आपके ऑर्डर #{id} का भुगतान सफल नहीं हुआ। आप यहां फिर से प्रयास कर सकते हैं: {url}",
    },
    "payment_failed_no_link": {
        LANG_EN: "Your payment for order #{id} didn't go through. Please contact us to complete your order.",
        LANG_HI: "आपके ऑर्डर #{id} का भुगतान सफल नहीं हुआ। कृपया अपना ऑर्डर पूरा करने के लिए हमसे संपर्क करें।",
    },
    "payment_problem_no_gateway": {
        LANG_EN: "There was a problem with payment for order #{id}. Please contact us to complete your order.",
        LANG_HI: "आपके ऑर्डर #{id} के भुगतान में समस्या हुई। कृपया अपना ऑर्डर पूरा करने के लिए हमसे संपर्क करें।",
    },
    "payment_link_retry_failed": {
        LANG_EN: "There was a problem generating a new payment link for order #{id}. Please contact us to complete your order.",
        LANG_HI: "ऑर्डर #{id} के लिए नया भुगतान लिंक बनाने में समस्या हुई। कृपया हमसे संपर्क करें।",
    },
    "payment_link_expired_new": {
        LANG_EN: "Your payment link for order #{id} expired. Here's a new one: {url}",
        LANG_HI: "आपके ऑर्डर #{id} का भुगतान लिंक समाप्त हो गया। यह रहा नया लिंक: {url}",
    },
    "audio_not_supported": {
        LANG_EN: "I couldn't process your audio. Could you send it as text instead?",
        LANG_HI: "मैं आपका ऑडियो संसाधित नहीं कर सका। कृपया इसे टेक्स्ट के रूप में भेजें।",
    },
    "nudge_with_link": {
        LANG_EN: "You still have an order waiting — order #{id}. Complete your payment here to confirm it: {url}",
        LANG_HI: "आपका एक ऑर्डर अभी भी लंबित है — ऑर्डर #{id}। इसे पक्का करने के लिए यहां भुगतान करें: {url}",
    },
    "nudge_without_link": {
        LANG_EN: "You still have an order waiting — order #{id}. Send us a message to complete your checkout.",
        LANG_HI: "आपका एक ऑर्डर अभी भी लंबित है — ऑर्डर #{id}। अपना चेकआउट पूरा करने के लिए हमें संदेश भेजें।",
    },
    "order_fulfilled": {
        LANG_EN: "Good news! Your order #{id} has been fulfilled and is on its way.",
        LANG_HI: "अच्छी खबर! आपका ऑर्डर #{id} पूरा हो गया है और जल्द ही आप तक पहुंच जाएगा।",
    },
    "ask_customer_name": {
        LANG_EN: "What name should we use for this order?",
        LANG_HI: "इस ऑर्डर के लिए हम किस नाम का उपयोग करें?",
    },
    "apply_coupon_button": {
        LANG_EN: "Apply Coupon",
        LANG_HI: "कूपन लगाएं",
    },
    "ask_coupon_code": {
        LANG_EN: "Please enter your coupon code.",
        LANG_HI: "कृपया अपना कूपन कोड दर्ज करें।",
    },
    "coupon_error_not_found": {
        LANG_EN: "\"{code}\" isn't a valid coupon code.",
        LANG_HI: "\"{code}\" एक मान्य कूपन कोड नहीं है।",
    },
    "coupon_error_inactive": {
        LANG_EN: "\"{code}\" is no longer active.",
        LANG_HI: "\"{code}\" अब सक्रिय नहीं है।",
    },
    "coupon_error_expired": {
        LANG_EN: "\"{code}\" has expired.",
        LANG_HI: "\"{code}\" की अवधि समाप्त हो गई है।",
    },
    "coupon_discount_label": {
        LANG_EN: "Discount ({code}): -{amount}",
        LANG_HI: "छूट ({code}): -{amount}",
    },
    "offers_list_header": {
        LANG_EN: "Here are our current offers — apply one at checkout:",
        LANG_HI: "यहां हमारे मौजूदा ऑफ़र हैं — चेकआउट पर लागू करें:",
    },
    "offers_percentage_line": {
        LANG_EN: "{code} — {value}% off",
        LANG_HI: "{code} — {value}% छूट",
    },
    "offers_flat_line": {
        LANG_EN: "{code} — {value} off",
        LANG_HI: "{code} — {value} छूट",
    },
    "no_offers_available": {
        LANG_EN: "We don't have any active offers right now — check back soon!",
        LANG_HI: "फ़िलहाल हमारे पास कोई सक्रिय ऑफ़र नहीं है — जल्द ही फिर देखें!",
    },
}

_STATUS_LABELS: dict[str, dict[str, str]] = {
    "browsing": {LANG_EN: "Browsing", LANG_HI: "ब्राउज़ हो रहा है"},
    "pending_payment": {LANG_EN: "Pending Payment", LANG_HI: "भुगतान लंबित"},
    "paid": {LANG_EN: "Paid", LANG_HI: "भुगतान हो गया"},
    "failed": {LANG_EN: "Failed", LANG_HI: "असफल"},
    "cancelled": {LANG_EN: "Cancelled", LANG_HI: "रद्द"},
    "fulfilled": {LANG_EN: "Fulfilled", LANG_HI: "पूर्ण"},
}


def t(key: str, lang: str | None, **kwargs) -> str:
    """Looks up `key` for `lang`, formats with kwargs. Falls back to
    DEFAULT_LANG if `lang` isn't recognized, and to the raw key (rather than
    raising) if the key itself doesn't exist -- a typo'd key should be
    visibly wrong in the chat, not crash message delivery."""
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    template = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    return template.format(**kwargs) if kwargs else template


def status_label(status: str, lang: str | None) -> str:
    entry = _STATUS_LABELS.get(status)
    if entry is None:
        return status
    return entry.get(lang) or entry.get(DEFAULT_LANG) or status
