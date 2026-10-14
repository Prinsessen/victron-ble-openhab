#!/usr/bin/env python3
"""List the GATT writes an app sent to a Victron charger, from an Android HCI snoop log.

How the charge-preset command (register 0xEDF1) was found, 2026-10-14:
  1. Android: Developer options -> "Enable Bluetooth HCI snoop log" on, Bluetooth off and on.
  2. Stop anything else holding the charger's BLE link (the Pi daemon), open VictronConnect,
     change the setting once, note the time.
  3. Developer options -> Bug report -> Full report; the log is FS/data/log/bt/btsnoop_hci.log
     inside the zip (the path varies by phone - look for btsnoop_hci.log).
  4. python3 btsnoop_writes.py btsnoop_hci.log

Prints every ATT write (Write Request 0x12 / Write Command 0x52) with its time, handle and
value, leaving out the connect sequence, the keepalive (f941), the app's periodic poll and
its register reads (05008119xxxx and the long batched chunks), so what is left is the
handful of commands the app actually sent.
A VE.Direct-over-BLE register write looks like 06 00 82 19 <reg lo> <reg hi> 41 <value>.

The log holds the phone's other Bluetooth traffic too - delete it when done, and do not
publish it.
"""
import datetime
import struct
import sys

BTSNOOP_EPOCH_US = 0x00DCDDB30F2F8000   # microseconds from year 0 to 1970-01-01
NOISE = ("f941", "0600821893421027")      # keepalive and the app's poll
# the connect sequence (CCCD enables, init on 306b0002, data commands on 306b0003)
CONNECT = ("0200", "00", "0000", "0100", "fa80ff", "f980", "01", "0300")
MAX_COMMAND_BYTES = 12   # the app's batched register reads arrive in 20-byte chunks; commands are short


def att_writes(path):
    data = open(path, "rb").read()
    if data[:8] != b"btsnoop\0":
        sys.exit(f"{path}: not a btsnoop file")
    off = 16
    while off + 24 <= len(data):
        _orig, incl, flags, _drops, ts = struct.unpack(">IIIIq", data[off:off + 24])
        pkt = data[off + 24:off + 24 + incl]
        off += 24 + incl
        # H4 ACL packet carrying L2CAP channel 4 (ATT)
        if len(pkt) < 12 or pkt[0] != 0x02 or struct.unpack("<H", pkt[7:9])[0] != 4:
            continue
        att = pkt[9:]
        if att[0] not in (0x12, 0x52):
            continue
        sent = (flags & 1) == 0
        when = datetime.datetime.fromtimestamp((ts - BTSNOOP_EPOCH_US) / 1e6, datetime.timezone.utc)
        yield when, sent, struct.unpack("<H", att[1:3])[0], att[3:].hex()


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: btsnoop_writes.py btsnoop_hci.log")
    for when, sent, handle, value in att_writes(sys.argv[1]):
        if not sent or value in NOISE or value in CONNECT or value.startswith("05008119"):
            continue
        if len(value) // 2 > MAX_COMMAND_BYTES:
            continue
        # Android writes the log's clock as local time in a UTC field
        print(f"{when:%H:%M:%S.%f}"[:12], f"handle 0x{handle:04x}", value)


if __name__ == "__main__":
    main()
