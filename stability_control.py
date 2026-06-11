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
SLED_SIZE   = 232   # sled portion (same as FH5)
CARDASH_SIZE = 324  # FH6 full packet (FH5 was 311; +12 for CarGroup/SmashableVelDiff/SmashableMass + 1 extra)

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
        # FH6 offsets: FH5 had Speed@244, Accel@303, Brake@304, Gear@307.
        # FH6 inserts CarGroup(U32)+SmashableVelDiff(F32)+SmashableMass(F32) = 12 bytes
        # after NumCylinders@228, so every field from 232 onward shifts by +12.
        frame["speed_ms"] = f(256)          # Speed (m/s)
        frame["tele_throttle"] = data[315] / 255.0  # Accel
        frame["brake"]         = data[316] / 255.0  # Brake
        frame["gear"]          = data[319]  # plain U8: 0=N, 1-10=gears, 11=R
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

        # Forward through virtual whenever SC is on — always, not just when capping.
        # Toggling in/out of forwarding on slip spikes caused double inputs on
        # buttons (physical + virtual both firing during the transition frame).
        if not self.sc_enabled:
            self.output_throttle = rt_raw
            if self._was_forwarding:
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

        # Buttons and d-pad are intentionally NOT forwarded through virtual.
        # Forza reads all XInput slots; forwarding buttons here creates a second
        # rising edge ~10 ms after physical, causing double shifts/inputs.
        # Physical buttons reach the game directly via their own XInput slot.
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
        self.strength = 1.0        # 0.0–1.0 from the slider
        self.permitted_slip = 0.45 # base slip threshold before TC intervenes
        self.tcr_start = 0.25      # additional offset above permitted_slip to first engage TC
        self.smoothed_cap = 1.0    # current cap value

        # TC logic diagnostics — updated each packet, read by _refresh_ui
        self._tc_front_max  = 0.0
        self._tc_rear_max   = 0.0
        self._tc_oversteer  = 0.0
        self._tc_eff_slip   = 0.0
        self._tc_base_tol   = 1.0
        self._tc_df_bonus   = 0.0
        self._tc_t_light    = 1.0



        self.root = tk.Tk()
        self.root.title("FH6 Stability Control")
        self.root.geometry("520x640")
        self.root.resizable(False, False)

        self._build_ui()

        if keyboard:
            keyboard.add_hotkey("f8", self.toggle_enabled)
        else:
            print("WARNING: 'keyboard' module not installed — use the button instead.")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(16, self._update_loop)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        BG   = "#0e0e0e"
        CARD = "#161616"
        BORD = "#252525"
        FG   = "#e8e8e8"
        DIM  = "#484848"
        BLUE = "#29b6f6"

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel",    background=BG,   foreground=FG, font=("Segoe UI", 9))
        style.configure("TFrame",    background=BG)
        style.configure("TButton",   background=BORD, foreground=FG,
                        font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.map("TButton",         background=[("active", "#333333")])
        style.configure("TScale",    background=CARD, troughcolor=BORD, sliderlength=12)
        style.configure("TCombobox", fieldbackground=BORD, background=BORD,
                        foreground=FG, selectbackground=BORD, selectforeground=FG)
        self.root.configure(bg=BG)

        # ── helpers ─────────────────────────────────────────────────────────

        def section(title):
            """Bordered card. Returns the inner content frame."""
            wrap = tk.Frame(self.root, bg=BORD)
            wrap.pack(fill="x", padx=12, pady=(0, 6))
            body = tk.Frame(wrap, bg=CARD)
            body.pack(fill="x", padx=1, pady=1)
            hdr = tk.Frame(body, bg=CARD)
            hdr.pack(fill="x", padx=10, pady=(7, 3))
            tk.Frame(hdr, bg=BLUE, width=3, height=12).pack(side="left")
            tk.Label(hdr, text=title, bg=CARD, fg=DIM,
                     font=("Segoe UI", 7, "bold")).pack(side="left", padx=(6, 0))
            inner = tk.Frame(body, bg=CARD)
            inner.pack(fill="x", padx=10, pady=(0, 8))
            return inner

        def svar(val="—"):
            return tk.StringVar(value=val)

        def slider_row(parent, label, var, lo, hi, cmd, init):
            r = tk.Frame(parent, bg=CARD)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, bg=CARD, fg=DIM,
                     font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
            ttk.Scale(r, from_=lo, to=hi, orient="horizontal",
                      variable=var, command=cmd).pack(
                          side="left", fill="x", expand=True, padx=(4, 6))
            lbl = tk.Label(r, text=init, bg=CARD, fg=FG,
                           font=("Consolas", 10), width=6, anchor="e")
            lbl.pack(side="left")
            return lbl

        def tile(parent, caption):
            f = tk.Frame(parent, bg="#111111", padx=8, pady=5)
            f.pack(side="left", expand=True, fill="x", padx=(0, 3))
            tk.Label(f, text=caption, bg="#111111", fg=DIM,
                     font=("Segoe UI", 7, "bold")).pack(anchor="w")
            v = svar()
            tk.Label(f, textvariable=v, bg="#111111", fg=FG,
                     font=("Segoe UI", 13, "bold")).pack(anchor="w")
            return v

        def slip_tile(parent, label):
            f = tk.Frame(parent, bg="#111111", padx=6, pady=5)
            f.pack(side="left", expand=True, fill="x", padx=(0, 3))
            tk.Label(f, text=label, bg="#111111", fg=DIM,
                     font=("Segoe UI", 7, "bold")).pack(anchor="w")
            v = svar()
            lbl = tk.Label(f, textvariable=v, bg="#111111", fg=FG,
                           font=("Consolas", 11))
            lbl.pack(anchor="w")
            return v, lbl

        def bar_row(parent, key, label, default_color):
            r = tk.Frame(parent, bg=CARD)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=label, bg=CARD, fg=DIM,
                     font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
            c = tk.Canvas(r, height=10, bg="#1a1a1a", highlightthickness=0)
            c.pack(side="left", fill="x", expand=True, padx=(4, 8))
            c.create_rectangle(0, 0, 0, 10, fill=default_color, outline="", tags="bar")
            self._bar_canvases[key] = (c, default_color)
            v = svar()
            tk.Label(r, textvariable=v, bg=CARD, fg=FG,
                     font=("Consolas", 10), width=6, anchor="e").pack(side="left")
            return v

        def diag_cell(parent, caption):
            f = tk.Frame(parent, bg=CARD)
            f.pack(side="left", expand=True, fill="x")
            tk.Label(f, text=caption, bg=CARD, fg=DIM,
                     font=("Segoe UI", 7)).pack(anchor="w")
            v = svar()
            tk.Label(f, textvariable=v, bg=CARD, fg=FG,
                     font=("Consolas", 10)).pack(anchor="w")
            return v

        # ── HEADER ──────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG)
        hdr.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(hdr, text="FH6", bg=BG, fg=BLUE,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Label(hdr, text=" STABILITY CONTROL", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        self.toggle_btn = ttk.Button(hdr, text="ENABLE  [F8]",
                                     command=self.toggle_enabled)
        self.toggle_btn.pack(side="right")
        self.status_lbl = tk.Label(hdr, text="● OFF", bg=BG, fg=DIM,
                                    font=("Segoe UI", 10, "bold"))
        self.status_lbl.pack(side="right", padx=(0, 10))

        tk.Frame(self.root, bg=BORD, height=1).pack(fill="x", padx=12, pady=(0, 8))

        # ── SETTINGS ────────────────────────────────────────────────────────
        sc = section("SETTINGS")
        self._strength_var      = tk.DoubleVar(value=1.0)
        self._permitted_slip_var = tk.DoubleVar(value=0.45)
        self._tcr_start_var     = tk.DoubleVar(value=0.25)
        self._strength_lbl      = slider_row(sc, "Strength",      self._strength_var,
                                              0.0,  1.0,  self._on_strength_change,      "100%")
        self._permitted_slip_lbl = slider_row(sc, "Permitted Slip", self._permitted_slip_var,
                                              0.05, 1.50, self._on_permitted_slip_change, "0.45")
        self._tcr_start_lbl     = slider_row(sc, "TCR Start",     self._tcr_start_var,
                                              0.25, 1.0,  self._on_tcr_start_change,     "0.25")

        # ── TELEMETRY ───────────────────────────────────────────────────────
        tel = section("TELEMETRY")
        tiles = tk.Frame(tel, bg=CARD)
        tiles.pack(fill="x", pady=(0, 6))
        self.v_speed   = tile(tiles, "SPEED")
        self.v_rpm     = tile(tiles, "RPM")
        self.v_gear    = tile(tiles, "GEAR")
        self.v_brake_t = tile(tiles, "BRAKE")

        slip_row = tk.Frame(tel, bg=CARD)
        slip_row.pack(fill="x")
        self.v_slip_fl, self._slip_fl_lbl = slip_tile(slip_row, "FL")
        self.v_slip_fr, self._slip_fr_lbl = slip_tile(slip_row, "FR")
        self.v_slip_rl, self._slip_rl_lbl = slip_tile(slip_row, "RL")
        self.v_slip_rr, self._slip_rr_lbl = slip_tile(slip_row, "RR")
        self.v_pkt_sz = svar()  # not displayed, kept for compat

        # ── THROTTLE / CAP ──────────────────────────────────────────────────
        self._bar_canvases = {}
        th = section("THROTTLE / CAP")
        self.v_ctrl_thr = bar_row(th, "ctrl", "PHYSICAL", "#37474f")
        self.v_raw_thr  = bar_row(th, "in",   "IN",       "#37474f")
        self.v_out_thr  = bar_row(th, "out",  "OUT",      "#388e3c")
        self.v_cap      = bar_row(th, "cap",  "CAP",      "#e65100")
        self.v_ctrl_brk = svar()  # not in bar, kept for compat
        self.limit_lbl  = tk.Label(th, text="", bg=CARD, fg="#ef5350",
                                    font=("Segoe UI", 9, "bold"))
        self.limit_lbl.pack(anchor="w", pady=(2, 0))

        # ── TC DIAGNOSTICS ──────────────────────────────────────────────────
        diag = section("TC DIAGNOSTICS")
        row1 = tk.Frame(diag, bg=CARD)
        row1.pack(fill="x", pady=(0, 4))
        row2 = tk.Frame(diag, bg=CARD)
        row2.pack(fill="x")
        self.v_front_max = diag_cell(row1, "FRONT SLIP")
        self.v_rear_max  = diag_cell(row1, "REAR SLIP")
        self.v_oversteer = diag_cell(row1, "OVERSTEER")
        self.v_base_tol  = diag_cell(row2, "PERM SLIP")
        self.v_df_bonus  = diag_cell(row2, "DF BONUS")
        self.v_eff_slip  = diag_cell(row2, "EFF SLIP")

        # ── FOOTER ──────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=BORD, height=1).pack(fill="x", padx=12, pady=(2, 6))
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=14, pady=(0, 8))
        self.status_bar = tk.Label(foot, text="Waiting for packets...",
                                    bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w")
        self.status_bar.pack(fill="x")
        self.vgp_status_lbl = tk.Label(foot, text="Virtual controller: disconnected",
                                        bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w")
        self.vgp_status_lbl.pack(fill="x")
        ctrl_row = tk.Frame(foot, bg=BG)
        ctrl_row.pack(fill="x", pady=(3, 0))
        self.ctrl_status_lbl = tk.Label(ctrl_row, text="Detecting controller...",
                                         bg=BG, fg=DIM, font=("Segoe UI", 8))
        self.ctrl_status_lbl.pack(side="left")
        self._joy_var = tk.StringVar(value="Auto")
        self._joy_combo = ttk.Combobox(ctrl_row, textvariable=self._joy_var,
                                       width=24, state="readonly")
        self._joy_combo.pack(side="left", padx=(6, 4))
        self._joy_combo.bind("<<ComboboxSelected>>", self._on_joy_select)
        ttk.Button(ctrl_row, text="Rescan",
                   command=self._rescan_controllers).pack(side="left")

    def _set_bar(self, key, value, color=None):
        if key not in self._bar_canvases:
            return
        c, default = self._bar_canvases[key]
        w = c.winfo_width()
        if w <= 1:
            w = 240
        fill_w = int(_clamp(value, 0.0, 1.0) * w)
        c.coords("bar", 0, 0, fill_w, 10)
        c.itemconfig("bar", fill=color or default)

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

    def _on_permitted_slip_change(self, _val=None):
        self.permitted_slip = self._permitted_slip_var.get()
        self._permitted_slip_lbl.config(text=f"{self.permitted_slip:.2f}")

    def _on_tcr_start_change(self, _val=None):
        self.tcr_start = self._tcr_start_var.get()
        self._tcr_start_lbl.config(text=f"{self.tcr_start:.2f}")

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
        rear_max  = max(abs(frame.get("slip_rl") or 0.0), abs(frame.get("slip_rr") or 0.0))
        front_max = max(abs(frame.get("slip_fl") or 0.0), abs(frame.get("slip_fr") or 0.0))
        max_slip  = max(rear_max, front_max)

        # Oversteer penalty: allow up to 0.35 rear-over-front freely,
        # then boost effective slip so excessive rotation cuts throttle sooner.
        oversteer = max(0.0, rear_max - front_max - 0.35)
        effective_slip = max_slip + oversteer * 0.5

        # User-set base tolerance and TCR engagement offset (from sliders).
        base_tol = self.permitted_slip

        # Speed-based downforce bonus: aerodynamic downforce ∝ v².
        # High-downforce cars at speed can sustain more slip. Capped at +0.4.
        speed_ms = frame.get("speed_ms") or frame.get("vel_ms") or 0.0
        df_bonus = min(0.4, (speed_ms / 80.0) ** 2 * 0.4)

        t_light  = base_tol + self.tcr_start        + df_bonus
        t_medium = base_tol + self.tcr_start + 0.25 + df_bonus
        t_heavy  = base_tol + self.tcr_start + 0.60 + df_bonus

        if effective_slip > t_heavy:    raw_cap = 0.35
        elif effective_slip > t_medium: raw_cap = 0.55
        elif effective_slip > t_light:  raw_cap = 0.75
        else:                           raw_cap = 1.0

        # Store diagnostics for UI display
        self._tc_front_max = front_max
        self._tc_rear_max  = rear_max
        self._tc_oversteer = rear_max - front_max   # raw difference (before deadzone)
        self._tc_eff_slip  = effective_slip
        self._tc_base_tol  = base_tol
        self._tc_df_bonus  = df_bonus
        self._tc_t_light   = t_light

        s = self.strength
        target = 1.0 - (1.0 - raw_cap) * s

        # Asymmetric smoothing: cut throttle instantly, restore gradually.
        if target < self.smoothed_cap:
            self.smoothed_cap = target          # immediate tightening
        else:
            self.smoothed_cap += (target - self.smoothed_cap) * 0.08  # gradual release

        return self.smoothed_cap

    def _handle_packet(self, data: bytes):
        frame = parse_packet(data)
        if frame is None:
            return
        self.last_frame = frame
        self.packets_received += 1
        self._last_pkt_size = len(data)

        if self.enabled:
            if not frame.get("is_race_on"):
                # Menus / paused / car select — release cap immediately so the
                # virtual controller stops forwarding and double-input stops.
                self.smoothed_cap = 1.0
                cap = 1.0
            else:
                cap = self._compute_cap(frame)  # smoothed_cap updated inside
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
        self.root.after(16, self._update_loop)

    def _refresh_ui(self):
        f = self.last_frame

        if self.enabled:
            self.status_lbl.config(text="● ON",  foreground="#4caf50")
            self.toggle_btn.config(text="DISABLE  [F8]")
        else:
            self.status_lbl.config(text="● OFF", foreground="#484848")
            self.toggle_btn.config(text="ENABLE  [F8]")

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
        self.v_speed.set(f"{speed_ms * 3.6:.0f} km/h")
        self.v_rpm.set(f"{f.get('engine_rpm') or 0.0:.0f}")
        gear = f.get("gear")
        self.v_gear.set(("N" if gear == 0 else "R" if gear == 11 else str(gear))  # 0=N, 1-10=gears, 11=R
                        if gear is not None else "—")

        slip_pairs = (
            (self.v_slip_fl, self._slip_fl_lbl, "slip_fl"),
            (self.v_slip_fr, self._slip_fr_lbl, "slip_fr"),
            (self.v_slip_rl, self._slip_rl_lbl, "slip_rl"),
            (self.v_slip_rr, self._slip_rr_lbl, "slip_rr"),
        )
        for var, widget, key in slip_pairs:
            s = f.get(key)
            var.set(f"{s:+.3f}" if s is not None else "—")
            av = abs(s) if s is not None else 0.0
            widget.config(fg="#4caf50" if av < 0.2 else "#ffa726" if av < 0.5 else "#ef5350")

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

        ctrl_val = self.controller.throttle if self.controller.available else 0.0
        self._set_bar("ctrl", ctrl_val)
        self._set_bar("in",   raw)
        self._set_bar("out",  out,
                      "#ef5350" if self.is_limiting else "#388e3c")
        self._set_bar("cap",  self.smoothed_cap if self.enabled else 0.0,
                      "#e65100" if self.enabled and self.smoothed_cap < 0.99 else "#388e3c")

        if self.enabled:
            self.v_front_max.set(f"{self._tc_front_max:.3f}")
            self.v_rear_max.set(f"{self._tc_rear_max:.3f}")
            ov = self._tc_oversteer
            self.v_oversteer.set(f"{'!' if ov > 0.35 else ' '}{ov:+.3f}")
            self.v_base_tol.set(f"{self._tc_base_tol:.2f}")
            self.v_df_bonus.set(f"{self._tc_df_bonus:.3f}")
            self.v_eff_slip.set(f"{self._tc_eff_slip:.3f}")
        else:
            for v in (self.v_front_max, self.v_rear_max, self.v_oversteer,
                      self.v_base_tol, self.v_df_bonus, self.v_eff_slip):
                v.set("—")

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
