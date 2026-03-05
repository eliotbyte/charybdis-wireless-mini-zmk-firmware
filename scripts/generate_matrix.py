import json

# === CONFIGURATION ===
board = "nice_nano"
keymap = "colemak_dh"

# Map each format to the shields it should build
format_shields = {
    "dongle": ["charybdis_left", "charybdis_right", "charybdis_dongle"],
}

groups = [{
    "keymap": keymap,
    "format": "dongle",
    "name": f"{keymap}-dongle",
    "board": board,
}]

# Dump matrix as compact JSON (GitHub expects it this way)
print(json.dumps(groups))
