# XIAO nRF52840 Sense (BLE) → Streamlit (two charts, smooth, CSV on Stop)
# MAC-free connect: resolve by device name and/or NUS service UUID
# Nordic UART Service (NUS):
#   Service: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
#   TX notify char (peripheral→host): 6E400003-B5A3-F393-E0A9-E50E24DCCA9E

import asyncio
import time
import threading
import queue
from collections import deque
from typing import Optional, Tuple, List

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.characteristic import BleakGATTCharacteristic

# ---------------- Config ----------------
REFRESH_MS = 250
RING = 4000
CSV_NAME_BASE = "data_xiao"

NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E".lower()
NUS_TX_CHAR = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E".lower()
PREF_NAME = "XIAO-Sense-BLE"

EXPECTED_KEYS = ("ax", "ay", "az", "gx", "gy", "gz")

# Connection robustness
FAST_START_WAIT_S = 1.0
CONNECT_RETRIES = 3
NOTIFY_RETRIES = 3
POST_CONNECT_GATT_REFRESH_S = 0.35
POST_NOTIFY_DELAY_S = 0.06
RECONNECT_BACKOFF_S = 1.5

st.set_page_config(page_title="XIAO Sense BLE — Two Charts", layout="wide")
st.title("XIAO nRF52840 Sense — BLE IMU (two charts, MAC-free connect)")

# ---------------- Session ----------------
ss = st.session_state
for k, v in {
    "ble_devices": [],                  # List[Tuple[label, BLEDevice]]
    "selected_label": None,
    "selected_dev": None,               # BLEDevice
    "reader_thread": None,
    "stop_event": None,
    "connected": False,
    "last_error": "",
    "t0": None,
    "q_parsed": queue.Queue(maxsize=20000),
    "data": deque(maxlen=RING),
    "all_rows": [],
    "acc_ph": None, "gyro_ph": None,
    "acc_chart": None, "gyro_chart": None,
    "plotted_n": 0,
    "download_bytes": b"",
    "download_name": "",
}.items():
    ss.setdefault(k, v)

# ---------------- Helpers ----------------
def parse_csv_line(line: str) -> Optional[Tuple[float,float,float,float,float,float,float]]:
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("err"):
        return None
    if "," in s and ":" not in s:
        parts = s.split(",")
        if len(parts) >= 7:
            try:
                t, ax, ay, az, gx, gy, gz = (float(x) for x in parts[:7])
                return (t, ax, ay, az, gx, gy, gz)
            except ValueError:
                return None
    if ":" in s:
        kv = {}
        for tok in s.split():
            if ":" not in tok: 
                continue
            k, v = tok.split(":", 1)
            if k in EXPECTED_KEYS:
                try: kv[k] = float(v)
                except ValueError: return None
        if all(k in kv for k in EXPECTED_KEYS):
            return (0.0, kv["ax"], kv["ay"], kv["az"], kv["gx"], kv["gy"], kv["gz"])
    return None

def safe_decode(b: bytes) -> str:
    return b.decode(errors="ignore")

def label_for(d: BLEDevice, ad: Optional[AdvertisementData] = None) -> str:
    nm = d.name or "Unknown"
    return f"{nm} — {d.address}"

def looks_like_xiao_nus(dev: BLEDevice, ad: Optional[AdvertisementData]) -> bool:
    name_ok = (dev.name or "").startswith("XIAO") or (dev.name or "") == PREF_NAME
    if ad and ad.service_uuids:
        has_nus = any((u or "").lower() == NUS_SERVICE for u in ad.service_uuids)
    else:
        has_nus = False
    # Accept if either the name looks right OR it advertises NUS
    return name_ok or has_nus

# ---------------- Scan (MAC-free list built from filters) ----------------
def do_scan(timeout: float = 4.0) -> List[Tuple[str, BLEDevice]]:
    async def _scan():
        seen = {}
        def _cb(d: BLEDevice, ad: AdvertisementData):
            if looks_like_xiao_nus(d, ad):
                seen[d.address] = (d, ad)

        # Start background detector
        scanner = BleakScanner(detection_callback=_cb)
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()

        out = []
        for addr, (dev, ad) in seen.items():
            out.append((label_for(dev, ad), dev))
        # Sort: exact PREF_NAME first, then other XIAO-like names
        out.sort(key=lambda x: (0 if PREF_NAME in x[0] else 1, x[0]))
        return out

    return asyncio.run(_scan())

# ---------------- Resolve by name/service only (ignore MAC entirely) -----
async def resolve_by_filter(timeout: float = 6.0) -> Optional[BLEDevice]:
    def filt(d: BLEDevice, ad: AdvertisementData):
        return looks_like_xiao_nus(d, ad)
    try:
        dev = await BleakScanner.find_device_by_filter(filt, timeout=timeout)
        return dev
    except Exception:
        return None

