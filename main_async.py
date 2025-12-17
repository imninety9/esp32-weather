# main.py  -- async weather + air quality logger with LCD health screen

# IMPROVEMENTS: 1. If you want to send failed data to mqtt later when reconnection happens,
#				then add a flag mqtt_sent = True/False at the end of the row in sd logging
#				and add another separate async mqtt publish task that runs every few seconds
#				and only publishes data rows or any last rows that have mqtt_sent flag set as False


import gc
import time
import uasyncio as asyncio

import machine

import config
from async_connect_wifi import async_connect_wifi
from sensors_handler import Sensors
from ds3231rtc import ds3231
from sd_logger import SDLogger
from mqtt_client import MQTTClientSimple
from owm import OWMClient
from pcd8544_fb import PCD8544_FB

debug = config.debug

rst_causes = {
    machine.PWRON_RESET: "PWRON RST", # when device first time starts up after being powered on
    machine.HARD_RESET: "HARD RST", # physical reset by a button or through power cycling
    machine.WDT_RESET: "WDT RST", # wdt resets the the device when it hangs
    machine.DEEPSLEEP_RESET: "DEEPSLP RST", # waking from a deep sleep
    machine.SOFT_RESET: "SOFT RST" # software rest by a command
    }
# --------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------

class State:
    __slots__ = (
        "boot_ticks",
        "rst_cause",
        "sensor_row",
        "owm_weather", "owm_aqi", "apm",
        "last_sensor_ts", "last_owm_ts", "last_apm_ts",
        "wifi_ok", "mqtt_ok", "wlan", "mqtt_client",
        "lcd_on", "current_page", "last_lcd_press",
        "button_pressed", "button_flag", "timeout_event",
        "apm_running",
        "sht_heater_mode", "sht_high_rh_count", "sht_heater_cycles_left",
        "heartbeat"
    )
    
    def __init__(self):
        # start timestamp
        self.boot_ticks = time.ticks_ms() # Uptime base (monotonic, not affected by RTC changes)
        
        # reset cause
        self.rst_cause = rst_causes.get(machine.reset_cause(), "UNKNOWN")
        
        # Last full row logged / ready to log (list of values)
        '''
        ["timestamp","bmp_temp","bmp_press","aht_temp","aht_hum",
         "ds18b20_temp","sht_temp","sht_hum",
         "pm1_0","pm2_5","pm10",
         "owm_temp","owm_temp_feels_like","owm_hum","owm_press",
         "owm_pm2_5","owm_pm10","owm_no2"]
         '''
        self.sensor_row = [""] * 18

        # Latest external sources
        self.owm_weather = [""] * 4  # temp, feels_like, hum, press
        self.owm_aqi = [""] * 3  # pm2.5, pm10, no2
        self.apm = [""] * 3  # pm1, pm2_5, pm10
        # aqi buffers
        self.apm_aqi_buf = [""] * 2    # PM2.5, PM10
        self.owm_aqi_buf = [""] * 3    # PM2.5, PM10, NO2

        # Timestamps
        self.last_sensor_ts = None
        self.last_owm_ts = None
        self.last_apm_ts = None

        # Network
        self.wifi_ok = False
        self.mqtt_ok = False
        self.wlan = None
        self.mqtt_client = None
        
        # LCD state
        self.lcd_on = False
        self.current_page = 1
        self.last_lcd_press = 0
        self.button_pressed = None # Which button was pressed (1, 2, or None) 
        self.button_flag = asyncio.ThreadSafeFlag() # asyncio.ThreadSafeFlag for button press (Use ThreadSafeFlag for IRQs - its safer)
        self.timeout_event = asyncio.Event() # asyncio.Event for LCD timeout (it is set from a Task, not an ISR - so safe)
        
        # APM sensor state
        self.apm_running = False
        #self.apm_evt = asyncio.Event() # trigger one APM run on long button press, etc
        
        # sht40 heater mode
        self.sht_heater_mode = False
        self.sht_high_rh_count = 0
        self.sht_heater_cycles_left = 0

        
        # heartbeat
        self.heartbeat = None


state = State()


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def sync_machine_rtc_from_ds3231(rtc):
    """
    Copy DS3231 time into ESP32 internal RTC.
    ds3231.get_time() -> (Y, M, D, hh, mm, ss, ...)
    """
    try:
        t = rtc.get_time() # (y, m, d, hh, mm, ss, wday, _)
        if t is None:
            return

        # machine.RTC.datetime: (year, month, day, weekday, hours, minutes, seconds, subseconds)
        r = machine.RTC()
        r.datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))
    except Exception as e:
        if debug:
            print("sync_machine_rtc_from_ds3231 error:", e)
            
def rtc_tup(rtc):
    """Return (Y, M, D, H, M, S, ...) tuple using ds3231.get_time() if available."""
    if rtc is not None:
        t = rtc.get_time()
        if t:
            return t
    # fallback
    return time.localtime()
    
