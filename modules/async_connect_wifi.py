# async_connect_wifi.py

import network, time
import uasyncio as asyncio
import config
    
async def async_connect_wifi(ssid, password, timeout=10, poll=0.2):
    """
    Try to connect; yield frequently so other tasks stay responsive.
    Returns wlan on success or raises OSError on timeout.
    """
    
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)
    
    # If already connected, return immediately
    if wlan.isconnected():
        if config.debug:
            print('wifi connected')
        return wlan
    
    # Start connect; returns quickly
    wlan.connect(ssid, password)
    
    start = time.time()
    while not wlan.isconnected():
        if time.time() - start >= timeout:
            raise OSError("WiFi connect timeout") # Ensure you let caller handle fail bookkeeping
        await asyncio.sleep(poll)
    return wlan

# Example Usage
if __name__ == "__main__":
    try:
        wlan =  asyncio.run(async_connect_wifi(config.WIFI_SSID, config.WIFI_PASS)) # this is the way to run an async function
        print(wlan.ifconfig())
    except Exception as e:
        print(e)
        