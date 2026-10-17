# A. O. Smith BLE

A Home Assistant custom integration for A. O. Smith heat pump water heaters that
speak Bluetooth Low Energy (the heater advertises as `iCOMM-AC000W037XXXXXX`, where
the tail is the serial number). It talks to the heater locally over Bluetooth. No
cloud account is needed.

## Disclaimer

This is an independent, unofficial community project. It is not affiliated with,
endorsed by or sponsored by A. O. Smith Corporation or its subsidiaries. "A. O. Smith",
"iCOMM" and the product names are trademarks of their respective owners and are used
only to identify the devices this integration talks to. The software is provided as
is, without warranty, under the MIT license. It writes settings to a water heater, so
use it at your own risk.

Requires Home Assistant 2026.3.0 or newer and a Bluetooth path to the heater. The
tested path is one ESPHome Bluetooth proxy with active connections, in range of the
heater (see the Bluetooth proxy note). Python 3.14 is the limiting factor: the
floor comes from Home Assistant requiring Python 3.14 from 2026.3. CI tests the latest
Home Assistant and 2026.9; versions before 2026.9 are untested.

## Installation

### HACS (custom repository)

1. In HACS, open the menu and choose Custom repositories.
2. Add this repository's URL with the category Integration.
3. Install "A. O. Smith BLE" and restart Home Assistant.

### Manual

Copy the `custom_components/aosmith_ble` folder from this repository into the
`custom_components` folder of your Home Assistant configuration directory, then
restart Home Assistant.

## Bluetooth proxy note

Tested with one ESPHome active proxy (esp-idf framework). Other ESPHome proxies and
local USB adapters are untested, and their result is unverified.

The proxy must have active connections enabled. A passive proxy (0 connection slots)
gives no discovery card, and adding the heater manually fails. An excerpt of the
ESPHome configuration of the tested proxy:

