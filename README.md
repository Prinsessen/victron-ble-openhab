# Victron Blue Smart IP65 12/10 — BLE GATT Monitor

Standalone BLE monitor daemon for the **Victron Blue Smart IP65 12/10** battery charger. Runs on a Raspberry Pi 3 Model B+, connects via BLE GATT to read charger data, and posts values to openHAB REST API.

## Hardware

| Component | Details |
|-----------|---------|
| Charger | Victron Blue Smart IP65 12/10 (12V, 10A) |
| BLE Address | `AA:BB:CC:DD:EE:FF` |
| BLE Name | "Springfield Charger" |
| Manufacturer ID | `0x02E1` (Victron Energy) |
| Host | Raspberry Pi 3 Model B+ (`192.168.1.60`, user `pi`) |
| BLE Adapter | hci0 (`AA:BB:CC:DD:EE:FF`) |
| openHAB | `192.168.1.21:8080` |

## How It Works

The daemon connects to the charger via BLE GATT every 30 seconds, reads battery voltage, charge current, yield, and derives the charge state. When the charger is unplugged from mains, BLE becomes unavailable and the daemon marks it offline after 3 consecutive failures (~90 seconds).

### BLE GATT Protocol

The Victron Blue Smart IP65 uses a proprietary BLE GATT service (`306b0001-b081-4037-83dc-e59fcc3cdfd0`) with three characteristics:

| UUID | Name | Properties | Purpose |
|------|------|------------|---------|
| `306b0002` | Init | read, write-no-response, notify | Initialization + keepalive |
| `306b0003` | Cmd | write-no-response, notify | Data commands + register data |
| `306b0004` | Bulk | write-no-response, notify | Bulk register data |

**Connection sequence:**
1. Connect and subscribe to all 3 characteristics (notifications)
2. Write init sequence to `306b0002`: `fa80ff`, `f980`, `01` (300ms apart)
3. Write data commands to `306b0003`: `01`, `0300`
4. Write subscribe command: `060082189342102703010303`
5. Send keepalive `f941` to `306b0002` every 1.5 seconds
6. Collect data for 12 seconds, disconnect

**Data packet format:**
```
08 00 19 [reg_hi] [reg_lo] [dtype] [value...]
```
Where dtype: `0x42` = uint16 (2 bytes), `0x44` = uint32 (4 bytes), `0x41` = uint8 (1 byte), `0x58` = other16 (2 bytes).

### Known Registers

| Register | Type | Scale | Description |
|----------|------|-------|-------------|
| `0xED8D` | u16 | × 0.01 | Battery Voltage (V) |
| `0xEDD5` | u16 | × 0.01 | Battery Voltage 2 (V) — duplicate |
| `0xED8C` | u32 | raw | Battery Current (mA) |
| `0xED8F` | u16 | × 0.1 | Battery Current (A) |
| `0xEDDB` | u16 | ÷ 1000 | Yield Today (raw Wh → kWh) |
| `0x0120` | u32 | raw | Timer/Counter (seconds) |

### Writing: the charge preset restarts the cycle (2026-10-14)

The IP65 has no on/off - not in VictronConnect, and a write to `0x0200` (device mode, as used
for Phoenix inverters, `0603821902004104`) is acknowledged `07030602` and ignored. What it does
have is the **charge preset**, register `0xEDF1`, captured from VictronConnect with an Android
HCI snoop log. Written on the command characteristic `306b0003`:

| Command | Meaning |
|---------|---------|
| `06008219edf14102` | preset = **High** |
| `06008219edf14100` | preset = **Normal** (the one in use) |
| `05008119edf1` | GET the preset (the app reads it before writing) |

Not used, never write them: Normal + recondition, High + recondition (~16 V), Li-ion.

Switching Normal -> High -> Normal restarts the charge algorithm: Storage -> Bulk -> Absorption.
Proven with the owner watching the LEDs: a **full** battery in Storage (13.4 V, 0 mA) then took
110-420 mA at 14.6 V, and Proof D said "current seen" - which is the clamp-on proof the passive
detection cannot get from a full battery. With the clamps off, no current can flow. The daemon
uses this as **the prick** - see "The prick" under Proof D below.

