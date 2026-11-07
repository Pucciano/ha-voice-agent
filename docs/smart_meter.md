# Smart meter reader

A bitShake SmartMeterReader reads the household electricity meter, an EMH
eHZB-W22E8, through its optical info interface and reports to Home
Assistant over MQTT. It is the grid source of the energy dashboard; the
smart plugs stay individual devices there.

## Hardware and firmware

- **Meter:** EMH eHZB-W22E8 (eHZ generation B), three phases, import and
  export registers, SML at 9600 baud on the front info interface.
- **Reader:** bitShake SmartMeterReader, ESP32-C3, Tasmota 15.3.0 in the
  bitShake build with the SML scripter. The read head sits on the info
  interface (the small IR window), held by a magnet, cable pointing down.
  It must not cover the optical button.
- **Never update the firmware over the air.** The bitShake build carries
  the SML driver; a standard Tasmota OTA image drops it. The device's OTA
  URL says so.
- **Matter is off** (`SetOption151 0`). It only exposed diagnostics, cost
  about 20 KB of the small heap, and the reader is in Home Assistant through
  the Tasmota integration instead.
- **Network:** fixed DHCP lease (`<reader-ip>`), hostname `<reader-hostname>`.
  The meter cabinet gets a weak 2.4 GHz signal (about -86 to -88 dBm). The
  access point must not drop weak clients ("minimum RSSI" off for the IoT
  2.4 GHz network), or the reader may not get back on after a restart.

## Unlocking the meter (full data set)

Without the PIN the meter sends the **reduced data set**: whole kWh and no
power. Symptom in Home Assistant: the import never changes its decimals and
power stays at 0 W.

The PIN comes from the grid operator. The meter is operated with light on
its optical button (a torch or a phone app such as edl21-control):

| Signal | Meaning |
|---|---|
| short, < 4.5 s | wake the display, count the current PIN digit up by one, next menu item |
| long, > 4.5 s | toggle the current menu item (`InF`, `PIn`, history resets) |
| 3 s without light | accept the digit and move to the next one |
| 120 s without light | back to normal display; settings stay |

Values that worked with edl21-control: one blink to start, about 5 s pause
before the first digit (until `- 0 - - - -` shows), short blinks of 0.5 s
with 0.5 s gaps, about 4 s between digits, long blinks of 6 s. For a 0,
give no blink and only wait.

1. One short blink: display test, then `- 0 - - - -`.
2. Enter the four digits as above. A correct PIN unlocks the display; a
   wrong one returns to the normal display. Six blinking dashes mean too
   many wrong attempts and a lockout.
3. Short blinks through `P`, `E`, `1d`, `7d`, `30d`, `365d` to `InF`. One
   long blink: `InF on` (full data set).
4. One short blink to `PIn`. One long blink: `PIn OFF` (no PIN on the
   next call).

**After a power loss the meter starts with its factory settings again**
(EMH manual), so `InF` and `PIn` may need setting again. The automation
below reports this case.

Source: EMH eHZB operating instructions (eHZB-BIA-D-1.02).

## What the meter sends

With `InF on` the meter sends every second (OBIS, scaler applied by the
SML driver):

| OBIS | Value | Tasmota key | Unit |
|---|---|---|---|
| 1.8.0 | Import register | `ImportActive` | kWh |
| 2.8.0 | Export register | `ExportActive` | kWh |
| 16.7.0 | Total active power | `Power` | W |
| 36.7.0 / 56.7.0 / 76.7.0 | Active power L1 / L2 / L3 | `Power_L1` … `Power_L3` | W |
| 32.7.0 / 52.7.0 / 72.7.0 | Voltage L1 / L2 / L3 | `Voltage_L1` … `Voltage_L3` | V |
| 31.7.0 / 51.7.0 / 71.7.0 | Current L1 / L2 / L3 | `Current_L1` … `Current_L3` | A |
| 14.7.0 | Frequency | `Frequency` | Hz |

It also sends phase angles (81.7.x) and its device id, which the script
skips. There are no tariff registers (1.8.1/1.8.2).

The export register held 0.005 kWh on 2026-11-06 although the house has no
PV: a few Wh from the meter test or brief feedback of devices. It is real,
not a reading error. A value that keeps growing would need a look.

## SML script

The bitShake default script read tariff registers and two extra power
values that this meter does not send. `config/tasmota/smart_meter_ehzb.sml`
is the script adapted to this meter.

Upload it in the Tasmota web interface (Tools, Edit script, enable, save),
or with the scripter's form endpoint:

```bash
curl -F "c1=on" -F "t1=<config/tasmota/smart_meter_ehzb.sml" -F "save=" http://<reader-ip>/ta
```

