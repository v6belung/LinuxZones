#!/usr/bin/env python3
"""LinuxZones — FancyZones-like window snapping for Linux (X11).

Usage:
  python3 linuxzones.py          # start in background (default)
  python3 linuxzones.py run      # same as above
  python3 linuxzones.py run --layout thirds
  python3 linuxzones.py editor   # open zone layout editor standalone
  python3 linuxzones.py list     # list available layouts
  python3 linuxzones.py --version
"""

__version__ = "0.1.7"

import argparse
import os
import queue
import signal
import sys
import threading
import tkinter as tk


# ------------------------------------------------------------------ helpers

def _check_x11():
    """Verify an X display is reachable (native X11 or XWayland)."""
    if os.environ.get("DISPLAY"):
        return   # X11 or XWayland — good to go
    if os.environ.get("WAYLAND_DISPLAY"):
        print(
            "[linuxzones] ERROR: No X display found (DISPLAY is not set).\n"
            "  You appear to be running Wayland with XWayland disabled.\n"
            "  LinuxZones requires XWayland, which is enabled by default on\n"
            "  GNOME and KDE — check your compositor settings to re-enable it."
        )
    else:
        print("[linuxzones] ERROR: DISPLAY is not set. Run inside a graphical session.")
    sys.exit(1)


def _get_screen_size():
    import Xlib.display
    dpy = Xlib.display.Display()
    screen = dpy.screen()
    w, h = screen.width_in_pixels, screen.height_in_pixels
    dpy.close()
    return w, h


def _get_work_area():
    """Return (x, y, w, h) of the usable work area from _NET_WORKAREA.

    Falls back to the full screen dimensions if the WM doesn't publish the
    property (headless / minimal WM environments).
    """
    import Xlib.display
    import Xlib.X as X
    dpy = Xlib.display.Display()
    screen = dpy.screen()
    sw, sh = screen.width_in_pixels, screen.height_in_pixels
    try:
        a = dpy.intern_atom("_NET_WORKAREA")
        prop = screen.root.get_full_property(a, X.AnyPropertyType)
        if prop and len(prop.value) >= 4:
            x, y, w, h = (int(v) for v in prop.value[:4])
            if w > 0 and h > 0:
                dpy.close()
                return x, y, w, h
    except Exception:
        pass
    dpy.close()
    return 0, 0, sw, sh


def _set_proc_name(name: str) -> None:
    """Set the process name shown in system monitors (via prctl PR_SET_NAME)."""
    try:
        import ctypes
        import ctypes.util
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        _libc.prctl(15, name.encode()[:15], 0, 0, 0)   # PR_SET_NAME = 15
    except Exception:
        pass


# ------------------------------------------------------------------ app

