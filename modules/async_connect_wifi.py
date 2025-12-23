# async_connect_wifi.py

import network, time
import uasyncio as asyncio
import config
    
_wlan = network.WLAN(network.STA_IF) # only create wlan object once

# Try to connect; yield frequently so other tasks stay responsive.
# Returns wlan on success or raises OSError on timeout.
async def async_connect_wifi(ssid, password, timeout_ms=10000, poll_ms=200):
    if not _wlan.active():
        _wlan.active(True)
    
    # If already connected, return immediately
    if _wlan.isconnected():
        if config.debug:
            print('wifi ok')
        return _wlan
    
    # Start connect; returns quickly
    _wlan.connect(ssid, password)
    
    start = time.ticks_ms()
    while not _wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), start) >= timeout_ms:
            raise OSError("WiFi timeout") # Ensure you let caller handle fail bookkeeping
        await asyncio.sleep_ms(poll_ms)
    if config.debug:
        print('wifi ok')
    return _wlan

# Example Usage
if __name__ == "__main__":
    try:
        wlan =  asyncio.run(async_connect_wifi(config.WIFI_SSID, config.WIFI_PASS)) # this is the way to run an async function
        print(wlan.ifconfig())
    except Exception as e:
        print(e)
        