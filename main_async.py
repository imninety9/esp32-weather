# main.py  -- async weather + air quality logger with LCD health screen

# IMPROVEMENTS: 1. If you want to send failed data to mqtt later when reconnection happens,
#				then add a flag mqtt_sent = True/False at the end of the row in sd logging
#				and add another separate async mqtt publish task that runs every few seconds
#				and only publishes data rows or any last rows that have mqtt_sent flag set as False


import gc
import time
import uasyncio as asyncio

from micropython import const
import machine

import config
from async_connect_wifi import async_connect_wifi
from sensors_handler import Sensors
from sensors_handler import S_BMP, S_AHT, S_DS, S_SHT, S_APM
from ds3231_driver import DS3231Driver
from sd_logger import SDLogger
from mqtt_client import MQTTClientSimple
from owm import OWMClient
from pcd8544_fb import PCD8544_FB

debug = config.debug

# ---- Error keys (string, stable) ----
E_WIFI_FAIL          = "wifi_fail"
E_MQTT_CONN_FAIL     = "mqtt_conn"
E_MQTT_PUB_FAIL      = "mqtt_pub"
E_WIFI_TASK          = "wifi_task"
E_SENSOR_TASK        = "sensor_task"
E_OWM_FAIL           = "owm"
E_APM_FAIL           = "apm"
E_SD_FAIL            = "sd"
E_WATCHDOG_STALE     = "wd_stale"
E_LCD_FAIL           = "lcd"
E_HEALTH_FAIL        = "health"
E_RESET              = "reset_cause"

rst_causes = {
    machine.PWRON_RESET: "PWRON RST", # when device first time starts up after being powered on
    machine.HARD_RESET: "HARD RST", # physical reset by a button or through power cycling
    machine.WDT_RESET: "WDT RST", # wdt resets the the device when it hangs
    machine.DEEPSLEEP_RESET: "DEEPSLP RST", # waking from a deep sleep
    machine.SOFT_RESET: "SOFT RST" # software rest by a command
    }

# button bit positions
BTN_1 = const(1 << 0) # 00001 = 1
BTN_2 = const(1 << 1) # 00010 = 2

# --------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------

class State:
    __slots__ = (
        "boot_ticks",
        "rst_cause",
        "sensor_row",
        "owm_weather", "owm_aqi", "apm",
        "apm_aqi_buf", "owm_aqi_buf",
        "last_sensor_ts", "last_owm_ts", "last_apm_ts",
        "wifi_ok", "mqtt_ok", "wlan", "mqtt_client",
        "lcd_on", "current_page", "last_lcd_press",
        "button_flag", "timeout_event",
        "apm_running",
        "sht_heater_mode", "sht_high_rh_count", "sht_heater_cycles_left",
        "heartbeat",
        "cached_uptime",
        "last_uptime_calc",
    )
    
    def __init__(self):
        # start timestamp
        self.boot_ticks = time.ticks_ms() # Uptime base (monotonic, not affected by RTC changes)
        
        # reset cause
        self.rst_cause = rst_causes.get(machine.reset_cause(), "UNKNOWN")
        
        # Last full row logged / ready to log (list of values)
        
        # ["timestamp","bmp_temp","bmp_press","aht_temp","aht_hum",
        #  "ds18b20_temp","sht_temp","sht_hum",
        #  "pm1_0","pm2_5","pm10",
        #  "owm_temp","owm_temp_feels_like","owm_hum","owm_press",
        #  "owm_pm2_5","owm_pm10","owm_no2"]
        
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
        
        # cached uptime in str format
        self.cached_uptime = ""
        self.last_uptime_calc = 0


state = State()


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def sync_machine_rtc_from_ds3231(ds3231):
#     Copy DS3231 time into ESP32 internal RTC.
#     ds3231.get_time() -> (Y, M, D, hh, mm, ss, ...)
    t = ds3231.get_time() # (y, m, d, hh, mm, ss, wday, _)
    if not t:
        return

    # machine.RTC.datetime: (year, month, day, weekday, hours, minutes, seconds, subseconds)
    r = machine.RTC()
    r.datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))
            
