# app_ble_fast.py — Streamlit BLE LED controller (persistent connection)
# pip install -U streamlit bleak

import sys, time, threading, asyncio
import streamlit as st
from bleak import BleakScanner, BleakClient

# Windows policy helps with bleak
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

st.set_page_config(page_title="XIAO LED Controller (BLE, fast)", page_icon="⚡", layout="centered")
st.title("XIAO nRF52840 Sense — LED Controller (BLE, fast)")
st.caption("Persistent BLE connection for low-latency control of D7/D8/D9/D10.")

SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
TX_UUID      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notify
RX_UUID      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write
TARGET_NAME  = "XIAO-LED"

# ---------- session priming BEFORE widgets ----------
# If Scan found an address on the previous run, populate the text input now.
if "pending_addr" in st.session_state:
    st.session_state["ble_addr_name"] = st.session_state.pop("pending_addr")

# ---------- Background BLE manager ----------
class BLEManager:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.client: BleakClient | None = None
        self.addr: str | None = None
        self.last_reply: str = ""
        self.connected: bool = False

    def stop(self):
        if self.client:
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop).result(timeout=3)
            except Exception:
                pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

    def _notify(self, _h, data: bytearray):
        try:
            self.last_reply = data.decode(errors="ignore").strip()
        except Exception:
            self.last_reply = repr(data)

    async def _connect(self, addr: str):
        self.addr = addr
        self.client = BleakClient(addr)
        await self.client.connect()
        if not self.client.is_connected:  # property (no parentheses)
            raise RuntimeError("BLE not connected")
        await self.client.start_notify(TX_UUID, self._notify)
        self.connected = True

    async def _disconnect(self):
        try:
            if self.client and self.client.is_connected:
                try:
                    await self.client.stop_notify(TX_UUID)
                except Exception:
                    pass
                await self.client.disconnect()
        finally:
            self.connected = False
            self.client = None

    def connect(self, addr: str):
        return asyncio.run_coroutine_threadsafe(self._connect(addr), self.loop).result(timeout=8)

    def disconnect(self):
        return asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop).result(timeout=5)

    def send(self, text: str):
        """Fire-and-forget write; returns quickly."""
        if not (self.client and self.client.is_connected):
            raise RuntimeError("Not connected")
        self.last_reply = ""
        coro = self.client.write_gatt_char(RX_UUID, (text + "\n").encode(), response=False)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=3)

    def send_and_wait(self, text: str, timeout_s: float = 2.0) -> str:
        """Send and wait (briefly) for a notify reply."""
        self.send(text)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout_s:
            if self.last_reply:
                return self.last_reply
            time.sleep(0.02)
        return "(no reply)"

# session-scoped BLE manager
if "ble" not in st.session_state:
    st.session_state.ble = BLEManager()

# ---------- helpers ----------
def looks_like_address(s: str) -> bool:
    s = (s or "").strip()
    return (":" in s) or (s.count("-") >= 5) or (len(s) >= 12 and all(c in "0123456789ABCDEF:-" for c in s.upper()))

async def scan_for_name(name: str, timeout=5.0):
    devs = await BleakScanner.discover(timeout=timeout)
    for d in devs:
        if (d.name or "").strip() == name:
            return d
    # loose
    lname = name.lower()
    for d in devs:
        if d.name and lname in d.name.lower():
            return d
    return None

async def resolve_addr(addr_or_name: str) -> str:
    s = (addr_or_name or "").strip()
    if not s:
        return ""
    if looks_like_address(s):
        return s
    dev = await scan_for_name(s, timeout=6.0)
    return dev.address if dev else ""

# ---------- UI ----------
colA, colB, colC = st.columns([3,1,1])
with colA:
    inp = st.text_input("BLE address or name",
                        key="ble_addr_name",
                        placeholder="e.g. F8:0D:39:39:4D:3C or XIAO-LED")
with colB:
    if st.button("Scan"):
        try:
            dev = asyncio.run(scan_for_name(TARGET_NAME, timeout=6.0))
            if dev:
                # Stage into pending and rerun; the priming block at the top will populate the widget next run.
                st.session_state["pending_addr"] = dev.address
                st.success(f"Found {dev.name or '(no name)'} @ {dev.address}")
                st.rerun()
            else:
                st.warning("Not found. Ensure the board is advertising.")
        except Exception as e:
            st.error(f"Scan error: {e}")
with colC:
    if st.button("Disconnect"):
        try:
            st.session_state.ble.disconnect()
            st.success("Disconnected.")
        except Exception as e:
            st.warning(f"Disconnect: {e}")

