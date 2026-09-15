"""hear_node board-class metadata shared by flash/enroll/release tooling.

⚠️PSRAM BUS MODE IS PER PHYSICAL NODE, NOT PER BOARD CLASS. `esp32s3-i2s-gps` is a nominal
class covering boards that are wired the same way but do NOT carry the same memory: ageev is
16MB flash / 8MB OCTAL PSRAM, gold is 8MB flash / 2MB QUAD PSRAM. Bus mode is a compile-time
pin-mux/ROM setting, not a runtime probe, so one class-wide FQBN necessarily mis-builds one of
them. Gold ran the octal image for weeks: `psramFound()` came back false, the raw ring was never
allocated (`praw  NO PSRAM ring`), and everything that expected PSRAM fell back onto the internal
heap until `heap_min` reached 92 B and `loop_max_ms` climbed past 1.4 s.

So the FQBN is chosen from (board class, node id), and a node may pin its own PSRAM mode here.
Nodes not listed keep their class default, i.e. their behaviour is unchanged.
"""

from __future__ import annotations

# Quad-PSRAM boards cannot use the XIAO_ESP32S3 board definition: its only PSRAM menu values are
# `opi` (octal) and `disabled` (no BOARD_HAS_PSRAM at all). The generic esp32s3 definition is the
# one that exposes `PSRAM=enabled` (QSPI + BOARD_HAS_PSRAM); the remaining options restate what
# XIAO_ESP32S3 sets by default so the only intended difference is the PSRAM bus:
#   flash 8MB, default_8MB partitions (3MB app, dual OTA), USB-OTG CDC on boot.
PSRAM_MODES = {
    "octal": {
        "fqbn": "esp32:esp32:XIAO_ESP32S3:PSRAM=opi",
        "expect_psram": True,
        "variant_suffix": "",
    },
    "quad": {
        "fqbn": ("esp32:esp32:esp32s3:PSRAM=enabled,FlashSize=8M,PartitionScheme=default_8MB,"
                 "USBMode=default,CDCOnBoot=cdc"),
        "expect_psram": True,
        "variant_suffix": "-qspi",
    },
}

DEFAULT_PSRAM_MODE = "octal"
FQBN = PSRAM_MODES[DEFAULT_PSRAM_MODE]["fqbn"]
DEFAULT_BOARD_CLASS = "xiao-s3-pps"

BOARD_PROFILES = {
    "xiao-s3-pps": {
        "cpp_flags": [],
        "release_stem": "hear_node-xiao-s3-pps",
        "board_header": "firmware/boards/xiao_s3_sense.h",
        "psram_mode": "octal",
    },
    "esp32s3-i2s-gps": {
        "cpp_flags": ["-DHEAR_BOARD_ESP32S3_I2S_GPS"],
        "release_stem": "hear_node-esp32s3-i2s-gps",
        "board_header": "firmware/boards/esp32s3_i2s_gps.h",
        "psram_mode": "octal",
    },
}

# Physical nodes whose silicon does not match their class default. Each entry is a measured fact
# about one board, so it names the evidence that put it here.
#
# kasami is deliberately ABSENT: it is the same class as gold and ageev and has no bare-board
# flash/PSRAM scan anywhere in the record (docs/REDESIGN-LESSONS.md §4), so its bus mode is
# UNKNOWN. Guessing it into this table would be the Ageev-class mistake again; it keeps the class
# default until someone scans it. The boot assertion added with this table is what will say so out
# loud if the default is wrong for it.
NODE_PSRAM_MODES = {
    "gold": {
        "board_class": "esp32s3-i2s-gps",
        "psram_mode": "quad",
        "why": ("8MB flash / 2MB quad PSRAM (docs/REDESIGN-LESSONS.md §1.8). The octal class "
                "image leaves it with psramFound()==false and no raw ring."),
    },
}

UPLOAD_SUFFIXES = {
    "app": (".bin", "hear_node.ino.bin"),
    "bootloader": ("-bootloader.bin", "hear_node.ino.bootloader.bin"),
    "partitions": ("-partitions.bin", "hear_node.ino.partitions.bin"),
    "merged": ("-merged.bin", "hear_node.ino.merged.bin"),
    "elf": (".elf", "hear_node.ino.elf"),
}


def known_board_classes():
    return tuple(BOARD_PROFILES)


