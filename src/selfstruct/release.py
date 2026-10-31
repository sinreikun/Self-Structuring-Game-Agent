"""Public update trust anchor. Signing key never ships with the application."""
UPDATE_KEY = '19f9b61fb5c3228e47516ff27c6f2a4bc850cee780c3a2a8dbee773a1e49c552'
UPDATE_URL = "https://raw.githubusercontent.com/sinreikun/Self-Structuring-Game-Agent/main/updates/{channel}.json"

# Previous trust anchor is accepted only for already-cached, verified bodies.
LEGACY_UPDATE_KEYS = ('6b2268b23361ab82b53b6803f0ed0cd2fd2097478beb45c4fdafadd24c13a88f',)
LAUNCHER_VERSION = '1.1.0'
