import gc
import struct
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk

try:
    import keyboard
except ImportError:
    keyboard = None

try:
    from vgamepad import VX360Gamepad, XUSB_BUTTON
except ImportError:
    VX360Gamepad = None
    XUSB_BUTTON = None

try:
    import pygame
except ImportError:
    pygame = None

PORT = 31000
SLED_SIZE = 232
CARDASH_SIZE = 311

# pygame XInput axis indices (Windows, SDL2)
# Triggers range: -1.0 (released) → 1.0 (fully pressed)
# Stick Y is positive-down in pygame, positive-up in XInput → must negate
LS_X, LS_Y = 0, 1
RS_X, RS_Y = 2, 3
LT_AXIS = 4
RT_AXIS = 5

# pygame button index → XUSB_BUTTON (XInput / Xbox layout)
_BTN_MAP = None  # built lazily once XUSB_BUTTON is confirmed available


def _build_btn_map():
    global _BTN_MAP
    if XUSB_BUTTON is None or _BTN_MAP is not None:
        return
    _BTN_MAP = [
        XUSB_BUTTON.XUSB_GAMEPAD_A,
        XUSB_BUTTON.XUSB_GAMEPAD_B,
        XUSB_BUTTON.XUSB_GAMEPAD_X,
        XUSB_BUTTON.XUSB_GAMEPAD_Y,
        XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        XUSB_BUTTON.XUSB_GAMEPAD_START,
        XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
        XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
    ]


