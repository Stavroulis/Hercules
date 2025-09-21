# xiao_usb_live.py — USB Serial → Streamlit (thread-safe, IMU columns, CSV logging)
# Expects newline CSV: t,ax,ay,az,gx,gy,gz  (floats)
# Shows live accel/gyro charts + tail + stats. Threaded reader (no Streamlit calls in thread).

import time
import threading
import queue
from collections import deque
from typing import Deque, Tuple, Optional

import pandas as pd
import streamlit as st
import serial
import serial.tools.list_ports

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

# ---------------- Config ----------------
CSV_HEADER = ["t", "ax", "ay", "az", "gx", "gy", "gz"]  # <— IMU columns
DEFAULT_BAUD = 115200
REFRESH_MS = 200
MAX_POINTS = 5000             # ring buffer for live view
RAW_TAIL_BYTES = 256

st.set_page_config(page_title="XIAO USB Live (IMU + CSV logging)", layout="wide")
st.title("XIAO nRF52840 Sense — USB live IMU stream (with CSV logging)")

# ---------------- Session state ----------------
if "reader_thread" not in st.session_state:
    st.session_state.reader_thread: Optional[threading.Thread] = None
if "stop_event" not in st.session_state:
    st.session_state.stop_event: Optional[threading.Event] = None
if "ser_open" not in st.session_state:
    st.session_state.ser_open = False

# live buffers (main thread owns these)
if "parsed" not in st.session_state:
    st.session_state.parsed: Deque[Tuple[float, ...]] = deque(maxlen=MAX_POINTS)
if "raw_tail" not in st.session_state:
    st.session_state.raw_tail = bytearray()
if "bytes_total" not in st.session_state:
    st.session_state.bytes_total = 0
if "lines_total" not in st.session_state:
    st.session_state.lines_total = 0
if "last_rx_ts" not in st.session_state:
    st.session_state.last_rx_ts = 0.0
if "last_error" not in st.session_state:
    st.session_state.last_error = ""

# queues from thread → main
if "q_parsed" not in st.session_state:
    st.session_state.q_parsed: "queue.Queue[Tuple[float, ...]]" = queue.Queue(maxsize=10000)
if "q_raw" not in st.session_state:
    st.session_state.q_raw: "queue.Queue[bytes]" = queue.Queue(maxsize=10000)

# logging state
if "logging" not in st.session_state:
    st.session_state.logging = False
if "log_rows" not in st.session_state:
    st.session_state.log_rows: list[Tuple[float, ...]] = []

# ---------------- Helpers ----------------
def list_ports():
    items = []
    for p in serial.tools.list_ports.comports():
        items.append((f"{p.device} — {p.description}", p.device))
    return items

def try_parse(line: str) -> Optional[Tuple[float, ...]]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split(",")
    if len(parts) < len(CSV_HEADER):
        return None
    try:
        vals = tuple(float(x) for x in parts[:len(CSV_HEADER)])
        return vals
    except ValueError:
        return None

def reader_thread_fn(port: str, baud: int, stop_event: threading.Event,
                     q_parsed: queue.Queue, q_raw: queue.Queue,
                     err_holder: list, ser_open_flag: list):
    """Background reader — NO Streamlit calls here."""
    ser = None
    try:
        ser = serial.Serial(
            port=port, baudrate=baud,
            timeout=0.5, write_timeout=0.5,
            rtscts=False, dsrdtr=False, xonxoff=False
        )
        # Assert DTR/RTS (helps some Windows CDC drivers begin streaming)
        try:
            ser.dtr = True
            ser.rts = True
        except Exception:
            pass

        ser_open_flag[:] = [True]
        time.sleep(0.2)
        ser.reset_input_buffer()

        buf = bytearray()
        while not stop_event.is_set():
            chunk = ser.read(256)  # blocks up to 0.5s
            if chunk:
                # push raw bytes
                try:
                    q_raw.put_nowait(chunk)
                except queue.Full:
                    pass

                buf += chunk
                # split on CR or LF
                while True:
                    npos = buf.find(b"\n")
                    rpos = buf.find(b"\r")
                    term = -1
                    if npos != -1 and rpos != -1:
                        term = min(npos, rpos)
                    elif npos != -1:
                        term = npos
                    elif rpos != -1:
                        term = rpos
                    if term == -1:
                        break

                    line = buf[:term].decode(errors="ignore")
                    drop = term + 1
                    if drop < len(buf) and buf[drop:drop+1] in (b"\n", b"\r"):
                        drop += 1
                    del buf[:drop]

                    parsed = try_parse(line)
                    if parsed:
                        try:
                            q_parsed.put_nowait(parsed)
                        except queue.Full:
                            pass
    except Exception as e:
        err_holder[:] = [f"{type(e).__name__}: {e}"]
    finally:
        try:
            if ser and ser.is_open:
                ser.close()
        except:
            pass
        ser_open_flag[:] = [False]

def start_reader(port: str, baud: int):
    if st.session_state.reader_thread and st.session_state.reader_thread.is_alive():
        return
    st.session_state.stop_event = threading.Event()
    st.session_state.last_error = ""
    st.session_state.parsed.clear()
    st.session_state.raw_tail = bytearray()
    st.session_state.bytes_total = 0
    st.session_state.lines_total = 0
    st.session_state.last_rx_ts = 0.0
    st.session_state.ser_open = False
    # do not clear log here so you can keep logging across reconnects if desired

    err_holder = [""]
    ser_open_flag = [False]

    th = threading.Thread(
        target=reader_thread_fn,
        name="USB-Serial-Reader",
        args=(port, baud, st.session_state.stop_event,
              st.session_state.q_parsed, st.session_state.q_raw,
              err_holder, ser_open_flag),
        daemon=True
    )
    th.start()
    st.session_state.reader_thread = th

    time.sleep(0.3)
    st.session_state.last_error = err_holder[0]
    st.session_state.ser_open = ser_open_flag[0]