def rtc_tup(ds3231):
    # Return (Y, M, D, H, M, S, ...) tuple using ds3231.get_time() if available.
    if ds3231 is not None:
        t = ds3231.get_time()
        if t:
            return t
    # fallback
    return time.localtime()
    
# Pre-allocate format strings at module level
_ISO_FMT = config.ISO_FMT
def tup_to_iso(t):  
    # Convert time tuple (Y, M, D, H, M, S, ...) to 'YYYY-MM-DD hh:mm:ss'.
    if not t:
        return "0000-00-00 00:00:00"
    return _ISO_FMT % (t[0], t[1], t[2], t[3], t[4], t[5])

# Pre-allocate format strings at module level
_FMT_2DEC = "%.2f" # precision is fixed at 2 decimal digits
def format_value(value, default = ""):
    # Format values for CSV / MQTT.
    if value is None:
        return default
    return str(value) if not isinstance(value, float) else _FMT_2DEC % value


def format_uptime(state, now_ticks=None):
    # uptime of project - can pass current ticks to avoid extra call.
    if now_ticks is None:
        now_ticks = time.ticks_ms()
        
    s = time.ticks_diff(now_ticks, state.boot_ticks) // 1000
    days, s = divmod(s, 86400)
    hrs, s = divmod(s, 3600)
    mins, s = divmod(s, 60)
    if days:
        state.cached_uptime = "%dd%02dh" % (days, hrs)
    else:
        state.cached_uptime = "%02d:%02d:%02d" % (hrs, mins, s)
    state.last_uptime_calc = now_ticks


def update_row(row, ts, sensor_data, owm_weather_data, owm_aqi_data, apm_data):
    # Update CSV row matching config.CSV_FILDS [list is mutable object]
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
#     Exponential backoff with jitter. fail_count=0 => no backoff (we'll still wait base once).
#     jitter_pct = ±15%
    if fail_count <= 0:
        backoff = base
    else:
        backoff = min(base * (2 ** (fail_count - 1)), max_backoff)
    # apply jitter ±jitter_pct using a pseudo-random number from ticks
    tms = time.ticks_ms() & 0xFFFF  # 0..65535
    frac = tms * 2 - 65535   # range [-65535 .. +65535]
    # scale to -j..+j
    jitter = (frac * jitter_pct * backoff) // 6553500 # 65535 * 100 to convrt into fraction
    #jitter = backoff * random.uniform(-jitter_pct, jitter_pct)
    wait = max(1, backoff + jitter)
    return wait


# particulate conc to AQI
# AQI index LOW and HIGH BREAKPOINTS
AQI_LO = (0, 51, 101, 201, 301, 401)
AQI_HI = (50, 100, 200, 300, 400, 500)
# Pollutant LOW and HIGH BREAKPOINTS
# Descriptor tuple: (name, low_breakpoints, high_breakpoints, decimals)
PM2_5 = ("PM2.5", (0, 31, 61, 91, 121, 251), (30, 60, 90, 120, 250), 0)
PM10 = ("PM10", (0, 51, 101, 251, 351, 431), (50, 100, 250, 350, 430), 0)
NO2 = ("NO2", (0, 41, 81, 181, 281, 401), (40, 80, 180, 280, 400), 0)
O3 = ("O3", (0, 51, 101, 169, 209, 749), (50, 100, 168, 208, 748), 0)
CO = ("CO", (0.0, 1.1, 2.1, 10.1, 17.1, 34.1), (1.0, 2.0, 10.0, 17.0, 34.0), 1)
SO2 = ("SO2", (0, 41, 81, 381, 801, 1601), (40, 80, 380, 800, 1600), 0)
NH3 = ("NH3", (0, 201, 401, 801, 1201, 1801), (200, 400, 800, 1200, 1800), 0)
Pb = ("Pb", (0.0, 0.6, 1.1, 2.1, 3.1, 3.6), (0.5, 1.0, 2.0, 3.0, 3.5), 1)
                   
