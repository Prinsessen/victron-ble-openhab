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
detection cannot get from a full battery (see "Secondary connection" below). With the clamps
off, no current can flow. Always end on Normal and read `0xEDF1` back.

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
| `CURRENT_MEMORY_SEC` | `14400` | Proof D: hold "Connected" this long after a current pulse (4 h) |
| `STARTUP_GRACE_SEC` | `3600` | Proof D: optimistic "Connected" window after a restart (1 h) |

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
`CURRENT_MEMORY_SEC` (default **14400 s = 4 h**), which comfortably bridges any
realistic Storage pulse gap while staying well inside an overnight window.

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
└── requirements.txt                # Python dependencies

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
| **Shelly ADC voltage** | Fallback (when BLE offline) | Shelly Plus Uni Voltmeter:100 → `MC_K7_Shelly_Voltage` |

### Key Integration Points

- **`MC_Charger_BLE_Online`** — Triggers Rule 8 (BLE Charger Online). BLE ON + charging → start dump sequence. BLE OFF → re-arm from DUMP_DONE.
- **`MC_Charger_State`** — Triggers Rule 9 (BLE Charge State). Off → re-arm to PARKED. Bulk/Absorption/Float/Storage/Idle → start dump sequence from PARKED (full battery may go straight to Storage).
- **`MC_Charger_Connection`** — Computed by Rule 10 from BLE Online + State. Displayed in sitemap.
- **Stabilisation authority** — During 60s stabilisation, BLE online + `isBLEConnected()` (charging OR Storage/Idle) = confirm. BLE online + not connected = reject (false positive from voltage spike).
- **Three detection tiers** — `isBLECharging()` (active) → `isBLEConnected()` (Storage/Idle included) → voltage fallback. Used in post-ignition check, `startChargerSequence`, and stabilisation.
- **Grace period** — After re-arm to PARKED, voltage-only charger detection suppressed for 5 minutes (battery voltage lingers > 13.0V). BLE always overrides.

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

## License

This project is provided as-is for personal and educational use.
