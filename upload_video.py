"""
Upload a video file to the video keychain/badge device via BLE.
Protocol reverse-engineered from com.legend.smartwatch.electronicbadge.android.

Usage:
    python upload_video.py <device_name_or_address> <video_file> [options]

Options:
    --fps N          Video FPS (default: 24). Lower → smaller file.
    --quality N      MJPEG quality 2-31 (default: 10). Higher → lower quality → smaller file.
    --duration N     Trim video to N seconds (default: no limit).
    --resolution NxM Output resolution (default: 480x480). E.g. 320x320.
    --no-audio       Drop audio track (saves ~2% space).
    --audio-rate N   Audio sample rate Hz (default: 16000). 8000 = phone quality but ~half size.

Device storage limit: ~1.74 MB blob. Fits ~14s at 480x480/q10 or ~20s at 240x240/q25.
Audio alone at 16kHz consumes 32KB/s. Use --audio-rate 8000 to halve audio overhead.

Example:
    python upload_video.py "BW01" my_video.mp4
    python upload_video.py "BW01" my_video.mp4 --duration 14
    python upload_video.py "BW01" my_video.mp4 --audio-rate 8000 --resolution 240x240 --fps 20
    python upload_video.py "9D:05:2E:7F:A2:05" my_video.mp4

Requirements:
    pip install bleak
"""

import asyncio
import struct
import subprocess
import sys
import tempfile

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import time
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    print("ERROR: bleak is not installed. Run: pip install bleak")
    sys.exit(1)

# ── BLE channel (confirmed working) ──────────────────────────────────────────
# The ae00/ae01/ae02 service does NOT respond; 7e400002/7e400003 is correct.
WRITE_UUID  = "7e400002-b5a3-f393-e0a9-e50e24dcca9d"
NOTIFY_UUID = "7e400003-b5a3-f393-e0a9-e50e24dcca9d"

# ── Protocol markers ──────────────────────────────────────────────────────────
HOST_MARKER   = 0xCD   # host → device
DEVICE_MARKER = 0xDC   # device → host

# ── 0x25 BajiProtocol (DEVICE_INFO handshake only) ───────────────────────────
PRODUCT_ID_25 = 0x25
PROTO_VERSION = 0x01
MOD_SYSTEM_INFO = 0x03
CMD_DEVICE_INFO_REQUEST  = 0x00
CMD_DEVICE_INFO_RESPONSE = 0x01
FILE_TYPE_VIDEO = 0x02  # used in DEVICE_INFO payload only

# ── 0x1f WatchTheme protocol (video upload) ───────────────────────────────────
PRODUCT_ID_1F = 0x1f
WT_CMD_DATA   = 0x01  # DATA chunk  (c42.e)
WT_CMD_START  = 0x02  # START       (c42.g) — triggers animation on device immediately
WT_CMD_FINISH = 0x03  # FINISH      (c42.f)

# WatchTheme3Body constants for video background upload
WT_WATCH_ID  = 5538   # 0x15a2 — hardcoded in firmware for video/background feature
WT_FILE_TYPE = 1      # AVI/MJPEG

# Device response code ranges (from lt2.smali constructor):
#   this.l = [1000, 100_000_000)  → byte-progress codes
#   this.m = [100_000_000, 200_000_000) → seek-to-offset codes
WT_RESP_RANGE_L0   = 1000
WT_RESP_RANGE_L1   = 100_000_000
WT_RESP_SUCCESS    = 2
WT_RESP_CHECK_FAIL = 1

# Default chunk size used by the phone app (lt2.t() default = 5000 bytes)
WT_CHUNK_SIZE = 5000

# Inter-write delay matching CommandPool.n(6) = 6ms
BLE_WRITE_INTERVAL = 0.006


# ── 0x25 packet builder (for handshake only) ─────────────────────────────────

def build_25_packet(module_id: int, command: int, payload: bytes = b"") -> bytes:
    """
    Build a 0x25 BajiProtocol packet (used only for DEVICE_INFO handshake).

      [0xCD][len 2B BE][0x25][0x01][module][cmd_data_len 2B BE][command][payload]
    """
    n = len(payload)
    return (struct.pack(">BH", HOST_MARKER, n + 6) +
            struct.pack(">BBBH", PRODUCT_ID_25, PROTO_VERSION, module_id, n + 1) +
            bytes([command]) + payload)


# ── 0x1f WatchTheme packet builder ───────────────────────────────────────────