def conc_to_aqi(pollutant_desc, concentration):
#     pollutant_desc: pollutant descriptor from above tuples
#     concentrations: pollutant's concentration
#     return aqi corresponding to pollutant concentration
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

async def sensor_and_log_task(ds3231, state, sensors, sd):
    # Read sensors every SENSOR_INTERVAL_SECS, build row, log to SD and publish MQTT.
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
                        # switch SHT40 into heater measurement mode (20 mW, 0.1s)
                        sensors.sht40.set_mode(mode=9) # mode 9 is 20mW heater for 0.1s
                        state.sht_heater_mode = True
                        state.sht_heater_cycles_left = sht_heat_cyc  # M heater readings
                        if debug:
                            print("SHT40 heater mode on")
                        state.sht_high_rh_count = 0 # reset counter
                else:
                    state.sht_high_rh_count = 0 # reset counter
            else:
                # HEATER MODE
                state.sht_heater_cycles_left -= 1
                if state.sht_heater_cycles_left <= 0:
                    sensors.sht40.set_mode(mode=1) # mode 1 is normal mode
                    state.sht_heater_mode = False
                    if debug:
                        print("SHT40 normal mode on")

            # 2. Timestamp from RTC (DS3231) and now_ticks
            ts = rtc_tup(ds3231)
            now_ticks = time.ticks_ms()
            
            # 3. Update row in state (uses last known OWM + APM data)
            row = update_row(state.sensor_row, ts, sensor_data, state.owm_weather, state.owm_aqi, state.apm)
            
            # 4. Append to SD
            sd.append_row(row, ts=ts)
            
            # 5. Try MQTT publish
            if state.mqtt_client and state.mqtt_ok:
                try:
                    if time.ticks_diff(now_ticks, state.last_uptime_calc) > 60000: # 1 min
                        format_uptime(state, now_ticks) # update cached_uptime
                    mqtt_msgs[0] = row[1]   # bmp_temp
                    mqtt_msgs[1] = row[2]   # bmp_press
                    mqtt_msgs[2] = row[3]  # aht_temp
                    mqtt_msgs[3] = row[4]  # aht_hum
                    mqtt_msgs[4] = _STATUS_FMT % (
                                        row[5], state.cached_uptime,
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
                    message = str(e) if debug else e.__class__.__name__
                    sd.append_error(E_MQTT_PUB_FAIL, message, ts=ts)
            
            state.last_sensor_ts = now_ticks
            state.heartbeat = now_ticks # <<-- Sensor log task success heartbeat
            
        except Exception as e:
            # NOTE: In MicroPython: never store exception objects — extract info, then let them die because they keep allocation alive.
            #       Hence, instead of passing e, we are creating a str of e and passing it while releasing e.
            # e.__class__.__name__ is a str object and tells the class name of error
            message = str(e) if debug else e.__class__.__name__ # if we are not in debug mode; then avoid full error (e) and a str conversion
            sd.append_error(E_SENSOR_TASK, message, ts=rtc_tup(ds3231), min_interval_ms=300000)
        
        # Sleep until next sample
        
        # NOTE: The asyncio.sleep(interval) in the finally style placement (after except) means
        # the loop won’t spin like crazy if something fails.
        
        await asyncio.sleep(interval)


async def owm_task(ds3231, state, owm_client, sd):
    # Fetch OpenWeatherMap data every OWM_FETCH_INTERVAL_SECS when Wi-Fi is OK.
    interval = config.OWM_WEATHER_FETCH_INTERVAL_SECS
    # fetch aqi each 60 minutes only
    aqi_count = 0
    aqi_cycle = max(1, config.OWM_AQI_FETCH_INTERVAL_SECS // interval)
    
    while True:
        # Wait for APM to finish if it's running
        while state.apm_running: # and state.lcd_on => to keep lcd responsive
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
                    await asyncio.sleep_ms(0) # yield, before another long blocking task -> to keep the system responsive
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
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_OWM_FAIL, message, ts=rtc_tup(ds3231))
              
        await asyncio.sleep(interval)


async def apm10_task(ds3231, state, sensors, sd):
#     Run APM10 once per hour with event notification, average last few readings.
#     Uses await sleeps between samples so other tasks can run.
    interval = config.APM10_INTERVAL_SECS  # seconds between PM updates
    STABILIZATION_SECS = const(15) # Wait for stabilization
    n_samples = const(3) # number of samples taken in a reading cycle
    
    # APM sensor obtained from Sensors
    apm_sensor = getattr(sensors, "apm", None)
    
    # If the sensor isn't present, exit the task cleanly without looping it
    if apm_sensor is None:
        if debug:
            print("apm10_task exiting")
        return
    
    while True:
#         # wait for either:
#         # - normal interval
#         # - forced event
#         try:
#             await asyncio.wait_for(state.apm_evt.wait(), interval)
#             state.apm_evt.clear()   # forced run
#         except asyncio.TimeoutError:
#             pass                    # normal scheduled run
        
        # Mark APM as running for UI
        state.apm_running = True
        
        try:
            # Enter measurement mode
            apm_sensor.enter_measurement_mode()
            await asyncio.sleep(STABILIZATION_SECS) # warm-up for stabilization
            
            n = 0
            pm1 = pm2_5 = pm10 = 0.0
            # Take ~3 samples, keep last 2; sensor min interval ~1s
            for i in range(n_samples):
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
                    
            sensors.status |= S_APM          # success → healthy
            
            t = time.ticks_ms()
            state.last_apm_ts = t
            state.heartbeat = t   # <<-- APM10 task success heartbeat

        except Exception as e:
            sensors.status &= ~S_APM         # failure → unhealthy
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_APM_FAIL, message, ts=rtc_tup(ds3231))
        finally:
            # Ensure we exit measurement mode and clear running flag
            try:
                apm_sensor.exit_measurement_mode()
            except Exception:
                pass
            state.apm_running = False

        await asyncio.sleep(interval)