# ---------------- BLE reader thread --------------------------------------
def ble_reader_thread(initial_dev: Optional[BLEDevice], stop_event: threading.Event,
                      q_parsed: "queue.Queue", err_holder: list, conn_flag: list):
    async def run():
        buffer = bytearray()

        def handle_notify(_: BleakGATTCharacteristic, data: bytearray):
            nonlocal buffer
            buffer += data
            while True:
                npos = buffer.find(b"\n")
                rpos = buffer.find(b"\r")
                term = -1
                if npos != -1 and rpos != -1: term = min(npos, rpos)
                elif npos != -1: term = npos
                elif rpos != -1: term = rpos
                if term == -1: break
                line = safe_decode(buffer[:term])
                drop = term + 1
                if drop < len(buffer) and buffer[drop:drop+1] in (b"\n", b"\r"):
                    drop += 1
                del buffer[:drop]
                parsed = parse_csv_line(line)
                if parsed:
                    try: q_parsed.put_nowait(parsed)
                    except queue.Full: pass

        async def connect_once() -> Optional[BleakClient]:
            # Always re-resolve the device by filter (ignore MAC/RPA)
            target = await resolve_by_filter(timeout=5.5)
            if target is None:
                err_holder[:] = [f"Device not found by name/service; is it advertising?"]
                return None

            last_exc: Optional[Exception] = None
            for i in range(CONNECT_RETRIES):
                try:
                    client = BleakClient(target, disconnected_callback=lambda _c: None)
                    await client.__aenter__()  # enter async context
                    await asyncio.sleep(POST_CONNECT_GATT_REFRESH_S)
                    try:
                        _ = await client.get_services()  # refresh GATT cache
                    except Exception:
                        pass
                    if client.is_connected:
                        return client
                except Exception as e:
                    last_exc = e
                    await asyncio.sleep(0.45 + 0.25 * i)
            err_holder[:] = [f"Connect failed: {type(last_exc).__name__}: {last_exc}"] if last_exc else ["Connect failed"]
            return None

        async def start_notifications(client: BleakClient) -> bool:
            char = None
            try:
                svcs = client.services or await client.get_services_by_uuid()
                for s in svcs:
                    for c in s.characteristics:
                        if (c.uuid or "").lower() == NUS_TX_CHAR:
                            char = c
                            break
                    if char:
                        break
            except Exception:
                char = None
            if char is None:
                char = NUS_TX_CHAR

            last_exc: Optional[Exception] = None
            for i in range(NOTIFY_RETRIES):
                try:
                    await asyncio.sleep(POST_NOTIFY_DELAY_S)
                    await client.start_notify(char, handle_notify)
                    return True
                except Exception as e:
                    last_exc = e
                    await asyncio.sleep(0.35 + 0.2 * i)
            err_holder[:] = [f"Notify start failed: {type(last_exc).__name__}: {last_exc}"] if last_exc else ["Notify start failed"]
            return False

        # Main loop: connect → notify → pump; reconnect on drop until Stop
        while not stop_event.is_set():
            client = await connect_once()
            if client is None:
                await asyncio.sleep(1.0)
                continue

            conn_flag[:] = [True]
            err_holder[:] = [""]

            try:
                ok = await start_notifications(client)
                if not ok:
                    await client.__aexit__(None, None, None)
                    conn_flag[:] = [False]
                    await asyncio.sleep(RECONNECT_BACKOFF_S)
                    continue

                while not stop_event.is_set() and client.is_connected:
                    await asyncio.sleep(0.1)

                try: await client.stop_notify(NUS_TX_CHAR)
                except Exception: pass

            except Exception as e:
                err_holder[:] = [f"{type(e).__name__}: {e}"]
            finally:
                try: await client.__aexit__(None, None, None)
                except Exception: pass
                conn_flag[:] = [False]

            if stop_event.is_set():
                break

            await asyncio.sleep(RECONNECT_BACKOFF_S)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run())
    loop.close()

def start_reader(dev: Optional[BLEDevice]):
    if ss.reader_thread and ss.reader_thread.is_alive():
        return
    ss.stop_event = threading.Event()
    ss.last_error = ""
    ss.data.clear()
    ss.all_rows = []
    ss.download_bytes = b""
    ss.download_name = ""
    ss.acc_chart = None
    ss.gyro_chart = None
    ss.plotted_n = 0
    ss.t0 = time.time()

    err_holder = [""]
    conn_flag = [False]
    th = threading.Thread(
        target=ble_reader_thread,
        name="BLE-Reader",
        args=(dev, ss.stop_event, ss.q_parsed, err_holder, conn_flag),
        daemon=True
    )
    th.start()
    ss.reader_thread = th

    time.sleep(0.5)
    ss.last_error = err_holder[0]
    ss.connected = conn_flag[0]

    # Fast-start seed
    t_deadline = time.time() + FAST_START_WAIT_S
    while time.time() < t_deadline:
        if not ss.q_parsed.empty():
            break
        time.sleep(0.02)
    pump_queue_into_buffers(repeat=3)
    st.rerun()