# Connect if not connected and we have an address/name
ble: BLEManager = st.session_state.ble
if not ble.connected and st.session_state.get("ble_addr_name", "").strip():
    try:
        addr = asyncio.run(resolve_addr(st.session_state["ble_addr_name"]))
        if addr:
            ble.connect(addr)
            st.success(f"Connected to {addr}")
        else:
            st.info("Enter a BLE MAC or click Scan.")
    except Exception as e:
        st.warning(f"Connect error: {e}")

st.divider()
st.write(("Status: **connected**" if ble.connected else "Status: **not connected**"))

col1, col2 = st.columns(2)
with col1:
    if st.button("Pulse D7"):
        try:
            st.write(ble.send_and_wait("7", timeout_s=0.5))
        except Exception as e:
            st.error(e)
    if st.button("Pulse D9"):
        try:
            st.write(ble.send_and_wait("9", timeout_s=0.5))
        except Exception as e:
            st.error(e)
with col2:
    if st.button("Pulse D8"):
        try:
            st.write(ble.send_and_wait("8", timeout_s=0.5))
        except Exception as e:
            st.error(e)
    if st.button("Pulse D10"):
        try:
            st.write(ble.send_and_wait("10", timeout_s=0.5))
        except Exception as e:
            st.error(e)

st.caption("Tip: keep this app connected; each click just writes a short packet (no reconnect). If you change boards, click Disconnect and Scan/Connect again.")


_ARDUINO_SKETCH = r"""The respective code for Arduino

// XIAO nRF52840 Sense — BLE LED Controller (Nordic UART style)
// Service UUID: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
//   TX notify: 6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (MCU -> Host)
//   RX write : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (Host -> MCU)
// Commands: "7","8","9","10","PING" (newline optional)

#include <ArduinoBLE.h>

#define BLE_DEVICE_NAME "XIAO-LED"
#define PIN_D7 D7
#define PIN_D8 D8
#define PIN_D9 D9
#define PIN_D10 D10

static const char* SVC_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* TX_UUID  = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"; // notify
static const char* RX_UUID  = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"; // write

BLEService uartService(SVC_UUID);
BLECharacteristic txChar(TX_UUID, BLERead | BLENotify, 64);
BLECharacteristic rxChar(RX_UUID, BLEWriteWithoutResponse | BLEWrite, 64);

static inline void pulsePin(uint8_t pin, unsigned long ms=1000) {
  digitalWrite(pin, HIGH);
  delay(ms);
  digitalWrite(pin, LOW);
}

void setup() {
  pinMode(PIN_D7, OUTPUT);
  pinMode(PIN_D8, OUTPUT);
  pinMode(PIN_D9, OUTPUT);
  pinMode(PIN_D10, OUTPUT);
  digitalWrite(PIN_D7, LOW);
  digitalWrite(PIN_D8, LOW);
  digitalWrite(PIN_D9, LOW);
  digitalWrite(PIN_D10, LOW);

  if (!BLE.begin()) {
    // Blink fast on failure
    pinMode(LED_BUILTIN, OUTPUT);
    while (true) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(100); }
  }

  BLE.setLocalName(BLE_DEVICE_NAME);
  BLE.setDeviceName(BLE_DEVICE_NAME);
  BLE.setAdvertisedService(uartService);
  uartService.addCharacteristic(txChar);
  uartService.addCharacteristic(rxChar);
  BLE.addService(uartService);

  txChar.writeValue((const uint8_t*)"READY", 5);
  BLE.advertise();
}

void loop() {
  BLEDevice central = BLE.central();
  if (!central) return;

  // Connected: keep handling writes
  while (central.connected()) {
    if (rxChar.written()) {
      uint8_t buf[64];
      int len = rxChar.valueLength();
      if (len > 64) len = 64;
      rxChar.readValue(buf, len);

      // Build trimmed string up to first CR/LF
      String cmd;
      for (int i = 0; i < len; ++i) {
        char c = (char)buf[i];
        if (c == '\n' || c == '\r') break;
        cmd += c;
      }
      cmd.trim();

      if (cmd.equalsIgnoreCase("PING")) {
        txChar.writeValue((const uint8_t*)"PONG", 4);
      } else if (cmd == "7") {
        pulsePin(PIN_D7);
        txChar.writeValue((const uint8_t*)"OK D7", 5);
      } else if (cmd == "8") {
        pulsePin(PIN_D8);
        txChar.writeValue((const uint8_t*)"OK D8", 5);
      } else if (cmd == "9") {
        pulsePin(PIN_D9);
        txChar.writeValue((const uint8_t*)"OK D9", 5);
      } else if (cmd == "10") {
        pulsePin(PIN_D10);
        txChar.writeValue((const uint8_t*)"OK D10", 6);
      } else {
        txChar.writeValue((const uint8_t*)"ERR", 3);
      }
    }
    delay(1);
  }

  // Disconnected → advertise again
  BLE.advertise();
}
"""