Read the running script back with `http://<reader-ip>/ufs/script.txt`.

**No data at all** (values frozen, kWh and power do not move): the read head
slipped. Check with the raw dump, which prints every SML telegram to the
web log:

```bash
curl "http://<reader-ip>/cm?cmnd=sensor53%20d1"
curl "http://<reader-ip>/cs?c2=0"
curl "http://<reader-ip>/cm?cmnd=sensor53%20d0"
```

Lines starting with `77 07 01 00 …` are OBIS records. None within a few
seconds means the head receives nothing.

## MQTT

The broker is Mosquitto in the `mqtt-broker` compose stack on the gateway
Pi. The reader logs in with its own user:

- `mosquitto.conf` has `password_file /mosquitto/config/passwd` in
  addition to `allow_anonymous true` (Zigbee2MQTT and Home Assistant still
  connect anonymously).
- Create or change the user inside the container:

  ```bash
  sudo docker exec mqtt mosquitto_passwd -b /mosquitto/config/passwd <reader-mqtt-user> '<password>'
  sudo docker restart mqtt
  ```

  The file must belong to uid 1883 (`chown 1883:1883`, mode `0700`).
- On the reader (Tasmota console or Settings, MQTT): `MqttHost`,
  `MqttUser <reader-mqtt-user>`, `MqttPassword`, `MqttClient <reader-mqtt-user>`,
  `SetOption19 0` (Tasmota integration discovery, not HA MQTT discovery).
  Changing the host restarts the reader.
- Keep the password out of the repository; it lives in a local env file
  only.

