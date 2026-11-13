"""
CafeMaster monitoring helpers for the gaming-PC agent.

* Screen capture (Windows GDI via ctypes, no extra installs; Pillow or ImageMagick as fallbacks)
* Foreground window title + app (shows which website / game is on screen)
* Process list and closing an app
* Rule checks: blocked apps (auto-closed) and blocked words in window titles
* Website blocking through a managed section of the hosts file

Standard library only.
"""
import ctypes
import csv
import io
import os
import platform
import re
import shutil
import struct
import subprocess
import zlib

WINDOWS = platform.system() == "Windows"
NO_WINDOW = 0x08000000 if WINDOWS else 0   # CREATE_NO_WINDOW: no console flash under pythonw

# Processes that must never be closed remotely (Windows core + the agent itself).
PROTECTED = {"system", "idle", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
             "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "python.exe", "pythonw.exe",
             "cafemaster-agent.exe", "conhost.exe", "explorer.exe", "taskmgr.exe", "registry",
             "memory compression", "systemd", "init", "xorg", "python3"}

HOSTS_START = "# >>> CafeMaster blocked sites (managed automatically, do not edit) >>>"
HOSTS_END = "# <<< CafeMaster blocked sites <<<"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PROCESS_RE = re.compile(r"^[\w .\-()+]{1,100}$")


# --------------------------------------------------------------------------- #
# PNG encoding (pure python, used for the Windows capture path)
# --------------------------------------------------------------------------- #
def encode_png_bgra(bgra, w, h, level=3):
    """Encode top-down 32-bit BGRA pixels as an RGB PNG."""
    rgb = bytearray(w * h * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# Screen capture
# --------------------------------------------------------------------------- #
def _capture_windows(max_w):
    from ctypes import wintypes
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    HANDLE = ctypes.c_void_p
    user32.GetDC.restype = HANDLE
    user32.GetDC.argtypes = [HANDLE]
    user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
    gdi32.CreateCompatibleDC.restype = HANDLE
    gdi32.CreateCompatibleDC.argtypes = [HANDLE]
    gdi32.CreateCompatibleBitmap.restype = HANDLE
    gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = HANDLE
    gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
    gdi32.DeleteObject.argtypes = [HANDLE]
    gdi32.DeleteDC.argtypes = [HANDLE]
    gdi32.SetStretchBltMode.argtypes = [HANDLE, ctypes.c_int]
    gdi32.StretchBlt.argtypes = [HANDLE] + [ctypes.c_int] * 4 + [HANDLE] + [ctypes.c_int] * 4 + [wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [HANDLE, HANDLE, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    w = min(max_w, sw)
    h = max(1, int(sh * w / sw))
    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        gdi32.SetStretchBltMode(mdc, 4)  # HALFTONE = good-quality downscale
        gdi32.StretchBlt(mdc, 0, 0, w, h, hdc, 0, 0, sw, sh, 0x00CC0020)  # SRCCOPY
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, hdc)
    return encode_png_bgra(buf.raw, w, h), "image/png"


def _capture_pillow(max_w):
    from PIL import ImageGrab  # optional dependency
    img = ImageGrab.grab()
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=70)
    return out.getvalue(), "image/jpeg"


def _capture_imagemagick(max_w):
    if not shutil.which("import"):
        raise RuntimeError("no capture tool")
    data = subprocess.run(["import", "-silent", "-window", "root", "-resize", f"{max_w}x>",
                           "-quality", "70", "jpg:-"], capture_output=True, timeout=10, check=True).stdout
    return data, "image/jpeg"


def capture_screen(max_w=1280):
    """Return (image_bytes, mime) of the primary screen, scaled to at most max_w pixels wide."""
    errors = []
    for fn in ((_capture_windows,) if WINDOWS else ()) + (_capture_pillow, _capture_imagemagick):
        try:
            return fn(max_w)
        except Exception as e:  # try the next method
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError("; ".join(errors))


# --------------------------------------------------------------------------- #
# Foreground window + processes
# --------------------------------------------------------------------------- #
def foreground():
    """{'title': window title, 'app': process name} of the window the user is looking at."""
    if not WINDOWS:
        return {"title": "", "app": ""}
    try:
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "", "app": ""}
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                app = os.path.basename(path.value)
            kernel32.CloseHandle(h)
        return {"title": buf.value[:200], "app": app}
    except Exception:
        return {"title": "", "app": ""}


