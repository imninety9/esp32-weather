# main.py  -- async weather + air quality logger with LCD health screen

# IMPROVEMENTS: 1. If you want to send failed data to mqtt later when reconnection happens,
#				then add a flag mqtt_sent = True/False at the end of the row in sd logging
#				and add another separate async mqtt publish task that runs every few seconds
#				and only publishes data rows or any last rows that have mqtt_sent flag set as False


import gc
import time
import uasyncio as asyncio

from machine import Pin, I2C, SoftI2C, SPI, reset

import config
from async_connect_wifi import async_connect_wifi
from sensors_handler import Sensors
from ds3231rtc import ds3231
from sd_logger import SDLogger
from mqtt_client import MQTTClientSimple
from owm import OWMClient
from pcd8544_fb import PCD8544_FB


# --------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------

class State:
    __slots__ = (
        "boot_ts",
        "sensor_row",
        "owm", "apm",
        "last_sensor_ts", "last_owm_ts", "last_apm_ts",
        "wifi_ok", "mqtt_ok", "wlan", "mqtt_client",
        "apm_running",
        "heartbeat"
    )
    
    def __init__(self):
        # start timestamp
        self.boot_ts = time.time()
        
        # Last full row logged / ready to log (list of values)
        self.sensor_row = None

        # Latest external sources
        self.owm = [None, None, None, None]   # temp, feels_like, hum, press
        self.apm = [None, None, None]         # pm1, pm2_5, pm10

        # Timestamps
        self.last_sensor_ts = None
        self.last_owm_ts = None
        self.last_apm_ts = None

        # Network
        self.wifi_ok = False
        self.mqtt_ok = False
        self.wlan = None
        self.mqtt_client = None
        
        # APM sensor
        self.apm_running = False
        
        # heartbeat
        self.heartbeat = None


state = State()


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def rtc_tup(rtc=None):
        """Return (Y, M, D, H, M, S, ...) tuple using rtc.get_time() if available."""
        try:
            if rtc:
                t = rtc.get_time()
                if t:
                    return t
        except Exception:
            pass
        # fallback
        return time.localtime()
    
def tup_to_iso(t):  
    """Convert time tuple (Y, M, D, H, M, S, ...) to 'YYYY-MM-DD hh:mm:ss'."""
    if not t:
        return "0000-00-00 00:00:00"
    return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])


def format_value(value, precision=2):
    """Format values for CSV / MQTT."""
    if value is None:
        return ""
    
    t = type(value)
    
    if t is int:
        return str(value)
    if t is float:
        return ("%." + str(precision) + "f") % value
    # Fallback
    return str(value)


def format_uptime(boot_ts):
    """
    uptime of project
    """
    s = int(time.time() - boot_ts)
    days, s = divmod(s, 86400)
    hrs, s = divmod(s, 3600)
    mins, s = divmod(s, 60)
    if days:
        return "%dd%02dh" % (days, hrs)
    return "%02d:%02d:%02d" % (hrs, mins, s)


def make_row(ts, sensor_data, owm_data, apm_data):
    """
    Build one CSV row matching config.csv_fields:
    ["timestamp","bmp_temp","bmp_press","aht_temp","aht_hum",
     "ds18b20_temp","sht_temp","sht_hum",
     "pm1_0","pm2_5","pm10",
     "owm_temp","owm_temp_feels_like","owm_hum","owm_press"]
    """
    
    return [
        tup_to_iso(ts),
        format_value(sensor_data[0][0]),
        format_value(sensor_data[0][1]),
        format_value(sensor_data[1][0]),
        format_value(sensor_data[1][1]),
        format_value(sensor_data[2]),
        format_value(sensor_data[3][0]),
        format_value(sensor_data[3][1]),
        format_value(apm_data[0]),
        format_value(apm_data[1]),
        format_value(apm_data[2]),
        format_value(owm_data[0]),
        format_value(owm_data[1]),
        format_value(owm_data[2]),
        format_value(owm_data[3])
    ]


