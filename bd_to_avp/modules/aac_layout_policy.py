from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class AacLayoutAction(StrEnum):
    PRESERVE = "preserve"
    REMAP = "remap"
    DOWNMIX = "downmix"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class AacLayoutPolicy:
    id: str
    source_layout: str
    action: AacLayoutAction
    target_layout: str
    pan_filter: str | None
    expected_identity_map: Mapping[str, tuple[str, ...]]
    rationale: str

    @property
    def source_channels(self) -> tuple[str, ...]:
        return LAYOUT_CHANNELS[self.source_layout]

    @property
    def target_channels(self) -> tuple[str, ...]:
        return LAYOUT_CHANNELS[self.target_layout]


@dataclass(frozen=True, slots=True)
class AacLayoutDecision:
    action: AacLayoutAction
    source_layout: str | None
    source_channel_count: int | None
    target_layout: str | None
    target_channel_count: int | None
    pan_filter: str | None
    reason: str | None
    policy: AacLayoutPolicy | None


class AacLayoutPolicyError(ValueError):
    pass


LAYOUT_CHANNELS: dict[str, tuple[str, ...]] = {
    "mono": ("FC",),
    "stereo": ("FL", "FR"),
    "2.1": ("FL", "FR", "LFE"),
    "3.0": ("FL", "FR", "FC"),
    "3.0(back)": ("FL", "FR", "BC"),
    "4.0": ("FL", "FR", "FC", "BC"),
    "quad": ("FL", "FR", "BL", "BR"),
    "quad(side)": ("FL", "FR", "SL", "SR"),
    "3.1": ("FL", "FR", "FC", "LFE"),
    "5.0": ("FL", "FR", "FC", "BL", "BR"),
    "5.0(side)": ("FL", "FR", "FC", "SL", "SR"),
    "4.1": ("FL", "FR", "FC", "LFE", "BC"),
    "5.1": ("FL", "FR", "FC", "LFE", "BL", "BR"),
    "5.1(side)": ("FL", "FR", "FC", "LFE", "SL", "SR"),
    "6.0": ("FL", "FR", "FC", "BC", "SL", "SR"),
    "6.0(front)": ("FL", "FR", "FLC", "FRC", "SL", "SR"),
    "hexagonal": ("FL", "FR", "FC", "BL", "BR", "BC"),
    "6.1": ("FL", "FR", "FC", "LFE", "BC", "SL", "SR"),
    "6.1(back)": ("FL", "FR", "FC", "LFE", "BL", "BR", "BC"),
    "6.1(front)": ("FL", "FR", "LFE", "FLC", "FRC", "SL", "SR"),
    "7.0": ("FL", "FR", "FC", "BL", "BR", "SL", "SR"),
    "7.0(front)": ("FL", "FR", "FC", "FLC", "FRC", "SL", "SR"),
    "7.1": ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"),
    "7.1(wide)": ("FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"),
    "7.1(wide-side)": ("FL", "FR", "FC", "LFE", "FLC", "FRC", "SL", "SR"),
    "octagonal": ("FL", "FR", "FC", "BL", "BR", "BC", "SL", "SR"),
}