def build_wt_packet(cmd: int, payload: bytes) -> bytes:
    """
    Build a 0x1f WatchTheme packet (8-byte header, confirmed from c42.t smali).

      [0xCD][len 2B BE][0x1F][0x01][cmd][payload_len 2B BE][payload]

    len field = 5 + payload_len  (counts everything after the 3-byte start+len header)
    """
    n = len(payload)
    return (struct.pack(">BHBBBH", HOST_MARKER, 5 + n,
                        PRODUCT_ID_1F, PROTO_VERSION, cmd, n) +
            payload)


def build_wt_start_payload(avi_data: bytes, declared_blob_size: int = None) -> bytes:
    """
    Build the START command payload (c42.g in the app → lt2.Q method).

    Layout (confirmed from Q() smali trace):
      [watchID 4B BE][fileType 1B][featureBits 1B][R 1B][G 1B][B 1B][blobSize 4B BE]
      [bgStyle 1B][timeStyle 1B][styleCount 1B][bgColorByte 1B]

    For video-only upload:
      - featureBits = 0b001000 = 8  (bit3 = hasBg/video, computed by he1.a() in app)
      - RGB = (0, 0, 0)  (no background color override)
      - blobSize = 4 + len(avi_data)  (the blob prepends a 4-byte size header)
      - style bytes all zero (no style list passed)

    declared_blob_size: if set, overrides the blobSize field sent to the device.
      The device checks this value against its firmware limit at START time.
      Actual data sent in chunks and FINISH checksum always use the real blob size.
    """
    actual_blob_size = 4 + len(avi_data)
    blob_size = declared_blob_size if declared_blob_size is not None else actual_blob_size
    feature_bits = 0b001000  # = 8

    return (struct.pack(">I", WT_WATCH_ID) +          # watchID (4B BE)
            bytes([WT_FILE_TYPE, feature_bits]) +       # fileType, featureBits
            bytes([0, 0, 0]) +                          # R, G, B (no bg color)
            struct.pack(">I", blob_size) +              # blobSize (4B BE) — may be overridden
            bytes([0, 0, 0, 0]))                        # style bytes (all zero)


def build_wt_chunk(index_1based: int, chunk_data: bytes) -> bytes:
    """
    Build one DATA chunk payload (format confirmed from lt2.O() and lt2.j() smali).

      [2B BE 1-based index][chunk_data][4B BE byte-sum checksum]

    The checksum covers ALL bytes of [index_bytes + chunk_data].
    The phone uses 1-based chunk indices (this.r starts at 0, sent as this.r+1).
    """
    index_bytes = struct.pack(">H", index_1based)
    indexed   = index_bytes + chunk_data
    checksum  = sum(b & 0xFF for b in indexed) & 0xFFFFFFFF
    return indexed + struct.pack(">I", checksum)


def build_wt_finish_payload(blob: bytes, declared_size: int = None) -> bytes:
    """
    Build the FINISH command payload (confirmed from lt2.k() smali).
    Returns 4-byte BE sum of blob bytes.

    When declared_size is set (fake-size upload), the device verifies checksum
    over only the first declared_size bytes (matching the blobSize it was told
    at START), not the full blob. We must match its expectation.
    """
    checksum_data = blob[:declared_size] if declared_size is not None else blob
    total_sum = sum(b & 0xFF for b in checksum_data) & 0xFFFFFFFF
    return struct.pack(">I", total_sum)


# ── Packet parsing ────────────────────────────────────────────────────────────

def parse_25_packet(data: bytes):
    """
    Parse a 0x25 device→host packet.
    Format: [0xDC][len 2B][0x25][module][cmd][payload...]  (min 8 bytes)
    Returns (module, cmd, payload) or None.
    """
    if len(data) < 8:
        return None
    if data[0] != DEVICE_MARKER or data[3] != PRODUCT_ID_25:
        return None
    return (data[4], data[5], bytes(data[6:]))


def parse_wt_response(data: bytes):
    """
    Parse a device→host WatchTheme response packet.

    Observed on-wire format (from actual device capture):
      [0xCD][len 2B][0x20][0x01][0x01][payloadLen 2B][responseCode 4B BE]
      idx:   0   1   2   3    4    5   6           7   8  9 10 11

    Note: device uses 0xCD (same as host) and product_id=0x20 for responses,
    NOT 0xDC/0x1f as one might expect. cmd=0x01 at byte[5] triggers the
    WatchTheme handler in c.b0() packed-switch (value 1 → pswitch_5 → lt2.N).

    Also handles 0x1f cmd=0x02 ACK (short packet, no responseCode).

    Returns:
      int  — responseCode from the 0x20 packet
      "ack" — for 0x1f cmd=0x02 start-ACK
      None — not a WatchTheme packet
    """
    if len(data) < 8:
        return None

    # Short ACK: [DC/CD][len][1F][cmd][payload...] — device ack of START/FINISH
    if data[3] == PRODUCT_ID_1F:
        return "ack"

    # ResponseCode packet: [CD][len][20][01][01][payloadLen 2B][4B BE code]
    if data[0] == HOST_MARKER and data[3] == 0x20 and len(data) >= 12:
        return struct.unpack_from(">I", data, 8)[0]

    return None