def stop_reader():
    ev = st.session_state.stop_event
    th = st.session_state.reader_thread
    if ev:
        ev.set()
    if th and th.is_alive():
        th.join(timeout=1.5)
    st.session_state.reader_thread = None
    st.session_state.stop_event = None
    st.session_state.ser_open = False

def pump_queues():
    """Drain thread queues into main-thread buffers and (optionally) the log."""
    # raw bytes
    while True:
        try:
            ch = st.session_state.q_raw.get_nowait()
        except queue.Empty:
            break
        st.session_state.bytes_total += len(ch)
        st.session_state.last_rx_ts = time.time()
        st.session_state.raw_tail += ch
        if len(st.session_state.raw_tail) > RAW_TAIL_BYTES:
            st.session_state.raw_tail = st.session_state.raw_tail[-RAW_TAIL_BYTES:]

    # parsed lines
    while True:
        try:
            row = st.session_state.q_parsed.get_nowait()
        except queue.Empty:
            break
        st.session_state.parsed.append(row)
        st.session_state.lines_total += 1
        st.session_state.last_rx_ts = time.time()
        if st.session_state.logging:
            st.session_state.log_rows.append(row)

# ---------------- UI: connection ----------------
ports = list_ports()
labels = [lbl for (lbl, dev) in ports]
devmap = {lbl: dev for (lbl, dev) in ports}

col1, col2, col3, col4 = st.columns([2.6, 1.1, 1.0, 1.6])
with col1:
    label = st.selectbox("Port", options=labels, placeholder="Pick COM port")
    port = devmap.get(label)
with col2:
    baud = st.number_input("Baud", value=DEFAULT_BAUD, step=1200)
with col3:
    start = st.toggle("Start", value=False, help="Open/close background reader")
with col4:
    if st.button("Clear live data"):
        st.session_state.parsed.clear()
        st.session_state.raw_tail = bytearray()
        st.session_state.bytes_total = 0
        st.session_state.lines_total = 0

status = st.empty()

# Logging controls
log_col1, log_col2, log_col3 = st.columns([1.2, 1.2, 2])
with log_col1:
    st.session_state.logging = st.toggle("Logging", value=st.session_state.logging,
                                         help="When ON, incoming parsed rows are appended to the session log.")
with log_col2:
    if st.button("Reset log"):
        st.session_state.log_rows = []

# Connection / data pump
if start and port:
    if not (st.session_state.reader_thread and st.session_state.reader_thread.is_alive()):
        start_reader(port, baud)

    pump_queues()

    if st.session_state.last_error:
        status.error(st.session_state.last_error)
    else:
        since = (time.time() - st.session_state.last_rx_ts) if st.session_state.last_rx_ts else None
        rx = f"(last RX {since:.1f}s ago)" if since is not None else "(waiting for first bytes…)"
        status.success(f"Port {port} @ {baud} — {'open' if st.session_state.ser_open else 'opening…'} {rx}")
else:
    stop_reader()
    status.info("Not connected.")
    st.stop()

# ---------------- Live views ----------------
left, right = st.columns([1, 2])

with left:
    st.subheader("Stats")
    st.metric("Total raw bytes", st.session_state.bytes_total)
    st.metric("Total parsed lines", st.session_state.lines_total)
    if st.session_state.last_rx_ts:
        st.caption(f"Last RX: {time.strftime('%H:%M:%S', time.localtime(st.session_state.last_rx_ts))}")

    st.subheader("Raw bytes tail")
    if st.session_state.raw_tail:
        tail = bytes(st.session_state.raw_tail)
        hexstr = " ".join(f"{b:02X}" for b in tail)
        txtstr = "".join(chr(b) if 32 <= b <= 126 else "." for b in tail)
        st.code(hexstr, language="text")
        st.text(txtstr)
    else:
        st.caption("No raw bytes yet.")

    st.subheader("Download log")
    if st.session_state.log_rows:
        df_log = pd.DataFrame(st.session_state.log_rows, columns=CSV_HEADER)
        st.download_button(
            "Download CSV",
            data=df_log.to_csv(index=False).encode(),
            file_name=f"xiao_imu_log_{int(time.time())}.csv",
            mime="text/csv",
        )
    else:
        st.caption("No logged rows yet. Toggle 'Logging' to start.")

with right:
    if st.session_state.parsed:
        df = pd.DataFrame(st.session_state.parsed, columns=CSV_HEADER)
        # Two charts: accel and gyro (each uses 't' as index)
        st.subheader("Accelerometer (g or m/s²)")
        st.line_chart(df.set_index("t")[["ax", "ay", "az"]])

        st.subheader("Gyroscope (deg/s or rad/s)")
        st.line_chart(df.set_index("t")[["gx", "gy", "gz"]])

        st.subheader("Tail (last 10 parsed)")
        st.code(df.tail(10).to_string(index=False), language="text")
    else:
        st.info("Parsed charts will appear when valid CSV lines arrive.")

# ---------------- Auto refresh ----------------
if st_autorefresh:
    st_autorefresh(interval=REFRESH_MS, key="usb_refresh")
