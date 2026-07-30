"""Settings blueprint — device config, OTA updates, shutdown/reboot, log download, config backup/restore."""

from flask import Blueprint, request, jsonify, current_app, render_template, Response, send_file
from datetime import datetime, timedelta
from utils.app_utils import sanitize_filename
import os
import subprocess
import time
import pytz
import logging
import io
import json
import zipfile
import shutil

# Try to import cysystemd for journal reading (Linux only)
try:
    from cysystemd.reader import JournalReader, JournalOpenMode, Rule
    JOURNAL_AVAILABLE = True
except ImportError:
    JOURNAL_AVAILABLE = False
    # Define dummy classes for when cysystemd is not available
    class JournalOpenMode:
        SYSTEM = None
    class Rule:
        pass
    class JournalReader:
        def __init__(self, *args, **kwargs):
            pass


logger = logging.getLogger(__name__)
settings_bp = Blueprint("settings", __name__)

def _get_version():
    """Read version from VERSION file."""
    version_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'VERSION')
    try:
        with open(version_file, 'r') as f:
            return f.read().strip()
    except Exception:
        return "?"

@settings_bp.route('/settings')
def settings_page():
    """Render device settings page (display, timezone, brightness schedule)."""
    device_config = current_app.config['DEVICE_CONFIG']
    timezones = sorted(pytz.all_timezones_set)

    # Get WiFi info for display
    wifi_manager = current_app.config.get('WIFI_MANAGER')
    wifi_ssid = wifi_manager.get_wifi_ssid() if wifi_manager else None
    wifi_ip = wifi_manager.get_ip_address() if wifi_manager else None

    return render_template('settings.html', device_settings=device_config.get_config(),
                           timezones=timezones, wifi_ssid=wifi_ssid, wifi_ip=wifi_ip)

@settings_bp.route('/save_settings', methods=['POST'])
def save_settings():
    """Save device settings from the settings form."""
    device_config = current_app.config['DEVICE_CONFIG']

    try:
        form_data = request.form.to_dict()

        time_format = form_data.get("timeFormat")
        if not form_data.get("timezoneName"):
            return jsonify({"error": "Time Zone is required"}), 400
        if not time_format or time_format not in ["12h", "24h"]:
            return jsonify({"error": "Time format is required"}), 400

        # Build image settings — include inky_saturation for e-ink displays
        def _clamp_float(val_str, default, lo=0.0, hi=2.0):
            try:
                return max(lo, min(hi, float(val_str)))
            except (TypeError, ValueError):
                return default

        image_settings = {
            "saturation": _clamp_float(form_data.get("saturation"), 1.0),
            "sharpness": _clamp_float(form_data.get("sharpness"), 1.0),
            "contrast": _clamp_float(form_data.get("contrast"), 1.0),
        }
        if "inkySaturation" in form_data:
            image_settings["inky_saturation"] = _clamp_float(form_data.get("inkySaturation"), 0.5)

        settings = {
            "device_name": form_data.get("deviceName", "").strip() or None,
            "orientation": form_data.get("orientation"),
            "inverted_image": form_data.get("invertImage"),
            "log_system_stats": form_data.get("logSystemStats"),
            "show_plugin_icon": form_data.get("showPluginIcon"),
            "timezone": form_data.get("timezoneName"),
            "time_format": form_data.get("timeFormat"),
            "image_settings": image_settings,
            "brightness_schedule": {
                "enabled": "brightnessScheduleEnabled" in form_data,
                "day_brightness": _clamp_float(form_data.get("dayBrightness"), 1.0),
                "evening_brightness": _clamp_float(form_data.get("eveningBrightness"), 0.6),
                "night_brightness": _clamp_float(form_data.get("nightBrightness"), 0.3),
                "day_start": form_data.get("dayStart", "07:00"),
                "evening_start": form_data.get("eveningStart", "18:00"),
                "night_start": form_data.get("nightStart", "22:00"),
            },
            "display_transitions": {
                "enabled": "displayTransitions" in form_data,
                "steps": 10,
                "duration_ms": 800,
            },
            "proxy": {
                "enabled": "proxyEnabled" in form_data,
                "host": form_data.get("proxyHost", "").strip(),
                "port": form_data.get("proxyPort", "").strip(),
            },
        }
        # Remove None device_name to keep existing value
        if settings["device_name"] is None:
            del settings["device_name"]
        device_config.update_config(settings)

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500
    return jsonify({"success": True, "message": "Saved settings."})

