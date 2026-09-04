# PitPat T01 — firmware 37 ("fba0") variant

Upstream targets the **T01 (BA04)** revision. This document describes a second
T01 revision that speaks the same application protocol over **different GATT
characteristics** and **without the 4-byte transport wrapper** in either
direction. On an unpatched checkout the dashboard connects and then shows
nothing, and the control buttons do nothing at all.

All findings below were confirmed against real hardware (firmware byte `37`,
`device_type` 5, max speed 6000, imperial units).

---

## 1. Do I have this variant?

Enumerate the device's services. If you see `fba0` rather than an `ff00`-family
service, this document applies:

```
svc 0000fba0-…      ← vendor service (this variant)
    0000fba2-…  read, notify      ← status stream
    0000fba1-…  read, write       ← commands
svc 00001910-…      ← advertised but inert; see §6
    00002b10-…  read, notify
    00002b11-…  write, write-without-response
svc 00001801-… / 00001800-…       ← standard GATT/GAP
```

Quick check (host or container):

```bash
python - <<'PY'
import asyncio
from bleak import BleakClient
async def m():
    async with BleakClient("AA:BB:CC:DD:EE:FF") as c:
        for s in c.services:
            print("svc", s.uuid)
            for ch in s.characteristics:
                print("   ", ch.uuid, ch.properties)
asyncio.run(m())
PY
```

---

## 2. The three deviations

### 2.1 Characteristic UUIDs

| Role   | Upstream (BA04) | This variant |
| ------ | --------------- | ------------ |
| notify | `0000ff02-…`    | `0000fba2-…` |
| write  | `0000ff01-…`    | `0000fba1-…` |

The read/write role split is identical, only the service family is renamed.

### 2.2 No transport wrapper — inbound

Upstream strips four bytes before parsing (`TreadmillData(data[4:])`). This
variant delivers the payload bare, starting at the frame-type byte, so the
strip must go.

The XOR checksum is the discriminator — it is decisive, so you never have to
guess:

```
TreadmillData(frame)      → checksum_valid = True    ← correct
TreadmillData(frame[4:])  → checksum_valid = False
```

### 2.3 No transport wrapper — outbound

**This is the one that costs hours.** Upstream frames every write as
`4d00 <counter> <length>` + packet. This variant expects the bare packet.

A wrapped write is **accepted at the ATT layer with no error and then silently
discarded.** `write_gatt_char()` returning successfully proves nothing on this
device — the belt simply never moves and no error appears anywhere.

```
4d 00 04 17 6a 17 00 …    ← wrapped: accepted, ignored
6a 17 00 …                ← bare:    works
```

Corollary: the heartbeat's payload is `6a05fdf843`. In upstream's
`HEARTBEAT_BODY = "056a05fdf843"` the leading `05` is the wrapper's *length*
byte, not part of the packet — don't carry it over.

Reading `fba1` back is a useful probe: it returns a RAM buffer containing your
previous writes verbatim, which confirms bytes are landing even when the device
acts on none of them.

### 2.4 Unit bit must match the device

The command byte carries a unit flag. A device reporting imperial needs
imperial-flagged commands, so derive it from the last status frame instead of
assuming metric:

```python
packet[12] = cmd & 0xF7 if is_kph else cmd | 0x08
#            0x04 metric        0x0C imperial
```

Not independently proven: the wrapper fix and the imperial flag landed in the
same successful write, so metric-flagged bare commands may well also work.
Matching the device is correct by construction regardless.

The flag does **not** change how the speed value is interpreted — see §2.5.

---

### 2.5 Speed and distance are metric on the wire, always

Whatever the unit bit says and whatever the treadmill's own display is set to,
`target_speed` is **thousandths of a kph** and `distance` is **metres**, in both
directions.

Measured on a unit displaying mph (`unit_mode` 1), with imperial-flagged
commands:

| Sent `target_speed` | Treadmill display |
| ------------------- | ----------------- |
| 3000                | 1.9 mph           |
| 1800                | 1.1 mph           |

3.0 / 1.609 = 1.86 and 1.8 / 1.609 = 1.12 — the value is taken as kph and
converted for display. Status frames come back the same way: stepping up until
the dashboard read "3.0 mph" left the treadmill showing 1.9.

So the conversion belongs at the UI boundary, not in the packet:

```python
wire  = round(mph * 1.609344 * 1000)   # outgoing
shown = wire / 1000 / 1.609344         # incoming
```