def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def _axis_to_01(raw: float) -> float:
    return _clamp((raw + 1.0) / 2.0, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Telemetry packet parser
# ---------------------------------------------------------------------------

def parse_packet(data: bytes):
    if len(data) < SLED_SIZE:
        return None

    def f(off):
        return struct.unpack_from("<f", data, off)[0]

    def i32(off):
        return struct.unpack_from("<i", data, off)[0]

    vx, vy, vz = f(32), f(36), f(40)
    frame = {
        "is_race_on": i32(0),
        "engine_rpm": f(16),
        "vel_ms": (vx**2 + vy**2 + vz**2) ** 0.5,
        "slip_fl": f(84), "slip_fr": f(88),
        "slip_rl": f(92), "slip_rr": f(96),
        "speed_ms": None, "tele_throttle": None, "brake": None, "gear": None,
    }
    if len(data) >= CARDASH_SIZE:
        frame["speed_ms"] = f(244)
        frame["tele_throttle"] = data[303] / 255.0
        frame["brake"] = data[304] / 255.0
        frame["gear"] = data[307]
    return frame


# ---------------------------------------------------------------------------
# Virtual controller names to skip when choosing physical controller
# ---------------------------------------------------------------------------

_VIRTUAL_NAMES = ("xbox 360 controller", "xbox one controller", "vigem")


def _is_virtual(name: str) -> bool:
    low = name.lower()
    return any(v in low for v in _VIRTUAL_NAMES)


# ---------------------------------------------------------------------------
# Controller reader + full passthrough forwarder
# ---------------------------------------------------------------------------

class ControllerReader(threading.Thread):
    """
    Runs at ~100 Hz.
    - Reads all physical controller state (axes, buttons, hat/d-pad).
    - Forwards everything to the virtual gamepad as a passthrough.
    - RT is capped to throttle_cap when sc_enabled=True.
    """

    def __init__(self, virtual_gamepad):
        super().__init__(daemon=True)
        self._vgp = virtual_gamepad
        # Set by the stability logic (main thread); float write is GIL-atomic
        self.throttle_cap: float = 1.0
        self.sc_enabled: bool = False

        # Readable from UI thread
        self.throttle: float = 0.0       # raw RT 0-1
        self.brake: float = 0.0          # raw LT 0-1
        self.output_throttle: float = 0.0
        self.available: bool = False
        self.name_str: str = "No controller found"
        self.all_joysticks: list = []

        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._joystick = None
        self._pygame = None
        self._was_forwarding: bool = False  # track transitions to reset virtual on exit

    # ---- joystick selection ------------------------------------------------

    def scan(self, preferred_index: int = -1):
        if pygame is None:
            self.name_str = "pygame not installed"
            return
        try:
            if not pygame.get_init():
                pygame.init()
            pygame.joystick.quit()
            pygame.joystick.init()

            found = []
            for i in range(pygame.joystick.get_count()):
                try:
                    js = pygame.joystick.Joystick(i)
                    js.init()
                    found.append((i, js.get_name(), js))
                except Exception:
                    pass

            self.all_joysticks = [(i, name) for i, name, _ in found]

            if not found:
                with self._lock:
                    self._joystick = None
                    self.available = False
                    self.name_str = "No controllers found"
                    self._pygame = None
                return

            chosen = None
            if 0 <= preferred_index < len(found):
                chosen = found[preferred_index]
            else:
                for entry in found:
                    if not _is_virtual(entry[1]):
                        chosen = entry
                        break
                if chosen is None:
                    chosen = found[0]

            with self._lock:
                self._joystick = chosen[2]
                self._pygame = pygame
                self.name_str = f"[{chosen[0]}] {chosen[1]}"
                self.available = True

        except Exception as exc:
            with self._lock:
                self._joystick = None
                self.available = False
                self.name_str = f"pygame error: {exc}"

    # ---- main loop ---------------------------------------------------------

    def run(self):
        _build_btn_map()
        self.scan()
        while not self.stop_event.is_set():
            with self._lock:
                js = self._joystick
                pg = self._pygame
                ok = self.available
            if ok and js is not None and pg is not None:
                try:
                    pg.event.pump()
                    self._read_and_forward(js)
                except Exception:
                    pass
            time.sleep(0.01)

    def _read_and_forward(self, js):
        n_axes = js.get_numaxes()
        axes = [js.get_axis(i) for i in range(n_axes)]

        rt_raw = _axis_to_01(axes[RT_AXIS]) if n_axes > RT_AXIS else 0.0
        lt_raw = _axis_to_01(axes[LT_AXIS]) if n_axes > LT_AXIS else 0.0
        self.throttle = rt_raw
        self.brake = lt_raw

        # Only forward to virtual controller while actively limiting.
        # When not limiting, the physical controller talks to the game directly —
        # forwarding would double every button press.
        actively_limiting = self.sc_enabled and self.throttle_cap < 1.0

        if not actively_limiting:
            self.output_throttle = rt_raw
            if self._was_forwarding:
                # Transition: release everything on the virtual controller
                self._reset_virtual()
                self._was_forwarding = False
            return

        # --- forwarding active from here ---
        self._was_forwarding = True
        rt_out = min(rt_raw, self.throttle_cap)
        self.output_throttle = rt_out

        if self._vgp is None:
            return
        gp = self._vgp

        # Sticks (negate Y: pygame positive-down → XInput positive-up)
        lx = _clamp(axes[LS_X]) if n_axes > LS_X else 0.0
        ly = _clamp(-axes[LS_Y]) if n_axes > LS_Y else 0.0
        gp.left_joystick_float(x_value_float=lx, y_value_float=ly)

        rx = _clamp(axes[RS_X]) if n_axes > RS_X else 0.0
        ry = _clamp(-axes[RS_Y]) if n_axes > RS_Y else 0.0
        gp.right_joystick_float(x_value_float=rx, y_value_float=ry)

        # Triggers
        gp.left_trigger_float(value_float=lt_raw)
        gp.right_trigger_float(value_float=rt_out)

        # Buttons
        if _BTN_MAP:
            n_btn = js.get_numbuttons()
            for i, xbtn in enumerate(_BTN_MAP):
                if i < n_btn and js.get_button(i):
                    gp.press_button(xbtn)
                else:
                    gp.release_button(xbtn)

        # D-pad
        if js.get_numhats() > 0:
            hx, hy = js.get_hat(0)
            _hat(gp, XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,    hy > 0)
            _hat(gp, XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,  hy < 0)
            _hat(gp, XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,  hx < 0)
            _hat(gp, XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT, hx > 0)

        gp.update()

    def _reset_virtual(self):
        if self._vgp is None:
            return
        try:
            self._vgp.reset()
            self._vgp.update()
        except Exception:
            pass

    def stop(self):
        self.stop_event.set()


def _hat(gp, btn, pressed: bool):
    if XUSB_BUTTON is None:
        return
    if pressed:
        gp.press_button(btn)
    else:
        gp.release_button(btn)


# ---------------------------------------------------------------------------
# UDP telemetry receiver
# ---------------------------------------------------------------------------

class TelemetryReceiver(threading.Thread):
    def __init__(self, packet_queue, port=PORT):
        super().__init__(daemon=True)
        self.packet_queue = packet_queue
        self.port = port
        self.stop_event = threading.Event()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(1.0)
        while not self.stop_event.is_set():
            try:
                data, _ = sock.recvfrom(65536)
                self.packet_queue.put(data)
            except socket.timeout:
                continue
            except OSError:
                break
        sock.close()

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class StabilityControlApp:
    def __init__(self):
        self.packet_queue = queue.Queue()
        self.receiver = TelemetryReceiver(self.packet_queue)
        self.receiver.start()

        # Virtual gamepad is created ONLY when SC is turned ON and destroyed when
        # turned OFF so it never appears as a second XInput device during normal play.
        self.virtual_gamepad = None

        self.controller = ControllerReader(None)
        self.controller.start()

        self.enabled = False
        self.last_frame = None
        self.max_throttle = 1.0
        self.is_limiting = False
        self.packets_received = 0
        self._last_pkt_size = 0
        self.strength = 1.0       # 0.0–1.0 from the slider
        self.smoothed_cap = 1.0   # exponentially-smoothed cap value

        self.root = tk.Tk()
        self.root.title("FH6 Stability Control")
        self.root.geometry("520x430")
        self.root.resizable(False, False)

        self._build_ui()

        if keyboard:
            keyboard.add_hotkey("f8", self.toggle_enabled)
        else:
            print("WARNING: 'keyboard' module not installed — use the button instead.")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._update_loop)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        BG, FG = "#1e1e1e", "#e0e0e0"
        MONO = ("Courier New", 10)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel",        background=BG, foreground=FG)
        style.configure("Status.TLabel", background=BG, foreground="#4fc3f7",
                        font=("Segoe UI", 13, "bold"))
        style.configure("Warn.TLabel",   background=BG, foreground="#ef9a9a",
                        font=("Segoe UI", 11, "bold"))
        style.configure("TButton", padding=6)
        style.configure("TFrame",  background=BG)
        style.configure("Sep.TFrame", background="#444444")
        self.root.configure(bg=BG)

        def sep():
            ttk.Frame(self.root, height=1, style="Sep.TFrame").pack(
                fill="x", padx=14, pady=5)

        def grid_row(parent, label, col, row):
            ttk.Label(parent, text=label, foreground="#888888", font=MONO).grid(
                row=row, column=col * 2, sticky="w", padx=(0, 4))
            var = tk.StringVar(value="—")
            ttk.Label(parent, textvariable=var, font=MONO, foreground=FG).grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 20))
            return var

        # Status + toggle
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(12, 4))
        self.status_lbl = ttk.Label(top, text="Status: OFF", style="Status.TLabel")
        self.status_lbl.pack(side="left")
        self.toggle_btn = ttk.Button(top, text="Turn ON  [F8]", command=self.toggle_enabled)
        self.toggle_btn.pack(side="right")

        sep()

        # Strength slider
        sr = ttk.Frame(self.root)
        sr.pack(fill="x", padx=14, pady=(0, 2))
        ttk.Label(sr, text="Strength", foreground="#888888",
                  font=("Segoe UI", 9)).pack(side="left")
        self._strength_var = tk.DoubleVar(value=1.0)
        ttk.Scale(sr, from_=0.0, to=1.0, orient="horizontal",
                  variable=self._strength_var,
                  command=self._on_strength_change).pack(
                      side="left", fill="x", expand=True, padx=(8, 8))
        self._strength_lbl = ttk.Label(sr, text="100%", width=5,
                                       font=("Courier New", 10))
        self._strength_lbl.pack(side="left")

        sep()

        # Telemetry
        tg = ttk.Frame(self.root)
        tg.pack(fill="x", padx=14)
        ttk.Label(tg, text="TELEMETRY", foreground="#888888",
                  font=("Segoe UI", 8)).grid(row=0, column=0, columnspan=6,
                                             sticky="w", pady=(0, 2))
        self.v_speed   = grid_row(tg, "Speed",   col=0, row=1)
        self.v_rpm     = grid_row(tg, "RPM",     col=1, row=1)
        self.v_gear    = grid_row(tg, "Gear",    col=2, row=1)
        self.v_slip_fl = grid_row(tg, "Slip FL", col=0, row=2)
        self.v_slip_fr = grid_row(tg, "Slip FR", col=1, row=2)
        self.v_slip_rl = grid_row(tg, "Slip RL", col=0, row=3)
        self.v_slip_rr = grid_row(tg, "Slip RR", col=1, row=3)
        self.v_brake_t = grid_row(tg, "Brake",   col=2, row=2)
        self.v_pkt_sz  = grid_row(tg, "Pkt",     col=2, row=3)

        sep()

        # Controller input
        cg = ttk.Frame(self.root)
        cg.pack(fill="x", padx=14)
        ttk.Label(cg, text="CONTROLLER (physical)", foreground="#888888",
                  font=("Segoe UI", 8)).grid(row=0, column=0, columnspan=6,
                                             sticky="w", pady=(0, 2))
        self.v_ctrl_thr  = grid_row(cg, "RT throttle", col=0, row=1)
        self.v_ctrl_brk  = grid_row(cg, "LT brake",    col=1, row=1)

        sep()

        # Virtual controller output
        og = ttk.Frame(self.root)
        og.pack(fill="x", padx=14)
        ttk.Label(og, text="VIRTUAL CONTROLLER OUTPUT", foreground="#888888",
                  font=("Segoe UI", 8)).grid(row=0, column=0, columnspan=6,
                                             sticky="w", pady=(0, 2))
        self.v_raw_thr = grid_row(og, "Throttle in",  col=0, row=1)
        self.v_out_thr = grid_row(og, "Throttle out", col=1, row=1)
        self.v_cap     = grid_row(og, "Cap",          col=2, row=1)

        self.limit_lbl = ttk.Label(self.root, text="", style="Warn.TLabel")
        self.limit_lbl.pack(anchor="w", padx=14, pady=(4, 0))

        sep()

        self.status_bar = ttk.Label(self.root, text="Waiting for packets...",
                                    wraplength=490, foreground="#666666",
                                    font=("Segoe UI", 9))
        self.status_bar.pack(anchor="w", padx=14)

        self.vgp_status_lbl = ttk.Label(
            self.root,
            text="Virtual controller: disconnected (turn SC ON to connect)",
            foreground="#888888", font=("Segoe UI", 9))
        self.vgp_status_lbl.pack(anchor="w", padx=14, pady=(1, 0))

        ctrl_row = ttk.Frame(self.root)
        ctrl_row.pack(anchor="w", padx=14, pady=(1, 8))
        self.ctrl_status_lbl = ttk.Label(ctrl_row, text="Detecting controller...",
                                         foreground="#888888", font=("Segoe UI", 9))
        self.ctrl_status_lbl.pack(side="left")

        self._joy_var = tk.StringVar(value="Auto")
        self._joy_combo = ttk.Combobox(ctrl_row, textvariable=self._joy_var,
                                       width=28, state="readonly")
        self._joy_combo.pack(side="left", padx=(8, 4))
        self._joy_combo.bind("<<ComboboxSelected>>", self._on_joy_select)
        ttk.Button(ctrl_row, text="Rescan",
                   command=self._rescan_controllers).pack(side="left")

    # ------------------------------------------------------------------ controller select

    def _rescan_controllers(self):
        self.controller.scan()
        self._update_joy_combo()

    def _on_joy_select(self, _event=None):
        sel = self._joy_var.get()
        if sel == "Auto":
            self.controller.scan()
        else:
            try:
                idx = int(sel.split("]")[0].lstrip("["))
                self.controller.scan(preferred_index=idx)
            except (ValueError, IndexError):
                self.controller.scan()

    def _update_joy_combo(self):
        options = ["Auto"] + [f"[{i}] {name}"
                               for i, name in self.controller.all_joysticks]
        self._joy_combo["values"] = options
        cur = self.controller.name_str
        self._joy_var.set(cur if cur in options else "Auto")

    # ------------------------------------------------------------------ gamepad

    def _create_virtual_gamepad(self):
        if VX360Gamepad is None:
            return None
        try:
            gp = VX360Gamepad()
            gp.reset()
            gp.update()
            return gp
        except Exception as exc:
            print(f"Virtual gamepad init failed: {exc}")
            return None

    def _destroy_virtual_gamepad(self):
        """Unplug the virtual controller from the system so it stops being a second device."""
        if self.virtual_gamepad is None:
            return
        self.controller._vgp = None
        try:
            self.virtual_gamepad.reset()
            self.virtual_gamepad.update()
        except Exception:
            pass
        self.virtual_gamepad = None
        gc.collect()  # force ViGEm to unregister the device immediately

    # ------------------------------------------------------------------ stability logic

    def _on_strength_change(self, _val=None):
        self.strength = self._strength_var.get()
        pct = int(round(self.strength * 100))
        self._strength_lbl.config(text=f"{pct}%")

    def toggle_enabled(self):
        self.enabled = not self.enabled
        if self.enabled:
            # Plug in the virtual controller only now
            if self.virtual_gamepad is None:
                self.virtual_gamepad = self._create_virtual_gamepad()
                self.controller._vgp = self.virtual_gamepad
            self.controller.sc_enabled = True
            self.smoothed_cap = 1.0
        else:
            self.controller.sc_enabled = False
            self.controller.throttle_cap = 1.0
            self.controller._was_forwarding = False
            # Unplug the virtual controller — it disappears from the system completely
            self._destroy_virtual_gamepad()

    def _compute_cap(self, frame: dict) -> float:
        slips = [abs(frame.get(k) or 0.0)
                 for k in ("slip_fl", "slip_fr", "slip_rl", "slip_rr")]
        max_slip = max(slips)

        # Hard caps at max strength (mirrors original behaviour)
        if max_slip > 0.8:   raw_cap = 0.35
        elif max_slip > 0.45: raw_cap = 0.55
        elif max_slip > 0.2:  raw_cap = 0.75
        else:                  raw_cap = 1.0

        s = self.strength

        # Scale aggressiveness: at s=1 → raw_cap; at s=0 → 1.0 (no effect)
        target = 1.0 - (1.0 - raw_cap) * s

        # Exponential smoothing toward target.
        # alpha = s means: at s=1.0 → alpha=1.0 → instant snap (current behaviour).
        # At lower strengths the cap eases in/out more gradually.
        # Use a faster alpha when tightening so the car doesn't spin before it kicks in,
        # and a slower alpha when releasing so throttle comes back smoothly.
        if target < self.smoothed_cap:           # tightening
            alpha = s
        else:                                    # releasing
            alpha = max(0.0, s * 0.35)           # always slower than tightening

        self.smoothed_cap += alpha * (target - self.smoothed_cap)
        self.smoothed_cap = max(0.0, min(1.0, self.smoothed_cap))
        return self.smoothed_cap

    def _handle_packet(self, data: bytes):
        frame = parse_packet(data)
        if frame is None:
            return
        self.last_frame = frame
        self.packets_received += 1
        self._last_pkt_size = len(data)

        if self.enabled:
            cap = self._compute_cap(frame)
            self.max_throttle = cap
            self.controller.throttle_cap = cap
            raw = self.controller.throttle
            self.is_limiting = self.controller.output_throttle < raw - 0.01
        else:
            self.max_throttle = 1.0
            self.is_limiting = False

    # ------------------------------------------------------------------ main loop

    def _update_loop(self):
        while not self.packet_queue.empty():
            try:
                self._handle_packet(self.packet_queue.get_nowait())
            except queue.Empty:
                break
        self._refresh_ui()
        self.root.after(100, self._update_loop)

    def _refresh_ui(self):
        f = self.last_frame

        if self.enabled:
            self.status_lbl.config(text="Status: ON", foreground="#66bb6a")
            self.toggle_btn.config(text="Turn OFF  [F8]")
        else:
            self.status_lbl.config(text="Status: OFF", foreground="#4fc3f7")
            self.toggle_btn.config(text="Turn ON  [F8]")

        if self.virtual_gamepad is not None:
            self.vgp_status_lbl.config(
                text="Virtual controller: CONNECTED", foreground="#4caf50")
        elif VX360Gamepad is None:
            self.vgp_status_lbl.config(
                text="Virtual controller: UNAVAILABLE (install ViGEm)", foreground="#ef9a9a")
        else:
            self.vgp_status_lbl.config(
                text="Virtual controller: disconnected  (turn SC ON to connect)",
                foreground="#888888")

        self._update_joy_combo()
        if self.controller.available:
            self.ctrl_status_lbl.config(
                text=f"Controller: {self.controller.name_str}",
                foreground="#4caf50")
        else:
            self.ctrl_status_lbl.config(
                text=f"Controller: {self.controller.name_str}",
                foreground="#ef9a9a")

        self.v_ctrl_thr.set(f"{self.controller.throttle:.3f}"
                            if self.controller.available else "—")
        self.v_ctrl_brk.set(f"{self.controller.brake:.3f}"
                            if self.controller.available else "—")

        if f is None:
            self.status_bar.config(
                text="No UDP packets. Forza: Settings → HUD & Gameplay → UDP Telemetry → ON, "
                     "IP 127.0.0.1 port 31000, format Car Dashboard.")
            return

        speed_ms = f.get("speed_ms") or f.get("vel_ms") or 0.0
        self.v_speed.set(f"{speed_ms * 3.6:.1f} km/h")
        self.v_rpm.set(f"{f.get('engine_rpm') or 0.0:.0f}")
        gear = f.get("gear")
        self.v_gear.set(("N" if gear == 0 else "R" if gear == 10 else str(gear))
                        if gear is not None else "—")

        for var, key in ((self.v_slip_fl, "slip_fl"), (self.v_slip_fr, "slip_fr"),
                         (self.v_slip_rl, "slip_rl"), (self.v_slip_rr, "slip_rr")):
            s = f.get(key)
            var.set((f"{'!' if abs(s) > 0.2 else ' '}{s:+.3f}") if s is not None else "—")

        brake = f.get("brake")
        self.v_brake_t.set(f"{brake:.2f}" if brake is not None else "—(Sled)")
        self.v_pkt_sz.set(f"CarDash({self._last_pkt_size})"
                          if self._last_pkt_size >= CARDASH_SIZE
                          else f"Sled({self._last_pkt_size})")

        raw = self.controller.throttle
        out = self.controller.output_throttle
        self.v_raw_thr.set(f"{raw:.3f}")
        self.v_out_thr.set(f"{out:.3f}")
        self.v_cap.set(f"{self.smoothed_cap:.3f}" if self.enabled else "—")

        if self.enabled and self.is_limiting:
            self.limit_lbl.config(text=f"LIMITING  ({raw:.3f} → {out:.3f})")
        else:
            self.limit_lbl.config(text="")

        src = "CarDash" if self._last_pkt_size >= CARDASH_SIZE else "Sled"
        ctrl = "controller" if self.controller.available else "no controller"
        self.status_bar.config(
            text=f"Packets: {self.packets_received}  |  Tele: {src}  |  Input: {ctrl}")

    # ------------------------------------------------------------------ close

    def on_close(self):
        self.receiver.stop()
        self.controller.stop()
        self._destroy_virtual_gamepad()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = StabilityControlApp()
    app.run()
