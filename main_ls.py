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
import esp32

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
E_WIFI_FAIL          = const("wifi_fail")
E_MQTT_CONN_FAIL     = const("mqtt_conn")
E_MQTT_PUB_FAIL      = const("mqtt_pub")
E_WIFI_TASK          = const("wifi_task")
E_SENSOR_TASK        = const("sensor_task")
E_OWM_FAIL           = const("owm")
E_APM_FAIL           = const("apm")
E_SD_FAIL            = const("sd")
E_LCD_FAIL           = const("lcd")
E_HEALTH_FAIL        = const("health")
E_RESET              = const("reset_cause")

rst_causes = {
    machine.PWRON_RESET: const("PWRON RST"), # when device first time starts up after being powered on
    machine.HARD_RESET: const("HARD RST"), # physical reset by a button or through power cycling
    machine.WDT_RESET: const("WDT RST"), # wdt resets the the device when it hangs
    machine.DEEPSLEEP_RESET: const("DEEPSLP RST"), # waking from a deep sleep
    machine.SOFT_RESET: const("SOFT RST") # software rest by a command
    }

# LCD Display	Meaning
# XX	sensor not present
# !!	sensor present but last read failed -> unhealthy
# OK	sensor present and last read succeeded -> healthy
_LCD_OK = const("OK")
_LCD_XX = const("XX")
_LCD_II = const("!!")


# Tasks
TASK_BIT_SENSOR = const(1 << 0)
TASK_BIT_MQTT = const(1 << 1)
TASK_BIT_OWM = const(1 << 2)
TASK_BIT_APM = const(1 << 3)
TASK_BIT_WIFI = const(1 << 4)
TASK_BIT_LCD1 = const(1 << 5)
TASK_BIT_LCD2 = const(1 << 6)
TASK_BIT_ALM = const(1 << 7)


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
        "wifi_request_event", "wifi_ready_event",
        "wifi_users_mask", "wifi_last_activity",
        "lcd_on", "current_page", "last_lcd_activity",
        "button_flag", "timeout_event",
        "alarm_flag",
        "apm_running",
        "sht_heater_mode", "sht_high_rh_count", "sht_heater_cycles_left",
        "heartbeat",
        "cached_uptime", "last_uptime_calc",
        "sensor_evt", "mqtt_evt",
        "owm_evt", "apm_evt",
        "active_tasks_mask"
    )
    
    def __init__(self):
        # start timestamp
        self.boot_ticks = time.ticks_ms() # Uptime base (monotonic, not affected by RTC changes)
        
        # reset cause
        self.rst_cause = rst_causes.get(machine.reset_cause(), const("UNKNOWN"))
        
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
        self.wifi_ok = False # True iff Wi-Fi is usable RIGHT NOW
        self.mqtt_ok = False
        self.wlan = None # WLAN object (owned ONLY by wifi_manager_task)
        self.mqtt_client = None
        
        # WiFi coordination
        self.wifi_request_event = asyncio.Event()  # PULSE / doorbell:
                                                   #   - Set by consumers to request Wi-Fi
                                                   #   - Waited on by wifi_manager_task ONLY
                                                   #   - Multiple sets collapse into one (intentional)
        self.wifi_ready_event = asyncio.Event()    # STATE / broadcast:
                                                   #   - Set by wifi_manager_task when Wi-Fi is usable
                                                   #   - Cleared by wifi_manager_task when Wi-Fi is NOT usable
                                                   #   - Waited on by consumers and consumers MUST check wifi_ok after waking
                                                   #   - MUST NEVER be cleared by consumers
        self.wifi_users_mask = 0 # Reference set of active Wi-Fi users using mask
                                 # Invariant:
                                 #   Wi-Fi must stay ON while this mask is non-zero
        self.wifi_last_activity = 0 # Timestamp of last WiFi use -> for keepalive / delayed disconnect
        
        # LCD state
        self.lcd_on = False
        self.current_page = 1
        self.last_lcd_activity = 0
        self.button_flag = asyncio.ThreadSafeFlag() # asyncio.ThreadSafeFlag for button press (Use ThreadSafeFlag for IRQs - its safer)
        self.timeout_event = asyncio.Event() # asyncio.Event for LCD timeout (it is set from a Task, not an ISR - so safe)
        
        # DS3231 Alarm Flag
        self.alarm_flag = asyncio.ThreadSafeFlag() # asyncio.ThreadSafeFlag for alarm firing (Use ThreadSafeFlag for IRQs - its safer)
        
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
        
        # task events
        self.sensor_evt = asyncio.Event()
        self.mqtt_evt = asyncio.Event()
        self.owm_evt = asyncio.Event()
        self.apm_evt = asyncio.Event()

        self.active_tasks_mask = 0 # mask for active tasks
        

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
    return _FMT_2DEC % value if type(value) is float else str(value)


def format_uptime(state, now_ticks=None, uptime_refresh_ms=60000):
    # uptime of project - can pass current ticks to avoid extra call.
    if now_ticks is None:
        now_ticks = time.ticks_ms()
        
    if time.ticks_diff(now_ticks, state.last_uptime_calc) > uptime_refresh_ms: # update cached_uptime        
        s = time.ticks_diff(now_ticks, state.boot_ticks) // 1000
        days, s = divmod(s, 86400)
        hrs, s = divmod(s, 3600)
        mins, s = divmod(s, 60)
        state.cached_uptime = (
            "%dd%02dh" % (days, hrs) if days else
            "%02d:%02d:%02d" % (hrs, mins, s)
        )
        state.last_uptime_calc = now_ticks      
    return state.cached_uptime


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
  

