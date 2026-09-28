from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Sequence


@dataclass(frozen=True)
class MediaCommand:
    action: str
    value: Optional[float] = None


class MprisMediaControls:
    """MPRIS bridge for desktop and headphone media controls."""

    _BUS_NAME = "org.mpris.MediaPlayer2.synthermia"
    _OBJECT_PATH = "/org/mpris/MediaPlayer2"

    def __init__(self, title: str = "Synthermia") -> None:
        self._commands: Deque[MediaCommand] = deque()
        self._commands_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: Dict[str, object] = {
            "paused": True,
            "position_sec": 0.0,
            "volume": 1.0,
            "title": str(title or "Synthermia"),
        }
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._player_service = None
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[Exception] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._run, name="synthermia-mpris", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        self._thread = None

    def queue(self, action: str, value: Optional[float] = None) -> None:
        with self._commands_lock:
            self._commands.append(MediaCommand(action, value))

    def pop_commands(self) -> list[MediaCommand]:
        with self._commands_lock:
            out = list(self._commands)
            self._commands.clear()
        return out

    def update(self, *, paused: bool, position_sec: float, volume: float, title: Optional[str] = None) -> None:
        changed: list[str] = []
        with self._state_lock:
            if self._state["paused"] != bool(paused):
                self._state["paused"] = bool(paused)
                changed.append("PlaybackStatus")
            self._state["position_sec"] = max(0.0, float(position_sec))
            next_volume = max(0.0, float(volume))
            if self._state["volume"] != next_volume:
                self._state["volume"] = next_volume
                changed.append("Volume")
            if title is not None and self._state["title"] != str(title or "Synthermia"):
                self._state["title"] = str(title or "Synthermia")
                changed.append("Metadata")
        if changed:
            self._emit_property_changes(changed)

    def snapshot(self) -> Dict[str, object]:
        with self._state_lock:
            return dict(self._state)

    def _emit_property_changes(self, names: Sequence[str]) -> None:
        loop = self._loop
        service = self._player_service
        if loop is not None and service is not None:
            loop.call_soon_threadsafe(service.emit_property_changes, list(names))

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self.error = exc

    async def _serve(self) -> None:
        from dbus_next.aio import MessageBus

        bus = await MessageBus().connect()
        root_service, player_service = _build_mpris_services(self)
        bus.export(self._OBJECT_PATH, root_service)
        bus.export(self._OBJECT_PATH, player_service)
        await bus.request_name(self._BUS_NAME)
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._player_service = player_service
        try:
            await self._stop_event.wait()
        finally:
            self._player_service = None
            self._stop_event = None
            self._loop = None
            bus.disconnect()


def _build_mpris_services(bridge: MprisMediaControls):
    from dbus_next import Variant
    from dbus_next.constants import PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property, method

    root_iface = "org.mpris.MediaPlayer2"
    player_iface = "org.mpris.MediaPlayer2.Player"

    class RootService(ServiceInterface):
        def __init__(self) -> None:
            super().__init__(root_iface)

        @method()
        def Raise(self) -> "":
            return None

        @method()
        def Quit(self) -> "":
            bridge.queue("quit")
            return None

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "Synthermia"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "synthermia"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return []

    class PlayerService(ServiceInterface):
        def __init__(self) -> None:
            super().__init__(player_iface)

        def _state(self) -> Dict[str, object]:
            return bridge.snapshot()

        def _metadata(self) -> Dict[str, Variant]:
            state = self._state()
            return {
                "mpris:trackid": Variant("o", "/org/synthermia/CurrentTrack"),
                "xesam:title": Variant("s", str(state["title"])),
                "xesam:artist": Variant("as", ["Synthermia"]),
            }

        def emit_property_changes(self, names: Sequence[str]) -> None:
            state = self._state()
            values: Dict[str, object] = {}
            for name in names:
                if name == "PlaybackStatus":
                    values[name] = "Paused" if bool(state["paused"]) else "Playing"
                elif name == "Volume":
                    values[name] = float(state["volume"])
                elif name == "Metadata":
                    values[name] = self._metadata()
            if values:
                self.emit_properties_changed(values)

        @method()
        def Next(self) -> "":
            bridge.queue("seek_forward")
            return None

        @method()
        def Previous(self) -> "":
            bridge.queue("seek_back")
            return None

        @method()
        def Pause(self) -> "":
            bridge.queue("pause")
            return None

        @method()
        def PlayPause(self) -> "":
            bridge.queue("toggle_pause")
            return None

        @method()
        def Stop(self) -> "":
            bridge.queue("pause")
            return None

        @method()
        def Play(self) -> "":
            bridge.queue("play")
            return None

        @method()
        def Seek(self, offset: "x") -> "":
            bridge.queue("seek_relative", float(offset) / 1_000_000.0)
            return None

        @method()
        def SetPosition(self, _track_id: "o", position: "x") -> "":
            bridge.queue("seek_absolute", max(0.0, float(position) / 1_000_000.0))
            return None

        @method()
        def OpenUri(self, _uri: "s") -> "":
            return None

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return "Paused" if bool(self._state()["paused"]) else "Playing"

        @dbus_property(access=PropertyAccess.READ)
        def LoopStatus(self) -> "s":
            return "None"

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def Shuffle(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return self._metadata()

        @dbus_property(access=PropertyAccess.READWRITE)
        def Volume(self) -> "d":
            return float(self._state()["volume"])

        @Volume.setter
        def Volume(self, value: "d") -> None:
            bridge.queue("set_volume", max(0.0, float(value)))

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return int(max(0.0, float(self._state()["position_sec"])) * 1_000_000)

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True

    return RootService(), PlayerService()
