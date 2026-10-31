# Mertik Maxitrol myfire WiFi Box — Wire Protocol

Local protocol of the Mertik Maxitrol WiFi modules (B6R-WME, B6R-W2BE-0,
B6R-WWN) used by the MyFire app and its white-label versions (Gazco MyFire,
Barbas Gas, Trimline …).

This document combines two independent sets of findings, and uses the same
field names, offsets and bit masks as
[diederikm/mertik-fireplace-wifi-ha `PROTOCOL.md`](https://github.com/diederikm/mertik-fireplace-wifi-ha/blob/main/PROTOCOL.md),
so the two can be read side by side. Where a statement comes from one source
only, it is tagged:

| Tag | Source |
|---|---|
| **G** | Measured on a Gazco fire with a B6R-WME / B6R-W2BE-0 box and a **B6R-H8TV4PB** receiver: debug logs of this integration and PCAPdroid captures of the MyFire app (2026-10) |
| **D** | Measured by diederikm on a Bellfires fire with a B6R-W2BE-0 box (2026-10) |
| **A** | Field and bit names from the MyFire / Barbas Gas app, as described by diederikm (static analysis; no app code reproduced) |
| **C** | Earlier community work: [erdebee](https://github.com/erdebee/homey-mertik-wifi), [rbrondgeest](https://github.com/rbrondgeest/homey-mertik-wifi), [tritter](https://github.com/tritter/homebridge-mertik-fireplace), [jkfran](https://github.com/jkfran/mertik-fireplace-wifi-ha) |

Untagged statements are confirmed by both **G** and **D**, or follow directly
from the format.

---

## Transport

- Plain **TCP, port 2000**, no authentication.
- The box effectively serves **one client at a time**. While another client
  (for example the MyFire app) holds a socket, a second client's socket gets
  stale data or none. **D**
- The box **pushes status frames unprompted** on an open socket, and one
  `recv()` can return several frames or part of one.
- **Recommended:** open a short connection for each poll or command, read
  until the expected reply arrives, take the newest status frame and close.
  This works alongside the MyFire app (verified with the app open while Home
  Assistant polled every 10 s). **D** This integration does this from 2.5.3;
  earlier versions kept one connection open.

---

## Framing

```
STX(0x02)  "3030303080"  <command id: 2 chars>  <payload: ASCII>  ETX(0x03)
```

Everything between STX and ETX is ASCII characters. As raw bytes, every
request starts with the 11-byte prefix

```
02 33 30 33 30 33 30 33 30 38 30
```

followed by the command, then `03`. Aux on/off are additionally followed by
`0A` (LF). **C G**

Replies start, after STX, with `303030300003` (status), `303030300001`
(device info) or `3030303000` + the command id (acknowledgement, see
[Other replies](#other-replies)). Some boxes send status frames starting
`030300000003`. **D** Receivers must split on STX/ETX, drop bytes before an
STX, and handle several frames, or part of one, per read.

---

## Commands

The command id is the two ASCII characters after the prefix. A bare id reads
a value; the same id with a payload writes it. **A**

| Command | Id | Bytes after the prefix | Notes |
|---|---|---|---|
| Read status | `03` | `30 33 03` | Replies with a status frame (see below) |
| Read device info | `01` | `30 31 03` | Model, versions, serials (e.g. `B6R-H8TV4P` in ASCII). Not a handshake **A**; earlier integrations called it "APP mode" and send it once at startup **C G** |
| Ignite | `1A` | `31 41 03` | |
| Off (pilot out) | `10` | `31 30 03` | Full off including the pilot |
| Burner output / standby | `16` | `31 36 <VV> 03` | `VV` = output as 2 ASCII hex chars. `00` = standby (turns a burning fire down to pilot only; does **not** light a cold fire **G**). Steps: see [Flame height](#flame-height) |
| Aux (rear burner) on / off | `20` | `32 30 30 31 03 0A` / `32 30 30 30 03 0A` | |
| Light on / off | `30` | `33 30 30 31 03` / `33 30 30 30 03` | |
| Light brightness | `30` | `33 30 46 45 <BB> 03` | `BB` = brightness as 2 ASCII hex chars, see [Light](#light) |
| Manual mode | `B0` | `42 30 03` | Exit eco or thermostat mode |
| Eco mode on | `B3` | `42 33 30 31 03` | Full payload layout unverified **A** |
| Thermostat mode on | `B2` | `42 32 …` | Payload carries state and set temperature x10 **A** |
| Countdown timer | `B1` | | **A**. Earlier versions of this integration had an unused "thermostat" command `B1` + temperature in 0.5 °C steps, which per the app is this timer instead |
| Program, fan, LED, clock, name, fault history, reboot, Wi-Fi … | `A0`, `40`, `50`, `06`, `05`, `C0`–`C2`, `0C`, `D0`–`D5` … | | Listed by diederikm **A**; not used here |

Three further requests seen in captures of the app (`50ff00`, `50ff01`, `C2`)
got no reply within 3 s on a long-lived connection, and their late replies
confused the pairing of later replies; this integration does not send them.
**G**

### Command pacing

Each command is acknowledged with `3030303000` followed by the command id
(`1A` ignite, `10` off, `16` burner output, `20` aux, `30` light). The
acknowledgement can arrive after a status frame. Sending the next command
before the acknowledgement (within about a second) was answered with "no
receiver" (`3030303000E101`) in 12 of 13 captured cases, and once was
followed by a 90-minute loss of the receiver (F43). This integration waits
for the acknowledgement (up to 3 s) and then 2 s before the next command. **G**

**Ignition is an exception: send nothing for about a minute.** After ignite
(`1A`), an aux-on sent 2 s after the acknowledgement was answered with "no
receiver" every time (5 of 5 on 30 Oct), while the receiver was busy
igniting (link bit clear in the next frames). It is not needed: both burners
light and the aux bit is set by the fireplace itself. **G** The integration
sends nothing during ignition; it waits until the burner is lit and then
35 s more before setting the heating mode, as earlier commands are ignored.
The same applies when the fire is lit with the handset or the app.

**Flame up, then aux on, gets "no receiver".** An aux-on sent 2 s after a
`16FF` (valve to full) was answered "no receiver" 3 times out of 3 on 31 Oct
while the valve moved; aux-on first, then `16FF`, avoids it. **G**

**Ignition right after a shutdown is ignored.** On 31 Oct 12:12:27 an ignite
sent in the first frame showing the fire fully out (`804F`, 25 s after the
off command) was acknowledged but nothing happened; 35 s later it lit. The
integration waits until the fire has been reported out for 15 s. **G**

**Beeps.** On G's fire, down to pilot (`1600`) never beeped on 31 Oct and
aux-on always did; flame-height and aux-off commands beeped at some times and
not others. **G**

---

## Status frame (command `03`)

Character offsets after stripping STX. Field names from the app **A**;
offsets confirmed against captured frames by both **G** and **D**.

Example (**G**, fire lit at Low Heat, aux off, light on, handset at 22.5 °C,
set temperature 20.0 °C):

```
303030300003 C6 80 80C5 AA 04 00 00 00E1 00C8 4C6976696E6720526F6F6D20 FFFF…
prefix       |  |  |    |  |  |  |  |    |    name "Living Room "
             |  |  |    |  |  |  |  |    set temperature
             |  |  |    |  |  |  |  room temperature
             |  |  |    |  |  |  system flags
             |  |  |    |  |  system mode
             |  |  |    |  fan level
             |  |  |    light level
             |  |  receiver state word
             |  burner output
             setup state
```

| Chars | Field | Notes |
|---|---|---|
| 0–11 | Prefix | `303030300003` (**G**) or `030300000003` (**D**) |
| 12–13 | Setup state | Feature bits (fan, light, aux, LED, cloud, setup complete, timer) **A**; bit values unverified. Constant per fire (`C6` **G**) |
| 14–15 | **Burner output** | Gas valve position `00`–`FF`. See [Burner output](#burner-output) |
| 16–19 | **Receiver state** | 16-bit word, see [Receiver state bits](#receiver-state-bits) |
| 20–21 | Light level | Brightness last set; reported with the light off too. See [Light](#light) |
| 22–23 | Fan level | `04` in every frame **G** (no fan fitted) |
| 24–25 | System mode | Char 24: `0` manual, `1` thermostat, `2` eco **A D**; `3` briefly during a mode change **D**. The handset's flame-wave programme reports `0` **D** |
| 26–27 | System flags | `01` countdown, `02` thermostat, `04` user profile, `08` child lock, `10` programmer, `20` programs invalid **A**. `00` in every frame **G** |
| 28–31 | **Room temperature** | From the handset, x10, 16-bit: `00E1` = 22.5 °C. **`0666` when the handset is not connected** (F44) **G**. A handset that stops sending (batteries out) keeps its last reading here for about 90 minutes before `0666` (30 Oct: `00D7` from 16:20 to 17:50) **G** |
| 32–35 | Set temperature | x10, 16-bit: `00C8` = 20.0 °C |
| 36– | Name | ASCII, padded with `FF`. Icon, Wi-Fi RSSI, handset mode, pilot and burner thermocouple mV and handset RSSI follow **A**; offsets unverified |

> **Correction (2.5.3).** Versions up to 2.5.2 of this integration read the
> room temperature from chars 34–35, which is the low byte of the set
> temperature (so the handset temperature always showed 20.0 °C), and took
> chars 28–29 (`06` of `0666`) as a "handset fault" byte.

### Receiver state bits

Chars 16–19 as a 16-bit word. Parsers must pad it to 16 bits: padding to 8
shifts every bit index by four whenever the top bit is clear. **D**

| Mask | Index (MSB = 0) | App name **A** | Observed meaning |
|---|---|---|---|
| `0x8000` | 0 | receiverConnected | Set in normal operation. **Clear for a few seconds while igniting and while shutting down; the other values in such a frame are unreliable.** Seen as `001B`, `001D` at ignition and `0183`, `01C7` at shutdown **G**, `021A`, `028A`, `008A` **D** |
| `0x0200` | 6 | handsetConnected | Set on **D**'s fire; **never set on G's**, even with the handset working — use the room temperature `0666` instead |
| `0x0100` | 7 | ignitionNotAllowed | Set while **shutting down** (roughly 15–30 s after switching off). The MyFire app then refuses ignition ("Ignition is not allowed"); commanding ignition meanwhile was seen to make the box lose the receiver (F43) **G** |
| `0x0080` | 8 | mainBurnerOn | Despite the name, set whenever the **pilot** burns: burner lit **and** standby. Clear only when fully off |
| `0x0040` | 9 | fanOn | Behaves like a fan relay with timers on **G**'s fire (no fan fitted): set 4 minutes after the burner output goes above zero, cleared about 10 minutes after standby or off. It follows the commanded output, not the flame |
| `0x0020` | 10 | mainBurner2ndTCOn | |
| `0x0010` | 11 | igniting | Set while igniting |
| `0x0008` | 12 | auxOn | Aux (rear burner) **setting**. Every aux-off clears it (`8D`→`85`, `8F`→`87`, `CF`→`C7`) **G**. **Stays set in standby** while only the pilot burns |
| `0x0004` | 13 | lightOn | Fire light on, with the fire lit or out. Set by light-on (`8B`→`8F`, `09`→`0D`), cleared by light-off (`87`→`83`) and when the fire goes out, and follows the handset's light button (`8D`→`89`→`8D`) **G** |
| `0x0002` | 14 | faulted | On **G**'s fire: set while a handset fault (F44) is stored, and cleared when the error is cleared in the MyFire app (`8087` → `8085`, 30 Oct 22:21), even with the handset still missing |
| `0x0001` | 15 | vModuleConnected | |

### Deriving the fireplace state

What this integration does (from 2.5.3), consistent with both sets of
measurements:

1. Receiver link bit clear, after a frame with it set has been seen: ignore
   the frame and keep the last values. (If this lasts 30 s, report F43.)
   After an ignite command, count as igniting until the next frame with the
   link bit: the frames during ignition (`001B`, `001D`, `005B` …) lack it
   for 20–30 s **G**, so without this the fire would look off meanwhile.
2. `0x0100` set: **shutting down**.
3. `0x0010` set: **igniting**.
4. `0x0080` clear: **off** (pilot out).
5. Burner output above `0x40` (64): **burner lit**. Otherwise **standby**
   (pilot only).
6. Aux burner lit: `0x0008` set **and** burner lit.

### Burner output

| Situation | Output | Source |
|---|---|---|
| Fully off, shutting down, igniting | `00` (**G** has also seen `82` left over with the fire out: check `0x0080` first) | D G |
| Standby (pilot only) | `00`, sometimes `20` (32) | D G |
| Handset flame-wave programme, low phase (real flames) | `69` (105) | D |
| Lowest step (Low Heat) | `80` (128); `7E`–`84` while the valve settles | D G |
| Highest step (Medium / Full Heat, and after any ignition) | `FF` (255) | D G |

The output is continuous; the threshold of 64 sits between standby (≤ 32)
and the lowest real flame (105). **D**

**The output is the valve setting, not proof of flame.** On G's fire (30 Oct,
handset batteries out, F16 in the app), Low, Medium and then Full were
selected from thermostat standby after about 4½ minutes on pilot. The output
went to `82` then `FF` and the aux bit set, but only the pilot burned for the
next 5 minutes. The same sequence after 1 minute on pilot, later that day,
lit the burner. Nothing in the status frame showed the difference. **G**

### Flame height

The handset and this integration use 12 steps, sent with command `16` as two
ASCII hex characters, and reported back as the burner output:

| Step | Output | Sent as |
|---|---|---|
| 1 (min) | `80` (128) | `38 30` |
| 2 | `8B` (139) | `38 42` |
| 3 | `97` (151) | `39 37` |
| 4 | `A2` (162) | `41 32` |
| 5 | `AE` (174) | `41 45` |
| 6 | `B9` (185) | `42 39` |
| 7 | `C5` (197) | `43 35` |
| 8 | `D0` (208) | `44 30` |
| 9 | `DC` (220) | `44 43` |
| 10 | `E7` (231) | `45 37` |
| 11 | `F3` (243) | `46 33` |
| 12 (max) | `FF` (255) | `46 46` |

To show the flame height, take the step nearest to the reported output, or 0
when the burner is not lit. This integration works with the steps internally
(Low Heat = step 1, Medium and Full Heat = step 12) but its Flame Height sensor
shows them in words: Off (no flame), Pilot only (step 0), Low (steps 1–6) and
High (steps 7–12). (Versions up to 2.5.2 of this integration did not
read the output and said the flame height was not reported.) The app sets
the output as `round(pct / 100 × 127 + 128)`. **A**

### Light

- **On/off:** bit `0x0004` of the receiver state word, in every status frame.
- **Brightness:** the light level (chars 20–21) is the brightness last set,
  as one hex byte. It is reported **whether the light is on or off**, so
  check `0x0004` first; the level only describes the light while that bit is
  set. It reads `00` for a moment during ignition and shutdown; ignore that.

Brightness is sent with `30FE` + two ASCII hex characters. Captured (**G**,
light on, fire lit):

| Set from Home Assistant | Command sent | Light level afterwards |
|---|---|---|
| 10 % | `30FE77` | `77` (119) |
| 50 % | `30FEAA` | `AA` (170) |
| 90 % | `30FEDD` | `DD` (221) |

The app maps its slider 0–100 % to `pct / 100 × 153 + 102`, i.e. `66`–`FF`.
**A** Brightness seen on G's fire (light levels set from Home Assistant):

| Level | Light |
|---|---|
| `63`, `66` | Dark (the light stays off) |
| `77` | Very dim |
| `88`, `99`, `AA`, `BB` | Brighter in clear steps |
| `CC` and above | Little visible change up to full (`FB`) |

From 2.5.3 this integration spreads HA brightness 1–100 % over nine levels,
`77` `88` `99` `AA` `BB` `CC` `DD` `EE` `FB`, so the lowest setting lights.
(Up to 2.5.2 it sent `63` or `66` below 7 %, leaving the light dark.) Each
level is sent as two ASCII characters, e.g. `AA` as `41 41`.

The MyFire app also dims the light: its slider sets the level byte to
values between the integration's nine levels. The handset only switches it on
and off. `A9` was
seen after switching it off and on with the handset; what sets that value is
not known. **G**

### Other replies

| Reply (between STX and ETX) | Meaning |
|---|---|
| `3030303000E101` | "No receiver": the box has lost its radio link to the fireplace receiver (the app shows F43). An occasional stray one is normal; a continuous stream means the link is down. Power-cycling the fireplace restores it **G** |
| `30303030003001xx` / `30303030003000xx` | Light on / off (`xx` = light level). Sent as the acknowledgement of a light command. **Not** sent when the light is switched with the handset, so use `0x0004` for the light state **G** |
| `3030303000` + id (`1A`, `10`, `16`, `20`, `30` …) | Acknowledgement of the command just received |

If the box stops answering altogether (every request times out), it has also
lost the fireplace; operating the fire with the handset usually restores it.
**G**

---

## Measured sequences

Polled every 10 s; `out` is the burner output, `word` the receiver state.

**Switching off (G, 29 Oct 17:07–17:14, Low Heat, light on):**

| out | word | state |
|---|---|---|
| `80` | `80C7` | burner lit, step 1, aux off, light on |
| `00` | `81C7` | shutting down |
| `00` | `01C7` | link bit clear: ignore |
| `00` | `804B` | off (pilot out), light off with the fire |

**Ignition from Home Assistant (G, 29 Oct):**

| out | word | state |
|---|---|---|
| `00` | `800D` | off, light on |
| `00` | `801D` | igniting |
| `00` | `001D` | link bit clear: ignore |
| `00` | `808D` | pilot lit |
| `FF` | `808D` | burner lit, step 12, aux setting on |
| `00` | `808D` | standby (thermostat): pilot only; aux bit still set |

**Handset light button with the fire on pilot (G, 29 Oct):** `808D` → `8089`
→ `808D` → `8089` → `808D` (off, on, off, on). No light message is sent.

**Shutting down from eco, handset off button (D):** `out 141 word 828A` (on,
mode 2) → `141 838A` (shutting down, mode 0) → `0 838A` → `0 820A` (off).

**Relighting from fully off (D):** the pilot only held on the third attempt:
`0 820A` → `0 821A` (igniting) → `0 021A` (link lost) → `255 828A` (on) →
`255 028A` → `255 008A` → `0 820A` (pilot did not hold, ignition retried).

**Handset flame wave (D):** reported as manual (char 24 `0`); the output
ramps down by about 10 every 10–30 s to 105, holds about 10 minutes, then
ramps back up by about 10 per minute.

---

## Sniffing your own device

```bash
# Linux/Raspberry Pi
sudo tcpdump -i any -w mertik.pcap 'tcp port 2000'

# Android (no root required)
# Use PCAPdroid app, target app: com.iqontrol.mertik
```

Open the capture in Wireshark and filter with `tcp.port == 2000`. Or enable
debug logging for `custom_components.mertik` in Home Assistant: every reply
is logged as `RAW RESPONSE` and every status frame as `STATUS PACKET`.
