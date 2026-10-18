"""Shared entity helpers for the Aegis Ajax integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo, DeviceRegistry
from homeassistant.util import slugify

from custom_components.aegis_ajax.const import (
    ACCOUNT_ENTITY_SLUG,
    COMMAND_ERROR_TRANSLATION_KEYS,
    DOMAIN,
    MANUFACTURER,
)

if TYPE_CHECKING:
    from custom_components.aegis_ajax.api.models import Device, DeviceCommand, Room
    from custom_components.aegis_ajax.coordinator import AjaxCobrandedCoordinator


# HA 2026.8 added `via_device_id` to `DeviceInfo` and, in the same release,
# `DeviceRegistry.async_get_device_by_identifier`; 2026.9 deprecates the
# identifier-tuple `via_device` and the cross-entry `async_get_device`, and
# 2027.8 removes both (#444). Older cores reject the new key with a TypeError
# deep inside `async_get_or_create`, so the key the running core understands is
# detected once here and every via link goes through `via_device_fields`.
#
# ⚠️ THE `via_device_id` BRANCH CANNOT BE RUN AGAINST A CORE THAT HAS IT.
# The newest `homeassistant` on PyPI is 2026.2.x, so CI — and any local dev
# image — resolves a core where `via_device_id` is absent from `DeviceInfo` and
# this flag is always False. The branch that every user on 2026.8+ actually
# takes is only ever exercised with the flag patched, against our idea of the
# API rather than the API. That is not a hypothetical: #489 shipped in 1.18.0
# and 1.19.0 with NVR-bridged Video Edge cameras silently orphaned, through
# fully green CI, because a device whose `hub_id` is its own id was handed
# itself as its parent and only a real 2026.8 core rejects that.
# So changes here are reasoned about, not merely tested. What guards them is an
# invariant rather than a version: `TestNoDeviceIsEverItsOwnParent` asserts,
# for every device family and both branches, that no via link ever points at
# the device's own identity — and it fails if the #489 fix below is removed.
# `__annotations__` lists the keys on both the old `TypedDict` and the
# dataclass `DeviceInfo` became in 2026.11 (#593), which keeps
# `__required_keys__`/`__optional_keys__` only as a compatibility layer.
_VIA_DEVICE_ID_SUPPORTED = "via_device_id" in DeviceInfo.__annotations__


def account_entity_slug(entry: object) -> str:
    """Return the stable, human-readable slug used in entity IDs for an account."""
    data = getattr(entry, "data", {})
    if not isinstance(data, Mapping):
        return "ajax_account"
    stored = data.get(ACCOUNT_ENTITY_SLUG)
    if isinstance(stored, str) and stored:
        return stored
    label = data.get("account_label")
    slug = slugify(str(label)) if isinstance(label, str) else ""
    if slug:
        return slug
    entry_id = getattr(entry, "entry_id", "")
    suffix = slugify(str(entry_id)[-8:]) if entry_id else ""
    return f"ajax_{suffix}" if suffix else "ajax_account"


def account_scoped_entity_id(entity_id: str, account_slug: str) -> str:
    """Prefix an entity object ID with its owning account slug."""
    domain, separator, object_id = entity_id.partition(".")
    if not separator or not object_id or not account_slug:
        return entity_id
    prefix = f"{account_slug}_"
    if object_id.startswith(prefix):
        return entity_id
    return f"{domain}.{prefix}{object_id}"


def account_scoped_unique_id(coordinator: AjaxCobrandedCoordinator, legacy_unique_id: str) -> str:
    """Scope an entity unique ID to the Ajax account that owns it.

    Ajax device IDs are only unique within an account. The old IDs therefore
    collided when two accounts exposed the same hub or device. Keep the
    legacy value for test/standalone coordinators without an entry ID, while
    real config entries get a stable account prefix.
    """
    entry_id = getattr(coordinator, "entry_id", "")
    if not isinstance(entry_id, str) or not entry_id:
        return legacy_unique_id
    prefix = f"{DOMAIN}_{entry_id}_"
    if legacy_unique_id.startswith(prefix):
        return legacy_unique_id
    domain_prefix = f"{DOMAIN}_"
    if not legacy_unique_id.startswith(domain_prefix):
        return legacy_unique_id
    return f"{prefix}{legacy_unique_id.removeprefix(domain_prefix)}"


def is_hub_device(device: Device) -> bool:
    """Whether an Ajax device row is a hub (the root of its device tree)."""
    return device.device_type.startswith("hub")


def via_device_fields(hub_id: str, hub_registry_id: str | None) -> dict[str, Any]:
    """The `DeviceInfo` fields linking a child device to its hub.

    On HA 2026.8+ the link is the hub's device-registry entry id; when the hub
    is not registered yet there is nothing valid to pass — HA rejects an
    unknown `via_device_id` together with the entity — so the link is simply
    omitted. Older cores get the identifier tuple they understand.
    """
    if not _VIA_DEVICE_ID_SUPPORTED:
        return {"via_device": (DOMAIN, hub_id)}
    if hub_registry_id is None:
        return {}
    return {"via_device_id": hub_registry_id}


def async_get_registered_device(
    registry: DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> DeviceEntry | None:
    """Look a device up by identifier, scoped to our config entry (#444).

    Falls back to the pre-2026.8 cross-entry lookup where the scoped one does
    not exist yet.
    """
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        return lookup(identifier, config_entry_id)  # type: ignore[no-any-return]
    return registry.async_get_device(identifiers={identifier})


# device_type → model display overrides for names `.title()` mangles
# (acronyms). Everything else keeps the generic title-cased fallback.
_MODEL_OVERRIDES: dict[str, str] = {
    "video_edge_nvr": "Video Edge NVR",
}


def build_device_info(
    device: Device,
    rooms: Mapping[str, Room] | None = None,
    *,
    firmware_version: str | None = None,
    via_device_id: str | None = None,
) -> DeviceInfo:
    """Build a HA DeviceInfo for an Ajax device.

    Sets `serial_number` from the Ajax device id (the hex hardware identifier
    shown in the Ajax app) and `suggested_area` from the device's Ajax room
    when available, so HA can auto-assign devices to matching areas.
    `firmware_version` is keyword-only so call sites stay explicit about
    populating `sw_version` (#388). `via_device_id` is the hub's registry
    entry id (`coordinator.hub_registry_id`), used for the child→hub link on
    HA 2026.8+ and ignored for hubs themselves (#444).
    """
    is_hub = is_hub_device(device)
    info = DeviceInfo(
        identifiers={(DOMAIN, device.id)},
        name=device.name,
        manufacturer=MANUFACTURER,
        model=_MODEL_OVERRIDES.get(
            device.device_type, device.device_type.replace("_", " ").title()
        ),
        serial_number=device.id,
    )
    # Video Edge channels and boxes have no Ajax hub parent. Their parser
    # deliberately uses their own id as `hub_id`, so linking them through that
    # id would make the HA device entry its own parent (#489).
    if not is_hub and device.hub_id != device.id:
        info.update(cast("DeviceInfo", via_device_fields(device.hub_id, via_device_id)))
    if rooms and device.room_id:
        room = rooms.get(device.room_id) if isinstance(rooms, dict) else None
        if room is not None:
            info["suggested_area"] = room.name
    if firmware_version:
        info["sw_version"] = firmware_version
    return info


async def async_send_device_command(
    coordinator: AjaxCobrandedCoordinator, command: DeviceCommand
) -> None:
    """Send a device command and refresh, mapping hub rejections to a clear,
    translated `HomeAssistantError`.

    A failure the hub reports (permission denied, hub offline, …) is surfaced
    with a factual message keyed off the server's reason; any unmapped reason
    falls back to `command_failed`, echoing the raw code. The coordinator is
    only refreshed on success.
    """
    from custom_components.aegis_ajax.api.devices import DeviceCommandError  # noqa: PLC0415

    try:
        await coordinator.devices_api.send_command(command)
    except DeviceCommandError as err:
        _raise_translated_command_error(err)
    await coordinator.async_request_refresh()


async def async_set_chimes_mode(
    coordinator: AjaxCobrandedCoordinator, hub_id: str, *, enable: bool
) -> None:
    """Toggle the hub-wide Chime, mapping hub rejections to a clear error (#239).

    Companion to `async_send_device_command` for the hub-level Chime command,
    which isn't a per-device `DeviceCommand`. Same error-to-translation mapping
    (`permission_denied` when the account lacks EDIT_CHIMES, `hub_offline`, …)
    and the coordinator is only refreshed on success.
    """
    from custom_components.aegis_ajax.api.devices import DeviceCommandError  # noqa: PLC0415

    try:
        await coordinator.devices_api.set_chimes_mode(hub_id, enable=enable)
    except DeviceCommandError as err:
        _raise_translated_command_error(err)
    await coordinator.async_request_refresh()


async def async_start_sound_test(coordinator: AjaxCobrandedCoordinator, device: Device) -> None:
    """Play a siren's test sound, mapping hub rejections to a clear error (#549)."""
    from custom_components.aegis_ajax.api.devices import DeviceCommandError  # noqa: PLC0415

    try:
        await coordinator.devices_api.start_sound_test(device.hub_id, device.id, device.device_type)
    except DeviceCommandError as err:
        _raise_translated_command_error(err)


def _raise_translated_command_error(err: Any) -> None:  # noqa: ANN401
    """Re-raise a `DeviceCommandError` as a translated `HomeAssistantError`.

    Maps the server's failure-oneof reason to an `exceptions.*` key when known,
    otherwise falls back to `command_failed` echoing the raw reason.
    """
    translation_key = COMMAND_ERROR_TRANSLATION_KEYS.get(err.reason or "")
    if translation_key is not None:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=translation_key
        ) from err
    placeholders: dict[str, Any] = {"reason": err.reason or "unknown"}
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_failed",
        translation_placeholders=placeholders,
    ) from err
