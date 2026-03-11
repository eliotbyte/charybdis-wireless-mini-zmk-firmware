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

    # 2) Inject scan retry + scan kick logic near start_scan()
    # Prefer inserting after a forward declaration; if none exists, insert just before the definition.
    if "scan_kick_work_handler" not in text:
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
        forward_decl_re = re.compile(r"^static\s+int\s+start_scan\s*\(\s*void\s*\)\s*;\s*$",
                                     re.MULTILINE)
        m = forward_decl_re.search(text)
        if m:
            insert_at = m.end()
            text = text[:insert_at] + injection + text[insert_at:]
        else:
            # Fall back to inserting before the function definition.
            def_re = re.compile(r"^static\s+int\s+start_scan\s*\(\s*void\s*\)\s*\{",
                                re.MULTILINE)
            m2 = def_re.search(text)
            if not m2:
                die("failed to find start_scan() declaration or definition anchor")
            insert_at = m2.start()
            text = text[:insert_at] + injection + text[insert_at:]

    # 3) Make scanning active + resilient.
    text = text.replace("BT_LE_SCAN_PASSIVE", "BT_LE_SCAN_ACTIVE")

    if "(void)bt_le_scan_stop();" not in text:
        # Insert stop call near the top of start_scan(), after the err declaration.
        text, n = re.subn(
            r"(static\s+int\s+start_scan\s*\(\s*void\s*\)\s*\{\s*\n(?:[ \t].*\n)*?[ \t]*int\s+err\s*;\s*\n)",
            r"\1\n\t(void)bt_le_scan_stop();\n",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n != 1:
            die("failed to inject bt_le_scan_stop() into start_scan() (pattern mismatch)")

    if "Scanning already active" not in text:
        # Try a conservative injection inside the "if (err) {" block following scan_start().
        # If upstream differs too much, we fail loudly rather than risking a half-broken patch.
        scan_start_re = re.compile(
            r"(err\s*=\s*bt_le_scan_start\(\s*BT_LE_SCAN_ACTIVE\s*,\s*split_central_device_found\s*\)\s*;\s*\n)"
            r"(\s*if\s*\(\s*err\s*\)\s*\{\s*\n)",
            re.MULTILINE,
        )
        m = scan_start_re.search(text)
        if not m:
            die("failed to find bt_le_scan_start() + if(err) block to patch")

        inject = (
            "\t\tif (err == -EALREADY) {\n"
            "\t\t\tLOG_DBG(\"Scanning already active\");\n"
            "\t\t\treturn 0;\n"
            "\t\t}\n\n"
            "\t\tif (atomic_cas(&scan_retry_scheduled, 0, 1)) {\n"
            "\t\t\tk_work_schedule(&scan_retry_work, K_MSEC(750));\n"
            "\t\t}\n\n"
        )

        # Insert just after the opening brace of the if(err) block.
        insert_at = m.end(2)
        text = text[:insert_at] + inject + text[insert_at:]

    # 4) Schedule scan kick in central init (idempotent).
    if "k_work_schedule(&scan_kick_work" not in text:
        text, n = re.subn(
            r"(bt_conn_cb_register\(&conn_callbacks\);\s*\n)",
            r"\1\n\tk_work_schedule(&scan_kick_work, K_NO_WAIT);\n",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n != 1:
            die("failed to inject scan kick schedule into zmk_split_bt_central_init() (anchor not found)")

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