async def wifi_mqtt_task(ds3231, state, sd):
#     Maintain Wi-Fi and MQTT with separate backoff for Wi-Fi and MQTT.
#     - wifi_fail_count increments on Wi-Fi connect failures.
#     - mqtt_fail_count increments on MQTT connect failures (only attempted when wifi_ok).
    interval = config.WIFI_TASK_INTERVAL_SECS
    WIFI_SSID = config.WIFI_SSID
    WIFI_PASS = config.WIFI_PASS
    WIFI_BASE_BACKOFF = getattr(config, "WIFI_BASE_BACKOFF", const(30))   # seconds
    WIFI_MAX_BACKOFF  = getattr(config, "WIFI_MAX_BACKOFF", const(3600)) # cap (e.g. 60 min)
    MQTT_BASE_BACKOFF = getattr(config, "MQTT_BASE_BACKOFF", const(15))
    MQTT_MAX_BACKOFF  = getattr(config, "MQTT_MAX_BACKOFF", const(1800))
    BACKOFF_JITTER_PCT = getattr(config, "BACKOFF_JITTER_PCT", const(15))  # ±15%
    CONNECT_POLL_MS = config.CONNECT_POLL_MS # 0.2s poll for responsiveness
    WIFI_CONNECT_TIMEOUT_MS = config.WIFI_CONNECT_TIMEOUT_MS  # secs per attempt

    wifi_fail_count = 0
    mqtt_fail_count = 0
    
    while True:
        try:
            # --- Try Wi-Fi if not connected ---
            if not state.wlan or not state.wlan.isconnected():
                try:
                    if debug:
                        print("WiFi (re)connecting...")
                    wlan = await async_connect_wifi(WIFI_SSID, WIFI_PASS,
                                                    timeout_ms=WIFI_CONNECT_TIMEOUT_MS,
                                                    poll_ms=CONNECT_POLL_MS)
                    # success
                    state.wlan = wlan
                    state.wifi_ok = True
                    state.mqtt_ok = False # since wifi (re)connected => mqtt must have been disconnected
                    wifi_fail_count = 0 # reset backoff state
                    state.heartbeat = time.ticks_ms()   # <<-- WIFI success heartbeat
                except Exception as e:
                    # Connection failed this cycle
                    state.wifi_ok = False # since, wifi connect failed
                    wifi_fail_count += 1
                    message = str(e) if debug else e.__class__.__name__
                    sd.append_error(E_WIFI_FAIL, message, ts=rtc_tup(ds3231))

            # --- Try MQTT if Wi-Fi is OK and no client (blocking) ---
            if state.wifi_ok and not state.mqtt_ok:
                try:
                    if not state.mqtt_client:
                        state.mqtt_client = MQTTClientSimple(
                            config.client_id,
                            config.server,
                            config.port,
                            config.user,
                            config.password,
                            config.KEEP_ALIVE_INTERVAL,
                        )
                        
                    if debug:
                        print("MQTT connecting...")
                    state.mqtt_client._connect()
                    state.mqtt_ok = True # connection successful, if reached here without any error
                    mqtt_fail_count = 0 # reset backoff state
                    state.heartbeat = time.ticks_ms()   # <<-- MQTT success heartbeat
                except Exception as e:
                    # Mark mqtt as failed; next cycle we will back off
                    state.mqtt_ok = False
                    mqtt_fail_count += 1
                    message = str(e) if debug else e.__class__.__name__
                    sd.append_error(E_MQTT_CONN_FAIL, message, ts=rtc_tup(ds3231))

            if not state.wifi_ok:
                # if Wi-Fi is down, MQTT can't be up
                state.mqtt_ok = False

        except Exception as e:
            # Unexpected outer error — log, but keep loop alive
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_WIFI_TASK, message, ts=rtc_tup(ds3231))
                 
        # BACKOFF
        # If Wi-Fi is not OK, wait wifi_wait. If Wi-Fi OK but MQTT failed, wait mqtt_wait;
        if not state.wifi_ok:
            wait = compute_backoff(WIFI_BASE_BACKOFF, wifi_fail_count, WIFI_MAX_BACKOFF, BACKOFF_JITTER_PCT) # wifi_wait
        elif state.wifi_ok and not state.mqtt_ok:
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
    # Return val or default without raising; val may be None.
    return val or default