def require_board_class(board_class):
    if board_class not in BOARD_PROFILES:
        raise ValueError("unknown board class %r (known: %s)"
                         % (board_class, ", ".join(sorted(BOARD_PROFILES))))
    return board_class


def build_extra_flags(board_class, *extra_flags):
    require_board_class(board_class)
    flags = [f for f in BOARD_PROFILES[board_class]["cpp_flags"] if f]
    flags.extend(f for f in extra_flags if f)
    return " ".join(flags)


def require_psram_mode(mode):
    if mode not in PSRAM_MODES:
        raise ValueError("unknown PSRAM mode %r (known: %s)"
                         % (mode, ", ".join(sorted(PSRAM_MODES))))
    return mode


def known_psram_modes():
    return tuple(sorted(PSRAM_MODES))


def node_override(node):
    """The per-node hardware record for `node`, or None. Node ids are case-insensitive."""
    if not node:
        return None
    return NODE_PSRAM_MODES.get(str(node).strip().lower())


def psram_mode(board_class, node=None):
    """The PSRAM bus mode to BUILD for this node, which is a property of its silicon.

    A node listed in NODE_PSRAM_MODES pins its own mode; anything else -- including a node nobody
    has scanned yet -- gets its class default, so this cannot silently change what an untested
    node has been running.
    """
    require_board_class(board_class)
    over = node_override(node)
    if over is not None:
        if over["board_class"] != board_class:
            raise ValueError("node %r is recorded as board class %r, not %r"
                             % (node, over["board_class"], board_class))
        return require_psram_mode(over["psram_mode"])
    return require_psram_mode(BOARD_PROFILES[board_class].get("psram_mode", DEFAULT_PSRAM_MODE))


def fqbn(board_class=None, node=None):
    """The FQBN to compile/upload with for this (class, node) pair."""
    if board_class is None:
        board_class = DEFAULT_BOARD_CLASS
    return PSRAM_MODES[psram_mode(board_class, node)]["fqbn"]


def expects_psram(board_class, node=None):
    """True when the chosen build defines BOARD_HAS_PSRAM, i.e. psramFound() must come back true."""
    return bool(PSRAM_MODES[psram_mode(board_class, node)]["expect_psram"])


def build_variant(board_class, node=None):
    """Name of the compiled image this node needs: the class, plus a suffix when its bus differs.

    The suffix exists because two nodes of one class can need two DIFFERENT binaries. Keeping the
    default suffix empty leaves every currently-built artifact named exactly as before.
    """
    mode = psram_mode(board_class, node)
    return board_class + PSRAM_MODES[mode]["variant_suffix"]


def release_variant_refusal(board_class, node, release_psram_mode=None):
    """Why this node must not take this release variant, or None.

    Release assets are named by board class plus PSRAM bus variant. The safety property is that
    the variant selected for a node must match the bus mode recorded for that physical board; a
    quad board must never receive the default octal asset, and vice versa.
    """
    want = psram_mode(board_class, node)
    got = want if release_psram_mode is None else require_psram_mode(release_psram_mode)
    if got == want:
        return None
    return ("%s needs a %s-PSRAM image for %s, not the %s-PSRAM release variant"
            % (node, want, board_class, got))


def release_stem(board_class, psram_mode_name=None):
    require_board_class(board_class)
    mode = require_psram_mode(psram_mode_name or BOARD_PROFILES[board_class].get(
        "psram_mode", DEFAULT_PSRAM_MODE))
    return BOARD_PROFILES[board_class]["release_stem"] + PSRAM_MODES[mode]["variant_suffix"]


def board_header(board_class):
    require_board_class(board_class)
    return BOARD_PROFILES[board_class]["board_header"]


def release_asset_name(tag, board_class, kind="app", psram_mode_name=None):
    require_board_class(board_class)
    if kind not in UPLOAD_SUFFIXES:
        raise ValueError("unknown release asset kind %r" % kind)
    suffix, _ = UPLOAD_SUFFIXES[kind]
    return "%s-%s%s" % (release_stem(board_class, psram_mode_name), tag, suffix)


def upload_filename(kind):
    try:
        return UPLOAD_SUFFIXES[kind][1]
    except KeyError as e:
        raise ValueError("unknown upload asset kind %r" % kind) from e
