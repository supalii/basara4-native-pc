"""Basara 4 PC launcher - by Razz.

Runs Sengoku Basara 4 Sumeragi (PS4, CUSA01159) on the native runtime in bin\\ (basara4-core.exe), a fork of
SpartanPort. Free software under the GNU GPL v2 or later; the credits are in NOTICE.md.

Everything lives next to this program:
  bin\\   the runtime (basara4-core.exe) and its DLLs
  game\\  the game dump (the folder with eboot.bin); another folder can be chosen
  out\\   the prepared game image, made automatically from eboot.bin
  user\\  saves, shader cache, settings, the log
"""
import hashlib
import importlib.util
import json
import os
import re
import runpy
import ssl
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
ROOT = (Path(sys.executable) if FROZEN else Path(__file__)).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
try:
    os.chdir(str(ROOT))
except OSError:
    pass
BIN, OUT, USER, SCRIPTS = ROOT / "bin", ROOT / "out", ROOT / "user", ROOT / "scripts"
DATA = ROOT / "data"
TRANSLATION_DB = DATA / "en_translation_db.json"
PATCH_DIR = ROOT / "mods" / "english_patch"
OVERLAY_DIR = USER / "app0_overlay"
CONFIG = USER / "launcher.json"
INI = USER / "basara4.ini"
OLD_INI = USER / "spartanport.ini"
WINDOW_ICON = USER / "window_icon.bmp"
CORE = BIN / "basara4-core.exe"
VERSION = "1.1.3"
BRAND = "Basara 4 PC"
AUTHOR = "Razz"
FORK = "DarkTama"
FORK_ACCENT = "#e0a952"
GITHUB_REPO = f"{FORK}/basara4-native-pc"


FATAL_PATTERNS = [
    r"(STOP: .*)",
    r"(Fault: .*)",
    r"(ASSERT_MSG.*)",
    r"(vk::Result=.*)",
    r"(VK_ERROR_\w+)",
    r"(GPU \[Render\.Vulkan\] <Error> .*)",
    r"(GPU \[Render\.Vulkan\] <Warning> .*)",
    r"(GPU \[Lib\.\w+\] <Error> .*)",
    r"(Runtime: open\(.*?\) -> not found)",
    r"(guest raised debug exception .*)",
    r"(cannot create \d+ MiB of guest memory .*)",
    r"(error 1455.*)",
    r"(ERROR_COMMITMENT_LIMIT.*)",
    r"(no rewrite table matches the image.*)",
    r"(missing MOVBE, BMI1, LZCNT.*)",
]


def extract_crash_summary(log_path, max_context_lines=10):
    """Parses log from bottom-up and extracts fatal signatures and tail context."""
    if not log_path.is_file():
        return {"detected_errors": ["File log tidak ditemukan (game belum sempat menulis log)."], "tail_context": ""}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"detected_errors": [f"Gagal membaca log: {e}"], "tail_context": ""}
    if not lines:
        return {"detected_errors": ["Log kosong (game berhenti sebelum inisialisasi)."], "tail_context": ""}

    tail_lines = lines[-max_context_lines:]
    detected = []
    for line in reversed(lines):
        for pat in FATAL_PATTERNS:
            m = re.search(pat, line)
            if m:
                matched_str = m.group(1).strip()
                if matched_str not in detected:
                    detected.append(matched_str)
        if len(detected) >= 4:
            break
    return {
        "detected_errors": detected if detected else ["Tidak ada fatal signature terdaftar (game keluar mendadak)."],
        "tail_context": "\n".join(tail_lines),
        "total_lines": len(lines)
    }


def parse_version(tag):
    """Parses version strings like 'v1.1.2' or '1.0.6' into a comparable tuple of ints."""
    nums = re.findall(r"\d+", str(tag))
    return tuple(int(n) for n in nums) if nums else (0,)


def check_latest_release(repo=GITHUB_REPO):
    """Checks the latest GitHub release. Returns dict of asset info or None."""
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "Basara4-PC-Launcher"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        if parse_version(tag) > parse_version(VERSION):
            for a in data.get("assets", []):
                name = a.get("name", "")
                if name.endswith(".zip") and ("basara4" in name.lower() or "windows" in name.lower()):
                    return {
                        "tag": tag,
                        "url": a.get("browser_download_url"),
                        "name": name,
                        "size": a.get("size", 0),
                        "body": data.get("body", ""),
                    }
            for a in data.get("assets", []):
                if a.get("name", "").endswith(".zip"):
                    return {
                        "tag": tag,
                        "url": a.get("browser_download_url"),
                        "name": name,
                        "size": a.get("size", 0),
                        "body": data.get("body", ""),
                    }
    except Exception:
        pass
    return None
LOG = USER / "last_run.log"
PREPARE_LOG = USER / "prepare.log"
MAPPING = USER / "gamepad_mapping.txt"
AUDIO_LOG = USER / "audio_debug.log"
AUDIO_WAV = USER / "audio_wav"  # SPARTAN_AUDIO_WAV_DIR: float WAV capture of the mix and of long voices
STAMP = OUT / "prepared.json"
WAV_WARNING = ("WAV capture saves the final mix and every long voice as uncompressed audio in user\\audio_wav: about 70 MB per "
               "minute, up to 3 GB for one session.\n\nUse it only to find an audio problem, and delete the files afterwards.\n\n"
               "It applies to the next game start only and switches itself off after that. It also turns on the audio debug "
               "log. Turn it on?")
PIPELINE = [("prepare.py", []), ("link_libc.py", []), ("link_modules.py", []),
            ("content_profile.py", ["--sku", "full"]),
            ("isa_rewrite.py", [])]
NO_WINDOW = 0x08000000


def wav_captures():
    """The audio captures an earlier session left in user\\audio_wav: (files, total bytes)."""
    files = sorted(AUDIO_WAV.glob("*.wav")) if AUDIO_WAV.is_dir() else []
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return files, total


def format_size(n):
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def check_commit_limit_mb():
    """Returns available virtual memory (page file commit limit) in MB, or None if unavailable."""
    try:
        import ctypes
        from ctypes import wintypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullAvailPageFile // (1024 * 1024)
    except Exception:
        pass
    return None


def run_script_mode():
    """`launcher.exe --run-script NAME args...`: runs one of the preparation scripts in this process."""
    script = SCRIPTS / sys.argv[2]
    sys.path.insert(0, str(SCRIPTS))
    sys.argv = [str(script)] + sys.argv[3:]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exit_:
        code = exit_.code
        sys.exit(code if isinstance(code, int) else (0 if code is None else 1))


if len(sys.argv) > 2 and sys.argv[1] == "--run-script":
    run_script_mode()
    sys.exit(0)

import tkinter as tk  # noqa: E402  (after the script mode, which needs no window)
from tkinter import filedialog, font as tkfont, messagebox, ttk  # noqa: E402

try:
    from PIL import Image, ImageTk
except ImportError:  # still works, just without artwork
    Image = None

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import basara4_patches  # noqa: E402
import basara4_translation  # noqa: E402
import basara4_overlay  # noqa: E402
import basara4_repatch  # noqa: E402
import game_update  # noqa: E402
import dlc  # noqa: E402
import patches  # noqa: E402

DEFAULTS = {"language": "en", "game": "", "game_update": "", "dlc_folder": "", "display_mode": "borderless", "vsync": True, "fps_limit": 60, "rcas": False,
            "anisotropy": 0, "resolution": "native", "upscale": "off", "audio_log": False, "audio_wav": False,
            "gpu_index": -1, "keymap": {}, "mouse_camera": True, "mouse_sens": 100, "mouse_invert_x": False,
            "mouse_invert_y": False, "english_patch": False, "ps3_patch_source": "",
            "p1_control": "pad1", "p2_control": "none"}

DEFAULT_PC_KEYMAP = {
    "square": "Mouse Left",       # Serangan Biasa (Normal Attack)
    "triangle": "Mouse Right",    # Special Skill 1
    "cross": "Space",             # Lompat (Jump)
    "l1": "Left Shift",           # Tangkis / Guard / Evade
    "r1": "E",                    # Special Skill 2
    "r2": "Q",                    # Special Skill 3
    "circle": "F",                # Basara Attack (Ultimate)
    "l2": "R",                    # Sengoku Drive / Poise Boost
    "touchpad": "Tab",            # Taunt / Provoke
    "options": "Return",          # Pause / In-Game Menu
    "up": "Up",                   # D-Pad Up
    "down": "Down",               # D-Pad Down
    "left": "Left",               # D-Pad Left
    "right": "Right",             # D-Pad Right
    "l3": "Z",                    # L3
    "r3": "C",                    # R3
}

ACTION_DESCRIPTIONS_EN = [
    ("square", "Normal Attack", "Square"),
    ("triangle", "Special Skill 1", "Triangle"),
    ("cross", "Jump", "Cross"),
    ("circle", "Basara Attack (Ultimate)", "Circle"),
    ("l1", "Guard / Evade / Dash", "L1"),
    ("r1", "Special Skill 2", "R1"),
    ("r2", "Special Skill 3", "R2"),
    ("l2", "Sengoku Drive / Boost", "L2"),
    ("touchpad", "Taunt / Provoke", "Touchpad / Tab"),
    ("options", "Pause / In-Game Menu", "Options / Enter"),
    ("up", "D-Pad Up", "D-Pad Up"),
    ("down", "D-Pad Down", "D-Pad Down"),
    ("left", "D-Pad Left", "D-Pad Left"),
    ("right", "D-Pad Right", "D-Pad Right"),
    ("l3", "L3 Action", "L3"),
    ("r3", "R3 Action / Lock-on", "R3"),
]

ACTION_DESCRIPTIONS_ID = [
    ("square", "Serangan Biasa (Normal Attack)", "Kotak / Square"),
    ("triangle", "Skill Spesial 1 (Skill Utama)", "Segitiga / Triangle"),
    ("cross", "Lompat (Jump)", "Silang / Cross"),
    ("circle", "Serangan Basara (Ultimate)", "Lingkaran / Circle"),
    ("l1", "Tangkis / Guard / Dash", "L1"),
    ("r1", "Skill Spesial 2 (Skill Tambahan)", "R1"),
    ("r2", "Skill Spesial 3 (Skill Karakter)", "R2"),
    ("l2", "Sengoku Drive / Poise Boost", "L2"),
    ("touchpad", "Ejekan (Taunt / Provoke)", "Touchpad / Tab"),
    ("options", "Jeda / Menu Game (Pause)", "Options / Enter"),
    ("up", "D-Pad Atas (Navigasi / Perintah)", "D-Pad Atas"),
    ("down", "D-Pad Bawah (Navigasi / Perintah)", "D-Pad Bawah"),
    ("left", "D-Pad Kiri (Navigasi / Perintah)", "D-Pad Kiri"),
    ("right", "D-Pad Kanan (Navigasi / Perintah)", "D-Pad Kanan"),
    ("l3", "Aksi L3", "L3"),
    ("r3", "Aksi R3 / Lock-on", "R3"),
]
ACTION_DESCRIPTIONS = ACTION_DESCRIPTIONS_ID