#---------------

def draw_lcd_page(ds3231, lcd, state, sensors, page):
#     Draw page 1, 2, 3, .. on the Nokia 5110 LCD (84x48).
#     NOTE: Each text is 8x8 pixel by default [display size: 84x48 (x, y)]
#     Page 1: status + uptime + heartbeat
#     Page 2: sensor snapshot (compact)
#     Page 3: detailed readings / PM + OWM expanded
#     ...

    lcd.fill(0) # clear screen (ddram)
    
    # Common X offsets for 8x8 font
    # W: 8px, Label (5 chars): 40px
    LABEL_OFFSET = const(0)
    VALUE_OFFSET = const(40) # 5 chars * 8px

    # -------------------------
    # PAGE 1: SYSTEM STATUS
    # -------------------------
    if page == 1:
        _OK = "OK"
        _XX = "XX"
        
        now_ticks = time.ticks_ms()
        # Title
        lcd.text("Room Logger", LABEL_OFFSET, 0)

        # WiFi / MQTT
        # "W:" is static (0 RAM alloc if const), status is dynamic
        lcd.text("W:", LABEL_OFFSET, 8) 
        lcd.text(_OK if state.wifi_ok else _XX, 16, 8)
        
        lcd.text("M:", 40, 8)
        lcd.text(_OK if state.mqtt_ok else _XX, 56, 8)

        # UPTIME
        # Avoid "U:" + uptime concatenation
        lcd.text("U:", LABEL_OFFSET, 16)
        if time.ticks_diff(now_ticks, state.last_uptime_calc) > 60000: # 1 min
            format_uptime(state, now_ticks) # update cached_uptime
        lcd.text(state.cached_uptime, 16, 16)

        # HEARTBEAT
        hb = state.heartbeat
        lcd.text("HB:", LABEL_OFFSET, 24)
        if hb:
            age = time.ticks_diff(now_ticks, hb) // 1000
            # if heartbeat younger than 120s show OK, else show seconds
            lcd.text(_OK if age <= 90 else str(age), 24, 24)
        else:
            lcd.text(_XX, 24, 24)

        # DS3231 time
        # Slice the ISO string to avoid creating a new "T:" + time string
        t = rtc_tup(ds3231)
        lcd.text("T:", LABEL_OFFSET, 32)
        lcd.text("%02d:%02d:%02d" % (t[3], t[4], t[5]), 16, 32) # Draw just the time part
        
        # RESET CAUSE
        lcd.text(state.rst_cause, LABEL_OFFSET, 40)

    # -------------------------
    # PAGE 2: SENSORS STATUS
    # -------------------------
    elif page == 2:
        # Display	Meaning
        # XX	sensor not present
        # !!	sensor present but last read failed -> unhealthy
        # OK	sensor present and last read succeeded -> healthy
        _OK = "OK"
        _XX = "XX"
        _II = "!!"
        
        _status = sensors.status
        
        # BMP280
        lcd.text("BMP:", LABEL_OFFSET, 0)
        lcd.text(
            _XX if not sensors.bmp280 else
            (_OK if (_status & S_BMP) else _II),
            VALUE_OFFSET, 0
        )

        # AHT25
        lcd.text("AHT:", LABEL_OFFSET, 8)
        lcd.text(
            _XX if not sensors.aht25 else
            (_OK if (_status & S_AHT) else _II),
            VALUE_OFFSET, 8
        )

        # DS18B20
        lcd.text("DS: ", LABEL_OFFSET, 16)
        lcd.text(
            _XX if not sensors.ds18b20 else
            (_OK if (_status & S_DS) else _II),
            VALUE_OFFSET, 16
        )

        # SHT40
        lcd.text("SHT:", LABEL_OFFSET, 24)
        lcd.text(
            _XX if not sensors.sht40 else
            (_OK if (_status & S_SHT) else _II),
            VALUE_OFFSET, 24
        )

        # APM10
        lcd.text("APM:", LABEL_OFFSET, 32)
        lcd.text(
            _XX if not sensors.apm else
            (_OK if (_status & S_APM) else _II),
            VALUE_OFFSET, 32
        )

        
    # -------------------------
    # PAGE 3: TEMPERATURES
    # -------------------------
    elif page == 3:
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
    # PAGE 4: HUM / PRESS
    # -------------------------
    elif page == 4:
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
    # PAGE 5: AQI
    # -------------------------
    elif page == 5:
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
    # PAGE 6: MEMORY
    # -------------------------
    elif page == 6:
        lcd.text("MEMORY (B)", LABEL_OFFSET, 0)
        
        lcd.text("FREE:", LABEL_OFFSET, 8)
        lcd.text(str(gc.mem_free()), VALUE_OFFSET, 8)
        
        lcd.text("USED:", LABEL_OFFSET, 16)
        lcd.text(str(gc.mem_alloc()), VALUE_OFFSET, 16)

    lcd.show() # show ddram on screen