def compute_backoff(base, fail_count, max_backoff, jitter_pct=0.15):
    """
    Exponential backoff with jitter. fail_count=0 => no backoff (we'll still wait base once).
    jitter_pct = fraction (0.15 => ±15%)
    """
    if fail_count <= 0:
        backoff = base
    else:
        backoff = min(base * (2 ** (fail_count - 1)), max_backoff)
    # apply jitter ±jitter_pct using a pseudo-random number from ticks
    tms = time.ticks_ms() & 0xFFFF  # 0..65535
    frac = (tms / 65535.0)  # 0.0 .. 1.0
    # scale to -j..+j
    jitter = (frac * 2.0 - 1.0) * jitter_pct * backoff
    #jitter = backoff * random.uniform(-jitter_pct, jitter_pct)
    wait = max(1, backoff + jitter)
    return int(wait)


# --------------------------------------------------------------------
# Async tasks
# --------------------------------------------------------------------

async def sensor_and_log_task(rtc, state, sensors, sd):
    """Read sensors every SENSOR_INTERVAL_SECS, build row, log to SD and publish MQTT."""
    interval = config.SENSOR_INTERVAL_SECS

    mqtt_feeds = config.feeds
    mqtt_msgs = [None]*9  # pre-allocating once at module top to avoid re-allocating new list each time

    while True:
        try:
            gc.collect()

            # 1. Read all local sensors (blocking but quick)
            sensor_data = sensors.read_measurements()

            # 2. Timestamp from RTC (DS3231)
            ts = rtc_tup(rtc)
            state.last_sensor_ts = ts

            # 3. Build row (uses last known OWM + APM data)
            row = make_row(ts, sensor_data, state.owm, state.apm)

            # 4. Append to SD
            sd.append_row(row, ts=ts)
            # Save latest row in state (for display or MQTT)
            state.sensor_row = row
            
            # 5. Try MQTT publish
            if state.mqtt_ok and state.mqtt_client:
                try:
                    mqtt_msgs[0] = row[1]   # bmp_temp
                    mqtt_msgs[1] = row[2]   # bmp_press
                    mqtt_msgs[2] = row[3]  # aht_temp
                    mqtt_msgs[3] = row[4]  # aht_hum
                    mqtt_msgs[4] = "%s %s" % (row[5], format_uptime(state.boot_ts)) # ds18b20_temp
                    mqtt_msgs[5] = row[11]  # owm_temp
                    mqtt_msgs[6] = row[12]  # owm_feels_like
                    mqtt_msgs[7] = row[13]  # owm_hum
                    mqtt_msgs[8] = row[14]  # owm_press
                    
                    state.mqtt_client.publish_data(mqtt_feeds, mqtt_msgs)
                except Exception as e:
                    # Mark MQTT as broken so wifi_mqtt_task will reconnect later
                    state.mqtt_ok = False
                    state.mqtt_client = None
                    sd.append_error("mqtt_publish_fail", e, ts=rtc_tup(rtc))
            
            state.heartbeat = time.time() # <<-- Sensor log task success heartbeat
            
        except Exception as e:
            sd.append_error("sensor_and_log_task_error", e, ts=rtc_tup(rtc), min_interval=300)
        
        # Sleep until next sample
        '''
        NOTE: The asyncio.sleep(interval) in the finally style placement (after except) means
        the loop won’t spin like crazy if something fails.
        '''
        await asyncio.sleep(interval)


async def owm_task(rtc, state, owm_client, sd):
    """Fetch OpenWeatherMap data every OWM_FETCH_INTERVAL_SECS when Wi-Fi is OK."""
    interval = config.OWM_FETCH_INTERVAL_SECS
    
    while True:
        if state.apm_running:
            # Wait a short time and try again later. we use this tactic because this task is "blocking" and we want apm sensor to take regular measurements
            await asyncio.sleep(10)
            continue
        
        # else go fetch OWM (with a short timeout)
        try:
            if state.wifi_ok:
                # blocking but with a timeout of 10 s
                owm_data = owm_client.fetch()  # [temp, feels_like, hum, press]
                state.owm = owm_data
                state.last_owm_ts = rtc_tup(rtc)
                state.heartbeat = time.time()   # <<-- OWM task success heartbeat
        except Exception as e:
            sd.append_error("owm_task_error", e, ts=rtc_tup(rtc))
                
        await asyncio.sleep(interval)