`src/app.py` does this in `_to_wire_speed()` / `_to_display_speed()`, and the
+/- buttons step by 0.1 of the *displayed* unit rather than a flat 100.

---

## 3. Status frame (notify on `fba2`)

53 bytes, streamed roughly once per second **unprompted** — no heartbeat needed
to start the flow. Offsets match upstream `TreadmillData` exactly.

```
66 35 00 03E8 03E8 00000000 00 00 00 00000000 0000 00000BB8 80 25 8B 1770 00 05 02 <serial…> 00 00 00 A2 43
```

| Offset  | Field                                          |
| ------- | ---------------------------------------------- |
| 0       | frame type — `0x66` here, `0x68` on BA04       |
| 1       | total length (`0x35` = 53)                     |
| 3–4     | current speed, BE (`0x03E8` = 1000 = 1.00)     |
| 5–6     | target speed, BE                               |
| 7–10    | distance                                       |
| 11 / 12 | current / target incline (run-walk in bits 6-7)|
| 13      | heart rate                                     |
| 14–17   | steps                                          |
| 18–19   | calories                                       |
| 20–23   | duration, ms (`0x0BB8` = 3000 = 3 s)           |
| 24      | cycle id (session id, assigned on first run)   |
| 25      | firmware version (`0x25` = 37)                 |
| 26      | flags — see below                              |
| 27–28   | max speed (`0x1770` = 6000)                    |
| 29 / 30 | max incline / device type (`& 31`)             |
| 31      | bracelet power                                 |
| 32–47   | serial number, 16 ASCII bytes                  |
| 48 / 49 | BLE model / brand                              |
| 51      | XOR checksum over bytes 1–50                   |
| 52      | terminator `0x43`                              |

### Flags byte (26)

| Bits         | Meaning                                              |
| ------------ | ---------------------------------------------------- |
| 0 (`0x01`)   | connected                                            |
| 1–2 (`0x06`) | binary                                               |
| 3–4 (`0x18`) | running state: `0x18`→0, `0x08`→1 run, `0x10`→2 pause, `0x00`→3 stop |
| 5–6 (`0x60`) | bracelet                                             |
| 7 (`0x80`)   | unit mode — 1 = imperial                             |

Observed: `0x83` idle (imperial, stopped) → `0x8B` running (bit 3 set).

---

## 4. Command frame (write to `fba1`)

23 bytes, sent bare. Verified working, byte-for-byte as captured:

| Command   | Packet                                             |
| --------- | -------------------------------------------------- |
| start 1.00| `6a17 00000000 03e8 01 00 50 00 0c <userid> 12 43`  |
| set 1.10  | `6a17 00000000 044c 05 00 50 00 0c <userid> b5 43`  |
| pause     | `6a17 00000000 0000 01 00 50 00 0a <userid> ff 43`  |
| stop      | `6a17 00000000 0000 01 00 50 00 08 <userid> fd 43`  |

| Offset | Field                                                     |
| ------ | --------------------------------------------------------- |
| 0      | opcode `0x6A`                                             |
| 1      | total length (`0x17` = 23)                                |
| 6–7    | target speed, BE                                          |
| 8      | acceleration ramp — 1 on start/stop/pause, 5 on set-speed |
| 9      | incline                                                   |
| 10     | user weight, kg (`0x50` = 80, hardcoded upstream)         |
| 11     | 0                                                         |
| 12     | command: 0 stop, 2 pause, 4 start/set — `\| 0x08` imperial |
| 13–20  | 8-byte user id                                            |
| 21     | XOR checksum over bytes 1–20                              |
| 22     | terminator `0x43`                                         |

Status-poll ("heartbeat") is the same shape at length 5: `6a 05 fd f8 43`,
where `0x05 ^ 0xFD = 0xF8` is the checksum.

**The user id is not validated.** Upstream's hardcoded
`DEFAULT_USER_ID = 58965456623` (`0x0DBA9D76EF`) — a value from the original
author's device — drives this unit fine. No pairing, bonding, or app-level
handshake is required either; the device is unpaired and accepts commands.

### Verified transitions

| Command  | Resulting state                          |
| -------- | ---------------------------------------- |
| start    | state 1, speed 1000 (1.00 mph)           |
| speed up | speed 1100                               |
| pause    | state 2, speed 0, target retained        |
| stop     | state 3, duration reset to 0             |