@settings_bp.route('/shutdown', methods=['POST'])
def shutdown():
    """Shutdown or reboot the Pi. Send {"reboot": true} for reboot."""
    data = request.get_json() or {}
    try:
        if data.get("reboot"):
            logger.info("Reboot requested")
            subprocess.run(["sudo", "reboot"], check=True, timeout=10)
        else:
            logger.info("Shutdown requested")
            subprocess.run(["sudo", "shutdown", "-h", "now"], check=True, timeout=10)
    except subprocess.SubprocessError as e:
        logger.error(f"Shutdown/reboot failed: {e}")
        return jsonify({"error": "Failed to execute shutdown command"}), 500
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Display mode switching — toggle the 7" screen between DashPi content
# (framebuffer) and the Raspberry Pi desktop (X session via xinit).
#
# DashPi writes images directly to /dev/fb0. The desktop runs on top of an
# X server. Because both compete for the same physical display, switching
# modes means:
#   - desktop: blank fb0 so DashPi's writes are invisible, then launch Xorg
#     with the Raspberry Pi Desktop (lxsession) via xinit. The X server
#     takes over the display output.
#   - dashpi:  kill the X server/lxsession, unblank fb0, then trigger a
#     manual refresh so the current plugin image is redrawn.
# The DashPi service itself keeps running in both modes (web UI + refresh
# task stay alive); only the on-screen content changes.
#
# We use xinit instead of lightdm because `systemctl start lightdm` blocks
# indefinitely when called from inside a systemd service (D-Bus/polkit
# policy), whereas xinit spawns Xorg directly and returns immediately.
# ---------------------------------------------------------------------------

# State file recording the current display mode so the UI can reflect it.
DISPLAY_MODE_FILE = "/tmp/dashpi_display_mode"
# Capture Xorg/lxsession stderr so desktop launch failures are diagnosable.
DESKTOP_LOG = "/tmp/dashpi_desktop.log"
# X display used by the desktop session. DashPi itself does not use X.
DISPLAY = ":0"
# The lxsession session to launch — rpd-x is the Raspberry Pi Desktop on X.
LXSESSION_CMD = ["/usr/bin/lxsession", "-s", "rpd-x", "-e", "LXDE"]

def _is_pi():
    """Return True if running on a Raspberry Pi."""
    return os.path.exists("/proc/device-tree/model")

def _write_display_mode(mode):
    """Persist the current display mode ('dashpi' or 'desktop') to a tmp file."""
    try:
        with open(DISPLAY_MODE_FILE, "w") as f:
            f.write(mode)
    except Exception as e:
        logger.warning("Could not write display mode file: %s", e)

def _read_display_mode():
    """Return the persisted display mode, defaulting to 'dashpi'."""
    try:
        with open(DISPLAY_MODE_FILE) as f:
            mode = f.read().strip()
            if mode in ("dashpi", "desktop"):
                return mode
    except Exception:
        pass
    return "dashpi"