# --- ISRs ---
def button1_isr(pin):
    # Interrupt handler for button 1 (NEXT)
    state.button_mask.set()   # IRQ-safe  # Wake up the LCD task (# ← "Ring the doorbell")

def button2_isr(pin):
    # Interrupt handler for button 2 (PREV)
    state.button_flag.set()   # IRQ-safe  # Wake up the LCD task (# ← "Ring the doorbell")
    
async def lcd_timeout_watcher(state, lcd):
    # Separate task that ONLY runs when LCD is on.
    # Watches for LCD timeout and turns off LCD.
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

async def button_lcd_task(ds3231, state, sensors, lcd, button1_pin, button2_pin):
#     Event-driven LCD task - only wakes when button is pressed.
#     Much more efficient than polling.
#     Two-button Prev/Next navigation for pages.
    
    # button bit positions
    BTN_1 = const(1 << 0) # 00001 = 1
    BTN_2 = const(1 << 1) # 00010 = 2
    
    debounce_ms = config.DEBOUNCE_MS
    total_pages = config.TOTAL_PAGES
    
    # Setup interrupts
    button1_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=button1_isr)
    button2_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=button2_isr)
    
    # ---- turn LCD on at startup and show page 1 ----
    lcd.power_on() # switch on the screen
    draw_lcd_page(ds3231, lcd, state, sensors, 1)   # show page 1 immediately
    state.lcd_on = True
    state.last_lcd_press = time.ticks_ms()
    state.timeout_event.set()  # Signal timeout watcher that LCD is on
    # --------------------------------------
    
    while True:
        try:
            # Wait FOREVER for button press (no timeout!)
            # This task literally sleeps until a button is pressed
            await state.button_flag.wait() # ThreadSafeFlag Auto-clears automatically
            
            # 1. Debounce check - ignore if pressed too soon after last press
            now = time.ticks_ms()
            if time.ticks_diff(now, state.last_lcd_press) < debounce_ms:
                continue # Ignore this press
            
            # 2. Capture button presses using mask
            state.last_lcd_press = now
            
            button_mask = 0 # Which button was pressed (1, 2, both or None) 
            if not button1_pin.value(): # button 1 is pressed -> LOW (0)
                button_mask |= BTN_1 # set bit (Mark Pressed)
            if not button2_pin.value(): # button 2 is pressed -> LOW (0)
                button_mask |= BTN_2 # set bit (Mark Pressed)

            # 3. Act on the mask -> Handle button press
            if button_mask & BTN_1:  # NEXT button
                if not state.lcd_on:
                    lcd.power_on() # switch on the screen
                    state.lcd_on = True
                    state.current_page = 1
                    state.timeout_event.set()  # Signal timeout watcher
                else:
                    state.current_page = 1 if state.current_page == total_pages else state.current_page + 1
                
            if button_mask & BTN_2:  # PREV button
                if not state.lcd_on:
                    lcd.power_on() # switch on the screen
                    state.lcd_on = True
                    state.current_page = 1
                    state.timeout_event.set()  # Signal timeout watcher
                else:
                    state.current_page = total_pages if state.current_page == 1 else state.current_page - 1
            
            draw_lcd_page(ds3231, lcd, state, sensors, state.current_page)
        
        except Exception as e:
            # Unexpected outer error — log, but keep loop alive
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_LCD_FAIL, message, ts=rtc_tup(ds3231))
        

    
# --------------------------------------------------------------------
# Watchdog
# -------------------------------------------------------------------

