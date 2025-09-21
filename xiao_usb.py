# xiao_usb_live.py
import serial, serial.tools.list_ports
import pandas as pd
from collections import deque
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ---------------- Config ----------------
DEFAULT_BAUD = 115200
MAX_POINTS = 500
REFRESH_MS = 200
CSV_HEADER = ["t","v1","v2"]

st.set_page_config(page_title="XIAO USB Live", layout="wide")
st.title("Live USB data from XIAO nRF52840 Sense")

# ---------------- State ----------------
if "ser" not in st.session_state:
    st.session_state.ser = None
if "buffer" not in st.session_state:
    st.session_state.buffer = ""
if "data" not in st.session_state:
    st.session_state.data = deque(maxlen=MAX_POINTS)

# ---------------- UI ----------------
ports = [p.device for p in serial.tools.list_ports.comports()]
col1, col2, col3 = st.columns([2,1,1])
with col1:
    port = st.selectbox("Port", options=ports, placeholder="Pick COM port")
with col2:
    baud = st.number_input("Baud", value=DEFAULT_BAUD, step=1200)
with col3:
    start = st.toggle("Start", value=False)

status = st.empty()

def try_parse(line):
    try:
        vals = [float(x) for x in line.strip().split(",")]
        if len(vals) == len(CSV_HEADER):
            return tuple(vals)
    except:
        return None
    return None

# ---------------- Connect / Disconnect ----------------
if start and port:
    if st.session_state.ser is None or not st.session_state.ser.is_open:
        try:
            st.session_state.ser = serial.Serial(port, baud, timeout=0.05)
            st.session_state.data.clear()
            st.session_state.buffer = ""
            status.success(f"Connected to {port}")
        except Exception as e:
            status.error(f"Open failed: {e}")
            st.stop()
else:
    if st.session_state.ser:
        try: st.session_state.ser.close()
        except: pass
    st.session_state.ser = None
    status.info("Not connected.")
    st.stop()

# ---------------- Read new data ----------------
ser = st.session_state.ser
if ser and ser.in_waiting:
    chunk = ser.read(ser.in_waiting).decode(errors="ignore")
    st.session_state.buffer += chunk
    if "\n" in st.session_state.buffer:
        lines = st.session_state.buffer.splitlines()
        if not st.session_state.buffer.endswith("\n"):
            st.session_state.buffer = lines.pop()
        else:
            st.session_state.buffer = ""
        for ln in lines:
            parsed = try_parse(ln)
            if parsed:
                st.session_state.data.append(parsed)

# ---------------- Show data live ----------------
if st.session_state.data:
    df = pd.DataFrame(st.session_state.data, columns=CSV_HEADER)
    st.subheader("Live feed (last 10 lines)")
    tail = df.tail(10).to_string(index=False)
    st.code(tail, language="text")

    st.subheader("Live chart")
    st.line_chart(df.set_index("t")[["v1","v2"]])
else:
    st.info("Waiting for data...")

# ---------------- Auto refresh ----------------
st_autorefresh(interval=REFRESH_MS, key="usb_refresh")
