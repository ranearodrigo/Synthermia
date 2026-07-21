from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


@dataclass(frozen=True)
class MediaCommand:
    action: str
    value: Optional[float] = None


class MprisMediaControls:
    """Optional MPRIS bridge for desktop/headphone media controls."""

    def __init__(self, title: str = "Synthermia") -> None:
        self._title = str(title or "Synthermia")
        self._commands: Deque[MediaCommand] = deque()
        self._commands_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: Dict[str, object] = {
            "paused": True,
            "position_sec": 0.0,
            "volume": 1.0,
        }
        self._loop = None
        self._thread: Optional[threading.Thread] = None
        self._bus_name = None
        self._service = None

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._run, name="synthermia-mpris", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass
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
        with self._state_lock:
            self._state["paused"] = bool(paused)
            self._state["position_sec"] = max(0.0, float(position_sec))
            self._state["volume"] = max(0.0, float(volume))
            if title is not None:
                self._title = str(title or "Synthermia")

    def snapshot(self) -> Dict[str, object]:
        with self._state_lock:
            state = dict(self._state)
            state["title"] = self._title
        return state

    def _run(self) -> None:
        try:
            import dbus
            import dbus.service
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib
        except Exception:
            return

        try:
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SessionBus()
            self._bus_name = dbus.service.BusName(
                "org.mpris.MediaPlayer2.synthermia",
                bus=bus,
                allow_replacement=True,
                replace_existing=True,
                do_not_queue=True,
            )
            service_class = _build_mpris_service_class()
            self._service = service_class(bus, self)
            loop = GLib.MainLoop()
            self._loop = loop
            loop.run()
        except Exception:
            return
        finally:
            self._loop = None
            self._service = None
            self._bus_name = None


def _build_mpris_service_class():
    import dbus
    import dbus.service

    player_iface = "org.mpris.MediaPlayer2.Player"
    root_iface = "org.mpris.MediaPlayer2"
    props_iface = "org.freedesktop.DBus.Properties"

    class Service(dbus.service.Object):  # type: ignore[misc]
        def __init__(self, bus, bridge: MprisMediaControls) -> None:
            super().__init__(bus, "/org/mpris/MediaPlayer2")
            self._bridge = bridge

        @dbus.service.method(root_iface, in_signature="", out_signature="")
        def Raise(self) -> None:
            return None

        @dbus.service.method(root_iface, in_signature="", out_signature="")
        def Quit(self) -> None:
            self._bridge.queue("quit")

        @dbus.service.method(player_iface, in_signature="", out_signature="")
        def Next(self) -> None:
            self._bridge.queue("seek_forward")

        @dbus.service.method(player_iface, in_signature="", out_signature="")
        def Previous(self) -> None:
            self._bridge.queue("seek_back")

        @dbus.service.method(player_iface, in_signature="", out_signature="")
        def Pause(self) -> None:
            self._bridge.queue("pause")

        @dbus.service.method(player_iface, in_signature="", out_signature="")
        def PlayPause(self) -> None:
            self._bridge.queue("toggle_pause")

        @dbus.service.method(player_iface, in_signature="", out_signature="")
        def Stop(self) -> None:
            self._bridge.queue("pause")

        @dbus.service.method(player_iface, in_signature="", out_signature="")
        def Play(self) -> None:
            self._bridge.queue("play")

        @dbus.service.method(player_iface, in_signature="x", out_signature="")
        def Seek(self, offset: int) -> None:
            self._bridge.queue("seek_relative", float(offset) / 1_000_000.0)

        @dbus.service.method(player_iface, in_signature="ox", out_signature="")
        def SetPosition(self, _track_id, position: int) -> None:
            self._bridge.queue("seek_absolute", max(0.0, float(position) / 1_000_000.0))

        @dbus.service.method(player_iface, in_signature="s", out_signature="")
        def OpenUri(self, _uri: str) -> None:
            return None

        @dbus.service.method(props_iface, in_signature="ss", out_signature="v")
        def Get(self, interface: str, prop: str):
            props = self._properties_for(interface)
            if prop not in props:
                raise dbus.exceptions.DBusException(
                    "org.freedesktop.DBus.Error.InvalidArgs",
                    f"Unknown property {interface}.{prop}",
                )
            return props[prop]

        @dbus.service.method(props_iface, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface: str):
            return dbus.Dictionary(self._properties_for(interface), signature="sv")

        @dbus.service.method(props_iface, in_signature="ssv", out_signature="")
        def Set(self, interface: str, prop: str, value) -> None:
            if interface == player_iface and prop == "Volume":
                self._bridge.queue("set_volume", max(0.0, float(value)))
                return
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.PropertyReadOnly",
                f"Property {interface}.{prop} is read-only",
            )

        def _properties_for(self, interface: str) -> Dict[str, object]:
            state = self._bridge.snapshot()
            if interface == root_iface:
                return {
                    "CanQuit": dbus.Boolean(True),
                    "CanRaise": dbus.Boolean(False),
                    "HasTrackList": dbus.Boolean(False),
                    "Identity": dbus.String("Synthermia"),
                    "DesktopEntry": dbus.String("synthermia"),
                    "SupportedUriSchemes": dbus.Array([], signature="s"),
                    "SupportedMimeTypes": dbus.Array([], signature="s"),
                }
            if interface == player_iface:
                paused = bool(state.get("paused", True))
                title = str(state.get("title", "Synthermia") or "Synthermia")
                position_usec = int(max(0.0, float(state.get("position_sec", 0.0))) * 1_000_000)
                return {
                    "PlaybackStatus": dbus.String("Paused" if paused else "Playing"),
                    "LoopStatus": dbus.String("None"),
                    "Rate": dbus.Double(1.0),
                    "Shuffle": dbus.Boolean(False),
                    "Metadata": dbus.Dictionary(
                        {
                            "mpris:trackid": dbus.ObjectPath("/org/synthermia/CurrentTrack"),
                            "xesam:title": dbus.String(title),
                        },
                        signature="sv",
                    ),
                    "Volume": dbus.Double(max(0.0, float(state.get("volume", 1.0)))),
                    "Position": dbus.Int64(position_usec),
                    "MinimumRate": dbus.Double(1.0),
                    "MaximumRate": dbus.Double(1.0),
                    "CanGoNext": dbus.Boolean(True),
                    "CanGoPrevious": dbus.Boolean(True),
                    "CanPlay": dbus.Boolean(True),
                    "CanPause": dbus.Boolean(True),
                    "CanSeek": dbus.Boolean(True),
                    "CanControl": dbus.Boolean(True),
                }
            return {}

    return Service