# Pre-allocate format strings at module level
_ISO_FMT = config.ISO_FMT
def tup_to_iso(t):  
    """Convert time tuple (Y, M, D, H, M, S, ...) to 'YYYY-MM-DD hh:mm:ss'."""
    if not t:
        return "0000-00-00 00:00:00"
    return _ISO_FMT % (t[0], t[1], t[2], t[3], t[4], t[5])

# Pre-allocate format strings at module level
_FMT_2DEC = "%.2f" # precision is fixed at 2 decimal digits
def format_value(value, default = ""):
    """
    Format values for CSV / MQTT.
    """
    if value is None:
        return default
    return str(value) if not isinstance(value, float) else _FMT_2DEC % value


def format_uptime(boot_ticks, now_ticks=None):
    """
    uptime of project - can pass current ticks to avoid extra call.
    """
    if now_ticks is None:
        now_ticks = time.ticks_ms()
        
    s = time.ticks_diff(now_ticks, boot_ticks) // 1000
    days, s = divmod(s, 86400)
    hrs, s = divmod(s, 3600)
    mins, s = divmod(s, 60)
    if days:
        return "%dd%02dh" % (days, hrs)
    return "%02d:%02d:%02d" % (hrs, mins, s)


def update_row(row, ts, sensor_data, owm_weather_data, owm_aqi_data, apm_data):
    """
    Update CSV row matching config.CSV_FILDS [list is mutable object]
    """
    row[0] = tup_to_iso(ts)
    row[1] = format_value(sensor_data[0][0])
    row[2] = format_value(sensor_data[0][1])
    row[3] = format_value(sensor_data[1][0])
    row[4] = format_value(sensor_data[1][1])
    row[5] = format_value(sensor_data[2])
    row[6] = format_value(sensor_data[3][0])
    row[7] = format_value(sensor_data[3][1])
    row[8] = apm_data[0]
    row[9] = apm_data[1]
    row[10] = apm_data[2]
    row[11] = owm_weather_data[0]
    row[12] = owm_weather_data[1]
    row[13] = owm_weather_data[2]
    row[14] = owm_weather_data[3]
    row[15] = owm_aqi_data[0]
    row[16] = owm_aqi_data[1]
    row[17] = owm_aqi_data[2]
    
    return row


def compute_backoff(base, fail_count, max_backoff, jitter_pct=15):
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
    frac = tms * 2 - 65535   # range [-65535 .. +65535]
    # scale to -j..+j
    jitter = (frac * jitter_pct * backoff) // 6553500 # 65535 * 100 for jitter %
    #jitter = backoff * random.uniform(-jitter_pct, jitter_pct)
    wait = max(1, backoff + jitter)
    return wait


# particulate conc to AQI
# AQI index LOW and HIGH BREAKPOINTS
AQI_LO = (0, 51, 101, 201, 301, 401)
AQI_HI = (50, 100, 200, 300, 400, 500)
# Pollutant LOW and HIGH BREAKPOINTS
# Descriptor tuple: (name, low_breakpoints, high_breakpoints, decimals)
PM2_5 = (
    "PM2.5",
    (0, 31, 61, 91, 121, 251),
    (30, 60, 90, 120, 250),
    0,
)
PM10 = (
    "PM10",
    (0, 51, 101, 251, 351, 431),
    (50, 100, 250, 350, 430),
    0,
)
NO2 = (
    "NO2",
    (0, 41, 81, 181, 281, 401),
    (40, 80, 180, 280, 400),
    0,
)
O3 = (
    "O3",
    (0, 51, 101, 169, 209, 749),
    (50, 100, 168, 208, 748),
    0,
)
CO = (
    "CO",
    (0.0, 1.1, 2.1, 10.1, 17.1, 34.1),
    (1.0, 2.0, 10.0, 17.0, 34.0),
    1,
)
SO2 = (
    "SO2",
    (0, 41, 81, 381, 801, 1601),
    (40, 80, 380, 800, 1600),
    0,
)
NH3 = (
    "NH3",
    (0, 201, 401, 801, 1201, 1801),
    (200, 400, 800, 1200, 1800),
    0,
)
Pb = (
    "Pb",
    (0.0, 0.6, 1.1, 2.1, 3.1, 3.6),
    (0.5, 1.0, 2.0, 3.0, 3.5),
    1,
)
                   
def conc_to_aqi(pollutant_desc, concentration):
    """
    pollutant_desc: pollutant descriptor from above tuples
    concentrations: pollutant's concentration
    return aqi corresponding to pollutant concentration
    """
    if concentration is None or concentration < 0:
        return None

    _, low_bp, high_bp, decimals = pollutant_desc

    # rounding style: 1 decimal place for CO/Pb, nearest int for others
    if decimals:
        c = round(concentration, 1)
    else:
        c = round(concentration)

    # Find the breakpoint interval
    for i, hi in enumerate(high_bp):
        if c <= hi:
            c_lo = low_bp[i]
            c_hi = high_bp[i]
            i_lo = AQI_LO[i]
            i_hi = AQI_HI[i]

            # linear interpolation
            # I = ((I_hi - I_lo)/(C_hi - C_lo)) * (C - C_lo) + I_lo
            return int(
                (i_hi - i_lo) * (c - c_lo) / (c_hi - c_lo)
                + i_lo
                )
    # above last breakpoint -> clamp to max AQI
    return 500
                