# heat index - calculates heat index given dry bulb temperature (in C) and relative humidity [source: wikipedia]
# Note: 1. The formula below approximates the heat index within 0.7 °C (except the values at 32 °C & 45%/70% relative humidity vary unrounded by less than ±1, respectively).
#       2. the equation described is valid only if the temperature is 27 °C or more and The relative humidity threshold is commonly set at an arbitrary 40%
#       3. Exposure to full sunshine can increase heat index values by up to 8 °C
# hi = -8.78469475556 + 1.61139411*temp + 2.33854883889*rh - 0.14611605*temp*rh -
#         0.012308094*temp*temp - 0.0164248277778*rh*rh + 2.211732e-3*temp*temp*rh +
#         7.2546e-4*temp*rh*rh - 3.582e-6*temp*temp*rh*rh

# Effects of the heat index (shade values):
# Temperature    Notes
# 27–32 °C       Caution: fatigue is possible with prolonged exposure and activity. Continuing activity could result in heat cramps.
# 32–41 °C       Extreme caution: heat cramps and heat exhaustion are possible. Continuing activity could result in heat stroke.
# 41–54 °C       Danger: heat cramps and heat exhaustion are likely; heat stroke is probable with continued activity.
# over 54 °C     Extreme danger: heat stroke is imminent.

def heat_index(t, r): # temp, rh
    # Validity check - branch early to save computation
    if t < 27 or r < 40:
        return None, None
    
    # Note: Values are truncated to 7-8 decimal places as MicroPython 
    # floats are usually 32-bit (single precision) anyway.
    # Factored Heat Index Equation (Horner-like nesting)
    # Reduces multiplications from ~14 down to 8.
    # HI = c1 + T(c2 + c5T) + R(c3 + c6R + T(c4 + c7T + R(c8 + c9T)))
    hi = (
        -8.784695 +
        t * (1.611394 - 0.012308 * t) +
        r * (2.338549 - 0.016425 * r +
             t * (-0.146116 + 0.0022117 * t +
                  r * (0.0007255 - 0.0000035 * t)))
    )
    
    if hi >= 54:
        return hi, 4
    if hi >= 41:
        return hi, 3
    if hi >= 32:
        return hi, 2
    # >= 27
    return hi, 1


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
    
    while True:
        try:
            await state.sensor_evt.wait()
            state.sensor_evt.clear()
            
            state.active_tasks_mask |= TASK_BIT_SENSOR
            
            # NOTE: We have created a sequential chain (OWM → Sensor/Log → MQTT) for tasks
            # IN CASE all are ready to run so that the data is as fresh as possible at every
            # place (log or publish).
            # Because we have a finally block in our tasks that clears the active_tasks_mask bits,
            # these loops will not deadlock even if a network task fails or times out.
            # --- MINIMAL FIX: Wait for OWM if a fetch is pending or active so that logged readings are fresh --- 
            while state.owm_evt.is_set() or (state.active_tasks_mask & TASK_BIT_OWM): # wait if OWM is either triggered or currently running
                await asyncio.sleep_ms(200) # Yield while OWM task finishes
            
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
                        
            state.last_sensor_ts = now_ticks
            state.heartbeat = now_ticks # <<-- Sensor log task success heartbeat
            
        except Exception as e:
            # NOTE: In MicroPython: never store exception objects — extract info, then let them die because they keep allocation alive.
            #       Hence, instead of passing e, we are creating a str of e and passing it while releasing e.
            # e.__class__.__name__ is a str object and tells the class name of error
            message = str(e) if debug else e.__class__.__name__ # if we are not in debug mode; then avoid full error (e) and a str conversion
            sd.append_error(E_SENSOR_TASK, message, ts=rtc_tup(ds3231), min_interval_ms=300000)
            
        finally:
            state.active_tasks_mask &= ~TASK_BIT_SENSOR

# MQTT Connection Helper
async def ensure_mqtt_connected(ds3231, state, sd):
#     Ensure MQTT is connected (assumes WiFi is already on).
#     Returns True if connected, False if failed.
    if state.mqtt_ok:
        return True
    
    if not state.wifi_ok:
        return False
    
    # Try to connect MQTT
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
        state.mqtt_client.connect()
        state.mqtt_ok = True
        
        return True
        
    except Exception as e:
        # Mark mqtt as failed; next cycle we will back off
        state.mqtt_client = None
        state.mqtt_ok = False
        message = str(e) if debug else e.__class__.__name__
        sd.append_error(E_MQTT_CONN_FAIL, message, ts=rtc_tup(ds3231))
        return False
    
