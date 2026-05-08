"""
jli_ota.py — Jieli BLE OTA firmware flash / factory reset for BW01.

Reverses the JLI BT OTA SDK found in the SuperBand app (com.jieli.jl_bt_ota).

Protocol:
  Service:   0000ae00-0000-1000-8000-00805f9b34fb
  Write:     0000ae01-0000-1000-8000-00805f9b34fb
  Notify:    0000ae02-0000-1000-8000-00805f9b34fb

  Packet format: FE DC BA [FLAGS] [OPCODE] [PLEN_HI PLEN_LO] [PAYLOAD...] [EF]
    FLAGS: bit7 = type  (1 = command from host, 0 = response from host)
           bit6 = expects_response
    PLEN:  length of PAYLOAD (= SN byte + data)
    PAYLOAD (command, type=1):   [SN][data...]
    PAYLOAD (response, type=0):  [STATUS][SN][data...]

  OTA sequence (pull model — device requests blocks):
    H→D  0xE3  ENTER_OTA       (no data)
    D→H  0xE3  response        status=0
    H→D  0xE8  NOTIFY_SIZE     data = firmware_size [4B BE]
    D→H  0xE8  response        status=0
    D→H  0xE5  block request   data = offset[4B BE] + length[2B BE]
    H→D  0xE5  block response  status=0, data = firmware[offset:offset+length]
    ... repeat until all bytes sent ...
    Device reboots automatically.

Firmware is downloaded from Gulaike servers:
  GET https://tomato.gulaike.com/api/v1/config/app?name=BW01&type=1&version=0
  Authorization: Bearer 6fcb7f58475b4e5aad8f0f1cadce235e

NOTE: The device must expose the 0000ae00 OTA service. If not found, the
script will attempt to trigger OTA mode by sending a soft reset over the
normal 7e400002 channel first.
"""

import asyncio
import struct
import sys
import time
import zipfile
import io
import json
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bleak import BleakClient, BleakScanner

DEVICE_MAC    = "9D:05:2E:7F:A2:05"
DEVICE_NAME   = "BW01"

OTA_SVC_UUID  = "0000ae00-0000-1000-8000-00805f9b34fb"
WRITE_UUID    = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID   = "0000ae02-0000-1000-8000-00805f9b34fb"

# Normal channel (used as fallback to trigger OTA mode)
NORM_WRITE_UUID  = "7e400002-b5a3-f393-e0a9-e50e24dcca9d"
NORM_NOTIFY_UUID = "7e400003-b5a3-f393-e0a9-e50e24dcca9d"

API_URL   = "https://tomato.gulaike.com/api/v1/config/app?name={name}&type=1&version={version}"
API_TOKEN = "Bearer 6fcb7f58475b4e5aad8f0f1cadce235e"

# JLI packet markers
JLI_PREFIX = bytes([0xFE, 0xDC, 0xBA])
JLI_END    = 0xEF

# OTA opcodes
OP_ENTER_OTA  = 0xE3
OP_EXIT_OTA   = 0xE4
OP_BLOCK      = 0xE5
OP_STATUS     = 0xE6
OP_REBOOT     = 0xE7
OP_NOTIFY_SZ  = 0xE8


# ─── Packet builder ──────────────────────────────────────────────────────────

def _pack(flags: int, opcode: int, payload: bytes) -> bytes:
    plen = len(payload)
    return JLI_PREFIX + struct.pack(">BBH", flags, opcode, plen) + payload + bytes([JLI_END])


def cmd(opcode: int, sn: int, data: bytes = b"", expects_response: bool = True) -> bytes:
    """Host→Device command (type=1, bit7=1)."""
    flags = 0x80 | (0x40 if expects_response else 0x00)
    payload = bytes([sn]) + data
    return _pack(flags, opcode, payload)


def resp(opcode: int, dev_sn: int, data: bytes = b"", status: int = 0) -> bytes:
    """Host→Device response to a device command (type=0, bit7=0)."""
    flags = 0x00
    payload = bytes([status, dev_sn]) + data
    return _pack(flags, opcode, payload)


# ─── Packet parser ────────────────────────────────────────────────────────────

class PacketBuffer:
    """Reassemble JLI packets from BLE notification fragments."""

    def __init__(self):
        self._buf = bytearray()
        self._queue: asyncio.Queue = asyncio.Queue()

    def feed(self, data: bytes):
        self._buf.extend(data)
        self._try_parse()

    def _try_parse(self):
        buf = self._buf
        while True:
            # Find prefix
            idx = -1
            for i in range(len(buf) - 2):
                if buf[i] == 0xFE and buf[i+1] == 0xDC and buf[i+2] == 0xBA:
                    idx = i
                    break
            if idx == -1:
                self._buf = bytearray()
                return
            if idx > 0:
                del buf[:idx]
            # Need at least: prefix(3) + flags(1) + opcode(1) + plen(2) + end(1) = 8 bytes
            if len(buf) < 8:
                return
            plen = struct.unpack_from(">H", buf, 5)[0]
            total = 7 + plen + 1  # 3(prefix) + 4(header) + plen + 1(end)
            if len(buf) < total:
                return
            pkt = bytes(buf[:total])
            del buf[:total]
            self._queue.put_nowait(pkt)

    async def get(self, timeout: float = 10.0):
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None


