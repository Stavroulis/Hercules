# app.py — XIAO nRF52840 Sense → Streamlit via BLE (Nordic UART Service)
# - Robust on Windows: runs its own asyncio loop in a background thread
# - Two ways to connect:
#     1) Quick connect by advertised name ("XIAO-Sense-BLE")
#     2) Scan list → select device → Connect
# - Expects CSV lines "t,v1,v2" from the board (one line per sample)

import asyncio
import threading
from collections import deque
from typing import Deque, Tuple, Optional

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from bleak import BleakScanner, BleakClient

# ---------------- Config ----------------
TARGET_NAME = "XIAO-Sense-BLE"  # must match Bluefruit setName() in your Arduino sketch
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify
NUS_RX_CHAR      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write (optional)

MAX_POINTS = 600        # ~60 s at 10 Hz
REFRESH_MS = 200        # UI refresh cadence

st.set_page_config(page_title="XIAO Sense BLE → Streamlit", layout="wide")
st.title("XIAO nRF52840 Sense → Streamlit via BLE (Nordic UART)")

# ---------------- Session state ----------------
if "loop" not in st.session_state:
    st.session_state.loop: Optional[asyncio.AbstractEventLoop] = None
if "loop_thread" not in st.session_state:
    st.session_state.loop_thread: Optional[threading.Thread] = None
if "devices" not in st.session_state:
    st.session_state.devices = []
if "selected_addr" not in st.session_state:
    st.session_state.selected_addr: Optional[str] = None
if "client" not in st.session_state:
    st.session_state.client: Optional[BleakClient] = None
if "connected" not in st.session_state:
    st.session_state.connected = False
if "queue" not in st.session_state:
    st.session_state.queue: Deque[Tuple[float, float, float]] = deque(maxlen=MAX_POINTS)
if "buffer" not in st.session_state:
    st.session_state.buffer = ""
if "listen_future" not in st.session_state:
    st.session_state.listen_future = None

# ---------------- Utilities ----------------
def ensure_loop_thread():
    """Create an asyncio loop running forever in a daemon thread (once)."""
    if st.session_state.loop and st.session_state.loop_thread and st.session_state.loop_thread.is_alive():
        return
    loop = asyncio.new_event_loop()
    st.session_state.loop = loop

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=run_loop, name="BLE-Loop-Thread", daemon=True)
    t.start()
    st.session_state.loop_thread = t

def run_coro(coro):
    """Schedule coroutine onto our background loop and return a concurrent.futures.Future."""
    ensure_loop_thread()
    return asyncio.run_coroutine_threadsafe(coro, st.session_state.loop)

def parse_line(line: str) -> Optional[Tuple[float, float, float]]:
    parts = line.strip().split(",")
    if len(parts) != 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None

# ---------------- BLE coroutines ----------------
async def scan_for_devices() -> list:
    """Return list of discovered BLE devices."""
    devs = await BleakScanner.discover()
    return devs

async def find_device_by_name(name: str, timeout: float = 12.0):
    """Find a device by advertised local name (more reliable on Windows)."""
    def _flt(d, ad):
        local = (ad and getattr(ad, "local_name", None)) or ""
        return (d.name == name) or (local == name)
    try:
        dev = await BleakScanner.find_device_by_filter(_flt, timeout=timeout)
        return dev
    except Exception:
        return None

async def connect_and_listen(address: str):
    """Connect to device and stream notifications from NUS TX characteristic."""
    # Clean previous client
    try:
        if st.session_state.client:
            await st.session_state.client.disconnect()
    except:
        pass

    client = BleakClient(address, timeout=10.0)
    await client.connect()
    st.session_state.client = client
    st.session_state.connected = await client.is_connected()

    def handle_notify(_, data: bytearray):
        # Called on BLE thread; keep it tiny & thread-safe (touch only session_state)
        try:
            text = data.decode(errors="ignore")
        except:
            return
        st.session_state.buffer += text
        if "\n" in st.session_state.buffer:
            lines = st.session_state.buffer.splitlines(keepends=False)
            if not st.session_state.buffer.endswith("\n"):
                st.session_state.buffer = lines.pop()
            else:
                st.session_state.buffer = ""
            for ln in lines:
                parsed = parse_line(ln)
                if parsed:
                    st.session_state.queue.append(parsed)

    await client.start_notify(NUS_TX_CHAR, handle_notify)

    try:
        # Keep alive until disconnected
        while await client.is_connected():
            await asyncio.sleep(0.2)
    finally:
        try:
            await client.stop_notify(NUS_TX_CHAR)
        except:
            pass
        try:
            await client.disconnect()
        except:
            pass
        st.session_state.connected = False
        st.session_state.client = None

