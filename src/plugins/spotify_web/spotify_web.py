"""Spotify Web Player plugin — launches a Chromium kiosk showing open.spotify.com.

Displays a placeholder image on the framebuffer while a separate Chromium
kiosk process renders the Spotify Web Player to the touchscreen via Xorg.
User credentials are managed via the plugin settings page: username lives
in device config, password in .env. A persistent Chromium user-data-dir
keeps session cookies so users only log in once.
"""

import logging
import os
import subprocess
import sys
import time

from PIL import Image, ImageDraw

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import get_font

logger = logging.getLogger(__name__)

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))
# Persistent Chromium profile — keeps login cookies across restarts.
PROFILE_DIR = os.path.join(SRC_DIR, "static", "spotify_profile")
# Process state file — written by /start endpoint, read by /status endpoint.
STATE_FILE = os.path.join(PROFILE_DIR, "kiosk_state.json")
# Capture subprocess stderr so kiosk crashes are diagnosable instead of black-box.
KIOSK_LOG = os.path.join(SRC_DIR, "static", "logs", "spotify_kiosk.log")

SPOTIFY_URL = "https://open.spotify.com/"
DISPLAY = ":0"


def _is_pi():
    """Return True if running on a Raspberry Pi."""
    return os.path.exists("/proc/device-tree/model")