# --------------------------------------------------------------------
# Async tasks
# --------------------------------------------------------------------

async def sensor_and_log_task(rtc, state, sensors, sd):
    """Read sensors every SENSOR_INTERVAL_SECS, build row, log to SD and publish MQTT."""
    interval = config.SENSOR_INTERVAL_SECS
    mem_threshold = config.MIN_MEM_THRESHOLD
    sht_rh_thresh = config.SHT_HIGH_RH_COUNT_THRESHOLD
    sht_rh_cnt_thresh = config.SHT_HIGH_RH_COUNT_THRESHOLD
    sht_heat_cyc = config.SHT_HEATER_CYCLES

    mqtt_feeds = config.feeds
    mqtt_msgs = [None]*9  # pre-allocating once at module top to avoid re-allocating new list each time

    _STATUS_FMT = "T:%s UP:%s iPM2.5:%s iPM10:%s oPM2.5:%s oPM10:%s"
    
    while True:
        try:
            if gc.mem_free() < mem_threshold:
                gc.collect()

            # 1. Read all local sensors (blocking but quick)
            sensor_data = sensors.read_measurements()
            
            # 1.1. Check if sht40 RH successive readings are >= 95%; if yes then may be switch the heater on for a couple of times
            if not state.sht_heater_mode:
                # NORMAL MODE
                sht_rh = sensor_data[3][1] # sht_hum = sensor_data[3][1]
                if sht_rh is not None and sht_rh >= sht_rh_thresh:
                    state.sht_high_rh_count += 1
                    if state.sht_high_rh_count >= sht_rh_cnt_thresh:  # N consecutive high-RH readings
                        try:
                            # switch SHT40 into heater measurement mode (20 mW, 0.1s)
                            sensors.sht40.set_mode(mode=9) # mode 9 is 20mW heater for 0.1s
                            state.sht_heater_mode = True
                            state.sht_heater_cycles_left = sht_heat_cyc  # M heater readings
                            if debug:
                                print("SHT40: entering heater mode")
                        except Exception as e:
                            if debug:
                                print("SHT40 heater mode error:", e)
                        state.sht_high_rh_count = 0 # reset counter
                else:
                    state.sht_high_rh_count = 0 # reset counter
            else:
                # HEATER MODE
                state.sht_heater_cycles_left -= 1
                if state.sht_heater_cycles_left <= 0:
                    try:
                        sensors.sht40.set_mode(mode=1) # mode 1 is normal mode
                        state.sht_heater_mode = False
                        if debug:
                            print("SHT40: back to normal mode")
                    except Exception as e:
                        if debug:
                            print("SHT40 normal mode error:", e)

            # 2. Timestamp from RTC (DS3231) and now_ticks
            ts = rtc_tup(rtc)
            t = time.ticks_ms()
            
            # 3. Update row in state (uses last known OWM + APM data)
            row = update_row(state.sensor_row, ts, sensor_data, state.owm_weather, state.owm_aqi, state.apm)
            
            # 4. Append to SD
            sd.append_row(row, ts=ts)
            
            # 5. Try MQTT publish
            if state.mqtt_client and state.mqtt_ok:
                try:
                    mqtt_msgs[0] = row[1]   # bmp_temp
                    mqtt_msgs[1] = row[2]   # bmp_press
                    mqtt_msgs[2] = row[3]  # aht_temp
                    mqtt_msgs[3] = row[4]  # aht_hum
                    mqtt_msgs[4] = _STATUS_FMT % (
                                        row[5], format_uptime(state.boot_ticks, t),
                                        state.apm_aqi_buf[0], state.apm_aqi_buf[1],
                                        state.owm_aqi_buf[0], state.owm_aqi_buf[1]
                                        ) # ds18b20_temp, uptime, indoor aqi, outdoor aqi
                    mqtt_msgs[5] = row[11]  # owm_temp
                    mqtt_msgs[6] = row[12]  # owm_feels_like
                    mqtt_msgs[7] = row[13]  # owm_hum
                    mqtt_msgs[8] = row[14]  # owm_press
                    
                    state.mqtt_client.publish_data(mqtt_feeds, mqtt_msgs)
                except Exception as e:
                    # Mark MQTT as broken so wifi_mqtt_task will reconnect later
                    state.mqtt_ok = False
                    sd.append_error("mqtt_publish_fail", str(e), ts=ts)
            
            state.last_sensor_ts = t
            state.heartbeat = t # <<-- Sensor log task success heartbeat
            
        except Exception as e:
            sd.append_error("sensor_and_log_task_error", str(e), ts=rtc_tup(rtc), min_interval=300)
        
        # Sleep until next sample
        '''
        NOTE: The asyncio.sleep(interval) in the finally style placement (after except) means
        the loop won’t spin like crazy if something fails.
        '''
        await asyncio.sleep(interval)