async def mqtt_task(ds3231, state, sd):
    # MQTT publish - publishes sensor data.    
    WIFI_WAIT_TIMEOUT_MS = getattr(config, "WIFI_WAIT_TIMEOUT_MS", const(30000))
    
    MQTT_BASE_BACKOFF = getattr(config, "MQTT_BASE_BACKOFF", const(150)) # seconds
    MQTT_MAX_BACKOFF  = getattr(config, "MQTT_MAX_BACKOFF", const(3600)) # cap (e.g. 60 min)
    BACKOFF_JITTER_PCT = getattr(config, "BACKOFF_JITTER_PCT", const(15))  # ±15%
    
    mqtt_feeds = config.feeds
    mqtt_msgs = [None]*9  # pre-allocating once at module top to avoid re-allocating new list each time

    _STATUS_FMT = "T:%s UP:%s iPM2.5:%s iPM10:%s oPM2.5:%s oPM10:%s"
        
    # wifi dependent tasks bit positions
    _MQTT = const(1 << 0) # 00001 = 1
        
    # Task-local state
    mqtt_fail_count = 0
    mqtt_next_allowed = None
    
    while True:
        await state.mqtt_evt.wait()
        state.mqtt_evt.clear()
        
        state.active_tasks_mask |= TASK_BIT_MQTT
        
        # --- MINIMAL FIX: Wait for Sensor/Log task to update the row so that published readings are fresh ---
        while state.sensor_evt.is_set() or (state.active_tasks_mask & TASK_BIT_SENSOR): # wait if SENSOR TASK is either triggered or currently running
            await asyncio.sleep_ms(200) # Yield while SENSOR task finishes
                
        now_ticks = time.ticks_ms() # now_ticks
        
        if mqtt_next_allowed and time.ticks_diff(now_ticks, mqtt_next_allowed) < 0:
            state.active_tasks_mask &= ~TASK_BIT_MQTT
            continue
        
        # Request WiFi connection
        if not request_wifi(state, _MQTT):  # ✅ Register as WiFi user
            # WiFi not ready
            # Wait for WiFi ready event (with timeout)
            try:
                await asyncio.wait_for_ms(state.wifi_ready_event.wait(), timeout=WIFI_WAIT_TIMEOUT_MS)
                # DO NOT clear here
            except asyncio.TimeoutError:
                sd.append_error(E_MQTT_PUB_FAIL, "wifi timeout", ts=rtc_tup(ds3231))
                release_wifi(state, _MQTT, now_ticks)  # ✅ Done with WiFi, unregister
                mqtt_fail_count += 1
                delay_ms = compute_backoff(MQTT_BASE_BACKOFF, mqtt_fail_count, MQTT_MAX_BACKOFF, BACKOFF_JITTER_PCT) * 1000
                mqtt_next_allowed = time.ticks_add(now_ticks, delay_ms)
                state.active_tasks_mask &= ~TASK_BIT_MQTT
                continue
        
        # IMPORTANT:
        # wifi_ready_event waking does NOT guarantee success
        # Always re-check wifi_ok
        if state.wifi_ok:
            if await ensure_mqtt_connected(ds3231, state, sd): # Ensure MQTT is connected
                try:
                    row = state.sensor_row
                    
                    mqtt_msgs[0] = row[1]   # bmp_temp
                    mqtt_msgs[1] = row[2]   # bmp_press
                    mqtt_msgs[2] = row[3]  # aht_temp
                    mqtt_msgs[3] = row[4]  # aht_hum  
                    mqtt_msgs[4] = _STATUS_FMT % (
                                        row[5], format_uptime(state, now_ticks),
                                        state.apm_aqi_buf[0], state.apm_aqi_buf[1],
                                        state.owm_aqi_buf[0], state.owm_aqi_buf[1]
                                        ) # ds18b20_temp, uptime, indoor aqi, outdoor aqi
                    mqtt_msgs[5] = row[11]  # owm_temp
                    mqtt_msgs[6] = row[12]  # owm_feels_like
                    mqtt_msgs[7] = row[13]  # owm_hum
                    mqtt_msgs[8] = row[14]  # owm_press
                    
                    state.mqtt_client.publish_data(mqtt_feeds, mqtt_msgs)
                      
                    # reset on success
                    mqtt_fail_count = 0
                    mqtt_next_allowed = None
                    
                    state.heartbeat = now_ticks
                    
                except Exception as e:
                    # Mark MQTT as broken so wifi_mqtt_task will reconnect later
                    state.mqtt_ok = False
                    message = str(e) if debug else e.__class__.__name__
                    sd.append_error(E_MQTT_PUB_FAIL, message, ts=rtc_tup(ds3231))
            else: # mqtt not ok
                mqtt_fail_count += 1
                delay_ms = compute_backoff(MQTT_BASE_BACKOFF, mqtt_fail_count, MQTT_MAX_BACKOFF, BACKOFF_JITTER_PCT) * 1000
                mqtt_next_allowed = time.ticks_add(now_ticks, delay_ms)
        else: # wifi not ok
            sd.append_error(E_MQTT_PUB_FAIL, "wifi_fail", ts=rtc_tup(ds3231))
            mqtt_fail_count += 1
            delay_ms = compute_backoff(MQTT_BASE_BACKOFF, mqtt_fail_count, MQTT_MAX_BACKOFF, BACKOFF_JITTER_PCT) * 1000
            mqtt_next_allowed = time.ticks_add(now_ticks, delay_ms)
                      
        # ✅ Finally, ensure cleanup to avoid leaking wifi_users_mask
        release_wifi(state, _MQTT, now_ticks)
        
        state.active_tasks_mask &= ~TASK_BIT_MQTT
                        
            