def stop_reader_and_save():
    ev, th = ss.stop_event, ss.reader_thread
    if ev: ev.set()
    if th and th.is_alive(): th.join(timeout=2.0)
    ss.reader_thread = None
    ss.stop_event = None
    ss.connected = False
    if ss.all_rows:
        df = pd.DataFrame(ss.all_rows, columns=["t","ax","ay","az","gx","gy","gz"])
        ss.download_bytes = df.to_csv(index=False).encode("utf-8")
        ts = time.strftime("%Y%m%d_%H%M%S")
        ss.download_name = f"{CSV_NAME_BASE}_{ts}.csv"
        st.success("Data ready. Use the download button below.")

def pump_queue_into_buffers(repeat: int = 1) -> int:
    total = 0
    for _ in range(max(1, repeat)):
        added = 0
        while True:
            try:
                row = ss.q_parsed.get_nowait()
            except queue.Empty:
                break
            t, ax, ay, az, gx, gy, gz = row
            t_rel = (time.time() - ss.t0) if ss.t0 else (t if t else 0.0)
            full = (t_rel, ax, ay, az, gx, gy, gz)
            ss.data.append(full)
            ss.all_rows.append(full)
            added += 1
        total += added
        if added == 0:
            break
        time.sleep(0.01)
    return total

# ---------------- Fixed placeholders (stable charts) ----------------
if ss.acc_ph is None or ss.gyro_ph is None:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Acceleration — ax, ay, az")
        ss.acc_ph = st.empty()
    with c2:
        st.subheader("Gyroscope — gx, gy, gz")
        ss.gyro_ph = st.empty()

# ---------------- Controls ----------------
row1 = st.columns([1.2, 0.8, 1.1, 1.0, 2.2])
with row1[0]:
    if st.button("Scan", disabled=ss.connected):
        try:
            ss.ble_devices = do_scan(4.0)
            if not ss.ble_devices:
                st.warning("Not found. Ensure the board advertises and is not paired in Windows.")
        except Exception as e:
            ss.last_error = f"Scan error: {e}"

with row1[1]:
    labels = [label for (label, _dev) in ss.ble_devices]
    label = st.selectbox("Device", options=labels, index=0 if labels else None,
                         placeholder="Scan & choose", disabled=ss.connected)
    if label:
        ss.selected_label = label
        for (lab, dev) in ss.ble_devices:
            if lab == label:
                ss.selected_dev = dev
                break

with row1[2]:
    start_clicked = st.button("Start", type="primary", disabled=ss.connected)  # no MAC needed
with row1[3]:
    stop_clicked  = st.button("Stop", disabled=not ss.connected)
with row1[4]:
    status = st.empty()

if start_clicked:
    # ignore MAC: we re-resolve by filter inside the thread just-in-time
    start_reader(ss.selected_dev)

if stop_clicked:
    stop_reader_and_save()

# ---------------- Data pump & charts ----------------
pump_queue_into_buffers(repeat=3)

if ss.acc_chart is None and len(ss.data):
    df0 = pd.DataFrame([ss.data[-1]], columns=["t","ax","ay","az","gx","gy","gz"]).set_index("t")
    ss.acc_chart  = ss.acc_ph.line_chart(df0[["ax","ay","az"]], use_container_width=True)
    ss.gyro_chart = ss.gyro_ph.line_chart(df0[["gx","gy","gz"]], use_container_width=True)
    ss.plotted_n = len(ss.data)

if ss.acc_chart is not None:
    n_total = len(ss.data)
    if n_total > ss.plotted_n:
        new_slice = list(ss.data)[ss.plotted_n:n_total]
        df_new = pd.DataFrame(new_slice, columns=["t","ax","ay","az","gx","gy","gz"]).set_index("t")
        try:
            ss.acc_chart.add_rows(df_new[["ax","ay","az"]])
            ss.gyro_chart.add_rows(df_new[["gx","gy","gz"]])
        except Exception as e:
            ss.last_error = f"Chart update error: {e}"
        ss.plotted_n = n_total

# Status & download
if ss.last_error:
    status.caption(f"⚠️ {ss.last_error}")
elif ss.connected:
    status.caption("Connected (resolved by name/service, not MAC).")
else:
    status.caption("Scan → select label (optional) → Start. Ensure device is NOT paired in Windows.")

if ss.download_bytes:
    st.download_button(
        "Download CSV",
        data=ss.download_bytes,
        file_name=ss.download_name or "data_xiao.csv",
        mime="text/csv",
        key="download_csv_ble",
    )

if ss.connected:
    st_autorefresh(interval=REFRESH_MS, key="ble_two_charts_refresh_fast")
