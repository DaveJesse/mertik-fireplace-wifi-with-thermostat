"""Mertik Maxitrol WiFi fireplace controller."""

import asyncio
import logging
import socket
import threading
import time

_LOGGER = logging.getLogger(__name__)

COMMAND_PREFIX = "0233303330333033303830"
STATUS_PREFIXES = ("303030300003", "030300000003")
# Reply sent instead of a status packet when the WiFi box has lost its radio
# link to the fireplace receiver (the MyFire app shows F43 "No receiver
# connected"). Seen repeated for every command until the link is restored.
RECEIVER_LOST_PREFIX = "3030303000E1"
FCODE_RECEIVER_LOST = 43
# Only report F43 once the box has sent nothing but "no receiver" replies for
# this long (three 10 s polls). The box can send an occasional stray one in
# normal use; a real loss of link repeats it for every command.
RECEIVER_LOST_AFTER_SECONDS = 30.0
# Report "not responding" once the box has sent nothing at all for this long
# (every command timing out). Seen when the box loses the fireplace entirely.
NOT_RESPONDING_AFTER_SECONDS = 30.0
# Shortest text that holds every status field we read (up to set temp [32:36]).
MIN_STATUS_LEN = 36
TCP_PORT = 2000
SOCKET_TIMEOUT = 3
# Commands (anything other than the status poll) are sent one at a time: after
# each, wait for the box's acknowledgement -- "3030303000" followed by the
# first two characters of the command, e.g. ...2000 for aux off, ...1A for
# ignite -- for up to ACK_TIMEOUT_SECONDS, then leave COMMAND_GAP_SECONDS
# before the next command. Sending a second command while the receiver was
# still handling the first got a "no receiver" (E1) reply in 12 of 13 cases in
# captures, and once was followed by a 90-minute loss of the receiver (F43).
ACK_PREFIX = "3030303000"
ACK_TIMEOUT_SECONDS = 3.0
COMMAND_GAP_SECONDS = 2.0
RECV_BUFFER = 4096

CMD_STATUS = "303303"
CMD_APP_MODE = "303103"
CMD_STANDBY = "3136303003"
CMD_IGNITE = "314103"
CMD_GUARD_FLAME_OFF = "313003"
CMD_AUX_ON = "32303031030a"
CMD_AUX_OFF = "32303030030a"
CMD_LIGHT_ON = "3330303103"
CMD_LIGHT_OFF = "3330303003"
CMD_SET_ECO = "4233303103"
CMD_SET_MANUAL = "423003"
CMD_FLAME_PREFIX = "3136"
CMD_FLAME_SUFFIX = "03"
CMD_BRIGHTNESS_PREFIX = "33304645"
CMD_BRIGHTNESS_SUFFIX = "03"
CMD_THERMOSTAT_PREFIX = "4231"
CMD_THERMOSTAT_SUFFIX = "03"

# Light levels sent with the brightness command, as ASCII hex: "77" ... "FB".
# Measured on a Gazco fire: 66 and below leave the light dark, 77 is very dim,
# and above CC the change is hard to see.
BRIGHTNESS_CODES = [
    "3737", "3838", "3939", "4141", "4242", "4343", "4444", "4545", "4642",
]

FLAME_HEIGHT_STEPS = [
    "3830",
    "3842",
    "3937",
    "4132",
    "4145",
    "4239",
    "4335",
    "4430",
    "4443",
    "4537",
    "4633",
    "4646",
]

# Status packet layout: character offsets in the text after STX. Field names
# follow the MyFire app (see PROTOCOL.md, with thanks to diederikm).
# [12:14] setup state (feature bits)
# [14:16] burner output: gas valve position 00-FF. 00 off / standby (pilot
#         only, sometimes 20), 80 lowest step ... FF highest step.
STATUS_BURNER_OUTPUT = slice(14, 16)
# [16:20] receiver state: 16-bit word, see the WORD_* masks.
STATUS_WORD = slice(16, 20)
# [20:22] light level (brightness last set, reported with the light off too)
STATUS_LIGHT = slice(20, 22)
# [22:24] fan level; [24:26] system mode (char 24: 0 manual, 1 thermostat,
# 2 eco); [26:28] system flags.
STATUS_MODE = slice(24, 26)
STATUS_FLAGS = slice(26, 28)
# [28:32] room temperature from the handset, x10 (00E1 = 22.5 C).
# "0666" when the handset is not connected (F44).
STATUS_ROOM_TEMP = slice(28, 32)
ROOM_TEMP_NO_HANDSET = "0666"
# [32:36] set temperature, x10 (00C8 = 20.0 C).
STATUS_SET_TEMP = slice(32, 36)

