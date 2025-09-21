# xiao_imu_two_charts.py  (fixed Stop → stable download button)

import time
import threading
import queue
from collections import deque
from typing import Optional, Tuple

import pandas as pd
import streamlit as st
import serial
import serial.tools.list_ports
from streamlit_autorefresh import st_autorefresh

# ---------------- Config ----------------
BAUD = 57600
REFRESH_MS = 400             # UI refresh cadence
RING = 2000                  # points kept on-screen
CSV_PATH = "data_xiao.csv"
EXPECTED_KEYS = ("ax","ay","az","gx","gy","gz")

st.set_page_config(page_title="XIAO Sense IMU — Two Charts", layout="wide")
st.title("XIAO nRF52840 Sense — IMU (two charts)")

# ---------------- Session state ----------------
ss = st.session_state
ss.setdefault("selected_port", None)
ss.setdefault("reader_thread", None)
ss.setdefault("stop_event", None)
ss.setdefault("ser_open", False)
ss.setdefault("last_error", "")
ss.setdefault("t0", None)

# thread → UI queues and buffers
ss.setdefault("q_parsed", queue.Queue(maxsize=20000))                 # (0, ax, ay, az, gx, gy, gz)
ss.setdefault("data", deque(maxlen=RING))                             # (t, ax..gz)
ss.setdefault("all_rows", [])                                         # accumulated session

# NEW: keep downloadable CSV in memory so Streamlit doesn't lose the handle
ss.setdefault("download_bytes", b"")
ss.setdefault("download_name", "data_xiao.csv")

def list_ports_labels():
    return [(f"{p.device} — {p.description}", p.device) for p in serial.tools.list_ports.comports()]

def parse_line(line: str) -> Optional[Tuple[float,float,float,float,float,float,float]]:
    s = line.strip()
    if not s or s.startswith("err"):
        return None
    kv = {}
    for tok in s.split():
        if ":" not in tok: continue
        k, v = tok.split(":", 1)
        if k in EXPECTED_KEYS:
            try: kv[k] = float(v)
            except ValueError: return None
    if all(k in kv for k in EXPECTED_KEYS):
        return (0.0, kv["ax"], kv["ay"], kv["az"], kv["gx"], kv["gy"], kv["gz"])
    return None

# ---------------- Reader thread (NO Streamlit calls) ----------------
def reader_thread_fn(port: str, stop_event: threading.Event, q_parsed: "queue.Queue",
                     err_holder: list, ser_open_flag: list):
    ser = None
    try:
        ser = serial.Serial(port, BAUD, timeout=0.05, write_timeout=0.3)
        try:
            ser.dtr = True; ser.rts = True  # helps Windows CDC
        except Exception:
            pass
        ser_open_flag[:] = [True]
        time.sleep(0.2)
        try: ser.reset_input_buffer()
        except Exception: pass

        buf = bytearray()
        while not stop_event.is_set():
            try:
                chunk = ser.read(256)
            except Exception as e:
                err_holder[:] = [f"Read error: {e}"]
                break
            if chunk:
                buf += chunk
                # split on CR/LF
                while True:
                    npos = buf.find(b"\n"); rpos = buf.find(b"\r")
                    term = -1
                    if npos != -1 and rpos != -1: term = min(npos, rpos)
                    elif npos != -1: term = npos
                    elif rpos != -1: term = rpos
                    if term == -1: break
                    line = buf[:term].decode(errors="ignore")
                    drop = term + 1
                    if drop < len(buf) and buf[drop:drop+1] in (b"\n", b"\r"): drop += 1
                    del buf[:drop]
                    parsed = parse_line(line)
                    if parsed:
                        try: q_parsed.put_nowait(parsed)
                        except queue.Full: pass
            # 20 ms pause for smoother visualisation / CPU relief
            time.sleep(0.02)
    except Exception as e:
        err_holder[:] = [f"{type(e).__name__}: {e}"]
    finally:
        try:
            if ser and ser.is_open: ser.close()
        except Exception:
            pass
        ser_open_flag[:] = [False]

