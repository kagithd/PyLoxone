"""Connection lifecycle regressions observed during live reloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.loxone as integration
from custom_components.loxone.const import DOMAIN
from custom_components.loxone.pyloxone_api.exceptions import LoxoneConnectionError


def test_unload_marks_intent_before_connection_cleanup(monkeypatch):
    """Intentional shutdown must be visible before the listener can finish."""

    async def scenario():
        cleanup_intent = []
        entry = SimpleNamespace(entry_id="entry-a")
        listening_task = asyncio.create_task(asyncio.Event().wait())
        coordinator = SimpleNamespace(
            config_entry=entry,
            listeners=[],
            _listening_task=listening_task,
            _unloading=False,
        )

        async def cleanup():
            cleanup_intent.append(coordinator._unloading)

        coordinator.async_cleanup = cleanup

        async def unload_platforms(_entry, _platforms):
            return True

        async def deactivate(_hass, _entry_id):
            return None

        hass = SimpleNamespace(
            data={DOMAIN: {entry.entry_id: coordinator}},
            services=SimpleNamespace(async_remove=lambda *_args: None),
            config_entries=SimpleNamespace(async_unload_platforms=unload_platforms),
        )
        monkeypatch.setattr(integration, "async_deactivate_engineering_view", deactivate)

        assert await integration.async_unload_entry(hass, entry) is True
        assert cleanup_intent == [True]
        assert listening_task.cancelled()

    asyncio.run(scenario())


def test_listener_failure_during_unload_does_not_schedule_reload():
    """A deliberate close must not recursively reload the same entry."""

    async def scenario():
        async def fail_listener():
            raise LoxoneConnectionError("closed during unload")

        listener = asyncio.create_task(fail_listener())
        await asyncio.sleep(0)
        scheduled = []

        def schedule(coro):
            scheduled.append(coro)
            coro.close()

        hass = SimpleNamespace(async_create_task=schedule)
        entry = SimpleNamespace(entry_id="entry-a")
        coordinator = SimpleNamespace(_unloading=True)

        integration._handle_listening_task_result(hass, entry, coordinator, listener)

        assert scheduled == []

    asyncio.run(scenario())


def test_listener_recovery_reloads_only_its_config_entry():
    """A real listener failure must not invoke the integration-wide reload service."""

    async def scenario():
        events = []

        async def close():
            events.append("close")

        async def reload_entry(entry_id):
            events.append(("reload", entry_id))

        async def service_call(domain, service):
            events.append(("service", domain, service))

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_reload=reload_entry),
            services=SimpleNamespace(async_call=service_call),
        )
        entry = SimpleNamespace(entry_id="entry-a")
        coordinator = SimpleNamespace(api=SimpleNamespace(close=close), _unloading=False)

        await integration._reload_after_listener_failure(hass, entry, coordinator, delay=0)

        assert events == ["close", ("reload", "entry-a")]

    asyncio.run(scenario())


def test_started_listener_is_tracked_for_unload():
    """The active listener must be available to the unload path."""

    async def scenario():
        async def start_listening(*, callback):
            await asyncio.Event().wait()

        coordinator = SimpleNamespace(
            api=SimpleNamespace(start_listening=start_listening),
            _listening_task=None,
        )
        callback = lambda _message: None
        done_callback = lambda _task: None

        task = integration._start_listening_task(coordinator, callback, done_callback)

        assert coordinator._listening_task is task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_listener_timeout_schedules_entry_recovery():
    """Transport timeouts are expected connection failures, not callback errors."""

    async def scenario():
        async def fail_listener():
            raise TimeoutError("connection timed out")

        listener = asyncio.create_task(fail_listener())
        await asyncio.sleep(0)
        scheduled = []

        def schedule(coro):
            scheduled.append(coro)
            coro.close()

        hass = SimpleNamespace(async_create_task=schedule)
        entry = SimpleNamespace(entry_id="entry-a")
        coordinator = SimpleNamespace(_unloading=False)

        integration._handle_listening_task_result(hass, entry, coordinator, listener)

        assert len(scheduled) == 1

    asyncio.run(scenario())