**After `apt upgrade` on the gateway:** when containerd was upgraded, the
running Docker daemon can no longer start containers ("shim was started
through the deprecated API"). `docker restart mqtt` then leaves the broker
stopped. Reboot the Pi, or run `sudo systemctl restart containerd docker`
and start any container that stays `Exited` with `docker start`. The OTBR
container runs with `network_mode: host`, so `docker ps` shows no ports for
it; that is expected.

## Home Assistant

- **Integration:** Tasmota (discovery prefix `tasmota/discovery`). It
  creates the device "bitShake SmartMeterReader" with
  `sensor.bitshake_smartmeterreader_ehz_*`.
- **Names:** the device is called "Stromzähler" (`name_by_user`, area
  Zählerschrank) and each entity has a short German name, so the display
  names read "Stromzähler Bezug", "Stromzähler Leistung L1" and so on. The
  entity ids keep the integration's English ids, which the energy
  dashboard, its statistics and the automation use.

  | Entity id suffix | Name |
  |---|---|
  | `ehz_importactive` | Bezug |
  | `ehz_exportactive` | Einspeisung |
  | `ehz_power` | Leistung |
  | `ehz_power_l1` … `_l3` | Leistung L1 … L3 |
  | `ehz_voltage_l1` … `_l3` | Spannung L1 … L3 |
  | `ehz_current_l1` … `_l3` | Strom L1 … L3 |
  | `ehz_frequency` | Netzfrequenz |
  | diagnostics | Letzter Neustart, WLAN-Verbindungen, MQTT-Verbindungen, Neustartgrund, WLAN, IP-Adresse, WLAN-Qualität, WLAN-Signal, Firmware |

  None of them is exposed to Assist.
- **Energy dashboard:** grid consumption
  `sensor.bitshake_smartmeterreader_ehz_importactive`, return to grid
  `sensor.bitshake_smartmeterreader_ehz_exportactive`, grid power
  `sensor.bitshake_smartmeterreader_ehz_power`. The smart plugs are
  individual devices; the rest shows as untracked consumption. The former
  group "Utility Meter" (`sensor.power_utility_meter`, the sum of the plug
  energies) was the grid source until 2026-11-07 and is deleted.
- **Per-phase values** have no unit or device class: the Tasmota
  integration only knows plain keys such as `Power` or `Voltage`.
- **Automation** `automation.smart_meter_data_check` ("Stromzähler
  Datenprüfung", `config/homeassistant/smart_meter/automation.yaml`):

  | Case | Trigger | Notification |
  |---|---|---|
  | Reduced data set or head slipped | power below 1 W for 15 minutes (the house never draws 0 W) | "Stromzähler": unlock again with the PIN, or check the read head |
  | Reader offline | power `unavailable` for 30 minutes | "Stromzähler": reader not reachable |
  | Values back | power above 0 W for 1 minute | removes the notification |

  Deploy: `POST /api/config/automation/config/smart_meter_data_check` with
  the entry, then rename the entity to `automation.smart_meter_data_check`
  once.

## Tariff

Prices live in Home Assistant helpers, never in this repository. Three
tariff slots, each a set of three UI helpers (not exposed to Assist):

| Helper | Type | Meaning |
|---|---|---|
| `input_datetime.electricity_tariff_N_valid_from` "Stromtarif N gültig ab" | date | first day of the tariff |
| `input_number.electricity_tariff_N_work_price` "Stromtarif N Arbeitspreis" | box, ct/kWh, 0–100, step 0.01 | work price; 0 marks the slot as unused |
| `input_number.electricity_tariff_N_base_price` "Stromtarif N Grundpreis" | box, EUR per month, 0–100, step 0.01 | monthly base price |

Each slot also names its contract, so a period can later be matched to
supplier and product (a tariff comparison, or an API of the supplier or the
grid operator):

| Helper | Type | Meaning |
|---|---|---|
| `input_text.electricity_tariff_N_provider` "Stromtarif N Anbieter" | text, up to 100 characters | supplier as on the contract |
| `input_text.electricity_tariff_N_product` "Stromtarif N Tarif" | text, up to 100 characters | product name as on the contract |
| `input_text.electricity_grid_operator` "Netzbetreiber" | text, up to 100 characters | grid operator of the connection (one for all slots: it belongs to the meter, not to the contract) |

Contract or meter numbers stay out of Home Assistant names and out of this
repository.

The active slot is the one with the latest date that is not after today
(local date), so a price change takes effect at midnight of its first day.
For a new tariff, fill an unused slot or overwrite the oldest past one.

Four helpers derive from the slots:

- **Template sensor "Stromtarif aktuell"** (`sensor.electricity_tariff`, text,
  `electricity_tariff.jinja`): "Anbieter · Tarif" of the active slot, "Tarif N"
  without names, `unknown` without an active slot. Its history records which
  contract was valid when.

- **Template sensor "Arbeitspreis aktuell"** (`sensor.electricity_work_price`,
  EUR/kWh, state template `config/homeassistant/helpers/electricity_work_price.jinja`).
  The energy dashboard prices the grid import with it ("Use an entity with
  current price"). Home Assistant then records the cost of every hour at the
  price valid in that hour as `sensor.bitshake_smartmeterreader_ehz_importactive_cost`.
- **Template sensor "Grundpreis je Stunde"** (`sensor.electricity_base_rate`,
  EUR/h, `electricity_base_rate.jinja`): the monthly base price of the active
  slot spread over the hours of the current month.
- **Integral helper "Grundpreis"** (`sensor.electricity_base_cost`, source
  `sensor.electricity_base_rate`, left Riemann sum, time unit hours, 4
  decimals, max sub-interval 15 minutes): the base price added up in EUR, a
  statistic next to the energy cost for later evaluations.

Both costs are recorded once and keep their price: changing a slot later
does not reprice hours that are already recorded. Fix a wrong price in the
statistics (Developer tools → Statistics → adjust a sum) if it matters. The
base price only accrues while Home Assistant runs the integral helper; it
does not start before the helper exists.

Set up on 2026-11-07 without prices or names (the text helpers then report
`unknown`, which the templates and the display treat as empty): all slots
have a work price of 0, so
the price sensors are `unknown`, `energy/validate` reports the price entity
as not numeric, no cost is recorded, and the display shows "Tarif fehlt".
Costs start with the first slot that gets a date and a work price; nothing
is priced backwards.

The helpers were created over the websocket (`input_datetime/create`,
`input_number/create`, `input_text/create`) and the config flows `template` (step `sensor`) and
`integration`, created with their German names and then renamed to the
entity ids above; all are unexposed from Assist.

The wall display reads the costs from these statistics (docs/wall_display.md):
the day view shows the work price only, week, month and year add the base
price.

## Testing

- `Status 10` on the reader shows `ImportActive` with decimals and `Power`
  above 0.
- The Mosquitto log shows `New client connected … as <reader-mqtt-user> (… u'<reader-mqtt-user>')`.
- `energy/validate` over the websocket reports no issues.
- The automation was tested on 2026-11-07 with a temporary copy whose
  trigger fired on every power change: the notification appeared with the
  current import, and the "values back" branch removed it. The copy was
  deleted afterwards.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Import in whole kWh, power 0 W | Meter in the reduced data set (after a power loss); unlock and set `InF on` |
| All values frozen | Read head slipped; reseat it (centred, flat, cable down) and check with the SML dump |
| Reader gone after a restart | Weak Wi-Fi; it reset itself after about 20 minutes on 2026-11-07. Check the AP's client list, then power-cycle the USB cable |
| Values 0 in fields `Power2`, `E_inHT` | Old default script; upload `smart_meter_ehzb.sml` |
| Broker down after a gateway upgrade | Restart containerd and Docker, or reboot the Pi |