async def disconnect_ble():
    if st.session_state.client:
        try:
            await st.session_state.client.disconnect()
        except:
            pass
    st.session_state.connected = False
    st.session_state.client = None

# ---------------- UI ----------------
col1, col2, col3 = st.columns([2, 2, 1])

with col1:
    # Quick connect by name (recommended on Windows; no OS pairing needed)
    quick = st.button(f"Quick connect to '{TARGET_NAME}'")

    # Optional: list scan
    do_scan = st.button("Scan BLE devices (show list)")
    options = []
    if do_scan:
        fut = run_coro(scan_for_devices())
        try:
            devs = fut.result(timeout=12)
        except Exception as e:
            st.error(f"Scan failed: {e}")
            devs = []
        st.session_state.devices = devs

    for d in st.session_state.devices:
        name = d.name or "Unknown"
        label = f"{name} [{d.address}]"
        options.append(label)

    choice = st.selectbox(
        "Pick device from scan (optional)",
        options=options,
        index=0 if options else None,
        placeholder="Click 'Scan BLE devices (show list)' to populate",
    )
    if choice:
        st.session_state.selected_addr = choice.split("[")[-1].rstrip("]").strip()

with col2:
    connect_sel = st.button("Connect (use selection)")
    disconnect_clicked = st.button("Disconnect")

with col3:
    st.metric("Status", "Connected" if st.session_state.connected else "Disconnected")

# --- Connection logic ---
if quick and not st.session_state.connected:
    st.session_state.queue.clear()
    st.session_state.buffer = ""
    dev = run_coro(find_device_by_name(TARGET_NAME)).result(timeout=14)
    if dev is None:
        st.error(f"'{TARGET_NAME}' not found. Ensure the board is advertising and not already connected.")
    else:
        st.info(f"Found {dev.name or 'Unknown'} [{dev.address}] — connecting…")
        st.session_state.listen_future = run_coro(connect_and_listen(dev.address))

if connect_sel and st.session_state.selected_addr and not st.session_state.connected:
    st.session_state.queue.clear()
    st.session_state.buffer = ""
    try:
        st.session_state.listen_future = run_coro(connect_and_listen(st.session_state.selected_addr))
    except Exception as e:
        st.error(f"Connect failed: {e}")

if disconnect_clicked and st.session_state.connected:
    try:
        run_coro(disconnect_ble()).result(timeout=5)
    except Exception as e:
        st.warning(f"Disconnect issue: {e}")

# --- Live view ---
raw_placeholder = st.empty()
chart_placeholder = st.empty()

if st.session_state.queue:
    tail = list(st.session_state.queue)[-10:]
    raw_lines = [f"{t:.3f},{v1:.3f},{v2:.3f}" for (t, v1, v2) in tail]
    raw_placeholder.code("\n".join(raw_lines), language="text")

    df = pd.DataFrame(st.session_state.queue, columns=["t", "v1", "v2"])
    chart_placeholder.line_chart(df.set_index("t")[["v1", "v2"]])
else:
    raw_placeholder.info("Waiting for BLE data… (connect your XIAO and ensure it sends lines)")

# Smooth periodic UI refresh (no st.autorefresh in core Streamlit)
st_autorefresh(interval=REFRESH_MS, key="ble_autorefresh")

# ---------------- Help ----------------
with st.expander("Troubleshooting & Tips"):
    st.markdown("""
- **No pairing needed** in Windows Bluetooth settings for Nordic UART. If already paired and scan is flaky, **remove device** in Settings and try again.
- Make sure your Arduino sketch advertises as **XIAO-Sense-BLE** and sends one **newline-terminated CSV** line per sample.
- If the XIAO LED is **solid ON**, it’s already connected to something (e.g., phone / nRF Connect). Disconnect other centrals and try again.
- Windows sometimes requires **Location ON** to allow BLE scanning: Settings → Privacy & security → Location.
- To send commands back (optional), write to **NUS_RX_CHAR** from another button/callback:
  ```python
  # Example:
  # await st.session_state.client.write_gatt_char(NUS_RX_CHAR, b"rate=20\\n")
""")