"""Bluetooth blueprint — manage Bluetooth devices from the Settings page.

Provides API endpoints for adapter status, device scanning, and
connect/disconnect operations. Mirrors the WiFi blueprint's pattern.
"""

import logging
import re

from flask import Blueprint, request, jsonify, current_app

logger = logging.getLogger(__name__)
bluetooth_bp = Blueprint("bluetooth", __name__)

# Validate MAC address format to prevent command injection
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}$")


def _get_manager():
    """Return the BluetoothManager from app config, or None."""
    return current_app.config.get("BLUETOOTH_MANAGER")


def _valid_mac(mac):
    """Return True if mac looks like a valid Bluetooth MAC address."""
    return isinstance(mac, str) and bool(_MAC_RE.match(mac))


@bluetooth_bp.route("/bluetooth/status")
def status():
    """Return adapter status and currently connected devices."""
    manager = _get_manager()
    if not manager:
        return jsonify({"available": False, "powered": False, "connected": [], "paired": []})

    adapter = manager.get_adapter_status()
    connected = manager.get_connected_devices() if adapter.get("powered") else []
    paired = manager.get_paired_devices() if adapter.get("powered") else []
    return jsonify({
        "available": adapter["available"],
        "powered": adapter["powered"],
        "address": adapter.get("address"),
        "connected": connected,
        "paired": paired,
    })


@bluetooth_bp.route("/bluetooth/scan")
def scan():
    """Scan for nearby Bluetooth devices.

    Query param:
        timeout: scan duration in seconds (default 10, max 30)
    """
    manager = _get_manager()
    if not manager:
        return jsonify({"devices": [], "error": "Bluetooth manager not available"})

    try:
        timeout = min(max(int(request.args.get("timeout", "10")), 3), 30)
    except ValueError:
        timeout = 10

    devices = manager.scan_devices(timeout=timeout)
    return jsonify({"devices": devices})


@bluetooth_bp.route("/bluetooth/connect", methods=["POST"])
def connect():
    """Pair (if needed) and connect to a device by MAC address."""
    manager = _get_manager()
    if not manager:
        return jsonify({"success": False, "error": "Bluetooth manager not available"}), 500

    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip()
    if not _valid_mac(mac):
        return jsonify({"success": False, "error": "Invalid or missing MAC address"}), 400

    success, message = manager.connect(mac)
    if success:
        return jsonify({"success": True, "message": message, "device": manager.get_device_info(mac)})
    return jsonify({"success": False, "error": message}), 500


@bluetooth_bp.route("/bluetooth/disconnect", methods=["POST"])
def disconnect():
    """Disconnect from a device by MAC address."""
    manager = _get_manager()
    if not manager:
        return jsonify({"success": False, "error": "Bluetooth manager not available"}), 500

    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip()
    if not _valid_mac(mac):
        return jsonify({"success": False, "error": "Invalid or missing MAC address"}), 400

    success, message = manager.disconnect(mac)
    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"success": False, "error": message}), 500


@bluetooth_bp.route("/bluetooth/power", methods=["POST"])
def power():
    """Power the Bluetooth adapter on or off."""
    manager = _get_manager()
    if not manager:
        return jsonify({"success": False, "error": "Bluetooth manager not available"}), 500

    data = request.get_json() or {}
    on = bool(data.get("on", True))
    success, message = manager.power(on=on)
    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"success": False, "error": message}), 500


@bluetooth_bp.route("/bluetooth/remove", methods=["POST"])
def remove():
    """Remove a paired device from the adapter."""
    manager = _get_manager()
    if not manager:
        return jsonify({"success": False, "error": "Bluetooth manager not available"}), 500

    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip()
    if not _valid_mac(mac):
        return jsonify({"success": False, "error": "Invalid or missing MAC address"}), 400

    success, message = manager.remove(mac)
    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"success": False, "error": message}), 500
