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

    # 2) Inject scan retry + scan kick logic near the scanning function.
    # ZMK has used both start_scan() and start_scanning() across revisions.
    scan_fn_names = ("start_scanning", "start_scan")

    def find_decl_or_def(name: str) -> tuple[str, re.Match[str] | None, re.Match[str] | None]:
        decl = re.search(rf"^static\s+int\s+{name}\s*\(\s*void\s*\)\s*;\s*$", text, re.MULTILINE)
        definition = re.search(rf"^static\s+int\s+{name}\s*\(\s*void\s*\)\s*\{{", text, re.MULTILINE)
        return name, decl, definition

    chosen = None
    for nm in scan_fn_names:
        _, d, df = find_decl_or_def(nm)
        if d or df:
            chosen = (nm, d, df)
            break

    if not chosen:
        die("failed to find start_scanning()/start_scan() declaration or definition anchor")

    scan_fn, decl_m, def_m = chosen

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
	%s();
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
	(void)%s();
	k_work_schedule(&scan_kick_work, K_MSEC(CONFIG_ZMK_SPLIT_BLE_CENTRAL_SCAN_KICK_INTERVAL_MS));
}
""" % (scan_fn, scan_fn)

        if decl_m:
            insert_at = decl_m.end()
            text = text[:insert_at] + injection + text[insert_at:]
        else:
            # Insert before the function definition.
            assert def_m is not None
            insert_at = def_m.start()
            text = text[:insert_at] + injection + text[insert_at:]

    # 3) Make scanning active + resilient.
    text = text.replace("BT_LE_SCAN_PASSIVE", "BT_LE_SCAN_ACTIVE")

    # ZMK mainline has its own stop_scanning(); don't try to force bt_le_scan_stop() here.

    # 3b) Ensure scan start is active.
    text = text.replace("BT_LE_SCAN_PASSIVE", "BT_LE_SCAN_ACTIVE")

    # 3c) Add retry-on-failure inside start_scanning()/start_scan() when scan_start fails.
    # We key off the existing log: "Scanning failed to start (err %d)"
    if "scan_retry_scheduled" in text and "k_work_schedule(&scan_retry_work" not in text:
        # Find the error log line and insert retry right after it.
        # Also reset any is_scanning flag if present so future kicks can restart.
        has_is_scanning = "static bool is_scanning" in text
        retry_snippet = "\n\t\tif (atomic_cas(&scan_retry_scheduled, 0, 1)) {\n"
        if has_is_scanning:
            retry_snippet += "\t\t\tis_scanning = false;\n"
        retry_snippet += "\t\t\tk_work_schedule(&scan_retry_work, K_MSEC(750));\n\t\t}\n"

        # Since we can't rely on exact surrounding braces, just inject after the LOG_ERR line once.
        text, n = re.subn(
            r'(LOG_ERR\("Scanning failed to start \(err %d\)", err\);\n)',
            r"\1" + retry_snippet,
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n != 1:
            die("failed to inject scan-start retry after LOG_ERR (anchor not found)")

    # 4) Schedule scan kick in central init (idempotent).
    if "k_work_schedule(&scan_kick_work" not in text:
        # This init function name is stable.
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