class LinuxZonesApp:
    """Main application: hidden Tk root + overlay Toplevel + daemon thread."""

    def __init__(self, layout_override: str | None = None):
        _check_x11()

        from zones import load_config, save_config
        self._save_config = save_config

        self.layouts, self.active, self.opacity, self.mod_snap, self.mod_key = load_config()

        if layout_override:
            if layout_override not in self.layouts:
                print(f"[linuxzones] Unknown layout '{layout_override}'.")
                print(f"  Available: {', '.join(self.layouts)}")
                sys.exit(1)
            self.active = layout_override

        self.screen_w, self.screen_h = _get_screen_size()
        self.work_x, self.work_y, self.work_w, self.work_h = _get_work_area()
        self.ui_queue: queue.Queue = queue.Queue()

        # Hidden root Tk — owns the event loop; never shown to the user.
        # className="linuxzones" sets WM_CLASS so the compositor / system monitor
        # can match this window back to the linuxzones.desktop entry for icons.
        self.root = tk.Tk(className="linuxzones")
        self.root.withdraw()
        self.root.title("LinuxZones")

        # Overlay as a Toplevel child of root
        from overlay import ZoneOverlay
        layout = self.layouts[self.active]
        self.overlay = ZoneOverlay(
            self.root, layout.zones,
            self.screen_w, self.screen_h,
            self.opacity,
            work_x=self.work_x,
            work_y=self.work_y,
            work_w=self.work_w,
            work_h=self.work_h,
        )

        # X11 event daemon (background thread)
        from daemon import ZoneDaemon
        self.daemon = ZoneDaemon(layout, self.ui_queue,
                                 mod_snap=self.mod_snap, mod_key=self.mod_key)
        threading.Thread(
            target=self.daemon.run, daemon=True, name="linuxzones-record"
        ).start()

        # SIGUSR1 → open editor.
        # A second invocation of 'linuxzones' sends this signal so double-clicking
        # the desktop icon while already running opens the editor instead of no-op.
        signal.signal(signal.SIGUSR1, lambda *_: self.ui_queue.put(("open_editor",)))

        mod_line = f"  {self.mod_key.capitalize()} key snap: enabled" if self.mod_snap else ""
        print(f"LinuxZones v{__version__}")
        print(f"  Layout : {self.active}  ({len(layout.zones)} zones)")
        print(f"  Opacity: {int(self.opacity * 100)}%")
        if mod_line:
            print(mod_line)
        print( "  Drag a window → hold right-click → release to snap to a zone.")
        if self.mod_snap:
            print(f"  Or hold {self.mod_key.capitalize()} while dragging as an alternative snap trigger.")
        print( "  Double-click the desktop icon again to open the layout editor.")
        print( "  Stop:  pkill linuxzones")
        print()

    # ------------------------------------------------------------------ pump

    def _pump(self):
        """Drain the daemon→UI queue; reschedule every 16 ms via Tk's after()."""
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg[0]
                if kind == "show":
                    self.overlay.show()
                elif kind == "hide":
                    self.overlay.hide()
                elif kind == "highlight":
                    self.overlay.highlight(msg[1])
                elif kind == "update_layout":
                    self.overlay.update_zones(msg[1].zones)
                elif kind == "open_editor":
                    self._open_editor()
                elif kind == "quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        # Poll at 16 ms while a drag is in progress (user may press B3/Shift
        # at any moment and we need the overlay to appear promptly).
        # Back off to 100 ms at idle to reduce CPU on slower hardware / VMs.
        interval = 16 if self.daemon.is_dragging else 100
        self.root.after(interval, self._pump)

    # ------------------------------------------------------------------ editor

    def _open_editor(self):
        from zones import save_config
        from editor import ZoneEditor

        # Hide overlay while the editor is open so it doesn't cover the canvas
        self.overlay.hide()

        editor = ZoneEditor(
            self.layouts, self.active,
            self.screen_w, self.screen_h,
            opacity=self.opacity,
            modifier_snap=self.mod_snap,
            modifier_key=self.mod_key,
            master=self.root,           # Toplevel mode; shares our mainloop
        )
        result = editor.run()           # blocks via wait_window()

        if result:
            new_layouts, new_active, new_opacity, new_mod_snap, new_mod_key = result
            self.layouts  = new_layouts
            self.active   = new_active
            self.opacity  = new_opacity
            self.mod_snap = new_mod_snap
            self.mod_key  = new_mod_key
            save_config(new_layouts, new_active, new_opacity, new_mod_snap, new_mod_key)

            new_layout = new_layouts[new_active]
            self.overlay.update_zones(new_layout.zones)
            self.overlay.set_opacity(new_opacity)
            self.daemon.update_layout(new_layout)
            self.daemon.update_mod_snap(new_mod_snap, new_mod_key)

            print(
                f"[linuxzones] Saved: layout='{new_active}', "
                f"opacity={new_opacity:.0%}, "
                f"modifier-snap={'on (' + new_mod_key + ')' if new_mod_snap else 'off'}"
            )

    # ------------------------------------------------------------------ quit

    def _quit(self):
        self.root.destroy()

    # ------------------------------------------------------------------ run

    def run(self):
        self.root.after(16, self._pump)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._quit()


# ------------------------------------------------------------------ standalone commands

def _pid_is_linuxzones(pid: int) -> bool:
    """True if /proc/<pid> looks like a running linuxzones instance.

    Guards the SIGUSR1 delivery in cmd_run(): after a crash the OS may have
    recycled the PID stored in linuxzones.pid for an unrelated process, and
    SIGUSR1's default action would terminate it.  We check the comm name
    (set via prctl) first and fall back to the full cmdline.
    """
    if pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/comm") as f:
            if f.read().strip() == "linuxzones":
                return True
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        return "linuxzones" in cmdline
    except OSError:
        return False