def identity_map(**channels: str | tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return {source: (outputs,) if isinstance(outputs, str) else outputs for source, outputs in channels.items()}


AAC_LAYOUT_POLICIES = (
    AacLayoutPolicy(
        "ch01-mono",
        "mono",
        AacLayoutAction.PRESERVE,
        "mono",
        None,
        identity_map(FC="FC"),
        "AAC config 1.",
    ),
    AacLayoutPolicy(
        "ch02-stereo",
        "stereo",
        AacLayoutAction.PRESERVE,
        "stereo",
        None,
        identity_map(FL="FL", FR="FR"),
        "AAC config 2.",
    ),
    AacLayoutPolicy(
        "ch03-2_1",
        "2.1",
        AacLayoutAction.DOWNMIX,
        "stereo",
        "pan=stereo|FL<FL+0.5*LFE|FR<FR+0.5*LFE",
        identity_map(FL="FL", FR="FR", LFE=("FL", "FR")),
        "FFmpeg emits PCE AAC that Apple drops; retain LFE contribution in stereo.",
    ),
    AacLayoutPolicy(
        "ch03-3_0",
        "3.0",
        AacLayoutAction.PRESERVE,
        "3.0",
        None,
        identity_map(FL="FL", FR="FR", FC="FC"),
        "AAC config 3.",
    ),
    AacLayoutPolicy(
        "ch03-3_0_back",
        "3.0(back)",
        AacLayoutAction.DOWNMIX,
        "stereo",
        "pan=stereo|FL<FL+0.707*BC|FR<FR+0.707*BC",
        identity_map(FL="FL", FR="FR", BC=("FL", "FR")),
        "Back-center has no safe same-count canonical AAC mapping.",
    ),
    AacLayoutPolicy(
        "ch04-4_0",
        "4.0",
        AacLayoutAction.PRESERVE,
        "4.0",
        None,
        identity_map(FL="FL", FR="FR", FC="FC", BC="BC"),
        "AAC config 4.",
    ),
    AacLayoutPolicy(
        "ch04-quad",
        "quad",
        AacLayoutAction.DOWNMIX,
        "stereo",
        "pan=stereo|FL<FL+0.707*BL|FR<FR+0.707*BR",
        identity_map(FL="FL", FR="FR", BL="FL", BR="FR"),
        "Apple retains this PCE layout, but physical placement is not qualified.",
    ),
    AacLayoutPolicy(
        "ch04-quad_side",
        "quad(side)",
        AacLayoutAction.DOWNMIX,
        "stereo",
        "pan=stereo|FL<FL+0.707*SL|FR<FR+0.707*SR",
        identity_map(FL="FL", FR="FR", SL="FL", SR="FR"),
        "PCE AAC is dropped by Apple.",
    ),
    AacLayoutPolicy(
        "ch04-3_1",
        "3.1",
        AacLayoutAction.DOWNMIX,
        "stereo",
        "pan=stereo|FL<FL+0.707*FC+0.5*LFE|FR<FR+0.707*FC+0.5*LFE",
        identity_map(FL="FL", FR="FR", FC=("FL", "FR"), LFE=("FL", "FR")),
        "PCE AAC is dropped by Apple; retain center and LFE contribution.",
    ),
    AacLayoutPolicy(
        "ch05-5_0",
        "5.0",
        AacLayoutAction.PRESERVE,
        "5.0",
        None,
        identity_map(FL="FL", FR="FR", FC="FC", BL="SL", BR="SR"),
        "AAC config 5; FFmpeg BL/BR samples become Apple SL/SR surround positions.",
    ),
    AacLayoutPolicy(
        "ch05-5_0_side",
        "5.0(side)",
        AacLayoutAction.REMAP,
        "5.0",
        "pan=5.0|FL=FL|FR=FR|FC=FC|BL=SL|BR=SR",
        identity_map(FL="FL", FR="FR", FC="FC", SL="SL", SR="SR"),
        "Lossless one-to-one remap to AAC config 5.",
    ),
    AacLayoutPolicy(
        "ch05-4_1",
        "4.1",
        AacLayoutAction.DOWNMIX,
        "stereo",
        "pan=stereo|FL<FL+0.707*FC+0.5*LFE+0.707*BC|FR<FR+0.707*FC+0.5*LFE+0.707*BC",
        identity_map(FL="FL", FR="FR", FC=("FL", "FR"), LFE=("FL", "FR"), BC=("FL", "FR")),
        "PCE AAC is dropped by Apple; no safe same-count mapping exists.",
    ),
    AacLayoutPolicy(
        "ch06-5_1",
        "5.1",
        AacLayoutAction.PRESERVE,
        "5.1",
        None,
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", BL="SL", BR="SR"),
        "AAC config 6; FFmpeg BL/BR samples become Apple SL/SR surround positions.",
    ),
    AacLayoutPolicy(
        "ch06-5_1_side",
        "5.1(side)",
        AacLayoutAction.REMAP,
        "5.1",
        "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=SL|BR=SR",
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", SL="SL", SR="SR"),
        "Lossless one-to-one remap already proven by the real-source gate in #364.",
    ),
    AacLayoutPolicy(
        "ch06-6_0",
        "6.0",
        AacLayoutAction.DOWNMIX,
        "5.0",
        "pan=5.0|FL=FL|FR=FR|FC=FC|BL<SL+0.707*BC|BR<SR+0.707*BC",
        identity_map(FL="FL", FR="FR", FC="FC", BC=("SL", "SR"), SL="SL", SR="SR"),
        "Collapse three surround positions into canonical AAC 5.0.",
    ),
    AacLayoutPolicy(
        "ch06-6_0_front",
        "6.0(front)",
        AacLayoutAction.DOWNMIX,
        "5.0",
        "pan=5.0|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC<0.5*FLC+0.5*FRC|BL=SL|BR=SR",
        identity_map(FL="FL", FR="FR", FLC=("FL", "FC"), FRC=("FR", "FC"), SL="SL", SR="SR"),
        "Fold front-wide channels into the canonical front stage.",
    ),
    AacLayoutPolicy(
        "ch06-hexagonal",
        "hexagonal",
        AacLayoutAction.DOWNMIX,
        "5.0",
        "pan=5.0|FL=FL|FR=FR|FC=FC|BL<BL+0.707*BC|BR<BR+0.707*BC",
        identity_map(FL="FL", FR="FR", FC="FC", BL="SL", BR="SR", BC=("SL", "SR")),
        "Fold back-center into the canonical surround pair.",
    ),
    AacLayoutPolicy(
        "ch07-6_1",
        "6.1",
        AacLayoutAction.DOWNMIX,
        "5.1",
        "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL<SL+0.707*BC|BR<SR+0.707*BC",
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", BC=("SL", "SR"), SL="SL", SR="SR"),
        "Fold back-center into canonical AAC 5.1 surrounds.",
    ),
    AacLayoutPolicy(
        "ch07-6_1_back",
        "6.1(back)",
        AacLayoutAction.DOWNMIX,
        "5.1",
        "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL<BL+0.707*BC|BR<BR+0.707*BC",
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", BL="SL", BR="SR", BC=("SL", "SR")),
        "Fold back-center into canonical AAC 5.1 surrounds.",
    ),
    AacLayoutPolicy(
        "ch07-6_1_front",
        "6.1(front)",
        AacLayoutAction.DOWNMIX,
        "5.1",
        "pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC<0.5*FLC+0.5*FRC|LFE=LFE|BL=SL|BR=SR",
        identity_map(
            FL="FL",
            FR="FR",
            LFE="LFE",
            FLC=("FL", "FC"),
            FRC=("FR", "FC"),
            SL="SL",
            SR="SR",
        ),
        "Fold front-wide channels into canonical AAC 5.1.",
    ),
    AacLayoutPolicy(
        "ch07-7_0",
        "7.0",
        AacLayoutAction.DOWNMIX,
        "5.0",
        "pan=5.0|FL=FL|FR=FR|FC=FC|BL<BL+SL|BR<BR+SR",
        identity_map(FL="FL", FR="FR", FC="FC", BL="SL", BR="SR", SL="SL", SR="SR"),
        "Collapse side and back pairs into canonical AAC 5.0 surrounds.",
    ),
    AacLayoutPolicy(
        "ch07-7_0_front",
        "7.0(front)",
        AacLayoutAction.DOWNMIX,
        "5.0",
        "pan=5.0|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|BL=SL|BR=SR",
        identity_map(FL="FL", FR="FR", FC="FC", FLC="FL", FRC="FR", SL="SL", SR="SR"),
        "Fold front-wide channels into canonical AAC 5.0.",
    ),
    AacLayoutPolicy(
        "ch08-7_1",
        "7.1",
        AacLayoutAction.DOWNMIX,
        "5.1",
        "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL<BL+SL|BR<BR+SR",
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", BL="SL", BR="SR", SL="SL", SR="SR"),
        "FFmpeg config 7 is decoded by Apple in AAC wide order, so track presence does not prove placement.",
    ),
    AacLayoutPolicy(
        "ch08-7_1_wide",
        "7.1(wide)",
        AacLayoutAction.DOWNMIX,
        "5.1",
        "pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|LFE=LFE|BL=BL|BR=BR",
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", BL="SL", BR="SR", FLC="FL", FRC="FR"),
        "Fold front-wide channels into canonical AAC 5.1.",
    ),
    AacLayoutPolicy(
        "ch08-7_1_wide_side",
        "7.1(wide-side)",
        AacLayoutAction.DOWNMIX,
        "5.1",
        "pan=5.1|FL<FL+0.707*FLC|FR<FR+0.707*FRC|FC=FC|LFE=LFE|BL=SL|BR=SR",
        identity_map(FL="FL", FR="FR", FC="FC", LFE="LFE", FLC="FL", FRC="FR", SL="SL", SR="SR"),
        "Fold front-wide channels while retaining the surround pair.",
    ),
    AacLayoutPolicy(
        "ch08-octagonal",
        "octagonal",
        AacLayoutAction.DOWNMIX,
        "5.0",
        "pan=5.0|FL=FL|FR=FR|FC=FC|BL<BL+SL+0.707*BC|BR<BR+SR+0.707*BC",
        identity_map(FL="FL", FR="FR", FC="FC", BL="SL", BR="SR", BC=("SL", "SR"), SL="SL", SR="SR"),
        "Collapse side, back, and back-center positions into canonical AAC 5.0.",
    ),
)

AAC_LAYOUT_POLICY_BY_SOURCE = {policy.source_layout: policy for policy in AAC_LAYOUT_POLICIES}
AAC_COPY_LAYOUT_CHANNELS = {
    policy.source_layout: len(policy.source_channels)
    for policy in AAC_LAYOUT_POLICIES
    if policy.action is AacLayoutAction.PRESERVE
}
UNLISTED_LAYOUT_POLICY = {
    "action": AacLayoutAction.FAIL.value,
    "reason": "Missing, unknown, custom, discrete, or unlisted layouts require new identity and Apple evidence.",
}


def resolve_aac_layout_policy(channel_layout: object, channels: object) -> AacLayoutDecision:
    normalized_layout = _normalize_layout(channel_layout)
    channel_count = _parse_channel_count(channels)
    if normalized_layout is None:
        return _failed_decision(None, channel_count, "channel_layout_missing")
    if channel_count is None:
        return _failed_decision(normalized_layout, None, "channel_count_missing")
    policy = AAC_LAYOUT_POLICY_BY_SOURCE.get(normalized_layout)
    if policy is None:
        return _failed_decision(normalized_layout, channel_count, "channel_layout_not_qualified")
    expected_channels = len(policy.source_channels)
    if channel_count != expected_channels:
        return _failed_decision(normalized_layout, channel_count, "channel_layout_mismatch")
    return AacLayoutDecision(
        action=policy.action,
        source_layout=normalized_layout,
        source_channel_count=channel_count,
        target_layout=policy.target_layout,
        target_channel_count=len(policy.target_channels),
        pan_filter=policy.pan_filter,
        reason=None,
        policy=policy,
    )


def _failed_decision(source_layout: str | None, channel_count: int | None, reason: str) -> AacLayoutDecision:
    return AacLayoutDecision(
        action=AacLayoutAction.FAIL,
        source_layout=source_layout,
        source_channel_count=channel_count,
        target_layout=None,
        target_channel_count=None,
        pan_filter=None,
        reason=reason,
        policy=None,
    )


def _normalize_layout(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _parse_channel_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None