async def owm_task(ds3231, state, owm_client, sd):
    # Fetch OpenWeatherMap data every OWM_FETCH_INTERVAL_SECS when Wi-Fi is OK.    
    WIFI_WAIT_TIMEOUT_MS = getattr(config, "WIFI_WAIT_TIMEOUT_MS", const(30000))
    
    OWM_BASE_BACKOFF = getattr(config, "OWM_BASE_BACKOFF", const(300))   # seconds
    OWM_MAX_BACKOFF  = getattr(config, "OWM_MAX_BACKOFF", const(7200)) # cap (sec)
    BACKOFF_JITTER_PCT = getattr(config, "BACKOFF_JITTER_PCT", const(15))  # ±15%
    
    # wifi dependent tasks bit positions
    _OWM = const(1 << 1) # 00010 = 2
        
    # fetch aqi each 60 minutes only
    aqi_count = 0
    aqi_cycle = const(6)
    
    # Task-local state
    owm_fail_count = 0
    owm_next_allowed = None
    
    while True:
        await state.owm_evt.wait()
        state.owm_evt.clear()

        state.active_tasks_mask |= TASK_BIT_OWM
        
        # Wait for APM to finish if it's running
        while state.apm_running: # and state.lcd_on => to keep lcd responsive
            # Wait a short time and try again later. we use this tactic because this task is "blocking" and we want apm sensor to take regular measurements
            await asyncio.sleep(5)
        
        # else go fetch OWM (with a short timeout)
        try:
            now_ticks = time.ticks_ms() # now_ticks
            
            if owm_next_allowed and time.ticks_diff(now_ticks, owm_next_allowed) < 0:
                state.active_tasks_mask &= ~TASK_BIT_OWM
                continue
            
            # Request WiFi connection
            if not request_wifi(state, _OWM): # ✅ Register as WiFi user
                # WiFi not ready
                # Wait for WiFi ready event (with timeout)
                try:
                    await asyncio.wait_for(state.wifi_ready_event.wait(), timeout=WIFI_WAIT_TIMEOUT_MS)
                    # DO NOT clear here
                except asyncio.TimeoutError:
                    sd.append_error(E_OWM_FAIL, "wifi timeout", ts=rtc_tup(ds3231))
                    release_wifi(state, _OWM, now_ticks)  # ✅ Done with WiFi, unregister
                    owm_fail_count += 1
                    delay_ms = compute_backoff(OWM_BASE_BACKOFF, owm_fail_count, OWM_MAX_BACKOFF, BACKOFF_JITTER_PCT) * 1000
                    owm_next_allowed = time.ticks_add(now_ticks, delay_ms)
                    state.active_tasks_mask &= ~TASK_BIT_OWM
                    continue
            
            # IMPORTANT:
            # wifi_ready_event waking does NOT guarantee success
            # Always re-check wifi_ok
            if state.wifi_ok:
                await asyncio.sleep_ms(0) # yield, before another long blocking task -> to keep the system responsive
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
                    
                # reset on success
                owm_fail_count = 0
                owm_next_allowed = None

                state.last_owm_ts = now_ticks
                state.heartbeat = now_ticks   # <<-- OWM task success heartbeat
            else: # wifi not ok
                sd.append_error(E_OWM_FAIL, "wifi_fail", ts=rtc_tup(ds3231))
                owm_fail_count += 1
                delay_ms = compute_backoff(OWM_BASE_BACKOFF, owm_fail_count, OWM_MAX_BACKOFF, BACKOFF_JITTER_PCT) * 1000
                owm_next_allowed = time.ticks_add(now_ticks, delay_ms)
        except Exception as e:
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_OWM_FAIL, message, ts=rtc_tup(ds3231))
            owm_fail_count += 1
            delay_ms = compute_backoff(OWM_BASE_BACKOFF, owm_fail_count, OWM_MAX_BACKOFF, BACKOFF_JITTER_PCT) * 1000
            owm_next_allowed = time.ticks_add(now_ticks, delay_ms)
           
        finally:
            # ✅ Finally, ensure cleanup to avoid leaking wifi_users_mask
            release_wifi(state, _OWM, now_ticks)
            
            state.active_tasks_mask &= ~TASK_BIT_OWM


async def apm10_task(ds3231, state, sensors, sd):
#     Run APM10 once per hour with event notification, average last few readings.
#     Uses await sleeps between samples so other tasks can run.
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
        
        await state.apm_evt.wait()
        state.apm_evt.clear()

        state.active_tasks_mask |= TASK_BIT_APM
        
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
                await asyncio.sleep_ms(1200) # APM10 min 1s interval
            
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
            
            now_ticks = time.ticks_ms()
            state.last_apm_ts = now_ticks
            state.heartbeat = now_ticks   # <<-- APM10 task success heartbeat

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
            
            state.active_tasks_mask &= ~TASK_BIT_APM


def request_wifi(state, task_code):
#     Register intent to use Wi-Fi.
# 
#     CONTRACT:
#     - Every call to request_wifi() MUST be paired with release_wifi()
#     - Caller must wait on wifi_ready_event if wifi_ok is False
# 
#     BEHAVIOR:
#     - Adds task to wifi_users_mask (reference count)
#     - If Wi-Fi is already up, returns True (no event set)
#     - Otherwise, pulses wifi_request_event to wake Wi-Fi manager
    state.wifi_users_mask |= task_code # register as active user (set bit)
    if state.wifi_ok:
        # Wi-Fi already usable — no need to wake manager
        return True
    else:
        # Wi-Fi not usable — request connection
        # Multiple .set() calls collapse; this is intentional
        state.wifi_request_event.set()
        return False

def release_wifi(state, task_code, now_ticks=None):
#     Release Wi-Fi usage.
# 
#     MUST be called in a finally block to avoid leaking wifi_users_mask, meaning must be called even if exception arises in task.
# 
#     This does NOT immediately disconnect Wi-Fi.
#     The Wi-Fi manager decides when to disconnect based on:
#       - wifi_users_mask being zero
#       - keepalive timeout
    if now_ticks is None: # we are using a little stale now_ticks sent by tasks to avoid repeated ticks call -> it will make wifi to turn off few secs earlier probably which is inconsequential
        now_ticks = time.ticks_ms()
    state.wifi_last_activity = now_ticks # mark last wifi activity
    state.wifi_users_mask &= ~task_code # de-register as active user (clear bit)
    