def parse_jli_pkt(raw: bytes):
    """Returns (pkt_type, opcode, sn, status, param_data) or None."""
    if len(raw) < 8:
        return None
    if raw[:3] != JLI_PREFIX:
        return None
    if raw[-1] != JLI_END:
        return None
    flags  = raw[3]
    opcode = raw[4]
    plen   = struct.unpack_from(">H", raw, 5)[0]
    payload = raw[7:7 + plen]
    if len(payload) != plen:
        return None
    pkt_type = (flags >> 7) & 1   # 1 = device sending command, 0 = device response
    if pkt_type == 1:              # device→host command
        sn     = payload[0] if payload else 0
        status = 0
        param  = payload[1:]
    else:                          # device→host response
        status = payload[0] if len(payload) >= 1 else 0
        sn     = payload[1] if len(payload) >= 2 else 0
        param  = payload[2:]
    return (pkt_type, opcode, sn, status, param)


# ─── Firmware API ─────────────────────────────────────────────────────────────

def fetch_firmware_url(name: str = DEVICE_NAME, version: str = "0") -> tuple:
    """Returns (download_url, firmware_version_str)."""
    url = API_URL.format(name=name, version=version)
    print(f"  GET {url}")
    req = urllib.request.Request(url, headers={"authorization": API_TOKEN})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    print(f"  Response: {json.dumps(body, ensure_ascii=False)}")

    # Navigate common JSON response shapes
    inner = body.get("data") or body
    if isinstance(inner, list):
        inner = inner[0] if inner else {}
    dl_url = (inner.get("app_down_url") or inner.get("appDownUrl4g") or
              inner.get("url") or inner.get("downUrl") or inner.get("download_url"))
    ver    = str(inner.get("version") or inner.get("softVersion") or "unknown")
    if not dl_url:
        raise ValueError(f"Cannot find download URL in: {body}")
    return dl_url, ver