async def owm_task(rtc, state, owm_client, sd):
    """Fetch OpenWeatherMap data every OWM_FETCH_INTERVAL_SECS when Wi-Fi is OK."""
    interval = config.OWM_WEATHER_FETCH_INTERVAL_SECS
    # fetch aqi each 60 minutes only
    aqi_count = 0
    aqi_cycle = max(1, config.OWM_AQI_FETCH_INTERVAL_SECS // interval)
    
    while True:
        # Wait for APM to finish if it's running
        while state.apm_running:
            # Wait a short time and try again later. we use this tactic because this task is "blocking" and we want apm sensor to take regular measurements
            await asyncio.sleep(10)
        
        # else go fetch OWM (with a short timeout)
        try:
            if state.wifi_ok:
                # blocking but with a timeout of 5 s
                wd = owm_client.fetch_weather() # [temp, feels_like, hum, press]
                state.owm_weather[0] = format_value(wd[0])
                state.owm_weather[1] = format_value(wd[1])
                state.owm_weather[2] = format_value(wd[2])
                state.owm_weather[3] = format_value(wd[3])
                
                if aqi_count == 0:
                    aq = owm_client.fetch_aqi() # [pm2.5, pm10, no2]
                    
                    state.owm_aqi[0] = format_value(aq[0])
                    state.owm_aqi[1] = format_value(aq[1])
                    state.owm_aqi[2] = format_value(aq[2])
                    
                    state.owm_aqi_buf[0] = format_value(conc_to_aqi(PM2_5, aq[0]))
                    state.owm_aqi_buf[1] = format_value(conc_to_aqi(PM10, aq[1]))
                    state.owm_aqi_buf[2] = format_value(conc_to_aqi(NO2, aq[2]))
                
                aqi_count = (aqi_count + 1) % aqi_cycle
                
                t = time.ticks_ms()
                state.last_owm_ts = t
                state.heartbeat = t   # <<-- OWM task success heartbeat
        except Exception as e:
            sd.append_error("owm_task_error", str(e), ts=rtc_tup(rtc))
              
        await asyncio.sleep(interval)


async def apm10_task(rtc, state, apm_sensor, sd):
    """
    Run APM10 once per hour with event notification, average last few readings.
    Uses await sleeps between samples so other tasks can run.
    """
    interval = config.APM10_INTERVAL_SECS  # seconds between PM updates
    STABILIZATION_SECS = 15 # Wait for stabilization
    
    # If the sensor isn't present, exit the task cleanly without looping it
    if apm_sensor is None:
        if debug:
            print("APM10: sensor not available, apm10_task exiting")
        return
    
    while True:
        '''
        # wait for either:
        # - normal interval
        # - forced event
        try:
            await asyncio.wait_for(state.apm_evt.wait(), interval)
            state.apm_evt.clear()   # forced run
        except asyncio.TimeoutError:
            pass                    # normal scheduled run
        '''
        
        # Mark APM as running for UI
        state.apm_running = True
        
        try:
            # Enter measurement mode
            apm_sensor.enter_measurement_mode()
            await asyncio.sleep(STABILIZATION_SECS) # warm-up for stabilization
            
            n = 0
            pm1 = pm2_5 = pm10 = 0.0
            # Take ~3 samples, keep last 2; sensor min interval ~1s
            for i in range(3):
                data = apm_sensor.read_measurements()  # (pm1, pm2_5, pm10) or (None,...)
                if data and data[0] is not None:
                    # drop oldest reading
                    if i > 0:
                        pm1 += data[0]
                        pm2_5 += data[1]
                        pm10 += data[2]
                        n += 1
                await asyncio.sleep(1.2) # APM10 min 1s interval
            
            if n == 0: # avoid division by zero
                pm1 = pm2_5 = pm10 = None
            else:
                inv_n = 1.0 / n
                pm1  *= inv_n
                pm2_5 *= inv_n
                pm10 *= inv_n 
            
            state.apm[0] = format_value(pm1)
            state.apm[1] = format_value(pm2_5)
            state.apm[2] = format_value(pm10)
            
            state.apm_aqi_buf[0] = format_value(conc_to_aqi(PM2_5, pm2_5))
            state.apm_aqi_buf[1] = format_value(conc_to_aqi(PM10, pm10))
                    
            t = time.ticks_ms()
            state.last_apm_ts = t
            state.heartbeat = t   # <<-- APM10 task success heartbeat

        except Exception as e:
            sd.append_error("apm10_task_error", str(e), ts=rtc_tup(rtc))
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
    interval = config.WIFI_TASK_INTERVAL_SECS
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
                    if debug:
                        print("WiFi: (re)connecting...")
                    wlan = await async_connect_wifi(WIFI_SSID, WIFI_PASS,
                                                    timeout=WIFI_CONNECT_TIMEOUT,
                                                    poll=CONNECT_POLL)
                    # success
                    state.wlan = wlan
                    state.wifi_ok = True
                    wifi_fail_count = 0 # reset backoff state
                    state.heartbeat = time.ticks_ms()   # <<-- WIFI success heartbeat
                except Exception as e:
                    # Connection failed this cycle
                    state.wifi_ok = False # since, wifi connect failed
                    wifi_fail_count += 1
                    sd.append_error("wifi_fail", str(e), ts=rtc_tup(rtc))

            # --- Try MQTT if Wi-Fi is OK and no client (blocking) ---
            if state.wifi_ok and not state.mqtt_ok:
                try:
                    if not state.mqtt_client:
                        if debug:
                            print("MQTT: connecting...")
                        state.mqtt_client = MQTTClientSimple(
                            config.client_id,
                            config.server,
                            config.port,
                            config.user,
                            config.password,
                            config.KEEP_ALIVE_INTERVAL,
                        )
                    else:
                        state.mqtt_client._connect()
                    state.mqtt_ok = True # connection successful, if reached here without any error
                    mqtt_fail_count = 0 # reset backoff state
                    state.heartbeat = time.ticks_ms()   # <<-- MQTT success heartbeat
                except Exception as e:
                    # Mark mqtt as failed; next cycle we will back off
                    state.mqtt_client = None
                    state.mqtt_ok = False
                    mqtt_fail_count += 1
                    sd.append_error("mqtt_connect_fail", str(e), ts=rtc_tup(rtc))

            if not state.wifi_ok:
                # if Wi-Fi is down, MQTT can't be up
                state.mqtt_ok = False

        except Exception as e:
            # Unexpected outer error — log, but keep loop alive
            sd.append_error("wifi_mqtt_task_error", str(e), ts=rtc_tup(rtc))
                 
        # BACKOFF
        # If Wi-Fi is not OK, wait wifi_wait. If Wi-Fi OK but MQTT failed, wait mqtt_wait;
        if not state.wifi_ok:
            wait = compute_backoff(WIFI_BASE_BACKOFF, wifi_fail_count, WIFI_MAX_BACKOFF, BACKOFF_JITTER_PCT) # wifi_wait
        elif state.wifi_ok and not state.mqtt_client:
            wait = compute_backoff(MQTT_BASE_BACKOFF, mqtt_fail_count, MQTT_MAX_BACKOFF, BACKOFF_JITTER_PCT) # mqtt_wait
        else:
            # Everything OK — keep a low-frequency check (e.g., 30s)
            wait = interval

        await asyncio.sleep(wait)


# --------------------------------------------------------------------
# LCD / button (toggle pages)
# --------------------------------------------------------------------

# --- Helper ---
def _safe_val(val, default="--"):
    """Return val or default without raising; val may be None."""
    return val or default
#---------------

def draw_lcd_page(rtc, lcd, state, page):
    """
    Draw page 1, 2, 3, .. on the Nokia 5110 LCD (84x48).
    NOTE: Each text is 8x8 pixel by default [display size: 84x48 (x, y)]
    Page 1: status + uptime + heartbeat
    Page 2: sensor snapshot (compact)
    Page 3: detailed readings / PM + OWM expanded
    ...
    """

    lcd.fill(0) # clear screen (ddram)
    
    # Common X offsets for 8x8 font
    # W: 8px, Label (5 chars): 40px
    LABEL_OFFSET = 0
    VALUE_OFFSET = 40 # 5 chars * 8px
    
    # -------------------------
    # PAGE 1: SYSTEM STATUS
    # -------------------------
    if page == 1:
        now_ticks = time.ticks_ms()
        # Title
        lcd.text("Room Logger", 0, 0)

        # WiFi / MQTT
        # "W:" is static (0 RAM alloc if const), status is dynamic
        lcd.text("W:", 0, 8) 
        lcd.text("OK" if state.wifi_ok else "XX", 16, 8)
        
        lcd.text("M:", 40, 8)
        lcd.text("OK" if state.mqtt_ok else "XX", 56, 8)

        # UPTIME
        # Avoid "U:" + uptime concatenation
        lcd.text("U:", 0, 16)
        lcd.text(format_uptime(state.boot_ticks, now_ticks), 16, 16)

        # HEARTBEAT
        try:
            hb = state.heartbeat
            lcd.text("HB:", 0, 24)
            if hb:
                age = time.ticks_diff(now_ticks, hb) // 1000
                # if heartbeat younger than 120s show OK, else show seconds
                if age <= 90:
                    lcd.text("OK", 24, 24)
                else:
                    lcd.text(str(age), 24, 24)
            else:
                lcd.text("--", 24, 24)
        except Exception:
            lcd.text("HB: ??", 0, 24)

        # RTC time
        # Slice the ISO string to avoid creating a new "T:" + time string
        t = rtc_tup(rtc)
        lcd.text("T:", 0, 32)
        lcd.text("%02d:%02d:%02d" % (t[3], t[4], t[5]), 16, 32) # Draw just the time part
        
        # RESET CAUSE
        lcd.text(state.rst_cause, 0, 40)

    # -------------------------
    # PAGE 2: TEMPERATURES
    # -------------------------
    elif page == 2:
        row = state.sensor_row
        # Draw labels and values separately to avoid concatenation allocations
        # Line 0
        lcd.text("bmpT", LABEL_OFFSET, 0)
        lcd.text(_safe_val(row[1]), VALUE_OFFSET, 0)
        
        # Line 1
        lcd.text("ahtT", LABEL_OFFSET, 8)
        lcd.text(_safe_val(row[3]), VALUE_OFFSET, 8)
        
        # Line 2
        lcd.text("shtT", LABEL_OFFSET, 16)
        lcd.text(_safe_val(row[6]), VALUE_OFFSET, 16)
        
        # Line 3
        lcd.text("dsT ", LABEL_OFFSET, 24)
        lcd.text(_safe_val(row[5]), VALUE_OFFSET, 24)
        
        # Line 4
        lcd.text("outT", LABEL_OFFSET, 32)
        lcd.text(_safe_val(row[11]), VALUE_OFFSET, 32)
        
        # Line 5
        lcd.text("appT", LABEL_OFFSET, 40)
        lcd.text(_safe_val(row[12]), VALUE_OFFSET, 40)

    # -------------------------
    # PAGE 3: HUM / PRESS
    # -------------------------
    elif page == 3:
        row = state.sensor_row
        
        lcd.text("ahtH", LABEL_OFFSET, 0)
        lcd.text(_safe_val(row[4]), VALUE_OFFSET, 0)

        lcd.text("shtH", LABEL_OFFSET, 8)
        lcd.text(_safe_val(row[7]), VALUE_OFFSET, 8)

        lcd.text("outH", LABEL_OFFSET, 16)
        lcd.text(_safe_val(row[13]), VALUE_OFFSET, 16)

        lcd.text("bmpP", LABEL_OFFSET, 24)
        lcd.text(_safe_val(row[2]), VALUE_OFFSET, 24)

        lcd.text("outP", LABEL_OFFSET, 32)
        lcd.text(_safe_val(row[14]), VALUE_OFFSET, 32)

    # -------------------------
    # PAGE 4: AQI
    # -------------------------
    elif page == 4:
        apm_aqi = state.apm_aqi_buf
        owm_aqi = state.owm_aqi_buf
        
        lcd.text("i2.5", LABEL_OFFSET, 0)
        lcd.text(_safe_val(apm_aqi[0]), VALUE_OFFSET, 0)

        lcd.text("i10 ", LABEL_OFFSET, 8)
        lcd.text(_safe_val(apm_aqi[1]), VALUE_OFFSET, 8)

        lcd.text("o2.5", LABEL_OFFSET, 16)
        lcd.text(_safe_val(owm_aqi[0]), VALUE_OFFSET, 16)

        lcd.text("o10 ", LABEL_OFFSET, 24)
        lcd.text(_safe_val(owm_aqi[1]), VALUE_OFFSET, 24)

        lcd.text("oNO2", LABEL_OFFSET, 32)
        lcd.text(_safe_val(owm_aqi[2]), VALUE_OFFSET, 32)

    # -------------------------
    # PAGE 5: MEMORY
    # -------------------------
    elif page == 5:
        lcd.text("MEMORY (B)", 0, 0)
        
        lcd.text("FREE:", 0, 8)
        lcd.text(str(gc.mem_free()), 40, 8)
        
        lcd.text("USED:", 0, 16)
        lcd.text(str(gc.mem_alloc()), 40, 16)

    lcd.show() # show ddram on screen


# --- ISRs ---
def button1_isr(pin):
    """Interrupt handler for button 1 (NEXT)"""
    state.button_pressed = 1
    state.button_flag.set()   # IRQ-safe  # Wake up the LCD task (# ← "Ring the doorbell")

def button2_isr(pin):
    """Interrupt handler for button 2 (PREV)"""
    state.button_pressed = 2
    state.button_flag.set()   # IRQ-safe  # Wake up the LCD task (# ← "Ring the doorbell")
    
async def lcd_timeout_watcher(state, lcd):
    """
    Separate task that ONLY runs when LCD is on.
    Watches for LCD timeout and turns off LCD.
    """
    timeout_ms = config.DISPLAY_TIMEOUT_MS # display timeout
    
    while True:
        # Wait for signal that LCD was turned on
        await state.timeout_event.wait() # ← Sleeps here until event is set
        state.timeout_event.clear()
        
        # Now LCD is on, start watching for timeout
        while state.lcd_on:
            now_ticks = time.ticks_ms()
            elapsed_ms = time.ticks_diff(now_ticks, state.last_lcd_press)            
            remaining_ms = timeout_ms - elapsed_ms
            
            if remaining_ms <= 0:
                # Timeout! Turn off LCD
                lcd.clear()
                lcd.power_off() # save power by switching the screen off
                state.lcd_on = False
                break # Exit the inner loop, go back to waiting
            
            # Sleep exactly until the timeout is supposed to happen.
            # If a user presses a button during this sleep, 'state.last_lcd_press' 
            # will update in the background. When we wake up, we will loop 
            # again, recalculate 'remaining', see that we have more time, 
            # and sleep again.
            await asyncio.sleep_ms(remaining_ms)
        
        # LCD is now off, go back to waiting for next turn-on

async def button_lcd_task(rtc, state, lcd, button1_pin, button2_pin):
    """
    Event-driven LCD task - only wakes when button is pressed.
    Much more efficient than polling.
    Two-button Prev/Next navigation for pages.
    """
    
    debounce_ms = config.DEBOUNCE_MS
    total_pages = config.TOTAL_PAGES
    
    # Setup interrupts
    button1_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=button1_isr)
    button2_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=button2_isr)
    
    # ---- turn LCD on at startup and show page 1 ----
    lcd.power_on() # switch on the screen
    draw_lcd_page(rtc, lcd, state, 1)   # show page 1 immediately
    state.lcd_on = True
    state.last_lcd_press = time.ticks_ms()
    state.timeout_event.set()  # Signal timeout watcher that LCD is on
    # --------------------------------------
    
    while True:
        try:
            # Wait FOREVER for button press (no timeout!)
            # This task literally sleeps until a button is pressed
            await state.button_flag.wait() # ThreadSafeFlag Auto-clears automatically
            
            # Debouncing - ignore if pressed too soon after last press
            now = time.ticks_ms()
            if time.ticks_diff(now, state.last_lcd_press) < debounce_ms:
                state.button_pressed = None
                continue # Ignore this press
            
            state.last_lcd_press = now

            # Handle button press
            if state.button_pressed == 1:  # NEXT button
                if not state.lcd_on:
                    lcd.power_on() # switch on the screen
                    state.lcd_on = True
                    state.current_page = 1
                    state.timeout_event.set()  # Signal timeout watcher
                else:
                    state.current_page = 1 if state.current_page == total_pages else state.current_page + 1
                
            elif state.button_pressed == 2:  # PREV button
                if not state.lcd_on:
                    lcd.power_on() # switch on the screen
                    state.lcd_on = True
                    state.current_page = 1
                    state.timeout_event.set()  # Signal timeout watcher
                else:
                    state.current_page = total_pages if state.current_page == 1 else state.current_page - 1
            
            draw_lcd_page(rtc, lcd, state, state.current_page)
            state.button_pressed = None  # Clear the button state
        
        except Exception as e:
            # Unexpected outer error — log, but keep loop alive
            sd.append_error("button_lcd_task_error", str(e), ts=rtc_tup(rtc))
        

    
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
      - checks state.heartbeat; if stale by soft_wdt_timeout_ms => reset

    Params:
      - rtc: rtc object
      - state: your State instance (shared object)
      - sd: sd card object
      - wdt: hardware WDT object or None 
    """
    
    hard_wdt_feed_interval_ms = config.HARD_WDT_FEED_INTERVAL_MS # how often to feed hardware WDT (milliseconds)
    soft_wdt_timeout_ms = config.SOFT_WDT_TIMEOUT_MS # software wdt timeout (milliseconds) - reset if no heartbeat            
    soft_wdt_check_interval_ms = config.SOFT_WDT_CHECK_INTERVAL_MS  # how often to check staleness of software WDT (milliseconds)
    
    soft_check_counter = 0
    soft_check_cycles = soft_wdt_check_interval_ms // hard_wdt_feed_interval_ms # hard_wdt_feed_interval_ms << soft_wdt_check_interval_ms

    while True:
        try:
            # feed hardware WDT at hw_feed_interval_ms cadence
            if hard_wdt:
                hard_wdt.feed()

            # Check software heartbeat less frequently
            soft_check_counter += 1
            if soft_check_counter >= soft_check_cycles:
                soft_check_counter = 0
                
                last = state.heartbeat
                if last is not None:
                    age_ms = time.ticks_diff(time.ticks_ms(), last)
                    if age_ms > soft_wdt_timeout_ms:
                        # Soft watchdog triggered: log and reset (best-effort, minimal prints)
                        sd.append_error("watchdog_stale", "stale:%.0f, reboot" % (age_ms//1000), ts=rtc_tup(rtc), force=True)
                        sd._safe_sync() # fsync before resetting
                        # short delay for logs to flush
                        await asyncio.sleep_ms(200)
                        machine.reset()
                # else: no heartbeat yet — just allow sleep and try again later   
        except Exception:
            # never let watchdog task crash; swallow and continue after a short pause
            pass

        await asyncio.sleep_ms(hard_wdt_feed_interval_ms)
        

# --------------------------------------------------------------------
# Health Log Task
# -------------------------------------------------------------------

async def health_log_task(rtc, state, sd):
    """Periodically log uptime and memory to health.csv for debugging."""
    interval = getattr(config, "HEALTH_LOG_INTERVAL_SECS", 3600)  # default 1 hour

    sd.ensure_health_header()

    while True:
        try:
            uptime = format_uptime(state.boot_ticks, time.ticks_ms())
            mem_free = gc.mem_free()
            mem_alloc = gc.mem_alloc()
            #if mem_free < 20_000: print("Memory low:", mem_free)
            #print(uptime, mem_free, mem_alloc)
            sd.append_health(rtc_tup(rtc), uptime, mem_free, mem_alloc)
        except Exception as e:
            sd.append_error("health_log_task_error", str(e), ts=rtc_tup(rtc))

        await asyncio.sleep(interval)



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
    i2c = machine.I2C(0, scl=machine.Pin(config.sclPIN), sda=machine.Pin(config.sdaPIN), freq=100000)
    alarmPIN = machine.Pin(config.alarmPIN, machine.Pin.IN, machine.Pin.PULL_UP)
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
            state.heartbeat = time.ticks_ms()   # <<-- mark initial good state
            if debug:
                print("RTC synced with NTP")
    except Exception as e:
        if debug:
            print("Initial WiFi/NTP failed:", e)
           
    # set internal RTC from DS3231 so filesystem timestamps (metadata) are correct
    sync_machine_rtc_from_ds3231(rtc)

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
    softi2c = machine.SoftI2C(scl=machine.Pin(config.soft_sclPIN), sda=machine.Pin(config.soft_sdaPIN), freq=100000)
    onewirePin = machine.Pin(config.ONEWIRE_PIN)
        
    sensors = Sensors(i2c=i2c,
                      softi2c=None,
                      onewirePin=onewirePin,
                      run_apm=config.run_apm)

    # APM sensor obtained from Sensors
    apm_sensor = getattr(sensors, "apm", None)
    
    # --- OWM client ---
    owm_client = OWMClient(config.OWM_URL, config.OWM_URL_AQI)
    
    # --- LCD (SPI) ---
    # NOTE: In, ESP32, SPI(2) bus is being internally used by machine.SDCard;
    #       so we can not re-initialize and re-use it with lcd without
    #       explicit spi bus object. Hence we will use separate hardware spi bus for lcd (HSPI)
    spi_lcd = machine.SPI(
        1,
        baudrate=2000000,        # 2 MHz is plenty for Nokia 5110
        polarity=0,
        phase=0,
        sck=machine.Pin(config.LCD_CLK),
        mosi=machine.Pin(config.LCD_DIN),
        miso=machine.Pin(0),              # dummy, LCD doesn't use MISO
        )
            
    lcd_cs  = machine.Pin(config.LCD_CE,  machine.Pin.OUT, value=1)   
    lcd_dc  = machine.Pin(config.LCD_DC,  machine.Pin.OUT)            
    lcd_rst = machine.Pin(config.LCD_RST, machine.Pin.OUT)            
    
    lcd = PCD8544_FB(spi_lcd, lcd_cs, lcd_dc, lcd_rst)
    # power off the screen
    lcd.power_off()
    
    # --- Buttons ---
    button1 = machine.Pin(config.BUTTON_1, machine.Pin.IN, machine.Pin.PULL_UP) # button next
    button2 = machine.Pin(config.BUTTON_2, machine.Pin.IN, machine.Pin.PULL_UP) # button prev
    
    # create hardware WDT (early if available)
    hard_wdt = init_hardware_wdt()
    
    # log reset cause (optional)
    sd.append_error("reset_cause", state.rst_cause, ts=rtc_tup(rtc), force=True)
    
    # --- Start tasks ---
    asyncio.create_task(watchdog_manager(rtc, state, sd, hard_wdt=hard_wdt))
    asyncio.create_task(wifi_mqtt_task(rtc, state, sd))
    asyncio.create_task(owm_task(rtc, state, owm_client, sd))
    if config.run_apm and apm_sensor:
        asyncio.create_task(apm10_task(rtc, state, apm_sensor, sd))
    elif debug:
        print("APM10: not detected, skipping apm10_task") # task not created
    asyncio.create_task(sensor_and_log_task(rtc, state, sensors, sd))
    asyncio.create_task(button_lcd_task(rtc, state, lcd, button1, button2))
    asyncio.create_task(lcd_timeout_watcher(state, lcd))
    if config.debug_mem:
        asyncio.create_task(health_log_task(rtc, state, sd))

    # Keep loop alive
    while True:
        await asyncio.sleep(3600)


# MicroPython uasyncio entry point
try:
    asyncio.run(main())
except KeyboardInterrupt:  # Trapping this is optional
    print('Keyboard Interrupted')  # or pass
finally:
    # Clean up event loop on soft reboot or crash or keyboard interrupt
    # NOTE: MicroPython can only have ONE active event loop at a time. So unless
    #       we create a new fresh event loop on restart/crash we may get RuntimeError: Event loop is closed.
    asyncio.new_event_loop()
