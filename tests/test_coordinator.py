"""Offline coordinator unit tests with HA stubs, not full HA runtime tests.

Real asyncio Futures and timeouts model executor completion independently of
Home Assistant, threads, and panel connections. Stub modules are restored after
every test and never replace HA modules outside the test's context.
"""

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "ultrasync"
_PACKAGE = "_ultrasync_coordinator_unit_tests"


def _details(status="Disarmed", sequence=1):
    return {
        "areas": [
            {"bank": 0, "sequence": sequence, "name": "Area 1", "status": status}
        ],
        "zones": [],
        "outputs": [],
        "history_data": [],
    }


class _TrackedFuture(asyncio.Future):
    def __init__(self):
        super().__init__()
        self.exception_reads = 0

    def exception(self):
        self.exception_reads += 1
        return super().exception()


class _ExecutorHassStub:
    def __init__(self):
        self.futures = []
        self.executor_calls = []
        self.bus = SimpleNamespace(fire=MagicMock())

    def async_add_executor_job(self, function, *args):
        self.executor_calls.append((function, args))
        future = _TrackedFuture()
        self.futures.append(future)
        return future


class _DataUpdateCoordinatorStub:
    def __init__(self, hass, logger, *, name, update_interval):
        self.hass = hass
        self.update_interval = update_interval


class _UpdateFailed(Exception):
    pass


def _stub_modules():
    package = ModuleType(_PACKAGE)
    package.__path__ = [str(_COMPONENT)]
    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    const = ModuleType("homeassistant.const")
    const.CONF_SCAN_INTERVAL = "scan_interval"
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = _ExecutorHassStub
    helpers = ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    dispatcher = ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = MagicMock()
    coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    coordinator.DataUpdateCoordinator = _DataUpdateCoordinatorStub
    coordinator.UpdateFailed = _UpdateFailed
    return {
        _PACKAGE: package,
        "homeassistant": homeassistant,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.helpers.update_coordinator": coordinator,
    }


class CoordinatorUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module_patch = patch.dict(sys.modules, _stub_modules())
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.module = importlib.import_module(_PACKAGE + ".coordinator")
        self.hass = _ExecutorHassStub()

    async def asyncTearDown(self):
        for future in self.hass.futures:
            if not future.done():
                future.cancel()
        await asyncio.sleep(0)

    def coordinator(self, legacy=True):
        with patch.object(self.module, "create_client", return_value=MagicMock()):
            return self.module.UltraSyncDataUpdateCoordinator(
                self.hass,
                config={"host": "panel.test", "username": "test", "pin": "0000"},
                options={"legacy_ssl": legacy},
            )

    async def start_poll(self, coordinator):
        task = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(0)
        return task

    async def time_out_poll(self, coordinator):
        coordinator._update_timeout = 0.005
        with self.assertRaises(TimeoutError):
            await coordinator._async_update_data()
        coordinator._update_timeout = 1

    async def test_timeout_preserves_worker_and_next_poll_reuses_it(self):
        coordinator = self.coordinator()
        await self.time_out_poll(coordinator)
        first = self.hass.futures[0]
        self.assertFalse(first.cancelled())
        self.assertFalse(first.done())

        next_poll = await self.start_poll(coordinator)
        self.assertEqual(len(self.hass.executor_calls), 1)
        first.set_result(_details())
        self.assertEqual(await next_poll, {"area01_state": "Disarmed"})

        fresh_poll = await self.start_poll(coordinator)
        self.assertEqual(len(self.hass.executor_calls), 2)
        self.hass.futures[1].set_result(_details("Armed", 2))
        self.assertEqual(await fresh_poll, {"area01_state": "Armed"})

    async def test_cancelled_refresh_does_not_cancel_worker(self):
        coordinator = self.coordinator()
        poll = await self.start_poll(coordinator)
        first = self.hass.futures[0]
        poll.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await poll
        self.assertFalse(first.cancelled())

        next_poll = await self.start_poll(coordinator)
        self.assertEqual(len(self.hass.executor_calls), 1)
        first.set_result(_details())
        self.assertEqual(await next_poll, {"area01_state": "Disarmed"})

    async def test_completed_late_result_is_consumed_before_starting_new_work(self):
        coordinator = self.coordinator()
        await self.time_out_poll(coordinator)
        self.hass.futures[0].set_result(_details())
        await asyncio.sleep(0)

        result = await coordinator._async_update_data()
        self.assertEqual(result, {"area01_state": "Disarmed"})
        self.assertEqual(len(self.hass.executor_calls), 1)

    async def test_late_error_is_retrieved_and_next_poll_observes_it(self):
        coordinator = self.coordinator()
        await self.time_out_poll(coordinator)
        first = self.hass.futures[0]
        first.set_exception(RuntimeError("Synthetic worker failure"))
        await asyncio.sleep(0)
        # Retrieval prevents an unhandled-Future warning if no refresh follows.
        self.assertGreaterEqual(first.exception_reads, 1)

        with self.assertRaisesRegex(RuntimeError, "Synthetic worker failure"):
            await coordinator._async_update_data()
        self.assertEqual(len(self.hass.executor_calls), 1)

        fresh_poll = await self.start_poll(coordinator)
        self.assertEqual(len(self.hass.executor_calls), 2)
        self.hass.futures[1].set_result(_details())
        await fresh_poll

    async def test_worker_cancellation_does_not_permanently_block_new_work(self):
        coordinator = self.coordinator()
        poll = await self.start_poll(coordinator)
        self.hass.futures[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await poll

        fresh_poll = await self.start_poll(coordinator)
        self.assertEqual(len(self.hass.executor_calls), 2)
        self.hass.futures[1].set_result(_details())
        await fresh_poll

    async def test_false_details_is_update_failure_in_both_modes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                coordinator = self.coordinator(legacy=legacy)
                poll = await self.start_poll(coordinator)
                self.hass.futures[-1].set_result({})
                with self.assertRaises(_UpdateFailed):
                    await poll

    async def test_worker_timeout_error_is_consumed_and_can_retry(self):
        coordinator = self.coordinator()
        poll = await self.start_poll(coordinator)
        self.hass.futures[0].set_exception(TimeoutError("Synthetic worker timeout"))
        with self.assertRaisesRegex(TimeoutError, "Synthetic worker timeout"):
            await poll

        fresh_poll = await self.start_poll(coordinator)
        self.assertEqual(len(self.hass.executor_calls), 2)
        self.hass.futures[1].set_result(_details())
        await fresh_poll


if __name__ == "__main__":
    unittest.main()
