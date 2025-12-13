# configuration file

from micropython import const

debug = True
debug_mem = False

# SENSOR UPDATE INTERVAL
SENSOR_INTERVAL_SECS = const(300) # [interval between weather readings update]
OWM_FETCH_INTERVAL_SECS = const(600)
APM10_INTERVAL_SECS = const(3600)
HEALTH_LOG_INTERVAL_SECS = const(30)

# WIFI PARAMETERS
# List of WiFi networks with priorities (higher the better) [pre-sorted]
WIFI_SSID = ".."
WIFI_PASS = ".."



# PHYSICAL PARAMETRS [GPIO PINS]
# I2C Pins
sdaPIN = const(21)
sclPIN = const(22)

# DS3231 rtc INT/SQW Pin
alarmPIN = const(27)

# Software I2C pins (used by sht40 sensor module)
soft_sdaPIN = const(25)
soft_sclPIN = const(26)

# SPI Pins
SPI_PIN_MISO = const(19)
SPI_PIN_MOSI = const(23)
SPI_PIN_SCK = const(18)
SPI_PIN_CS = const(5) # You can choose any available GPIO pin for CS

SD_MOUNT_POINT = '/sd'

# Nokia 5510 LCD Pins (uses the hspi bus)
# Nokia 5510 LCD Pin Function ESP32 GPIO	Notes
LCD_RST = const(16) # Reset
LCD_CE = const(17) # Chip Enable (CS)
LCD_DC = const(32) # Data/Command
LCD_DIN = const(13) # Data In (MOSI) [SPI MOSI]
LCD_CLK = const(14) # Clock [SPI SCK]

# Onewire pin (for onewire communication protocol devices like ds18b20)
ONEWIRE_PIN = const(4)

# Input Button Pin
BUTTON_1 = const(35) # (LOW when pressed, pulled HIGH via 10k ohm resistor)
BUTTON_2 = const(34) # (LOW when pressed, pulled HIGH via 10k ohm resistor)


# lat, long = ghitorni, new delhi
latitude = const(..)
longitude = const(..)

# OPENWEATHERMAP API PARAMETERS
owm_api_key = '..'
OWM_URL = f"http://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={owm_api_key}&units=metric"
OWM_URL_AQI = f'http://api.openweathermap.org/data/2.5/air_pollution?lat={latitude}&lon={longitude}&appid={owm_api_key}'
OWM_HEADERS = {}
'''
# TOMORROW.IO API PARAMETERS
tom_api_key = '..'
TOM_URL = f'http://api.tomorrow.io/v4/weather/realtime?location={latitude}%2C{longitude}&units=metric&apikey={tom_api_key}' # %2C represents a comma(,) here
TOM_HEADERS = {"accept": "application/json"}
# Note: we are using unsecure http request rather than https to reduce computation on esp32; as the information is not critical
'''


# MQTT PARAMETERS
client_id = b"esp32-weather"
user = b".."
password = b".."
server = "io.adafruit.com"
port = 1883
# feeds
aht_temp = "raspi8751/feeds/aht_temp"
aht_hum = "raspi8751/feeds/aht_hum"
bmp_temp = "raspi8751/feeds/bmp_temp"
bmp_press = "raspi8751/feeds/bmp_press"
ds18b20_temp = "raspi8751/feeds/status"
out_temp = "raspi8751/feeds/out_temp"
out_feels_like_temp = "raspi8751/feeds/out_feels_like_temp"
out_hum = "raspi8751/feeds/out_hum"
out_press = "raspi8751/feeds/out_press"
status = "raspi8751/feeds/status"
feeds = [
        bmp_temp,
        bmp_press,
        aht_temp,
        aht_hum,
        ds18b20_temp,
        out_temp,
        out_feels_like_temp,
        out_hum,
        out_press
        ]

KEEP_ALIVE_INTERVAL = SENSOR_INTERVAL_SECS + 60 # sec
LAST_WILL_MESSAGE = b"ESP32 disconnected unexpectedly!"
    


# Misc
csv_fields = ["timestamp",
              "bmp_temp","bmp_press",
              "aht_temp","aht_hum",
              "ds18b20_temp",
              "sht_temp","sht_hum",
              "pm1_0", "pm2_5", "pm10",
              "owm_temp","owm_temp_feels_like","owm_hum","owm_press",
              "owm_pm2_5","owm_pm10","owm_no2"
              ]


# wifi, mqtt backoff
WIFI_BASE_BACKOFF = const(30)   # seconds
WIFI_MAX_BACKOFF = const(3600) # cap (e.g. 30 min)
MQTT_BASE_BACKOFF = const(15) # sec
MQTT_MAX_BACKOFF  = const(1800) # sec
BACKOFF_JITTER_PCT =  const(0.15)  # ±15%
CONNECT_POLL =  const(0.2)  # 0.2s poll for responsiveness
WIFI_CONNECT_TIMEOUT = const(10)  # secs per attempt


# Logging
DEFAULT_DATA_PATH = "/sd/weather.csv"
DEFAULT_ERROR_PATH = "/sd/errors.log"
DEFAULT_HEALTH_PATH = "/sd/health.csv" # Health log (uptime, memory)
DEFAULT_ERROR_ROW_LIMIT = const(500)  #
ERROR_AVG_ROW = const(60) # bytes
DEFAULT_ERROR_RETENTION = const(5) # number of files
DEFAULT_ERROR_THROTTLE_MS = const(3600000)        # milliseconds


# watchdog
HARD_WDT_TIMEOUT_MS = const(120000) # 120 s (keep it significantly larger than the largest blocking window in the code)
SOFT_WDT_TIMEOUT_MS = const(1200000) # 20 min
HARD_WDT_FEED_INTERVAL_MS = const(20000)