class SpotifyWeb(BasePlugin):
    """Spotify Web Player plugin.

    The plugin itself only renders a placeholder image. The actual web
    player is launched via the /plugin/spotify_web/start endpoint, which
    spawns Xorg + Chromium kiosk. This keeps the refresh task's image
    generation fast and decouples display from playback.
    """

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = False
        template_params['hide_refresh_interval'] = True
        return template_params

    def generate_image(self, settings, device_config):
        """Return a placeholder image telling the user the player is launching.

        This image is only shown briefly on the framebuffer before the
        /start endpoint is called (which switches the display to Xorg).
        If the player is already running, the framebuffer is not visible.
        """
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        width, height = dimensions
        image = Image.new("RGBA", dimensions, (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)

        # Spotify green accent
        green = (29, 185, 84, 255)
        white = (255, 255, 255, 255)

        title_font = get_font("Jost", int(min(height, width) * 0.06), "bold")
        sub_font = get_font("Jost", int(min(height, width) * 0.035))

        title = "Spotify Web Player"
        sub = "Click 'Start' in the settings page to launch"

        tw = draw.textlength(title, font=title_font)
        draw.text(((width - tw) // 2, height // 2 - int(height * 0.06)),
                  title, font=title_font, fill=white)

        sw = draw.textlength(sub, font=sub_font)
        draw.text(((width - sw) // 2, height // 2 + int(height * 0.02)),
                  sub, font=sub_font, fill=green)

        return image

    def cleanup(self, settings):
        """Terminate Chromium + Xorg when the plugin instance is removed."""
        self._stop_kiosk()

    # ------------------------------------------------------------------
    # Kiosk process management — called by blueprint endpoints
    # ------------------------------------------------------------------

    @staticmethod
    def _write_state(pid_x, pid_chrome):
        """Persist kiosk PIDs so the /status and /stop endpoints can find them."""
        import json
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({
                "xorg_pid": pid_x,
                "chrome_pid": pid_chrome,
                "started_at": time.time(),
            }, f)

    @staticmethod
    def _read_state():
        """Return the persisted kiosk state, or None if not running."""
        import json
        if not os.path.exists(STATE_FILE):
            return None
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def _clear_state():
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass

    @staticmethod
    def _pid_alive(pid):
        """Return True if a process with the given PID is currently running.

        Uses /proc/<pid>/status to distinguish live processes from zombies
        (<defunct>): os.kill(pid, 0) returns success for zombies too, which
        previously caused is_running() to report a dead chromium as alive.
        """
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        # Distinguish zombie (state 'Z') from actually-running processes.
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("State:") and "Z" in line.split()[1]:
                        return False
        except Exception:
            pass
        return True

    def is_running(self):
        """Return True if the kiosk (Xorg or Chromium) is currently running."""
        state = self._read_state()
        if not state:
            return False
        return self._pid_alive(state.get("xorg_pid")) or \
            self._pid_alive(state.get("chrome_pid"))

    def _default_bluetooth_sink(self):
        """Find the name of the connected Bluetooth audio sink via pactl.

        Returns the sink name string, or None if no bluetooth sink is
        connected or pactl is unavailable.
        """
        if not _is_pi():
            return None
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and "bluez" in parts[1]:
                    return parts[1]
        except Exception as e:
            logger.debug("Could not query pactl for bluetooth sink: %s", e)
        return None

    def _route_audio_to_bluetooth(self):
        """Set the default PulseAudio sink to the connected Bluetooth device."""
        sink = self._default_bluetooth_sink()
        if not sink:
            logger.info("No bluetooth audio sink detected, using default output")
            return
        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink],
                capture_output=True, timeout=5,
            )
            logger.info("Default audio sink set to bluetooth: %s", sink)
        except Exception as e:
            logger.warning("Failed to set bluetooth sink: %s", e)

    def _has_persisted_session(self):
        """Return True if the persistent profile already contains session cookies.

        When true, Chromium will resume the Spotify session automatically on
        next launch — no manual login needed. We probe the Cookies SQLite
        file for any non-expired spotify.com cookie.
        """
        cookies_db = os.path.join(PROFILE_DIR, "Default", "Network", "Cookies")
        if not os.path.exists(cookies_db):
            # Older Chromium layout
            cookies_db = os.path.join(PROFILE_DIR, "Default", "Cookies")
            if not os.path.exists(cookies_db):
                return False
        try:
            import sqlite3
            import time as _time
            now = int(_time.time())
            con = sqlite3.connect(cookies_db)
            cur = con.execute(
                "SELECT COUNT(*) FROM cookies "
                "WHERE host_key LIKE '%spotify.com%' AND "
                "(expires_utc = 0 OR expires_utc > ?)",
                (now,),
            )
            count = cur.fetchone()[0]
            con.close()
            return count > 0
        except Exception as e:
            logger.debug("Could not inspect persisted cookies: %s", e)
            return False

    def reset_login(self):
        """Clear the persistent Chromium profile so the next launch shows
        the Spotify login page again.

        Safe to call while the kiosk is stopped. Returns (bool, str).
        """
        if self.is_running():
            return False, "Stop the player before clearing login"
        if not os.path.isdir(PROFILE_DIR):
            return True, "No saved login to clear"
        try:
            import shutil
            shutil.rmtree(PROFILE_DIR)
            logger.info("Spotify profile cleared: %s", PROFILE_DIR)
            return True, "Saved login cleared"
        except Exception as e:
            logger.error("Failed to clear Spotify profile: %s", e)
            return False, f"Failed to clear profile: {e}"

    def _open_kiosk_log(self):
        """Open (append) the kiosk stderr log file. Returns a file object or DEVNULL.

        The parent dir is created on demand. The file is opened in append mode
        so each start adds a new entry; a timestamp header line is written so
        multiple runs are distinguishable.
        """
        try:
            os.makedirs(os.path.dirname(KIOSK_LOG), exist_ok=True)
            fh = open(KIOSK_LOG, "a")
            fh.write("\n" + "=" * 70 + "\n")
            fh.write(f"kiosk start at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            fh.write("=" * 70 + "\n")
            fh.flush()
            return fh
        except Exception as e:
            logger.warning("Could not open kiosk log %s: %s", KIOSK_LOG, e)
            return subprocess.DEVNULL

    def _find_chrome_binary(self):
        """Locate a Chromium/Chrome binary on the current platform.

        Returns the path to the binary, or None if not found. On Pi we
        expect chromium-browser; on macOS we look for Google Chrome.
        """
        if _is_pi():
            candidates = ["/usr/bin/chromium-browser", "/usr/bin/chromium"]
        elif sys.platform == "darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        else:  # Linux dev
            candidates = ["chromium-browser", "chromium", "google-chrome"]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def start_kiosk(self):
        """Launch Chromium/Chrome kiosk displaying the Spotify Web Player.

        Returns (success: bool, message: str). Works on both Raspberry Pi
        (Xorg + chromium-browser kiosk) and macOS dev (native Chrome
        window, no Xorg needed) so the touchscreen UX can be tested
        locally.

        Login strategy: relies on the persistent Chromium user-data-dir to
        keep Spotify session cookies. On first launch the user logs in
        manually (Spotify → "Continue with Google" → Google OAuth flow).
        Subsequent launches resume the session automatically from the
        persisted cookies.
        """
        if self.is_running():
            return True, "Kiosk already running"

        chrome_bin = self._find_chrome_binary()
        if not chrome_bin:
            return False, ("Chrome/Chromium not found. On Pi: install "
                           "chromium-browser. On Mac: install Google Chrome.")

        os.makedirs(PROFILE_DIR, exist_ok=True)

        # On Pi only: route audio to bluetooth + blank framebuffer + start Xorg
        xorg_pid = None
        x_proc = None
        kiosk_log = self._open_kiosk_log()
        if _is_pi():
            self._route_audio_to_bluetooth()
            try:
                with open("/sys/class/graphics/fb0/blank", "w") as f:
                    f.write("1")
            except Exception as e:
                logger.warning("Could not blank framebuffer before Xorg: %s", e)
            try:
                # xinit syntax: xinit <client> -- <server args>.
                # We must pass a client, otherwise xinit tries to run xterm
                # (not installed on Pi kiosk) and exits immediately, taking
                # the X server down with it. `/usr/bin/sleep infinity` is a
                # harmless placeholder that keeps the X server alive until we
                # kill it. Full path is required because systemd services run
                # with a minimal PATH that xinit does not search for clients.
                x_proc = subprocess.Popen(
                    ["xinit", "/usr/bin/sleep", "infinity", "--",
                     DISPLAY, "-nocursor", "-nolisten", "tcp"],
                    stdout=kiosk_log, stderr=kiosk_log,
                    env={**os.environ, "DISPLAY": DISPLAY},
                )
                xorg_pid = x_proc.pid
            except FileNotFoundError:
                return False, "xinit not installed — run install.sh"
            except Exception as e:
                return False, f"Failed to start Xorg: {e}"
            time.sleep(2)
            if not self._pid_alive(xorg_pid):
                return False, ("Xorg exited immediately — see "
                               f"{KIOSK_LOG} or ~/.local/share/xorg/Xorg.0.log")

        # Build kiosk command (cross-platform). On Mac we use --app=URL for
        # a borderless window (true --kiosk is too aggressive on desktop);
        # on Pi we use --kiosk for full-screen takeover.
        if _is_pi():
            mode_flag = "--kiosk"
            extra_flags = []
        else:
            # Mac/dev: use --app for a clean windowed kiosk that's closable
            mode_flag = f"--app={SPOTIFY_URL}"
            extra_flags = [
                "--window-size=1024,600",  # mimic 7" touchscreen resolution
                "--window-position=0,0",
            ]

        chrome_cmd = [
            chrome_bin,
            "--noerrdialogs",
            "--disable-translate",
            "--disable-popup-blocking",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=TranslateUI",
            "--no-first-run",
            "--no-default-browser-check",
            "--restore-last-session=false",
            "--no-sandbox",  # required when launched from non-standard parent (Python subprocess, kiosk)
            f"--user-data-dir={PROFILE_DIR}",
        ]
        if _is_pi():
            chrome_cmd.append(mode_flag)
            chrome_cmd.append("--window-position=0,0")
        else:
            chrome_cmd.append(mode_flag)  # --app=URL includes the URL
            chrome_cmd.extend(extra_flags)
        if _is_pi():
            chrome_cmd.append(SPOTIFY_URL)

        try:
            chrome_proc = subprocess.Popen(
                chrome_cmd,
                stdout=kiosk_log, stderr=kiosk_log,
                env={**os.environ, "DISPLAY": DISPLAY} if _is_pi() else os.environ,
            )
        except Exception as e:
            if xorg_pid:
                try:
                    os.kill(xorg_pid, 15)
                except Exception:
                    pass
            return False, f"Failed to start Chromium: {e}"

        # Give chromium a moment, then check it hasn't already crashed.
        # Chromium exits immediately on missing libs, sandbox issues, GPU
        # failures, etc. — catching that here turns a "black screen" into
        # a useful error message and reads the crash reason from the log.
        time.sleep(3)
        if chrome_proc.poll() is not None:
            try:
                kiosk_log.flush()
                with open(KIOSK_LOG) as f:
                    tail = f.read()[-2000:]
            except Exception:
                tail = "(could not read kiosk log)"
            if xorg_pid:
                try:
                    os.kill(xorg_pid, 15)
                except Exception:
                    pass
            return False, (f"Chromium exited immediately (code="
                           f"{chrome_proc.returncode}). Tail of {KIOSK_LOG}:\n"
                           f"{tail}")

        self._write_state(xorg_pid, chrome_proc.pid)
        logger.info("Spotify kiosk started: xorg=%s chromium=%d",
                    xorg_pid, chrome_proc.pid)
        return True, "Kiosk started"

    def _stop_kiosk(self):
        """Terminate Chromium/Chrome and (on Pi) Xorg processes.

        On Mac/dev: only kills the Chrome app process. On Pi: also kills
        Xorg and unblanks the framebuffer.
        """
        state = self._read_state()
        if not state:
            return True, "Not running"

        # Kill Chromium first so it can shut down cleanly
        for pid_key in ("chrome_pid", "xorg_pid"):
            pid = state.get(pid_key)
            if not pid or not self._pid_alive(pid):
                continue
            try:
                os.kill(pid, 15)  # SIGTERM
            except Exception as e:
                logger.warning("Failed to SIGTERM pid %d: %s", pid, e)

        # Give processes a moment, then SIGKILL stragglers
        time.sleep(1)
        for pid_key in ("chrome_pid", "xorg_pid"):
            pid = state.get(pid_key)
            if not pid or not self._pid_alive(pid):
                continue
            try:
                os.kill(pid, 9)  # SIGKILL
            except Exception:
                pass

        # On Pi: kill orphaned chromium/xinit + unblank framebuffer
        if _is_pi():
            for pattern in ["chromium-browser", "xinit"]:
                try:
                    subprocess.run(["pkill", "-f", pattern],
                                   capture_output=True, timeout=3)
                except Exception:
                    pass
            try:
                with open("/sys/class/graphics/fb0/blank", "w") as f:
                    f.write("0")
            except Exception as e:
                logger.warning("Could not unblank framebuffer: %s", e)

        self._clear_state()

        logger.info("Spotify kiosk stopped")
        return True, "Kiosk stopped"