def _stop_desktop_session():
    """Kill any running Xorg / lxsession / xinit processes.

    Idempotent — safe to call when nothing is running. Kills lxsession and
    xinit first (the clients) so the X server tears down cleanly, then
    force-kills any lingering Xorg.
    """
    for pattern in ["lxsession", "lxpanel", "pcmanfm", "openbox", "xinit"]:
        try:
            subprocess.run(["pkill", "-f", pattern],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    # Xorg may linger after xinit exits; give it a moment then force-kill.
    time.sleep(1)
    try:
        subprocess.run(["pkill", "-9", "-f", "Xorg"],
                       capture_output=True, timeout=3)
    except Exception:
        pass
    # Remove stale X lock files so a subsequent xinit can claim display :0.
    for lock in ("/tmp/.X0-lock", "/tmp/.X11-unix/X0"):
        try:
            os.remove(lock)
        except OSError:
            pass

def _queue_refresh_after_restore():
    """Fallback: queue a forced manual refresh to redraw the current plugin.

    Used when directly restoring current_image.png to the display fails —
    the refresh task will regenerate the image and write it to fb0. Note
    this may be skipped by the refresh task if the image hash is unchanged,
    which is why the direct display_image() path in set_display_mode() is
    preferred.
    """
    refresh_task = current_app.config.get('REFRESH_TASK')
    if refresh_task and refresh_task.running:
        from refresh_task import LoopRefresh
        device_config = current_app.config['DEVICE_CONFIG']
        loop_manager = device_config.get_loop_manager()
        loop = loop_manager.determine_active_loop(datetime.now())
        if loop and loop.plugin_order:
            plugin_ref = loop.get_next_plugin()
            refresh_action = LoopRefresh(loop, plugin_ref, force=True)
            refresh_task.queue_manual_update(refresh_action)
            logger.info("Queued manual refresh after switching to dashpi mode")

@settings_bp.route('/api/display/mode', methods=['GET'])
def get_display_mode():
    """Return the current display mode: 'dashpi' or 'desktop'."""
    return jsonify({"mode": _read_display_mode()})

@settings_bp.route('/api/display/mode', methods=['POST'])
def set_display_mode():
    """Switch the 7\" screen between DashPi content and the Pi desktop.

    Body: {"mode": "dashpi" | "desktop"}.
    - "desktop": blank the framebuffer and launch Xorg + lxsession (rpd-x
                 Raspberry Pi Desktop) via xinit.
    - "dashpi":  kill the X session, unblank the framebuffer, and trigger a
                 manual refresh so the current plugin image is redrawn.
    The DashPi service keeps running in both modes.
    """
    data = request.get_json() or {}
    mode = data.get("mode")
    if mode not in ("dashpi", "desktop"):
        return jsonify({"error": "mode must be 'dashpi' or 'desktop'"}), 400

    if not _is_pi():
        return jsonify({"error": "Display switching is only available on the Raspberry Pi"}), 400

    try:
        if mode == "desktop":
            # If a desktop session is already running, do nothing.
            if _read_display_mode() == "desktop":
                return jsonify({"success": True, "mode": "desktop",
                                "message": "Desktop already running"})
            # Blank fb0 so DashPi's periodic framebuffer writes don't show on
            # screen while the X session owns the display.
            try:
                with open("/sys/class/graphics/fb0/blank", "w") as f:
                    f.write("1")
            except Exception as e:
                logger.warning("Could not blank framebuffer: %s", e)
            # Launch Xorg + lxsession via xinit. We redirect stdout/stderr to
            # a log file for diagnosability. The Popen returns immediately;
            # the desktop runs in the background as a child process.
            log_fd = open(DESKTOP_LOG, "ab")
            try:
                env = {
                    **os.environ,
                    "HOME": "/home/lazybeartoby",
                    "USER": "lazybeartoby",
                    "DISPLAY": DISPLAY,
                    "LANG": "en_GB.UTF-8",
                }
                # xinit syntax: xinit <client_path> <client_args> -- <server_args>.
                # We pass lxsession + its args as the client; Xorg args follow --.
                subprocess.Popen(
                    ["xinit"] + LXSESSION_CMD + ["--", DISPLAY,
                     "-nocursor", "-nolisten", "tcp"],
                    stdout=log_fd, stderr=log_fd, env=env,
                )
            except Exception as e:
                log_fd.close()
                return jsonify({"error": f"Failed to start desktop: {e}"}), 500
            # Give Xorg a moment to come up, then verify it's running.
            time.sleep(3)
            try:
                check = subprocess.run(["pgrep", "-x", "Xorg"],
                                       capture_output=True, timeout=3)
                xorg_running = check.returncode == 0
            except Exception:
                xorg_running = True  # assume ok if pgrep fails
            if not xorg_running:
                _write_display_mode("dashpi")
                try:
                    with open("/sys/class/graphics/fb0/blank", "w") as f:
                        f.write("0")
                except Exception:
                    pass
                return jsonify({"error": "Xorg failed to start — see " + DESKTOP_LOG}), 500
            _write_display_mode("desktop")
            logger.info("Display switched to desktop mode (xinit + lxsession started)")
            return jsonify({"success": True, "mode": "desktop"})

        # mode == "dashpi": tear down the X session and restore framebuffer.
        _stop_desktop_session()
        # Unblank fb0 so framebuffer writes reach the screen again.
        try:
            with open("/sys/class/graphics/fb0/blank", "w") as f:
                f.write("0")
        except Exception as e:
            logger.warning("Could not unblank framebuffer: %s", e)
        _write_display_mode("dashpi")
        logger.info("Display switched to dashpi mode (X session stopped)")

        # Force-rewrite the last plugin image to fb0. We cannot rely on the
        # refresh_task's queue_manual_update() here because it skips the
        # actual display write when the image hash is unchanged — and while
        # the X session was running, fb0's content was clobbered by Xorg, so
        # the screen is currently black/blank even though dashpi thinks the
        # image is "already displayed". Reading current_image.png (which the
        # refresh task keeps updated) and pushing it directly to the display
        # guarantees the on-screen content is restored immediately.
        display_manager = current_app.config.get('DISPLAY_MANAGER')
        device_config = current_app.config['DEVICE_CONFIG']
        if display_manager and device_config:
            try:
                from PIL import Image
                current_image_path = device_config.current_image_file
                if os.path.exists(current_image_path):
                    img = Image.open(current_image_path)
                    img.load()
                    display_manager.display_image(img)
                    logger.info("Restored current image to display after switching to dashpi mode")
                else:
                    logger.warning("current_image.png not found at %s, queuing refresh", current_image_path)
                    _queue_refresh_after_restore()
            except Exception as e:
                logger.warning("Failed to restore current image directly, falling back to refresh: %s", e)
                _queue_refresh_after_restore()
        return jsonify({"success": True, "mode": "dashpi"})
    except subprocess.SubprocessError as e:
        logger.error("Display mode switch failed: %s", e)
        return jsonify({"error": f"Failed to switch display mode: {e}"}), 500
    except Exception as e:
        logger.exception("Unexpected error switching display mode")
        return jsonify({"error": str(e)}), 500

@settings_bp.route('/api/update/check', methods=['GET'])
def check_for_updates():
    """Check if there are updates available on the remote repository."""
    try:
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        # Read current local version
        version_file = os.path.join(repo_dir, 'VERSION')
        local_version = '?'
        if os.path.isfile(version_file):
            with open(version_file, 'r') as f:
                local_version = f.read().strip()

        # Detect current branch
        branch_result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else 'main'

        # Fetch latest from remote (non-destructive)
        result = subprocess.run(
            ['git', 'fetch', 'origin', branch],
            cwd=repo_dir, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({
                "error": f"Git fetch failed: {result.stderr.strip()}",
                "local_version": local_version
            }), 500

        remote_ref = f'origin/{branch}'

        # Compare local HEAD with remote
        local_hash = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        ).stdout.strip()

        remote_hash = subprocess.run(
            ['git', 'rev-parse', remote_ref],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        ).stdout.strip()

        # Read remote version
        remote_version_result = subprocess.run(
            ['git', 'show', f'{remote_ref}:VERSION'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        )
        remote_version = remote_version_result.stdout.strip() if remote_version_result.returncode == 0 else '?'

        # Count commits behind
        behind_result = subprocess.run(
            ['git', 'rev-list', '--count', f'HEAD..{remote_ref}'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        )
        commits_behind = int(behind_result.stdout.strip()) if behind_result.returncode == 0 else 0

        # Get commit log of what's new
        changelog = []
        if commits_behind > 0:
            log_result = subprocess.run(
                ['git', 'log', '--oneline', f'HEAD..{remote_ref}', '--max-count=20'],
                cwd=repo_dir, capture_output=True, text=True, timeout=10
            )
            if log_result.returncode == 0:
                changelog = [line.strip() for line in log_result.stdout.strip().split('\n') if line.strip()]

        return jsonify({
            "update_available": local_hash != remote_hash,
            "local_version": local_version,
            "remote_version": remote_version,
            "commits_behind": commits_behind,
            "changelog": changelog,
            "local_hash": local_hash[:8],
            "remote_hash": remote_hash[:8],
            "branch": branch
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Git operation timed out"}), 500
    except Exception as e:
        logger.error(f"Update check failed: {e}")
        return jsonify({"error": str(e)}), 500


@settings_bp.route('/api/update/apply', methods=['POST'])
def apply_update():
    """Pull latest code from remote and restart the service."""
    try:
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        # Detect current branch
        branch_result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=repo_dir, capture_output=True, text=True, timeout=10
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else 'main'

        # Stash any local changes (e.g., __pycache__, config edits)
        subprocess.run(
            ['git', 'stash', '--include-untracked'],
            cwd=repo_dir, capture_output=True, text=True, timeout=15
        )

        # Pull latest from current branch
        result = subprocess.run(
            ['git', 'reset', '--hard', f'origin/{branch}'],
            cwd=repo_dir, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({"error": f"Git reset failed: {result.stderr.strip()}"}), 500

        # Read the new version
        version_file = os.path.join(repo_dir, 'VERSION')
        new_version = '?'
        if os.path.isfile(version_file):
            with open(version_file, 'r') as f:
                new_version = f.read().strip()

        # Schedule a service restart (delayed so this response can be sent first)
        subprocess.Popen(
            ['bash', '-c', 'sleep 2 && sudo systemctl restart dashpi 2>/dev/null || sudo systemctl restart inkypi 2>/dev/null'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        return jsonify({
            "success": True,
            "new_version": new_version,
            "message": "Update applied. Service restarting..."
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Git operation timed out"}), 500
    except Exception as e:
        logger.error(f"Update apply failed: {e}")
        return jsonify({"error": str(e)}), 500


@settings_bp.route('/download-logs')
def download_logs():
    """Download service logs as a text file. Reads from systemd journal."""
    try:
        buffer = io.StringIO()
        
        # Get 'hours' from query parameters, default to 2 if not provided or invalid
        hours_str = request.args.get('hours', '2')
        try:
            hours = min(max(int(hours_str), 1), 168)  # Clamp 1 hour to 1 week
        except ValueError:
            hours = 2
        since = datetime.now() - timedelta(hours=hours)

        if not JOURNAL_AVAILABLE:
            # Return a message when running in development mode without systemd
            buffer.write(f"Log download not available in development mode (cysystemd not installed).\n")
            buffer.write(f"Logs would normally show DashPi service logs from the last {hours} hours.\n")
            buffer.write(f"\nTo see Flask development logs, check your terminal output.\n")
        else:
            reader = JournalReader()
            reader.open(JournalOpenMode.SYSTEM)
            # Match either service name (dashpi or inkypi) for backwards compatibility
            reader.add_filter(Rule("_SYSTEMD_UNIT", "dashpi.service"))
            reader.add_filter(Rule("_SYSTEMD_UNIT", "inkypi.service"))
            reader.seek_realtime_usec(int(since.timestamp() * 1_000_000))

            for record in reader:
                try:
                    ts = datetime.fromtimestamp(record.get_realtime_usec() / 1_000_000)
                    formatted_ts = ts.strftime("%b %d %H:%M:%S")
                except Exception:
                    formatted_ts = "??? ?? ??:??:??"

                data = record.data
                hostname = data.get("_HOSTNAME", "unknown-host")
                identifier = data.get("SYSLOG_IDENTIFIER") or data.get("_COMM", "?")
                pid = data.get("_PID", "?")
                msg = data.get("MESSAGE", "").rstrip()

                # Format the log entry similar to the journalctl default output
                buffer.write(f"{formatted_ts} {hostname} {identifier}[{pid}]: {msg}\n")

        buffer.seek(0)
        # Add date and time to the filename
        now_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"dashpi_{now_str}.log"
        return Response(
            buffer.read(),
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        return Response("Error reading logs", status=500, mimetype="text/plain")


@settings_bp.route('/api/config/export')
def export_config():
    """Export device configuration as a ZIP archive.

    Query params:
        include_env: Include .env API keys (default false)
        include_images: Include saved user images (default false)
    """
    try:
        device_config = current_app.config['DEVICE_CONFIG']
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        include_env = request.args.get('include_env', 'false').lower() == 'true'
        include_images = request.args.get('include_images', 'false').lower() == 'true'

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Always include device.json (exclude transient state)
            config = device_config.get_config().copy()
            config.pop('refresh_info', None)
            config.pop('loop_override', None)
            zf.writestr('device.json', json.dumps(config, indent=2))

            # Optionally include .env
            if include_env:
                env_path = os.path.join(base_dir, '.env')
                if os.path.isfile(env_path):
                    zf.write(env_path, '.env')

            # Optionally include saved images (skip dotfiles and non-image files)
            if include_images:
                image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
                saved_dir = os.path.join(base_dir, 'src', 'static', 'images', 'saved')
                if os.path.isdir(saved_dir):
                    for fname in os.listdir(saved_dir):
                        if fname.startswith('.'):
                            continue
                        if os.path.splitext(fname)[1].lower() not in image_extensions:
                            continue
                        fpath = os.path.join(saved_dir, fname)
                        if os.path.isfile(fpath):
                            zf.write(fpath, f'saved_images/{fname}')

        buffer.seek(0)
        now_str = datetime.now().strftime("%Y-%m-%d")
        device_name = device_config.get_config().get('device_name') or 'DashPi'
        version = _get_version()
        filename = f"{device_name}-DashPi V{version}-{now_str}.zip"
        return send_file(
            buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        logger.error(f"Config export failed: {e}")
        return jsonify({"error": f"Export failed: {e}"}), 500


@settings_bp.route('/api/config/import', methods=['POST'])
def import_config():
    """Import device configuration from a previously exported ZIP archive.

    Validates the ZIP contents, backs up current config, then applies.
    Returns JSON with restart_required flag.
    """
    try:
        device_config = current_app.config['DEVICE_CONFIG']
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({"error": "No file uploaded"}), 400

        # Validate file extension
        if not file.filename.lower().endswith('.zip'):
            return jsonify({"error": "File must be a .zip archive"}), 400

        # Read ZIP into memory
        zip_data = io.BytesIO(file.read())
        if not zipfile.is_zipfile(zip_data):
            return jsonify({"error": "File is not a valid ZIP archive"}), 400

        zip_data.seek(0)
        with zipfile.ZipFile(zip_data, 'r') as zf:
            names = zf.namelist()

            # ZIP bomb guard: check total uncompressed size (max 128MB)
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > 128 * 1024 * 1024:
                return jsonify({"error": "ZIP contents too large (max 128MB uncompressed)"}), 400

            # Must contain device.json
            if 'device.json' not in names:
                return jsonify({"error": "ZIP must contain device.json"}), 400

            # Validate device.json
            try:
                config_data = json.loads(zf.read('device.json'))
            except (json.JSONDecodeError, ValueError) as e:
                return jsonify({"error": f"Invalid device.json: {e}"}), 400

            if not isinstance(config_data, dict):
                return jsonify({"error": "device.json must be a JSON object"}), 400

            # Check for expected keys (at least orientation should exist)
            if 'orientation' not in config_data:
                return jsonify({"error": "device.json missing required fields"}), 400

            # Validate .env if present
            has_env = '.env' in names
            if has_env:
                env_content = zf.read('.env').decode('utf-8', errors='ignore')
                for line in env_content.strip().splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        return jsonify({"error": f"Invalid .env line: {line[:50]}"}), 400

            # Validate images if present
            image_files = [n for n in names if n.startswith('saved_images/') and not n.endswith('/')]
            for img_name in image_files:
                img_data = zf.read(img_name)
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(img_data))
                    img.verify()
                except Exception:
                    return jsonify({"error": f"Invalid image: {os.path.basename(img_name)}"}), 400

            # --- All validation passed, apply changes ---

            # Backup current device.json
            config_path = device_config.config_file
            backup_path = config_path + '.bak'
            if os.path.isfile(config_path):
                shutil.copy2(config_path, backup_path)
                logger.info(f"Backed up config to {backup_path}")

            # Apply device.json — update in-memory config AND rebuild model objects.
            # Order matters: write_config serializes from loop_manager.to_dict(), so we
            # must rebuild loop_manager from the imported data BEFORE calling write_config.
            # Calling update_config() here would be wrong: it triggers write_config
            # internally, which first overwrites loop_config with the old loop_manager.
            device_config.config.update(config_data)
            if 'loop_config' in config_data:
                device_config.loop_manager = device_config.load_loop_manager()
            device_config.write_config()
            logger.info("Imported device.json")

            # Apply .env if present
            if has_env:
                env_path = os.path.join(base_dir, '.env')
                env_backup = env_path + '.bak'
                if os.path.isfile(env_path):
                    shutil.copy2(env_path, env_backup)
                with open(env_path, 'w') as f:
                    f.write(env_content)
                logger.info("Imported .env")

            # Apply saved images if present
            if image_files:
                saved_dir = os.path.join(base_dir, 'src', 'static', 'images', 'saved')
                os.makedirs(saved_dir, exist_ok=True)
                for img_name in image_files:
                    fname = sanitize_filename(img_name)
                    if fname:
                        with open(os.path.join(saved_dir, fname), 'wb') as f:
                            f.write(zf.read(img_name))
                logger.info(f"Imported {len(image_files)} saved image(s)")

        summary = "Restored: device.json"
        if has_env:
            summary += ", API keys"
        if image_files:
            summary += f", {len(image_files)} image(s)"

        return jsonify({
            "success": True,
            "message": summary + ". Restart required for changes to take effect.",
            "restart_required": True,
            "restored_env": has_env,
            "restored_images": len(image_files)
        })

    except Exception as e:
        logger.error(f"Config import failed: {e}")
        return jsonify({"error": f"Import failed: {e}"}), 500

