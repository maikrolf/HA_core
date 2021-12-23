"""Support for restoring entity states on startup."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, cast

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, State, callback, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
import homeassistant.util.dt as dt_util

from . import entity_registry, start
from .entity import Entity
from .event import async_track_time_interval
from .json import JSONEncoder
from .singleton import singleton
from .storage import Store

DATA_RESTORE_STATE_TASK = "restore_state_task"

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "core.restore_state"
STORAGE_VERSION = 1

# How long between periodically saving the current states to disk
STATE_DUMP_INTERVAL = timedelta(minutes=15)

# How long should a saved state be preserved if the entity no longer exists
STATE_EXPIRATION = timedelta(days=7)


class StoredState:
    """Object to represent a stored state."""

    def __init__(self, state: State, last_seen: datetime) -> None:
        """Initialize a new stored state."""
        self.state = state
        self.last_seen = last_seen

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the stored state."""
        return {"state": self.state.as_dict(), "last_seen": self.last_seen}

    @classmethod
    def from_dict(cls, json_dict: dict) -> StoredState:
        """Initialize a stored state from a dict."""
        last_seen = json_dict["last_seen"]

        if isinstance(last_seen, str):
            last_seen = dt_util.parse_datetime(last_seen)

        return cls(State.from_dict(json_dict["state"]), last_seen)


class RestoreStateData:
    """Helper class for managing the helper saved data."""

    @staticmethod
    @singleton(DATA_RESTORE_STATE_TASK)
    async def async_get_instance(hass: HomeAssistant) -> RestoreStateData:
        """Get the singleton instance of this data helper."""
        data = RestoreStateData(hass)

        try:
            stored_states = await data.store.async_load()
        except HomeAssistantError as exc:
            _LOGGER.error("Error loading last states", exc_info=exc)
            stored_states = None

        if stored_states is None:
            _LOGGER.debug("Not creating cache - no saved states found")
            data.last_states = {}
        else:
            data.last_states = {
                item["state"]["entity_id"]: StoredState.from_dict(item)
                for item in stored_states
                if valid_entity_id(item["state"]["entity_id"])
            }
            _LOGGER.debug("Created cache with %s", list(data.last_states))

        async def hass_start(hass: HomeAssistant) -> None:
            """Start the restore state task."""
            data.async_setup_dump()

        start.async_at_start(hass, hass_start)

        return data

    @classmethod
    async def async_save_persistent_states(cls, hass: HomeAssistant) -> None:
        """Dump states now."""
        data = await cls.async_get_instance(hass)
        await data.async_dump_states()

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the restore state data class."""
        self.hass: HomeAssistant = hass
        self.store: Store = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, encoder=JSONEncoder
        )
        self.last_states: dict[str, StoredState] = {}
        self.entity_ids: set[str] = set()

    @callback
    def async_get_stored_states(self) -> list[StoredState]:
        """Get the set of states which should be stored.

        This includes the states of all registered entities, as well as the
        stored states from the previous run, which have not been created as
        entities on this run, and have not expired.
        """
        now = dt_util.utcnow()
        all_states = self.hass.states.async_all()
        # Entities currently backed by an entity object
        current_entity_ids = {
            state.entity_id
            for state in all_states
            if not state.attributes.get(entity_registry.ATTR_RESTORED)
        }

        # Start with the currently registered states
        stored_states = [
            StoredState(state, now)
            for state in all_states
            if state.entity_id in self.entity_ids and
            # Ignore all states that are entity registry placeholders
            not state.attributes.get(entity_registry.ATTR_RESTORED)
        ]
        expiration_time = now - STATE_EXPIRATION

        for entity_id, stored_state in self.last_states.items():
            # Don't save old states that have entities in the current run
            # They are either registered and already part of stored_states,
            # or no longer care about restoring.
            if entity_id in current_entity_ids:
                continue

            # Don't save old states that have expired
            if stored_state.last_seen < expiration_time:
                continue

            stored_states.append(stored_state)

        return stored_states

    async def async_dump_states(self) -> None:
        """Save the current state machine to storage."""
        _LOGGER.debug("Dumping states")
        try:
            await self.store.async_save(
                [
                    stored_state.as_dict()
                    for stored_state in self.async_get_stored_states()
                ]
            )
        except HomeAssistantError as exc:
            _LOGGER.error("Error saving current states", exc_info=exc)

    @callback
    def async_setup_dump(self, *args: Any) -> None:
        """Set up the restore state listeners."""

        async def _async_dump_states(*_: Any) -> None:
            await self.async_dump_states()

        # Dump the initial states now. This helps minimize the risk of having
        # old states loaded by overwriting the last states once Home Assistant
        # has started and the old states have been read.
        self.hass.async_create_task(_async_dump_states())

        # Dump states periodically
        cancel_interval = async_track_time_interval(
            self.hass, _async_dump_states, STATE_DUMP_INTERVAL
        )

        async def _async_dump_states_at_stop(*_: Any) -> None:
            cancel_interval()
            await self.async_dump_states()

        # Dump states when stopping hass
        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _async_dump_states_at_stop
        )

    @callback
    def async_restore_entity_added(self, entity_id: str) -> None:
        """Store this entity's state when hass is shutdown."""
        self.entity_ids.add(entity_id)

    @callback
    def async_restore_entity_removed(self, entity_id: str) -> None:
        """Unregister this entity from saving state."""
        # When an entity is being removed from hass, store its last state. This
        # allows us to support state restoration if the entity is removed, then
        # re-added while hass is still running.
        state = self.hass.states.get(entity_id)
        # To fully mimic all the attribute data types when loaded from storage,
        # we're going to serialize it to JSON and then re-load it.
        if state is not None:
            state = State.from_dict(_encode_complex(state.as_dict()))
        if state is not None:
            self.last_states[entity_id] = StoredState(state, dt_util.utcnow())

        self.entity_ids.remove(entity_id)


def _encode(value: Any) -> Any:
    """Little helper to JSON encode a value."""
    try:
        return JSONEncoder.default(
            None,  # type: ignore
            value,
        )
    except TypeError:
        return value


def _encode_complex(value: Any) -> Any:
    """Recursively encode all values with the JSONEncoder."""
    if isinstance(value, dict):
        return {_encode(key): _encode_complex(value) for key, value in value.items()}
    if isinstance(value, list):
        return [_encode_complex(val) for val in value]

    new_value = _encode(value)

    if isinstance(new_value, type(value)):
        return new_value

    return _encode_complex(new_value)


class RestoreEntity(Entity):
    """Mixin class for restoring previous entity state."""

    async def async_internal_added_to_hass(self) -> None:
        """Register this entity as a restorable entity."""
        _, data = await asyncio.gather(
            super().async_internal_added_to_hass(),
            RestoreStateData.async_get_instance(self.hass),
        )
        data.async_restore_entity_added(self.entity_id)

    async def async_internal_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _, data = await asyncio.gather(
            super().async_internal_will_remove_from_hass(),
            RestoreStateData.async_get_instance(self.hass),
        )
        data.async_restore_entity_removed(self.entity_id)

    async def async_get_last_state(self) -> State | None:
        """Get the entity state from the previous run."""
        if self.hass is None or self.entity_id is None:
            # Return None if this entity isn't added to hass yet
            _LOGGER.warning("Cannot get last state. Entity not added to hass")  # type: ignore[unreachable]
            return None
        data = cast(
            RestoreStateData, await RestoreStateData.async_get_instance(self.hass)
        )
        if self.entity_id not in data.last_states:
            return None
        return data.last_states[self.entity_id].state
