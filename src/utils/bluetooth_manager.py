"""Bluetooth manager — handles adapter state, device scanning, and connection.

Uses bluetoothctl (BlueZ) to manage Bluetooth state. Supports scanning for
nearby devices, pairing/connecting, and disconnecting.

In dev mode (non-Pi), all operations return mock data so the web UI remains
functional during development.
"""

import logging
import os
import subprocess
import threading
import re

logger = logging.getLogger(__name__)


def _is_pi():
    """Check if running on a Raspberry Pi (vs Mac dev machine)."""
    return os.path.exists("/proc/device-tree/model")


def _run_bluetoothctl(args, timeout=15):
    """Run a bluetoothctl command and return (success, stdout).

    Args:
        args: List of bluetoothctl arguments (without 'bluetoothctl' prefix).
        timeout: Command timeout in seconds.

    Returns:
        Tuple of (success: bool, output: str).
    """
    cmd = ["bluetoothctl"] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        # bluetoothctl often returns non-zero on benign messages; treat output
        # as the source of truth.
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        logger.error("bluetoothctl timed out: %s", " ".join(cmd))
        return False, "timeout"
    except FileNotFoundError:
        logger.error("bluetoothctl not found — BlueZ not installed")
        return False, "bluetoothctl not found"


class BluetoothManager:
    """Manages Bluetooth adapter state, scanning, and device connections.

    On a Raspberry Pi with BlueZ, this class uses bluetoothctl to:
    - Query adapter power state
    - List paired/connected devices
    - Scan for nearby devices
    - Pair, trust, connect, and disconnect devices

    In dev mode (non-Pi), all operations return mock data.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._is_pi = _is_pi()

    # ------------------------------------------------------------------
    # Adapter
    # ------------------------------------------------------------------

    def get_adapter_status(self):
        """Return adapter power state and address.

        Returns:
            dict with keys: available (bool), powered (bool), address (str|None)
        """
        if not self._is_pi:
            return {"available": False, "powered": False, "address": None}

        success, output = _run_bluetoothctl(["show"], timeout=5)
        if not success:
            return {"available": False, "powered": False, "address": None}

        powered = "Powered: yes" in output
        addr_match = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", output)
        address = addr_match.group(1) if addr_match else None
        return {"available": True, "powered": powered, "address": address}

    def power(self, on=True):
        """Power the Bluetooth adapter on or off.

        Returns:
            (success: bool, message: str)
        """
        if not self._is_pi:
            return True, "dev mode"

        action = "on" if on else "off"
        success, output = _run_bluetoothctl(["power", action], timeout=5)
        if success:
            return True, f"Adapter powered {action}"
        return False, output or f"Failed to power {action}"

    # ------------------------------------------------------------------
    # Device listing
    # ------------------------------------------------------------------

    def _parse_devices(self, output):
        """Parse `bluetoothctl devices` output into a list of dicts.

        Lines look like:
            Device XX:XX:XX:XX:XX:XX Device Name
        """
        devices = []
        for line in output.splitlines():
            line = line.strip()
            m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s*(.*)", line)
            if m:
                mac = m.group(1)
                name = m.group(2).strip() or mac
                devices.append({"mac": mac, "name": name})
        return devices

    def get_paired_devices(self):
        """Return list of paired devices (mac, name)."""
        if not self._is_pi:
            return []
        success, output = _run_bluetoothctl(["devices", "Paired"], timeout=5)
        if not success:
            return []
        return self._parse_devices(output)

    def get_connected_devices(self):
        """Return list of connected devices with full info."""
        if not self._is_pi:
            return []
        success, output = _run_bluetoothctl(["devices", "Connected"], timeout=5)
        if not success:
            return []
        connected = self._parse_devices(output)
        # Enrich with detailed info (icon type, etc.)
        for dev in connected:
            info = self.get_device_info(dev["mac"])
            dev.update(info)
        return connected

    def get_device_info(self, mac):
        """Return detailed info for a single device.

        Returns:
            dict with keys: mac, name, paired, connected, trusted, icon
        """
        if not self._is_pi:
            return {"mac": mac, "name": mac, "paired": False,
                    "connected": False, "trusted": False, "icon": None}

        success, output = _run_bluetoothctl(["info", mac], timeout=5)
        if not success:
            return {"mac": mac, "name": mac, "paired": False,
                    "connected": False, "trusted": False, "icon": None}

        name = mac
        name_m = re.search(r"Name:\s*(.+)", output)
        if name_m:
            name = name_m.group(1).strip()

        return {
            "mac": mac,
            "name": name,
            "paired": "Paired: yes" in output,
            "connected": "Connected: yes" in output,
            "trusted": "Trusted: yes" in output,
            "icon": _extract_field(output, "Icon"),
        }

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_devices(self, timeout=10):
        """Scan for nearby Bluetooth devices.

        Starts a discovery scan, waits for `timeout` seconds, then stops and
        returns all discovered devices (including non-paired ones).

        Returns:
            list of dicts: mac, name, paired, connected
        """
        if not self._is_pi:
            return []

        # `bluetoothctl scan on` blocks until stopped. Run it as a background
        # process, let it collect device discoveries, then terminate and read
        # the buffered output.
        try:
            proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            logger.error("bluetoothctl not found — BlueZ not installed")
            return []

        try:
            proc.stdin.write("scan on\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Expected — stop the scan and read what we captured
            try:
                proc.stdin.write("scan off\nquit\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

        output = proc.stdout.read() if proc.stdout else ""

        # Collect all unique devices seen during the scan output
        seen = {}
        for line in output.splitlines():
            line = line.strip()
            m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s*(.*)", line)
            if m:
                mac = m.group(1)
                name = m.group(2).strip() or mac
                if mac not in seen:
                    seen[mac] = {"mac": mac, "name": name}

        # If the scan found nothing new, fall back to the paired device list
        # (the adapter may already know devices that didn't broadcast during
        # the scan window).
        if not seen:
            seen = {d["mac"]: d for d in self.get_paired_devices()}

        # Enrich with current connection/pair state
        paired = {d["mac"]: d for d in self.get_paired_devices()}
        connected_macs = {d["mac"] for d in self.get_connected_devices()}
        for dev in seen.values():
            dev["paired"] = dev["mac"] in paired
            dev["connected"] = dev["mac"] in connected_macs
            # Prefer the paired device's name if scan only gave a MAC
            if dev["mac"] in paired and dev["name"] == dev["mac"]:
                dev["name"] = paired[dev["mac"]]["name"]
        return list(seen.values())

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self, mac):
        """Pair (if needed), trust, and connect to a device.

        Returns:
            (success: bool, message: str)
        """
        if not self._is_pi:
            return True, "dev mode"

        # Ensure adapter is powered on
        status = self.get_adapter_status()
        if not status["available"]:
            return False, "Bluetooth adapter not available"
        if not status["powered"]:
            ok, msg = self.power(on=True)
            if not ok:
                return False, msg

        # Pair first (no-op if already paired). bluetoothctl pair may need
        # the agent to be registered, but the default agent handles PIN prompts.
        pair_ok, pair_out = _run_bluetoothctl(["pair", mac], timeout=30)
        if not pair_ok and "AlreadyExists" not in pair_out and "already paired" not in pair_out.lower():
            # Some devices connect without explicit pairing; continue to connect
            logger.warning("Pair failed for %s: %s — attempting connect anyway", mac, pair_out)

        # Trust so the device auto-reconnects in the future
        _run_bluetoothctl(["trust", mac], timeout=5)

        # Connect
        ok, out = _run_bluetoothctl(["connect", mac], timeout=20)
        if ok:
            return True, f"Connected to {mac}"
        return False, out or f"Failed to connect to {mac}"

    def disconnect(self, mac):
        """Disconnect from a device.

        Returns:
            (success: bool, message: str)
        """
        if not self._is_pi:
            return True, "dev mode"

        ok, out = _run_bluetoothctl(["disconnect", mac], timeout=15)
        if ok:
            return True, f"Disconnected from {mac}"
        return False, out or f"Failed to disconnect from {mac}"

    def remove(self, mac):
        """Remove a paired device from the adapter.

        Returns:
            (success: bool, message: str)
        """
        if not self._is_pi:
            return True, "dev mode"

        ok, out = _run_bluetoothctl(["remove", mac], timeout=10)
        if ok:
            return True, f"Removed {mac}"
        return False, out or f"Failed to remove {mac}"


def _extract_field(output, field):
    """Extract the value of a 'Field: value' line from bluetoothctl output."""
    m = re.search(rf"^{re.escape(field)}:\s*(.+)$", output, re.MULTILINE)
    return m.group(1).strip() if m else None