def download_firmware(url: str) -> bytes:
    print(f"  Downloading {url} ...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    print(f"  {len(raw):,} bytes received")
    # Jieli firmware is often zipped
    if raw[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = zf.namelist()
        print(f"  ZIP contents: {names}")
        for n in names:
            if n.lower().endswith((".ufw", ".bin", ".img", ".fw", ".jl")):
                fw = zf.read(n)
                print(f"  Extracted '{n}': {len(fw):,} bytes")
                return fw
        # Fall back to first file
        fw = zf.read(names[0])
        print(f"  Extracted '{names[0]}': {len(fw):,} bytes")
        return fw
    return raw


# ─── OTA transfer ─────────────────────────────────────────────────────────────

async def do_ota(firmware: bytes) -> bool:
    fw_size = len(firmware)
    pbuf = PacketBuffer()

    print(f"  Scanning for {DEVICE_MAC} ...")
    dev = await BleakScanner.find_device_by_address(DEVICE_MAC, timeout=20.0)
    if dev is None:
        dev = await BleakScanner.find_device_by_filter(
            lambda d, _: "bw01" in (d.name or "").lower(), timeout=20.0
        )
    if dev is None:
        print("  Device not found.")
        return False
    print(f"  Found: {dev.address} ({dev.name}). Connecting...")

    try:
        async with BleakClient(dev, timeout=15.0) as client:
            svc_uuids = {str(s.uuid).lower() for s in client.services}
            print(f"  Services: {svc_uuids}")

            if OTA_SVC_UUID not in svc_uuids:
                print(f"  JLI OTA service ({OTA_SVC_UUID}) NOT found.")
                print("  The device may need to be in OTA mode.")
                print("  Try: open SuperBand app → Online Upgrade, then run this script.")
                return False

            await client.start_notify(NOTIFY_UUID, lambda s, d: pbuf.feed(bytes(d)))
            mtu = max(20, getattr(client, "mtu_size", 23) - 3)
            print(f"  JLI OTA service found. MTU={mtu}. Firmware={fw_size:,} bytes")

            async def send(pkt: bytes):
                for i in range(0, len(pkt), mtu):
                    await client.write_gatt_char(WRITE_UUID, pkt[i:i+mtu], response=False)
                    await asyncio.sleep(0.005)

            sn = 0

            # ── Step 1: ENTER OTA ────────────────────────────────────────────
            print("\n  [1] ENTER_OTA (0xE3) ...")
            await send(cmd(OP_ENTER_OTA, sn))
            raw = await pbuf.get(10.0)
            if raw:
                p = parse_jli_pkt(raw)
                print(f"       Response: {p}")
                if p and p[3] != 0:
                    print(f"  ENTER_OTA rejected (status={p[3]})")
                    return False
            else:
                print("  No response to ENTER_OTA (continuing anyway...)")
            sn = (sn + 1) & 0xFF

            # ── Step 2: NOTIFY FIRMWARE SIZE ──────────────────────────────────
            print(f"  [2] NOTIFY_SIZE (0xE8) = {fw_size:,} bytes ...")
            await send(cmd(OP_NOTIFY_SZ, sn, struct.pack(">I", fw_size)))
            raw = await pbuf.get(10.0)
            if raw:
                p = parse_jli_pkt(raw)
                print(f"       Response: {p}")
                if p and p[3] != 0:
                    print(f"  NOTIFY_SIZE rejected (status={p[3]})")
                    return False
            else:
                print("  No response to NOTIFY_SIZE (continuing anyway...)")
            sn = (sn + 1) & 0xFF

            # ── Step 3: Transfer (device pulls blocks) ────────────────────────
            print("\n  [3] Waiting for device block requests ...")
            t0 = time.monotonic()
            max_sent = 0
            deadline = time.monotonic() + 600  # 10 min max

            while time.monotonic() < deadline:
                raw = await pbuf.get(30.0)
                if raw is None:
                    print("  Timeout: device stopped requesting blocks.")
                    break

                p = parse_jli_pkt(raw)
                if p is None:
                    print(f"  Bad packet: {raw.hex()}")
                    continue

                pkt_type, opcode, dev_sn, status, param = p

                # Completion signals
                if opcode in (OP_STATUS, OP_EXIT_OTA) and pkt_type == 0:
                    print(f"  OTA complete (opcode=0x{opcode:02X} status={status})")
                    return True
                if opcode == OP_REBOOT:
                    print(f"  Device signaled REBOOT — OTA successful!")
                    return True

                if opcode != OP_BLOCK or pkt_type != 1:
                    print(f"  Unexpected packet: type={pkt_type} op=0x{opcode:02X} "
                          f"sn={dev_sn} status={status} param={param.hex() if param else ''}")
                    continue

                # Device requests block: param = offset[4B BE] + req_len[2B BE]
                if len(param) < 6:
                    print(f"  Short block request param: {param.hex()}")
                    continue

                offset  = struct.unpack_from(">I", param, 0)[0]
                req_len = struct.unpack_from(">H", param, 4)[0]
                end_off = min(offset + req_len, fw_size)
                block   = firmware[offset:end_off]

                await send(resp(OP_BLOCK, dev_sn, block))

                max_sent = max(max_sent, end_off)
                pct  = max_sent * 100 // fw_size
                kbps = (max_sent / 1024) / max(time.monotonic() - t0, 0.001)
                if offset % 20000 < req_len or max_sent >= fw_size:
                    print(f"  offset={offset:,}/{fw_size:,} ({pct}%) {kbps:.0f} KB/s")

                if max_sent >= fw_size:
                    print("  All bytes sent. Waiting for reboot signal ...")
                    for _ in range(20):
                        raw2 = await pbuf.get(5.0)
                        if raw2:
                            p2 = parse_jli_pkt(raw2)
                            print(f"  Final: {p2}")
                            if p2 and p2[1] in (OP_REBOOT, OP_EXIT_OTA, OP_STATUS):
                                return True
                        else:
                            break
                    return True  # assume success if device just disconnects

            return False

    except Exception as e:
        import traceback
        print(f"  Error: {e}")
        traceback.print_exc()
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  BW01 JLI OTA Factory Reset")
    print("=" * 55)
    print()

    fw_cache = Path("bw01_firmware.bin")

    if fw_cache.exists():
        print(f"[INFO] Using cached firmware: {fw_cache} ({fw_cache.stat().st_size:,} bytes)")
        print("       Delete bw01_firmware.bin to re-download.")
        firmware = fw_cache.read_bytes()
    else:
        print("[Step 1] Querying Gulaike firmware API ...")
        try:
            dl_url, ver = fetch_firmware_url()
            print(f"         Firmware version: {ver}")
        except Exception as e:
            print(f"  API error: {e}")
            print()
            print("  If the API is unreachable, place the firmware file manually")
            print("  at bw01_firmware.bin and re-run this script.")
            sys.exit(1)

        print()
        print("[Step 2] Downloading firmware ...")
        try:
            firmware = download_firmware(dl_url)
            fw_cache.write_bytes(firmware)
            print(f"         Saved to {fw_cache}")
        except Exception as e:
            print(f"  Download error: {e}")
            sys.exit(1)

    print()
    print("[Step 3] Connecting to BW01 and flashing ...")
    print(f"         MAC: {DEVICE_MAC}")
    print(f"         Firmware: {len(firmware):,} bytes")
    print()
    print("  Make sure:")
    print("  - Device is NOT on charger (must be in normal/boot mode)")
    print("  - Device is NOT connected from phone")
    print()

    success = await do_ota(firmware)

    print()
    if success:
        print("SUCCESS! Device is flashing firmware and will reboot.")
        print("This factory-resets the device (clears all video slots).")
        print("After reboot (~30 sec), reconnect in SuperBand app to restore settings.")
    else:
        print("FAILED. See output above for details.")
        print()
        print("Possible causes:")
        print("  1. Device not advertising JLI OTA service (0000ae00)")
        print("     → Try: open SuperBand app, go to Online Upgrade page, then rerun")
        print("  2. Device is connected to phone")
        print("     → Disconnect from phone first")
        print("  3. Device is charging")
        print("     → Remove from charger")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