async def wifi_manager_task(ds3231, state, sd):
#     Event-driven Wi-Fi manager.
# 
#     ROLE:
#     - Sole owner of Wi-Fi hardware and wifi_ready_event
#     - Waits for WiFi request (event)
#     - Converts request pulses into stable Wi-Fi state (Connects WiFi)
#     - Broadcasts readiness to all consumers
#     - Stays on while users active OR keepalive timeout
#     - Disconnects when all users done + timeout
    WIFI_SSID = config.WIFI_SSID
    WIFI_PASS = config.WIFI_PASS
    CONNECT_POLL_MS = config.CONNECT_POLL_MS # 0.2s poll for responsiveness
    WIFI_CONNECT_TIMEOUT_MS = config.WIFI_CONNECT_TIMEOUT_MS  # secs per attempt
    
    WIFI_KEEPALIVE_MS = getattr(config, "WIFI_KEEPALIVE_MS", const(15000))
    
    while True:
        try:
            # ============================================================
            # STATE: Wi-Fi OFF / IDLE
            # Wait until at least one task requests Wi-Fi.
            #
            # NOTE:
            # - wifi_request_event is a *pulse*, not a state
            # - Clearing before wait avoids reconnection requests from tasks
            #   that have already just finished because we have just disconnected.
            # ============================================================
            state.wifi_request_event.clear() # clearing before wait [FOR DETAILED EXPLANATION -> SEE DESIGN DOC] 
            await state.wifi_request_event.wait()  # ✅ Event-driven (no polling!)
            
            state.active_tasks_mask |= TASK_BIT_WIFI

            # ============================================================
            # STATE: CONNECTING
            # ============================================================
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
                except Exception as e:
                    # ========================================================
                    # STATE: CONNECT FAILED
                    #
                    # We must wake ALL waiting consumers so they can:
                    #   - stop waiting
                    #   - observe wifi_ok == False
                    #
                    # CRITICAL:
                    # - event.set() only marks waiters runnable
                    # - we MUST yield to allow them to run
                    # ========================================================
                    state.wifi_ok = False # since, wifi connect failed
                    message = str(e) if debug else e.__class__.__name__
                    sd.append_error(E_WIFI_FAIL, message, ts=rtc_tup(ds3231))
                    
                    # Signal waiting tasks (they'll see wifi_ok=False)
                    state.wifi_ready_event.set() # wake waiter tasks so they can see failure and stay set while the waiting tasks run
                    await asyncio.sleep_ms(0) # CRITICAL: Yield control => let all the waiting tasks resume/run, before clearing the flag
                    state.wifi_ready_event.clear() # STATE: wifi is disconnected -> Clear ready signal
                    state.active_tasks_mask &= ~TASK_BIT_WIFI
                    continue # NOTE: finally, block will run anyway before continuing

            # ========================================================
            # STATE: WIFI READY
            #
            # wifi_ready_event now represents STATE:
            #   "Wi-Fi is usable right now"
            #
            # IMPORTANT:
            # - This event stays SET while Wi-Fi is usable
            # - Consumers NEVER clear this event
            # ========================================================
            state.wifi_ready_event.set() # Wake all waiting tasks
        
            # ========================================================
            # STATE: KEEPALIVE
            # Stay here while users are active or within timeout.
            # ========================================================
            while True:
                if state.wifi_users_mask: # any bit is set => non-zero
                    # Users still active, keep WiFi on
                    await asyncio.sleep_ms(WIFI_KEEPALIVE_MS) # Check after WIFI_KEEPALIVE_MS => this also yields control; so that awaiting tasks can run before clearing the wifi ready flag
                else:
                    # No users — check keepalive timeout
                    now = time.ticks_ms()
                    elapsed = time.ticks_diff(now, state.wifi_last_activity)
                    remaining = WIFI_KEEPALIVE_MS - elapsed

                    if remaining <= 0:
                        # Timeout! Disconnect
                        break
                    
                    # Still within keepalive, check again soon
                    await asyncio.sleep_ms(remaining) # => this also yields control; so that awaiting tasks can run before clearing the wifi ready flag
                            
        except Exception as e:
            # Unexpected outer error — log and keep manager alive
            message = str(e) if debug else e.__class__.__name__
            sd.append_error(E_WIFI_TASK, message, ts=rtc_tup(ds3231))
        
        finally: # THIS ALWAYS RUNS
            # ============================================================
            # STATE: DISCONNECTING
            #
            # Ensure state is consistent even if cleanup fails
            # NO awaits after this point!
            #
            # RATIONALE:
            # - No yielding control
            # - Prevent new tasks from seeing Wi-Fi as usable
            # - Disconnect must be atomic from scheduler's perspective
            # ============================================================
            # Disconnect MQTT
            try:
                if state.mqtt_client and state.mqtt_ok:
                    state.mqtt_client.disconnect()
            except Exception:
                pass
            finally:
                state.mqtt_client = None
                state.mqtt_ok = False
            # Disconnect WiFi
            try:
                if state.wlan:
                    if state.wifi_ok:
                        state.wlan.disconnect()
                    state.wlan.active(False)
            except Exception:
                pass
            finally:
                state.wifi_ok = False
                state.wifi_ready_event.clear() # STATE: wifi is disconnected -> Clear ready signal
                
                state.active_tasks_mask &= ~TASK_BIT_WIFI

# --------------------------------------------------------------------
# LCD / button (toggle pages)
# --------------------------------------------------------------------