async def apm10_task(rtc, state, apm_sensor, sd):
    """
    Run APM10 once per hour, average last ~10 readings.
    Uses await sleeps between samples so other tasks can run.
    """
    interval = config.APM10_INTERVAL_SECS  # seconds between PM updates
    
    # If the sensor isn't present, exit the task cleanly without looping it
    if apm_sensor is None:
        if config.debug:
            print("APM10: sensor not available, apm10_task exiting")
        return
    
    while True:
        # Mark APM as running for UI
        state.apm_running = True
        
        try:
            # Enter measurement mode
            apm_sensor.enter_measurement_mode()
            samples = []

            # Warm-up and Take ~25 samples, keep last 10; sensor min interval ~1s
            for _ in range(25):
                try:
                    data = apm_sensor.read_measurements()  # (pm1, pm2_5, pm10) or (None,...)
                    if data and data[0] is not None:
                        samples.append(data)
                        # keep list length ≤ 10, drop oldest
                        if len(samples) > 10:
                            del samples[0]
                except Exception as e:
                    if config.debug:
                        print("APM read error:", e)
                await asyncio.sleep(1.2) # APM10 min 1s interval

            if samples:
                # Use last ~10 to avoid warm-up
                n = len(samples)
                total_pm1 = 0.0
                total_pm25 = 0.0
                total_pm10 = 0.0
                # manual sums (no generator allocations)
                for pm1, pm25, pm10 in samples:
                    total_pm1 += pm1
                    total_pm25 += pm25
                    total_pm10 += pm10

                state.apm = [
                    total_pm1 / n,
                    total_pm25 / n,
                    total_pm10 / n,
                ]
                state.last_apm_ts = rtc_tup(rtc)
                state.heartbeat = time.time()   # <<-- APM10 task success heartbeat

        except Exception as e:
            sd.append_error("apm10_task_error", e, ts=rtc_tup(rtc))
        finally:
            # Ensure we exit measurement mode and clear running flag
            try:
                apm_sensor.exit_measurement_mode()
            except Exception:
                pass
            state.apm_running = False

        await asyncio.sleep(interval)


