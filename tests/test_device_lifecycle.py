"""Registry changes, rather than config-entry updates, drive live-view entities."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr

from custom_components.simplirtc.camera import async_setup_entry


class SystemV3:
    def __init__(self, cameras):
        self.cameras = {camera.serial: camera for camera in cameras}


class LiveView:
    def __init__(self, *args, pending=False):
        self._simplisafe = args[0]
        self.camera = self._device = args[2]
        self.parent = args[-1]
        self._platform_ready = asyncio.Event()
        self._added_to_hass = not pending
        if not pending:
            self._platform_ready.set()
        self.removing = asyncio.Event()
        self.allow_remove = asyncio.Event()
        self.allow_remove.set()
        self.removed = False

    async def async_remove(self):
        self.removing.set()
        await self.allow_remove.wait()
        self._added_to_hass = False
        self.removed = True


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


@asynccontextmanager
async def camera_watcher(*, initial=('livekit',), pending=False, during_scan=False):
    models = [SimpleNamespace(serial=serial, camera_settings={'admin': {'webRTCProvider': provider}})
              for serial, provider in [('livekit', 'mist'), ('kinesis', 'kvs'), ('legacy', 'simplisafe'), ('future', 'other'), ('missing', None)]]
    models.append(SimpleNamespace(serial='no-admin', camera_settings={'admin': None}))
    models.append(SimpleNamespace(serial='no-settings', camera_settings={}))
    system = SystemV3(models)
    devices = {}
    listeners = {}
    tasks = []
    added = []
    def source(serial, owner='core'):
        return SimpleNamespace(id='device-'+serial, config_entry_id=owner, identifiers={('simplisafe', serial)})
    for serial in initial:
        device = source(serial)
        devices[device.id] = device
    def fire(action, device_id):
        callback = listeners.get(dr.EVENT_DEVICE_REGISTRY_UPDATED)
        if callback:
            callback(SimpleNamespace(data={'action': action, 'device_id': device_id}))
    def listen(event_type, action):
        listeners[event_type] = action
        return lambda: listeners.pop(event_type, None)
    def create_task(hass, coroutine, name, **kwargs):
        task = asyncio.create_task(coroutine, name=name)
        tasks.append(task)
        return task
    entry = SimpleNamespace(
        state=ConfigEntryState.LOADED, entry_id='helper', data={},
        runtime_data=SimpleNamespace(systems={1: system}), async_create_background_task=create_task,
    )
    registry = Mock()
    registry.async_get.side_effect = devices.get
    accounts = {'core': SimpleNamespace(domain='simplisafe', disabled_by=None,
                state=ConfigEntryState.LOADED, runtime_data=entry.runtime_data)}
    class DeviceCollection:
        def __iter__(self):
            current = list(devices.values())
            if during_scan:
                late = source('kinesis')
                devices[late.id] = late
                assert dr.EVENT_DEVICE_REGISTRY_UPDATED in listeners
                fire('create', late.id)
            return iter(current)
    registry.devices = DeviceCollection()
    hass = SimpleNamespace(bus=SimpleNamespace(async_listen=listen),
                           config_entries=SimpleNamespace(async_get_entry=accounts.get))
    ctx = SimpleNamespace(devices=devices, listeners=listeners, added=added, entry=entry,
                          system=system, source=source, fire=fire, tasks=tasks, registry=registry, accounts=accounts)
    with patch('custom_components.simplirtc.camera.SystemV3', SystemV3), patch(
        'custom_components.simplirtc.camera.dr.async_get', return_value=registry,
    ), patch(
        'custom_components.simplirtc.camera.SimpliSafeCamera', side_effect=lambda *args: LiveView(*args, pending=pending),
    ):
        try:
            await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
            await settle()
            yield ctx
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert not listeners


@pytest.mark.asyncio
async def test_initial_scan_only_extends_supported_existing_core_devices():
    async with camera_watcher(initial=('livekit', 'kinesis', 'legacy', 'future', 'missing', 'no-admin', 'no-settings')) as ctx:
        assert [entity.camera.serial for entity in ctx.added] == ['livekit', 'kinesis']
        ctx.registry.async_get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_registration_during_scan_is_not_missed_or_added_twice():
    async with camera_watcher(during_scan=True) as ctx:
        assert sorted(entity.camera.serial for entity in ctx.added) == ['kinesis', 'livekit']
        ctx.fire('create', 'device-kinesis')
        ctx.fire('update', 'device-kinesis')
        await settle()
        assert len(ctx.added) == 2


@pytest.mark.asyncio
async def test_missing_source_is_added_later_without_reloading_config_entry():
    async with camera_watcher(initial=()) as ctx:
        assert not ctx.added
        device = ctx.source('livekit')
        ctx.devices[device.id] = device
        ctx.fire('create', device.id)
        await settle()
        assert len(ctx.added) == 1
        assert len(ctx.tasks) == 1


@pytest.mark.asyncio
async def test_other_accounts_and_non_camera_devices_are_ignored():
    async with camera_watcher(initial=()) as ctx:
        for device in (ctx.source('livekit', owner='different-account'), ctx.source('alarm-base')):
            ctx.devices[device.id] = device
            ctx.fire('create', device.id)
        await settle()
        assert not ctx.added


@pytest.mark.asyncio
async def test_remove_and_readd_only_replaces_the_matching_entity():
    async with camera_watcher(initial=('livekit', 'kinesis')) as ctx:
        first, second = ctx.added
        del ctx.devices[first.parent.id]
        ctx.fire('remove', first.parent.id)
        await settle()
        assert first.removed
        assert not second.removed
        restored = ctx.source('livekit')
        ctx.devices[restored.id] = restored
        ctx.fire('create', restored.id)
        await settle()
        assert len(ctx.added) == 3
        assert ctx.added[-1].camera.serial == 'livekit'


@pytest.mark.asyncio
async def test_create_then_remove_before_processing_does_not_add_an_entity():
    async with camera_watcher(initial=()) as ctx:
        device = ctx.source('livekit')
        ctx.devices[device.id] = device
        ctx.fire('create', device.id)
        del ctx.devices[device.id]
        ctx.fire('remove', device.id)
        await settle()
        assert not ctx.added


@pytest.mark.asyncio
async def test_remove_waits_for_entity_addition_to_finish():
    async with camera_watcher(pending=True) as ctx:
        entity = ctx.added[0]
        del ctx.devices[entity.parent.id]
        ctx.fire('remove', entity.parent.id)
        await settle()
        assert not entity.removing.is_set()
        entity._added_to_hass = True
        entity._platform_ready.set()
        await settle()
        assert entity.removed


@pytest.mark.asyncio
async def test_aborted_addition_does_not_attempt_to_remove_an_unadded_entity():
    async with camera_watcher(pending=True) as ctx:
        entity = ctx.added[0]
        del ctx.devices[entity.parent.id]
        ctx.fire('remove', entity.parent.id)
        entity._platform_ready.set()  # HA's add-abort cleanup callback.
        await settle()
        assert not entity.removing.is_set()


@pytest.mark.asyncio
async def test_removal_waits_for_cleanup_before_readding_a_device():
    async with camera_watcher() as ctx:
        entity = ctx.added[0]
        entity.allow_remove.clear()
        del ctx.devices[entity.parent.id]
        ctx.fire('remove', entity.parent.id)
        async with asyncio.timeout(1):
            await entity.removing.wait()
        restored = ctx.source('livekit')
        ctx.devices[restored.id] = restored
        ctx.fire('create', restored.id)
        await settle()
        assert len(ctx.added) == 1
        entity.allow_remove.set()
        await settle()
        assert entity.removed
        assert len(ctx.added) == 2


@pytest.mark.asyncio
async def test_provider_or_ownership_changes_remove_the_live_view_entity():
    async with camera_watcher(initial=('livekit', 'kinesis')) as ctx:
        first, second = ctx.added
        first.camera.camera_settings['admin']['webRTCProvider'] = 'unsupported'
        ctx.fire('update', first.parent.id)
        second.parent.config_entry_id = 'different-account'
        ctx.fire('update', second.parent.id)
        await settle()
        assert first.removed and second.removed


@pytest.mark.asyncio
async def test_unloading_ignores_new_devices_and_unsubscribes():
    async with camera_watcher(initial=()) as ctx:
        ctx.entry.state = ConfigEntryState.UNLOAD_IN_PROGRESS
        device = ctx.source('livekit')
        ctx.devices[device.id] = device
        ctx.fire('create', device.id)
        await settle()
        assert not ctx.added


@pytest.mark.asyncio
async def test_changed_camera_identity_replaces_just_that_entity():
    async with camera_watcher() as ctx:
        old = ctx.added[0]
        old.parent.identifiers = {('simplisafe', 'kinesis')}
        ctx.fire('update', old.parent.id)
        await settle()
        assert old.removed
        assert len(ctx.added) == 2
        assert ctx.added[-1].camera.serial == 'kinesis'


@pytest.mark.asyncio
async def test_one_failed_entity_does_not_stop_watching_other_devices():
    async with camera_watcher(initial=()) as ctx:
        with patch('custom_components.simplirtc.camera.SimpliSafeCamera', side_effect=RuntimeError('bad entity')):
            first = ctx.source('livekit')
            ctx.devices[first.id] = first
            ctx.fire('create', first.id)
            await settle()
        second = ctx.source('kinesis')
        ctx.devices[second.id] = second
        ctx.fire('create', second.id)
        await settle()
        assert len(ctx.added) == 1
        assert ctx.added[0].camera.serial == 'kinesis'


@pytest.mark.asyncio
async def test_one_watcher_uses_each_accounts_runtime_and_survives_empty_accounts():
    async with camera_watcher() as ctx:
        other_model = SimpleNamespace(serial='livekit', camera_settings={'admin': {'webRTCProvider': 'mist'}})
        other_runtime = SimpleNamespace(systems={2: SystemV3([other_model])})
        ctx.accounts['second'] = SimpleNamespace(domain='simplisafe', disabled_by=None,
            state=ConfigEntryState.SETUP_IN_PROGRESS, runtime_data=other_runtime)
        other_device = ctx.source('livekit', owner='second')
        other_device.id = 'second-device'
        ctx.devices[other_device.id] = other_device
        ctx.fire('create', other_device.id)
        await settle()
        assert len(ctx.added) == 2
        assert ctx.added[0]._simplisafe is ctx.accounts['core'].runtime_data
        assert ctx.added[1]._simplisafe is other_runtime
        assert len(ctx.tasks) == 1
        del ctx.devices['device-livekit']
        ctx.fire('remove', 'device-livekit')
        await settle()
        assert ctx.added[0].removed
        assert not ctx.added[1].removed
        assert not ctx.tasks[0].done()


@pytest.mark.asyncio
async def test_disabled_or_unready_accounts_are_ignored():
    async with camera_watcher(initial=()) as ctx:
        for owner, attributes in [('disabled', dict(disabled_by='user')),
                                  ('unloaded', dict(state=ConfigEntryState.NOT_LOADED)),
                                  ('no-runtime', {})]:
            account = SimpleNamespace(domain='simplisafe', disabled_by=None,
                                      state=ConfigEntryState.LOADED)
            if owner != 'no-runtime':
                account.runtime_data = ctx.accounts['core'].runtime_data
            for key, value in attributes.items():
                setattr(account, key, value)
            ctx.accounts[owner] = account
            device = ctx.source('livekit', owner=owner)
            device.id = owner
            ctx.devices[device.id] = device
            ctx.fire('create', device.id)
        await settle()
        assert not ctx.added