# --- Helper ---
def _safe_val(val, default=const("--")):
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
        now_ticks = time.ticks_ms()
        # Title
        lcd.text("Room Logger", LABEL_OFFSET, 0)

        # WiFi / MQTT
        # "W:" is static (0 RAM alloc if const), status is dynamic
        lcd.text("W:", LABEL_OFFSET, 8) 
        lcd.text(_LCD_OK if state.wifi_ok else _LCD_XX, 16, 8)
        
        lcd.text("M:", 40, 8)
        lcd.text(_LCD_OK if state.mqtt_ok else _LCD_XX, 56, 8)

        # UPTIME
        # Avoid "U:" + uptime concatenation
        lcd.text("U:", LABEL_OFFSET, 16)
        lcd.text(format_uptime(state, now_ticks), 16, 16)

        # HEARTBEAT
        hb = state.heartbeat
        lcd.text("HB:", LABEL_OFFSET, 24)
        if hb:
            age = time.ticks_diff(now_ticks, hb) // 1000
            # if heartbeat younger than 120s show OK, else show seconds
            lcd.text(_LCD_OK if age <= 90 else str(age), 24, 24)
        else:
            lcd.text(_LCD_XX, 24, 24)

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
        _status = sensors.status
        
        # BMP280
        lcd.text("BMP:", LABEL_OFFSET, 0)
        lcd.text(
            _LCD_XX if not sensors.bmp280 else
            (_LCD_OK if (_status & S_BMP) else _LCD_II),
            VALUE_OFFSET, 0
        )

        # AHT25
        lcd.text("AHT:", LABEL_OFFSET, 8)
        lcd.text(
            _LCD_XX if not sensors.aht25 else
            (_LCD_OK if (_status & S_AHT) else _LCD_II),
            VALUE_OFFSET, 8
        )

        # DS18B20
        lcd.text("DS: ", LABEL_OFFSET, 16)
        lcd.text(
            _LCD_XX if not sensors.ds18b20 else
            (_LCD_OK if (_status & S_DS) else _LCD_II),
            VALUE_OFFSET, 16
        )

        # SHT40
        lcd.text("SHT:", LABEL_OFFSET, 24)
        lcd.text(
            _LCD_XX if not sensors.sht40 else
            (_LCD_OK if (_status & S_SHT) else _LCD_II),
            VALUE_OFFSET, 24
        )

        # APM10
        lcd.text("APM:", LABEL_OFFSET, 32)
        lcd.text(
            _LCD_XX if not sensors.apm else
            (_LCD_OK if (_status & S_APM) else _LCD_II),
            VALUE_OFFSET, 32
        )

        
    # -------------------------
    # PAGE 3: TEMPERATURES
    # -------------------------
    elif page == 3:
        row = state.sensor_row
        owm = state.owm_weather
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
        lcd.text(_safe_val(owm[0]), VALUE_OFFSET, 32)
        
        # Line 5
        lcd.text("appT", LABEL_OFFSET, 40)
        lcd.text(_safe_val(owm[1]), VALUE_OFFSET, 40)

    # -------------------------
    # PAGE 4: HUM / PRESS
    # -------------------------
    elif page == 4:
        row = state.sensor_row
        owm = state.owm_weather
        
        lcd.text("ahtH", LABEL_OFFSET, 0)
        lcd.text(_safe_val(row[4]), VALUE_OFFSET, 0)

        lcd.text("shtH", LABEL_OFFSET, 8)
        lcd.text(_safe_val(row[7]), VALUE_OFFSET, 8)

        lcd.text("outH", LABEL_OFFSET, 16)
        lcd.text(_safe_val(owm[2]), VALUE_OFFSET, 16)

        lcd.text("bmpP", LABEL_OFFSET, 24)
        lcd.text(_safe_val(row[2]), VALUE_OFFSET, 24)

        lcd.text("outP", LABEL_OFFSET, 32)
        lcd.text(_safe_val(owm[3]), VALUE_OFFSET, 32)

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
    state.button_flag.set()   # IRQ-safe  # Wake up the LCD task (# ← "Ring the doorbell")

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
        
        state.active_tasks_mask |= TASK_BIT_LCD1

        # Now LCD is on, start watching for timeout
        while state.lcd_on:
            now_ticks = time.ticks_ms()
            elapsed_ms = time.ticks_diff(now_ticks, state.last_lcd_activity)            
            remaining_ms = timeout_ms - elapsed_ms
            
            if remaining_ms <= 0:
                # Timeout! Turn off LCD
                lcd.clear()
                lcd.power_off() # save power by switching the screen off
                state.lcd_on = False
                break # Exit the inner loop, go back to waiting
            
            # Sleep exactly until the timeout is supposed to happen.
            # If a user presses a button during this sleep, 'state.last_lcd_activity' 
            # will update in the background. When we wake up, we will loop 
            # again, recalculate 'remaining', see that we have more time, 
            # and sleep again.
            await asyncio.sleep_ms(remaining_ms)
        
        # LCD is now off, go back to waiting for next turn-on
        state.active_tasks_mask &= ~TASK_BIT_LCD1

async def button_lcd_task(ds3231, state, sensors, lcd, sd, button1_pin, button2_pin):
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
    
    last_press_time = 0 # last button press time
    
    # ---- at startup after boot - show page 1 ----
    state.last_lcd_activity = time.ticks_ms()
    if not state.lcd_on:
        lcd.power_on() # switch on the screen
        state.lcd_on = True
    state.timeout_event.set()  # Signal timeout watcher that LCD is on
    draw_lcd_page(ds3231, lcd, state, sensors, 1)   # show page 1 immediately
    # --------------------------------------
        
    while True:
        try:
            # Wait FOREVER for button press (no timeout!)
            # This task literally sleeps until a button is pressed
            await state.button_flag.wait() # ThreadSafeFlag Auto-clears automatically
            
            state.active_tasks_mask |= TASK_BIT_LCD2
            
            # 1. Debounce check - ignore if pressed too soon after last press
            now = time.ticks_ms()
            if time.ticks_diff(now, last_press_time) < debounce_ms:
                state.active_tasks_mask &= ~TASK_BIT_LCD2
                continue # Ignore this press
            
            # 2. Capture button presses using mask
            # NOTE: Instead of accessing and making the mask inside the isr, we are reading the
            # button presses directly inside this function to avoid race condtions.
            # Although, it has an issue if button might be released before we read it.
            # But, mechanical buttons are usually held long enough and also we have
            # if button_mask == 0: continue check as a safeguard.
            button_mask = 0 # Which button was pressed (1, 2, both or None) 
            if not button1_pin.value(): # button 1 is pressed -> LOW (0)
                button_mask |= BTN_1 # set bit (Mark Pressed)
            if not button2_pin.value(): # button 2 is pressed -> LOW (0)
                button_mask |= BTN_2 # set bit (Mark Pressed)

            # 3. Act on the mask -> Handle button press
            if button_mask == 0: # CRITICAL
                # Button released too fast, ignore
                state.active_tasks_mask &= ~TASK_BIT_LCD2
                continue
            
            last_press_time = now
            state.last_lcd_activity = now
            
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
                    
        state.active_tasks_mask &= ~TASK_BIT_LCD2
        
        
     