def start_reader(port: str):
    if ss.reader_thread and ss.reader_thread.is_alive():
        return
    ss.stop_event = threading.Event()
    ss.last_error = ""
    ss.data.clear()
    ss.all_rows = []
    ss.download_bytes = b""  # clear any previous download
    ss.download_name = "data_xiao.csv"
    ss.t0 = time.time()

    err_holder = [""]
    ser_open_flag = [False]
    th = threading.Thread(
        target=reader_thread_fn,
        name="USB-Serial-Reader",
        args=(port, ss.stop_event, ss.q_parsed, err_holder, ser_open_flag),
        daemon=True
    )
    th.start()
    ss.reader_thread = th
    time.sleep(0.25)
    ss.last_error = err_holder[0]
    ss.ser_open = ser_open_flag[0]

def stop_reader_and_save():
    ev = ss.stop_event
    th = ss.reader_thread
    if ev: ev.set()
    if th and th.is_alive():
        th.join(timeout=1.5)
    ss.reader_thread = None
    ss.stop_event = None
    ss.ser_open = False

    # Build CSV once (bytes) and also write to disk (optional)
    if ss.all_rows:
        df = pd.DataFrame(ss.all_rows, columns=["t","ax","ay","az","gx","gy","gz"])
        # save to disk (optional; nice to have)
        try:
            df.to_csv(CSV_PATH, index=False)
        except Exception:
            pass
        # keep bytes in memory for a stable download button
        ss.download_bytes = df.to_csv(index=False).encode("utf-8")
        # timestamped filename is helpful
        ts = time.strftime("%Y%m%d_%H%M%S")
        ss.download_name = f"data_xiao_{ts}.csv"
        st.success("Data saved. Use the download button below.")

def pump_queue_into_buffers():
    new_rows = []
    while True:
        try:
            row = ss.q_parsed.get_nowait()
        except queue.Empty:
            break
        _, ax, ay, az, gx, gy, gz = row
        t_rel = time.time() - ss.t0 if ss.t0 else 0.0
        full = (t_rel, ax, ay, az, gx, gy, gz)
        ss.data.append(full)
        ss.all_rows.append(full)
        new_rows.append(full)
    return new_rows

# ---------------- UI: controls ----------------
col1, col2, col3 = st.columns([2.6, 1.0, 1.0])
with col1:
    items = list_ports_labels()
    labels = [lbl for (lbl, dev) in items]
    devmap = {lbl: dev for (lbl, dev) in items}
    default_idx = 0
    if ss.selected_port:
        for i,(lbl,dev) in enumerate(items):
            if dev == ss.selected_port:
                default_idx = i; break
    sel = st.selectbox("Select COM port", options=labels, index=default_idx if labels else None,
                       placeholder="Choose the Xiao's COM port")
    ss.selected_port = devmap.get(sel) if sel else None
with col2:
    start_clicked = st.button("Start", type="primary")
with col3:
    stop_clicked = st.button("Stop")

if start_clicked:
    if not ss.selected_port:
        st.warning("Pick a COM port first (and close Arduino Serial Monitor).")
    else:
        start_reader(ss.selected_port)

if stop_clicked:
    stop_reader_and_save()

# Drain any incoming samples
_ = pump_queue_into_buffers()

# ---------------- Two charts only ----------------
if ss.data:
    df = pd.DataFrame(list(ss.data), columns=["t","ax","ay","az","gx","gy","gz"]).set_index("t")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Acceleration (ax, ay, az)")
        st.line_chart(df[["ax","ay","az"]], use_container_width=True)
    with c2:
        st.subheader("Gyroscope (gx, gy, gz)")
        st.line_chart(df[["gx","gy","gz"]], use_container_width=True)
else:
    st.info("Press Start to begin streaming.")

# brief status
if ss.last_error:
    st.caption(f"⚠️ {ss.last_error}")
elif ss.ser_open:
    st.caption(f"Reading from {ss.selected_port} @ {BAUD}")
else:
    st.caption("Idle. Press Start to stream, Stop to save.")

# Stable download button (from bytes in session state)
if ss.download_bytes:
    st.download_button(
        "Download CSV",
        data=ss.download_bytes,
        file_name=ss.download_name,
        mime="text/csv",
        key="download_csv_stable",
    )

# gentle auto-refresh while reading
if ss.ser_open:
    st_autorefresh(interval=REFRESH_MS, key="imu_two_charts_refresh")