async def wifi_mqtt_task(rtc, state, sd):
    """
    Maintain Wi-Fi and MQTT with separate backoff for Wi-Fi and MQTT.
    - wifi_fail_count increments on Wi-Fi connect failures.
    - mqtt_fail_count increments on MQTT connect failures (only attempted when wifi_ok).
    """
    WIFI_SSID = config.WIFI_SSID
    WIFI_PASS = config.WIFI_PASS
    WIFI_BASE_BACKOFF = getattr(config, "WIFI_BASE_BACKOFF", 30)   # seconds
    WIFI_MAX_BACKOFF  = getattr(config, "WIFI_MAX_BACKOFF", 3600) # cap (e.g. 60 min)
    MQTT_BASE_BACKOFF = getattr(config, "MQTT_BASE_BACKOFF", 15)
    MQTT_MAX_BACKOFF  = getattr(config, "MQTT_MAX_BACKOFF", 1800)
    BACKOFF_JITTER_PCT = getattr(config, "BACKOFF_JITTER_PCT", 0.15)  # ±15%
    CONNECT_POLL = getattr(config, "CONNECT_POLL", 0.2)  # 0.2s poll for responsiveness
    WIFI_CONNECT_TIMEOUT = getattr(config, "WIFI_CONNECT_TIMEOUT", 10)  # secs per attempt

    wifi_fail_count = 0
    mqtt_fail_count = 0
    
    while True:
        try:
            # --- Try Wi-Fi if not connected ---
            if not state.wlan or not state.wlan.isconnected():
                try:
                    if config.debug:
                        print("WiFi: (re)connecting...")
                    wlan = await async_connect_wifi(WIFI_SSID, WIFI_PASS,
                                                    timeout=WIFI_CONNECT_TIMEOUT,
                                                    poll=CONNECT_POLL)
                    # success
                    state.wlan = wlan
                    state.wifi_ok = True
                    wifi_fail_count = 0 # reset backoff state
                    state.heartbeat = time.time()   # <<-- WIFI success heartbeat
                except Exception as e:
                    # Connection failed this cycle
                    state.wifi_ok = False # since, wifi connect failed
                    wifi_fail_count += 1
                    sd.append_error("wifi_fail", e, ts=rtc_tup(rtc))

            # --- Try MQTT if Wi-Fi is OK and no client (blocking) ---
            if state.wifi_ok and not state.mqtt_client:
                try:
                    if config.debug:
                        print("MQTT: connecting...")
                    state.mqtt_client = MQTTClientSimple(
                        config.client_id,
                        config.server,
                        config.port,
                        config.user,
                        config.password,
                        config.KEEP_ALIVE_INTERVAL,
                    )
                    state.mqtt_ok = True # connection successful, if reached here without any error
                    mqtt_fail_count = 0 # reset backoff state
                    state.heartbeat = time.time()   # <<-- MQTT success heartbeat
                except Exception as e:
                    # Mark mqtt as failed; next cycle we will back off
                    state.mqtt_client = None
                    state.mqtt_ok = False
                    mqtt_fail_count += 1
                    sd.append_error("mqtt_connect_fail", e, ts=rtc_tup(rtc))

            if not state.wifi_ok:
                # if Wi-Fi is down, MQTT can't be up
                state.mqtt_client = None
                state.mqtt_ok = False

        except Exception as e:
            # Unexpected outer error — log, but keep loop alive
            sd.append_error("wifi_mqtt_task_error", e, ts=rtc_tup(rtc))
                 
        # BACKOFF
        # If Wi-Fi is not OK, wait wifi_wait. If Wi-Fi OK but MQTT failed, wait mqtt_wait;
        if not state.wifi_ok:
            wait = compute_backoff(WIFI_BASE_BACKOFF, wifi_fail_count, WIFI_MAX_BACKOFF, BACKOFF_JITTER_PCT) # wifi_wait
        elif state.wifi_ok and not state.mqtt_client:
            wait = compute_backoff(MQTT_BASE_BACKOFF, mqtt_fail_count, MQTT_MAX_BACKOFF, BACKOFF_JITTER_PCT) # mqtt_wait
        else:
            # Everything OK — keep a low-frequency check (e.g., 30s)
            wait = 30

        await asyncio.sleep(wait)


# --------------------------------------------------------------------
# LCD / button (toggle pages)
# --------------------------------------------------------------------

# --- Helper ---
def _safe_row_val(row, idx, default="--"):
    """Return row[idx] or default without raising; row may be None."""
    try:
        return row[idx] or default
    except Exception:
        return default
#---------------
    