Each write is acknowledged with the new value (`...080019edf14102`), and a GET returns
`080019edf141<value>`, so every step can be read back.

#### How the preset command was found (Bluetooth HCI snoop)

Olen's protocol work covers reading; nothing published covered writing a setting on the
IP65, and his Phoenix on/off write did nothing here. So the command was taken from the
official app while it did the thing:

1. **Capture.** On an Android phone: Developer options -> *Enable Bluetooth HCI snoop log*,
   then Bluetooth off and on (many phones only start the log then). Stop anything else that
   holds the charger's BLE link (here: `sudo systemctl stop victron-ble-monitor` on the Pi).
   Open VictronConnect, change the setting once (preset Normal -> High -> Normal), note the
   time, and watch the charger's LEDs to see what the change does.
2. **Collect.** Developer options -> *Bug report* -> *Full report*. The log is
   `FS/data/log/bt/btsnoop_hci.log` inside the zip (the path varies by phone).
3. **Read.** `python3 btsnoop_writes.py btsnoop_hci.log` lists the GATT writes the phone sent,
   minus the connect sequence, keepalive, poll and register reads. What is left is the
   command: on 2026-10-14, exactly four writes at the two preset changes, done twice -
   `06008219edf14102` (High) and `06008219edf14100` (Normal).
4. **Replay.** Send the same bytes from the Pi on the same characteristic (`306b0003`) and
   read the register back (`05008119edf1`) to prove the charger took it.

