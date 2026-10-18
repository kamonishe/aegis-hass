"""Account-specific panels retain existing registry identities during migration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.aegis_ajax.alarm_control_panel import async_setup_entry
from custom_components.aegis_ajax.api.models import Group, Space
from custom_components.aegis_ajax.const import ConnectionStatus, SecurityState

if TYPE_CHECKING:
    from pathlib import Path


async def test_migration_preserves_entity_id_and_other_account(tmp_path: Path) -> None:
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = MagicMock()
    alice = SimpleNamespace(
        entry_id="alice",
        data={"account_label": "Main"},
        options={},
        pref_disable_new_entities=False,
        disabled_by=None,
    )
    bob = SimpleNamespace(
        entry_id="bob",
        data={"account_label": "Guest"},
        options={},
        pref_disable_new_entities=False,
        disabled_by=None,
    )
    hass.config_entries.async_entries.return_value = [alice, bob]
    hass.config_entries.async_get_entry.side_effect = {"alice": alice, "bob": bob}.get
    with patch(
        "homeassistant.helpers.device_registry.async_get",
        return_value=MagicMock(async_wait_loaded=AsyncMock()),
    ):
        registry = er.async_get(hass)
        await registry.async_load()
    with patch.object(registry, "async_schedule_save"):
        old = registry.async_get_or_create(
            "alarm_control_panel",
            "aegis_ajax",
            "aegis_ajax_alarm_space",
            config_entry=alice,
            suggested_object_id="existing_alarm",
        )
        old_group = registry.async_get_or_create(
            "alarm_control_panel",
            "aegis_ajax",
            "aegis_ajax_alarm_space_group_group",
            config_entry=alice,
            suggested_object_id="existing_group",
        )
        other = registry.async_get_or_create(
            "alarm_control_panel",
            "aegis_ajax",
            "aegis_ajax_alarm_bob_space",
            config_entry=bob,
            suggested_object_id="bobs_alarm",
        )
        registry.async_update_entity(old.entity_id, name="Keep my name")
        space = Space(
            id="space",
            hub_id="hub",
            name="Home",
            security_state=SecurityState.DISARMED,
            connection_status=ConnectionStatus.ONLINE,
            malfunctions_count=0,
            group_mode_enabled=True,
            groups=(
                Group(
                    id="group",
                    space_id="space",
                    name="Downstairs",
                    security_state=SecurityState.DISARMED,
                ),
            ),
        )
        for entry in (alice, bob):
            entry.runtime_data = MagicMock()
            entry.runtime_data.entry_id = entry.entry_id
            entry.runtime_data.config_entry = entry
            entry.runtime_data.hass = hass
            entry.runtime_data._space_ids = ["space"]
            entry.runtime_data.spaces = {"space": space}
            entry.runtime_data.devices = {}
        panels = []
        await async_setup_entry(hass, alice, panels.extend)
        assert registry.async_get(old.entity_id).unique_id == "aegis_ajax_alarm_alice_space"
        assert registry.async_get(old.entity_id).name == "Keep my name"
        assert (
            registry.async_get(old_group.entity_id).unique_id
            == "aegis_ajax_alarm_alice_space_group_group"
        )
        assert registry.async_get(other.entity_id).unique_id == "aegis_ajax_alarm_bob_space"
        assert {panel.unique_id for panel in panels} == {
            "aegis_ajax_alarm_alice_space",
            "aegis_ajax_alarm_alice_space_group_group",
        }
        # Repeated setup must not create another migration or another identity.
        reloaded = []
        await async_setup_entry(hass, alice, reloaded.extend)
        assert [panel.unique_id for panel in reloaded] == [panel.unique_id for panel in panels]
        bobs_panels = []
        await async_setup_entry(hass, bob, bobs_panels.extend)
        assert {panel.unique_id for panel in panels}.isdisjoint(
            panel.unique_id for panel in bobs_panels
        )


async def test_migrates_visible_entity_ids_under_account_slug(tmp_path: Path) -> None:
    """Account labels prefix non-panel IDs while alarm panels keep their names."""
    from custom_components.aegis_ajax import (
        _ensure_account_entity_slug,
        _migrate_account_entity_ids,
    )

    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = MagicMock()
    alice = SimpleNamespace(
        entry_id="alice",
        data={"account_label": "Main Site"},
        pref_disable_new_entities=False,
        disabled_by=None,
    )
    bob = SimpleNamespace(
        entry_id="bob",
        data={"account_label": "Guest"},
        pref_disable_new_entities=False,
        disabled_by=None,
    )
    hass.config_entries.async_entries.return_value = [alice, bob]
    hass.config_entries.async_update_entry.side_effect = lambda entry, *, data: setattr(
        entry, "data", data
    )
    with patch(
        "homeassistant.helpers.device_registry.async_get",
        return_value=MagicMock(async_wait_loaded=AsyncMock()),
    ):
        registry = er.async_get(hass)
        await registry.async_load()
    with patch.object(registry, "async_schedule_save"):
        entity = registry.async_get_or_create(
            "binary_sensor",
            "aegis_ajax",
            "aegis_ajax_alice_315C2836_external_contact_broken",
            config_entry=alice,
            suggested_object_id="staircase_door",
        )
        old_entity_id = entity.entity_id
        panel = registry.async_get_or_create(
            "alarm_control_panel",
            "aegis_ajax",
            "aegis_ajax_alarm_alice_space",
            config_entry=alice,
            suggested_object_id="ajax_main_site_home",
        )
        account_slug = _ensure_account_entity_slug(hass, alice)
        _migrate_account_entity_ids(hass, alice, account_slug)

    assert account_slug == "main_site"
    assert registry.async_get("binary_sensor.main_site_staircase_door") is not None
    assert registry.async_get("alarm_control_panel.ajax_main_site_home") == panel
    assert registry.async_get(old_entity_id) is None


async def test_migrates_legacy_entity_ids_without_renaming_entities(tmp_path: Path) -> None:
    """All non-panel entities gain account scope while keeping entity IDs."""
    from custom_components.aegis_ajax import _migrate_account_scoped_entity_ids

    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = MagicMock()
    alice = SimpleNamespace(entry_id="alice", pref_disable_new_entities=False, disabled_by=None)
    bob = SimpleNamespace(entry_id="bob", pref_disable_new_entities=False, disabled_by=None)
    hass.config_entries.async_entries.return_value = [alice, bob]
    with patch(
        "homeassistant.helpers.device_registry.async_get",
        return_value=MagicMock(async_wait_loaded=AsyncMock()),
    ):
        registry = er.async_get(hass)
        await registry.async_load()
    with patch.object(registry, "async_schedule_save"):
        legacy = registry.async_get_or_create(
            "binary_sensor",
            "aegis_ajax",
            "aegis_ajax_315C2836_external_contact_broken",
            config_entry=alice,
            suggested_object_id="staircase_door",
        )
        already_scoped = registry.async_get_or_create(
            "button",
            "aegis_ajax",
            "aegis_ajax_alice_315C2836_siren_sound_test",
            config_entry=alice,
            suggested_object_id="siren_test",
        )
        other_account = registry.async_get_or_create(
            "binary_sensor",
            "aegis_ajax",
            "aegis_ajax_other_account_external_contact_broken",
            config_entry=bob,
            suggested_object_id="guest_door",
        )
        _migrate_account_scoped_entity_ids(hass, alice)

    assert registry.async_get(legacy.entity_id).unique_id == (
        "aegis_ajax_alice_315C2836_external_contact_broken"
    )
    assert registry.async_get(legacy.entity_id).entity_id == "binary_sensor.staircase_door"
    assert registry.async_get(already_scoped.entity_id).unique_id == (
        "aegis_ajax_alice_315C2836_siren_sound_test"
    )
    assert registry.async_get(other_account.entity_id).unique_id == (
        "aegis_ajax_other_account_external_contact_broken"
    )
