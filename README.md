# Mertik Maxitrol Fireplace — Home Assistant Integration

A Home Assistant custom integration for gas fireplaces controlled by the
**Mertik Maxitrol myfire WiFi box** (B6R-WME / B6R-W2BE-0 and similar).

This implementation adds thermostatic control which can use the Maxitrol
handset as the temperature sensor, or any other HA temperature sensor.
Of course the sensor needs to be in the room with the fire!

This is a substantially enhanced fork of
[jkfran/mertik-fireplace-wifi-ha](https://github.com/jkfran/mertik-fireplace-wifi-ha).
The wire protocol was reverse-engineered through PCAPdroid packet captures of
the official myfire app, cross-referenced with community work by
[erdebee](https://github.com/erdebee/homey-mertik-wifi),
[rbrondgeest](https://github.com/rbrondgeest/homey-mertik-wifi), and
[tritter](https://github.com/tritter/homebridge-mertik-fireplace).

See [PROTOCOL.md](PROTOCOL.md) for full wire-level documentation.

---

## What's new vs the original

| Feature | Original | This fork |
|---------|----------|-----------|
| On / Off | ✅ | ✅ |
| Flame height (12 steps) | ✅ (broken) | ✅ fixed |
| Light on/off + dimming | ✅ (broken) | ✅ fixed |
| Aux / rear burner | ✅ (broken) | ✅ fixed |
| Temperature sensor | ✅ (broken) | ✅ fixed |
| APP mode handshake | ❌ | ✅ |
| Light follows the fire (on with the fire, off when it goes out) | ❌ | ✅ |
| Heating mode selector | ❌ | ✅ |
| Thermostatic control | ❌ | ✅ |
| Selectable external temperature sensor | ❌ | ✅ |
| Adjustable thermostat thresholds and hysteresis | ❌ | ✅ |
| Complete control panel on the standard Thermostat card | ❌ | ✅ |
| Fault alerts (F43, F44, WiFi box not responding) | ❌ | ✅ |

---

## Compatible hardware

| WiFi Box Part No. | Status |
|---|---|
| B6R-WME | Confirmed working |
| B6R-W2BE-0 | Confirmed working |
| B6R-WWN | Likely working, unconfirmed |

Tested against a **B6R-H8TV4PB** receiver. The same WiFi box is sold under
many brand names: RAISfire, Gazco MyFire, Trimline Fires, Thermocet,
ITALKERO, Signi, SAFIRE, attika, Ortal, and Fire Connects.

---

## Entities

| Entity | Type | Notes |
|--------|------|-------|
| Fireplace | Switch | Main on/off |
| Heating Mode | Select | Standby / Full Heat / Medium Heat / Low Heat / Thermostatic |
| Thermostat | Climate | The whole control panel: Heat/Off, heating mode presets (Low, Medium, Full, Thermostatic), target and measured room temperature, and heating/idle status |
| Light | Light | Dimmable. On/off and brightness as reported by the fireplace, so it follows the handset and the MyFire app too. Comes on when the fire is switched on (optional) and goes off when the fire goes out; can also be switched on its own |
| Aux Burner | Binary sensor | Read-only: whether the rear / second burner is lit, as reported by the fireplace. Off while the thermostat holds the fire on pilot |
| Flame Height | Sensor | Read-only: **Off**, **Pilot only**, **Low** or **High**, from the flame the fireplace reports (Low Heat shows Low; Medium and Full Heat show High; the fire lights at High). Steps 1–6 set with the handset show as Low, 7–12 as High |
| Handset Temperature | Sensor | Room temperature from the paired handset; unknown while the handset is not connected (F44). The fireplace keeps showing a missing handset's last reading for about 90 minutes, so the integration also treats it as not connected when the reading has not changed for 30 minutes with the burner lit. Hidden automatically when another temperature sensor is selected. |
| Fault Code | Sensor | Active Mertik fault, e.g. F44 (handset out of range). Shows "No fault" when all clear. |

Aux and Flame Height are indicators, not controls: the heating mode decides
them. Versions before 2.5.0 had them as a switch and a slider; those are
removed automatically on upgrade.

Each fireplace you add has its own name, temperature sensor, thresholds and
default target temperature, so two fires in one house can use different room
sensors.

The Heating Mode select and the Thermostat's presets are the same setting:
change either and the other follows.

---

## Dashboard card

The Thermostat entity is a complete control panel for Home Assistant's
standard **Thermostat** card. Add the card, pick your fireplace's Thermostat
entity, and add the **HVAC modes** and **Preset modes** features, or paste
this YAML (**Edit dashboard → Add card → Manual**), replacing `fireplace` with
your entity names:

> Your entity IDs start with your fireplace's name, e.g.
> `light.living_room_fireplace_light` rather than `light.fireplace_light`.
> Find them under **Settings → Devices & services → Mertik Maxitrol → your
> fireplace**. A card naming an entity that does not exist does nothing (the
> log says "Referenced entities … are missing").

```yaml
type: vertical-stack
cards:
  - type: thermostat
    entity: climate.fireplace_thermostat
    features:
      - type: climate-hvac-modes
        hvac_modes:
          - heat
          - "off"
      - type: climate-preset-modes
        style: icons
        preset_modes:
          - low_heat
          - medium_heat
          - full_heat
          - thermostatic
  - type: grid
    columns: 3
    square: false
    cards:
      - type: tile
        entity: binary_sensor.fireplace_aux_burner
        tap_action:
          action: none
      - type: tile
        entity: sensor.fireplace_flame_height
        color: deep-orange
        tap_action:
          action: none
      - type: tile
        entity: sensor.fireplace_fault_code
        color: red
        tap_action:
          action: none
  - type: tile
    entity: light.fireplace_light
    icon_tap_action:
      action: toggle
    features:
      - type: light-brightness
```

Home Assistant only colours a sensor's tile when the card sets a `color`, as
above: Flame Height then shows in colour for Pilot only, Low and High and grey
for Off, and Fault Code shows red for a fault and grey for No fault.

The light shows nine distinct brightness levels across the slider (see
[PROTOCOL.md](PROTOCOL.md#light)); the lowest setting is very dim.

The target temperature is used in Thermostatic mode; in the other modes it
is kept for the next time Thermostatic is selected.

---

## Heating modes

Select the mode using the **Heating Mode** entity:

| Mode | Behaviour |
|------|-----------|
| **Standby** | Pilot flame only; main burners off but instant re-ignition |
| **Full Heat** | Both burners, maximum flame |
| **Medium Heat** | Front burner only, maximum flame |
| **Low Heat** | Front burner only, minimum flame |
| **Thermostatic** | HA automatically controls the heating mode based on room temperature vs setpoint |

To turn the fire off, use the **Fireplace** switch. This extinguishes both
the main burners and the pilot flame.

### Thermostatic control

Set the target temperature using the **Thermostat** entity's temperature
slider. Each time the fire is switched on (from HA, the handset or the MyFire
app) the target is reset to the fireplace's **Default target temperature**
(20 °C unless changed), unless you changed it while the fire was off: then
your setting is kept. Changes made while the fire is on last until it is
switched off. HA reads the current room temperature every 10 seconds and applies
the appropriate heating mode:

With **gap = setpoint − room temperature**, Low threshold *L* (default 1 °C),
Full threshold *F* (default 2 °C) and hysteresis *h* (default 0.1 °C):

| Gap | Mode applied |
|-----|--------------|
| gap ≤ −h | Standby (pilot flame only) |
| h ≤ gap ≤ L − h | Low Heat |
| L + h ≤ gap ≤ F − h | Medium Heat |
| gap ≥ F + h | Full Heat |

Between these bands (within *h* either side of 0, *L* and *F*) the current
mode is kept, so a sensor reading that wobbles around a threshold does not
make the fire switch back and forth. Example with a 19.5 °C setpoint and the
defaults: the burner drops to pilot at 19.6 °C and relights at 19.4 °C.

When the room reaches the setpoint the main burners are extinguished but
the **pilot flame remains lit**, allowing fast re-ignition when the room
cools: the burner relights from the pilot straight away. To extinguish the
pilot as well, turn off the Fireplace switch.

**Switching on while the fire is shutting down** — after the fire is switched
off, the receiver runs a shutdown sequence (roughly 15–30 seconds), during
which the MyFire app reports "Ignition is not allowed". Home Assistant does the
same: when you switch on, it first asks the fireplace for a fresh status, and
refuses with the message "Ignition is not allowed while the fireplace is
shutting down" if the fireplace reports shutting down, if a flame is still
burning on a fire that has been switched off, or if it was switched off from
Home Assistant moments ago and the fireplace has not reported since. Nothing
is sent; switch on again once the fire is fully out. (Commanding the receiver
mid-shutdown was seen to make the WiFi box lose it, F43.)

The thresholds and hysteresis are adjustable via
**Settings → Devices & Services → Mertik → Configure**.

**Switching on in Thermostatic** — the fire is always ignited (both burners
light). After the burner is confirmed lit and a 35-second settle period, the
thermostat applies the right mode: Low / Medium / Full if the room needs heat,
otherwise it turns the fire down to pilot only. The delay is required because
the device ignores flame height and aux commands in the seconds immediately
after ignition. (The device's standby command only turns a burning fire down
to pilot; it does not light a cold fire.)

### Temperature sensor

By default the thermostat uses the ambient temperature from the fireplace's
own handset sensor. You can select any other HA temperature sensor via
**Settings → Devices & Services → Mertik → Configure**.

> **Note:** The handset sensor only transmits temperature when it has entered
> APP mode (indicated by "APP" on the handset display). If no temperature
> appears, configure an external sensor — any HA `sensor` with
> `device_class: temperature` appears in the dropdown.

**Handset fault (F44) behaviour** — when the handset is out of range or has a
low battery, the device reports fault F44. In this state, the device cannot
reliably measure room temperature. If no external sensor is configured, the
integration automatically switches thermostatic mode to **Standby** (pilot
flame only) rather than running on a meaningless temperature reading. Normal
thermostatic control resumes automatically once the handset reconnects and the
fault clears. If an external HA temperature sensor is configured, it is used
instead and thermostatic control continues unaffected by the handset fault.

---

## Data updates

The integration communicates with the fireplace over a **direct local TCP connection** (port 2000) with no cloud dependency. Everything it shows comes from polling the fireplace's status. It opens a short connection for each poll or command and closes it again, because the WiFi box serves one client at a time: this lets the integration and the MyFire app work side by side.

HA **polls the device every 10 seconds** by sending a status request and parsing the response. Each poll refreshes:

- On/off state and flame status: pilot only or burner lit, and the flame height
- Ignition and shutdown flags
- Aux (rear) burner
- Light on/off (however it was switched: Home Assistant, the handset or the MyFire app)
- Room temperature from the handset
- Fault state (F43 / F44)

The 10-second interval is chosen for two reasons:
1. **Thermostatic control** — the control loop runs on every poll and must react to temperature changes promptly to avoid overshooting the target.
2. **Optimistic state window** — after a command (e.g. turn on), the integration shows the new state optimistically for up to 20 seconds while the device executes the request. A slower poll interval would leave the UI unresponsive for an unacceptable portion of the ignition cycle.

**Optimistic state model** — because the device takes several seconds to physically execute commands (ignition, shutdown), the integration maintains local optimistic state between polls. For example, after pressing "On", the Fireplace switch shows as on immediately; if the device has not confirmed the change within 20 seconds, the state reverts to what the device reports. This avoids a flickering UI during normal use.

**Only what changes is sent** — every command makes the fire beep, so a
heating-mode change sends only the commands that change something, judged from
the fireplace's latest status (for example Medium → Low sends just the flame
height, not "aux off" as well). About 25 seconds later the integration checks
that the fireplace reports the new mode and, if not, sends it once more; if
that fails too, it logs a warning.

**Commands are sent one at a time** — after each command (anything other than
the status poll) the integration waits for the fireplace's acknowledgement,
then leaves 2 seconds before the next. Sending a second command while the
receiver was still handling the first was seen to be refused ("no receiver")
almost every time, and once to be followed by a long loss of the receiver
(F43). A heating-mode change therefore takes a few seconds to complete.

There is no user-configurable polling interval. The interval is fixed because the thermostatic control algorithm depends on a consistent polling rate.

---

## Installation

Requires **Home Assistant 2025.3 or later**.

### HACS (recommended)
1. Open HACS and search for "Mertik Maxitrol Fireplace"
2. Download it, then restart Home Assistant

### Manual
1. Copy `custom_components/mertik/` into `config/custom_components/`
2. Restart Home Assistant

### Configuration
**Settings → Devices & Services → Add Integration → Mertik Maxitrol**

Enter the IP address of your myfire WiFi box. Assign a static DHCP lease
so the IP does not change.

#### Initial setup parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| **Name** | Yes | Display name for the fireplace in Home Assistant, e.g. `Lounge fire`. Default: `Mertik Maxitrol`. |
| **Host** | Yes | IP address of the myfire WiFi box (e.g. `192.168.1.100`) |
| **Temperature sensor** | No | Room temperature sensor for thermostatic control. Default: the Mertik handset. |
| **Low Heat threshold** | No | Degrees °C below the thermostat setpoint at which Low Heat is applied. Default: `1.0`. Must be greater than 0 and less than the Full Heat threshold. |
| **Full Heat threshold** | No | Degrees °C below the thermostat setpoint at which Full Heat is applied. Default: `2.0`. Must be greater than the Low Heat threshold. |
| **Default target temperature** | No | Thermostat target applied each time the fire is switched on (unless a target was set while the fire was off). Default: `20.0`. 5–36 °C. |

#### Options (Settings → Devices & Services → Mertik Maxitrol → Configure)

| Parameter | Description |
|-----------|-------------|
| **Temperature sensor** | HA temperature sensor entity to use for thermostatic control. Leave empty to use the Mertik handset's built-in sensor. Any `sensor` entity with `device_class: temperature` appears in the dropdown. |
| **Low Heat threshold** | See above. Can be adjusted without restarting HA. |
| **Full Heat threshold** | See above. Can be adjusted without restarting HA. |
| **Thermostat setpoint step** | Increment for the thermostat temperature slider (degrees °C). Default: `0.1`. Must be greater than 0. |
| **Default target temperature** | See above. |
| **Hysteresis** | Degrees °C either side of each threshold within which the current mode is kept (see *Thermostatic control*). Default: `0.1`. Twice the value must fit within the Low band and the Medium band. |
| **Switch the light on when the fire is switched on** | On by default, matching the handset. When you switch the fire on from Home Assistant, the fire light comes on at its last brightness as soon as a flame is seen. It is only done once per switch-on, so a light you turn off stays off while the fire cycles. The light can always be switched on and off on its own, with the fire on or off. |

### Removing the integration

1. **Settings → Devices & Services → Mertik Maxitrol → ⋮ → Delete**
   This removes the integration and all its entities from Home Assistant.
2. *(HACS installs only)* Open HACS → Integrations → Mertik Maxitrol Fireplace →
   **Remove** to uninstall the component files, then restart Home Assistant.
3. *(Manual installs only)* Delete the `custom_components/mertik/` folder from
   your config directory and restart Home Assistant.

The myfire WiFi box requires no additional steps — it continues to operate
normally via the physical handset after the integration is removed.

### Blueprints

Import any of these automation blueprints directly into Home Assistant.

| Blueprint | Description | Import |
|-----------|-------------|--------|
| Safety shutoff on restart | Turns the fire off a few seconds after HA starts, preventing unattended operation after a restart | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FDaveJesse%2Fmertik-fireplace-wifi-with-thermostat%2Fmain%2Fblueprints%2Fsafety_shutoff_on_restart.yaml) |
| Turn off when nobody home | Turns the fire off when all tracked people leave | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FDaveJesse%2Fmertik-fireplace-wifi-with-thermostat%2Fmain%2Fblueprints%2Fturn_off_when_nobody_home.yaml) |
| Schedule-based on/off | Turns the fire on in Thermostatic mode and off at set times each day | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FDaveJesse%2Fmertik-fireplace-wifi-with-thermostat%2Fmain%2Fblueprints%2Fschedule_based_control.yaml) |

> **Note:** The integration sends nothing to the fire at startup. To make sure
> the fire is off after Home Assistant restarts, use the **Safety shutoff**
> blueprint above.

---

## Known limitations

**Switching off always wins** — once the fire is switched off, from Home
Assistant, the handset or the MyFire app, Home Assistant treats it as off and
the thermostat will not light it again, however cold the room gets. This holds
while the flame is still dying down (it takes about 30 seconds to go out). To
resume thermostatic control, switch the fire on again. If the fire is lit from
the handset or the app after it has gone out, Home Assistant follows that once
the flame has been reported continuously for 15 seconds. The handset and the
app always light the fire at Full Heat, so a manual Heating Mode selected in
Home Assistant changes to Full Heat to match (nothing is sent to the fire); in
Thermostatic the thermostat takes over as usual (a single status
message with a flipped flame bit has been seen, and is ignored). After a Home
Assistant restart the fire counts as off unless the first status shows a flame.

**Rear burner at ignition** — Both burners light at ignition, and the
fireplace reports the rear burner on by itself; the heating mode then turns it
off if not needed. (Up to 2.5.2 the integration also sent "aux on" straight
after ignite; the fireplace answered it with "no receiver" every time, so it
is no longer sent.)

**Light when fire turns off** — The fireplace switches the light off when
the fire is turned off (from Home Assistant, the handset or the app), and the
light stays off; Home Assistant shows it off once the fireplace reports it.
After the shutdown has finished, the integration sends its brightness setting
to the fire (this does not switch the light on), so that the next time the
light comes on it is at the level Home Assistant shows. It comes on again when
the fire is next switched on from Home Assistant (with **Switch the light on
when the fire is switched on**), or whenever you switch it on yourself, fire
on or off.

**Handset APP mode** — The handset shows "APP" only when specific conditions
are met. Controls work correctly regardless of whether "APP" is displayed.

---

## Troubleshooting

### Integration fails to connect / stays unavailable

**Symptom:** The integration shows as unavailable or the setup fails with "Unable to connect to fireplace".

**Description:** The integration communicates over TCP port 2000. If the fireplace's myfire WiFi box cannot be reached on that port, setup or polling will fail.

**Resolution:**
1. Confirm the myfire WiFi box is powered and joined to your network (the handset should show "APP").
2. Verify the IP address entered during setup. Open a terminal and run `ping <ip>` — the box should respond.
3. Check that nothing on your network is blocking TCP port 2000 (router firewall, VLAN isolation, etc.).
4. If the IP address has changed (DHCP re-assignment), update it via **Settings → Devices & Services → Mertik Maxitrol → ⋮ → Reconfigure**, or assign the box a static DHCP lease in your router.

---

### Handset does not enter APP mode / entities are unavailable

**Symptom:** The handset never shows "APP" and ambient temperature is missing or stuck.

**Description:** APP mode is required for the WiFi box to accept remote commands. The handset enters APP mode automatically once the myfire box establishes a TCP connection with the integration — it is not a manual step. If APP mode never appears, the box has not connected.

**Resolution:**
1. Confirm the integration is set up and the config entry is loaded (not in a failed state).
2. Power-cycle the myfire WiFi box (unplug it from the fireplace for 10 seconds).
3. Check the HA log (`Settings → System → Logs`) for connection errors referencing the fireplace IP.
4. If problems persist, close the MyFire app on all phones and tablets and try again. The integration uses a short connection for each poll so it can work alongside the app, but the box serves only one client at a time.

---

### Flame Height lags behind a heating mode change

**Description:** The Flame Height sensor shows the gas valve position the fireplace reports. The valve moves gradually, so after a change it can take a poll or two (10–20 seconds) to settle (for example Low → High). Flame height commands sent within ~35 seconds of ignition are ignored by the firmware; the integration waits for this automatically.

---

### Temperature sensor does not appear in the Configure dropdown

**Symptom:** An external temperature sensor is not listed when configuring the thermostatic sensor.

**Description:** The dropdown only shows sensors that have `device_class: temperature` set in their state attributes and have a current state in HA. Sensors that are unavailable, unknown, or missing the device class are excluded.

**Resolution:**
1. Go to **Developer Tools → States** and find your sensor. Check that `device_class` is `temperature` in the attributes.
2. If `device_class` is missing, add it via a `template` sensor or customize the entity in `configuration.yaml`.
3. If the sensor is from a custom integration, ensure it is currently available (not `unavailable` or `unknown`).
4. After confirming the sensor is valid, return to **Settings → Devices & Services → Mertik Maxitrol → Configure** and the sensor should appear.

---

### "The fire did not follow Home Assistant" alert

**Symptom:** **Settings → Repairs** shows "the fire did not follow Home Assistant", for example the burner still lit after the thermostat turned it down to pilot.

**Description:** After each heating-mode change the integration checks, about 25 seconds later, that the fireplace reports the new mode, and sends it again if not. The fireplace has twice been seen to acknowledge "down to pilot" and carry on at Low. If three attempts fail, this alert is raised so that Home Assistant does not quietly show a mode the fire is not in.

**Resolution:** Check the fire. Select the heating mode again, or switch the fire off. The alert clears by itself once the fire follows.

---

### "WiFi box is not responding" alert

**Symptom:** The controls show as unavailable and **Settings → Repairs** shows "WiFi box is not responding". The MyFire app usually shows a loss of signal at the same time.

**Description:** The WiFi box has not answered any command for 30 seconds.

**Resolution:** Operating the fire with the handset often restores the link. If not, switch the fireplace's power off for 30 seconds and back on, and check the receiver's power supply or batteries. The alert clears by itself once the box replies again.

---

### Fault Code sensor shows F43 — WiFi box cannot reach the fireplace

**Symptom:** The fire does not respond to Home Assistant (no beeps, light does not change), the Fireplace switch and Thermostat show as unavailable, and **Settings → Repairs** shows "WiFi box cannot reach the fireplace (F43)". The MyFire app shows F43 "No receiver connected". The handset may still work.

**Description:** The myfire WiFi box is on the network but has lost its radio link to the fireplace receiver. It answers every command with a short error reply instead of a status packet (or with status packets marked as having no receiver link), so nothing reaches the fire. A link that drops for a few seconds while the fire ignites or shuts down is normal and is ignored; F43 is reported only if it lasts 30 seconds.

**Resolution:**
1. Switch the fireplace's power off for 30 seconds and back on.
2. If F43 persists in the MyFire app, re-pair the WiFi box with the receiver following the box's instructions (disable the integration while pairing so the app has the box's only connection).
3. The entities and the Repairs alert recover by themselves once the link is restored.

---

### Fault Code sensor shows F44 — Handset not in range or low battery

**Symptom:** The Fault Code sensor shows "F44 – Handset not in range or low battery". In Thermostatic mode, the fire may drop to Standby even though the room is cold.

**Description:** Shown when the fireplace reports the handset missing, or when the handset's temperature has not changed for 30 minutes with the burner lit (a handset without batteries keeps its last reading in the fireplace's status for about 90 minutes before the fireplace reports it missing). The myfire handset communicates with the WiFi receiver via 868 MHz RF. When the handset is out of range, has a low battery, or is turned off, the receiver loses contact and reports F44. Because the handset is also the device's temperature sensor, the integration cannot trust any temperature reading the device reports in this state. When no external HA sensor is configured, the integration automatically falls back to Standby to avoid heating based on a meaningless temperature value.

**Resolution:**
1. Check handset battery — replace if low.
2. Ensure the handset is within RF range of the receiver (typically the same room).
3. If the handset cannot be kept in range, configure an independent HA temperature sensor via **Settings → Devices & Services → Mertik → Configure** — thermostatic control then operates independently of the handset.
4. The fault clears automatically once the handset reconnects; thermostatic control resumes without any manual intervention.

---

### Fire turns itself off while in Thermostatic mode

**Symptom:** The fire extinguishes unexpectedly when the room is warm, then re-ignites when it cools.

**Description:** This is intentional thermostatic Standby behaviour. When the room temperature reaches the setpoint, the main burners are extinguished but the pilot flame stays lit. "Off" in this context means the main burners are off, not that the device has turned off entirely. The Fireplace switch remains on.

**Resolution:**
This is not a fault. If you want the fire to stay at a fixed output rather than cycling, select **Full Heat**, **Medium Heat**, or **Low Heat** from the Heating Mode entity instead of Thermostatic.

---

## Safety

This integration controls a gas appliance. Use at your own risk. The authors
accept no responsibility for any damage or injury. Always ensure your
fireplace has a working ODS (Oxygen Depletion Sensor) and is maintained by
a qualified engineer.
