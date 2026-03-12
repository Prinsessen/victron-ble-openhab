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
from bleak import BleakClient, BleakError

# ── Configuration ────────────────────────────────────────────────────────────
CHARGER_ADDR = "EB:A8:21:DD:9C:A0"
OPENHAB_URL = "http://10.0.5.21:8080/rest/items"
POLL_INTERVAL = 30          # seconds between connection cycles
CONNECT_TIMEOUT = 10        # BLE connect timeout
DATA_COLLECT_TIME = 12      # seconds to collect data after init (voltage arrives late)
KEEPALIVE_INTERVAL = 1.5    # seconds between keepalives during collection
OFFLINE_THRESHOLD = 3       # consecutive failures before marking offline
MAX_RETRIES = 2             # retries per poll cycle

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

        Current level does NOT determine phase — the charger can push
        several amps at Float voltage if the battery demands it.

        Voltage stability (history) distinguishes Bulk (voltage rising through
        a setpoint zone) from actually being AT a setpoint.
        """
        if voltage is None:
            return "Unknown"

        # No current flowing = Idle (battery disconnected or fully charged)
        if current_ma is not None and current_ma < 50:
            return "Idle"

        # Reconditioning / Equalization: voltage pushed very high
        if voltage >= self.V_RECONDITION:
            return "Recondition"

        # Check if voltage is stable (not rising through a zone during Bulk)
        is_stable = self._is_voltage_stable()

        # Match voltage to nearest setpoint using tolerance bands
        # Check from highest setpoint downward
        if abs(voltage - self.V_ABSORPTION) <= self.V_SETPOINT_TOL:
            # In absorption zone (14.1 - 14.7V)
            return "Absorption"
        elif abs(voltage - self.V_FLOAT) <= self.V_SETPOINT_TOL:
            # In float zone (13.5 - 14.1V)
            if is_stable:
                return "Float"
            else:
                # Voltage still rising through this zone → Bulk
                return "Bulk"
        elif abs(voltage - self.V_STORAGE) <= self.V_SETPOINT_TOL:
            # In storage zone (12.9 - 13.5V)
            if is_stable:
                return "Storage"
            else:
                return "Bulk"
        elif voltage < self.V_STORAGE - self.V_SETPOINT_TOL:
            # Below all setpoints — battery is being charged up
            return "Bulk"
        else:
            # Fallback for gaps between zones
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

    # ── Single poll cycle ────────────────────────────────────────────────
    async def _poll_once(self) -> bool:
        """Connect, collect data, parse, return True on success."""
        self._data_buffer = bytearray()
        self._disconnected = False

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
                self._cache["state"] = "Off"
                self._cache["current_a"] = 0.0
                self._cache["current_ma"] = 0
                self._cache["voltage"] = None
                self._voltage_history.clear()

    # ── Main loop ────────────────────────────────────────────────────────
    async def run(self):
        """Main polling loop."""
        log.info("Victron BLE Monitor starting — charger %s", CHARGER_ADDR)
        log.info("openHAB: %s  poll interval: %ds", OPENHAB_URL, POLL_INTERVAL)

        while self.running:
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
