"""One UI/import entry replaces the released per-account setup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.simplirtc import async_setup, async_setup_entry, async_migrate_entry
from custom_components.simplirtc.config_flow import CloudConfigFlow


@pytest.mark.asyncio
async def test_ui_confirmation_creates_single_global_entry_without_account_selection():
    flow = CloudConfigFlow()
    with patch.object(flow, '_async_current_entries', return_value=[]), patch.object(
        flow, 'async_set_unique_id', new_callable=AsyncMock,
    ) as unique, patch.object(flow, '_abort_if_unique_id_configured'), patch.object(
        flow, 'async_show_form', return_value={'type': 'form'},
    ) as show, patch.object(flow, 'async_create_entry', return_value={'type': 'create_entry'}) as create:
        assert (await flow.async_step_user())['type'] == 'form'
        show.assert_called_once_with(step_id='user')
        assert (await flow.async_step_user({}))['type'] == 'create_entry'
        create.assert_called_once_with(title='SimpliRTC', data={})
        assert all(call.args == ('simplirtc',) for call in unique.call_args_list)


@pytest.mark.asyncio
async def test_import_and_ui_refuse_a_second_entry_even_if_legacy():
    flow = CloudConfigFlow()
    with patch.object(flow, '_async_current_entries', return_value=[object()]), patch.object(
        flow, 'async_abort', return_value={'type': 'abort'},
    ) as abort:
        assert (await flow.async_step_user({}))['type'] == 'abort'
        assert (await flow.async_step_import({}))['type'] == 'abort'
        assert all(call.kwargs == {'reason': 'single_instance_allowed'} for call in abort.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('yaml,existing,imports', [(True, [], 1), (True, [object()], 0), (False, [], 0)])
async def test_yaml_is_imported_once_without_installing_global_listeners(yaml, existing, imports):
    init = AsyncMock()
    tasks = []
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: existing,
        flow=SimpleNamespace(async_init=init)), async_create_task=lambda coro, name: tasks.append(coro))
    with patch('custom_components.simplirtc.async_when_setup'):
        assert await async_setup(hass, {'simplirtc': {}} if yaml else {})
    for coro in tasks:
        await coro
    assert init.await_count == imports


@pytest.mark.asyncio
async def test_original_entries_merge_after_relinking_devices_and_preserve_entity_ids():
    first = SimpleNamespace(entry_id='first', data={'config_entry_id': 'account-one'},
                            version=1, minor_version=1, unique_id=None)
    second = SimpleNamespace(entry_id='second', data={'config_entry_id': 'account-two'},
                             version=1, minor_version=1, unique_id=None)
    entries = [first, second]
    records = [SimpleNamespace(entity_id='camera.one', config_entry_id='first'),
               SimpleNamespace(entity_id='camera.two', config_entry_id='second'),
               SimpleNamespace(entity_id='event.old_motion', config_entry_id='second')]
    operations = []
    registry = Mock()
    def transfer(entity_id, **changes):
        record = next(r for r in records if r.entity_id == entity_id)
        record.config_entry_id = changes['config_entry_id']
        operations.append(('transfer', entity_id))
    registry.async_update_entity.side_effect = transfer
    def update(entry, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)
    async def remove(entry_id):
        assert all(r.config_entry_id != entry_id for r in records)
        operations.append(('remove', entry_id))
        entries[:] = [e for e in entries if e.entry_id != entry_id]
        return True
    forward = AsyncMock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda *args, **kwargs: list(entries),
        async_update_entry=Mock(side_effect=update), async_remove=AsyncMock(side_effect=remove),
        async_forward_entry_setups=forward))
    with patch('custom_components.simplirtc.dr.async_get'), patch(
        'custom_components.simplirtc.dr.async_entries_for_config_entry',
        side_effect=lambda reg, parent: [SimpleNamespace(id='device-'+parent)],
    ), patch('custom_components.simplirtc.async_remove_helper_devices') as cleanup, patch(
        'custom_components.simplirtc.er.async_get', return_value=registry,
    ), patch('custom_components.simplirtc.er.async_entries_for_config_entry',
             side_effect=lambda reg, entry_id: [r for r in records if r.config_entry_id == entry_id]):
        assert await async_migrate_entry(hass, first)
        assert await async_migrate_entry(hass, second)
        # A duplicate may finish its setup first; it must not start a watcher.
        assert await async_setup_entry(hass, second)
        forward.assert_not_awaited()
        assert await async_setup_entry(hass, first)
        assert cleanup.call_count == 2
        assert [c.kwargs['source_device_id'] for c in cleanup.call_args_list] == [
            'device-account-one', 'device-account-two']
        assert await async_setup_entry(hass, first)
        assert cleanup.call_count == 2  # No cleanup on subsequent loads.
    assert entries == [first]
    assert first.data == {}
    assert first.unique_id == 'simplirtc'
    assert first.minor_version == CloudConfigFlow.MINOR_VERSION == 2
    assert {r.entity_id for r in records} == {'camera.one', 'camera.two', 'event.old_motion'}
    assert all(r.config_entry_id == 'first' for r in records)
    assert operations == [('transfer', 'camera.two'), ('transfer', 'event.old_motion'), ('remove', 'second')]