```yaml
esp32:
  # board: <your board>
  framework:
    type: esp-idf

esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

A proxy has connection slots only when `bluetooth_proxy` has `active: true`, as in
the excerpt above. Without it the proxy is passive (0 slots).

The integration permanently holds one connection slot on the adapter or proxy it
uses.

## Tested on

- Home Assistant 2026.9, heater model HPTS-50, one ESPHome Bluetooth proxy configured
  as above.
- Verified there: discovery, pairing, polling every 60 s (about 1 s per poll),
  setpoint writes and the range refusal, permanent modes, timed Electric, Vacation
  through away mode and back, a mode change made on the heater's panel (seen within
  one poll), a reload after a link held for more than 30 minutes (reconnects in about
  30 s), a proxy reload (reconnects in about 18 s), and a proxy powered off for about
  12 minutes (repair issue at the third failure, recovery about 28 s after the proxy
  returned, issue cleared by itself).
- Not tested: other proxies, local USB adapters, whether a connect cancelled
  mid-way releases a proxy connection slot, recovery from a heater whose
  Bluetooth signal has stopped (that needs a button press), Home Assistant 2026.3 to
  2026.8, and Home Assistant 2026.10 on hardware (CI only).

## Pairing

The heater only accepts a new session while it is in pairing mode. When Home
Assistant discovers the heater (or you add it manually from Settings -> Devices &
services):

1. Close the A. O. Smith app.
2. Press the Bluetooth button on the heater's panel once so its LED flashes. If it
   was already flashing, turn Bluetooth off and on at the panel.
3. Submit the setup form.

The same steps apply when Home Assistant asks you to authenticate again (reauth).

## Entities

Entities are created from what your heater model reports and allows. A control only
appears if the heater accepts writes to it.

| Entity | Platform | Notes |
| --- | --- | --- |
| Water heater (named after the device) | `water_heater` | Setpoint in Fahrenheit, operation mode, away mode (Vacation). |
| Operating mode | `select` | Hybrid, Heat pump, Electric, Vacation, for the modes the heater lets you set. |
| Fault | `sensor` | `none`, a fault key such as `dry_fire`, or `unknown`. Attribute `code` holds the number. |
| Problem | `binary_sensor` | On while a fault is active. |
| Electric days remaining | `sensor` | Days left while in Electric mode, otherwise 0. |
| Vacation days remaining | `sensor` | Days left while in Vacation mode, otherwise 0. |
| Electric mode days | `number` | 1-7. Configuration entity. |
| Vacation mode days | `number` | 1-99. Configuration entity. |

The service `aosmith_ble.set_timed_mode` (fields `mode`: `electric` or `vacation`,
and `days`) starts a timed mode directly on one or more water heater entities.

## Mode mapping

This matches the core cloud `aosmith` integration, so the same states appear if you
move between them.

| Heater mode | Water heater operation | Select option |
| --- | --- | --- |
| Hybrid | `eco` | `hybrid` |
| Heat pump | `heat_pump` | `heat_pump` |
| Electric | `electric` | `electric` |
| Vacation | `off`, with away mode on | `vacation` |
| Guest | `high_demand` (display only) | none |

Guest mode can be shown but never set. Turning away mode off returns to the last
permanent mode (Hybrid if none is known).

## Day counts

Electric and Vacation are timed modes. The Electric mode days and Vacation mode days
number entities hold the number of days to use the next time you select that mode.
Selecting the mode (from the water heater, the select, or the set timed mode
service) starts it for that many days. The service takes its own `days` value and
leaves the stored numbers unchanged.

Changing a day-count number entity does not change a timed mode that is already
running. To change a running countdown, select the mode again after changing the
number, or call the service with the new value.

## Repairs and recovery

Problems show up as repair issues on the entry, each with a plain instruction:

- Connectivity: the heater's Bluetooth signal is not active, no Bluetooth
  connection is available, or the heater is unreachable (after three failed polls in
  a row). Close the A. O. Smith app, press the Bluetooth button on the heater's
  panel, and check range. "No Bluetooth connection available" means either all
  connection slots on the adapter or proxy that hears the heater are in use, or that
  adapter or proxy is offline. Check that the adapter or proxy is online, free a
  connection slot, or add another proxy. The issue clears on the next successful
  connection.
- Unsupported configuration: the display units are not Fahrenheit (set them on the
  panel), or the adapter or proxy negotiated too small an MTU (use another one).
  The fix flow has a Retry button that reloads the entry.
- Unsupported device: see below.

If the heater rejects the session, Home Assistant starts a reauthentication; follow
the pairing steps above. Removing the entry removes its issues.

## Polling and backoff

The heater is read every 60 seconds by default. Change this under the integration's
Configure option, from 15 to 300 seconds.

From the third failed poll in a row the interval grows: it is 2x the configured
interval at the third failure and 4x at the fourth, doubling with each further
failure, up to 10 minutes. A successful poll restores the configured interval. A
Bluetooth advertisement from the heater still triggers an immediate refresh, so
pressing the panel button never waits for the backoff. On Home Assistant before
2026.10 an advertisement whose data is unchanged may not trigger it; recovery then
happens on the next poll.

## Unsupported devices

If the heater model or firmware is not recognized, the entry loads in a
diagnostics-only mode and raises an "unsupported device" repair issue showing the
model and firmware. Press the Bluetooth button on the heater's panel, download
diagnostics from the entry, and attach them to a new issue in this repository. The
diagnostics are scrubbed of addresses and serial numbers. No re-pairing is needed
when an update adds support: the entry just sets up normally on the next load, or
use the Retry button in the repair.

## Debug logging

Everything this integration logs is scrubbed of the Bluetooth address and serial
number. Home Assistant's own `habluetooth` logger prints the heater's full advertised
name (which holds the serial number and the pairing code) and its address at INFO
level, and third-party libraries (bleak, bleak-retry-connector) can log the address at
debug level. Scrub Home Assistant logs before sharing them.

## Known limits

- No tank temperature and no energy data.
- The heater accepts one Bluetooth central at a time, so the vendor app cannot
  connect while the integration is loaded. Disable the entry to use the app.
- Temperatures are Fahrenheit only; the heater's panel must be set to Fahrenheit.
- After a change, the entities update from a fresh read of the heater. If a read
  finished less than a second earlier, or is still running, the fresh read waits for
  it and then about a second more, so the old state can show briefly.
- Pressing the heater's Bluetooth button while the integration is connected does
  nothing.