def draw_lcd_page(lcd, state, page):
    """
    Draw page 1, 2, 3, .. on the Nokia 5110 LCD (84x48).
    NOTE: Each text is 8x8 pixel by default [display size: 84x48 (x, y)]
    Page 1: status + uptime + heartbeat
    Page 2: sensor snapshot (compact)
    Page 3: detailed readings / PM + OWM expanded
    ...
    """
    lcd.power_on() # switch on the screen
    lcd.fill(0) # clear screen (ddram)
    
    row = state.sensor_row or [""] * 16 # Remember: row is list of formatted strings

    if page == 1: # page == 1
        # System status
        lcd.text("Room Logger", 0, 0)
        lcd.text("W:%s" % ("OK" if state.wifi_ok else "XX"), 0, 8)
        lcd.text("M:%s" % ("OK" if state.mqtt_ok else "XX"), 40, 8)
        
        # Uptime
        lcd.text("U:%s" % (format_uptime(state.boot_ts)), 0, 16)
        
        # heartbeat age in seconds (or "OK" if recent)
        try:
            hb = getattr(state, "heartbeat", None)
            if hb:
                age = int(time.time() - hb)
                # if heartbeat younger than 120s show OK, else show seconds
                if age <= 90:
                    lcd.text("HB: OK", 0, 24)
                else:
                    lcd.text("HB:%3ds" % age, 0, 24)
            else:
                lcd.text("HB: --", 0, 24)
        except Exception:
            lcd.text("HB: ??", 0, 24)

        # last sensor reading timestamp
        ts = tup_to_iso(state.last_sensor_ts)
        lcd.text("S:%s" % ts[-8:], 0, 32)  # hh:mm:ss
        
        # rtc time
        #ts = tup_to_iso(rtc_tup(rtc=rtc))
        #lcd.text("S:%s" % ts[-8:], 0, 40)  # hh:mm:ss
        
        # reset cause

    elif page == 2: # page == 2
        # Temperature snapshot
        lcd.text("bmpT %s" % _safe_row_val(row,1), 0, 0)
        lcd.text("ahtT %s" % _safe_row_val(row,3), 0, 8)
        lcd.text("shtT %s" % _safe_row_val(row,6), 0, 16)
        lcd.text("dsT %s" % _safe_row_val(row,5), 0, 24)
        lcd.text("outT %s" % _safe_row_val(row,11), 0, 32)
        lcd.text("appT %s" % _safe_row_val(row,12), 0, 40)
        
    elif page == 3: # page == 3
        # Humidity, Pressure snapshot
        lcd.text("ahtH %s" % _safe_row_val(row,4), 0, 0)
        lcd.text("shtH %s" % _safe_row_val(row,7), 0, 8)
        lcd.text("outH %s" % _safe_row_val(row,13), 0, 16)
        lcd.text("bmpP %s" % _safe_row_val(row,2), 0, 24)
        lcd.text("outP %s" % _safe_row_val(row,14), 0, 32)
        
    elif page == 4: # page == 4
        # APM snapshot
        lcd.text("PM1.0 %s" % _safe_row_val(row,8), 0, 0)
        lcd.text("PM2.5 %s" % _safe_row_val(row,9), 0, 8)
        lcd.text("PM10 %s" % _safe_row_val(row,10), 0, 16)
    
    elif page == 5: # page == 5
        # Debug Page
        # ram/memory status
        lcd.text("MEMORY(KB)", 0, 0)
        lcd.text("FREE:%s" % gc.mem_free(), 0, 8)
        lcd.text("USED:%s" % gc.mem_alloc(), 0, 16)
        
    lcd.show() # show ddram on screen


async def button_lcd_task(state, lcd, button1_pin, button2_pin):
    """
    Two-button Prev/Next navigation for pages.
    - button1_pin, button2_pin: machine.Pin objects (pull-ups; pressed == 0)
    - DISPLAY_TIMEOUT: seconds to auto-off after last user action
    - DEBOUNCE_MS: simple debounce window
    - POLL_MS: main loop sleep
    """
    POLL_MS = 80 # existing sleep
    DISPLAY_TIMEOUT = 15  # seconds after last button press
    DEBOUNCE_MS = 20
    TOTAL_PAGES = 5 # total number of pages

    lcd_on = False
    current_page = 1
    last_press_time = 0
    last_level1 = 1  # pull-up, 1 = not pressed
    last_level2 = 1  # pull-up, 1 = not pressed
    
    # ---- turn LCD on at startup and show page 1 ----
    draw_lcd_page(lcd, state, 1)   # show page 1 immediately
    lcd_on = True
    last_press_time = time.time()
    # --------------------------------------
    
    while True:
        level1 = button1_pin.value()  # 0 = pressed
        level2 = button2_pin.value()

        # NEXT button edge: just pressed
        if level1 == 0 and last_level1 == 1:
            now = time.time()
            # debounce
            await asyncio.sleep_ms(DEBOUNCE_MS)
            if button1_pin.value() == 0:
                last_press_time = now

                if not lcd_on:
                    lcd_on = True
                    current_page = 1
                else:
                    # advance page: 1->2->3->...->1
                    current_page = 1 if current_page == TOTAL_PAGES else current_page + 1

                draw_lcd_page(lcd, state, current_page)
                
        # PREV button edge: just pressed
        if level2 == 0 and last_level2 == 1:
            now = time.time()
            # debounce
            await asyncio.sleep_ms(DEBOUNCE_MS)
            if button2_pin.value() == 0:
                last_press_time = now

                if not lcd_on:
                    lcd_on = True
                    current_page = 1
                else:
                    # go back page: 1<-2<-3...<-1
                    current_page = TOTAL_PAGES if current_page == 1 else current_page - 1

                draw_lcd_page(lcd, state, current_page)
                
        
        # auto-off
        if lcd_on and (time.time() - last_press_time >= DISPLAY_TIMEOUT):
            lcd.clear()            
            lcd.power_off() # save power by switching the screen off
            lcd_on = False

        last_level1 = level1
        last_level2 = level2
        await asyncio.sleep_ms(POLL_MS)