# Receiver state word bits.
WORD_RECEIVER_LINK = 0x8000  # clear briefly while igniting/shutting down:
#                              the other values in that frame are unreliable
WORD_HANDSET = 0x0200  # app: handsetConnected (never set on our fireplace)
WORD_SHUTTING_DOWN = 0x0100  # app: ignitionNotAllowed
WORD_PILOT = 0x0080  # app: mainBurnerOn, but set whenever the pilot burns
WORD_FAN = 0x0040
WORD_IGNITING = 0x0010
# Aux (rear burner) setting. Every aux_off clears it (8D->85, 8F->87,
# CF->C7); it stays set while only the pilot burns after a standby.
WORD_AUX = 0x0008
# Fire light on, with the fire lit or out. Set by light_on (8B->8F, 09->0D),
# cleared by light_off (87->83) and when the fire goes out; follows the
# handset's light button (8D->89->8D), which sends no light message.
WORD_LIGHT = 0x0004
WORD_FAULTED = 0x0002

# Burner output above this means the burner is lit; at or below it, only the
# pilot (standby reports 00 or 20, the lowest step is 80).
BURNER_LIT_ABOVE = 0x40

# Light messages: "3030303000" + "3001"/"3000" + brightness code. Sent as the
# acknowledgement of HA's light commands. NOT sent when the light is switched
# with the handset, so not used for the light state.
LIGHT_ON_MSG = "30303030003001"
LIGHT_OFF_MSG = "30303030003000"

FCODE_HANDSET = 44  # F44: handset not in range or low battery

# After a light command, a status packet may still show the old state for a
# moment; the reported state is ignored for this long.
LIGHT_COMMAND_SETTLE_SECONDS = 8.0
# Light level byte reported for each level 1-9 (BRIGHTNESS_CODES as values).
LIGHT_LEVEL_BYTES = [0x77, 0x88, 0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFB]
# A shutdown seen this recently counts for ignition_settled.
RECENT_SHUTDOWN_SECONDS = 120.0
# Handset temperature unchanged for this long with the burner lit = stale.
HANDSET_STALE_SECONDS = 30 * 60
# A status this long after the last command is taken to show its effect
# (the valve moves FF -> 80 within about 10 s).
STATE_SETTLE_SECONDS = 8.0
# Flame height the fireplace lights at (handset, MyFire app and ignite
# command all start at full height, both burners).
IGNITION_FLAME_HEIGHT = 12
# Burner output of each flame step 1-12 (the values set_flame_height sends).
FLAME_STEP_OUTPUTS = [int(bytes.fromhex(c).decode(), 16) for c in FLAME_HEIGHT_STEPS]


def flame_step_from_output(output: int) -> int:
    """Nearest flame step (1-12) for a burner output; 0 if only the pilot."""
    if output <= BURNER_LIT_ABOVE:
        return 0
    nearest = min(FLAME_STEP_OUTPUTS, key=lambda v: abs(v - output))
    return FLAME_STEP_OUTPUTS.index(nearest) + 1


