# Victron Blue Smart IP65 12/10 — BLE GATT Monitor

Standalone BLE monitor daemon for the **Victron Blue Smart IP65 12/10** battery charger. Runs on a Raspberry Pi 4, connects via BLE GATT to read charger data, and posts values to openHAB REST API.

## Hardware

| Component | Details |
|-----------|---------|
| Charger | Victron Blue Smart IP65 12/10 (12V, 10A) |
| BLE Address | `EB:A8:21:DD:9C:A0` |
| BLE Name | "Springfield Charger" |
| Manufacturer ID | `0x02E1` (Victron Energy) |
| Host | Raspberry Pi 4 (`10.0.5.60`, user `pi`) |
| BLE Adapter | hci0 (`B8:27:EB:79:D9:95`) |
| openHAB | `10.0.5.21:8080` |

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
# On Pi 4 (10.0.5.60)
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
  # Wait for EB:A8:21:DD:9C:A0 to appear
  pair EB:A8:21:DD:9C:A0
  # Enter passkey: 000000
  trust EB:A8:21:DD:9C:A0
  quit
```

### Deploy Daemon

```bash
# From openHAB host
scp -i ~/.ssh/id_ed25519 victron_ble_monitor.py pi@10.0.5.60:/home/pi/victron-ble/
scp -i ~/.ssh/id_ed25519 victron-ble-monitor.service pi@10.0.5.60:/tmp/

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

### Sitemap

Located in `sitemaps/myhouse.sitemap` under the "Victron Charger" frame, between INNOVV K7 and Romeo Robot sections.

## Configuration

All configuration is at the top of `victron_ble_monitor.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CHARGER_ADDR` | `EB:A8:21:DD:9C:A0` | BLE MAC address of charger |
| `OPENHAB_URL` | `http://10.0.5.21:8080/rest/items` | openHAB REST endpoint |
| `POLL_INTERVAL` | `30` | Seconds between BLE poll cycles |
| `DATA_COLLECT_TIME` | `12` | Seconds to collect data per connection |
| `OFFLINE_THRESHOLD` | `3` | Consecutive failures before marking offline |
| `MAX_RETRIES` | `2` | Retries per poll cycle |

### Charge Profile Setpoints

Factory default for 12V lead-acid (configurable as class constants):

| Setpoint | Voltage |
|----------|---------|
| Absorption | 14.4V |
| Float | 13.8V |
| Storage | 13.2V |
| Tolerance | ± 0.3V |

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

# Deployed on Pi (10.0.5.60):
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
- Check pairing: `bluetoothctl info EB:A8:21:DD:9C:A0` → should show "Paired: yes, Bonded: yes"
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
- **`MC_Charger_State`** — Triggers Rule 9 (BLE Charge State). Idle/Off → re-arm to PARKED. Bulk/Absorption/Float → start dump sequence.
- **`MC_Charger_Connection`** — Computed by Rule 10 from BLE Online + State. Displayed in sitemap.
- **Stabilisation authority** — During 60s stabilisation, BLE online + not charging = reject (false positive from voltage spike).
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
