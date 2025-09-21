import serial

ser = serial.Serial("COM5", 9600, timeout=1)  # change COM6 to yours
print("Opened:", ser.name)

for i in range(10):
    line = ser.readline().decode(errors="ignore").strip()
    if line:
        print("Got:", line)

ser.close()