# Threshold: codes in this.m = [100M, 200M). Decoded by lt2.u(J):
#   str(code)[1] == '0'  → type-0: fatal "upgrade failed" error
#   str(code)[1] == '1'  → type-1: device silently waits (observed: file too large)
#   value = int(str(code)[2:]) — embedded value (suspected: device storage limit in bytes)
WT_RESP_M_RANGE_LO = 100_000_000
WT_RESP_M_RANGE_HI = 200_000_000


# ── Video preprocessing ───────────────────────────────────────────────────────

def find_ffmpeg():
    import shutil
    for name in ("ffmpeg", "ffmpeg.exe"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"D:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def patch_avi_riff_size(blob: bytes, declared_size: int) -> bytes:
    """
    Patch the RIFF chunk-size field in a fake-size blob so the AVI player
    knows the file ends at declared_size bytes and doesn't read past EOF.

    blob layout: [4B BE avi_size_header][AVI file bytes...]
    AVI file starts at blob[4]. RIFF chunk size at blob[8:12] (LE) should be
    (avi_file_bytes_count - 8) = (declared_size - 4 - 8) = declared_size - 12.
    """
    if len(blob) >= 16 and blob[4:8] == b"RIFF":
        blob = bytearray(blob)
        riff_chunk_size = max(0, declared_size - 12)
        blob[8:12] = struct.pack("<I", riff_chunk_size)
        return bytes(blob)
    return blob


def prepare_video(input_path: Path, output_path: Path,
                  width: int = 480, height: int = 480,
                  fps: int = 24, quality: int = 10,
                  duration: float = None, no_audio: bool = False,
                  audio_rate: int = 16000,
                  ffmpeg: str = "ffmpeg") -> bool:
    """
    Preprocess video for the device.
    Device requires AVI/MJPEG (confirmed from VideoCutActivity.smali FFmpeg command).
    Audio: PCM 16-bit LE, 16 kHz, mono.

    quality: MJPEG q:v value (2=best, 31=worst). Default 10 (high quality).
             Higher value → smaller file. Try 18-22 if file too large.
    duration: trim to N seconds if set.
    """
    vf = (f"crop=min(iw\\,ih):min(iw\\,ih),"
          f"scale={width}:{height}:flags=lanczos,"
          f"fps={fps}")

    qmin = max(2, quality - 2)
    qmax = min(31, quality + 2)

    cmd = [ffmpeg, "-y", "-i", str(input_path)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += [
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "mjpeg", "-vtag", "mjpg", "-pix_fmt", "yuvj420p",
        "-q:v", str(quality), "-coder", "1", "-flags", "+loop+global_header",
        "-pred", "1", "-qmin", str(qmin), "-qmax", str(qmax),
        "-vsync", "cfr", "-video_track_timescale", str(fps),
        "-packetsize", "4096",
    ]
    if no_audio:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "pcm_s16le", "-ar", str(audio_rate), "-ac", "1"]
    cmd += ["-f", "avi", str(output_path)]

    print(f"FFmpeg: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:")
        print(result.stderr[-2000:])
    return result.returncode == 0


def build_device_info_payload() -> bytes:
    """Minimal DeviceInfo payload for the 0x25 handshake."""
    name     = b"SuperBand"
    version  = b"1.0.0"
    proto    = b"1.0"
    types    = bytes([FILE_TYPE_VIDEO])
    features = b""
    p = b""
    p += struct.pack(">I", len(name))    + name
    p += struct.pack(">I", len(version)) + version
    p += struct.pack(">I", len(proto))   + proto
    p += struct.pack(">Q", 0)
    p += struct.pack(">Q", 0)
    p += struct.pack(">I", len(types))   + types
    p += struct.pack(">Q", 1 << 32)
    p += struct.pack(">I", len(features))+ features
    return p


# ── Main upload logic ─────────────────────────────────────────────────────────

class VideoUploader:
    def __init__(self, client: BleakClient,
                 write_uuid: str = WRITE_UUID, notify_uuid: str = NOTIFY_UUID,
                 verbose: bool = True, start_size_override: int = None):
        self.client       = client
        self._write_uuid  = write_uuid
        self._notify_uuid = notify_uuid
        self.verbose      = verbose
        self._start_size_override = start_size_override  # fake blobSize in START packet
        self._queue: asyncio.Queue = asyncio.Queue()
        self._raw_queue: asyncio.Queue = asyncio.Queue()  # raw bytes for debugging
        self._buffer = bytearray()
        self._mtu_payload = 512  # overridden after connect

    def log(self, *args):
        if self.verbose:
            print(*args, flush=True)

    def _on_notify(self, sender, data: bytearray):
        """BLE notification handler — buffers and enqueues complete packets."""
        self._raw_queue.put_nowait(bytes(data))  # raw for debugging
        self._buffer.extend(data)

        while len(self._buffer) >= 3:
            marker = self._buffer[0]
            if marker not in (HOST_MARKER, DEVICE_MARKER):
                # Desync — scan forward
                idx = next((i for i in range(1, len(self._buffer))
                             if self._buffer[i] in (HOST_MARKER, DEVICE_MARKER)), -1)
                if idx < 0:
                    self._buffer.clear()
                    return
                del self._buffer[:idx]
                continue

            declared_len = struct.unpack_from(">H", self._buffer, 1)[0]
            total_len = 3 + declared_len
            if len(self._buffer) < total_len:
                break  # wait for more data

            packet_bytes = bytes(self._buffer[:total_len])
            del self._buffer[:total_len]
            self._queue.put_nowait(packet_bytes)

    async def _recv_raw(self, timeout: float = 30.0) -> bytes:
        """Wait for the next complete packet (raw bytes)."""
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)

    async def _write(self, data: bytes):
        """Write data over BLE, splitting by MTU."""
        chunk = max(20, self._mtu_payload)
        for i in range(0, len(data), chunk):
            await self.client.write_gatt_char(self._write_uuid, data[i:i + chunk],
                                               response=False)
            await asyncio.sleep(BLE_WRITE_INTERVAL)

    # ── 0x25 helpers ──────────────────────────────────────────────────────────

    async def _send_25(self, module: int, cmd: int, payload: bytes = b""):
        pkt = build_25_packet(module, cmd, payload)
        self.log(f"  >> 0x25 [{module:#04x}:{cmd:#04x}] {len(pkt)}B  {pkt[:16].hex()}")
        await self._write(pkt)

    # ── 0x1f helpers ──────────────────────────────────────────────────────────

    async def _send_wt(self, cmd: int, payload: bytes):
        pkt = build_wt_packet(cmd, payload)
        cmd_names = {WT_CMD_DATA: "DATA", WT_CMD_START: "START", WT_CMD_FINISH: "FINISH"}
        self.log(f"  >> 0x1f [{cmd_names.get(cmd, f'{cmd:#04x}')}] "
                 f"{len(pkt)}B  {pkt[:16].hex()}{'...' if len(pkt) > 16 else ''}")
        await self._write(pkt)

    # ── Handshake (0x25 DEVICE_INFO) ─────────────────────────────────────────

    async def handshake(self) -> bool:
        self.log("\n[0] Handshake (0x25 DEVICE_INFO)...")
        await self._send_25(MOD_SYSTEM_INFO, CMD_DEVICE_INFO_REQUEST)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self._recv_raw(), timeout=5)
            except asyncio.TimeoutError:
                self.log("  (no response, retrying DEVICE_INFO_REQUEST...)")
                await self._send_25(MOD_SYSTEM_INFO, CMD_DEVICE_INFO_REQUEST)
                continue

            self.log(f"  << raw {len(raw)}B: {raw.hex()}")

            parsed = parse_25_packet(raw)
            if parsed:
                mod, cmd, payload = parsed
                if mod == MOD_SYSTEM_INFO and cmd == CMD_DEVICE_INFO_RESPONSE:
                    self.log("     Handshake OK (device sent DEVICE_INFO_RESPONSE).")
                    return True
                if mod == MOD_SYSTEM_INFO and cmd == CMD_DEVICE_INFO_REQUEST:
                    self.log("     Device asked for our info — sending DEVICE_INFO_RESPONSE...")
                    await self._send_25(MOD_SYSTEM_INFO, CMD_DEVICE_INFO_RESPONSE,
                                        build_device_info_payload())
                    self.log("     Handshake OK.")
                    return True

            # Check if it's a 0x1f packet — device may send unsolicited 0x1f status
            wt_code = parse_wt_response(raw)
            if wt_code is not None:
                self.log(f"  (unsolicited 0x1f packet during handshake, code={wt_code})")
                continue

            self.log(f"  [INFO] unexpected packet during handshake, continuing...")

        self.log("ERROR: handshake timed out.")
        return False

    # ── Main upload via 0x1f WatchTheme protocol ─────────────────────────────

    async def upload(self, video_path: Path) -> bool:
        avi_data  = video_path.read_bytes()
        avi_len   = len(avi_data)

        # Build blob: [4-byte BE AVI size][AVI bytes]
        # Confirmed from lt2.S() smali: p4 = size headers block, this.o = file data,
        # then this.o = concat(p4, file_data). For video-only: blob = [4B AVI_len][AVI]
        #
        # When start_size_override is set we also fake blob[0:4].
        # The device validates blob[0:4] == declared_blobSize - 4 on the first DATA chunk.
        # If there is a mismatch it responds with responseCode=0 (validation error).
        if self._start_size_override is not None:
            fake_header = self._start_size_override - 4
            blob = struct.pack(">I", fake_header) + avi_data
            self.log(f"     [!] Faking blob[0:4] = {fake_header:,} "
                     f"to match declared blobSize (actual AVI = {avi_len:,})")
            # Patch AVI RIFF header so the AVI player knows the file ends at
            # declared_size bytes and doesn't try to read beyond the buffer.
            blob = patch_avi_riff_size(blob, self._start_size_override)
            self.log(f"     [!] Patched AVI RIFF chunk-size to {self._start_size_override - 12:,} "
                     f"(tells player file ends at byte {self._start_size_override - 4:,})")
        else:
            blob = struct.pack(">I", avi_len) + avi_data
        blob_len = len(blob)

        self.log(f"\nFile: {video_path.name}  ({avi_len:,} bytes AVI)")
        self.log(f"Blob: {blob_len:,} bytes  (4-byte size header + AVI)")

        # Subscribe
        await self.client.start_notify(self._notify_uuid, self._on_notify)
        self.log(f"Notifications enabled on {self._notify_uuid}.")

        # Listen for any unsolicited packets for 1 second
        self.log("  (listening for unsolicited packets 1s...)")
        try:
            while True:
                raw = await asyncio.wait_for(self._recv_raw(), timeout=1.0)
                self.log(f"  << UNSOLICITED {len(raw)}B: {raw.hex()}")
        except asyncio.TimeoutError:
            pass

        await asyncio.sleep(0.1)

        # ── Step 0: Handshake ─────────────────────────────────────────────────
        if not await self.handshake():
            return False

        await asyncio.sleep(0.3)

        # ── Step 1: Send WatchTheme START ─────────────────────────────────────
        # This is what triggers the animation on the device immediately.
        # (c42.g → cmd=0x02, before ANY data is sent)
        # Retry up to 5 times if device returns error code 4 (errorCode 1009 = "device busy"
        # / "upgrade in progress" — seen in charging/recovery mode). A delay often resolves it.
        start_payload = build_wt_start_payload(avi_data, self._start_size_override)
        start_chunk = None
        for start_attempt in range(5):
            if start_attempt > 0:
                self.log(f"\n[1] Retrying WatchTheme START (attempt {start_attempt+1}/5, "
                         f"waiting 8s for device to settle)...")
                await asyncio.sleep(8.0)
            else:
                self.log("\n[1] Sending WatchTheme START (0x1f cmd=0x02)...")
            if self._start_size_override is not None:
                self.log(f"     [!] Declaring fake blobSize = {self._start_size_override:,} "
                         f"(actual = {blob_len:,}) to bypass firmware size check")
            self.log(f"     START payload ({len(start_payload)}B): {start_payload.hex()}")
            await self._send_wt(WT_CMD_START, start_payload)

            # ── Step 2: Wait for device "ready" response ──────────────────────────
            self.log("\n[2] Waiting for device START acknowledgement...")
            resp = await self._wait_wt_response(timeout=15.0, context="START ack")
            if resp is None:
                self.log("  (timeout on START ack)")
                continue
            kind, val = resp
            if kind == "error" and val == 4:
                self.log(f"  Device returned code 4 (errorCode 1009 — busy/locked). Will retry.")
                continue
            # Got a real response — break out of retry loop
            start_chunk = (kind, val)
            break

        if start_chunk is None:
            self.log("ERROR: device did not acknowledge START after retries.")
            return False

        kind, val = start_chunk
        if kind == "rejected":
            limit_mb = val / 1024 / 1024
            blob_mb  = blob_len / 1024 / 1024
            self.log(f"\nERROR: Device rejected upload — blob too large.")
            self.log(f"  Blob size:    {blob_mb:.1f} MB ({blob_len:,} bytes)")
            self.log(f"  Device limit: ~{limit_mb:.1f} MB ({val:,} bytes) [inferred from response code]")
            self.log(f"\nTo fix, re-encode with smaller output. Examples:")
            self.log(f"  --duration 7        trim to 7 seconds")
            self.log(f"  --quality 20        lower quality (higher number = smaller file)")
            self.log(f"  --fps 15            15 fps instead of 24")
            self.log(f"  --resolution 320x320  lower resolution")
            return False
        if kind == "error":
            self.log(f"ERROR: device returned error code {val}.")
            return False
        if kind == "success":
            self.log("WARNING: device reported success immediately (unexpected).")
            return True
        # kind == "progress", val = normalized chunk number (0 = start from beginning)
        start_chunk = val  # normalized=0 → start from chunk 1 (index 0)
        # (start_chunk shadows the outer loop variable — intended)
        self.log(f"     Device ready. Normalized={val}. Starting from chunk {start_chunk+1}.")

        # ── Step 3: Send data chunks (stop-and-wait) ──────────────────────────
        # Protocol (from lt2.Y() → O() → device N()):
        #   Phone sends chunk N (1-based index).
        #   Device ACKs with responseCode = 1000 + N (normalized = N).
        #   Phone then sends chunk N+1. Repeat until all chunks sent.
        #   After last chunk, phone sends FINISH.
        # When fake-size is active, only send up to declared_size bytes.
        # Sending MORE than declared causes a buffer overflow in firmware → crash loop.
        send_limit = self._start_size_override if self._start_size_override is not None else blob_len
        if send_limit > blob_len:
            send_limit = blob_len
        self.log(f"\n[3] Sending {send_limit:,} bytes in {WT_CHUNK_SIZE}-byte chunks "
                 f"({'declared limit' if self._start_size_override else 'full blob'})...")
        total_chunks = (send_limit + WT_CHUNK_SIZE - 1) // WT_CHUNK_SIZE
        t0 = time.monotonic()

        chunk_idx = start_chunk  # 0-based, will send chunk (chunk_idx+1)

        while chunk_idx < total_chunks:
            chunk_no    = chunk_idx + 1  # 1-based index sent in packet
            chunk_start = chunk_idx * WT_CHUNK_SIZE
            chunk_end   = min(chunk_start + WT_CHUNK_SIZE, send_limit)
            chunk_data  = blob[chunk_start:chunk_end]

            pkt_payload = build_wt_chunk(chunk_no, chunk_data)
            await self._send_wt(WT_CMD_DATA, pkt_payload)

            # Wait for device ACK
            resp = await self._wait_wt_response(timeout=15.0, context=f"chunk {chunk_no}")
            if resp is None:
                self.log(f"ERROR: timeout on chunk {chunk_no}/{total_chunks}")
                return False

            kind, val = resp
            if kind == "success":
                self.log("     Device reported success mid-transfer!")
                return True
            if kind == "error":
                self.log(f"ERROR: device error code {val} on chunk {chunk_no}")
                return False

            # kind == "progress", val = chunk number ACK'd by device
            acked_chunk = val  # should equal chunk_no
            if acked_chunk != chunk_no:
                self.log(f"     [WARN] expected ACK for chunk {chunk_no}, "
                         f"got {acked_chunk} — adjusting")
                chunk_idx = max(0, acked_chunk)  # resync
            else:
                chunk_idx += 1

            if chunk_idx % 5 == 0 or chunk_idx >= total_chunks:
                elapsed = time.monotonic() - t0
                bytes_done = min(chunk_idx * WT_CHUNK_SIZE, blob_len)
                pct  = bytes_done * 100 // blob_len
                kbps = (bytes_done / 1024) / max(elapsed, 0.001)
                self.log(f"     Progress: chunk {chunk_idx}/{total_chunks} ({pct}%)  "
                         f"{kbps:.0f} KB/s")

        elapsed = time.monotonic() - t0
        self.log(f"     All data sent in {elapsed:.1f}s")

        # ── Step 4: Send FINISH ───────────────────────────────────────────────
        # Payload = 4-byte BE sum of ALL blob bytes (confirmed from lt2.k() smali)
        self.log("\n[4] Sending WatchTheme FINISH (0x1f cmd=0x03)...")
        finish_payload = build_wt_finish_payload(blob, self._start_size_override)
        checksum_range = (f"first {self._start_size_override:,} bytes"
                         if self._start_size_override else "full blob")
        self.log(f"     blob checksum = {struct.unpack('>I', finish_payload)[0]:#010x}"
                 f"  (over {checksum_range})")
        await self._send_wt(WT_CMD_FINISH, finish_payload)

        # ── Step 5: Wait for success ──────────────────────────────────────────
        self.log("\n[5] Waiting for device SUCCESS (responseCode=2)...")
        resp = await self._wait_wt_response(timeout=20.0, context="FINISH ack")
        if resp is None:
            self.log("WARNING: timeout waiting for final SUCCESS. Video may or may not be saved.")
            return False
        kind, val = resp
        if kind == "success":
            self.log("\nSUCCESS — video registered in device gallery!")
            return True
        self.log(f"WARNING: unexpected final response kind={kind} val={val}.")
        return False

    async def _wait_wt_response(self, timeout: float, context: str = ""):
        """
        Wait for a 0x1f device response.

        Response codes (from lt2 constructor, this.l=[1000,100M), this.m=[100M,200M)):
          [1000, 100M)  → progress; normalized = code - 1000 = chunk-number ACK'd
          2             → success/done
          1             → check failed
          others        → error or unexpected

        Returns:
          ("progress", normalized)  — chunk ACK; normalized = chunk number ACK'd (1-based)
          ("success",  None)        — upload complete
          ("error",    code)        — device error
          None                      — timeout
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(self._recv_raw(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                if context:
                    self.log(f"  [WARN] no packet in 5s ({context}), waiting...")
                continue  # try again until overall deadline expires

            self.log(f"  << {len(raw)}B: {raw.hex()}")

            code = parse_wt_response(raw)
            if code is not None:
                if code == "ack":
                    self.log(f"     WatchTheme short ACK (0x1f cmd packet)")
                    continue  # wait for the actual responseCode packet
                self.log(f"     WatchTheme responseCode = {code}")
                if code == WT_RESP_SUCCESS:
                    return ("success", None)
                if code == WT_RESP_CHECK_FAIL:
                    self.log("ERROR: device reported check failure!")
                    return ("error", code)
                if WT_RESP_RANGE_L0 <= code < WT_RESP_RANGE_L1:
                    normalized = code - WT_RESP_RANGE_L0
                    self.log(f"     Normalized = {normalized} (chunk ACK)")
                    return ("progress", normalized)
                if WT_RESP_M_RANGE_LO <= code < WT_RESP_M_RANGE_HI:
                    # this.m range: decoded by lt2.u(J) via string parsing
                    # str(code)[1] == '0' → type-0: fatal error (V(1010,...) in app)
                    # str(code)[1] == '1' → type-1: device rejects silently (too large?)
                    s = str(code)
                    m_type = int(s[1])
                    m_value = int(s[2:])
                    if m_type == 0:
                        self.log(f"     Device returned fatal error (this.m type-0, "
                                 f"errorCode=1010, value={m_value})")
                        return ("rejected", m_value)
                    else:
                        self.log(f"     Device rejected upload (this.m type-{m_type}, "
                                 f"value={m_value:,}). Likely blob too large.")
                        return ("rejected", m_value)
                self.log(f"     [WARN] unexpected WatchTheme code {code}")
                return ("error", code)

            # 0x25 packets — respond to DEVICE_INFO_REQUEST instead of ignoring.
            # Device may send multiple rounds of DEVICE_INFO_REQUEST (e.g. in charging mode)
            # and reject WatchTheme uploads if we don't respond to each one.
            parsed = parse_25_packet(raw)
            if parsed:
                mod, cmd, payload = parsed
                self.log(f"  (0x25 packet: mod={mod:#04x} cmd={cmd:#04x} payload={payload.hex()})")
                if mod == MOD_SYSTEM_INFO and cmd == CMD_DEVICE_INFO_REQUEST:
                    self.log(f"    → Responding to DEVICE_INFO_REQUEST mid-transfer...")
                    await self._send_25(MOD_SYSTEM_INFO, CMD_DEVICE_INFO_RESPONSE,
                                        build_device_info_payload())
                continue

            self.log(f"  [WARN] unrecognized packet: {raw.hex()}")

        return None


# ── Device discovery ──────────────────────────────────────────────────────────

async def find_device(name_or_addr: str):
    print(f"Scanning for device: {name_or_addr!r} ...")
    is_addr = len(name_or_addr) == 17 and name_or_addr.count(":") == 5
    if is_addr:
        return await BleakScanner.find_device_by_address(name_or_addr, timeout=30.0)
    return await BleakScanner.find_device_by_filter(
        lambda d, _: name_or_addr.lower() in (d.name or "").lower(),
        timeout=30.0,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Upload video to BLE video keychain/badge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("device", help="Device name (BW01) or MAC address")
    parser.add_argument("video",  help="Input video file (any format ffmpeg supports)")
    parser.add_argument("--fps",        type=int,   default=24,  help="FPS (default: 24)")
    parser.add_argument("--quality",    type=int,   default=10,  help="MJPEG quality 2-31 (default: 10, higher=smaller)")
    parser.add_argument("--duration",   type=float, default=None,help="Trim to N seconds")
    parser.add_argument("--resolution", type=str,   default="480x480", help="Output WxH (default: 480x480)")
    parser.add_argument("--no-audio",   action="store_true",     help="Drop audio track")
    parser.add_argument("--audio-rate", type=int,   default=16000,
                        help="Audio sample rate Hz (default: 16000). 8000 = phone quality, half size.")
    parser.add_argument("--start-size", type=int, default=None,
                        help="Override blobSize declared in START packet (bytes). "
                             "Use to bypass firmware size check: declare small value, "
                             "send actual larger blob. Try: --start-size 1000000")

    args = parser.parse_args()
    name_or_addr = args.device
    video_file   = Path(args.video)

    try:
        res_w, res_h = (int(x) for x in args.resolution.lower().split("x"))
    except Exception:
        print(f"ERROR: invalid --resolution format: {args.resolution!r}. Use WxH, e.g. 320x320")
        sys.exit(1)

    if not video_file.exists():
        print(f"ERROR: file not found: {video_file}")
        sys.exit(1)

    # ── Preprocess video ──────────────────────────────────────────────────────
    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path is None:
        print("ERROR: ffmpeg not found.")
        sys.exit(1)

    print(f"FFmpeg: {ffmpeg_path}")
    tmp_dir  = Path(tempfile.gettempdir())
    # Include quality/fps/duration in filename so re-runs don't reuse wrong cache
    suffix = f"_q{args.quality}_fps{args.fps}_ar{args.audio_rate}"
    if args.duration:
        suffix += f"_t{args.duration}"
    prepared = tmp_dir / f"badge_upload_{video_file.stem}{suffix}.avi"

    # Skip conversion if input is already a compatible AVI and no options changed defaults
    if (video_file.suffix.lower() == ".avi" and
            args.fps == 24 and args.quality == 10 and
            args.duration is None and not args.no_audio and
            args.resolution == "480x480"):
        print(f"\n[pre] Input is already .avi with default settings — skipping conversion.")
        prepared = video_file
    else:
        print(f"\n[pre] Preparing video -> {prepared}")
        print(f"      ({res_w}x{res_h}, {args.fps}fps, MJPEG q={args.quality}, "
              f"{'no audio' if args.no_audio else 'PCM audio'}"
              f"{f', trim {args.duration}s' if args.duration else ''})")
        if not prepare_video(video_file, prepared,
                             width=res_w, height=res_h,
                             fps=args.fps, quality=args.quality,
                             duration=args.duration, no_audio=args.no_audio,
                             audio_rate=args.audio_rate,
                             ffmpeg=ffmpeg_path):
            print("ERROR: FFmpeg failed.")
            sys.exit(1)
        sz = prepared.stat().st_size
        blob_sz = sz + 4
        print(f"      Done. {sz:,} bytes (blob: {blob_sz:,} bytes)")
        DEVICE_LIMIT_BYTES = 1_740_798  # confirmed from device type-0 response code (101740798)
        if blob_sz > DEVICE_LIMIT_BYTES:
            over_pct = (blob_sz - DEVICE_LIMIT_BYTES) * 100 // DEVICE_LIMIT_BYTES
            print(f"\n  WARNING: blob ({blob_sz/1024/1024:.1f} MB) exceeds estimated device limit "
                  f"({DEVICE_LIMIT_BYTES/1024/1024:.1f} MB) by {over_pct}%.")
            print(f"  The device will likely reject this upload.")
            print(f"  Try: --quality 20, --fps 15, --duration 7, or --resolution 320x320")

    # ── Connect ───────────────────────────────────────────────────────────────
    device = await find_device(name_or_addr)
    if device is None:
        print(f"ERROR: device not found: {name_or_addr!r}")
        sys.exit(1)
    print(f"Found: {device.name!r}  ({device.address})")

    async with BleakClient(device, timeout=30.0) as client:
        mtu = getattr(client, "mtu_size", 512)
        print(f"Connected. MTU={mtu}")

        # List all services
        print("\n--- BLE Services & Characteristics ---")
        for svc in client.services:
            print(f"  Service: {svc.uuid}")
            for ch in svc.characteristics:
                print(f"    Char:  {ch.uuid}  [{','.join(ch.properties)}]")
        print("--------------------------------------\n")

        uploader = VideoUploader(client, write_uuid=WRITE_UUID, notify_uuid=NOTIFY_UUID,
                                 start_size_override=args.start_size)
        uploader._mtu_payload = max(20, mtu - 3)
        success = await uploader.upload(prepared)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