def cmd_run(layout_name: str | None):
    import fcntl
    import datetime

    _set_proc_name("linuxzones")

    # ---- persistent log/lock directory ---------------------------------------
    _log_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "linuxzones")
    os.makedirs(_log_dir, exist_ok=True)

    # ---- single-instance lock ------------------------------------------------
    # We use TWO files so that opening the lock file (which truncates it) never
    # destroys the PID that the running instance wrote.
    #   linuxzones.lock — held open for the process lifetime; content unused
    #   linuxzones.pid  — stores the running PID for SIGUSR1 delivery
    _lock_path = os.path.join(_log_dir, "linuxzones.lock")
    _pid_path  = os.path.join(_log_dir, "linuxzones.pid")
    _lock_fh = open(_lock_path, "w")          # kept open for process lifetime
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We own the lock — write our PID for a future second invocation
        with open(_pid_path, "w") as _pf:
            _pf.write(str(os.getpid()))
    except OSError:
        # Another instance is running — send SIGUSR1 to open its editor.
        # Verify the PID actually belongs to a linuxzones process first: after
        # a crash the OS may have recycled it for an unrelated process, and
        # SIGUSR1's default disposition would terminate that innocent victim.
        try:
            with open(_pid_path) as _f:
                _pid = int(_f.read().strip())
            if _pid_is_linuxzones(_pid):
                os.kill(_pid, signal.SIGUSR1)
        except Exception:
            pass
        sys.exit(0)

    # ---- log redirect --------------------------------------------------------
    # When launched from a .desktop file there is no terminal; redirect output
    # so errors are captured and not silently dropped.
    if not sys.stdout.isatty():
        _log_path = os.path.join(_log_dir, "linuxzones.log")
        _log_fh = open(_log_path, "a", buffering=1)   # line-buffered
        sys.stdout = _log_fh
        sys.stderr = _log_fh
        print(f"\n{'─' * 60}")
        print(f"LinuxZones started  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'─' * 60}")

    app = LinuxZonesApp(layout_override=layout_name)
    app.run()


def cmd_editor():
    _check_x11()

    from zones import load_config, save_config
    from editor import ZoneEditor

    layouts, active, opacity, mod_snap, mod_key = load_config()
    screen_w, screen_h = _get_screen_size()

    editor = ZoneEditor(layouts, active, screen_w, screen_h,
                        opacity=opacity, modifier_snap=mod_snap, modifier_key=mod_key)
    result = editor.run()
    if result:
        new_layouts, new_active, new_opacity, new_mod_snap, new_mod_key = result
        save_config(new_layouts, new_active, new_opacity, new_mod_snap, new_mod_key)
        print(
            f"[linuxzones] Saved: layout='{new_active}', "
            f"opacity={new_opacity:.0%}, "
            f"modifier-snap={'on (' + new_mod_key + ')' if new_mod_snap else 'off'}"
        )
    else:
        print("[linuxzones] Editor closed without saving.")


def cmd_list():
    from zones import load_config
    layouts, active, opacity, mod_snap, mod_key = load_config()
    mod_state = f"on ({mod_key})" if mod_snap else "off"
    print(
        f"LinuxZones v{__version__} — layouts (* = active)  "
        f"opacity: {opacity:.0%}  modifier-snap: {mod_state}"
    )
    for name, layout in layouts.items():
        marker     = " *" if name == active else "  "
        zones_desc = f"{len(layout.zones)} zone{'s' if len(layout.zones) != 1 else ''}"
        print(f"{marker} {name:<24} {zones_desc}")


# ------------------------------------------------------------------ CLI

def main():
    parser = argparse.ArgumentParser(
        description=f"LinuxZones v{__version__} — FancyZones-like window snapping for Linux (X11)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.strip(),
    )
    parser.add_argument("--version", "-V", action="version",
                        version=f"linuxzones {__version__}")

    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Start in background (default)")
    run_p.add_argument("--layout", "-l", metavar="NAME", help="Layout to use")

    sub.add_parser("editor", help="Open zone layout editor")
    sub.add_parser("list",   help="List available layouts")

    args = parser.parse_args()

    if args.cmd in (None, "run"):
        cmd_run(getattr(args, "layout", None))
    elif args.cmd == "editor":
        cmd_editor()
    elif args.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
