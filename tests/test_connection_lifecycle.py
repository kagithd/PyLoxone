"""Connection lifecycle regressions observed during live reloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.loxone as integration
import custom_components.loxone.pyloxone_api.connection as connection_module
from custom_components.loxone.const import DOMAIN
from custom_components.loxone.pyloxone_api.connection import LoxoneConnection, MessageForQueue
from custom_components.loxone.pyloxone_api.exceptions import LoxoneConnectionError


@pytest.mark.parametrize("prepared", [False, True])
def test_listener_opens_once_and_immediately_starts_handshake(prepared):
    """Prepare fresh callers, but reuse HA preparation without idling a socket."""
    async def scenario():
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")
        if prepared:
            connection._session_key = b"prepared-session"
        handshaking = asyncio.Event()
        opens = []

        async def send(_message):
            handshaking.set()
            await asyncio.Event().wait()

        socket = SimpleNamespace(send=send)

        async def open_websocket():
            opens.append(True)
            return socket

        async def open_connection():
            assert not prepared, "listener repeated configuration preparation"
            connection._session_key = b"prepared-session"
            return await open_websocket()

        connection._open_websocket = open_websocket
        connection.open = open_connection
        listener = asyncio.create_task(connection.start_listening())
        try:
            await asyncio.wait_for(handshaking.wait(), timeout=0.2)
            assert connection.connection is socket
            assert opens == [True]
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_initial_connection_failure_is_delegated_to_home_assistant(monkeypatch):
    """Config-entry setup must not hide one failure behind an internal retry loop."""

    async def scenario():
        attempts = 0

        class FailingHttpClient:
            def __init__(self, **_kwargs):
                pass

            async def get(self, _endpoint):
                nonlocal attempts
                attempts += 1
                raise ConnectionError("synthetic connection failure")

        async def unexpected_sleep(_delay):
            raise AssertionError("connection retry must be scheduled by Home Assistant")

        monkeypatch.setattr(connection_module, "LoxoneAsyncHttpClient", FailingHttpClient)
        monkeypatch.setattr(connection_module.asyncio, "sleep", unexpected_sleep)
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")

        with pytest.raises(ConnectionError, match="synthetic connection failure"):
            await connection.open(session=SimpleNamespace(closed=False))

        assert attempts == 1

    asyncio.run(scenario())


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


def test_listener_recovery_delegates_cleanup_to_config_entry_reload():
    """Recovery must not hang on a duplicate close before config-entry unload."""

    async def scenario():
        events = []

        async def close():
            raise AssertionError("config-entry unload owns connection cleanup")

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

        assert events == [("reload", "entry-a")]

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


@pytest.mark.parametrize("failure", [TimeoutError("connection timed out"), ConnectionError("connection reset")])
def test_listener_transport_failure_schedules_entry_recovery(failure):
    """Transport recovery runs in the background and cannot hold up HA startup."""

    async def scenario():
        async def fail_listener():
            raise failure

        listener = asyncio.create_task(fail_listener())
        await asyncio.sleep(0)
        scheduled = []

        def schedule(coro, name):
            scheduled.append((coro, name))
            coro.close()

        hass = SimpleNamespace(async_create_background_task=schedule)
        entry = SimpleNamespace(entry_id="entry-a")
        coordinator = SimpleNamespace(_unloading=False)

        integration._handle_listening_task_result(hass, entry, coordinator, listener)

        assert len(scheduled) == 1
        assert scheduled[0][1] == "PyLoxone listener recovery"

    asyncio.run(scenario())


def test_unload_is_bounded_when_connection_shutdown_stalls(monkeypatch):
    """A stuck connection task must not block config-entry reload forever."""

    async def scenario():
        release = asyncio.Event()
        cleanup_attempts = 0

        async def resist_cancellation():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        async def cleanup():
            nonlocal cleanup_attempts
            cleanup_attempts += 1
            await resist_cancellation()

        entry = SimpleNamespace(entry_id="entry-a")
        listening_task = asyncio.create_task(resist_cancellation())
        coordinator = SimpleNamespace(
            config_entry=entry,
            listeners=[],
            _listening_task=listening_task,
            _unloading=False,
            async_cleanup=cleanup,
        )

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
        monkeypatch.setattr(integration, "_UNLOAD_STEP_TIMEOUT", 0.01, raising=False)

        unload_task = asyncio.create_task(integration.async_unload_entry(hass, entry))
        try:
            assert await asyncio.wait_for(asyncio.shield(unload_task), timeout=0.1) is False
            assert hass.data[DOMAIN][entry.entry_id] is coordinator
            assert not listening_task.done()
        finally:
            release.set()
            await asyncio.wait_for(unload_task, timeout=1)
            await asyncio.sleep(0)
        assert await integration.async_unload_entry(hass, entry) is True
        assert cleanup_attempts == 1, "retry started a second cleanup instead of joining the first"

    asyncio.run(scenario())


def test_connection_close_discards_queued_commands():
    """Shutdown must not execute stale actuator commands or wait for a consumer."""

    async def scenario():
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")
        await connection._message_queue.put(MessageForQueue("first", True))
        await connection._message_queue.put(MessageForQueue("second", False))
        secured_command = asyncio.sleep(0)
        await connection._secured_queue.put(secured_command)

        try:
            await asyncio.wait_for(connection.close(), timeout=0.1)

            assert connection._message_queue.empty()
            assert connection._secured_queue.empty()
            await asyncio.wait_for(connection._message_queue.join(), timeout=0.1)
            await asyncio.wait_for(connection._secured_queue.join(), timeout=0.1)
        finally:
            secured_command.close()

    asyncio.run(scenario())


def test_closed_connection_rejects_new_commands():
    """No actuator command may enter either queue after shutdown starts."""

    async def scenario():
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")
        connection._closed = True
        connection._shutdown_event.set()

        with pytest.raises(LoxoneConnectionError):
            await connection.send_websocket_command("device", "on")
        with pytest.raises(LoxoneConnectionError):
            await connection.send_secured__websocket_command("device", "on", "code")

        assert connection._message_queue.empty()
        assert connection._secured_queue.empty()

    asyncio.run(scenario())


def test_rejected_secured_command_is_rolled_back():
    """A partially full queue must not retain a command reported as rejected."""

    async def scenario():
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")
        connection._message_queue = asyncio.Queue(maxsize=1)
        await connection._message_queue.put(MessageForQueue("occupied", True))

        try:
            with pytest.raises(RuntimeError):
                await connection.send_secured__websocket_command("device", "on", "code")
            assert connection._secured_queue.empty()
        finally:
            while not connection._secured_queue.empty():
                command = connection._secured_queue.get_nowait()
                command.close()
                connection._secured_queue.task_done()

    asyncio.run(scenario())


def test_connection_close_cancels_in_flight_queue_send():
    """Closing the worker must also cancel the command it is currently sending."""

    async def scenario():
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")
        send_started = asyncio.Event()
        send_cancelled = asyncio.Event()
        release_send = asyncio.Event()

        async def blocked_send(_command, *, encrypted):
            send_started.set()
            try:
                await release_send.wait()
            except asyncio.CancelledError:
                send_cancelled.set()
                raise

        connection._send_text_command = blocked_send
        await connection._message_queue.put(MessageForQueue("actuator", True))
        worker = asyncio.create_task(connection._process_message())
        connection._pending_task = [worker]
        await asyncio.wait_for(send_started.wait(), timeout=0.1)

        try:
            await asyncio.wait_for(connection.close(), timeout=0.1)
            assert send_cancelled.is_set()
            await asyncio.wait_for(connection._message_queue.join(), timeout=0.1)
        finally:
            release_send.set()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_completed_failed_listener_does_not_block_unload(monkeypatch):
    """A stopped listener is safe to unload even when it ended with a connection error."""

    async def scenario():
        async def fail_listener():
            raise LoxoneConnectionError("connection lost")

        entry = SimpleNamespace(entry_id="entry-a")
        listening_task = asyncio.create_task(fail_listener())
        await asyncio.sleep(0)
        coordinator = SimpleNamespace(
            config_entry=entry,
            listeners=[],
            _listening_task=listening_task,
            _unloading=False,
        )

        async def cleanup():
            return None

        coordinator.async_cleanup = cleanup
        unloaded = []

        async def unload_platforms(_entry, _platforms):
            unloaded.append(_entry.entry_id)
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
        assert unloaded == [entry.entry_id]

    asyncio.run(scenario())


def test_cancelled_connection_close_can_be_retried():
    """An interrupted close must not claim resources were fully closed."""

    async def scenario():
        connection = LoxoneConnection("192.0.2.1", "user", "test-password")
        first_attempt_started = asyncio.Event()
        websocket_closed = []
        close_attempts = 0

        class OpenState:
            CLOSED = "closed"

        async def close_websocket():
            nonlocal close_attempts
            close_attempts += 1
            if close_attempts == 1:
                first_attempt_started.set()
                await asyncio.Event().wait()
            websocket_closed.append(True)

        connection.connection = SimpleNamespace(state=OpenState(), close=close_websocket)

        first_close = asyncio.create_task(connection.close())
        await asyncio.wait_for(first_attempt_started.wait(), timeout=0.1)
        first_close.cancel()
        try:
            await first_close
        except asyncio.CancelledError:
            pass

        assert connection._closed is False
        await asyncio.wait_for(connection.close(), timeout=0.1)
        assert connection._closed is True
        assert websocket_closed == [True]

    asyncio.run(scenario())