A VE.Direct-over-BLE register write reads `06 00 82 19 <reg lo> <reg hi> 41 <value>` - byte 2
is `00` in everything the app sends (Olen's inverter command has `03` there). The same
method should find any other setting VictronConnect can change.

The bug report and the log hold the phone's other Bluetooth traffic and much more - extract
the one file, delete the rest, never publish either.

> **Note:** Register `0xEDD4` (native charger state) is NOT available over BLE GATT on this charger model. Explicit GET requests return error responses. The "Instant Readout" BLE advertisement feature is also not available on the Blue Smart IP65.

### Charge State Derivation

Since the charger doesn't expose its internal state register, the charging phase is derived by matching the measured output voltage to the charger's known voltage setpoints:

| State | Voltage Range | Description |
|-------|---------------|-------------|
| **Absorption** | 14.1 – 14.7V (14.4V ± 0.3) | Voltage held at absorption setpoint, current tapering |
| **Float** | 13.5 – 14.1V (13.8V ± 0.3) | Voltage held at float setpoint, any current level |
| **Storage** | 12.9 – 13.5V (13.2V ± 0.3) | Reduced voltage, minimal current |
| **Bulk** | Below setpoints | Max current, voltage rising toward absorption |
| **Recondition** | > 15.0V | Desulfation pulse mode |
| **Idle** | Any voltage, < 50mA | No battery load (disconnected or fully charged) |
| **Off** | N/A | Charger mains disconnected (BLE offline) |

**Key insight:** Current level does NOT determine the charge phase. The charger can push several amps at Float voltage if the battery demands it. The voltage setpoint is what determines the phase.

**Voltage stability tracking:** A history of the last 10 voltage readings distinguishes Bulk (voltage rising through a setpoint zone) from actually being AT a stable setpoint.

### BLE RSSI Monitoring

Before each GATT connection, a quick BLE scan measures the signal strength (RSSI) of the charger. The raw dBm value is posted to `MC_Charger_BLE_RSSI`, and a human-readable quality label to `MC_Charger_BLE_Signal`:

| dBm Range | Quality |
|-----------|----------|
| ≥ −60 | Excellent |
| −60 to −70 | Good |
| −70 to −80 | Fair |
| −80 to −90 | Weak |
| < −90 | Very Weak |

Uses `BleakScanner.discover(return_adv=True)` — bleak 2.x requires `AdvertisementData` for RSSI (not available on `BLEDevice`).

### Offline Detection

When the charger is disconnected from mains power, its BLE radio shuts off. After 3 consecutive failed BLE connection attempts (~90 seconds), the daemon:
- Sets `MC_Charger_BLE_Online` → `OFF`
- Clears `MC_Charger_State` → `Off`
- Clears `MC_Charger_Voltage` → `0`
- Clears `MC_Charger_Current` → `0`
- Clears `MC_Charger_Current_mA` → `0`

## Deployment

### Pi Setup

```bash
# On Pi 3 (192.168.1.60)
mkdir -p /home/pi/victron-ble
python3 -m venv /home/pi/victron-ble/.venv
source /home/pi/victron-ble/.venv/bin/activate
pip install bleak aiohttp
```

### BLE Pairing

The charger must be paired and bonded before the daemon can connect:

```bash
# On Pi — pair with default passkey 000000
bluetoothctl
  scan on
  # Wait for AA:BB:CC:DD:EE:FF to appear
  pair AA:BB:CC:DD:EE:FF
  # Enter passkey: 000000
  trust AA:BB:CC:DD:EE:FF
  quit
```

### Deploy Daemon

```bash
# From openHAB host
scp -i ~/.ssh/id_ed25519 victron_ble_monitor.py pi@192.168.1.60:/home/pi/victron-ble/
scp -i ~/.ssh/id_ed25519 victron-ble-monitor.service pi@192.168.1.60:/tmp/

# On Pi
sudo cp /tmp/victron-ble-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable victron-ble-monitor
sudo systemctl start victron-ble-monitor
```

### Verify

```bash
# Check service status
sudo systemctl status victron-ble-monitor

# Watch live logs
journalctl -u victron-ble-monitor -f --no-pager

# Expected output:
# V=13.80V  I=1400mA (1.40A)  Yield=3.27kWh  State=Float  regs=10
```

## openHAB Integration

### Items

Defined in `items/motorcycle_k7_power.items`:

| Item | Type | Description |
|------|------|-------------|
| `MC_Charger_BLE_Online` | Switch | BLE connection status (ON/OFF) |
| `MC_Charger_Connection` | String | Human-readable status: "Offline" / "Standby (cable detached)" / "Charging — \<stage\>" |
| `MC_Charger_Voltage` | Number | Battery voltage (V) |
| `MC_Charger_Current` | Number | Charge current (A) |
| `MC_Charger_Current_mA` | Number | Charge current (mA) |
| `MC_Charger_Yield` | Number | Charged energy today (kWh) |
| `MC_Charger_State` | String | Charge state (Off/Idle/Bulk/Absorption/Float/Storage/Recondition) |
| `MC_Charger_Last_Update` | DateTime | Last successful data update |
| `MC_Charger_BLE_RSSI` | Number | BLE signal strength in dBm (updated each poll cycle) |
| `MC_Charger_BLE_Signal` | String | Human-readable signal quality, e.g. "Good (-70 dBm)" |
| `MC_Charger_Secondary_Proof` | String | **Proof D** verdict — are the DC clamps on the bike? See below |
| `MC_Secondary_Connected` | Switch | Boolean clamp truth derived from Proof D (consumed by the JS rule) |
| `MC_Charger_Reg_Count` | Number | BLE registers/cycle (A/B-test evidence, InfluxDB history only) |
| `MC_Charger_Data_Bytes` | Number | BLE data bytes/cycle (A/B-test evidence, InfluxDB history only) |
| `MC_Charger_Prick` | Switch | ON = prick on the next cycle (manual); the daemon switches it back OFF |
| `MC_Charger_Prick_Result` | String | What the last prick did and what it proved, e.g. "14/10 21:05 current seen - clamps on the bike" |

### Sitemap

Located in `sitemaps/myhouse.sitemap` under the "Victron Charger" frame, between INNOVV K7 and Romeo Robot sections.

## Configuration

All configuration is at the top of `victron_ble_monitor.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CHARGER_ADDR` | `AA:BB:CC:DD:EE:FF` | BLE MAC address of charger |
| `OPENHAB_URL` | `http://192.168.1.21:8080/rest/items` | openHAB REST endpoint |
| `POLL_INTERVAL` | `30` | Seconds between BLE poll cycles |
| `DATA_COLLECT_TIME` | `12` | Seconds to collect data per connection |
| `OFFLINE_THRESHOLD` | `3` | Consecutive failures before marking offline |
| `MAX_RETRIES` | `2` | Retries per poll cycle |
| `CURRENT_MEMORY_SEC` | `10800` | Proof D: hold "Connected" this long after a current pulse (3 h) |
| `STARTUP_GRACE_SEC` | `3600` | Proof D: optimistic "Connected" window after a restart (1 h) |
| `PRICK_PROOF_HOLD_SEC` | `10800` | The prick: a prick that saw current proves the clamps on for 3 h |
| `PRICK_BACKOFF_SEC` | `43200` | The prick: after a prick that saw no current (or failed), no automatic prick for 12 h |
| `PRICK_EVAL_SEC` | `600` | The prick: current must show within 10 min to prove the clamps on |

### Charge Profile Setpoints

Factory default for 12V lead-acid (configurable as class constants):

| Setpoint | Voltage |
|----------|---------|
| Absorption | 14.4V |
| Float | 13.8V |
| Storage | 13.2V |
| Tolerance | ± 0.3V |

## Proof D — Secondary-Connection (Clamp) Detection

"Is the charger powered on 230V?" is easy (BLE online). The harder, drain-
preventing question is **"are the DC clamps physically on the bike battery?"**
The Blue Smart IP65 exposes **no** 'secondary connected' register (Victron-
confirmed; `0xEDD5`/`0xEDD7` proved to be plain mirrors of the main
voltage/current in a live clamp on/off A/B test on 2026-08-09).

**The insight:** the BLE stream itself changes with the clamp state, because
with the clamps off there is no real battery to regulate. Crucially, **charge-
current registers (`0xED8C`/`0xED8F`/`0xEDD7`) only appear when current is
actually flowing — and current can only flow through a closed circuit.** So
"current seen" is hard physical proof the clamps are on the bike.

### `MC_Charger_Secondary_Proof` verdicts

| Verdict | Meaning |
|---------|---------|
| `Connected (current seen)` | Current in one of the last 3 BLE cycles — clamps on (hard proof) |
| `Connected (pulse Nm ago)` | No current now, but a pulse `N` min ago still within the memory window — clamps on |
| `Connected (startup grace)` | Just restarted, no pulse yet — optimistically connected for `STARTUP_GRACE_SEC` |
| `Uncertain (settling)` | Ambiguous cycle, no current, no recent pulse |
| `Off? (sparse data — clamps off)` | 3 sparse cycles, no current, no recent pulse — clamps off |
| `Offline` | Charger BLE offline (no mains) |

The daemon posts this verdict; `isSecondaryConnected()` in
`vehicle-motorcycle-k7-power.js` consumes it as the **primary** input and writes
the boolean `MC_Secondary_Connected`, which in turn drives the Springfield
"connect the charger" reminder in `vehicle-springfield-ble-presence.js`.

### Current-pulse memory (`CURRENT_MEMORY_SEC`, added 2026-09-01)

**Problem:** in **Storage on a full battery** the charger emits a visible
maintenance-current pulse only **rarely** — gaps of **30+ minutes** were observed
live, growing sparser the fuller the battery is — with genuine 0 mA stretches in
between. Judging only the last 3 BLE cycles (~2 min), Proof D flapped
`Connected → Uncertain → Off?` across every gap, dragged `MC_Secondary_Connected`
OFF, and when a gap exceeded the 30-min reminder grace it fired a **false
"Victron lader ikke tilsluttet"** email even though the clamps were on the bike.

**Fix:** a current pulse is HARD proof of a closed circuit (clamps on), so the
proof does not need frequent renewal. The daemon remembers the timestamp of the
last cycle in which current was seen and holds the `Connected` verdict for up to
`CURRENT_MEMORY_SEC` (default **10800 s = 3 h**, 4 h until 2026-10-16), which bridges
most Storage pulse gaps; a longer gap is bridged by the prick (below).

**Why a long window is safe for drain detection:**

| Case | Behaviour |
|------|-----------|
| Never plugged in | Charger emits **no** pulse → memory never starts → reminder still fires in 30 min (unchanged) |
| Clamps on, sparse Storage pulses | One pulse latches `Connected` for hours → **no false reminder** |
| Clamps unplugged mid-session | Caught when the window ages out (hours) — fine for battery health, still inside overnight |

The window only relaxes the **already-proven-connected** case, never the
dangerous never-connected case. A voltage-delta discriminator was rejected
(team A/B test 2026-08-09: it false-positives at a full battery, where the
charger open-circuit voltage ≈ resting battery voltage). The memory is cleared
when the charger goes offline (mains lost).

### Startup grace (`STARTUP_GRACE_SEC`, added 2026-09-01)

After a daemon or Pi restart the pulse memory is empty. Because deep-Storage
pulse gaps can be 45+ min, a restart landing mid-gap would flip
`MC_Secondary_Connected` OFF until the next pulse and risk a false reminder.
For the first `STARTUP_GRACE_SEC` (default **3600 s = 1 h**) the daemon reports
`Connected (startup grace)` while the charger is online but no pulse has yet
been seen. This makes routine restarts seamless, is bounded to ~1 h so a genuine
clamps-off at restart is still caught soon after (comparable to the 30-min
reminder grace), and never masks a mains loss — the offline path reports
`Offline` before the grace is reached.

### The prick (added 2026-10-14)

**Problem:** the memory only bridges gaps. When a full battery sits in Storage
for more than `CURRENT_MEMORY_SEC` without a pulse, the verdict falls to
`Uncertain`/`Off?` with the clamps still on, and the reminder fires falsely
("Springfield — Husk Laderen!"). Every proof above is passive, and with 230 V
always on nothing ever happens to measure.

**Fix:** make something happen. The charger cannot be switched off over BLE,
but its charge preset can be set (register `0xEDF1`, see "Writing" above), and
Normal -> High -> Normal restarts the cycle. A battery on the clamps then takes
100-400 mA even when full, which the next cycles see as `current seen`. With the
clamps off there is no circuit and no current.

**When the daemon pricks:**

| Condition | Why |
|-----------|-----|
| Verdict is `Uncertain` or `Off?` | Only when in doubt — a `Connected` verdict needs no prick |
| Charger state is **Storage** | From Absorption (14.4-14.7 V) a prick cannot raise the voltage, so a full battery takes nothing and "no current" proves nothing. Seen on the first manual test: the clamps were on, the charger was in Absorption, and the result wrongly said "clamps off" |
| No proof and no backoff running | A prick that saw current holds `Connected (prick Nm ago)` for 3 h, so the next one comes when that runs out - at most eight a day, each a short extra Absorption for a full battery (the charger does one a week by itself). A prick that saw **no** current, or failed, backs off 12 h so a charger without a battery is not pricked every cycle |
| `MC_Charger_Prick` ON | A manual prick, any state — the result says "not conclusive" unless it was made from Storage |

**Safety:** the prick reads the preset first and leaves the charger alone
unless it reads **Normal**. It writes High, reads it back, writes Normal, reads
it back, and writes Normal again if the read-back is anything else (reported as
`ALERT` in the result). Recondition and Li-ion are never written. One short BLE
connection (the charger drops links after ~12 s; keepalive every 1 s).

**Why the hold and the prick interval are one number (2026-10-15):** the first version remembered a
prick's current only for the 4 h pulse memory but waited 12 h before the next
prick, so the verdict fell to Off at about 08:00 after a 04:00 prick, with the
clamps on, and a false reminder went out at 11:04. The trade-off of the hold: a
clamp taken off soon after a successful prick, without the bike leaving home, is
only noticed when the hold runs out (riding away is noticed at once - the bike is
no longer home).

**3 h since 2026-10-16** (it was 12 h): with 12 h a clamp taken off just after a
successful prick took up to 12.5 h to report. Now the pulse memory and the proof
hold are both 3 h, so the worst case is about 3.5 h: 3 h hold, the prick and its
10 min, then the reminder's 30 min grace. The price is up to eight short
Absorptions a day instead of two on a full battery.

**Results** in `MC_Charger_Prick_Result`:

| Result | Meaning |
|--------|---------|
| `… pricked (auto, from Storage) - watching for current` | Done, waiting up to `PRICK_EVAL_SEC` |
| `… current seen - clamps on the bike` | Proof D turns `Connected (current seen)`, then holds `Connected (prick Nm ago)` for 3 h — reminder cancelled |
| `… no current after the prick - clamps off` | From Storage: the clamps are off, the reminder is right |
| `… no current - not conclusive, charger was in X` | A manual prick from another state |
| `… skipped - preset reads N, not Normal; left alone` | Someone set another preset; the daemon does not touch it |
| `… ALERT - preset reads N after the prick, not Normal` | Check the charger in VictronConnect |

## BLE Services Reference

Full service enumeration of the Blue Smart IP65 12/10:

| Service UUID | Description | Notes |
|---|---|---|
| `00001800` | Generic Access | Device name: "Springfield Charger" |
| `00001801` | Generic Attribute | Standard BLE |
| `306b0001` | Victron Data | Main data service (3 characteristics) |
| `68c10001` | Unknown | Write-no-response + write/notify pair |
| `97580001` | Device Info | Contains model ID (0x01A3), firmware info |

## Test Results (2026-03-12)

| Test | Expected | Result |
|------|----------|--------|
| Battery disconnect | Current → 0, State → Idle | **PASS** |
| Battery reconnect | Current resumes, State → Float | **PASS** |
| Different battery | Correct charge phase detected | **PASS** (Absorption at 14.4V) |
| Charger LED vs daemon | States match | **PASS** (Float = green LED) |
| Charger mains disconnect | BLE → OFF after 3 failures | **PASS** (~90 sec) |
| Items cleared on offline | State=Off, V=0, I=0 | **PASS** |
| Charger mains reconnect | BLE → ON, data resumes | **PASS** |
| Full charge cycle | Bulk → Absorption → Float | **PASS** |

## Files

```
victron-ble/
├── README.md                       # This file
├── victron_ble_monitor.py          # Daemon source (reference copy)
├── victron-ble-monitor.service     # Systemd service file
├── requirements.txt                # Python dependencies
└── btsnoop_writes.py               # Lists an app's GATT writes from an Android HCI snoop log

# Deployed on Pi (192.168.1.60):
/home/pi/victron-ble/
├── victron_ble_monitor.py          # Active daemon
└── .venv/                          # Python virtual environment

# openHAB:
items/motorcycle_k7_power.items     # Charger items (MC_Charger_*)
sitemaps/myhouse.sitemap            # Victron Charger frame
```

## Troubleshooting

**BLE connection fails ("Device not found")**
- Charger must be plugged into mains (BLE radio only active when powered)
- Check pairing: `bluetoothctl info AA:BB:CC:DD:EE:FF` → should show "Paired: yes, Bonded: yes"
- Re-pair if needed (see BLE Pairing section)

**Voltage not received (V=0.00V)**
- Voltage registers arrive late in the data stream (~10-12 seconds)
- Ensure `DATA_COLLECT_TIME` is at least 12 seconds
- The cache preserves last-known voltage between polls

**State shows "Unknown"**
- Occurs when no voltage data has been received yet (first poll after restart)
- Should resolve after 1-2 poll cycles

**The prick never runs**
- It needs the verdict `Uncertain`/`Off?` AND the charger in Storage AND no 3 h proof hold or 12 h backoff running
- Right after a restart the verdict is `Connected (startup grace)` for 1 h — no prick
- Press "Probe charger now" (`MC_Charger_Prick`) to prick by hand; watch `journalctl -u victron-ble-monitor | grep Prick`

**Charger shows "Bulk" but LED says "Float"**
- Check that charge profile setpoints match your charger configuration
- Default: Absorption=14.4V, Float=13.8V, Storage=13.2V
- Adjust class constants `V_ABSORPTION`, `V_FLOAT`, `V_STORAGE` if using a custom charge profile

## K7 Auto-Dump Integration (Active)

The BLE charger data is the **primary sensor** for the INNOVV K7 auto-dump state machine (`automation/js/vehicle-motorcycle-k7-power.js`). When the charger is connected and actively charging, the K7 dashcam is automatically powered on, footage is dumped to NAS, then the K7 is powered off.

### Dual-Sensor Architecture

| Sensor | Role | Source |
|--------|------|--------|
| **BLE charger state** | Primary authority | This daemon → `MC_Charger_BLE_Online` + `MC_Charger_State` |
| **Battery voltage** | Fallback (when BLE offline) | The GPS tracker's reading of the bike battery → `Vehicle10_Power` (the Shelly Plus Uni ADC until 2026-09-12; it now measures the relay coil's current, `MC_K7_Shelly_Voltage`) |

