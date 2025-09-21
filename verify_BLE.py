# find_xiao_nus_compat.py
import asyncio
from bleak import BleakClient, BleakScanner

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"  # Nordic UART Service UUID (lowercase)

async def main():
    print("Scanning ~7s… (close nRF Connect on your phone; keep board near PC)")
    seen = {}  # addr -> {"name": str|None, "rssi": int}

    def cb(device, adv_data):
        addr = device.address
        entry = seen.get(addr, {"name": None, "rssi": -999})
        name = device.name or getattr(adv_data, "local_name", None) or entry["name"]
        rssi = getattr(adv_data, "rssi", None)
        if rssi is None: rssi = entry["rssi"]
        seen[addr] = {"name": name, "rssi": rssi}

    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(7.0)
    await scanner.stop()

    if not seen:
        print("No advertisers seen. Move the board closer and try again.")
        return

    # Try strongest first
    addrs = sorted(seen.items(), key=lambda kv: kv[1]["rssi"], reverse=True)
    addrs = [addr for addr, _ in addrs[:12]]

    for addr in addrs:
        meta = seen[addr]
        print(f"\nTrying {addr} (name={meta['name']!r}, rssi={meta['rssi']}) …")
        try:
            async with BleakClient(addr, timeout=8.0) as client:
                if not client.is_connected:
                    print("  - connect failed")
                    continue

                # Get services (new Bleak) or fall back to .services (old Bleak)
                services = None
                try:
                    services = await client.get_services()  # new Bleak
                except AttributeError:
                    services = client.services            # old Bleak
                    if services is None:
                        # Some very old versions need a small wait
                        await asyncio.sleep(0.5)
                        services = client.services

                if not services:
                    print("  - could not read services")
                    continue

                uuids = {s.uuid.lower() for s in services}
                if NUS_SERVICE in uuids:
                    print("  ✅ Found Nordic UART Service on", addr)
                    # Optional: try to read GAP device name (2A00)
                    GAP_DEVICE_NAME = "00002a00-0000-1000-8000-00805f9b34fb"
                    try:
                        for s in services:
                            for ch in s.characteristics:
                                if ch.uuid.lower() == GAP_DEVICE_NAME and "read" in ch.properties:
                                    nm = await client.read_gatt_char(ch.uuid)
                                    print("  Device Name:", nm.decode("utf-8", "ignore"))
                                    break
                    except Exception:
                        pass
                    return
                else:
                    print("  - NUS not present")
        except Exception as e:
            print("  - error:", e)

    print("\nNo device with NUS found.")
    print("Tips: keep the board very close; toggle Windows Bluetooth; or try a USB BLE 5.0 dongle.")

if __name__ == "__main__":
    asyncio.run(main())