# # --------------------------------------------------------------------
# # Health Log Task
# # -------------------------------------------------------------------
# 
# async def health_log_task(ds3231, state, sd):
#     # Periodically log uptime and memory to health.csv for debugging.
#     interval = getattr(config, "HEALTH_LOG_INTERVAL_SECS", const(3600))  # default 1 hour
# 
#     sd.ensure_health_header()
# 
#     while True:
#         try:
#             uptime = format_uptime(state)
#             mem_free = gc.mem_free()
#             mem_alloc = gc.mem_alloc()
#             #if mem_free < 20_000: print("Memory low:", mem_free)
#             #print(uptime, mem_free, mem_alloc)
#             sd.append_health(rtc_tup(ds3231), uptime, mem_free, mem_alloc)
#         except Exception as e:
#             message = str(e) if debug else e.__class__.__name__
#             sd.append_error(E_HEALTH_FAIL, message, ts=rtc_tup(ds3231))
# 
#         await asyncio.sleep(interval)

 

# --------------------------------------------------------------------
# DS3231 Alarm
# -------------------------------------------------------------------  

def set_next_nmin_alarm(ds3231, n=5, alarm_num=1):
    # set the alarm at the next nth minute of the hour
    # n must be an int b/w 1 to 59, both included
    y, m, d, hh, mm, ss, *_ = ds3231.get_time()

    mm = (mm // n + 1) * n
    if mm >= 60:
        mm = 0
        hh = (hh + 1) % 24

    ds3231.set_alarm(
        alarm_num,
        DS3231Driver.EVERY_DAY,
        hr=hh,
        min=mm,
        sec=0
    )
    
# --- ISR ---
def alarm_isr(pin):
    # Interrupt handler for DS3231 Alarm
    # Called when INT/SQW pin goes LOW.
    # Note: Keep this very minimal (no prints, no allocations, super fast, etc) and ISR-safe!
    state.alarm_flag.set()   # IRQ-safe  # Wake up the Alarm task (# ← "Ring the doorbell")

# ---------------- Async alarm task ----------------
async def alarm_task(ds3231, state, sd):
    # Set alarms, initially
    set_next_nmin_alarm(ds3231, n=5, alarm_num=1)
    set_next_nmin_alarm(ds3231, n=10, alarm_num=2)
    
    # Ensure alarm flags are clean initially
    ds3231.clear_alarm(1)
    ds3231.clear_alarm(2)

    apm_counter = 0
    
    while True:
        await state.alarm_flag.wait() # Wait here until alarm/irq triggers => ThreadsafeFlag auto clears
        
        state.active_tasks_mask |= TASK_BIT_ALM

        fired = ds3231.poll() # alarm mask - which alarm fired (1, 2, both or None) 
        
        if not fired:
            state.active_tasks_mask &= ~TASK_BIT_ALM
            continue
        
        # ---- Alarm 1: sensors every 5 min ----
        if fired & 1:
            state.sensor_evt.set()
            
            # Set alarm
            set_next_nmin_alarm(ds3231, n=5, alarm_num=1)

        # ---- Alarm 2: net every 10 min ----
        if fired & 2:
            state.mqtt_evt.set()
            state.owm_evt.set()

            if config.run_apm:
                if apm_counter == 0:
                    state.apm_evt.set()
                apm_counter = (apm_counter + 1) % 6
                
            # Set alarm
            set_next_nmin_alarm(ds3231, n=10, alarm_num=2)

        state.active_tasks_mask &= ~TASK_BIT_ALM


async def light_sleep_manager(ds3231, state, alarm_pin, btn2_pin):
#     Manages light sleep with proper synchronization.
#     
#     Sleep Policy:
#     - Enter sleep only when active_tasks_mask == 0
#     - Wake sources: DS3231 alarm (ext0) OR button2 (ext1)
#     - After wake, yield immediately to let ISR tasks run
    
    # Configure wake sources (only needs to be done once)
    # Enable wake on LOW level
    esp32.wake_on_ext1(pins=(btn2_pin,), level=esp32.WAKEUP_ALL_LOW)
    # Enable wake on LOW level
    esp32.wake_on_ext0(pin=alarm_pin, level=esp32.WAKEUP_ALL_LOW)
    
    SLEEP_CHECK_INTERVAL_MS = const(500)
    PRE_SLEEP_SETTLE_S = const(1)  # Let tasks finish up
    POST_WAKE_YIELD_MS = const(800)    # Let ISRs fire, set flags and isr tasks run
    
    while True:
        # ============================================================
        # PHASE 1: Wait for idle state
        # ============================================================
        while state.active_tasks_mask:
            # stay awake until all tasks are not IDLE
            await asyncio.sleep_ms(SLEEP_CHECK_INTERVAL_MS) # Yield - allow scheduler to run tasks
          
        # ============================================================
        # PHASE 2: Pre-sleep settle period
        # Give tasks one last chance to finish up or set their bit if active 
        # ============================================================
        await asyncio.sleep(PRE_SLEEP_SETTLE_S)
        
        # ============================================================
        # PHASE 3: Final double check before sleep
        # ============================================================
        if state.active_tasks_mask:
            # A task became active during settle period, going back to waiting/polling
            if debug:
                print("Sleep aborted - task became active")
            continue
        
        # ============================================================
        # PHASE 4: Enter light sleep
        # ============================================================
        if debug:
            t = rtc_tup(ds3231)
            print("→ Sleep at %02d:%02d:%02d" % (t[3], t[4], t[5]))
            time.sleep_ms(100) # let the print happen if debugging => no yielding at this point
        machine.lightsleep()  # Indefinite sleep until wake source
        
        # NOTE: After awaking from lightsleep (either by button or alarm),
        # the code starts just after the above line (means from here) in lighsleep.
        # So, finally will get executed immediately and then ISR's (button or alarm
        # whichever caused the wake) are executed immediately even if this task DOES
        # NOT YIELD. So, isr flags are set even before this task yields and as soon
        # as this task yields after waking, those tasks waiting on these isr flags
        # run. And, so on tasks keep running in event loop until we lightsleep again.
            
            
        # ============================================================
        # PHASE 5: Post-wake handling
        # 
        # CRITICAL SECTION:
        # After wake, we must yield to let:
        #   1. ISR handlers finish (they may still be running)
        #   2. ISR flags propagate to waiting tasks
        #   3. Tasks check their flags and mark themselves as running in the mask
        #
        # The yield timing is crucial for race-free operation.
        # ============================================================
        if debug:
            t = rtc_tup(ds3231)
            print("← Woke at %02d:%02d:%02d" % (t[3], t[4], t[5]))
        # NOTE:
        # DS3231 alarm flags are intentionally NOT cleared here after wake.
        # ds3231.poll() is the single authority that:
        #   - reads which alarm(s) fired
        #   - clears the corresponding alarm flag(s)
        #
        # Clearing alarms before poll() would lose edge information
        # (we would not know which alarm fired).
        #
        # Invariant:
        #   poll() MUST be called exactly once per INT event,
        #   and it MUST clear the alarm flags internally.
        
        
        # CRITICAL: Yield immediately to process wake events (button/alarm)
        await asyncio.sleep_ms(POST_WAKE_YIELD_MS)
        # At this point:
        # - ISRs have set their ThreadSafeFlags
        # - Waiting tasks have woken and set their bit in mask to mark themeselves as running
        # - active_tasks_mask reflects the true busy state
        #
        # Loop back to Phase 1 to wait for tasks to complete
        
        

# --------------------------------------------------------------------
# Main setup
# --------------------------------------------------------------------


async def main():
    gc.collect()
    
    # Init hardware/drivers once
    
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
    # show status on lcd at start-up
    state.lcd_on = True
    lcd.text("Booting...", 0, 0)
    lcd.show()
    
    # --- DS3231 ---
    # Initialize i2c bus and alarm pin
    
    # NOTE: Multiple I2C instances: creating separate I2C(...) objects from different modules
    # fragments the heap. I now use a single shared I2C instance created once and passed to
    # sensors or returned from an internal getter.
    
    i2c = machine.I2C(0, scl=machine.Pin(config.sclPIN), sda=machine.Pin(config.sdaPIN), freq=100000)
    ds3231 = DS3231Driver(i2c)
    alarmPIN = machine.Pin(config.alarmPIN, machine.Pin.IN, machine.Pin.PULL_UP)
           
    # attach irq to alarm pin
    # NOTE: I don't know why but if we move it to alarm_task before its while loop
    # starts, then the alarm task or even AWAKENING from alarm pin does not work.
    alarmPIN.irq(handler=alarm_isr, trigger=machine.Pin.IRQ_FALLING)
    
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
    
    # --- Buttons ---
    button1 = machine.Pin(config.BUTTON_1, machine.Pin.IN, machine.Pin.PULL_UP) # button next
    button2 = machine.Pin(config.BUTTON_2, machine.Pin.IN, machine.Pin.PULL_UP) # button prev
       
    # log reset cause (optional)
    sd.append_error(E_RESET, state.rst_cause, ts=rtc_tup(ds3231), force=True)
    
    
    # ---- One-time boot readings ----
    state.sensor_evt.set()
    state.mqtt_evt.set()
    state.owm_evt.set()
    if config.run_apm:
        state.apm_evt.set()
    
    
    # --- Start tasks ---
    asyncio.create_task(alarm_task(ds3231, state, sd))
    asyncio.create_task(wifi_manager_task(ds3231, state, sd))
    asyncio.create_task(mqtt_task(ds3231, state, sd))
    asyncio.create_task(owm_task(ds3231, state, owm_client, sd))
    if config.run_apm:
        asyncio.create_task(apm10_task(ds3231, state, sensors, sd))
    elif debug:
        print("skipping apm10_task") # task not created
    asyncio.create_task(sensor_and_log_task(ds3231, state, sensors, sd))
    asyncio.create_task(button_lcd_task(ds3231, state, sensors, lcd, sd, button1, button2))
    asyncio.create_task(lcd_timeout_watcher(state, lcd))
    asyncio.create_task(light_sleep_manager(ds3231, state, alarmPIN, button2))
#     if config.debug_mem:
#         asyncio.create_task(health_log_task(ds3231, state, sd))


    # Keep loop alive
    while True:
        # Keep loop alive forever
        await asyncio.Event().wait()



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
