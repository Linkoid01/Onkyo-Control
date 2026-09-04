#!/usr/bin/env python3
"""
Onkyo TX-NR709 Control App
---------------------------
Controls Main and Zone 2 power/volume over the network using Onkyo's
eISCP protocol (TCP port 60128). Pure standard library - no pip installs
required. Works on Windows, macOS, and Linux (needs Python 3.7+ with tkinter).

On the receiver, enable: Setup -> Hardware -> Network -> Network Control
(otherwise it won't respond to commands while in standby).
"""

import json
import os
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

EISCP_PORT = 60128
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".onkyo_control.json")

# Full input source list documented for the TX-NR709 (SLI command, from the
# onkyo-eiscp project's protocol reference). Zone 2 uses the same codes via
# the SLZ command; not every source is guaranteed to be selectable on Zone 2
# specifically, and a handful of entries (Airplay/Bluetooth/DAB/Strm Box) are
# protocol placeholders that may not correspond to a real feature on this
# particular unit's hardware/firmware. The receiver will simply not switch
# if a code isn't actually supported.
INPUT_SOURCES = [
    ("00", "VIDEO1 / VCR/DVR"),
    ("01", "VIDEO2 / CBL/SAT"),
    ("02", "VIDEO3 / GAME"),
    ("03", "VIDEO4 / AUX1"),
    ("04", "VIDEO5 / AUX2"),
    ("05", "VIDEO6 / PC"),
    ("06", "VIDEO7"),
    ("07", "Hidden1 / EXTRA1"),
    ("08", "Hidden2 / EXTRA2"),
    ("09", "Hidden3 / EXTRA3"),
    ("10", "DVD / BD"),
    ("11", "STRM BOX"),
    ("12", "TV"),
    ("20", "TAPE(1) / TV-TAPE"),
    ("21", "TAPE2"),
    ("22", "PHONO"),
    ("23", "CD / TV-CD"),
    ("24", "FM"),
    ("25", "AM"),
    ("26", "TUNER"),
    ("27", "MUSIC SERVER / DLNA"),
    ("28", "INTERNET RADIO"),
    ("29", "USB (Front)"),
    ("2A", "USB (Rear)"),
    ("2B", "NETWORK"),
    ("2C", "USB (toggle)"),
    ("2D", "AirPlay"),
    ("2E", "Bluetooth"),
    ("30", "MULTI CH"),
    ("31", "XM"),
    ("32", "SIRIUS"),
    ("33", "DAB"),
    ("40", "Universal PORT"),
    ("55", "HDMI 5"),
    ("56", "HDMI 6"),
    ("57", "HDMI 7"),
]
SOURCE_LABEL_TO_CODE = {label: code for code, label in INPUT_SOURCES}
SOURCE_CODE_TO_LABEL = {code: label for code, label in INPUT_SOURCES}

# ---------------------------------------------------------------------------
# eISCP protocol helpers
# ---------------------------------------------------------------------------

def build_packet(command: str) -> bytes:
    """Wrap an ISCP command string (e.g. 'PWR01') in an eISCP TCP frame."""
    iscp_msg = f"!1{command}\r"
    data = iscp_msg.encode("ascii")
    header = struct.pack(
        "!4sIIcxxx",
        b"ISCP",
        16,          # header size
        len(data),   # data size
        b"\x01",     # version
    )
    return header + data


def parse_packet(raw: bytes) -> str:
    """Extract the ISCP command string from a single raw eISCP TCP frame."""
    if len(raw) < 16 or raw[:4] != b"ISCP":
        return ""
    header_size = struct.unpack("!I", raw[4:8])[0]
    data_size = struct.unpack("!I", raw[8:12])[0]
    data = raw[header_size:header_size + data_size]
    # data looks like: !1PWR01\x1a\r\n  (strip leading '!1' and trailing junk)
    text = data.decode("ascii", errors="ignore")
    if text[:2] in ("!1", "!x"):
        text = text[2:]
    text = text.strip("\x1a\r\n\x00 ")
    return text