class Mertik:
    def __init__(self, ip: str) -> None:
        self.ip = ip

        # Flame height last commanded; used only until a status packet has
        # reported the burner output (see get_flame_height).
        self.flameHeight = 0
        self._local_aux = False  # aux last commanded (fallback before a status)

        # From the status packet. Values from a frame without the receiver
        # link bit are not used (see _process_status).
        self.on = False  # burner lit (output above the pilot level)
        self.flame_on = False  # any flame: pilot or burner (WORD_PILOT)
        self._burner_output: int | None = None
        self._aux_reported: bool | None = None
        self._aux_bit = False  # aux setting as reported (also on pilot)
        self._light_byte = 0  # light level byte [20:22] (brightness last set)
        # time.monotonic() of the last frame showing "shutting down", and of
        # the first frame since then showing the fire fully out (see
        # ignition_settled).
        self._shutdown_seen_at: float | None = None
        self._out_since: float | None = None
        # Handset staleness (see _process_status).
        self._room_raw: str | None = None
        self._room_changed_at = time.monotonic()
        self._burner_lit_since: float | None = None
        self._handset_stale = False
        self._last_status_at = 0.0  # time.monotonic() of the last usable frame
        self._shutting_down = False
        self._guard_flame_on = False
        self._igniting = False
        self._prev_flame_on = False
        self._handset_connected = True
        self._ambient_temperature: float | None = None
        self._set_temperature: float | None = None
        # Light on/off from the status word (None until a status packet), and
        # time.monotonic() of the last light command. See light_reported.
        self._light_bit: bool | None = None
        self._light_command_at: float | None = None
        # Latest light on/off message (acknowledgement of HA's light commands).
        self._light_event: bool | None = None
        # Receiver link: whether a frame with the link bit has been seen, and
        # time.monotonic() since frames have arrived without it (or None).
        self._link_seen = False
        self._unlinked_since: float | None = None
        # time.monotonic() of the first "no receiver" reply since the last
        # status packet, or None. See receiver_lost.
        self._no_receiver_since: float | None = None
        # time.monotonic() of the last reply of any kind from the box.
        self._last_reply = time.monotonic()
        # time.monotonic() when the last command finished (ack or timeout).
        self._last_command_done = 0.0
        # One exchange with the box at a time.
        self._io_lock = threading.RLock()
        # Number of usable status packets processed (lets callers tell
        # whether a status has arrived since some event).
        self.status_count = 0

        # Fail now (and let HA retry the setup) if the box is not reachable.
        self._open().close()
        self._startup_sequence()

    @classmethod
    async def async_connect(cls, ip: str) -> "Mertik":
        """Create a Mertik instance (checks the box is reachable) via executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls, ip)

    def _startup_sequence(self) -> None:
        """Read the status and the device information once.

        CMD_GUARD_FLAME_OFF is NOT sent at startup: nothing is sent to the
        fire unless the user asks. To make sure the fire is off after an HA
        restart, use the Safety shutoff blueprint.
        """
        self._send_command(CMD_STATUS)
        self._send_command(CMD_APP_MODE)

    @property
    def is_on(self) -> bool:
        return self.on

    @property
    def is_flame_on(self) -> bool:
        """Any flame burning: pilot or burner."""
        return self.flame_on

    @property
    def is_burner_lit(self) -> bool:
        """Main burner lit (not just the pilot), from the burner output."""
        return self.on

    @property
    def receiver_lost(self) -> bool:
        """True once the box has had no link to the receiver for a while (F43).

        Either "no receiver" replies, or status frames without the receiver
        link bit, for RECEIVER_LOST_AFTER_SECONDS with no good status between.
        """
        now = time.monotonic()
        for since in (self._no_receiver_since, self._unlinked_since):
            if since is not None and now - since >= RECEIVER_LOST_AFTER_SECONDS:
                return True
        return False

    def take_light_event(self) -> bool | None:
        """Return the latest light on/off message since the last call, once."""
        event, self._light_event = self._light_event, None
        return event

    @property
    def light_reported(self) -> bool | None:
        """Light on/off as reported by the fireplace, or None if not known.

        None until the first status packet, and for a few seconds after a
        light command (the next status shows the new state).
        """
        if (
            self._light_command_at is not None
            and time.monotonic() - self._light_command_at
            < LIGHT_COMMAND_SETTLE_SECONDS
        ):
            return None
        return self._light_bit

    @property
    def not_responding(self) -> bool:
        """True once the box has not replied to anything for a while."""
        return time.monotonic() - self._last_reply >= NOT_RESPONDING_AFTER_SECONDS

    @property
    def is_pilot_on(self) -> bool:
        """Pilot flame lit (also set while the burner burns)."""
        return self._guard_flame_on

    @property
    def is_aux_on(self) -> bool:
        """Aux (rear) burner lit: aux bit set and the main burner lit.

        The aux bit stays set while only the pilot burns, so it is gated on
        the burner output. Falls back to the last command before a status.
        """
        if self._aux_reported is not None:
            return self._aux_reported and self.on
        return self._local_aux and self.flame_on

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def is_igniting(self) -> bool:
        return self._igniting

    @property
    def fault_code(self) -> int:
        """Return the active fault as an F-code number (e.g. 44 for F44), or 0."""
        if self.receiver_lost:
            return FCODE_RECEIVER_LOST
        if not self.is_handset_connected:
            return FCODE_HANDSET
        return 0

    @property
    def is_handset_connected(self) -> bool:
        """Handset reported, and its temperature is not stale."""
        return self._handset_connected and not self._handset_stale

    @property
    def ambient_temperature(self) -> float | None:
        """Room temperature from the handset, or None if not connected."""
        return self._ambient_temperature if self.is_handset_connected else None

    @property
    def set_temperature(self) -> float | None:
        """Set temperature held by the fireplace (handset setting)."""
        return self._set_temperature

    @property
    def out_after_shutdown_seconds(self) -> float | None:
        """Seconds the fire has been reported fully out since a recent shutdown.

        None if no shutdown was seen in the last RECENT_SHUTDOWN_SECONDS.
        0 while it is still shutting down or a flame is still reported.
        """
        now = time.monotonic()
        if (
            self._shutdown_seen_at is None
            or now - self._shutdown_seen_at > RECENT_SHUTDOWN_SECONDS
        ):
            return None
        if self._out_since is None:
            return 0.0
        return now - self._out_since

    @property
    def last_shutdown_at(self) -> float | None:
        """time.monotonic() of the last frame showing "shutting down"."""
        return self._shutdown_seen_at

    @property
    def light_level_reported(self) -> int | None:
        """Light level 1-9 as reported (nearest), or None if not known.

        Only while the light is reported on: the level byte keeps the last
        brightness with the light off too. 00 (seen briefly during ignition
        and shutdown) is ignored. Values from the MyFire app's slider fall
        between the integration's nine levels and are rounded to the nearest.
        """
        if not self.light_reported or not self._light_byte:
            return None
        nearest = min(LIGHT_LEVEL_BYTES, key=lambda v: abs(v - self._light_byte))
        return LIGHT_LEVEL_BYTES.index(nearest) + 1

    @property
    def aux_setting(self) -> bool:
        """Aux (rear burner) setting as reported; stays set on pilot."""
        return self._aux_bit

    @property
    def state_settled(self) -> bool:
        """The last usable status came at least STATE_SETTLE_SECONDS after the
        last command finished, so it shows that command's effect."""
        return (
            self._burner_output is not None
            and self._last_status_at - self._last_command_done >= STATE_SETTLE_SECONDS
        )

    @property
    def burner_output(self) -> int | None:
        """Raw burner output 0-255 from the last usable status."""
        return self._burner_output

    def standBy(self) -> None:
        self._send_command(CMD_STANDBY)
        self._local_aux = False
        self.flameHeight = 0

    def aux_on(self) -> None:
        self._send_command(CMD_AUX_ON)
        self._local_aux = True

    def aux_off(self) -> None:
        self._send_command(CMD_AUX_OFF)
        self._local_aux = False

    def ignite_fireplace(self) -> None:
        """Ignite. Both burners light: the fire starts at full height, aux on.

        No aux_on is sent after it: the fireplace sets aux itself, and an
        aux_on sent during ignition was answered "no receiver" every time
        (5 of 5 on 30 Oct). The heating mode turns aux off if not wanted.
        """
        self._send_command(CMD_IGNITE)
        self._local_aux = True
        self._handset_stale = False  # judge the handset afresh
        # The frames that follow lack the receiver link bit for 20-30 s and
        # are not used; count as igniting until the next usable frame says
        # otherwise, so the fire does not look off meanwhile.
        self._igniting = True
        self.flameHeight = IGNITION_FLAME_HEIGHT  # the fire lights at full height

    def refresh_status(self) -> None:
        self._send_command(CMD_STATUS)

    async def close(self) -> None:
        """Nothing to close: each exchange uses its own connection."""

    def guard_flame_off(self) -> None:
        self._send_command(CMD_GUARD_FLAME_OFF)
        self._local_aux = False
        self.flameHeight = 0

    def light_on(self) -> None:
        self._send_command(CMD_LIGHT_ON)
        self._light_command_at = time.monotonic()

    def light_off(self) -> None:
        self._send_command(CMD_LIGHT_OFF)
        self._light_command_at = time.monotonic()

    def set_light_brightness(self, brightness: int) -> None:
        """Set the light level from HA brightness 1-255.

        Spread over nine levels from 77 (dimmest that lights; 66 and below
        leave the light dark) to FB (full).
        """
        normalized = max(0.0, min(1.0, (brightness - 1) / 254))
        device_code = BRIGHTNESS_CODES[round(normalized * (len(BRIGHTNESS_CODES) - 1))]
        self._send_command(
            f"{CMD_BRIGHTNESS_PREFIX}{device_code}{CMD_BRIGHTNESS_SUFFIX}"
        )
        self._light_command_at = time.monotonic()

    def set_eco(self) -> None:
        self._send_command(CMD_SET_ECO)

    def set_manual(self) -> None:
        self._send_command(CMD_SET_MANUAL)

    def set_thermostat(self, temp_celsius: float) -> None:
        snapped = round(temp_celsius * 2) / 2.0
        snapped = max(5.0, min(36.0, snapped))
        half_degrees = int(snapped * 2)
        hex_chars = f"{half_degrees:02X}"
        self._send_command(f"{CMD_THERMOSTAT_PREFIX}{hex_chars}{CMD_THERMOSTAT_SUFFIX}")

    def get_flame_height(self) -> int:
        """Flame step 1-12, or 0 when the main burner is not lit.

        Read from the burner output in the status packet (nearest step, as
        the valve moves gradually). Before any status has reported it, the
        last commanded height is used.
        """
        if self._burner_output is not None:
            return flame_step_from_output(self._burner_output) if self.on else 0
        return self.flameHeight if self.flame_on else 0

    def set_flame_height(self, flame_height: int) -> None:
        """Set flame to step 1-13. Updates local tracker on ACK."""
        idx = max(0, min(11, int(flame_height) - 1))
        step_code = FLAME_HEIGHT_STEPS[idx]
        self._send_command(f"{CMD_FLAME_PREFIX}{step_code}{CMD_FLAME_SUFFIX}")
        # Update local tracker - command confirmed by device ACK
        self.flameHeight = int(flame_height)
        self.refresh_status()

    def _open(self) -> socket.socket:
        """Open a new connection to the box (one per exchange).

        The box serves one client at a time and pushes status frames on an
        open socket; a long-lived connection collects stale frames and can
        stop getting data while the MyFire app is open. A short connection
        per poll or command works alongside the app.
        """
        sock = socket.create_connection((self.ip, TCP_PORT), timeout=SOCKET_TIMEOUT)
        sock.settimeout(SOCKET_TIMEOUT)
        return sock

    def _send_command(self, msg: str) -> None:
        """Send a command and consume its response(s), one exchange at a time."""
        with self._io_lock:
            self._send_command_locked(msg)

    def _send_command_locked(self, msg: str) -> None:
        """Connect, send one command, read its reply, close.

        Any command other than the status poll first waits until
        COMMAND_GAP_SECONDS have passed since the previous command finished,
        then reads replies until the box acknowledges it (or
        ACK_TIMEOUT_SECONDS pass), so the receiver is never sent a command
        while it is still handling the last one. The status poll reads until
        a status frame arrives. Every status frame received is parsed.
        """
        is_command = msg != CMD_STATUS
        if is_command:
            wait = COMMAND_GAP_SECONDS - (time.monotonic() - self._last_command_done)
            if wait > 0:
                time.sleep(wait)

        payload = bytes.fromhex(COMMAND_PREFIX + msg)
        try:
            sock = self._open()
        except OSError as err:
            _LOGGER.debug("Cannot connect to %s: %s", self.ip, err)
            self._command_finished(is_command)
            return
        try:
            sock.sendall(payload)
            self._read_replies(sock, msg, is_command)
        except OSError as err:
            _LOGGER.debug("Exchange with %s failed (%s): %s", self.ip, msg, err)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._command_finished(is_command)

    def _read_replies(self, sock: socket.socket, msg: str, is_command: bool) -> None:
        """Read until the command's acknowledgement or a status frame arrives."""
        if is_command:
            try:
                code = bytes.fromhex(msg).decode("ascii")[:2]
            except (ValueError, UnicodeDecodeError):
                code = ""
            wanted = ACK_PREFIX + code
            timeout = ACK_TIMEOUT_SECONDS
        else:
            wanted = None
            timeout = SOCKET_TIMEOUT
        text = ""
        pending = b""  # bytes after the last complete frame (ETX)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data = sock.recv(RECV_BUFFER)
            except TimeoutError:
                break
            if not data:
                break
            # A frame can be split across reads: parse complete frames only.
            pending += data
            if b"\x03" in pending:
                complete, pending = pending.rsplit(b"\x03", 1)
                self._handle_reply(complete + b"\x03")
            else:
                self._last_reply = time.monotonic()
            text += data.decode("ascii", errors="replace")
            if wanted is not None and wanted in text:
                break
            if wanted is None and (
                any(p in text for p in STATUS_PREFIXES) and text.rstrip().endswith("\x03")
                or RECEIVER_LOST_PREFIX in text
            ):
                break
        if pending:
            self._handle_reply(pending)  # unframed remainder (fallback parse)
        if not text:
            _LOGGER.debug("No response to command %s (timeout)", msg)
        elif is_command:
            if wanted in text:
                _LOGGER.debug("Command %s acknowledged", msg)
            else:
                _LOGGER.debug(
                    "Command %s not acknowledged within %.0fs", msg, ACK_TIMEOUT_SECONDS
                )

    def _handle_reply(self, data: bytes) -> None:
        self._last_reply = time.monotonic()
        _LOGGER.debug("RAW RESPONSE hex=%s", data.hex())
        self._parse_any_status(data)

    def _command_finished(self, is_command: bool) -> None:
        if is_command:
            self._last_command_done = time.monotonic()

    def _parse_any_status(self, data: bytes) -> None:
        """Parse the newest complete status packet in the received data.

        One read can hold several packets (STX ... ETX), including older
        status packets, light notifications and "no receiver" replies. The
        last complete status packet is the most recent state.
        """
        text = data.decode("ascii", errors="replace")
        packets = [chunk.split("\x02")[-1] for chunk in text.split("\x03")]
        for pkt in packets:
            if pkt.startswith(LIGHT_ON_MSG):
                self._light_event = True
            elif pkt.startswith(LIGHT_OFF_MSG):
                self._light_event = False
        statuses = [
            pkt
            for pkt in packets
            if pkt.startswith(STATUS_PREFIXES) and len(pkt) >= MIN_STATUS_LEN
        ]
        if not statuses:
            # Fall back to a status packet that is not cleanly framed.
            for prefix in STATUS_PREFIXES:
                idx = text.find(prefix)
                if idx != -1 and len(text) - idx >= MIN_STATUS_LEN:
                    statuses = [text[idx:]]
                    break
        if statuses:
            if self.receiver_lost:
                _LOGGER.info("Fireplace receiver link restored")
            self._no_receiver_since = None
            self._process_status(statuses[-1])
            return
        if RECEIVER_LOST_PREFIX in text:
            _LOGGER.debug("WiFi box replied 'no receiver' (E1): %s", data.hex())
            if self._no_receiver_since is None:
                self._no_receiver_since = time.monotonic()
            return
        _LOGGER.debug("Response contains no status packet: %s", data.hex())

    def _process_status(self, status_str: str) -> None:
        _LOGGER.debug("STATUS PACKET (len=%d): %s", len(status_str), status_str)
        if len(status_str) < MIN_STATUS_LEN:
            return
        try:
            output = int(status_str[STATUS_BURNER_OUTPUT], 16)
            word = int(status_str[STATUS_WORD], 16)
        except ValueError:
            return

        # Frames without the receiver link bit (seen for a few seconds while
        # igniting and shutting down) carry unreliable values: keep the last
        # good ones. Accepted only until a linked frame has been seen.
        if not word & WORD_RECEIVER_LINK and self._link_seen:
            _LOGGER.debug("Receiver link bit clear: status values not used")
            if self._unlinked_since is None:
                self._unlinked_since = time.monotonic()
            return
        if word & WORD_RECEIVER_LINK:
            self._link_seen = True
            if self._unlinked_since is not None and self.receiver_lost:
                _LOGGER.info("Fireplace receiver link restored")
            self._unlinked_since = None

        self.status_count += 1
        self._last_status_at = time.monotonic()
        self._aux_bit = bool(word & WORD_AUX)
        try:
            self._light_byte = int(status_str[STATUS_LIGHT], 16)
        except ValueError:
            pass
        now = time.monotonic()
        if word & WORD_SHUTTING_DOWN:
            self._shutdown_seen_at = now
            self._out_since = None
        elif word & (WORD_PILOT | WORD_IGNITING):
            self._out_since = None
        elif self._out_since is None:
            self._out_since = now
        self._burner_output = output
        self._shutting_down = bool(word & WORD_SHUTTING_DOWN)
        self._igniting = bool(word & WORD_IGNITING)
        self._guard_flame_on = bool(word & WORD_PILOT)
        self._aux_reported = bool(word & WORD_PILOT) and bool(word & WORD_AUX)
        self._light_bit = bool(word & WORD_LIGHT)

        self._prev_flame_on = self.flame_on
        self.flame_on = bool(word & WORD_PILOT)
        self.on = self.flame_on and output > BURNER_LIT_ABOVE
        if self._prev_flame_on and not self.flame_on:
            # Fire out: forget the commanded state.
            self._local_aux = False
            self.flameHeight = 0

        room = status_str[STATUS_ROOM_TEMP]
        self._handset_connected = room != ROOM_TEMP_NO_HANDSET
        # A handset that has stopped sending (batteries out) keeps its last
        # reading in the status for about 90 minutes before the box reports
        # it missing (30 Oct: 00D7 from 16:20 to 17:50, then 0666). With the
        # burner lit the room temperature always moves, so a reading that
        # has not changed for HANDSET_STALE_SECONDS of burning is stale.
        if room != self._room_raw:
            self._room_raw = room
            self._room_changed_at = now
            self._handset_stale = False
        if self.on:
            if self._burner_lit_since is None:
                self._burner_lit_since = now
        else:
            self._burner_lit_since = None
        if (
            self._handset_connected
            and not self._handset_stale
            and self._burner_lit_since is not None
            and now - max(self._room_changed_at, self._burner_lit_since)
            >= HANDSET_STALE_SECONDS
        ):
            _LOGGER.info(
                "Handset temperature unchanged for %d minutes of burning: "
                "treating the handset as not connected",
                HANDSET_STALE_SECONDS // 60,
            )
            self._handset_stale = True
        try:
            if self._handset_connected:
                self._ambient_temperature = int(room, 16) / 10
            self._set_temperature = int(status_str[STATUS_SET_TEMP], 16) / 10
        except ValueError:
            pass

        _LOGGER.debug(
            "Status: output=%d word=%04X mode=%s flags=%s room=%s set=%s",
            output,
            word,
            status_str[STATUS_MODE],
            status_str[STATUS_FLAGS],
            room,
            status_str[STATUS_SET_TEMP],
        )