---

## 5. Running under Docker on Linux

The README's `--device=/dev/rfcomm0` is wrong for this app: rfcomm is Bluetooth
Classic serial, while this is BLE GATT. bleak never touches the HCI device —
every operation is a D-Bus call to the host's `bluetoothd`. So all the container
needs is the system bus socket. No `--privileged`, no `--net=host`, no
`CAP_NET_ADMIN`:

```bash
docker run -d --name pitpat -p 8050:8050 \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  --restart unless-stopped \
  pitpat-treadmill-control --host 0.0.0.0 --port 8050
```

Verify the plumbing independently of any protocol problem — if this lists
devices, Bluetooth is fine and anything still broken is application-level:

```bash
docker exec pitpat python -c \
 "import asyncio;from bleak import BleakScanner;print(asyncio.run(BleakScanner.discover(timeout=8)))"
```

---

## 6. Troubleshooting

### `Device with address … was not found` (after ~10 s)

Almost always a **stale link**, not a missing device. A BLE peripheral stops
advertising while connected, so if anything already holds it — the desktop
Bluetooth panel, a previous app session, an unclean exit — bleak's scan cannot
see it. The signature is unmistakable:

```
$ bluetoothctl info <MAC> | grep Connected
        Connected: yes          ← right there, and still "not found"
```

Fix: `bluetoothctl disconnect <MAC>`. **Never connect the treadmill from the
desktop Bluetooth panel** — let the app own the link. `_release_stale_link()` in
`bluetooth_manager.py` now does this automatically over
`org.bluez.Device1.Disconnect` before each connect.

Pairing is neither needed nor wanted; the device works unpaired.

### Same error, instantly or with a doubled space in the log

A pasted address with surrounding whitespace. `Device with address  7C:…` (two
spaces) is the tell. The connect handler now strips it.

### Connects, dashboard stays empty

Wrong notify characteristic, or the `data[4:]` strip still present — §2.1, §2.2.

### Connects and shows data, but buttons do nothing

The outbound wrapper — §2.3. Check the logged bytes actually begin `6a`, not
`4d00`.

### `2b10` / `2b11` (service `1910`)

Advertised but **inert** on this firmware. `2b10` reads `00` and never notifies;
`2b11` accepts writes, wrapped or bare, that do nothing. It is not a control
channel — don't spend time there.

---

## 7. Patch summary

| File                       | Change                                                            |
| -------------------------- | ----------------------------------------------------------------- |
| `src/bluetooth_manager.py` | notify/write UUIDs → `fba2` / `fba1`                              |
| `src/bluetooth_manager.py` | `TreadmillData(data[4:])` → `TreadmillData(data)`                  |
| `src/bluetooth_manager.py` | writes sent bare; heartbeat → `6a05fdf843`; wrapper + counter gone |
| `src/bluetooth_manager.py` | new `_release_stale_link()`, called from `connect()`               |
| `src/app.py`               | new `_is_kph()`, threaded through all 5 command call sites         |
| `src/app.py`               | strip whitespace from the pasted device address                   |
| `src/bluetooth_manager.py` | new `discover_treadmills()` — scan + BlueZ known devices           |
| `src/app.py`               | Scan button, address suggestions, auto-scan on page load           |
| `src/app.py`               | speed entry field → `set_speed`, clamped to `max_speed`            |
| `src/app.py`               | kph↔mph conversion at the UI boundary (§2.5)                       |

Both UUID/wrapper changes are currently unconditional. Upstreaming this cleanly
means **detecting** the variant — probe for `fba0` vs the `ff00` family at
connect time and select UUIDs plus wrapper mode from that, so one build serves
both revisions.

## 8. Still unverified

- Incline commands — this unit reports `max_incline` 0, so there was nothing to test.
- Metric-flagged bare commands (§2.4). The flag does not affect the *value*
  scaling either way (§2.5).
- Whether heartbeats are needed at all: the device streams status unprompted and
  did not stop the belt when writes went quiet during testing. They may be a
  no-op here, or a watchdog on longer runs.
- Distance units. Speed is confirmed metric-on-the-wire (§2.5) and `distance` is
  converted the same way on the assumption it is metres; not yet checked against
  a measured run.
- `duration_seconds` assumes ms for firmware > 19; consistent with the `0x0BB8`
  = 3 s observation but only checked over a few seconds.