def extract_frames(buffer: bytes):
    """Split a byte buffer into complete eISCP frames plus any leftover bytes."""
    frames = []
    while True:
        if len(buffer) < 16:
            break
        if buffer[:4] != b"ISCP":
            idx = buffer.find(b"ISCP", 1)
            if idx == -1:
                buffer = b""
                break
            buffer = buffer[idx:]
            continue
        header_size = struct.unpack("!I", buffer[4:8])[0]
        data_size = struct.unpack("!I", buffer[8:12])[0]
        total_size = header_size + data_size
        if total_size <= 0 or len(buffer) < total_size:
            break
        frames.append(buffer[:total_size])
        buffer = buffer[total_size:]
    return frames, buffer


class ReceiverConnection:
    """A small persistent-ish TCP connection to the receiver."""

    def __init__(self, host: str, port: int = EISCP_PORT, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send_command(self, command: str, read_response: bool = True, expect_prefix: str = None) -> str:
        """Send a command. If read_response is True, read messages from the
        receiver until one matching expect_prefix arrives (or, if
        expect_prefix is None, return the first message received). A
        receiver in network standby can send unrelated status messages
        interleaved with the actual reply, so we don't stop at the first one
        unless it's the one we asked for."""
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(build_packet(command))
            if not read_response:
                return ""

            buffer = b""
            fallback = ""
            deadline = time.time() + self.timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buffer += chunk
                frames, buffer = extract_frames(buffer)
                for frame in frames:
                    text = parse_packet(frame)
                    if not text:
                        continue
                    if expect_prefix is None or text.startswith(expect_prefix):
                        return text
                    if not fallback:
                        fallback = text
            return fallback


def discover_receivers(timeout: float = 3.0):
    """Broadcast an eISCP discovery packet and collect responses."""
    msg = "!xECNQSTN"
    data = msg.encode("ascii")
    header = struct.pack("!4sIIcxxx", b"ISCP", 16, len(data), b"\x01")
    packet = header + data

    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, ("255.255.255.255", EISCP_PORT))
        start = time.time()
        while time.time() - start < timeout:
            try:
                raw, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            text = parse_packet(raw)
            # response looks like: ECNmodel_name/port/area/id
            if text.startswith("ECN"):
                parts = text[3:].split("/")
                model = parts[0] if parts else "Unknown"
                found[addr[0]] = model
    finally:
        sock.close()
    return found


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class OnkyoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Onkyo TX-NR709 Control")
        self.resizable(False, False)
        self.host = None
        self.main_volume = tk.IntVar(value=40)
        self.zone2_volume = tk.IntVar(value=30)
        self.status_var = tk.StringVar(value="Not connected")
        self.main_power_state = tk.StringVar(value="\u25cf Unknown")
        self.zone2_power_state = tk.StringVar(value="\u25cf Unknown")
        self.power_labels = {}  # is_zone2 -> ttk.Label
        self.main_mute_state = tk.StringVar(value="\u25cf Unknown")
        self.zone2_mute_state = tk.StringVar(value="\u25cf Unknown")
        self.mute_labels = {}  # is_zone2 -> ttk.Label
        self.main_source = tk.StringVar(value="")
        self.zone2_source = tk.StringVar(value="")
        self.source_combos = {}  # is_zone2 -> ttk.Combobox

        self._load_config()
        self._build_ui()

        if self.host:
            self.status_var.set(f"Configured: {self.host}")
            self.after(300, lambda: self._query_power(False))
            self.after(300, lambda: self._query_power(True))
            self.after(300, lambda: self._query_volume(False))
            self.after(300, lambda: self._query_volume(True))
            self.after(300, lambda: self._query_mute(False))
            self.after(300, lambda: self._query_mute(True))
            self.after(300, lambda: self._query_source(False))
            self.after(300, lambda: self._query_source(True))

    # ---- config ----
    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    cfg = json.load(f)
                    self.host = cfg.get("host")
            except Exception:
                self.host = None

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump({"host": self.host}, f)
        except Exception:
            pass

    # ---- UI ----
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        conn_frame = ttk.LabelFrame(self, text="Receiver")
        conn_frame.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        self.host_entry = ttk.Entry(conn_frame, width=20)
        self.host_entry.grid(row=0, column=0, padx=6, pady=6)
        if self.host:
            self.host_entry.insert(0, self.host)
        else:
            self.host_entry.insert(0, "receiver IP")

        ttk.Button(conn_frame, text="Set IP", command=self._set_host).grid(row=0, column=1, padx=4)
        ttk.Button(conn_frame, text="Discover", command=self._discover).grid(row=0, column=2, padx=4)
        ttk.Button(conn_frame, text="Fetch Status", command=self._fetch_status).grid(row=0, column=3, padx=4)

        ttk.Label(conn_frame, textvariable=self.status_var, foreground="gray").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6)
        )

        main_frame = self._zone_frame("Main Zone", is_zone2=False)
        main_frame.grid(row=1, column=0, sticky="nsew", **pad)

        zone2_frame = self._zone_frame("Zone 2", is_zone2=True)
        zone2_frame.grid(row=1, column=1, sticky="nsew", **pad)

    def _zone_frame(self, title, is_zone2):
        frame = ttk.LabelFrame(self, text=title)

        state_var = self.zone2_power_state if is_zone2 else self.main_power_state
        status_label = ttk.Label(frame, textvariable=state_var, foreground="gray")
        status_label.pack(pady=(6, 0))
        self.power_labels[is_zone2] = status_label

        power_row = ttk.Frame(frame)
        power_row.pack(padx=10, pady=8)
        ttk.Button(power_row, text="Power On",
                   command=lambda: self._power(is_zone2, True)).pack(side="left", padx=4)
        ttk.Button(power_row, text="Power Off",
                   command=lambda: self._power(is_zone2, False)).pack(side="left", padx=4)

        var = self.zone2_volume if is_zone2 else self.main_volume
        vol_label = ttk.Label(frame, text="Volume")
        vol_label.pack(pady=(4, 0))

        vol_row = ttk.Frame(frame)
        vol_row.pack(padx=10, pady=4)
        ttk.Button(vol_row, text="-", width=3,
                   command=lambda: self._volume_step(is_zone2, -1)).pack(side="left")
        scale = ttk.Scale(vol_row, from_=0, to=100, orient="horizontal", length=160,
                           variable=var, command=lambda v: self._volume_set(is_zone2, int(float(v))))
        scale.pack(side="left", padx=6)
        ttk.Button(vol_row, text="+", width=3,
                   command=lambda: self._volume_step(is_zone2, 1)).pack(side="left")

        ttk.Label(frame, textvariable=var).pack(pady=(0, 6))

        mute_state_var = self.zone2_mute_state if is_zone2 else self.main_mute_state
        mute_status_label = ttk.Label(frame, textvariable=mute_state_var, foreground="gray")
        mute_status_label.pack(pady=(2, 0))
        self.mute_labels[is_zone2] = mute_status_label

        mute_row = ttk.Frame(frame)
        mute_row.pack(padx=10, pady=(4, 8))
        ttk.Button(mute_row, text="Mute", width=8,
                   command=lambda: self._mute(is_zone2, True)).pack(side="left", padx=4)
        ttk.Button(mute_row, text="Unmute", width=8,
                   command=lambda: self._mute(is_zone2, False)).pack(side="left", padx=4)

        ttk.Label(frame, text="Source").pack(pady=(0, 2))
        source_var = self.zone2_source if is_zone2 else self.main_source
        combo = ttk.Combobox(frame, textvariable=source_var, state="readonly",
                              values=[label for _, label in INPUT_SOURCES], width=20)
        combo.pack(padx=10, pady=(0, 10))
        combo.bind("<<ComboboxSelected>>",
                   lambda event: self._select_source(is_zone2, source_var.get()))
        self.source_combos[is_zone2] = combo

        return frame

    # ---- actions ----
    def _set_host(self):
        self.host = self.host_entry.get().strip()
        self._save_config()
        self.status_var.set(f"Configured: {self.host}")
        self._query_power(False)
        self._query_power(True)
        self._query_volume(False)
        self._query_volume(True)
        self._query_mute(False)
        self._query_mute(True)
        self._query_source(False)
        self._query_source(True)

    def _discover(self):
        self.status_var.set("Discovering...")
        self.update_idletasks()

        def worker():
            found = discover_receivers()
            if not found:
                self.status_var.set("No receivers found on this network")
                return
            ip = list(found.keys())[0]
            model = found[ip]
            self.host = ip
            self.host_entry.delete(0, tk.END)
            self.host_entry.insert(0, ip)
            self._save_config()
            self.status_var.set(f"Found {model} at {ip}")
            self._query_power(False)
            self._query_power(True)
            self._query_volume(False)
            self._query_volume(True)
            self._query_mute(False)
            self._query_mute(True)
            self._query_source(False)
            self._query_source(True)

        threading.Thread(target=worker, daemon=True).start()

    def _require_host(self):
        if not self.host:
            messagebox.showwarning("No receiver", "Set the receiver's IP address first (or click Discover).")
            return False
        return True

    def _fetch_status(self):
        if not self._require_host():
            return
        self.status_var.set("Fetching status...")
        self._query_power(False)
        self._query_power(True)
        self._query_volume(False)
        self._query_volume(True)
        self._query_mute(False)
        self._query_mute(True)
        self._query_source(False)
        self._query_source(True)
        self.after(1500, lambda: self.status_var.set(f"Status updated: {self.host}"))

    def _send(self, command):
        if not self._require_host():
            return
        conn = ReceiverConnection(self.host)

        def worker():
            try:
                conn.send_command(command, read_response=False)
            except (socket.timeout, OSError) as e:
                self.status_var.set(f"Error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _power(self, is_zone2, on):
        prefix = "ZPW" if is_zone2 else "PWR"
        self._send(f"{prefix}{'01' if on else '00'}")
        # Optimistic update, then confirm against the receiver shortly after
        # (some AVRs take a moment to actually report the new state).
        self._update_power_indicator(is_zone2, on)
        self.after(1200, lambda: self._query_power(is_zone2))
        if on:
            self.after(1200, lambda: self._query_volume(is_zone2))

    def _query_power(self, is_zone2):
        if not self.host:
            return
        prefix = "ZPW" if is_zone2 else "PWR"
        conn = ReceiverConnection(self.host)

        def worker():
            state = None
            try:
                resp = conn.send_command(f"{prefix}QSTN", read_response=True, expect_prefix=prefix)
                if resp.startswith(prefix):
                    state = resp[len(prefix):len(prefix) + 2] == "01"
            except (socket.timeout, OSError):
                state = None
            self.after(0, lambda: self._update_power_indicator(is_zone2, state))

        threading.Thread(target=worker, daemon=True).start()

    def _update_power_indicator(self, is_zone2, on):
        var = self.zone2_power_state if is_zone2 else self.main_power_state
        label = self.power_labels.get(is_zone2)
        if on is True:
            var.set("\u25cf On")
            color = "#1a7a1a"
        elif on is False:
            var.set("\u25cf Off")
            color = "#a02020"
        else:
            var.set("\u25cf Unknown")
            color = "gray"
        if label is not None:
            label.configure(foreground=color)

    def _mute(self, is_zone2, mute_on):
        prefix = "ZMT" if is_zone2 else "AMT"
        self._send(f"{prefix}{'01' if mute_on else '00'}")
        self._update_mute_indicator(is_zone2, mute_on)
        self.after(800, lambda: self._query_mute(is_zone2))

    def _query_mute(self, is_zone2):
        if not self.host:
            return
        prefix = "ZMT" if is_zone2 else "AMT"
        conn = ReceiverConnection(self.host)

        def worker():
            state = None
            try:
                resp = conn.send_command(f"{prefix}QSTN", read_response=True, expect_prefix=prefix)
                if resp.startswith(prefix):
                    state = resp[len(prefix):len(prefix) + 2] == "01"
            except (socket.timeout, OSError):
                state = None
            self.after(0, lambda: self._update_mute_indicator(is_zone2, state))

        threading.Thread(target=worker, daemon=True).start()

    def _update_mute_indicator(self, is_zone2, muted):
        var = self.zone2_mute_state if is_zone2 else self.main_mute_state
        label = self.mute_labels.get(is_zone2)
        if muted is True:
            var.set("\u25cf Muted")
            color = "#a02020"
        elif muted is False:
            var.set("\u25cf Not muted")
            color = "#1a7a1a"
        else:
            var.set("\u25cf Unknown")
            color = "gray"
        if label is not None:
            label.configure(foreground=color)

    def _select_source(self, is_zone2, label):
        code = SOURCE_LABEL_TO_CODE.get(label)
        if code is None:
            return
        prefix = "SLZ" if is_zone2 else "SLI"
        self._send(f"{prefix}{code}")
        self.after(800, lambda: self._query_source(is_zone2))

    def _query_source(self, is_zone2):
        if not self.host:
            return
        prefix = "SLZ" if is_zone2 else "SLI"
        conn = ReceiverConnection(self.host)

        def worker():
            label = None
            try:
                resp = conn.send_command(f"{prefix}QSTN", read_response=True, expect_prefix=prefix)
                if resp.startswith(prefix):
                    code = resp[len(prefix):len(prefix) + 2].upper()
                    label = SOURCE_CODE_TO_LABEL.get(code)
            except (socket.timeout, OSError):
                label = None
            if label is not None:
                self.after(0, lambda: self._update_source(is_zone2, label))

        threading.Thread(target=worker, daemon=True).start()

    def _update_source(self, is_zone2, label):
        var = self.zone2_source if is_zone2 else self.main_source
        var.set(label)

    def _query_volume(self, is_zone2):
        if not self.host:
            return
        prefix = "ZVL" if is_zone2 else "MVL"
        conn = ReceiverConnection(self.host)

        def worker():
            value = None
            try:
                resp = conn.send_command(f"{prefix}QSTN", read_response=True, expect_prefix=prefix)
                if resp.startswith(prefix):
                    hex_part = resp[len(prefix):]
                    try:
                        value = int(hex_part, 16)
                    except ValueError:
                        value = None
            except (socket.timeout, OSError):
                value = None
            if value is not None:
                self.after(0, lambda: self._update_volume(is_zone2, value))

        threading.Thread(target=worker, daemon=True).start()

    def _update_volume(self, is_zone2, value):
        var = self.zone2_volume if is_zone2 else self.main_volume
        var.set(max(0, min(100, value)))

    def _volume_set(self, is_zone2, value):
        # ttk.Scale writes raw float precision to its linked variable while
        # dragging; re-set it here as a clean int so the label doesn't show
        # long decimal noise.
        var = self.zone2_volume if is_zone2 else self.main_volume
        var.set(int(value))

        prefix = "ZVL" if is_zone2 else "MVL"
        self._send(f"{prefix}{value:02X}")

    def _volume_step(self, is_zone2, delta):
        var = self.zone2_volume if is_zone2 else self.main_volume
        new_val = max(0, min(100, var.get() + delta))
        var.set(new_val)
        self._volume_set(is_zone2, new_val)


if __name__ == "__main__":
    app = OnkyoApp()
    app.mainloop()