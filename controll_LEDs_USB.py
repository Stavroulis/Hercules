# app.py — Streamlit LED controller for XIAO nRF52840 Sense over USB
# Requirements: streamlit, pyserial
#   pip install streamlit pyserial

import time
import streamlit as st
import serial
import serial.tools.list_ports as list_ports

st.set_page_config(page_title="XIAO LED Controller", page_icon="🔦", layout="centered")
st.title("XIAO nRF52840 Sense — LED Controller (USB)")
st.caption("Click a button to pulse an LED on D7 / D8 / D9 / D10 for 1 second.")

# ---------- Helpers ----------
def find_ports():
    return [p.device for p in list_ports.comports()]

def try_ping(port: str, baud: int = 115200, timeout: float = 1.0) -> bool:
    """Open port briefly and check for READY/PONG response."""
    try:
        with serial.Serial(port, baudrate=baud, timeout=timeout) as ser:
            time.sleep(0.2)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(b"PING\n")
            line = ser.readline().decode(errors="ignore").strip()
            # Accept "PONG" (reply to PING) or the initial "READY" banner
            return ("PONG" in line) or ("READY" in line)
    except Exception:
        return False

def send_cmd(port: str, cmd: str, baud: int = 115200, timeout: float = 2.0) -> str:
    """Send a single-line command ('7','8','9','10') and read one short reply."""
    try:
        with serial.Serial(port, baudrate=baud, timeout=timeout) as ser:
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write((cmd + "\n").encode())
            t_end = time.time() + 1.2
            reply = ""
            while time.time() < t_end:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    reply = line
                    break
            return reply or "(no reply)"
    except Exception as e:
        return f"(error: {e})"

# ---------- Port picker ----------
ports = find_ports()
if "xiao_port" not in st.session_state:
    st.session_state.xiao_port = ports[0] if ports else ""

colA, colB = st.columns([3, 1])
with colA:
    current = st.session_state.xiao_port
    options = [""] + ports
    index = options.index(current) if current in options else 0
    selected = st.selectbox(
        "Serial port",
        options=options,
        index=index,
        help="Pick the COM port for your XIAO (e.g., COM3/COM5 on Windows).",
    )
with colB:
    if st.button("Refresh"):
        st.rerun()

if selected:
    st.session_state.xiao_port = selected

# ---------- Connection test (corrected display) ----------
status = st.empty()
if st.session_state.xiao_port:
    ok = try_ping(st.session_state.xiao_port)
    if ok:
        status.success(f"Connected to {st.session_state.xiao_port}")
    else:
        status.warning(
            f"Tried {st.session_state.xiao_port} but no response. "
            f"Make sure Arduino Serial Monitor/Plotter is CLOSED."
        )
else:
    ok = False
    status.info("Select a serial port above.")

st.divider()

# ---------- Buttons ----------
col1, col2 = st.columns(2)
with col1:
    if st.button("Pulse D7"):
        if ok:
            st.write(send_cmd(st.session_state.xiao_port, "7"))
        else:
            st.error("Not connected.")
    if st.button("Pulse D9"):
        if ok:
            st.write(send_cmd(st.session_state.xiao_port, "9"))
        else:
            st.error("Not connected.")

with col2:
    if st.button("Pulse D8"):
        if ok:
            st.write(send_cmd(st.session_state.xiao_port, "8"))
        else:
            st.error("Not connected.")
    if st.button("Pulse D10"):
        if ok:
            st.write(send_cmd(st.session_state.xiao_port, "10"))
        else:
            st.error("Not connected.")

st.caption(
    "Tip: If buttons do nothing, close Arduino Serial Monitor/Plotter, replug USB, "
    "choose the correct COM port, and try again."
)


"""The following is the respective arduino Code"""


"""
    // XIAO nRF52840 Sense — LED pulse over USB Serial
// D7: , D8: , D9: , D10: // Commands (ending with \n): "7", "8", "9", "10"
// Optional: "PING" -> replies "PONG"

void pulsePin(uint8_t pin, unsigned long ms=1000) {
  digitalWrite(pin, HIGH);
  delay(ms);
  digitalWrite(pin, LOW);
}

void setup() {
  pinMode(D7, OUTPUT);
  pinMode(D8, OUTPUT);
  pinMode(D9, OUTPUT);
  pinMode(D10, OUTPUT);
  digitalWrite(D7, LOW);
  digitalWrite(D8, LOW);
  digitalWrite(D9, LOW);
  digitalWrite(D10, LOW);

  Serial.begin(115200);
  while (!Serial) { ; }  // wait for USB
  Serial.println("READY");
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd.equalsIgnoreCase("PING")) {
      Serial.println("PONG");
      return;
    }

    if (cmd == "7")      { pulsePin(D7);  Serial.println("OK D7");  }
    else if (cmd == "8") { pulsePin(D8);  Serial.println("OK D8");  }
    else if (cmd == "9") { pulsePin(D9);  Serial.println("OK D9");  }
    else if (cmd == "10"){ pulsePin(D10); Serial.println("OK D10"); }
    else {
      Serial.println("ERR");
    }
  }
}
"""

