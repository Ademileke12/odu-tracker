"""ODU Tracker package
"""
"""ODU Tracker - MTN ODU (ZLT X28-class) network monitor.

Cross-platform (Linux/Windows) companion app for the MTN Broadband 5G ODU
web interface at http://192.168.0.1/.

Features:
  - Data-usage statistics (total / daily / per-poll deltas)
  - Connected device discovery with first-seen tracking
  - Dangerous-device heuristics + alerts (new device, random hostname,
    previously blocked, impatient join rate, etc.)
  - Website visibility: URL filter rules + optional local DNS logging proxy
  - System status: signal, network type, battery (ODU has internal battery)
"""

__version__ = "0.1.0"
APP_NAME = "ODU Tracker"
