"""hear_node board-class metadata shared by flash/enroll/release tooling."""

from __future__ import annotations

FQBN = "esp32:esp32:XIAO_ESP32S3:PSRAM=opi"
DEFAULT_BOARD_CLASS = "xiao-s3-pps"

BOARD_PROFILES = {
    "xiao-s3-pps": {
        "cpp_flags": [],
        "release_stem": "hear_node-xiao-s3-pps",
    },
    "esp32s3-i2s-gps": {
        "cpp_flags": ["-DHEAR_BOARD_ESP32S3_I2S_GPS"],
        "release_stem": "hear_node-esp32s3-i2s-gps",
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


def release_stem(board_class):
    require_board_class(board_class)
    return BOARD_PROFILES[board_class]["release_stem"]


def release_asset_name(tag, board_class, kind="app"):
    require_board_class(board_class)
    if kind not in UPLOAD_SUFFIXES:
        raise ValueError("unknown release asset kind %r" % kind)
    suffix, _ = UPLOAD_SUFFIXES[kind]
    return "%s-%s%s" % (release_stem(board_class), tag, suffix)


def upload_filename(kind):
    try:
        return UPLOAD_SUFFIXES[kind][1]
    except KeyError as e:
        raise ValueError("unknown upload asset kind %r" % kind) from e
