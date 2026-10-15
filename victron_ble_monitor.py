#!/usr/bin/env python3
"""
Victron Blue Smart IP65 12/10 BLE Monitor Daemon
=================================================
Connects to Victron charger via BLE GATT (306b service), reads battery data,
and posts values to openHAB REST API.

Charger BLE is only active when mains-powered → presence = charging.

Protocol (from Olen/VictronConnect phoenix.py):
  - 306b0002: Init writes + keepalive (write-without-response + notify)
  - 306b0003: Data commands + individual data (write-without-response + notify)
  - 306b0004: Bulk data (write-without-response + notify)

Data packet format: 08 00 19 [reg_hi] [reg_lo] [dtype] [value...]
  dtype: 0x42=uint16(2B), 0x44=uint32(4B), 0x58=other16(2B), 0x41=uint8(1B)

Known registers for Blue Smart IP65 12/10:
  0xED8D: Battery Voltage      (u16, *0.01 = V)
  0xED8C: Battery Current      (u32, raw mA, signed)
  0xED8F: Battery Current      (u16, *0.1 = A)
  0xEDD5: Battery Voltage 2    (u16, *0.01 = V)
  0xEDDB: Yield Today          (u16, *0.01 = kWh)
  0x0120: Timer/Counter        (u32, seconds)

State derivation: The charger's native state register (0xEDD4) is NOT available
over BLE GATT on this model. State is derived by matching the measured voltage
to the charger's known voltage setpoints (matching VictronConnect app behavior):
  Absorption: 14.4V ± 0.3V  →  voltage held at absorption setpoint
  Float:      13.8V ± 0.25V →  voltage held at float setpoint
  Storage:    13.2V ± 0.25V →  voltage reduced to storage level
  Bulk:       voltage below setpoints, current flowing (battery charging up)
  Recondition: voltage > 15.0V (desulfation mode)

Deployment: /home/pi/victron-ble/victron_ble_monitor.py
Venv: /home/pi/victron-ble/.venv (bleak, aiohttp)
Service: victron-ble-monitor.service
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Optional

import aiohttp
from bleak import BleakClient, BleakError, BleakScanner

# ── Configuration ────────────────────────────────────────────────────────────
CHARGER_ADDR = "AA:BB:CC:DD:EE:FF"
OPENHAB_URL = "http://192.168.1.21:8080/rest/items"
POLL_INTERVAL = 30          # seconds between connection cycles
CONNECT_TIMEOUT = 10        # BLE connect timeout
DATA_COLLECT_TIME = 12      # seconds to collect data after init (voltage arrives late)
KEEPALIVE_INTERVAL = 1.5    # seconds between keepalives during collection
OFFLINE_THRESHOLD = 3       # consecutive failures before marking offline
# Proof D current-pulse memory: how long a single "current seen" pulse keeps
# proving the clamps are on the bike. A current pulse is HARD proof of a closed
# circuit (clamps on). In Storage on a FULL battery the charger emits a visible
# maintenance-current pulse only rarely — gaps of 30+ min were observed live on
# 2026-09-01, and can be longer the fuller the battery is. Without a long-
# enough window Proof D flaps Connected->Uncertain->Off? across those gaps,
# drags MC_Secondary_Connected OFF and fires a false "charger not connected"
# reminder. 4 h comfortably bridges any realistic Storage gap.
#
# Why a long window is SAFE for drain detection:
#   - "Never plugged in" produces NO pulse at all -> memory never starts ->
#     the reminder still fires promptly (30-min grace). Unaffected.
#   - "Clamps unplugged mid-session" is caught when the window ages out (hours),
#     which is fine for battery health and still well inside an overnight window.
# The window only relaxes the already-proven-connected case, never the
# dangerous never-connected case.
CURRENT_MEMORY_SEC = 14400  # 4 h: hold "Connected" across deep-Storage pulse gaps
# Startup grace: after a daemon/Pi restart the pulse memory is empty, so a
# restart that lands mid-gap (Storage pulses can be 45+ min apart) would flip
# MC_Secondary_Connected OFF until the next pulse and risk a false reminder.
# For the first STARTUP_GRACE_SEC we optimistically assume "connected" so a
# routine restart/reboot is seamless. Bounded to ~1 h so a genuine clamps-off
# at restart is still caught soon after — comparable to the 30-min reminder
# grace, and negligible against the overnight timescale the feature protects.
# Does NOT apply when the charger is offline (no mains): that path reports
# "Offline" directly and never reaches the grace.
STARTUP_GRACE_SEC = 3600    # 1 h optimistic "connected" grace after a restart
MAX_RETRIES = 2             # retries per poll cycle

# ── The prick (2026-10-14) ──────────────────────────────────────────────────
# A full battery in Storage takes no current, so Proof D cannot tell clamps-on
# from clamps-off once CURRENT_MEMORY_SEC has run out. The charger cannot be
# switched off over BLE (0x0200 is ignored), but its charge PRESET can be set
# (register 0xEDF1, bytes captured from VictronConnect with an HCI snoop), and
# Normal -> High -> Normal restarts the charge algorithm: Storage -> Bulk ->
# Absorption. A battery on the clamps then takes 100-400 mA even when full
# (proven 2026-10-14); with the clamps off nothing can flow. So when the verdict
# drops to Off?/Uncertain, prick once and let the next cycles' current decide.
# ONLY FROM STORAGE: in Absorption (14.4-14.7 V) a prick cannot raise the voltage,
# so a full battery takes nothing and "no current" proves nothing - seen on the
# first manual test, 2026-10-14 21:05. Automatic pricks wait for Storage; a
# manual one from another state reports "not conclusive" instead of "clamps off".
# Only ever touches the charger when the preset reads Normal, and always ends on
# Normal, read back. Recondition and Li-ion are never written.
PRESET_MARK = bytes.fromhex("080019edf141")   # a 0xEDF1 value follows this
PRESET_NORMAL = 0x00
PRESET_HIGH = 0x02
CMD_PRESET_GET = "05008119edf1"
CMD_PRESET_HIGH = "06008219edf14102"
CMD_PRESET_NORMAL = "06008219edf14100"
PRICK_PROOF_HOLD_SEC = 12 * 3600    # a prick that saw current proves the clamps on for 12 h
PRICK_BACKOFF_SEC = 12 * 3600       # after a prick that saw NO current (or failed): no auto prick for 12 h
PRICK_EVAL_SEC = 600                # current must show within 10 min of a prick
PRICK_KEEPALIVE_SEC = 1.0           # the charger drops a link on slower keepalives

# BLE GATT UUIDs (Victron 306b service)
CHAR_INIT = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
CHAR_CMD  = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
CHAR_BULK = "306b0004-b081-4037-83dc-e59fcc3cdfd0"

# openHAB item names
ITEMS = {
    "online":      "MC_Charger_BLE_Online",
    "voltage":     "MC_Charger_Voltage",
    "current":     "MC_Charger_Current",
    "current_ma":  "MC_Charger_Current_mA",
    "yield_kwh":   "MC_Charger_Yield",
    "state":       "MC_Charger_State",
    "last_update": "MC_Charger_Last_Update",
    "ble_rssi":    "MC_Charger_BLE_RSSI",
    "ble_signal":  "MC_Charger_BLE_Signal",
    # Proof D — secondary-connection via BLE data richness (diagnostic mirror)
    "reg_count":   "MC_Charger_Reg_Count",
    "data_bytes":  "MC_Charger_Data_Bytes",
    "sec_proof":   "MC_Charger_Secondary_Proof",
    "prick":       "MC_Charger_Prick",           # ON = prick now (manual), daemon resets it
    "prick_result": "MC_Charger_Prick_Result",
}

# Victron register definitions: reg_id -> (name, dtype_expected, scale_fn)
REGISTERS = {
    0xED8D: ("voltage",    "u16", lambda v: round(v * 0.01, 2)),    # Volts
    0xED8C: ("current_ma", "u32", lambda v: v),                      # milliamps
    0xED8F: ("current",    "u16", lambda v: round(v * 0.1, 1)),     # Amps
    0xEDD5: ("voltage2",   "u16", lambda v: round(v * 0.01, 2)),    # Volts
    0xEDD7: ("current2",   "u16", lambda v: round(v * 0.1, 1)),     # Amps (charge current)
    0xEDDB: ("yield_kwh",  "u16", lambda v: round(v / 1000.0, 3)),  # raw Wh → kWh
    0x0120: ("counter",    "u32", lambda v: v),                      # seconds
}

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("victron-ble")


class VictronBLEMonitor:
    """Monitors Victron charger via BLE GATT and posts data to openHAB."""

    # Victron Blue Smart IP65 12/10 charge profile setpoints (12V lead-acid)
    # These match the factory default profile for standard lead-acid batteries.
    V_ABSORPTION = 14.4   # Absorption voltage setpoint
    V_FLOAT = 13.8        # Float voltage setpoint
    V_STORAGE = 13.2      # Storage voltage setpoint
    V_RECONDITION = 15.0  # Reconditioning/equalization threshold
    V_SETPOINT_TOL = 0.3  # Tolerance band for setpoint matching (±V)

    def __init__(self):
        self.running = True
        self.consecutive_failures = 0
        self.last_online_state: Optional[bool] = None
        self._data_buffer = bytearray()
        self._disconnected = False
        # Voltage history for trend detection (Bulk vs stable setpoint)
        self._voltage_history: list[float] = []
        # Cache last-known values so partial reads still post complete data
        self._cache = {
            "voltage": None,
            "current_a": None,
            "current_ma": None,
            "yield_kwh": None,
            "state": None,
        }
        # Proof D — rolling history of the last 3 successful cycles' data
        # richness (register count, byte count, whether any current register
        # was present). Empirically the clamp-on vs clamp-off discriminator.
        self._richness_history: list[dict] = []
        # Monotonic timestamp of the most recent cycle in which charge current
        # was seen. Powers the CURRENT_MEMORY_SEC window in Proof D so a full
        # battery's sparse Storage pulses don't flap the verdict (see below).
        self._last_current_seen_ts: Optional[float] = None
        # Process start time, for the STARTUP_GRACE_SEC optimistic-connected
        # window that keeps a restart from flapping MC_Secondary_Connected OFF
        # before the first post-restart current pulse arrives.
        self._startup_ts: float = time.monotonic()
        # The prick: why one is due ("auto"/"manual"), when the last one ran,
        # and until when the following cycles are watched for current.
        self._prick_pending: Optional[str] = None
        # 2026-10-15: one interval for everything left a gap - a prick's proof was
        # remembered 4 h (CURRENT_MEMORY_SEC) but the next prick waited 12 h, so the
        # verdict sat at Off from ~08 to 16 with the clamps on and a false reminder
        # went out at 11:04. Now a prick that saw current holds the verdict for
        # PRICK_PROOF_HOLD_SEC, and only a prick that saw nothing (or failed) backs
        # the automatic prick off; a manual prick never blocks the automatic one.
        self._prick_proven_ts: Optional[float] = None
        self._prick_backoff_until: float = 0.0
        self._prick_eval_until: Optional[float] = None
        self._prick_stamp = ""
        self._prick_from = "Unknown"   # charger state when the prick was made

    # ── BLE notification callbacks ───────────────────────────────────────
    def _on_notify(self, _sender, data: bytearray):
        """Collect all notification data into a single buffer."""
        self._data_buffer.extend(data)

    def _on_disconnect(self, _client):
        self._disconnected = True

    # ── Buffer parser ────────────────────────────────────────────────────
    def _parse_registers(self) -> dict:
        """Parse concatenated notification buffer for register data."""
        data = self._data_buffer
        results = {}
        i = 0
        while i < len(data):
            if i + 6 <= len(data) and data[i] == 0x08 and data[i + 2] == 0x19:
                reg = (data[i + 3] << 8) | data[i + 4]
                dtype = data[i + 5]

                if dtype == 0x42 and i + 8 <= len(data):  # uint16
                    val = data[i + 6] | (data[i + 7] << 8)
                    results[reg] = val
                    i += 8
                    continue
                elif dtype == 0x44 and i + 10 <= len(data):  # uint32
                    val = (data[i + 6] | (data[i + 7] << 8) |
                           (data[i + 8] << 16) | (data[i + 9] << 24))
                    results[reg] = val
                    i += 10
                    continue
                elif dtype == 0x58 and i + 8 <= len(data):  # other16
                    val = data[i + 6] | (data[i + 7] << 8)
                    results[reg] = val
                    i += 8
                    continue
                elif dtype == 0x41 and i + 7 <= len(data):  # uint8
                    val = data[i + 6]
                    results[reg] = val
                    i += 7
                    continue
            i += 1
        return results

    # ── Charger state derivation ─────────────────────────────────────────
    def _derive_charger_state(self, voltage: Optional[float],
                              current_ma: Optional[int]) -> str:
        """Derive charging state matching VictronConnect app behavior.

        The Blue Smart IP65 doesn't expose register 0xEDD4 (charger state)
        over BLE GATT. State is derived by matching the measured output voltage
        to the charger's known voltage setpoints, which is how the charger's
        internal state machine operates:

        The charger regulates to SPECIFIC voltage setpoints per phase:
          Absorption: 14.4V (held constant, current tapering)
          Float:      13.8V (held constant, any current level)
          Storage:    13.2V (reduced voltage, minimal current)
          Bulk:       below all setpoints, max current, voltage rising
          Recondition: >15.0V (desulfation pulse)

        CRITICAL: Voltage setpoint matching takes priority over current level.
        In Storage/Float modes with a full battery, the charger holds the
        setpoint voltage with near-zero current (<50mA). This is NOT idle —
        the charger is actively regulating. "Idle" only applies when voltage
        doesn't match any setpoint AND current is low.

        Voltage stability (history) distinguishes Bulk (voltage rising through
        a setpoint zone) from actually being AT a setpoint.
        """
        if voltage is None:
            return "Unknown"

        low_current = current_ma is not None and current_ma < 50

        # Reconditioning / Equalization: voltage pushed very high
        if voltage >= self.V_RECONDITION:
            return "Recondition"

        # Check if voltage is stable (not rising through a zone during Bulk)
        is_stable = self._is_voltage_stable()

        # Match voltage to nearest setpoint using tolerance bands.
        # Setpoint matching takes priority over current level — the charger
        # actively maintains voltage at these setpoints even with near-zero
        # current (e.g. Storage at 13.2V on a full battery draws <50mA).
        # Check from highest setpoint downward.
        if abs(voltage - self.V_ABSORPTION) <= self.V_SETPOINT_TOL:
            # In absorption zone (14.1 - 14.7V)
            return "Absorption"
        elif abs(voltage - self.V_FLOAT) <= self.V_SETPOINT_TOL:
            # In float zone (13.5 - 14.1V)
            if low_current:
                return "Float"  # Holding float setpoint with minimal current
            elif is_stable:
                return "Float"
            else:
                # Voltage still rising through this zone → Bulk
                return "Bulk"
        elif abs(voltage - self.V_STORAGE) <= self.V_SETPOINT_TOL:
            # In storage zone (12.9 - 13.5V)
            if low_current:
                return "Storage"  # Holding storage setpoint with minimal current
            elif is_stable:
                return "Storage"
            else:
                return "Bulk"
        elif voltage < self.V_STORAGE - self.V_SETPOINT_TOL:
            # Below all setpoints
            if low_current:
                return "Idle"  # No setpoint match + no current = truly idle
            return "Bulk"
        else:
            # Between setpoint zones
            if low_current:
                return "Idle"
            return "Bulk"

    def _is_voltage_stable(self) -> bool:
        """Check if voltage has been stable over recent readings.

        Returns True if we have enough history and the voltage spread
        is within 0.15V (charger is holding a setpoint, not ramping).
        """
        if len(self._voltage_history) < 3:
            # Not enough history yet — assume stable (conservative)
            return True
        recent = self._voltage_history[-5:]  # Last 5 readings (~2.5 minutes)
        return (max(recent) - min(recent)) < 0.15

    # ── Proof D: secondary connection via BLE data richness ──────────────
    def _derive_secondary_proof(self) -> str:
        """Infer secondary (DC clamp) connection from BLE data richness.

        Empirically validated on this charger (2026-08-09 clamp on/off A/B
        test). The charger has NO 'secondary connected' characteristic (Victron
        confirmed, and 0xEDD5/0xEDD7 proved to be plain mirrors of the main
        voltage/current). But the STREAM ITSELF changes with the clamp state,
        because with the clamps off there is no real battery to regulate:

          clamps ON  : 6-10 registers/cycle, 320-550 bytes, current registers present
          clamps OFF : 1-2 registers/cycle, 140-190 bytes, current registers ABSENT

        Single cycles are noisy (a connected full battery can briefly drop the
        current registers), so we judge over the last 3 cycles:
          - current seen in ANY of the last 3  -> Connected (hard: current flows)
          - else a pulse within CURRENT_MEMORY_SEC -> Connected (full battery in
            Storage pulses maintenance current only rarely; 30+ min gaps seen)
          - all 3 sparse (<=3 regs) with no current + no recent pulse -> Off
          - otherwise -> Uncertain (settling / ambiguous)

        CONSUMED BY THE JS RULE: this verdict is the PRIMARY input to
        isSecondaryConnected() in vehicle-motorcycle-k7-power.js. "Connected
        (current seen)" is treated as hard proof the clamps are on the bike.
        """
        h = self._richness_history
        if not h:
            return "Unknown"
        # Fast path: current in any of the last 3 cycles -> hard proof, on now.
        if any(c["cur"] for c in h):
            return "Connected (current seen)"
        # Memory path: a recent current pulse still proves the clamps are on.
        # In Storage on a full battery the charger pulses maintenance current
        # only rarely (30+ min gaps observed, longer when very full), leaving
        # long genuine-zero stretches. Hold "Connected" across them (up to
        # CURRENT_MEMORY_SEC) so MC_Secondary_Connected stays stable and no
        # false "charger disconnected" reminder fires. A truly disconnected
        # charger has NO pulses, so this never starts and the real clamps-off
        # case is still caught.
        if self._last_current_seen_ts is not None:
            age = time.monotonic() - self._last_current_seen_ts
            if age <= CURRENT_MEMORY_SEC:
                return f"Connected (pulse {int(age // 60)}m ago)"
        # Startup grace: right after a restart the pulse memory is empty. Rather
        # than flap OFF until the (possibly 45+ min away) next pulse, assume
        # connected for STARTUP_GRACE_SEC. Only reached when the charger is
        # online (offline is handled earlier), so this never masks a mains loss.
        if self._prick_proven_ts is not None:
            age = time.monotonic() - self._prick_proven_ts
            if age <= PRICK_PROOF_HOLD_SEC:
                return f"Connected (prick {int(age // 60)}m ago)"
        if (time.monotonic() - self._startup_ts) <= STARTUP_GRACE_SEC:
            return "Connected (startup grace)"
        if len(h) >= 3 and all(c["regs"] <= 3 for c in h):
            return "Off? (sparse data \u2014 clamps off)"
        return "Uncertain (settling)"

    # ── The prick ─────────────────────────────────────────────────────────
    async def _judge_prick(self, proof: str, has_current: bool):
        """After a prick: did current come? Otherwise: is a prick due?"""
        now = time.monotonic()
        if self._prick_eval_until is not None:
            if has_current:
                self._prick_eval_until = None
                self._prick_proven_ts = now
                await self._prick_result(f"{self._prick_stamp} current seen - clamps on the bike")
            elif now > self._prick_eval_until:
                self._prick_eval_until = None
                if self._prick_from == "Storage":
                    self._prick_backoff_until = now + PRICK_BACKOFF_SEC
                    await self._prick_result(f"{self._prick_stamp} no current after the prick - clamps off")
                else:
                    await self._prick_result(f"{self._prick_stamp} no current - not conclusive, "
                                             f"charger was in {self._prick_from} (only a prick from Storage decides)")
            return
        if not (proof.startswith("Off") or proof.startswith("Uncertain")):
            return
        if self._cache.get("state") != "Storage":
            return
        if now < self._prick_backoff_until:
            return
        self._prick_pending = self._prick_pending or "auto"

    async def _prick_result(self, text: str):
        log.info("Prick: %s", text)
        await self._post_to_openhab(ITEMS["prick_result"], text)

    async def _check_manual_prick(self):
        """MC_Charger_Prick ON from openHAB = prick on this cycle."""
        url = f"{OPENHAB_URL}/{ITEMS['prick']}/state"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    state = (await resp.text()).strip() if resp.status == 200 else ""
        except Exception:
            return
        if state == "ON":
            self._prick_pending = "manual"
            await self._post_to_openhab(ITEMS["prick"], "OFF")

    async def _prick(self):
        """Preset Normal -> High -> Normal in one short connection, each step read back."""
        why, self._prick_pending = self._prick_pending, None
        self._prick_stamp = time.strftime("%d/%m %H:%M")
        self._prick_from = self._cache.get("state") or "Unknown"
        stamp = self._prick_stamp
        buf = bytearray()

        def note(_sender, data: bytearray):
            buf.extend(data)

        def preset() -> Optional[int]:
            i = buf.rfind(PRESET_MARK)
            return buf[i + len(PRESET_MARK)] if i >= 0 and i + len(PRESET_MARK) < len(buf) else None

        client = BleakClient(CHARGER_ADDR, timeout=CONNECT_TIMEOUT)

        async def send(cmd: str, hold: float):
            await client.write_gatt_char(CHAR_CMD, bytes.fromhex(cmd), response=False)
            end = time.monotonic() + hold
            while time.monotonic() < end:
                await client.write_gatt_char(CHAR_INIT, bytes.fromhex("f941"), response=False)
                await asyncio.sleep(PRICK_KEEPALIVE_SEC)

        high_sent = False
        result = f"{stamp} prick failed"
        try:
            await client.connect()
            for char_uuid in (CHAR_INIT, CHAR_CMD, CHAR_BULK):
                await client.start_notify(char_uuid, note)
            for cmd in ("fa80ff", "f980", "01"):
                await client.write_gatt_char(CHAR_INIT, bytes.fromhex(cmd), response=False)
                await asyncio.sleep(0.3)
            await client.write_gatt_char(CHAR_CMD, bytes.fromhex("01"), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(CHAR_CMD, bytes.fromhex("0300"), response=False)
            await asyncio.sleep(1.0)

            buf.clear()
            await send(CMD_PRESET_GET, 1.0)
            before = preset()
            if before != PRESET_NORMAL:
                self._prick_backoff_until = time.monotonic() + PRICK_BACKOFF_SEC
                result = f"{stamp} skipped - preset reads {before}, not Normal; left alone"
                return
            buf.clear()
            high_sent = True
            await send(CMD_PRESET_HIGH, 0.5)
            await send(CMD_PRESET_GET, 2.5)
            high = preset()
            buf.clear()
            await send(CMD_PRESET_NORMAL, 0.5)
            await send(CMD_PRESET_GET, 1.5)
            after = preset()
            if after != PRESET_NORMAL:
                buf.clear()
                await send(CMD_PRESET_NORMAL, 0.5)
                await send(CMD_PRESET_GET, 1.5)
                after = preset()
            if after != PRESET_NORMAL:
                self._prick_backoff_until = time.monotonic() + PRICK_BACKOFF_SEC
                result = f"{stamp} ALERT - preset reads {after} after the prick, not Normal"
                log.error("Prick: preset not back on Normal (%s)", after)
            elif high != PRESET_HIGH:
                self._prick_backoff_until = time.monotonic() + PRICK_BACKOFF_SEC
                result = f"{stamp} High not confirmed (read {high}); preset Normal"
            else:
                self._prick_eval_until = time.monotonic() + PRICK_EVAL_SEC
                result = f"{stamp} pricked ({why}, from {self._prick_from}) - watching for current"
        except Exception as e:
            self._prick_backoff_until = time.monotonic() + PRICK_BACKOFF_SEC
            result = f"{stamp} prick failed: {e}"
            if high_sent and client.is_connected:
                try:
                    await send(CMD_PRESET_NORMAL, 1.0)
                except Exception:
                    pass
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            await self._prick_result(result)

    # ── openHAB REST API ─────────────────────────────────────────────────
    async def _post_to_openhab(self, item: str, value: str):
        """Post a state update to openHAB REST API."""
        url = f"{OPENHAB_URL}/{item}/state"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    url,
                    data=value,
                    headers={"Content-Type": "text/plain"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status not in (200, 202):
                        log.warning("PUT %s=%s → HTTP %d", item, value, resp.status)
        except Exception as e:
            log.warning("PUT %s failed: %s", item, e)

    async def _post_command(self, item: str, value: str):
        """Send a command to openHAB item."""
        url = f"{OPENHAB_URL}/{item}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=value,
                    headers={"Content-Type": "text/plain"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status not in (200, 202):
                        log.warning("CMD %s=%s → HTTP %d", item, value, resp.status)
        except Exception as e:
            log.warning("CMD %s failed: %s", item, e)

    # ── BLE signal quality ────────────────────────────────────────────────
    @staticmethod
    def _rssi_to_quality(dbm: int) -> str:
        """Convert RSSI dBm to human-readable quality string."""
        if dbm >= -60:
            quality = "Excellent"
        elif dbm >= -70:
            quality = "Good"
        elif dbm >= -80:
            quality = "Fair"
        elif dbm >= -90:
            quality = "Weak"
        else:
            quality = "Very Weak"
        return f"{quality} ({dbm} dBm)"

    # ── BLE RSSI scan ────────────────────────────────────────────────────
    async def _scan_rssi(self) -> Optional[int]:
        """Quick BLE scan to get RSSI for the charger.

        Returns RSSI in dBm or None if charger not found.
        Uses return_adv=True to get AdvertisementData which holds RSSI
        (BLEDevice alone doesn't carry RSSI in bleak 2.x).
        """
        try:
            devices = await BleakScanner.discover(
                timeout=5.0, return_adv=True
            )
            for addr, (dev, adv) in devices.items():
                if addr.upper() == CHARGER_ADDR.upper():
                    log.info("BLE RSSI: %d dBm (charger %s)", adv.rssi, addr)
                    return adv.rssi
            log.debug("Charger not found in BLE scan")
        except Exception as e:
            log.warning("BLE RSSI scan failed: %s", e)
        return None

    # ── Single poll cycle ────────────────────────────────────────────────
    async def _poll_once(self) -> bool:
        """Connect, collect data, parse, return True on success."""
        self._data_buffer = bytearray()
        self._disconnected = False

        # Scan for RSSI before connecting (scan and GATT can't overlap)
        rssi = await self._scan_rssi()
        if rssi is not None:
            await self._post_to_openhab(ITEMS["ble_rssi"], str(rssi))
            await self._post_to_openhab(
                ITEMS["ble_signal"], self._rssi_to_quality(rssi)
            )

        client = BleakClient(
            CHARGER_ADDR,
            timeout=CONNECT_TIMEOUT,
            disconnected_callback=self._on_disconnect,
        )

        try:
            await client.connect()
            if not client.is_connected:
                log.warning("Connect returned but not connected")
                return False
            log.info("Connected to charger")

            # Subscribe to all 3 characteristics
            for char_uuid in [CHAR_INIT, CHAR_CMD, CHAR_BULK]:
                await client.start_notify(char_uuid, self._on_notify)

            # Init sequence on 0x0002
            for cmd in ["fa80ff", "f980", "01"]:
                await client.write_gatt_char(
                    CHAR_INIT, bytes.fromhex(cmd), response=False
                )
                await asyncio.sleep(0.3)

            # Data commands on 0x0003
            await client.write_gatt_char(
                CHAR_CMD, bytes.fromhex("01"), response=False
            )
            await asyncio.sleep(0.2)
            await client.write_gatt_char(
                CHAR_CMD, bytes.fromhex("0300"), response=False
            )
            await asyncio.sleep(1.5)

            # Subscribe command
            await client.write_gatt_char(
                CHAR_CMD,
                bytes.fromhex("060082189342102703010303"),
                response=False,
            )
            await asyncio.sleep(1.5)

            # Keepalive loop to collect data
            elapsed = 0.0
            while elapsed < DATA_COLLECT_TIME and not self._disconnected:
                try:
                    await client.write_gatt_char(
                        CHAR_INIT, bytes.fromhex("f941"), response=False
                    )
                except (BleakError, Exception):
                    break
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                elapsed += KEEPALIVE_INTERVAL

            return True

        except BleakError as e:
            log.warning("BLE error: %s", e)
            return False
        except Exception as e:
            log.warning("Unexpected error: %s", e)
            return False
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

    # ── Process and post results ─────────────────────────────────────────
    async def _process_results(self):
        """Parse collected data and post to openHAB."""
        raw_regs = self._parse_registers()

        # ── Proof D: secondary-connection via BLE data richness ──────────
        # Computed BEFORE the empty-register early-return so a fully sparse
        # cycle (the clamps-off signature) still updates the diagnostic items.
        reg_count = len(raw_regs)
        data_bytes = len(self._data_buffer)
        has_current = any(r in raw_regs for r in (0xED8C, 0xED8F, 0xEDD7))
        if has_current:
            self._last_current_seen_ts = time.monotonic()
        self._richness_history.append(
            {"regs": reg_count, "bytes": data_bytes, "cur": has_current}
        )
        if len(self._richness_history) > 3:
            self._richness_history.pop(0)
        proof = self._derive_secondary_proof()
        await self._post_to_openhab(ITEMS["reg_count"], str(reg_count))
        await self._post_to_openhab(ITEMS["data_bytes"], str(data_bytes))
        await self._post_to_openhab(ITEMS["sec_proof"], proof)
        log.info("Proof D: regs=%d bytes=%d cur=%s -> %s",
                 reg_count, data_bytes, has_current, proof)
        await self._judge_prick(proof, has_current)

        if not raw_regs:
            log.warning("No register data parsed from %d bytes",
                        len(self._data_buffer))
            return

        # Extract scaled values
        voltage = None
        current_ma = None
        current_a = None
        yield_kwh = None

        for reg_id, raw_val in raw_regs.items():
            if reg_id in REGISTERS:
                name, _, scale_fn = REGISTERS[reg_id]
                scaled = scale_fn(raw_val)

                if name == "voltage":
                    voltage = scaled
                elif name == "voltage2" and voltage is None:
                    voltage = scaled   # Use EDD5 as fallback for ED8D
                elif name == "current_ma":
                    current_ma = raw_val
                elif name in ("current", "current2"):
                    if current_a is None:
                        current_a = scaled
                elif name == "yield_kwh":
                    yield_kwh = scaled

        # Cross-derive current values when one is missing
        if current_a is None and current_ma is not None:
            current_a = round(current_ma / 1000.0, 2)
        elif current_ma is None and current_a is not None:
            current_ma = int(current_a * 1000)

        # If charger sends data but NO current registers at all,
        # it means no battery load → force current to 0
        if raw_regs and current_a is None and current_ma is None:
            current_a = 0.0
            current_ma = 0

        # Update cache with fresh values, keep old for missing ones
        if voltage is not None:
            self._cache["voltage"] = voltage
            # Track voltage history for stability detection
            self._voltage_history.append(voltage)
            if len(self._voltage_history) > 10:
                self._voltage_history.pop(0)  # Keep last 10 readings (~5 min)
        if current_a is not None:
            self._cache["current_a"] = current_a
        if current_ma is not None:
            self._cache["current_ma"] = current_ma
        if yield_kwh is not None:
            self._cache["yield_kwh"] = yield_kwh

        # Use cached values for state derivation and posting
        v = self._cache["voltage"]
        i_ma = self._cache["current_ma"]
        i_a = self._cache["current_a"]
        y = self._cache["yield_kwh"]

        state = self._derive_charger_state(v, i_ma)
        if state != "Unknown":
            self._cache["state"] = state
        state = self._cache["state"] or "Unknown"

        log.info(
            "V=%.2fV  I=%smA (%.2fA)  Yield=%skWh  State=%s  regs=%d",
            v or 0,
            i_ma if i_ma is not None else "?",
            i_a or 0,
            y if y is not None else "?",
            state,
            len(raw_regs),
        )

        # Post all cached values to openHAB
        updates = []
        if v is not None:
            updates.append((ITEMS["voltage"], str(v)))
        if i_ma is not None:
            updates.append((ITEMS["current_ma"], str(i_ma)))
        if i_a is not None:
            updates.append((ITEMS["current"], str(i_a)))
        if y is not None:
            updates.append((ITEMS["yield_kwh"], str(y)))
        if state != "Unknown":
            updates.append((ITEMS["state"], state))

        # Timestamp in ISO format
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        updates.append((ITEMS["last_update"], now_iso))

        for item, value in updates:
            await self._post_to_openhab(item, value)

    # ── Online/offline management ────────────────────────────────────────
    async def _set_online(self, online: bool):
        """Update online state, only post if changed."""
        if online != self.last_online_state:
            self.last_online_state = online
            value = "ON" if online else "OFF"
            await self._post_command(ITEMS["online"], value)
            log.info("Charger BLE: %s", "ONLINE" if online else "OFFLINE")

            if not online:
                # Charger is off — clear all stale charging data
                await self._post_to_openhab(ITEMS["state"], "Off")
                await self._post_to_openhab(ITEMS["current"], "0")
                await self._post_to_openhab(ITEMS["current_ma"], "0")
                await self._post_to_openhab(ITEMS["voltage"], "0")
                await self._post_to_openhab(ITEMS["reg_count"], "0")
                await self._post_to_openhab(ITEMS["data_bytes"], "0")
                await self._post_to_openhab(ITEMS["sec_proof"], "Offline")
                self._cache["state"] = "Off"
                self._cache["current_a"] = 0.0
                self._cache["current_ma"] = 0
                self._cache["voltage"] = None
                self._voltage_history.clear()
                self._richness_history.clear()
                # Drop the pulse memory too: once mains is gone the previous
                # pulse no longer proves anything about the next session.
                self._last_current_seen_ts = None
                self._prick_proven_ts = None    # a proof from before a mains loss proves nothing now

    # ── Main loop ────────────────────────────────────────────────────────
    async def run(self):
        """Main polling loop."""
        log.info("Victron BLE Monitor starting — charger %s", CHARGER_ADDR)
        log.info("openHAB: %s  poll interval: %ds", OPENHAB_URL, POLL_INTERVAL)

        while self.running:
            await self._check_manual_prick()
            if self._prick_pending:
                await self._prick()
                await asyncio.sleep(3)

            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                if not self.running:
                    break
                if attempt > 1:
                    log.info("Retry %d/%d", attempt, MAX_RETRIES)
                    await asyncio.sleep(3)

                result = await self._poll_once()
                if result and len(self._data_buffer) > 10:
                    success = True
                    break
                elif result:
                    log.warning("Connected but got only %d bytes",
                                len(self._data_buffer))

            if success:
                self.consecutive_failures = 0
                await self._set_online(True)
                await self._process_results()
            else:
                self.consecutive_failures += 1
                log.warning("Poll failed (%d consecutive)",
                            self.consecutive_failures)
                if self.consecutive_failures >= OFFLINE_THRESHOLD:
                    await self._set_online(False)

            # Wait for next cycle
            if self.running:
                await asyncio.sleep(POLL_INTERVAL)

    def stop(self):
        self.running = False


def main():
    monitor = VictronBLEMonitor()

    def handle_signal(sig, frame):
        log.info("Signal %d received, shutting down...", sig)
        monitor.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        pass
    log.info("Victron BLE Monitor stopped")


if __name__ == "__main__":
    main()
