import pathlib
import re
import sys


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def patch_file(path: pathlib.Path) -> None:
    if not path.exists():
        die(f"file not found: {path}")

    original = path.read_text(encoding="utf-8", errors="strict")
    text = original

    # 1) Ensure atomic include exists.
    if "#include <zephyr/sys/atomic.h>" not in text:
        text, n = re.subn(
            r"(#include <zephyr/logging/log\.h>\n)",
            r"\1#include <zephyr/sys/atomic.h>\n",
            text,
            count=1,
        )
        if n != 1:
            die("failed to insert atomic include (anchor not found)")

    # 2) Inject scan retry + scan kick logic after "static int start_scan(void);"
    injection_anchor = "static int start_scan(void);\n"
    if "scan_kick_work_handler" not in text:
        if injection_anchor not in text:
            die("failed to find start_scan() forward decl anchor")

        injection = """\

/*
 * Some peripherals (esp. after deep sleep) may only include the split service UUID
 * in the scan response, not the primary advertising packet. Passive scanning
 * won't see that data, so we use active scanning and make scan startup resilient.
 */
static atomic_t scan_retry_scheduled;
static void scan_retry_work_handler(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(scan_retry_work, scan_retry_work_handler);

static void scan_retry_work_handler(struct k_work *work) {
	ARG_UNUSED(work);
	atomic_clear(&scan_retry_scheduled);
	start_scan();
}

/*
 * Periodic "scan kick" to keep scanning alive even if the controller/stack ends up
 * in a non-scanning state after deep sleep disconnects. Dongle is USB-powered.
 */
#ifndef CONFIG_ZMK_SPLIT_BLE_CENTRAL_SCAN_KICK_INTERVAL_MS
#define CONFIG_ZMK_SPLIT_BLE_CENTRAL_SCAN_KICK_INTERVAL_MS 5000
#endif

static void scan_kick_work_handler(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(scan_kick_work, scan_kick_work_handler);

static void scan_kick_work_handler(struct k_work *work) {
	ARG_UNUSED(work);
	(void)start_scan();
	k_work_schedule(&scan_kick_work, K_MSEC(CONFIG_ZMK_SPLIT_BLE_CENTRAL_SCAN_KICK_INTERVAL_MS));
}
"""
        text = text.replace(injection_anchor, injection_anchor + injection, 1)

    # 3) Make scanning active + resilient.
    text = text.replace("BT_LE_SCAN_PASSIVE", "BT_LE_SCAN_ACTIVE")

    if "(void)bt_le_scan_stop();" not in text:
        # Insert stop call at the top of start_scan()
        text, n = re.subn(
            r"(static int start_scan\(void\) \{\n\tint err;\n)",
            r"\1\n\t(void)bt_le_scan_stop();\n",
            text,
            count=1,
        )
        if n != 1:
            die("failed to inject bt_le_scan_stop() into start_scan()")

    if "Scanning already active" not in text:
        # Wrap bt_le_scan_start() error handling to treat -EALREADY as success and retry otherwise.
        # This is intentionally minimal and keyed by the existing log string.
        if "Scanning failed to start" not in text:
            die("expected log anchor not found (Scanning failed to start)")

        # Replace the existing error block only if it matches the simple upstream structure.
        text, n = re.subn(
            r"""err = bt_le_scan_start\(BT_LE_SCAN_ACTIVE, split_central_device_found\);\n\tif \(err\) \{\n\t\tLOG_ERR\("Scanning failed to start \(err %d\)", err\);\n\t\treturn err;\n\t\}""",
            """err = bt_le_scan_start(BT_LE_SCAN_ACTIVE, split_central_device_found);\n\tif (err) {\n\t\tif (err == -EALREADY) {\n\t\t\tLOG_DBG("Scanning already active");\n\t\t\treturn 0;\n\t\t}\n\n\t\tLOG_ERR("Scanning failed to start (err %d)", err);\n\n\t\tif (atomic_cas(&scan_retry_scheduled, 0, 1)) {\n\t\t\tk_work_schedule(&scan_retry_work, K_MSEC(750));\n\t\t}\n\n\t\treturn err;\n\t}""",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n != 1:
            # If upstream changed shape, don't silently produce a half-patch.
            die("failed to rewrite start_scan() error handling (pattern mismatch)")

    # 4) Schedule scan kick in central init (idempotent).
    if "k_work_schedule(&scan_kick_work" not in text:
        text, n = re.subn(
            r"(bt_conn_cb_register\(&conn_callbacks\);\n\n\treturn start_scan\(\);\n\})",
            r'bt_conn_cb_register(&conn_callbacks);\n\n\tk_work_schedule(&scan_kick_work, K_NO_WAIT);\n\n\treturn start_scan();\n}',
            text,
            count=1,
        )
        if n != 1:
            die("failed to inject scan kick schedule into zmk_split_bt_central_init()")

    if text == original:
        print(f"{path}: already patched (no changes)")
        return

    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"{path}: patched")


def main() -> None:
    # Called from repo root after west update.
    candidate_paths = [
        pathlib.Path("zmk/app/src/split/bluetooth/central.c"),
        pathlib.Path("zmk/app/src/split/bluetooth/central.c.in"),
    ]
    for p in candidate_paths:
        if p.exists():
            patch_file(p)
            return
    die("could not find ZMK split central.c under ./zmk/app/src/split/bluetooth/")


if __name__ == "__main__":
    main()

