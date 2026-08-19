import asyncio
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
from .engineering_config import EngineeringInventory, download_engineering_inventory
from .engineering_runtime import (
    RUNTIME_PROBE_CONCURRENCY,
    EngineeringRuntimeInventory,
    RuntimeProbeClient,
    async_probe_engineering_runtime,
)
from .miniserver import MiniServer
from .pyloxone_api.connection import LoxoneConnection, LoxoneException

_LOGGER = logging.getLogger(__name__)


class LoxoneCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Loxone Miniserver."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name="PyLoxone Coordinator",
            update_method=None,  # Not polling!
        )
        self.config_entry = config_entry
        self._username = config_entry.options[CONF_USERNAME]
        self._password = config_entry.options[CONF_PASSWORD]
        self._host = config_entry.options[CONF_HOST]
        self._port = config_entry.options[CONF_PORT]
        self._verify_ssl = config_entry.options.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

        self.api: LoxoneConnection | None = None
        self.miniserver: MiniServer | None = None
        self.listeners = []
        self.engineering_inventory: EngineeringInventory | None = None
        self.engineering_runtime: EngineeringRuntimeInventory | None = None

    async def async_config_entry_first_refresh(self) -> None:
        _LOGGER.debug("async_config_entry_first_refresh")
        if self.api and self.api.connection:
            await self.api.close()
            self.api.connection = None

        if "token" in self.config_entry.data:
            self.api = LoxoneConnection(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                token=self.config_entry.data,
                verify_ssl=self._verify_ssl,
            )
        else:
            self.api = LoxoneConnection(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                verify_ssl=self._verify_ssl,
            )
        try:
            session = async_get_clientsession(self.hass)
            await self.api.open(session)
        except LoxoneException as e:
            _LOGGER.error("Could not connect to Loxone Miniserver")
            raise e
        except Exception as e:
            _LOGGER.error("Could not connect to Loxone Miniserver")
            raise e

        self.miniserver = MiniServer(self.hass, self.api.structure_file, self.config_entry)

        return None

    async def _async_update_data(self) -> None:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        print("_async_update_data")
        return None

    async def async_refresh_engineering_inventory(self) -> EngineeringInventory:
        """Read and parse the complete engineering config on explicit request."""
        inventory = await self.hass.async_add_executor_job(
            lambda: download_engineering_inventory(
                self._host,
                self._username,
                self._password,
                verify_ssl=self._verify_ssl,
            )
        )
        self.engineering_inventory = inventory
        session = async_get_clientsession(self.hass)
        runtime_client = RuntimeProbeClient(
            session=session,
            base_url=f"{self.api.scheme}://{self.api.url}",
            auth=aiohttp.BasicAuth(self._username, self._password, encoding="utf-8"),
            verify_ssl=self._verify_ssl,
            semaphore=asyncio.Semaphore(RUNTIME_PROBE_CONCURRENCY),
        )
        self.engineering_runtime = await async_probe_engineering_runtime(
            inventory,
            client=runtime_client,
        )
        return inventory

    async def async_cleanup(self):
        """Clean up resources."""
        if hasattr(self, "listeners"):
            # Clean up all event listeners
            for listener in self.listeners:
                if listener is not None:
                    listener()
            self.listeners = []

        # Close API connection
        if hasattr(self, "api"):
            await self.api.close()