def parse_tasklist(text):
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 5 and row[1].isdigit():
            mem = int(re.sub(r"\D", "", row[4]) or 0)  # "123,456 K"
            out.append({"name": row[0], "pid": int(row[1]), "mem_mb": round(mem / 1024, 1)})
    return out


def parse_ps(text):
    out = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            out.append({"name": parts[2].strip(), "pid": int(parts[0]),
                        "mem_mb": round(int(parts[1]) / 1024, 1) if parts[1].isdigit() else 0})
    return out


def processes():
    """List running processes, biggest memory users first."""
    try:
        if WINDOWS:
            text = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                  timeout=10, creationflags=NO_WINDOW).stdout
            procs = parse_tasklist(text)
        else:
            text = subprocess.run(["ps", "-eo", "pid=,rss=,comm="], capture_output=True, text=True,
                                  timeout=10).stdout
            procs = parse_ps(text)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(procs, key=lambda p: -p["mem_mb"])


def is_protected(name):
    return (name or "").strip().lower() in PROTECTED


def kill_process(name, dry_run=False):
    """Close every process with this image name. Returns an error string or None."""
    name = (name or "").strip()
    if not PROCESS_RE.match(name):
        return "Invalid process name"
    if is_protected(name):
        return f"{name} is protected"
    cmd = (["taskkill", "/f", "/t", "/im", name] if WINDOWS else ["pkill", "-x", name])
    if dry_run:
        print("[dry-run] would run:", " ".join(cmd), flush=True)
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        return None if r.returncode == 0 else (r.stderr or r.stdout or "failed").strip()[:200]
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def _norm_app(name):
    name = (name or "").strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def check_policy(policy, fg, procs):
    """Return a list of violations: {'kind', 'detail', 'kill'}.

    kind = 'blocked_app'  -> a blocked program is running (it will be closed)
           'blocked_word' -> the window on screen has a blocked word in its title
    """
    policy = policy or {}
    found = []
    blocked_apps = {_norm_app(a) for a in policy.get("blocked_apps", []) if a.strip()}
    seen = set()
    for p in procs or []:
        n = _norm_app(p["name"])
        if n in blocked_apps and n not in seen and not is_protected(p["name"]):
            seen.add(n)
            found.append({"kind": "blocked_app", "detail": f"Blocked app running: {p['name']}",
                          "kill": p["name"]})
    title = (fg or {}).get("title", "")
    low = title.lower()
    for word in policy.get("blocked_keywords", []):
        w = word.strip().lower()
        if w and w in low:
            found.append({"kind": "blocked_word", "detail": f'"{word.strip()}" on screen: {title[:120]}',
                          "kill": None})
            break
    return found


# --------------------------------------------------------------------------- #
# Website blocking via hosts file
# --------------------------------------------------------------------------- #
def clean_domains(domains):
    out = []
    for d in domains or []:
        d = re.sub(r"^[a-z]+://", "", d.strip().lower()).split("/")[0].split(":")[0]
        d = d[4:] if d.startswith("www.") else d
        if DOMAIN_RE.match(d) and d not in out:
            out.append(d)
    return out


def hosts_with_blocks(text, domains):
    """Return hosts-file text with CafeMaster's managed section replaced by `domains`."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.strip() == HOSTS_START:
            inside = True
            continue
        if line.strip() == HOSTS_END:
            inside = False
            continue
        if not inside:
            lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    domains = clean_domains(domains)
    if domains:
        lines += ["", HOSTS_START]
        for d in domains:
            lines += [f"0.0.0.0 {d}", f"0.0.0.0 www.{d}"]
        lines.append(HOSTS_END)
    return "\n".join(lines) + "\n"


def hosts_path():
    if WINDOWS:
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
    return "/etc/hosts"


def apply_site_blocks(domains, dry_run=False, path=None):
    """Write blocked domains into the hosts file. Needs admin rights. Returns error or None."""
    path = path or hosts_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            current = f.read()
    except OSError as e:
        return f"Cannot read hosts file: {e}"
    new = hosts_with_blocks(current, domains)
    if new == current:
        return None
    if dry_run:
        print(f"[dry-run] would block {len(clean_domains(domains))} site(s) in {path}", flush=True)
        return None
    try:
        with open(path, "w", encoding="utf-8", newline="\r\n" if WINDOWS else "\n") as f:
            f.write(new)
    except OSError as e:
        return f"Cannot write hosts file (run the agent as administrator): {e}"
    if WINDOWS:
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, creationflags=NO_WINDOW)
    return None
