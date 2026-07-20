from __future__ import annotations

import hashlib


PRODUCTION_PRODUCT_NAME = "3D Blu-ray to Vision Pro"
PRODUCTION_BUNDLE_IDENTIFIER = "com.shinycomputers.bd-to-avp"
PRODUCTION_FEED_URL = "https://cbusillo.github.io/BD_to_AVP/appcast.xml"
PRODUCTION_SPARKLE_PUBLIC_KEY = "nJv1BH0mc2KFORVIDLlSI2A9mvGpVWdLcxlmz++OODU="
PRODUCTION_SPARKLE_PUBLIC_KEY_SHA256 = "74fe16c5b2761a4c2e562d72eff6095a1bada3e68f78e9eb384af808fb7b3196"
PRODUCTION_TEAM_ID = "MM5YXC7T6E"
PRODUCTION_DEVELOPER_IDENTITY = "Developer ID Application: Shiny Computers Leasing LLC (MM5YXC7T6E)"
PRODUCTION_DISTRIBUTION_CHANNEL = "direct"


def validate_production_public_key(value: str) -> str:
    if value != PRODUCTION_SPARKLE_PUBLIC_KEY:
        raise ValueError("Sparkle public key does not match the production identity.")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if digest != PRODUCTION_SPARKLE_PUBLIC_KEY_SHA256:
        raise ValueError("Sparkle public key digest does not match the production identity.")
    return value