# --- Hardware Watchdog ---
def init_hardware_wdt():
#     Return Hardware WDT object or None if not available.
#     Resets if system hangs.
    try:
        from machine import WDT
        return WDT(timeout=getattr(config, "HARD_WDT_TIMEOUT_MS", const(120000)))  # e.g. 20s
    except Exception:
        return None

async def watchdog_manager(ds3231, state, sd, hard_wdt=None):
#     Single watchdog task that:
#       - feeds hardware WDT (wdt.feed())
#       - checks state.heartbeat; if stale by soft_wdt_timeout_ms => reset
# 
#     Params:
#       - ds3231: ds3231 object
#       - state: your State instance (shared object)
#       - sd: sd card object
#       - wdt: hardware WDT object or None 
    
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
                        sd.append_error(E_WATCHDOG_STALE, "stale:%.0f, reboot" % (age_ms//1000), ts=rtc_tup(ds3231), force=True)
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

async def health_log_task(ds3231, state, sd):
    # Periodically log uptime and memory to health.csv for debugging.
    interval = getattr(config, "HEALTH_LOG_INTERVAL_SECS", const(3600))  # default 1 hour

    sd.ensure_health_header()

    while True:
        try:
            now_ticks = time.ticks_ms()
            if time.ticks_diff(now_ticks, state.last_uptime_calc) > 60000: # 1 min
                format_uptime(state, now_ticks) # update cached_uptime
            mem_free = gc.mem_free()
            mem_alloc = gc.mem_alloc()
            #if mem_free < 20_000: print("Memory low:", mem_free)
            #print(state.cached_uptime, mem_free, mem_alloc)
            sd.append_health(rtc_tup(ds3231), state.cached_uptime, mem_free, mem_alloc)
        except Exception as e:
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_HEALTH_FAIL, message, ts=rtc_tup(ds3231))

        await asyncio.sleep(interval)



# --------------------------------------------------------------------
# Main setup
# --------------------------------------------------------------------


async def main():
    gc.collect()
    
    # Init hardware/drivers once
    
    # --- DS3231 ---
    # Initialize i2c bus and alarm pin
    
    # NOTE: Multiple I2C instances: creating separate I2C(...) objects from different modules
    # fragments the heap. I now use a single shared I2C instance created once and passed to
    # sensors or returned from an internal getter.
    
    i2c = machine.I2C(0, scl=machine.Pin(config.sclPIN), sda=machine.Pin(config.sdaPIN), freq=100000)
    ds3231 = DS3231Driver(i2c)
    alarmPIN = machine.Pin(config.alarmPIN, machine.Pin.IN, machine.Pin.PULL_UP)

    # Optional one-time NTP sync at boot (if Wi-Fi available)
    try:
        wlan = await async_connect_wifi(config.WIFI_SSID, config.WIFI_PASS,
                                        timeout_ms=config.WIFI_CONNECT_TIMEOUT_MS,
                                        poll_ms=config.CONNECT_POLL_MS)
        if wlan and wlan.isconnected():
            state.wlan = wlan
            state.wifi_ok = True
            #ds3231.sync_time_with_ntp()
            state.heartbeat = time.ticks_ms()   # <<-- mark initial good state
            if debug:
                print("DS3231 synced with NTP")
    except Exception as e:
        if debug:
            print("Initial WiFi/NTP failed:", e)
           
    # set internal RTC from DS3231 so filesystem timestamps (metadata) are correct
    sync_machine_rtc_from_ds3231(ds3231)

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
    # NOTE: creating single instance of softi2c object and passing it to different sensor modules
    softi2c = machine.SoftI2C(scl=machine.Pin(config.soft_sclPIN), sda=machine.Pin(config.soft_sdaPIN), freq=100000)
    onewirePin = machine.Pin(config.ONEWIRE_PIN)
        
    sensors = Sensors(i2c=i2c,
                      softi2c=None,
                      onewirePin=onewirePin,
                      run_apm=config.run_apm)
    
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
    sd.append_error(E_RESET, state.rst_cause, ts=rtc_tup(ds3231), force=True)
    
    # --- Start tasks ---
    asyncio.create_task(watchdog_manager(ds3231, state, sd, hard_wdt=hard_wdt))
    asyncio.create_task(wifi_mqtt_task(ds3231, state, sd))
    asyncio.create_task(owm_task(ds3231, state, owm_client, sd))
    if config.run_apm:
        asyncio.create_task(apm10_task(ds3231, state, sensors, sd))
    elif debug:
        print("skipping apm10_task") # task not created
    asyncio.create_task(sensor_and_log_task(ds3231, state, sensors, sd))
    asyncio.create_task(button_lcd_task(ds3231, state, sensors, lcd, button1, button2))
    asyncio.create_task(lcd_timeout_watcher(state, lcd))
    if config.debug_mem:
        asyncio.create_task(health_log_task(ds3231, state, sd))

    # Keep loop alive
    interval = const(3600)
    while True:
        await asyncio.sleep(interval)


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