def enumerate_gpus():
    """Enumerate system Vulkan GPUs using vulkan-1.dll. Returns list of (index, display_name, is_discrete)."""
    gpus = [(-1, "Auto (Prefer discrete GPU)", True)]
    try:
        import ctypes
        vulkan = ctypes.cdll.LoadLibrary("vulkan-1.dll")
        class VkApplicationInfo(ctypes.Structure):
            _fields_ = [
                ("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p),
                ("pApplicationName", ctypes.c_char_p), ("applicationVersion", ctypes.c_uint32),
                ("pEngineName", ctypes.c_char_p), ("engineVersion", ctypes.c_uint32),
                ("apiVersion", ctypes.c_uint32),
            ]
        class VkInstanceCreateInfo(ctypes.Structure):
            _fields_ = [
                ("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p),
                ("flags", ctypes.c_uint32), ("pApplicationInfo", ctypes.POINTER(VkApplicationInfo)),
                ("enabledLayerCount", ctypes.c_uint32), ("ppEnabledLayerNames", ctypes.c_void_p),
                ("enabledExtensionCount", ctypes.c_uint32), ("ppEnabledExtensionNames", ctypes.c_void_p),
            ]
        class VkPhysicalDeviceProperties(ctypes.Structure):
            _fields_ = [
                ("apiVersion", ctypes.c_uint32), ("driverVersion", ctypes.c_uint32),
                ("vendorID", ctypes.c_uint32), ("deviceID", ctypes.c_uint32),
                ("deviceType", ctypes.c_uint32), ("deviceName", ctypes.c_char * 256),
                ("pipelineCacheUUID", ctypes.c_ubyte * 16), ("limits", ctypes.c_byte * 504),
                ("sparseProperties", ctypes.c_byte * 20),
            ]
        app_info = VkApplicationInfo(sType=1, pApplicationName=b"Basara4Launcher", apiVersion=(1 << 22) | (2 << 12))
        ci = VkInstanceCreateInfo(sType=2, pApplicationInfo=ctypes.pointer(app_info))
        vulkan.vkCreateInstance.argtypes = [ctypes.POINTER(VkInstanceCreateInfo), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        vulkan.vkCreateInstance.restype = ctypes.c_int32
        vulkan.vkDestroyInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        vulkan.vkEnumeratePhysicalDevices.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
        vulkan.vkEnumeratePhysicalDevices.restype = ctypes.c_int32
        vulkan.vkGetPhysicalDeviceProperties.argtypes = [ctypes.c_void_p, ctypes.POINTER(VkPhysicalDeviceProperties)]

        inst = ctypes.c_void_p()
        if vulkan.vkCreateInstance(ctypes.byref(ci), None, ctypes.byref(inst)) == 0:
            count = ctypes.c_uint32(0)
            vulkan.vkEnumeratePhysicalDevices(inst, ctypes.byref(count), None)
            if count.value > 0:
                devs = (ctypes.c_void_p * count.value)()
                vulkan.vkEnumeratePhysicalDevices(inst, ctypes.byref(count), devs)
                for i in range(count.value):
                    p = VkPhysicalDeviceProperties()
                    vulkan.vkGetPhysicalDeviceProperties(devs[i], ctypes.byref(p))
                    name = p.deviceName.decode("utf-8", errors="replace").strip("\x00")
                    is_disc = (p.deviceType == 2)
                    kind_tag = "dGPU" if is_disc else ("iGPU" if p.deviceType == 1 else "GPU")
                    gpus.append((i, f"GPU {i}: {name} ({kind_tag})", is_disc))
            vulkan.vkDestroyInstance(inst, None)
    except Exception:
        pass
    return gpus


SYSTEM_GPUS = enumerate_gpus()


def detect_connected_gamepads():
    """Detects connected gamepads using bin/SDL3.dll via ctypes. Returns list of pad names."""
    gamepads = []
    sdl_dll = BIN / "SDL3.dll"
    if not sdl_dll.is_file():
        return gamepads
    try:
        import ctypes
        sdl = ctypes.CDLL(str(sdl_dll))
        SDL_INIT_GAMEPAD = 0x00002000
        SDL_INIT_JOYSTICK = 0x00000200
        if hasattr(sdl, "SDL_InitSubSystem"):
            sdl.SDL_InitSubSystem.restype = ctypes.c_bool
            sdl.SDL_InitSubSystem.argtypes = [ctypes.c_uint32]
            sdl.SDL_InitSubSystem(SDL_INIT_GAMEPAD | SDL_INIT_JOYSTICK)
        if hasattr(sdl, "SDL_UpdateGamepads"):
            sdl.SDL_UpdateGamepads()
        if hasattr(sdl, "SDL_PumpEvents"):
            sdl.SDL_PumpEvents()

        if hasattr(sdl, "SDL_GetGamepads"):
            sdl.SDL_GetGamepads.restype = ctypes.POINTER(ctypes.c_uint32)
            sdl.SDL_GetGamepads.argtypes = [ctypes.POINTER(ctypes.c_int)]
            count = ctypes.c_int(0)
            pads = sdl.SDL_GetGamepads(ctypes.byref(count))
            if pads and count.value > 0:
                if hasattr(sdl, "SDL_GetGamepadNameForID"):
                    sdl.SDL_GetGamepadNameForID.restype = ctypes.c_char_p
                    sdl.SDL_GetGamepadNameForID.argtypes = [ctypes.c_uint32]
                for i in range(count.value):
                    name_bytes = sdl.SDL_GetGamepadNameForID(pads[i]) if hasattr(sdl, "SDL_GetGamepadNameForID") else None
                    name = name_bytes.decode("utf-8", errors="replace").strip() if name_bytes else f"Controller {i + 1}"
                    gamepads.append(name)
                if hasattr(sdl, "SDL_free"):
                    sdl.SDL_free(pads)

        if not gamepads and hasattr(sdl, "SDL_GetJoysticks"):
            sdl.SDL_GetJoysticks.restype = ctypes.POINTER(ctypes.c_uint32)
            sdl.SDL_GetJoysticks.argtypes = [ctypes.POINTER(ctypes.c_int)]
            jcount = ctypes.c_int(0)
            joys = sdl.SDL_GetJoysticks(ctypes.byref(jcount))
            if joys and jcount.value > 0:
                if hasattr(sdl, "SDL_GetJoystickNameForID"):
                    sdl.SDL_GetJoystickNameForID.restype = ctypes.c_char_p
                    sdl.SDL_GetJoystickNameForID.argtypes = [ctypes.c_uint32]
                for i in range(jcount.value):
                    name_bytes = sdl.SDL_GetJoystickNameForID(joys[i]) if hasattr(sdl, "SDL_GetJoystickNameForID") else None
                    name = name_bytes.decode("utf-8", errors="replace").strip() if name_bytes else f"Joystick {i + 1}"
                    gamepads.append(name)
                if hasattr(sdl, "SDL_free"):
                    sdl.SDL_free(joys)

        if hasattr(sdl, "SDL_QuitSubSystem"):
            sdl.SDL_QuitSubSystem.argtypes = [ctypes.c_uint32]
            sdl.SDL_QuitSubSystem(SDL_INIT_GAMEPAD | SDL_INIT_JOYSTICK)
    except Exception:
        pass
    return gamepads


DISPLAY_MODES_EN = [("Windowed", "windowed"), ("Borderless", "borderless"), ("Exclusive", "fullscreen")]
DISPLAY_MODES_ID = [("Jendela", "windowed"), ("Tanpa Batas", "borderless"), ("Layar Penuh", "fullscreen")]

FPS_LIMITS_EN = [("60 (game default)", 60), ("90", 90), ("120", 120), ("144", 144), ("165", 165),
                 ("240", 240), ("Unlimited", 0)]
FPS_LIMITS_ID = [("60 (standar game)", 60), ("90", 90), ("120", 120), ("144", 144), ("165", 165),
                 ("240", 240), ("Tanpa Batas", 0)]

RESOLUTIONS_EN = [("1920 x 1080  (native)", "native"), ("1280 x 720", "720p"), ("1600 x 900", "900p"),
                  ("2560 x 1440", "1440p"), ("3200 x 1800", "1800p"), ("3840 x 2160  (4K)", "4K")]
RESOLUTIONS_ID = [("1920 x 1080  (asli)", "native"), ("1280 x 720", "720p"), ("1600 x 900", "900p"),
                  ("2560 x 1440", "1440p"), ("3200 x 1800", "1800p"), ("3840 x 2160  (4K)", "4K")]

UPSCALERS_EN = [("Off (Native 1080p)", "off"), ("FSR 1 (Spatial Sharpening)", "fsr1"),
                ("FSR 3.1 (Temporal Reconstruction)", "fsr3"), ("DLSS (NVIDIA RTX)", "dlss")]
UPSCALERS_ID = [("Nonaktif (Native 1080p)", "off"), ("FSR 1 (Penajaman Spasial)", "fsr1"),
                ("FSR 3.1 (Rekonstruksi Temporal)", "fsr3"), ("DLSS (Khusus NVIDIA RTX)", "dlss")]

P1_CONTROLS_EN = [
    ("Gamepad 1 (Primary / First detected)", "pad1"),
    ("Gamepad 2 (Secondary)", "pad2"),
    ("Keyboard & Mouse Only", "kbm"),
]
P1_CONTROLS_ID = [
    ("Gamepad 1 (Stik Utama / Terdeteksi Pertama)", "pad1"),
    ("Gamepad 2 (Stik Kedua)", "pad2"),
    ("Keyboard & Mouse Saja", "kbm"),
]

P2_CONTROLS_EN = [
    ("None (Disabled / Single Player)", "none"),
    ("Gamepad 2 (Secondary)", "pad2"),
    ("Gamepad 1 (Co-op with P1 on KBM)", "pad1"),
    ("Keyboard & Mouse", "kbm"),
]
P2_CONTROLS_ID = [
    ("Tidak Ada (Nonaktif / Single Player)", "none"),
    ("Gamepad 2 (Stik Kedua)", "pad2"),
    ("Gamepad 1 (Co-op dengan P1 pakai KBM)", "pad1"),
    ("Keyboard & Mouse", "kbm"),
]

P1_CONTROL_MAP = {n: v for n, v in P1_CONTROLS_EN + P1_CONTROLS_ID}
P1_CONTROL_MAP.update({"pad1": "pad1", "pad2": "pad2", "kbm": "kbm"})

P2_CONTROL_MAP = {n: v for n, v in P2_CONTROLS_EN + P2_CONTROLS_ID}
P2_CONTROL_MAP.update({"none": "none", "pad2": "pad2", "pad1": "pad1", "kbm": "kbm"})

ANISO_EN = [("Off", 0), ("2x", 2), ("4x", 4), ("8x", 8), ("16x", 16)]
ANISO_ID = [("Nonaktif", 0), ("2x", 2), ("4x", 4), ("8x", 8), ("16x", 16)]

FPS_LIMIT_MAP = {
    "60 (game default)": 60, "60 (standar game)": 60, "60": 60,
    "90": 90, "120": 120, "144": 144, "165": 165, "240": 240,
    "Unlimited": 0, "Tanpa Batas": 0, "0": 0
}

UPSCALER_MAP = {
    "Off (Native 1080p)": "off", "Nonaktif (Native 1080p)": "off",
    "Off  (plain scaling)": "off", "Nonaktif  (skala standar)": "off", "Off": "off", "Nonaktif": "off",
    "FSR 1 (Spatial Sharpening)": "fsr1", "FSR 1 (Penajaman Spasial)": "fsr1",
    "FSR 1  (sharper)": "fsr1", "FSR 1  (lebih tajam)": "fsr1", "fsr1": "fsr1", "off": "off",
    "FSR 3.1 (Temporal Reconstruction)": "fsr3", "FSR 3.1 (Rekonstruksi Temporal)": "fsr3", "fsr3": "fsr3",
    "DLSS (NVIDIA RTX)": "dlss", "DLSS (Khusus NVIDIA RTX)": "dlss", "dlss": "dlss",
}

ANISO_MAP = {
    "Off": 0, "Nonaktif": 0, "0": 0,
    "2x": 2, "4x": 4, "8x": 8, "16x": 16,
    2: 2, 4: 4, 8: 8, 16: 16, 0: 0
}

RESOLUTION_MAP = {
    "1920 x 1080  (native)": "native", "1920 x 1080  (asli)": "native", "native": "native",
    "1280 x 720": "720p", "720p": "720p",
    "1600 x 900": "900p", "900p": "900p",
    "2560 x 1440": "1440p", "1440p": "1440p",
    "3200 x 1800": "1800p", "1800p": "1800p",
    "3840 x 2160  (4K)": "4K", "4K": "4K"
}

BG, PANEL, FG, MUTED, GOLD, RED = "#12100e", "#1c1916", "#eadfcb", "#8d8374", "#c9a24a", "#9c2b24"
RAZZ = "#e0526a"  # the accent of the by-Razz mark
FIELD, EDGE, LINE, RULE, HOVER, OK = "#12100e", "#3f382f", "#332d27", "#26211d", "#221e1a", "#7da36b"
FONT, FONT_SMALL = ("Segoe UI", 10), ("Segoe UI", 8)
CHEAT_HINT_EN = "Can also be toggled in game (Insert / R3+L2)"
CHEAT_HINT_ID = "Dapat diatur juga di dalam game (Insert / R3+L2)"
CHEAT_KEYS = ["cheat_gold", "cheat_coins", "cheat_growth_points", "cheat_weapon_points", "cheat_weapon_rank"]

NAV_EN = [("game", "Game"), ("display", "Display"), ("controls", "Controls"), ("mods", "Mods & Cheats"), ("tools", "Tools")]
NAV_ID = [("game", "Game"), ("display", "Tampilan"), ("controls", "Kontrol"), ("mods", "Mod & Cheat"), ("tools", "Alat")]

PAGE_SUBTITLE_EN = {"display": "Applied the next time you press PLAY."}
PAGE_SUBTITLE_ID = {"display": "Diterapkan saat Anda menekan tombol PLAY berikutnya."}

PAGE_HINT_EN = {
    "game": "The game image is made on the first start (about a minute).",
    "display": "In-game menu: Insert, or R3 + L2 on the pad (it changes these too).",
    "controls": "Controller management, routing (P1 / P2 co-op), button mapping, and KBM settings.",
    "mods": "Import needs your own PS3 patch files. Cheats can also be toggled in game.",
    "tools": "Open log: the launcher and runtime log of the last run.",
}
PAGE_HINT_ID = {
    "game": "Game image dibuat saat pertama kali mulai (sekitar satu menit).",
    "display": "Menu dalam game: Insert, atau R3 + L2 pada stick (bisa diubah di sana juga).",
    "controls": "Manajemen controller: deteksi gamepad, routing (P1 / P2 co-op), pemetaan tombol, dan KBM.",
    "mods": "Impor memerlukan file patch PS3 Anda sendiri. Cheat juga dapat diaktifkan di dalam game.",
    "tools": "Buka log: log launcher dan runtime dari sesi permainan terakhir.",
}

GROUP_EN = {"translation": "Translation", "cheats": "Cheats"}
GROUP_ID = {"translation": "Terjemahan", "cheats": "Cheat"}

ROW_EN = {
    "game_folder": ("Game folder", "The folder that holds eboot.bin."),
    "game_image": ("Game image", "Made from eboot.bin on the first start; again if eboot.bin changes."),
    "game_update": ("Game update folder", "Separate update dump, e.g. CUSA01159-patch (v01.02). Your dump is not modified."),
    "dlc_folder": ("DLC folder", "Folder holding extracted add-ons: <root>/CUSA01159/<entitlement>/"),
    "language": ("Language", "Choose the launcher interface language."),
    "display_mode": ("Display mode", "Windowed, borderless fullscreen or exclusive fullscreen."),
    "gpu": ("Graphics card", "Auto picks the discrete GPU when there is one."),
    "resolution": ("Window size", "The game draws at 1080p. Window size sets windowed mode."),
    "fps": ("Frame limit", "Above 60 turns off the game's own limiter. VSync still caps at monitor refresh."),
    "upscale": ("Upscaling", "FSR 1 spatial sharpening or FSR 3.1 temporal reconstruction / DLSS for higher clarity."),
    "aniso": ("Anisotropic", "Texture filtering: sharper at a slant, costs a little speed."),
    "vsync": ("VSync", "Caps the frame rate at the monitor refresh."),
    "rcas": ("Sharpening (RCAS)", "Sharpens the final image."),
    "pad_detect": ("Connected Gamepads", "Lists controllers detected via SDL3. Click Refresh to scan again."),
    "p1_control": ("Player 1 Control", "Assign input device for Player 1. Falls back to Keyboard & Mouse if unplugged."),
    "p2_control": ("Player 2 Control", "Assign input device for Player 2 (local co-op) or disable for single player."),
    "pad": ("Gamepad Mapping", "Custom controller mapping state (gamepad_mapping.txt vs default)."),
    "pad_actions": ("Mapping Actions", "Configure gamepad buttons/sticks or reset back to default database."),
    "map_pad": ("Map All Buttons", "Press each gamepad button when prompted in the mapper window."),
    "map_sticks": ("Map sticks only", "Maps only the two sticks; the buttons stay as they are."),
    "reset_map": ("Reset mapping", "Deletes the saved controller mapping (a .bak copy is kept)."),
    "keymap": ("Keyboard & Mouse controls", "Choose the key and mouse bindings and the mouse camera."),
    "mouse_camera": ("Enable Mouse Camera", "Free camera look using mouse movement."),
    "mouse_sens": ("Mouse Camera Sensitivity", "Mouse camera rotation sensitivity (20% - 300%, default 100%)."),
    "mouse_invert_x": ("Invert X", "Invert mouse horizontal camera axis (SPARTAN_MOUSE_INVERT_X=1)."),
    "mouse_invert_y": ("Invert Y", "Invert mouse vertical camera axis (SPARTAN_MOUSE_INVERT_Y=1)."),
    "english": ("English Translation Patch", "PS3 v0.85 port. Needs an imported patch; applied when you press PLAY."),
    "import_patch": ("Import PS3 English Patch", "Pick your own PS3 (BLJM61248) English patch folder."),
    "rebuild_patch": ("Rebuild Translation Patch", "Builds the patch again from the imported files."),
    "cheat_gold": ("Gold won't decrease", CHEAT_HINT_EN),
    "cheat_coins": ("Special Coin x99999", CHEAT_HINT_EN),
    "cheat_growth_points": ("Growth points won't decrease", CHEAT_HINT_EN),
    "cheat_weapon_points": ("Weapon points won't decrease", CHEAT_HINT_EN),
    "cheat_weapon_rank": ("Weapon rank points won't decrease", CHEAT_HINT_EN),
    "open_log": ("Open log", "The launcher and runtime log of the last run."),
    "open_audio_log": ("Open audio log", "Needs \"Audio debug log\" on, then a game start."),
    "open_saves": ("Open saves", "Opens user\\savedata in Explorer."),
    "audio_log": ("Audio debug log", "One timestamped line per audio event, for bug reports."),
    "audio_wav": ("WAV capture (up to 3 GB)", "Next game start only; about 70 MB per minute. Turns the audio log on."),
}

ROW_ID = {
    "game_folder": ("Folder game", "Folder yang berisi file eboot.bin."),
    "game_image": ("Game image", "Dibuat dari eboot.bin saat awal mulai; diperbarui jika eboot.bin berubah."),
    "game_update": ("Folder update game", "Dump update terpisah, cth. CUSA01159-patch (v01.02). Dump asli tidak diubah."),
    "dlc_folder": ("Folder DLC", "Folder berisi add-on yang diekstrak: <root>/CUSA01159/<entitlement>/"),
    "language": ("Bahasa", "Pilih bahasa antarmuka untuk launcher."),
    "display_mode": ("Mode tampilan", "Jendela (Windowed), Tanpa Batas (Borderless), atau Layar Penuh (Exclusive)."),
    "gpu": ("Kartu grafis (GPU)", "Otomatis memilih GPU diskrit jika tersedia."),
    "resolution": ("Ukuran jendela", "Game dirender pada 1080p. Ukuran jendela mengatur resolusi tampilan."),
    "fps": ("Batas FPS", "Di atas 60 mematikan pembatas bawaan game. VSync tetap membatasi ke refresh monitor."),
    "upscale": ("Upscaling", "FSR 1 penajaman spasial atau FSR 3.1 rekonstruksi temporal / DLSS untuk gambar lebih jernih."),
    "aniso": ("Anisotropik", "Penyaringan tekstur: lebih tajam pada sudut miring, sedikit beban performa."),
    "vsync": ("VSync", "Membatasi frame rate sesuai refresh rate monitor."),
    "rcas": ("Penajaman (RCAS)", "Mempertajam kualitas gambar akhir."),
    "pad_detect": ("Gamepad Terdeteksi", "Daftar controller yang terdeteksi via SDL3. Klik Deteksi Ulang untuk memindai ulang."),
    "p1_control": ("Kontrol Player 1", "Pilih perangkat input untuk Player 1. Otomatis ke Keyboard & Mouse jika stik dilepas."),
    "p2_control": ("Kontrol Player 2", "Pilih perangkat input untuk Player 2 (co-op lokal) atau nonaktifkan untuk single player."),
    "pad": ("Pemetaan Gamepad", "Status pemetaan controller kustom (gamepad_mapping.txt vs default)."),
    "pad_actions": ("Aksi Pemetaan", "Konfigurasi tombol/analog stick controller atau reset ke database default."),
    "map_pad": ("Atur Semua Tombol", "Tekan setiap tombol stick saat diminta di jendela mapper."),
    "map_sticks": ("Atur analog saja", "Hanya mengatur kedua analog stick; tombol lainnya tetap."),
    "reset_map": ("Reset pemetaan", "Menghapus pemetaan controller yang tersimpan (salinan .bak disimpan)."),
    "keymap": ("Kontrol Keyboard & Mouse", "Atur tombol keyboard, mouse, dan kamera mouse."),
    "mouse_camera": ("Aktifkan Kamera Mouse (Mouse Look)", "Kontrol kamera bebas menggunakan pergerakan mouse."),
    "mouse_sens": ("Sensitivitas Kamera Mouse", "Sensitivitas rotasi kamera mouse (20% - 300%, default 100%)."),
    "mouse_invert_x": ("Invert X", "Membalik arah horizontal kamera mouse (SPARTAN_MOUSE_INVERT_X=1)."),
    "mouse_invert_y": ("Invert Y", "Membalik arah vertikal kamera mouse (SPARTAN_MOUSE_INVERT_Y=1)."),
    "english": ("Patch Terjemahan Inggris", "Port PS3 v0.85. Memerlukan patch impor; diterapkan saat klik PLAY."),
    "import_patch": ("Impor Patch PS3 Inggris", "Pilih folder patch Bahasa Inggris PS3 (BLJM61248) milik Anda."),
    "rebuild_patch": ("Bangun Ulang Patch", "Membangun ulang patch dari file yang diimpor."),
    "cheat_gold": ("Gold tidak berkurang", CHEAT_HINT_ID),
    "cheat_coins": ("Koin Spesial x99999", CHEAT_HINT_ID),
    "cheat_growth_points": ("Poin Growth tidak berkurang", CHEAT_HINT_ID),
    "cheat_weapon_points": ("Poin Weapon tidak berkurang", CHEAT_HINT_ID),
    "cheat_weapon_rank": ("Poin Rank Weapon tidak berkurang", CHEAT_HINT_ID),
    "open_log": ("Buka log", "Log launcher dan runtime dari sesi permainan terakhir."),
    "open_audio_log": ("Buka log audio", "Memerlukan \"Log debug audio\" aktif, lalu jalankan game."),
    "open_saves": ("Buka savedata", "Membuka folder user\\savedata di Explorer."),
    "audio_log": ("Log debug audio", "Satu baris bertanda waktu per peristiwa audio, untuk laporan bug."),
    "audio_wav": ("Perekaman WAV (hingga 3 GB)", "Hanya untuk sesi mulai berikutnya; ~70 MB per menit. Mengaktifkan log audio."),
}

UI_STRINGS_EN = {
    "check_updates": "Check for updates",
    "ready": "Ready",
    "play": "PLAY",
    "stop": "Stop game",
    "browse": "Browse…",
    "clear": "Clear",
    "map": "Map…",
    "map_sticks": "Map sticks…",
    "reset": "Reset",
    "edit": "Edit…",
    "import": "Import…",
    "rebuild": "Rebuild",
    "open": "Open",
    "on": "On",
    "pad_custom": "Custom Map active (gamepad_mapping.txt)",
    "pad_default": "Default XInput / SDL Database active",
    "image_no_eboot": "No eboot.bin in this folder.",
    "image_ready": "Game image ready.",
    "image_needed": "The game image is made on the first start (about a minute); again if eboot.bin changes.",
    "preparing": "Preparing game image…",
    "starting": "Starting…",
    "running": "Running",
    "closed": "Game closed.",
    "press_key": "[ PRESS A KEY... ]",
    "keymap_title": "Keyboard & Mouse Controls — Custom Keymap",
    "keymap_header": "⚙️ Keyboard & Mouse Controls Settings",
    "keymap_sub": "Click a button to change key mapping. Press any Keyboard key or Mouse button to bind.",
    "keymap_cam": "Enable Mouse Camera (Mouse Look)",
    "keymap_sens": "Sensitivity: ",
    "keymap_notice": "ℹ️ Character movement is bound to W, A, S, D (PC Standard). Mouse camera controls the right analog stick.",
    "keymap_reset": "🔄 Reset to PC Defaults",
    "keymap_cancel": "Cancel",
    "keymap_save": "💾 Save Settings",
    "keymap_reset_msg": "Control settings reset to default PC preset!",
    "keymap_save_msg": "Keyboard & Mouse control settings saved successfully!",
    "keymap_saved_title": "Saved",
}

UI_STRINGS_ID = {
    "check_updates": "Periksa Pembaruan",
    "ready": "Siap",
    "play": "PLAY",
    "stop": "Hentikan Game",
    "browse": "Telusuri…",
    "clear": "Hapus",
    "map": "Atur…",
    "map_sticks": "Atur analog…",
    "reset": "Reset",
    "edit": "Ubah…",
    "import": "Impor…",
    "rebuild": "Bangun Ulang",
    "open": "Buka",
    "on": "Aktif",
    "pad_custom": "Custom Map aktif (gamepad_mapping.txt)",
    "pad_default": "Default XInput / SDL Database aktif",
    "image_no_eboot": "Tidak ada eboot.bin di folder ini.",
    "image_ready": "Game image siap.",
    "image_needed": "Game image dibuat saat pertama kali mulai (sekitar satu menit); diperbarui jika eboot.bin berubah.",
    "preparing": "Menyiapkan image game…",
    "starting": "Memulai…",
    "running": "Sedang berjalan",
    "closed": "Game ditutup.",
    "press_key": "[ TEKAN TOMBOL... ]",
    "keymap_title": "Kontrol Keyboard & Mouse — Pemetaan Kustom",
    "keymap_header": "⚙️ Pengaturan Kontrol Keyboard & Mouse",
    "keymap_sub": "Klik tombol untuk mengubah pemetaan. Tekan tombol Keyboard atau Mouse untuk mengikat.",
    "keymap_cam": "Aktifkan Kamera Mouse (Mouse Look)",
    "keymap_sens": "Sensitivitas: ",
    "keymap_notice": "ℹ️ Pergerakan Karakter otomatis terikat pada W, A, S, D (Standar PC). Kamera mouse mengontrol analog kanan.",
    "keymap_reset": "🔄 Reset ke Default PC",
    "keymap_cancel": "Batal",
    "keymap_save": "💾 Simpan Pengaturan",
    "keymap_reset_msg": "Pengaturan kontrol berhasil dikembalikan ke Preset Default PC!",
    "keymap_save_msg": "Pengaturan kontrol Keyboard & Mouse berhasil disimpan!",
    "keymap_saved_title": "Tersimpan",
}

DISPLAY_MODES = DISPLAY_MODES_EN
FPS_LIMITS = FPS_LIMITS_EN
RESOLUTIONS = RESOLUTIONS_EN
UPSCALERS = UPSCALERS_EN
P1_CONTROLS = P1_CONTROLS_EN
P2_CONTROLS = P2_CONTROLS_EN
ANISO = ANISO_EN
ROW = ROW_EN
NAV = NAV_EN
PAGE_HINT = PAGE_HINT_EN
PAGE_SUBTITLE = PAGE_SUBTITLE_EN
GROUP_EN = {"translation": "Translation", "cheats": "Cheats", "controller": "Controller Management", "kbm": "Keyboard & Mouse Controls"}
GROUP_ID = {"translation": "Terjemahan", "cheats": "Cheat", "controller": "Manajemen Controller", "kbm": "Kontrol Keyboard & Mouse"}
GROUP = GROUP_EN
CHEAT_HINT = CHEAT_HINT_EN


def find_game():
    """The first folder under game\\ that holds an eboot.bin."""
    base = ROOT / "game"
    if (base / "eboot.bin").is_file():
        return str(base)
    for child in sorted(base.iterdir()) if base.is_dir() else []:
        if (child / "eboot.bin").is_file():
            return str(child)
    return ""


def load_config():
    cfg = dict(DEFAULTS)
    raw = {}
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        cfg.update(raw)
    except (OSError, ValueError):
        pass
    legacy_routing = raw.get("pad_routing") or raw.get("gamepad_routing")
    if legacy_routing == "p2" and "p1_control" not in raw and "p2_control" not in raw:
        cfg["p1_control"] = "kbm"
        cfg["p2_control"] = "pad1"
    cfg.pop("pad_routing", None)
    cfg.pop("gamepad_routing", None)
    if cfg.get("p1_control") not in ("pad1", "pad2", "kbm"):
        cfg["p1_control"] = "pad1"
    if cfg.get("p2_control") not in ("none", "pad2", "pad1", "kbm"):
        cfg["p2_control"] = "none"
    if cfg.get("language") not in ("id", "en"):
        cfg["language"] = "id"
    if not cfg["game"] or not (Path(cfg["game"]) / "eboot.bin").is_file():
        cfg["game"] = find_game()
    return cfg


def save_config(cfg):
    USER.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def patch_needs_rebuild(game: Path, db_path: Path, patch_dir: Path) -> bool:
    manifest_file = patch_dir / "manifest.json"
    if not manifest_file.is_file():
        return True
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

    # If patch was imported from PS3 patch, do not clobber it with DB rebuild
    if manifest.get("mode") == "ps3_import":
        return False

    # Check DB sha256
    try:
        db_bytes = db_path.read_bytes()
        db_hash = hashlib.sha256(db_bytes).hexdigest()
        if manifest.get("db_sha256") != db_hash:
            return True
    except OSError:
        return True

    # Check written_arcs source arc size/mtime
    written_arcs = manifest.get("written_arcs", [])
    if not written_arcs:
        return True

    _, rom_dir = basara4_translation.resolve_game_paths(game)
    for entry in written_arcs:
        rel = entry.get("relative_path")
        if not rel:
            return True
        src_arc = rom_dir / rel
        if not src_arc.is_file():
            return True
        st = src_arc.stat()
        if entry.get("source_size") != st.st_size:
            return True
        if abs(entry.get("source_mtime", 0) - st.st_mtime) > 1e-4:
            return True
        out_arc = patch_dir / "nativePS4" / "rom" / rel
        if not out_arc.is_file():
            return True

    return False


def read_ini(path):
    values = {}
    try:
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.strip().split("=", 1)
                values[key] = value
    except OSError:
        pass
    return values


def write_ini(path, updates):
    """Update only the given keys; every other line the runtime wrote stays as it was."""
    path = Path(path)
    lines, seen = [], set()
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key = line.split("=", 1)[0] if "=" in line else None
            if key in updates:
                lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                lines.append(line)
    except OSError:
        pass
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def image_ready(game):
    """Whether out\\ holds an image made from this game's current eboot.bin."""
    try:
        stamp = json.loads(STAMP.read_text(encoding="utf-8-sig"))
        return (stamp.get("eboot_sha256") == file_sha256(Path(game) / "eboot.bin")
                and all((OUT / name).is_file() for name in ("boot-linked.bin", "content.bin", "eboot.elf")))
    except (OSError, ValueError):
        return False


def prepare_image(game, progress):
    """Runs the preparation scripts on the game's eboot.bin. Returns an error text or None."""
    OUT.mkdir(parents=True, exist_ok=True)
    USER.mkdir(parents=True, exist_ok=True)
    with open(PREPARE_LOG, "w", encoding="utf-8", errors="replace") as log:
        for index, (name, extra) in enumerate(PIPELINE, 1):
            progress(f"Preparing the game image ({index}/{len(PIPELINE)}): {name[:-3]}…")
            script = [sys.executable, "--run-script", name] if FROZEN else [sys.executable, str(SCRIPTS / name)]
            result = subprocess.run(script + [str(game), "--out", str(OUT)] + extra, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            if result.returncode:
                return f"{name} failed (exit {result.returncode}). See user\\prepare.log"
    STAMP.write_text(json.dumps({"eboot_sha256": file_sha256(Path(game) / "eboot.bin"), "game": str(game)}),
                     encoding="utf-8")
    return None


class Launcher(tk.Tk):
    def __init__(self, play_now=False):
        super().__init__()
        self.cfg = load_config()
        self.proc = None
        self.busy = False
        self.user_stopped = False
        self.pending = None
        self.play_now = play_now
        self.title("Sengoku Basara 4 Sumeragi - Basara 4 PC")
        self.configure(bg=BG)
        self.geometry("1000x620")
        self.resizable(False, False)
        icon = Path(__file__).resolve().parent / "basara4.ico"
        if icon.is_file():
            try:
                self.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self._style()
        self._art()
        self._build()
        self._load_ini_into_ui()
        self._refresh_image_label()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(500, self._poll)
        self.update_info = None
        self.update_btn = None
        if play_now:
            self.withdraw()
            self.after(300, self.on_play)
        else:
            self.after(700, self._check_wav_leftovers)
            self.after(1200, lambda: threading.Thread(target=self._check_update_thread, daemon=True).start())

    # ---- look ----
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=PANEL,
                    lightcolor=PANEL, darkcolor=PANEL, font=FONT)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Head.TLabel", background=PANEL, foreground=GOLD, font=("Segoe UI Semibold", 11))
        s.configure("TRadiobutton", background=PANEL, foreground=FG)
        s.configure("TCheckbutton", background=PANEL, foreground=FG, indicatorbackground=FIELD,
                    indicatorforeground=GOLD, upperbordercolor=EDGE, lowerbordercolor=EDGE)
        s.map("TRadiobutton", background=[("active", PANEL)], foreground=[("active", GOLD)])
        s.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", GOLD)],
              indicatorbackground=[("active", FIELD)])
        s.configure("TButton", background="#2a2622", foreground=FG, padding=(12, 2), borderwidth=1,
                    bordercolor="#2a2622", lightcolor="#2a2622", darkcolor="#2a2622")
        edge = [("disabled", LINE), ("active", "#3a342e")]  # a disabled button keeps a visible outline
        s.map("TButton", background=[("active", "#3a342e"), ("disabled", "#1c1916")],
              foreground=[("disabled", MUTED)], bordercolor=edge, lightcolor=edge, darkcolor=edge)
        s.configure("Play.TButton", background=RED, foreground="#fff", font=("Segoe UI Semibold", 15),
                    padding=(10, 12), bordercolor=RED, lightcolor=RED, darkcolor=RED)
        play_edge = [("disabled", "#4a2522"), ("active", "#b8362d")]
        s.map("Play.TButton", background=[("active", "#b8362d"), ("disabled", "#4a2522")],
              bordercolor=play_edge, lightcolor=play_edge, darkcolor=play_edge)
        s.configure("Seg.Toolbutton", background="#2a2622", foreground=FG, padding=(10, 6), borderwidth=0,
                    anchor="center")
        s.map("Seg.Toolbutton", background=[("selected", GOLD), ("active", "#3a342e")],
              foreground=[("selected", BG)])
        s.configure("TCombobox", arrowcolor=GOLD, bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE,
                    padding=(6, 4), selectbackground=FIELD, selectforeground=FG)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
              bordercolor=[("focus", GOLD)], lightcolor=[("focus", GOLD)], darkcolor=[("focus", GOLD)])
        s.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=EDGE,
                    lightcolor=EDGE, darkcolor=EDGE, padding=(6, 4))
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", GOLD)
        self.option_add("*TCombobox*Listbox.selectForeground", BG)

    def _art(self):
        self.cover = None
        if Image is None or not self.cfg["game"]:
            return
        try:
            self.cover = ImageTk.PhotoImage(
                Image.open(Path(self.cfg["game"]) / "sce_sys" / "icon0.png").convert("RGB").resize((220, 220)))
        except OSError:
            pass

    # ---- layout ----
    def _build(self):
        self._font = tkfont.Font(self, font=FONT)
        self.game_var = tk.StringVar(value=self.cfg.get("game", ""))
        self.game_update_var = tk.StringVar(value=self.cfg.get("game_update", ""))
        self.dlc_var = tk.StringVar(value=self.cfg.get("dlc_folder", ""))
        self.update_view = None
        self.mode_var = tk.StringVar(value=self.cfg["display_mode"])
        self.gpu_var, self.fps_var, self.aniso_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.res_var, self.up_var = tk.StringVar(), tk.StringVar()
        self.vsync_var = tk.BooleanVar(value=self.cfg["vsync"])
        self.rcas_var = tk.BooleanVar(value=self.cfg["rcas"])
        self.audio_var = tk.BooleanVar(value=self.cfg.get("audio_log", False))
        self.wav_var = tk.BooleanVar(value=False)
        self.english_var = tk.BooleanVar(value=self.cfg.get("english_patch", False))
        self.mouse_camera_var = tk.BooleanVar(value=self.cfg.get("mouse_camera", True))
        self.mouse_sens_var = tk.IntVar(value=self.cfg.get("mouse_sens", 100))
        self.mouse_invert_x_var = tk.BooleanVar(value=self.cfg.get("mouse_invert_x", False))
        self.mouse_invert_y_var = tk.BooleanVar(value=self.cfg.get("mouse_invert_y", False))
        is_id = self.cfg.get("language") == "id"
        p1_opts = P1_CONTROLS_ID if is_id else P1_CONTROLS_EN
        p2_opts = P2_CONTROLS_ID if is_id else P2_CONTROLS_EN
        cur_p1 = self.cfg.get("p1_control", "pad1")
        cur_p2 = self.cfg.get("p2_control", "none")
        self.p1_control_var = tk.StringVar(value=next((n for n, v in p1_opts if v == cur_p1), p1_opts[0][0]))
        self.p2_control_var = tk.StringVar(value=next((n for n, v in p2_opts if v == cur_p2), p2_opts[0][0]))
        self.cheat_vars = {key: tk.BooleanVar(value=False) for key in CHEAT_KEYS}  # live in the runtime ini

        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True, padx=14, pady=14)
        left = tk.Frame(shell, bg=PANEL, width=240, padx=16, pady=16)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        self._build_left(left)
        right = tk.Frame(shell, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)
        self._build_right(right)
        game = self.cfg["game"]
        self.show_page("display" if game and (Path(game) / "eboot.bin").is_file() else "game")

    def _build_left(self, left):
        bottom = tk.Frame(left, bg=PANEL)  # packed first so a long status can never push it out
        bottom.pack(side="bottom", fill="x")
        self.mark_frame = bottom
        self.version_label = tk.Label(bottom, text=f"v{VERSION}", bg=PANEL, fg=MUTED, font=FONT_SMALL)
        self.version_label.pack(pady=(0, 6))
        self.check_btn = ttk.Button(bottom, text="Check for updates", command=self.check_update_manual)
        self.check_btn.pack(fill="x")
        credit = tk.Frame(bottom, bg=PANEL, cursor="hand2")  # clicking it opens the credits
        credit.pack(pady=(10, 0))
        c_line1 = tk.Frame(credit, bg=PANEL)
        c_line1.pack()
        tk.Label(c_line1, text=f"{BRAND} ", bg=PANEL, fg=FG, font=FONT_SMALL, bd=0).pack(side="left")
        tk.Label(c_line1, text=f"({FORK} Fork)", bg=PANEL, fg=FORK_ACCENT, font=("Segoe UI", 8, "bold"), bd=0).pack(side="left")
        c_line2 = tk.Frame(credit, bg=PANEL)
        c_line2.pack(pady=(1, 0))
        tk.Label(c_line2, text=f"\u00b7 Original port by {AUTHOR}", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "italic"), bd=0).pack(side="left")
        for widget in [credit, c_line1, c_line2] + c_line1.winfo_children() + c_line2.winfo_children():
            widget.bind("<Button-1>", lambda _e: self.show_credits())

        if self.cover:
            frame = tk.Frame(left, bg=GOLD, padx=1, pady=1)
            frame.pack()
            tk.Label(frame, image=self.cover, bd=0, bg=PANEL).pack()
        tk.Label(left, text="戦国BASARA4 皇", bg=PANEL, fg=GOLD, font=("Segoe UI Semibold", 12)).pack(pady=(12, 0))
        tk.Label(left, text="Sengoku Basara 4 Sumeragi", bg=PANEL, fg=FG, font=FONT).pack()
        tk.Label(left, text="CUSA01159", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack()
        self.status = tk.Label(left, text="Ready", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=208)
        self.status.pack(pady=(14, 8))
        self.play = ttk.Button(left, text="PLAY", style="Play.TButton", command=self.on_play)
        self.play.pack(fill="x", pady=(0, 6))
        self.stop = ttk.Button(left, text="Stop game", command=self.on_stop, state="disabled")
        self.stop.pack(fill="x")

    def _build_right(self, right):
        nav = tk.Frame(right, bg=PANEL, width=150, pady=14)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        tk.Frame(right, bg=LINE, width=1).pack(side="left", fill="y")
        self.nav = {}
        for key, text in NAV:
            item = tk.Frame(nav, bg=PANEL)
            item.pack(fill="x")
            bar = tk.Frame(item, bg=PANEL, width=3)
            bar.pack(side="left", fill="y")
            label = tk.Label(item, text=text, bg=PANEL, fg=MUTED, font=("Segoe UI", 11), anchor="w", padx=13,
                             pady=9, cursor="hand2", takefocus=1, highlightthickness=1, highlightbackground=PANEL,
                             highlightcolor=GOLD)
            label.pack(side="left", fill="x", expand=True)
            for widget in (item, bar, label):
                widget.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
            label.bind("<Return>", lambda _e, k=key: self.show_page(k))
            label.bind("<space>", lambda _e, k=key: self.show_page(k))
            self.nav[key] = (item, bar, label)

        content = tk.Frame(right, bg=PANEL)
        content.pack(side="left", fill="both", expand=True)
        tip = tk.Frame(content, bg=PANEL, height=35)  # the docked hint bar
        tip.pack(side="bottom", fill="x")
        tip.pack_propagate(False)
        tk.Frame(tip, bg=LINE, height=1).pack(fill="x")
        tk.Label(tip, text="?", bg=PANEL, fg=GOLD, font=("Segoe UI Semibold", 9)).pack(side="left", padx=(20, 6))
        self.hint = tk.Label(tip, text="", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.hint.pack(side="left", fill="x", expand=True)
        host = tk.Frame(content, bg=PANEL)
        host.pack(side="top", fill="both", expand=True, padx=20, pady=(16, 0))
        self.pages, self._lit_bar = {}, None
        for key, name in NAV:
            page = self.pages[key] = tk.Frame(host, bg=PANEL)
            title = tk.Frame(page, bg=PANEL)
            title.pack(fill="x", pady=(0, 8))
            tk.Label(title, text=name, bg=PANEL, fg=GOLD, font=("Segoe UI Semibold", 14)).pack(side="left")
            if key in PAGE_SUBTITLE:
                tk.Label(title, text=PAGE_SUBTITLE[key], bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(
                    side="left", padx=12, pady=(4, 0))
        self._page_game(self.pages["game"])
        self._page_display(self.pages["display"])
        self._page_controls(self.pages["controls"])
        self._page_mods(self.pages["mods"])
        self._page_tools(self.pages["tools"])

    def show_page(self, key):
        for name, page in self.pages.items():
            page.pack_forget()
            item, bar, label = self.nav[name]
            on = name == key
            fill = HOVER if on else PANEL
            item.config(bg=fill)
            label.config(bg=fill, fg=GOLD if on else MUTED, highlightbackground=fill,
                         font=("Segoe UI Semibold" if on else "Segoe UI", 11))
            bar.config(bg=GOLD if on else PANEL)
        self.pages[key].pack(fill="both", expand=True)
        self._light(None)
        self.hint.config(text=PAGE_HINT[key])
        if key == "controls":
            self._refresh_gamepads()
            self._pad_text()

    # ---- settings rows ----
    def _light(self, bar):
        """The gold bar of the row being hovered or focused (None: no row)."""
        if self._lit_bar is not None:
            self._lit_bar.config(bg=PANEL)
        self._lit_bar = bar
        if bar is not None:
            bar.config(bg=GOLD)

    def _row(self, page, key, make, width=250, sub=False, stack=False, height=30):
        """One ruled row: label left, the control `make(holder)` right. Hovering or focusing it shows its hint."""
        label, hint = ROW[key]
        row = tk.Frame(page, bg=PANEL)
        row.pack(fill="x")
        row.columnconfigure(1, weight=1)
        row.rowconfigure(0, minsize=40)
        bar = tk.Frame(row, bg=PANEL, width=3)
        bar.grid(row=0, column=0, sticky="ns")
        tk.Label(row, text=label, bg=PANEL, fg=FG, font=FONT).grid(row=0, column=1, sticky="w",
                                                                    padx=(10 + (23 if sub else 0), 0))
        holder = tk.Frame(row, bg=PANEL)
        if stack:  # a value too long for the right column gets the full width under its label
            holder.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(13, 10), pady=(0, 8))
        else:
            holder.grid(row=0, column=2, sticky="e", padx=(0, 10))
        if width and not stack:
            holder.config(width=width, height=height)
            holder.pack_propagate(False)
        make(holder)
        tk.Frame(row, bg=RULE, height=1).grid(row=2, column=0, columnspan=3, sticky="ew")

        def show(_event=None):
            self._light(bar)
            self.hint.config(text=hint)

        def wire(widget):
            widget.bind("<Enter>", show, add="+")
            widget.bind("<FocusIn>", show, add="+")  # the same hint for keyboard and pad users
            for child in widget.winfo_children():
                wire(child)
        wire(row)
        return row

    def _group(self, page, key):
        tk.Label(page, text=GROUP[key].upper(), bg=PANEL, fg=GOLD, font=("Segoe UI Semibold", 9),
                 anchor="w").pack(fill="x", padx=13, pady=(14, 2))

    def _combo_row(self, page, key, var, values):
        need = max(self._font.measure(v) for v in values) + 48  # text + arrow + padding: nothing truncated
        self._row(page, key, lambda h: ttk.Combobox(h, textvariable=var, state="readonly", values=values).pack(
            fill="both", expand=True), width=max(250, need), stack=need > 330)

    def _check_row(self, page, key, var, command=None, sub=False):
        self._row(page, key, lambda h: ttk.Checkbutton(h, text="On", variable=var, command=command).pack(side="left"),
                  sub=sub)

    def _slider_row(self, page, key, var, from_, to):
        def make(holder):
            lbl = ttk.Label(holder, text=f"{int(var.get())}%", style="Panel.TLabel", width=5)
            lbl.pack(side="right")
            def on_scroll(val):
                lbl.config(text=f"{int(float(val))}%")
            scale = ttk.Scale(holder, from_=from_, to=to, variable=var, command=on_scroll, length=120)
            scale.pack(side="right", padx=6)
        self._row(page, key, make, width=200)

    def _button_row(self, page, key, text, command, attr=None):
        def make(holder):
            button = ttk.Button(holder, text=text, command=command)
            button.pack(fill="both", expand=True)
            if attr:
                setattr(self, attr, button)
        self._row(page, key, make, height=34)

    def _page_game(self, page):
        def folder(holder):
            ttk.Button(holder, text="Browse…", command=self._browse).pack(side="right", padx=(6, 0))
            entry = ttk.Entry(holder, textvariable=self.game_var)
            entry.pack(side="left", fill="both", expand=True)
            entry.bind("<Map>", lambda _e: entry.xview_moveto(1))  # the folder name matters more than the drive
        self._row(page, "game_folder", folder, width=340)

        def image(holder):
            self.image_label = tk.Label(holder, bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=330,
                                        justify="right", anchor="e")
            self.image_label.pack(side="right")
        self._row(page, "game_image", image, width=None)
        self.game_var.trace_add("write", lambda *_: self._refresh_image_label())

        def update_folder(holder):
            ttk.Button(holder, text="Clear", command=self._clear_update).pack(side="right", padx=(4, 0))
            ttk.Button(holder, text="Browse…", command=self._browse_update).pack(side="right", padx=(6, 0))
            entry = ttk.Entry(holder, textvariable=self.game_update_var)
            entry.pack(side="left", fill="both", expand=True)
            entry.bind("<Map>", lambda _e: entry.xview_moveto(1))
        self._row(page, "game_update", update_folder, width=340)
        self.game_update_var.trace_add("write", lambda *_: self._refresh_image_label())

        def dlc_folder(holder):
            ttk.Button(holder, text="Clear", command=self._clear_dlc).pack(side="right", padx=(4, 0))
            ttk.Button(holder, text="Browse…", command=self._browse_dlc).pack(side="right", padx=(6, 0))
            entry = ttk.Entry(holder, textvariable=self.dlc_var)
            entry.pack(side="left", fill="both", expand=True)
            entry.bind("<Map>", lambda _e: entry.xview_moveto(1))
        self._row(page, "dlc_folder", dlc_folder, width=340)

    def _page_display(self, page):
        def modes(holder):
            for label, key in DISPLAY_MODES:
                ttk.Radiobutton(holder, text=label, value=key, variable=self.mode_var, style="Seg.Toolbutton").pack(
                    side="left", fill="both", expand=True, padx=1)
        self._row(page, "display_mode", modes, width=340)
        self._combo_row(page, "gpu", self.gpu_var, [name for _, name, _ in SYSTEM_GPUS])
        self._combo_row(page, "resolution", self.res_var, [n for n, _ in RESOLUTIONS])
        self._combo_row(page, "fps", self.fps_var, [n for n, _ in FPS_LIMITS])
        self._combo_row(page, "upscale", self.up_var, [n for n, _ in UPSCALERS])
        self._combo_row(page, "aniso", self.aniso_var, [n for n, _ in ANISO])
        self._check_row(page, "vsync", self.vsync_var)
        self._check_row(page, "rcas", self.rcas_var)

    def _page_controls(self, page):
        canvas = tk.Canvas(page, bg=PANEL, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        content = tk.Frame(canvas, bg=PANEL)
        content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        cwin = canvas.create_window((0, 0), window=content, anchor="nw")

        def on_canvas_configure(e):
            canvas.itemconfig(cwin, width=e.width)

        canvas.bind("<Configure>", on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        def _bind_mw(w):
            w.bind("<MouseWheel>", _on_mousewheel, add="+")
            for c in w.winfo_children():
                _bind_mw(c)

        self._group(content, "controller")

        is_id = self.cfg.get("language") == "id"

        # 1. Controller Detection & Live Status Card (per slot)
        def pad_detect(holder):
            status_card = tk.Frame(holder, bg=FIELD, highlightbackground=EDGE, highlightthickness=1, padx=10, pady=6)
            status_card.pack(fill="x", expand=True)
            refresh_txt = "Deteksi Ulang" if is_id else "Refresh"
            btn = ttk.Button(status_card, text=refresh_txt, command=self._refresh_gamepads)
            btn.pack(side="right", padx=(8, 0))
            slots_box = tk.Frame(status_card, bg=FIELD)
            slots_box.pack(side="left", fill="x", expand=True)
            self.pad_slot_labels = []
            for _ in range(2):
                lbl = tk.Label(slots_box, text="", bg=FIELD, fg=MUTED, font=("Segoe UI", 9),
                               justify="left", anchor="w")
                lbl.pack(fill="x", anchor="w", pady=1)
                self.pad_slot_labels.append(lbl)
            self._refresh_gamepads()

        self._row(content, "pad_detect", pad_detect, stack=True)

        # 2. Player 1 & Player 2 Control Assignment
        p1_opts = P1_CONTROLS_ID if is_id else P1_CONTROLS_EN
        p2_opts = P2_CONTROLS_ID if is_id else P2_CONTROLS_EN
        self._combo_row(content, "p1_control", self.p1_control_var, [n for n, _ in p1_opts])
        self._combo_row(content, "p2_control", self.p2_control_var, [n for n, _ in p2_opts])

        # 3. Controller Mapping & Visual Layout Indicators
        def pad_status(holder):
            self.pad_label = tk.Label(holder, bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=340,
                                      justify="right", anchor="e")
            self.pad_label.pack(side="right")

        self._row(content, "pad", pad_status, width=None)
        self._pad_text()

        def pad_actions(holder):
            ttk.Button(holder, text="Map All Buttons…", command=lambda: self.map_pad([])).pack(
                side="left", padx=2, fill="x", expand=True)
            ttk.Button(holder, text="Map Sticks Only…", command=lambda: self.map_pad(["--sticks"])).pack(
                side="left", padx=2, fill="x", expand=True)
            ttk.Button(holder, text="Reset Custom Mapping", command=self.reset_map).pack(
                side="left", padx=2, fill="x", expand=True)

        self._row(content, "pad_actions", pad_actions, width=380)

        # Interactive Visual Layout Guide Card
        card = tk.Frame(content, bg=FIELD, highlightbackground=EDGE, highlightthickness=1)
        card.pack(fill="x", padx=10, pady=(8, 6))

        top_bar = tk.Frame(card, bg=FIELD)
        top_bar.pack(fill="x", padx=10, pady=(6, 4))
        guide_title = "🎮 Layout Aksi Controller" if is_id else "🎮 Controller Layout & Action Guide"
        tk.Label(top_bar, text=guide_title, bg=FIELD, fg=GOLD, font=("Segoe UI Semibold", 9)).pack(side="left")

        btn_ps = tk.Button(top_bar, text="PlayStation", font=("Segoe UI", 8), bd=0, padx=8, pady=2, cursor="hand2")
        btn_xbox = tk.Button(top_bar, text="Xbox (XInput)", font=("Segoe UI", 8), bd=0, padx=8, pady=2, cursor="hand2")
        btn_ps.pack(side="right", padx=2)
        btn_xbox.pack(side="right", padx=2)

        diag_frame = tk.Frame(card, bg=FIELD)
        diag_frame.pack(fill="x", padx=10, pady=(2, 6))

        if is_id:
            xbox_data = [
                (("LS", "Gerak Karakter"), ("RS", "Putar Kamera")),
                (("D-Pad", "Perintah Sekutu"), ("X", "Serangan Biasa")),
                (("LB", "Tangkis / Evade"), ("Y", "Skill Spesial 1")),
                (("LT", "Sengoku Drive"), ("A", "Lompat / Konfirmasi")),
                (("Back", "Ejekan (Taunt)"), ("B", "Serangan Basara")),
                (("Start", "Menu Jeda / Pause"), ("RB / RT", "Skill 2 / Skill 3")),
            ]
            ps_data = [
                (("LS / L3", "Gerak Karakter"), ("RS / R3", "Putar Kamera")),
                (("D-Pad", "Perintah Sekutu"), ("Kotak (□)", "Serangan Biasa")),
                (("L1", "Tangkis / Evade"), ("Segitiga (△)", "Skill Spesial 1")),
                (("L2", "Sengoku Drive"), ("Silang (✕)", "Lompat / Konfirmasi")),
                (("Touchpad", "Ejekan (Taunt)"), ("Lingkaran (◯)", "Serangan Basara")),
                (("Options", "Menu Jeda / Pause"), ("R1 / R2", "Skill 2 / Skill 3")),
            ]
        else:
            xbox_data = [
                (("LS", "Move Character"), ("RS", "Camera Look")),
                (("D-Pad", "Ally Orders"), ("X", "Normal Attack")),
                (("LB", "Guard / Evade"), ("Y", "Special Skill 1")),
                (("LT", "Sengoku Drive"), ("A", "Jump / Confirm")),
                (("Back", "Taunt / Provoke"), ("B", "Basara Attack")),
                (("Start", "Pause Menu"), ("RB / RT", "Skill 2 / Skill 3")),
            ]
            ps_data = [
                (("LS / L3", "Move Character"), ("RS / R3", "Camera Look")),
                (("D-Pad", "Ally Orders"), ("Square (□)", "Normal Attack")),
                (("L1", "Guard / Evade"), ("Triangle (△)", "Special Skill 1")),
                (("L2", "Sengoku Drive"), ("Cross (✕)", "Jump / Confirm")),
                (("Touchpad", "Taunt / Provoke"), ("Circle (◯)", "Basara Attack")),
                (("Options", "Pause Menu"), ("R1 / R2", "Skill 2 / Skill 3")),
            ]

        rows_widgets = []
        for r in range(6):
            f_l = tk.Frame(diag_frame, bg=FIELD)
            f_l.grid(row=r, column=0, sticky="w", pady=1, padx=(0, 10))
            b_l = tk.Label(f_l, text="", bg="#25211c", fg=GOLD, font=("Segoe UI", 8, "bold"), width=12, anchor="center")
            b_l.pack(side="left")
            a_l = tk.Label(f_l, text="", bg=FIELD, fg=FG, font=("Segoe UI", 8))
            a_l.pack(side="left", padx=(4, 0))

            f_r = tk.Frame(diag_frame, bg=FIELD)
            f_r.grid(row=r, column=1, sticky="w", pady=1)
            b_r = tk.Label(f_r, text="", bg="#25211c", fg=GOLD, font=("Segoe UI", 8, "bold"), width=12, anchor="center")
            b_r.pack(side="left")
            a_r = tk.Label(f_r, text="", bg=FIELD, fg=FG, font=("Segoe UI", 8))
            a_r.pack(side="left", padx=(4, 0))

            rows_widgets.append(((b_l, a_l), (b_r, a_r)))

        def show_layout(mode):
            data = xbox_data if mode == "xbox" else ps_data
            for i, ((btn1, act1), (btn2, act2)) in enumerate(data):
                (b_l, a_l), (b_r, a_r) = rows_widgets[i]
                b_l.config(text=btn1)
                a_l.config(text=act1)
                b_r.config(text=btn2)
                a_r.config(text=act2)
            if mode == "xbox":
                btn_xbox.config(bg=GOLD, fg=BG, font=("Segoe UI", 8, "bold"))
                btn_ps.config(bg="#26211d", fg=MUTED, font=("Segoe UI", 8))
            else:
                btn_ps.config(bg=GOLD, fg=BG, font=("Segoe UI", 8, "bold"))
                btn_xbox.config(bg="#26211d", fg=MUTED, font=("Segoe UI", 8))

        btn_xbox.config(command=lambda: show_layout("xbox"))
        btn_ps.config(command=lambda: show_layout("ps"))
        show_layout("xbox")

        # 4. Keyboard & Mouse Controls Settings
        self._group(content, "kbm")
        self._button_row(content, "keymap", "Edit Custom Keymap…", self.open_keymap_ui)
        self._check_row(content, "mouse_camera", self.mouse_camera_var)
        self._slider_row(content, "mouse_sens", self.mouse_sens_var, 20, 300)
        self._check_row(content, "mouse_invert_x", self.mouse_invert_x_var)
        self._check_row(content, "mouse_invert_y", self.mouse_invert_y_var)

        _bind_mw(canvas)
        _bind_mw(content)

    def _page_mods(self, page):
        self._group(page, "translation")
        self._check_row(page, "english", self.english_var)
        self._button_row(page, "import_patch", "Import…", self.on_import_ps3_patch, "import_patch_btn")
        self._button_row(page, "rebuild_patch", "Rebuild", self.on_rebuild_patch, "rebuild_patch_btn")
        self._group(page, "cheats")
        for key in CHEAT_KEYS:
            self._check_row(page, key, self.cheat_vars[key])

    def _page_tools(self, page):
        self._button_row(page, "open_log", "Open", self.open_log)
        self._button_row(page, "open_audio_log", "Open", self.open_audio_log)
        self._button_row(page, "open_saves", "Open", self.open_saves)
        self._check_row(page, "audio_log", self.audio_var, self._audio_toggled)
        self._check_row(page, "audio_wav", self.wav_var, self._wav_toggled, sub=True)

    # ---- helpers ----
    def _browse(self):
        path = filedialog.askdirectory(initialdir=self.game_var.get() or str(ROOT), mustexist=True)
        if path:
            self.game_var.set(os.path.normpath(path))

    def _refresh_image_label(self):
        game = self.game_var.get().strip()
        update = self.game_update_var.get().strip() if hasattr(self, "game_update_var") else ""
        target = update if (update and (Path(update) / "eboot.bin").is_file()) else game
        if not target or not (Path(target) / "eboot.bin").is_file():
            text = "No eboot.bin in this folder."
        elif image_ready(target):
            text = "Game image ready."
        else:
            text = "The game image is made on the first start (about a minute); again if eboot.bin changes."
        self.image_label.config(text=text, fg=OK if text == "Game image ready." else MUTED)

    def _cleanup_update_view(self):
        if getattr(self, "update_view", None):
            try:
                game_update.remove_view(self.update_view)
            except Exception:
                pass
            self.update_view = None

    def _get_effective_game(self, game):
        update_dir = self.cfg.get("game_update", "").strip() if isinstance(self.cfg.get("game_update"), str) else ""
        if update_dir and game:
            update_path = Path(update_dir)
            if update_path.is_dir() and (update_path / "eboot.bin").is_file():
                self._cleanup_update_view()
                self.update_view = game_update.create_view(game, update_path, OUT)
                return self.update_view
        self._cleanup_update_view()
        return game

    def _browse_update(self):
        path = filedialog.askdirectory(initialdir=self.game_update_var.get() or self.game_var.get() or str(ROOT), mustexist=True)
        if not path:
            return
        p = Path(path).resolve()
        sfo = patches.find_sfo(p)
        if not sfo or not sfo.is_file():
            self.status.config(text="Error: missing sce_sys/param.sfo")
            return
        tid = patches.game_title_id(p)
        ver = patches.game_app_ver(p)
        if not tid:
            self.status.config(text="Error: missing TITLE_ID in update")
            return
        base_game = self.game_var.get().strip()
        if base_game and Path(base_game).is_dir():
            base_tid = patches.game_title_id(base_game)
            if base_tid and base_tid != tid:
                self.status.config(text=f"Error: title ID mismatch ({tid} vs {base_tid})")
                return
        if not (p / "eboot.bin").is_file():
            self.status.config(text="Error: missing eboot.bin in update folder")
            return
        self.game_update_var.set(os.path.normpath(str(p)))
        self._collect()
        self._cleanup_update_view()
        self._refresh_image_label()
        prefix = "" if ver and ver.lower().startswith("v") else "v"
        self.status.config(text=f"Update {prefix}{ver} found" if ver else "Update found")

    def _clear_update(self):
        self.game_update_var.set("")
        self._collect()
        self._cleanup_update_view()
        self._refresh_image_label()
        self.status.config(text="Game update cleared.")

    def _browse_dlc(self):
        path = filedialog.askdirectory(initialdir=self.dlc_var.get() or str(ROOT), mustexist=True)
        if not path:
            return
        p = Path(path).resolve()
        try:
            base_game = self.game_var.get().strip()
            tid = (patches.game_title_id(base_game) if base_game and Path(base_game).is_dir() else None) or "CUSA01159"
            addons = dlc.discover_addons(p, title_id=tid)
            if not addons:
                self.status.config(text=f"No add-ons found for {tid}")
            else:
                count = len(addons)
                self.status.config(text=f"{count} add-on{'s' if count != 1 else ''} found")
            self.dlc_var.set(os.path.normpath(str(p)))
            self._collect()
        except Exception as e:
            self.status.config(text=f"DLC error: {e}")

    def _clear_dlc(self):
        self.dlc_var.set("")
        self._collect()
        self.status.config(text="DLC folder cleared.")

    def _ini(self):
        if OLD_INI.exists() and not INI.exists():
            os.replace(OLD_INI, INI)  # settings made before the runtime was renamed
        return INI

    def _load_ini_into_ui(self):
        ini = read_ini(self._ini())
        mode = ini.get("display_mode", self.cfg["display_mode"])
        if mode in {k for _, k in DISPLAY_MODES}:
            self.mode_var.set(mode)
        if "vsync" in ini:
            self.vsync_var.set(ini["vsync"] == "1")
        if "rcas" in ini:
            self.rcas_var.set(ini["rcas"] == "1")
        res = ini.get("render_resolution", self.cfg["resolution"])
        self.res_var.set(next((n for n, v in RESOLUTIONS if v == res), RESOLUTIONS[0][0]))
        upscaler_val = ini.get("upscaler")
        fsr1_val = ini.get("fsr1")
        if upscaler_val == "fsr3":
            up = "fsr3"
        elif upscaler_val == "dlss":
            up = "dlss"
        elif fsr1_val == "1":
            up = "fsr1"
        elif upscaler_val is not None or fsr1_val is not None:
            up = "off"
        else:
            up = self.cfg.get("upscale", "off")
        self.up_var.set(next((n for n, v in UPSCALERS if v == up), UPSCALERS[0][0]))
        fps = int(self.cfg["fps_limit"])
        self.fps_var.set(next((n for n, v in FPS_LIMITS if v == fps), FPS_LIMITS[0][0]))
        an = int(ini["anisotropy"]) if ini.get("anisotropy", "").isdigit() else self.cfg["anisotropy"]
        self.aniso_var.set(next((n for n, v in ANISO if v == an), "Off"))
        for key, var in self.cheat_vars.items():  # the in-game menu writes these too
            var.set(ini.get(key) == "1")
        saved_gpu = self.cfg.get("gpu_index", -1)
        self.gpu_var.set(next((name for idx, name, _ in SYSTEM_GPUS if idx == saved_gpu), SYSTEM_GPUS[0][1]))

    def _audio_toggled(self):
        if not self.audio_var.get():
            self.wav_var.set(False)  # the capture belongs to the log

    def _wav_toggled(self):
        if not self.wav_var.get():
            return
        if messagebox.askyesno("WAV capture", WAV_WARNING, icon="warning", default="no", parent=self):
            self.audio_var.set(True)
        else:
            self.wav_var.set(False)

    def _check_wav_leftovers(self):
        files, total = wav_captures()
        if not files or total < 50 * 2**20:
            return
        if messagebox.askyesno("Audio captures", f"user\\audio_wav holds {format_size(total)} of audio captures "
                               f"({len(files)} files) from an earlier session.\n\nDelete them to free the space?",
                               default="no", parent=self):
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    pass

    def _pad_text(self):
        if hasattr(self, "pad_label"):
            is_id = self.cfg.get("language") == "id"
            if MAPPING.exists():
                txt = "Status: Pemetaan Kustom aktif (gamepad_mapping.txt)" if is_id else "Status: Custom mapping active (gamepad_mapping.txt)"
                self.pad_label.config(text=txt, fg=GOLD)
            else:
                txt = "Status: Default XInput / SDL Database aktif" if is_id else "Status: Default XInput / SDL Database active"
                self.pad_label.config(text=txt, fg=MUTED)

    def _refresh_gamepads(self):
        pads = detect_connected_gamepads()
        is_id = self.cfg.get("language") == "id"
        not_detected = (
            "(Belum terdeteksi - otomatis aktif saat dicolok)"
            if is_id
            else "(Not detected yet - will connect automatically when plugged in)"
        )
        if hasattr(self, "pad_slot_labels"):
            for idx, lbl in enumerate(self.pad_slot_labels):
                if idx < len(pads):
                    lbl.config(text=f"🎮 Gamepad {idx + 1}: {pads[idx]}", fg=GOLD)
                else:
                    lbl.config(text=f"🎮 Gamepad {idx + 1}: {not_detected}", fg=MUTED)

    def _collect(self):
        selected_gpu_name = self.gpu_var.get()
        selected_gpu_idx = next((idx for idx, name, _ in SYSTEM_GPUS if name == selected_gpu_name), -1)
        p1_str = self.p1_control_var.get() if hasattr(self, "p1_control_var") else "pad1"
        p2_str = self.p2_control_var.get() if hasattr(self, "p2_control_var") else "none"
        p1_val = P1_CONTROL_MAP.get(p1_str, "pad1")
        p2_val = P2_CONTROL_MAP.get(p2_str, "none")
        self.cfg.update(
            game=self.game_var.get().strip(),
            game_update=self.game_update_var.get().strip() if hasattr(self, "game_update_var") else "",
            dlc_folder=self.dlc_var.get().strip() if hasattr(self, "dlc_var") else "",
            display_mode=self.mode_var.get(), vsync=self.vsync_var.get(),
            rcas=self.rcas_var.get(), fps_limit=dict(FPS_LIMITS)[self.fps_var.get()],
            resolution=dict(RESOLUTIONS)[self.res_var.get()],
            upscale=dict(UPSCALERS).get(self.up_var.get(), "off"),
            anisotropy=dict(ANISO)[self.aniso_var.get()], audio_log=self.audio_var.get(),
            audio_wav=self.audio_var.get() and self.wav_var.get(),
            gpu_index=selected_gpu_idx,
            english_patch=self.english_var.get(),
            mouse_camera=self.mouse_camera_var.get(),
            mouse_sens=int(self.mouse_sens_var.get()),
            mouse_invert_x=self.mouse_invert_x_var.get(),
            mouse_invert_y=self.mouse_invert_y_var.get(),
            p1_control=p1_val,
            p2_control=p2_val)
        save_config(self.cfg)

        up_val = self.cfg.get("upscale", "off")
        if up_val == "fsr1":
            fsr1_val, upscaler_val = 1, "off"
        elif up_val == "fsr3":
            fsr1_val, upscaler_val = 0, "fsr3"
        elif up_val == "dlss":
            fsr1_val, upscaler_val = 0, "dlss"
        else:
            fsr1_val, upscaler_val = 0, "off"

        write_ini(self._ini(), {
            "display_mode": self.cfg["display_mode"], "vsync": int(self.cfg["vsync"]),
            "fps_limit": self.cfg["fps_limit"], "rcas": int(self.cfg["rcas"]),
            "anisotropy": self.cfg["anisotropy"], "render_resolution": self.cfg["resolution"],
            "fsr1": fsr1_val,
            "upscaler": upscaler_val,
            "gpu_id": self.cfg.get("gpu_index", -1),
            **{key: int(var.get()) for key, var in self.cheat_vars.items()}})

    def _env(self):
        env = os.environ.copy()
        env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
        env["SPARTAN_GPU_USER_DIR"] = str(USER)
        env["SPARTAN_CONFIG"] = str(self._ini())
        env["SPARTAN_WINDOW_TITLE"] = "Sengoku Basara 4 Sumeragi - Basara 4 PC"
        gpu_idx = self.cfg.get("gpu_index", -1)
        if gpu_idx != -1:
            env["SPARTAN_GPU_ID"] = str(gpu_idx)
        else:
            # Auto: If discrete GPU exists, prioritize it automatically
            disc_idx = next((idx for idx, _, is_disc in SYSTEM_GPUS if is_disc and idx >= 0), None)
            if disc_idx is not None:
                env["SPARTAN_GPU_ID"] = str(disc_idx)
            else:
                env.pop("SPARTAN_GPU_ID", None)
        avail_mb = check_commit_limit_mb()
        if avail_mb is not None and avail_mb < 3200:
            env["SPARTAN_DMEM_MB"] = "2048"
        elif avail_mb is not None and avail_mb < 4608:
            env["SPARTAN_DMEM_MB"] = "2560"
        if WINDOW_ICON.exists():
            env["SPARTAN_WINDOW_ICON"] = str(WINDOW_ICON)
        if MAPPING.exists():
            env["SDL_GAMECONTROLLERCONFIG_FILE"] = str(MAPPING)
        p1_ctrl = self.cfg.get("p1_control", "pad1")
        p2_ctrl = self.cfg.get("p2_control", "none")
        env["SPARTAN_PAD_P1"] = p1_ctrl
        env["SPARTAN_PAD_P2"] = p2_ctrl
        if p1_ctrl == "kbm" and p2_ctrl == "pad1":
            env["SPARTAN_COOP_PAD_TO_P2"] = "1"
        else:
            env.pop("SPARTAN_COOP_PAD_TO_P2", None)
        if self.cfg.get("audio_log"):  # one timestamped line per audio event, for bug reports
            env["SPARTAN_AUDIO_DEBUG"] = str(AUDIO_LOG)
        if self.cfg.get("audio_log") and self.cfg.get("audio_wav"):  # needs about 70 MB per minute
            env["SPARTAN_AUDIO_WAV_DIR"] = str(AUDIO_WAV)
        if self.cfg["fps_limit"] != 60:  # uncapped: 480 Hz vblank, the present thread paces to the limit
            env["SPARTAN_VBLANK_HZ"] = "0"
            env["SPARTAN_FPS_LIMIT"] = str(self.cfg["fps_limit"])

        # Keyboard & Mouse Control Mapping
        user_keymap = dict(DEFAULT_PC_KEYMAP)
        user_keymap.update(self.cfg.get("keymap", {}))
        # Format spec: button=name,button=name,...
        spec_parts = [f"{btn}={src}" for btn, src in user_keymap.items() if src]
        if spec_parts:
            env["SPARTAN_KEY_MAP"] = ",".join(spec_parts)

        if self.cfg.get("mouse_camera", True):
            env["SPARTAN_MOUSE_LOCK"] = "1"
            env["SPARTAN_MOUSE_AIM_STYLE"] = "0"  # 0: relative / free camera rotation
            sens = int(self.cfg.get("mouse_sens", 100))
            env["SPARTAN_MOUSE_AIM_SENSITIVITY"] = str(sens)
            if self.cfg.get("mouse_invert_x", False):
                env["SPARTAN_MOUSE_INVERT_X"] = "1"
            if self.cfg.get("mouse_invert_y", False):
                env["SPARTAN_MOUSE_INVERT_Y"] = "1"
        else:
            env["SPARTAN_MOUSE_LOCK"] = "0"

        return env

    # ---- starting the game ----
    def on_play(self):
        if self.busy or self.proc:
            return
        self.user_stopped = False
        self._collect()
        game = Path(self.cfg["game"]) if self.cfg["game"] else None
        exe = CORE
        if not game or not (game / "eboot.bin").is_file():
            self._deiconify()
            messagebox.showerror("Cannot start", "Choose the game folder (the one with eboot.bin).")
            return
        if not exe.is_file():
            self._deiconify()
            messagebox.showerror("Cannot start", f"Runtime not found: {exe}")
            return
        avail_mb = check_commit_limit_mb()
        if avail_mb is not None and avail_mb < 2560:
            if not messagebox.askyesno(
                "Virtual Memory Sangat Rendah",
                f"Sisa alokasi memori virtual Windows Anda hanya ~{avail_mb} MB.\n"
                "Game membutuhkan setidaknya ~3584 MB (3.5 GB) virtual memory.\n\n"
                "Jika game crash dengan Error 1455, pastikan Virtual Memory (Paging File) aktif di Windows.\n\n"
                "Tetap jalankan game?"):
                return
        self.busy = True
        self.play.config(state="disabled")
        self.pending = None
        threading.Thread(target=self._prepare_then_launch, args=(game, exe), daemon=True).start()
        self.after(200, self._wait_prepare)

    def on_import_ps3_patch(self):
        if self.busy or self.proc:
            return
        game = Path(self.cfg["game"]) if self.cfg.get("game") else None
        if not game or not (game / "eboot.bin").is_file():
            messagebox.showerror("Cannot import patch", "Choose the game folder (the one with eboot.bin) first.")
            return

        initial_dir = self.cfg.get("ps3_patch_source") or str(ROOT / "mods")
        src_dir = filedialog.askdirectory(title="Select the PS3 (BLJM61248) English patch folder", initialdir=initial_dir)
        if not src_dir:
            return

        self.cfg["ps3_patch_source"] = src_dir
        save_config(self.cfg)
        self._run_ps3_repatch(Path(src_dir), game)

    def _run_ps3_repatch(self, src_path: Path, game: Path):
        self.busy = True
        self.play.config(state="disabled")
        if hasattr(self, "import_patch_btn"):
            self.import_patch_btn.config(state="disabled")
        if hasattr(self, "rebuild_patch_btn"):
            self.rebuild_patch_btn.config(state="disabled")
        self.status.config(text="Importing PS3 English patch (GMD + TEX)…")

        def _worker():
            err = None
            res = None
            view = None
            try:
                update_dir = self.cfg.get("game_update", "").strip() if isinstance(self.cfg.get("game_update"), str) else ""
                effective_game = game
                if update_dir:
                    update_path = Path(update_dir)
                    if update_path.is_dir() and (update_path / "eboot.bin").is_file():
                        self.after(0, lambda: self.status.config(text="Applying game update…"))
                        view = game_update.create_view(game, update_path, OUT)
                        effective_game = view

                self.after(0, lambda: self.status.config(text="Importing PS3 English patch (GMD + TEX)…"))
                def _prog(cur, tot, msg):
                    self.after(0, lambda c=cur, t=tot, m=msg: self.status.config(
                        text=f"Importing PS3 patch… {c}/{t} ({m[:30]})"
                    ))
                res = basara4_repatch.repatch(
                    src_path, effective_game, PATCH_DIR, progress=_prog
                )
                if OVERLAY_DIR.exists():
                    try:
                        basara4_overlay._safe_remove_tree(OVERLAY_DIR)
                    except Exception:
                        pass
            except BaseException as e:
                err = str(e)
            finally:
                if view:
                    try:
                        game_update.remove_view(view)
                    except Exception:
                        pass

            def _done():
                self.busy = False
                self.play.config(state="normal")
                if hasattr(self, "import_patch_btn"):
                    self.import_patch_btn.config(state="normal")
                if hasattr(self, "rebuild_patch_btn"):
                    self.rebuild_patch_btn.config(state="normal")
                if err:
                    self.status.config(text=f"PS3 patch import failed: {err}")
                    messagebox.showerror("PS3 Patch Import Failed", err, parent=self)
                else:
                    self.status.config(text="PS3 English patch imported successfully.")
                    s = res["stats"] if res else {}
                    v = res["verification"] if res else {}
                    summary = (
                        f"PS3 English patch imported successfully!\n\n"
                        f"Verification: {v.get('total_matched', 0)} files matched\n"
                        f"Archives patched: {s.get('arcs_written', 0)}\n"
                        f"GMD strings replaced: {s.get('gmd_strings_replaced', 0)}\n"
                        f"Textures translated: {s.get('textures_translated', 0)}\n"
                        f"Textures kept (native): {s.get('textures_kept', 0)}\n"
                        f"Elapsed: {s.get('elapsed_seconds', 0)}s\n"
                        f"Output location: {PATCH_DIR}"
                    )
                    messagebox.showinfo("PS3 Patch Import Complete", summary, parent=self)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def on_rebuild_patch(self):
        if self.busy or self.proc:
            return
        game = Path(self.cfg["game"]) if self.cfg.get("game") else None
        if not game or not (game / "eboot.bin").is_file():
            messagebox.showerror("Cannot build patch", "Choose the game folder (the one with eboot.bin) first.")
            return

        ps3_src = self.cfg.get("ps3_patch_source")
        if ps3_src and Path(ps3_src).exists():
            self._run_ps3_repatch(Path(ps3_src), game)
            return

        if not TRANSLATION_DB.is_file():
            messagebox.showerror(
                "Cannot build patch",
                'No English patch yet: click "Import PS3 English Patch…" and select your PS3 (BLJM61248) English patch folder.',
                parent=self,
            )
            return
        self.busy = True
        self.play.config(state="disabled")
        if hasattr(self, "import_patch_btn"):
            self.import_patch_btn.config(state="disabled")
        if hasattr(self, "rebuild_patch_btn"):
            self.rebuild_patch_btn.config(state="disabled")
        self.status.config(text="Building English patch…")

        def _worker():
            err = None
            stats = None
            view = None
            try:
                update_dir = self.cfg.get("game_update", "").strip() if isinstance(self.cfg.get("game_update"), str) else ""
                effective_game = game
                if update_dir:
                    update_path = Path(update_dir)
                    if update_path.is_dir() and (update_path / "eboot.bin").is_file():
                        self.after(0, lambda: self.status.config(text="Applying game update…"))
                        view = game_update.create_view(game, update_path, OUT)
                        effective_game = view

                self.after(0, lambda: self.status.config(text="Building English patch…"))
                def _prog(cur, tot, msg):
                    self.after(0, lambda c=cur, t=tot: self.status.config(text=f"Building English patch… {c + 1}/{tot}"))
                stats = basara4_translation.build_patch(
                    effective_game, TRANSLATION_DB, PATCH_DIR, progress=_prog
                )
                if OVERLAY_DIR.exists():
                    try:
                        basara4_overlay._safe_remove_tree(OVERLAY_DIR)
                    except Exception:
                        pass
            except BaseException as e:
                err = str(e)
            finally:
                if view:
                    try:
                        game_update.remove_view(view)
                    except Exception:
                        pass

            def _done():
                self.busy = False
                self.play.config(state="normal")
                if hasattr(self, "import_patch_btn"):
                    self.import_patch_btn.config(state="normal")
                if hasattr(self, "rebuild_patch_btn"):
                    self.rebuild_patch_btn.config(state="normal")
                if err:
                    self.status.config(text=f"English patch build failed: {err}")
                    messagebox.showerror("English Patch Build Failed", err, parent=self)
                else:
                    self.status.config(text="English patch built successfully.")
                    s = stats.get("stats", {}) if stats else {}
                    summary = (
                        f"English translation patch built successfully!\n\n"
                        f"Archives patched: {s.get('arcs_written', 0)} / {s.get('arcs_found', 0)}\n"
                        f"GMD files modified: {s.get('gmds_modified', 0)}\n"
                        f"Strings replaced: {s.get('strings_replaced', 0)}\n"
                        f"Output location: {PATCH_DIR}"
                    )
                    messagebox.showinfo("English Patch Complete", summary, parent=self)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _progress(self, text):
        self.pending_text = text

    def _prepare_then_launch(self, game, exe):
        self.pending_text = "Starting…"
        error = None
        update_dir = self.cfg.get("game_update", "").strip() if isinstance(self.cfg.get("game_update"), str) else ""
        effective_game = game
        if update_dir:
            update_path = Path(update_dir)
            if not update_path.is_dir() or not (update_path / "eboot.bin").is_file():
                self.pending = (f"Invalid update folder: {update_dir}", game, exe, [], "", game)
                return
            try:
                self._progress("Applying game update…")
                if hasattr(self, "_cleanup_update_view"):
                    self._cleanup_update_view()
                elif getattr(self, "update_view", None):
                    try:
                        game_update.remove_view(self.update_view)
                    except Exception:
                        pass
                    self.update_view = None
                view = game_update.create_view(game, update_path, OUT)
                if hasattr(self, "update_view"):
                    self.update_view = view
                effective_game = view
            except Exception as e:
                self.pending = (f"Failed to create update view: {e}", game, exe, [], "", game)
                return
        else:
            if hasattr(self, "_cleanup_update_view"):
                self._cleanup_update_view()
            elif getattr(self, "update_view", None):
                try:
                    game_update.remove_view(self.update_view)
                except Exception:
                    pass
                self.update_view = None
            effective_game = game

        if not image_ready(effective_game):
            error = prepare_image(effective_game, self._progress)
        patch_args, notice = [], ""
        if not error and self.cfg.get("fps_limit", 60) != 60:
            try:
                writes, _ = basara4_patches.build(OUT / "eboot.elf", OUT / "patches.bin", True)
            except OSError:
                writes = []
            if writes:
                patch_args = ["--patches", str(OUT / "patches.bin")]
            else:
                self.cfg["fps_limit"] = 60
                notice = "Frame limiter not found in this eboot.bin: staying at 60.  "

        app0 = effective_game
        if not error and self.cfg.get("english_patch"):
            manifest_file = PATCH_DIR / "manifest.json"
            is_ps3_import = False
            if manifest_file.is_file():
                try:
                    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    is_ps3_import = (manifest_data.get("mode") == "ps3_import")
                except (OSError, ValueError):
                    pass

            if is_ps3_import:
                try:
                    app0 = basara4_overlay.build_overlay(effective_game, PATCH_DIR, OVERLAY_DIR)
                except Exception as e:
                    error = f"Failed to build English patch: {e}"
            elif TRANSLATION_DB.is_file():
                try:
                    if patch_needs_rebuild(effective_game, TRANSLATION_DB, PATCH_DIR):
                        def _prog(cur, tot, msg):
                            self._progress(f"Building English patch… {cur + 1}/{tot}")
                        basara4_translation.build_patch(
                            effective_game, TRANSLATION_DB, PATCH_DIR, progress=_prog
                        )
                    app0 = basara4_overlay.build_overlay(effective_game, PATCH_DIR, OVERLAY_DIR)
                except Exception as e:
                    error = f"Failed to build English patch: {e}"
            else:
                error = 'No English patch yet: click "Import PS3 English Patch…" and select your PS3 (BLJM61248) English patch folder.'

        addons_args = []
        dlc_dir = self.cfg.get("dlc_folder", "").strip() if isinstance(self.cfg.get("dlc_folder"), str) else ""
        if not error and dlc_dir:
            dlc_path = Path(dlc_dir)
            if not dlc_path.is_dir():
                error = f"DLC folder does not exist: {dlc_dir}"
            else:
                self._progress("Generating DLC manifest…")
                dlc_script = [sys.executable, "--run-script", "dlc.py"] if FROZEN else [sys.executable, str(SCRIPTS / "dlc.py")]
                res = subprocess.run(
                    dlc_script + [str(dlc_path), "--out", str(OUT)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=NO_WINDOW
                )
                addons_bin = OUT / "addons.bin"
                if res.returncode == 0 and addons_bin.is_file():
                    addons_args = ["--addons", str(addons_bin)]
                else:
                    error = f"Failed to generate DLC manifest: {res.stdout.decode('utf-8', errors='replace').strip()}"

        self.pending = (error, game, exe, addons_args + patch_args, notice, app0)

    def _wait_prepare(self):
        if self.pending is None:
            self.status.config(text=getattr(self, "pending_text", "Starting…"))
            self.after(200, self._wait_prepare)
            return
        error, game, exe, patch_args, notice, app0 = self.pending
        self.busy = False
        if error:
            self.play.config(state="normal")
            self._deiconify()
            self.status.config(text=error)
            messagebox.showerror("Cannot prepare the game", error)
            return
        if self.cfg["fps_limit"] == 60:
            self.fps_var.set(FPS_LIMITS[0][0])
        USER.mkdir(parents=True, exist_ok=True)
        self._make_window_icon(game)
        if self.cfg.get("audio_log") and AUDIO_LOG.exists():  # keep the previous run's log
            try:
                os.replace(AUDIO_LOG, AUDIO_LOG.with_name("audio_debug.previous.log"))
            except OSError:
                pass
        if self.cfg.get("audio_log") and self.cfg.get("audio_wav"):  # only this run's captures stay
            AUDIO_WAV.mkdir(parents=True, exist_ok=True)
            for old in AUDIO_WAV.glob("*.wav"):
                try:
                    old.unlink()
                except OSError:
                    pass
        extra_args = []
        isa_table = OUT / "isa-rewrite.bin"
        if isa_table.is_file():
            extra_args = ["--isa-rewrite", str(isa_table)]

        log = open(LOG, "wb")
        self.proc = subprocess.Popen(
            [str(exe), str(OUT / "boot-linked.bin"), "--content-profile", str(OUT / "content.bin"),
             "--app0", str(app0), "--user", str(USER), "--no-game-hooks", "--cheats"] + extra_args + patch_args,
            env=self._env(), cwd=str(USER), stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        if self.cfg.get("audio_wav"):  # a one-session option: the next start has it off again
            self.cfg["audio_wav"] = False
            save_config(self.cfg)
            self.wav_var.set(False)
        self.stop.config(state="normal")
        self.status.config(text=notice + ("Running, writing user\\audio_debug.log…" if self.cfg.get("audio_log")
                                          else "Running…  (Insert opens the in-game menu)"))
        self._refresh_image_label()
        if self.play_now:
            self._exit_after_play_launch()

    def _exit_after_play_launch(self):
        self.proc = None
        self.destroy()

    def _make_window_icon(self, game):
        if Image is None or WINDOW_ICON.exists():
            return
        try:
            Image.open(Path(game) / "sce_sys" / "icon0.png").convert("RGBA").resize((128, 128)).save(
                WINDOW_ICON, format="BMP")
        except OSError:
            pass

    def show_credits(self):
        notice = ROOT / "NOTICE.md"
        try:
            text = notice.read_text(encoding="utf-8")
        except OSError:
            text = "Credits are in NOTICE.md."
        win = tk.Toplevel(self)
        win.title(f"{BRAND} ({FORK} Fork) \u00b7 Original port by {AUTHOR} - Credits")
        win.configure(bg=BG)
        win.geometry("640x460")
        box = tk.Text(win, bg=PANEL, fg=FG, wrap="word", relief="flat", padx=14, pady=12, font=("Segoe UI", 10))
        box.insert("1.0", text)
        box.config(state="disabled")
        box.pack(fill="both", expand=True, padx=10, pady=10)

    # ---- auto update ----
    def _check_update_thread(self):
        info = check_latest_release()
        if info:
            self.update_info = info
            self.after(0, self._show_update_available)

    def _show_update_available(self):
        if not self.update_info or getattr(self, "_update_btn_shown", False):
            return
        self._update_btn_shown = True
        tag = self.update_info["tag"]
        self.version_label.config(text=f"v{VERSION}  (Update: {tag})", fg=GOLD)
        self.update_btn = tk.Button(
            self.mark_frame,
            text=f"Update ke {tag}",
            bg=GOLD,
            fg=BG,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            activebackground="#dbb458",
            activeforeground=BG,
            cursor="hand2",
            command=self.prompt_update,
        )
        self.update_btn.pack(before=self.check_btn, fill="x", pady=(0, 6), ipady=3)

    def check_update_manual(self):
        self.status.config(text="Memeriksa pembaruan...")

        def run():
            info = check_latest_release()
            if info:
                self.update_info = info
                self.after(0, lambda: (
                    self.status.config(text="Ready"),
                    self._show_update_available(),
                    self.prompt_update()
                ))
            else:
                self.after(0, lambda: (
                    self.status.config(text="Ready"),
                    messagebox.showinfo("Update", f"Anda sudah menggunakan versi terbaru (v{VERSION}).")
                ))

        threading.Thread(target=run, daemon=True).start()

    def prompt_update(self):
        if not self.update_info:
            return
        tag = self.update_info["tag"]
        size_mb = self.update_info["size"] / (1024 * 1024)
        if messagebox.askyesno(
            "Update Tersedia",
            f"Versi baru {tag} tersedia di GitHub!\nUkuran unduhan: ~{size_mb:.1f} MB.\n\n"
            "Launcher akan mengunduh dan menerapkan update secara otomatis.\n"
            "Data game (folder game\\) dan save data Anda (folder user\\) TIDAK akan terhapus.\n\n"
            "Unduh dan pasang update sekarang?"
        ):
            self._download_and_apply_update(self.update_info)

    def _download_and_apply_update(self, info):
        win = tk.Toplevel(self)
        win.title("Basara 4 PC - Updater")
        win.configure(bg=BG)
        win.geometry("440x180")
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text=f"Mengunduh Pembaruan {info['tag']}...", bg=BG, fg=GOLD, font=("Segoe UI", 11, "bold")).pack(pady=(18, 8))
        status_lbl = tk.Label(win, text="Menghubungkan ke GitHub...", bg=BG, fg=FG, font=("Segoe UI", 9))
        status_lbl.pack(pady=(0, 8))

        prog = ttk.Progressbar(win, orient="horizontal", length=360, mode="determinate")
        prog.pack(pady=6)

        def worker():
            updates_dir = USER / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            zip_path = updates_dir / "update.zip"

            try:
                req = urllib.request.Request(info["url"], headers={"User-Agent": "Basara4-PC-Launcher"})
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    total = int(resp.headers.get("Content-Length", info["size"] or 1))
                    downloaded = 0
                    chunk_size = 64 * 1024
                    with open(zip_path, "wb") as f:
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            percent = int((downloaded / total) * 100) if total else 0
                            cur_mb = downloaded / (1024 * 1024)
                            tot_mb = total / (1024 * 1024)
                            win.after(0, lambda p=percent, c=cur_mb, t=tot_mb: (
                                prog.configure(value=p),
                                status_lbl.configure(text=f"Mengunduh: {c:.1f} MB / {t:.1f} MB ({p}%)")
                            ))

                win.after(0, lambda: self._apply_update_and_restart(win))
            except Exception as e:
                win.after(0, lambda err=e: (
                    win.destroy(),
                    messagebox.showerror("Gagal Mengunduh", f"Pembaruan gagal diunduh:\n{err}")
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update_and_restart(self, win):
        win.destroy()
        updater_cmd = USER / "updater.cmd"
        cmd_content = (
            "@echo off\r\n"
            "setlocal enabledelayedexpansion\r\n"
            "title Basara 4 PC - Updater\r\n"
            "cd /d \"%~dp0\"\r\n"
            "echo Menunggu launcher menutup...\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            "set \"ZIP=%~dp0updates\\update.zip\"\r\n"
            "set \"EXTRACT=%~dp0updates\\extract\"\r\n"
            "set \"DEST=%~dp0..\"\r\n"
            "if not exist \"!ZIP!\" exit /b 1\r\n"
            "echo Mengekstrak pembaruan...\r\n"
            "if exist \"!EXTRACT!\" rmdir /s /q \"!EXTRACT!\"\r\n"
            "mkdir \"!EXTRACT!\"\r\n"
            "tar -xf \"!ZIP!\" -C \"!EXTRACT!\" 2>nul\r\n"
            "if errorlevel 1 (\r\n"
            "    powershell.exe -NoProfile -Command \"Expand-Archive -Path '!ZIP!' -DestinationPath '!EXTRACT!' -Force\"\r\n"
            ")\r\n"
            "set \"SRC=!EXTRACT!\"\r\n"
            "if not exist \"!EXTRACT!\\Sengoku Basara 4.exe\" (\r\n"
            "    for /d %%D in (\"!EXTRACT!\\*\") do (\r\n"
            "        if exist \"%%~fD\\Sengoku Basara 4.exe\" set \"SRC=%%~fD\"\r\n"
            "    )\r\n"
            ")\r\n"
            "echo Menerapkan file baru...\r\n"
            "if exist \"!SRC!\\_internal\" xcopy /e /y /i \"!SRC!\\_internal\" \"!DEST!\\_internal\" >nul\r\n"
            "if exist \"!SRC!\\bin\" xcopy /e /y /i \"!SRC!\\bin\" \"!DEST!\\bin\" >nul\r\n"
            "if exist \"!SRC!\\scripts\" xcopy /e /y /i \"!SRC!\\scripts\" \"!DEST!\\scripts\" >nul\r\n"
            "if exist \"!SRC!\\launcher\" xcopy /e /y /i \"!SRC!\\launcher\" \"!DEST!\\launcher\" >nul\r\n"
            "if exist \"!SRC!\\Sengoku Basara 4.exe\" copy /y \"!SRC!\\Sengoku Basara 4.exe\" \"!DEST!\\\" >nul\r\n"
            "if exist \"!SRC!\\Play Sengoku Basara 4.cmd\" copy /y \"!SRC!\\Play Sengoku Basara 4.cmd\" \"!DEST!\\\" >nul\r\n"
            "if exist \"!SRC!\\NOTICE.md\" copy /y \"!SRC!\\NOTICE.md\" \"!DEST!\\\" >nul\r\n"
            "if exist \"!SRC!\\LICENSE\" copy /y \"!SRC!\\LICENSE\" \"!DEST!\\\" >nul\r\n"
            "rmdir /s /q \"!EXTRACT!\" 2>nul\r\n"
            "del /f /q \"!ZIP!\" 2>nul\r\n"
            "echo Memulai versi baru...\r\n"
            "start \"\" \"!DEST!\\Sengoku Basara 4.exe\"\r\n"
            "del /f /q \"%~f0\" 2>nul\r\n"
            "exit /b 0\r\n"
        )
        updater_cmd.write_text(cmd_content, encoding="ascii")
        subprocess.Popen(["cmd.exe", "/c", str(updater_cmd)], creationflags=NO_WINDOW)
        self.destroy()

    def _deiconify(self):
        self.deiconify()

    def on_stop(self):
        if self.proc and self.proc.poll() is None:
            self.user_stopped = True
            self.proc.kill()

    def _show_crash_dialog(self, code):
        data = extract_crash_summary(LOG)
        win = tk.Toplevel(self)
        win.title("Basara 4 PC — Crash Diagnostic")
        win.configure(bg=BG)
        win.geometry("740x560")
        win.minsize(680, 480)
        win.grab_set()

        # Bottom Button Bar dipack DULUAN dengan side="bottom" agar TIDAK TERDORONG KELUAR LAYAR
        btn_bar = ttk.Frame(win, style="TFrame", padding=12)
        btn_bar.pack(side="bottom", fill="x")

        # Top Header Frame
        header_frame = ttk.Frame(win, style="Panel.TFrame", padding=14)
        header_frame.pack(side="top", fill="x", padx=12, pady=(12, 6))

        exit_hex = f"{code & 0xFFFFFFFF:#x}" if isinstance(code, int) else "Unknown"
        tk.Label(
            header_frame,
            text=f"⚠️ Game Berhenti Mendadak (Exit Code {exit_hex})",
            bg=PANEL,
            fg=RED,
            font=("Segoe UI", 12, "bold")
        ).pack(anchor="w")

        tk.Label(
            header_frame,
            text="Terdeteksi indikasi error dari log permainan di bawah ini:",
            bg=PANEL,
            fg=FG,
            font=("Segoe UI", 9)
        ).pack(anchor="w", pady=(4, 6))

        for err in data["detected_errors"]:
            tk.Label(
                header_frame,
                text=f"• {err}",
                bg=PANEL,
                fg=GOLD,
                font=("Consolas", 9, "bold"),
                wraplength=680,
                justify="left"
            ).pack(anchor="w", padx=6)

        # Center Body Frame
        body_frame = ttk.Frame(win, style="TFrame")
        body_frame.pack(side="top", fill="both", expand=True, padx=12, pady=6)

        ttk.Label(body_frame, text="Cuplikan 10 Baris Log Terakhir (Raw Context):", style="Panel.TLabel").pack(anchor="w", pady=(0, 4))
        txt = tk.Text(body_frame, bg="#0d0b0a", fg="#d6cfc7", relief="flat", wrap="none", font=("Consolas", 9), padx=8, pady=8)
        txt.insert("1.0", data["tail_context"])
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)

        def copy_details():
            try:
                import platform
                import ctypes
                from ctypes import wintypes
                k = ctypes.windll.kernel32
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64),
                    ]
                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                k.GlobalMemoryStatusEx(ctypes.byref(mem))
                ram_total = f"{mem.ullTotalPhys // (1024*1024)} MB"
                ram_avail = f"{mem.ullAvailPhys // (1024*1024)} MB"
                commit_avail = f"{mem.ullAvailPageFile // (1024*1024)} MB"
            except Exception:
                ram_total = ram_avail = commit_avail = "N/A"

            gpu_list = "\n".join(f"  - {name}" for _, name, _ in SYSTEM_GPUS) if SYSTEM_GPUS else "  - None detected"

            audio_tail = ""
            if AUDIO_LOG.is_file():
                try:
                    a_lines = AUDIO_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                    if a_lines:
                        audio_tail = "\n".join(a_lines[-6:])
                except Exception:
                    pass

            clip_text = (
                f"=== Sengoku Basara 4 PC — Crash Diagnostic Report ===\n"
                f"Version       : v{VERSION}\n"
                f"Exit Code     : {exit_hex}\n"
                f"OS            : {platform.system()} {platform.release()} (Build {platform.version()})\n"
                f"Architecture  : {platform.machine()} ({platform.processor()})\n"
                f"RAM           : Total {ram_total} | Free {ram_avail} | Available Commit {commit_avail}\n"
                f"Vulkan GPUs   :\n{gpu_list}\n"
                f"Detected Fatal Signatures:\n" + "\n".join(f"  * {e}" for e in data["detected_errors"]) + "\n\n"
                f"--- [Last 10 Log Lines (last_run.log)] ---\n"
                f"{data['tail_context']}\n"
            )
            if audio_tail:
                clip_text += f"\n--- [Recent Audio Log (audio_debug.log)] ---\n{audio_tail}\n"

            clip_text += "\n=== End of Report ==="
            self.clipboard_clear()
            self.clipboard_append(clip_text)
            messagebox.showinfo(
                "Tersalin ke Clipboard",
                "Laporan crash lengkap (spek hardware, GPU, log error, dan audio log) berhasil disalin!\n"
                "Anda tinggal Paste (Ctrl+V) langsung ke laporan GitHub untuk bantuan teknis.",
                parent=win
            )

        def open_issues():
            import webbrowser
            webbrowser.open(f"https://github.com/{GITHUB_REPO}/issues")

        ttk.Button(btn_bar, text="📋 Copy Error ke Clipboard", command=copy_details).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="📁 Buka File Log", command=self.open_log).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="Report on GitHub", command=open_issues).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="Tutup", command=win.destroy).pack(side="right", padx=4)

    # ---- keyboard & mouse mapping dialog ----
    def open_keymap_ui(self):
        win = tk.Toplevel(self)
        win.title("Keyboard & Mouse Controls — Custom Keymap")
        win.configure(bg=BG)
        win.geometry("740x620")
        win.minsize(680, 520)
        win.grab_set()

        cur_map = dict(DEFAULT_PC_KEYMAP)
        cur_map.update(self.cfg.get("keymap", {}))
        mouse_cam_var = tk.BooleanVar(value=self.mouse_camera_var.get() if hasattr(self, "mouse_camera_var") else self.cfg.get("mouse_camera", True))
        mouse_sens_var = tk.IntVar(value=self.mouse_sens_var.get() if hasattr(self, "mouse_sens_var") else self.cfg.get("mouse_sens", 100))
        mouse_inv_x_var = tk.BooleanVar(value=self.mouse_invert_x_var.get() if hasattr(self, "mouse_invert_x_var") else self.cfg.get("mouse_invert_x", False))
        mouse_inv_y_var = tk.BooleanVar(value=self.mouse_invert_y_var.get() if hasattr(self, "mouse_invert_y_var") else self.cfg.get("mouse_invert_y", False))

        # Top Header
        top_frame = ttk.Frame(win, style="Panel.TFrame", padding=12)
        top_frame.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(top_frame, text="⚙️ Pengaturan Kontrol Keyboard & Mouse", bg=PANEL, fg=GOLD, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(top_frame, text="Klik tombol untuk mengubah tombol pemetaan. Tekan tombol Keyboard atau Mouse untuk mengikat.", bg=PANEL, fg=FG, font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        # Mouse Settings Bar
        mouse_bar = ttk.Frame(top_frame, style="Panel.TFrame")
        mouse_bar.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(mouse_bar, text="Aktifkan Kamera Mouse (Mouse Look)", variable=mouse_cam_var).pack(side="left")
        ttk.Checkbutton(mouse_bar, text="Invert X", variable=mouse_inv_x_var).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(mouse_bar, text="Invert Y", variable=mouse_inv_y_var).pack(side="left", padx=(6, 0))

        sens_frame = ttk.Frame(mouse_bar, style="Panel.TFrame")
        sens_frame.pack(side="right")
        ttk.Label(sens_frame, text="Sensitivitas: ", style="Panel.TLabel").pack(side="left")
        sens_lbl = ttk.Label(sens_frame, text=f"{mouse_sens_var.get()}%", style="Panel.TLabel", width=5)
        sens_lbl.pack(side="right")
        def on_sens_scroll(val):
            sens_lbl.config(text=f"{int(float(val))}%")
        scale = ttk.Scale(sens_frame, from_=20, to=300, variable=mouse_sens_var, command=on_sens_scroll, length=120)
        scale.pack(side="left", padx=6)

        # Body - Scrollable Mapping Table
        body_container = ttk.Frame(win, style="TFrame")
        body_container.pack(fill="both", expand=True, padx=14, pady=6)

        canvas = tk.Canvas(body_container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body_container, orient="vertical", command=canvas.yview)
        scroll_content = ttk.Frame(canvas, style="TFrame")

        scroll_content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=scroll_content, anchor="nw")
        def on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)
        canvas.bind("<Configure>", on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        btn_widgets = {}
        active_listening = [None]  # holds current button listening

        def format_binding_display(key_str):
            if not key_str:
                return "None"
            return key_str.upper() if len(key_str) <= 3 else key_str

        def finish_listen(action_name, new_key):
            if active_listening[0] == action_name:
                active_listening[0] = None
                cur_map[action_name] = new_key
                btn = btn_widgets.get(action_name)
                if btn:
                    btn.config(text=format_binding_display(new_key), bg="#2c2824", fg=GOLD)
                win.unbind("<Key>")
                win.unbind("<Button-1>")
                win.unbind("<Button-2>")
                win.unbind("<Button-3>")

        def start_listen(action_name):
            # If another was listening, reset it
            if active_listening[0]:
                old_action = active_listening[0]
                old_btn = btn_widgets.get(old_action)
                if old_btn:
                    old_btn.config(text=format_binding_display(cur_map.get(old_action, "")), bg="#2c2824", fg=GOLD)
            active_listening[0] = action_name
            btn = btn_widgets.get(action_name)
            if btn:
                btn.config(text="[ TEKAN TOMBOL... ]", bg=RED, fg="#fff")

            def on_key_event(event):
                # Translate tkinter keysym to SDL scancode name
                keysym = event.keysym
                # Special mappings
                key_map_table = {
                    "Return": "Return", "Escape": "Escape", "space": "Space",
                    "BackSpace": "Backspace", "Tab": "Tab", "Shift_L": "Left Shift",
                    "Shift_R": "Right Shift", "Control_L": "Left Ctrl", "Control_R": "Right Ctrl",
                    "Alt_L": "Left Alt", "Alt_R": "Right Alt", "Caps_Lock": "CapsLock",
                    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
                    "comma": "Comma", "equal": "Equals", "period": "Period",
                    "slash": "Slash", "semicolon": "Semicolon", "minus": "Minus"
                }
                mapped_name = key_map_table.get(keysym, keysym.capitalize() if len(keysym) > 1 else keysym.upper())
                finish_listen(action_name, mapped_name)
                return "break"

            def on_mouse_event(btn_num):
                name_map = {1: "Mouse Left", 2: "Mouse Middle", 3: "Mouse Right"}
                finish_listen(action_name, name_map.get(btn_num, f"Mouse {btn_num}"))
                return "break"

            win.bind("<Key>", on_key_event)
            # Give focus to window to capture keys
            win.focus_set()
            # Allow clicking mouse buttons to bind
            win.bind("<Button-1>", lambda e: on_mouse_event(1))
            win.bind("<Button-2>", lambda e: on_mouse_event(2))
            win.bind("<Button-3>", lambda e: on_mouse_event(3))

        # Render Table Items (Styled like game interaction list)
        for row_idx, (action_key, action_label, ps_button_label) in enumerate(ACTION_DESCRIPTIONS):
            item_frame = tk.Frame(scroll_content, bg="#181512", padx=12, pady=7, highlightbackground="#25211d", highlightthickness=1)
            item_frame.pack(fill="x", pady=2, padx=4)

            # Left Label: Action name
            lbl_text = f"{action_label}"
            tk.Label(item_frame, text=lbl_text, bg="#181512", fg=FG, font=("Segoe UI Semibold", 9), anchor="w").pack(side="left", padx=(4, 0))

            # Sub-label for PS button
            tk.Label(item_frame, text=f"({ps_button_label})", bg="#181512", fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(side="left", padx=8)

            # Right: Key Box Button
            val = cur_map.get(action_key, "")
            key_btn = tk.Button(
                item_frame,
                text=format_binding_display(val),
                bg="#2c2824",
                fg=GOLD,
                activebackground=RED,
                activeforeground="#fff",
                font=("Consolas", 10, "bold"),
                width=16,
                relief="flat",
                bd=0,
                padx=8,
                pady=4,
                cursor="hand2",
                command=lambda a=action_key: start_listen(a)
            )
            key_btn.pack(side="right")
            btn_widgets[action_key] = key_btn

        # Movement Fixed Notice
        notice_frame = tk.Frame(scroll_content, bg=PANEL, padx=12, pady=6)
        notice_frame.pack(fill="x", pady=(8, 4), padx=4)
        tk.Label(
            notice_frame,
            text="ℹ️ Pergerakan Karakter otomatis terikat pada W, A, S, D (Standar PC). Kamera mouse mengontrol analog kanan.",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 8, "italic")
        ).pack(anchor="w")

        # Bottom Action Bar
        bottom_bar = ttk.Frame(win, style="TFrame", padding=12)
        bottom_bar.pack(fill="x")

        def reset_defaults():
            cur_map.clear()
            cur_map.update(DEFAULT_PC_KEYMAP)
            mouse_cam_var.set(True)
            mouse_sens_var.set(100)
            mouse_inv_x_var.set(False)
            mouse_inv_y_var.set(False)
            sens_lbl.config(text="100%")
            for k, b in btn_widgets.items():
                b.config(text=format_binding_display(cur_map.get(k, "")), bg="#2c2824", fg=GOLD)
            messagebox.showinfo("Reset Preset", "Pengaturan kontrol berhasil dikembalikan ke Preset Default PC!", parent=win)

        def save_and_close():
            self.cfg["keymap"] = cur_map
            self.cfg["mouse_camera"] = mouse_cam_var.get()
            self.cfg["mouse_sens"] = int(mouse_sens_var.get())
            self.cfg["mouse_invert_x"] = mouse_inv_x_var.get()
            self.cfg["mouse_invert_y"] = mouse_inv_y_var.get()
            if hasattr(self, "mouse_camera_var"):
                self.mouse_camera_var.set(mouse_cam_var.get())
            if hasattr(self, "mouse_sens_var"):
                self.mouse_sens_var.set(int(mouse_sens_var.get()))
            if hasattr(self, "mouse_invert_x_var"):
                self.mouse_invert_x_var.set(mouse_inv_x_var.get())
            if hasattr(self, "mouse_invert_y_var"):
                self.mouse_invert_y_var.set(mouse_inv_y_var.get())
            save_config(self.cfg)
            win.destroy()
            messagebox.showinfo("Tersimpan", "Pengaturan kontrol Keyboard & Mouse berhasil disimpan!", parent=self)

        ttk.Button(bottom_bar, text="🔄 Reset ke Default PC", command=reset_defaults).pack(side="left", padx=4)
        ttk.Button(bottom_bar, text="Batal", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(bottom_bar, text="💾 Simpan Pengaturan", style="Play.TButton", command=save_and_close).pack(side="right", padx=4)

    def _check_exit_error(self, code):
        if code in (0, None) or not LOG.is_file():
            return None
        self._show_crash_dialog(code)
        return f"Game crash (exit code {code & 0xFFFFFFFF:#x}). Silakan cek jendela diagnostik."

    def _poll(self):
        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            self.play.config(state="normal")
            self.stop.config(state="disabled")
            if code not in (0, None) and not self.user_stopped:
                self._deiconify()
                self._show_crash_dialog(code)
            if self.play_now:
                if code in (0, None) or self.user_stopped:
                    self.destroy()
                return
            err_msg = f"Game exited with code {code & 0xFFFFFFFF:#x}. See the log." if (code not in (0, None) and not self.user_stopped) else None
            self.status.config(text=err_msg if err_msg else "Game closed.")
            self._load_ini_into_ui()
        self.after(500, self._poll)

    # ---- controller and files ----
    def map_pad(self, extra):
        exe = BIN / "basara4-pad-mapper.exe"
        if not exe.is_file():
            messagebox.showerror("Mapper missing", str(exe))
            return
        env = os.environ.copy()
        env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
        USER.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([str(exe), str(MAPPING)] + extra, env=env, creationflags=0x00000010)  # new console
        self.status.config(text="Follow the prompts in the mapper window, then press PLAY.")
        self.after(4000, self._pad_text)

    def reset_map(self):
        if MAPPING.exists() and messagebox.askyesno("Reset mapping", "Delete the saved controller mapping?"):
            os.replace(MAPPING, str(MAPPING) + ".bak")
            self._pad_text()

    def open_log(self):
        if LOG.exists():
            os.startfile(LOG)
        else:
            messagebox.showinfo("Log", "No log yet. Start the game first.")

    def open_audio_log(self):
        if AUDIO_LOG.exists():
            os.startfile(AUDIO_LOG)
        else:
            messagebox.showinfo("Audio log", "No audio log yet. Tick \"Audio debug log\", then start the game.")

    def open_saves(self):
        path = USER / "savedata"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(
                "Game running",
                "Close the launcher and leave the game running? The launcher will no longer monitor it."
            ):
                return
            self.proc = None
        self._cleanup_update_view()
        try:
            self._collect()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    Launcher(play_now="--play" in sys.argv).mainloop()