# --------------------------------------------------------------------
# Watchdog
# -------------------------------------------------------------------

# --- Hardware Watchdog ---
def init_hardware_wdt():
    """
    Return Hardware WDT object or None if not available.
    Resets if system hangs.
    """
    try:
        from machine import WDT
        return WDT(timeout=getattr(config, "HARD_WDT_TIMEOUT_MS", 120000))  # e.g. 20s
    except Exception:
        return None

async def watchdog_manager(rtc, state, sd, hard_wdt=None):
    """
    Single watchdog task that:
      - feeds hardware WDT (wdt.feed())
      - checks state.heartbeat; if stale by soft_wdt_timeout_s => reset

    Params:
      - rtc: rtc object
      - state: your State instance (shared object)
      - sd: sd card object
      - wdt: hardware WDT object or None 
    """
    
    hard_wdt_feed_interval_s = config.HARD_WDT_FEED_INTERVAL_S # how often to feed hardware WDT (seconds)
    soft_wdt_timeout_s = config.SOFT_WDT_TIMEOUT_S # software wdt timeout (seconds) - reset if no heartbeat            
    check_interval_s = min(hard_wdt_feed_interval_s, max(1, soft_wdt_timeout_s // 3)) # how often to run checks

    while True:
        try:
            # feed hardware WDT at hw_feed_interval_s cadence
            if hard_wdt:
                try:
                    hard_wdt.feed()
                except Exception:
                    # ignore feed failures; next iteration will try again
                    pass

            # check software heartbeat
            last = getattr(state, "heartbeat", None)
            if last is not None:
                now = time.time()
                if now - last > soft_wdt_timeout_s:
                    # Soft watchdog triggered: log and reset (best-effort, minimal prints)
                    sd.append_error("watchdog_stale:", "stale:%.0f, reboot" % (now - last), ts=rtc_tup(rtc), force=True)
                    # short delay for logs to flush
                    await asyncio.sleep(0.2)
                    reset() # machine.reset()
            # else: no heartbeat yet — just allow sleep and try again later
            
        except Exception:
            # never let watchdog task crash; swallow and continue after a short pause
            pass

        await asyncio.sleep(check_interval_s)
        

# --------------------------------------------------------------------
# Main setup
# --------------------------------------------------------------------


async def main():
    gc.collect()
    
    # Init hardware/drivers once
    
    # --- RTC ---
    # Initialize i2c bus and alarm pin
    '''
    NOTE: Multiple I2C instances: creating separate I2C(...) objects from different modules
    fragments the heap. I now use a single shared I2C instance created once and passed to
    sensors or returned from an internal getter.
    '''
    i2c = I2C(0, scl=Pin(config.sclPIN), sda=Pin(config.sdaPIN), freq=100000)
    alarmPIN = Pin(config.alarmPIN, Pin.IN, Pin.PULL_UP)
    
    rtc = ds3231(i2c, alarmPIN)

    # Optional one-time NTP sync at boot (if Wi-Fi available)
    try:
        wlan = await async_connect_wifi(config.WIFI_SSID, config.WIFI_PASS,
                                        timeout=config.WIFI_CONNECT_TIMEOUT,
                                        poll=config.CONNECT_POLL)
        if wlan and wlan.isconnected():
            state.wlan = wlan
            state.wifi_ok = True
            #rtc.sync_time_with_ntp()
            state.heartbeat = time.time()   # <<-- mark initial good state
            if config.debug:
                print("RTC synced with NTP")
    except Exception as e:
        if config.debug:
            print("Initial WiFi/NTP failed:", e)

    # --- SD logger ---
    sd = SDLogger(
        spi_pin_miso=config.SPI_PIN_MISO,
        spi_pin_mosi=config.SPI_PIN_MOSI,
        spi_pin_sck=config.SPI_PIN_SCK,
        spi_pin_cs=config.SPI_PIN_CS,
        data_path=config.DEFAULT_DATA_PATH,
        error_path=config.DEFAULT_ERROR_PATH,
        debug=config.debug,
    )

    # --- Sensors (I2C + onewire) ---
    # Initialize softi2c bus and onewire pin
    '''creating single instance of softi2c object and passing it to different sensor modules'''
    softi2c = SoftI2C(scl=Pin(config.soft_sclPIN), sda=Pin(config.soft_sdaPIN), freq=100000)
    onewirePin = Pin(config.ONEWIRE_PIN)
        
    sensors = Sensors(i2c=i2c,
                      softi2c=None,
                      onewirePin=onewirePin)

    # APM sensor obtained from Sensors
    apm_sensor = getattr(sensors, "apm", None)
    
    # --- OWM client ---
    owm_client = OWMClient(config.OWM_URL, config.OWM_HEADERS)
    
    # --- LCD (SPI) ---
    # NOTE: In, ESP32, SPI(2) bus is being internally used by machine.SDCard;
    #       so we can not re-initialize and re-use it with lcd without
    #       explicit spi bus object. Hence we will use separate hardware spi bus for lcd (HSPI)
    spi_lcd = SPI(
        1,
        baudrate=2000000,        # 2 MHz is plenty for Nokia 5110
        polarity=0,
        phase=0,
        sck=Pin(config.LCD_CLK),
        mosi=Pin(config.LCD_DIN),
        miso=Pin(0),              # dummy, LCD doesn't use MISO
        )
            
    lcd_cs  = Pin(config.LCD_CE,  Pin.OUT, value=1)   
    lcd_dc  = Pin(config.LCD_DC,  Pin.OUT)            
    lcd_rst = Pin(config.LCD_RST, Pin.OUT)            
    
    lcd = PCD8544_FB(spi_lcd, lcd_cs, lcd_dc, lcd_rst)
    # power off the screen
    lcd.power_off()
    
    # --- Buttons ---
    button1 = Pin(config.BUTTON_1, Pin.IN, Pin.PULL_UP) # button next
    button2 = Pin(config.BUTTON_2, Pin.IN, Pin.PULL_UP) # button prev
    
    # create hardware WDT (early if available)
    hard_wdt = init_hardware_wdt()
    
    # --- Start tasks ---
    asyncio.create_task(watchdog_manager(rtc, state, sd, hard_wdt=hard_wdt))
    asyncio.create_task(wifi_mqtt_task(rtc, state, sd))
    asyncio.create_task(owm_task(rtc, state, owm_client, sd))
    if apm_sensor:
        asyncio.create_task(apm10_task(rtc, state, apm_sensor, sd))
    elif config.debug:
        print("APM10: not detected, skipping apm10_task") # task not created
    asyncio.create_task(sensor_and_log_task(rtc, state, sensors, sd))
    asyncio.create_task(button_lcd_task(state, lcd, button1, button2))

    # Keep loop alive
    while True:
        await asyncio.sleep(3600)


# MicroPython uasyncio entry point
try:
    asyncio.run(main())
finally:
    # Clean up event loop on soft reboot or crash or keyboard interrupt
    # NOTE: MicroPython can only have ONE active event loop at a time. So unless
    #       we create a new fresh event loop on restart/crash we may get RuntimeError: Event loop is closed.
    asyncio.new_event_loop()