### Key Integration Points

- **`MC_Charger_BLE_Online`** — Triggers Rule 8 (BLE Charger Online). BLE ON + charging → start dump sequence. BLE OFF → re-arm from DUMP_DONE.
- **`MC_Charger_State`** — Triggers Rule 9 (BLE Charge State). Off → re-arm to PARKED. Bulk/Absorption/Float/Storage/Idle → start dump sequence from PARKED (full battery may go straight to Storage).
- **`MC_Charger_Connection`** — Computed by Rule 10 from BLE Online + State. Displayed in sitemap.
- **Stabilisation authority** — During 60s stabilisation, BLE online + `isBLEConnected()` (charging OR Storage/Idle) = confirm. BLE online + not connected = reject (false positive from voltage spike).
- **Three detection tiers** — `isBLECharging()` (active) → `isBLEConnected()` (Storage/Idle included) → voltage fallback. Used in post-ignition check, `startChargerSequence`, and stabilisation.
- **Grace period** — After re-arm to PARKED, voltage-only charger detection suppressed for 5 minutes (battery voltage lingers > 13.0V). BLE always overrides.
- **Clamps on the bike** — the tiers above say whether the charger is on mains and charging; whether its clamps are on the battery is `MC_Secondary_Connected`, from Proof D and the prick (see above).

### MC_Charger_Connection States

| BLE Online | BLE State | Connection String |
|------------|-----------|-------------------|
| OFF | — | `Offline` |
| ON | Idle / Off | `Standby (cable detached)` |
| ON | Bulk | `Charging — Bulk` |
| ON | Absorption | `Charging — Absorption` |
| ON | Float | `Charging — Float` |
| ON | Storage | `Charging — Storage` |

See `innovv-k7/K7_AUTO_POWER_README.md` for full state machine documentation.

## Credits

**Author:** Nanna Agesen ([@Prinsessen](https://github.com/Prinsessen)) — Nanna@agesen.dk

**BLE Protocol Reference:** [Olen](https://github.com/Olen) — Victron BLE GATT protocol reverse engineering via [VictronConnect / phoenix.py](https://github.com/Olen/solar-monitor). The init sequence, register map, and data packet format used in this daemon are based on Olen's work.

**Charge-preset write (`0xEDF1`) and the prick:** found by the author with an Android HCI snoop
of VictronConnect, 2026-10-14 (see "How the preset command was found").

## License

This project is provided as-is for personal and educational use.
