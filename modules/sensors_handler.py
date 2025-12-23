# sensor manager class to handle all sensors

_NONE2 = (None, None) # micro-optimization - avoid creating it repeatedly

# -------------------------------------------------
# Sensor health bitmask
#
# Bit meaning:
#   1 = sensor present AND last read succeeded
#   0 = sensor missing OR last read failed
#
# Ownership rules (IMPORTANT):
#   - Sensors.read_measurements():
#       owns BMP280, AHT25, DS18B20, SHT40 bits
#   - apm10_task():
#       owns APM bit
#   - No other task may modify these bits
#
# Rationale:
#   Single-writer per bit avoids logical races
#
# SENSORS STATUS: Full Bitmask Implementation
# 1 = OK; 0 = FAIL OR NOT PRESENT
# How to interpret the bit (important):
# if sensors.status & S_BMP:
#     # BMP280 present AND last read succeeded
# else:
#     # either:
#     #   - sensor missing
#     #   - OR last read failed
#     
# If you need to distinguish missing vs failed, you already have:
# sensors.bmp280 is None   # missing
# sensors.bmp280 exists    # present but possibly failing

# sensor bit positions (powers of two)
S_BMP = 1 << 0   # 00001 = 1
S_AHT = 1 << 1   # 00010 = 2
S_DS  = 1 << 2   # 00100 = 4
S_SHT = 1 << 3   # 01000 = 8
S_APM = 1 << 4   # 10000 = 16

ALL_OK = S_BMP | S_AHT | S_DS | S_SHT | S_APM  # 11111 = 31

class Sensors:
    __slots__ = ("aht25", "bmp280", "ds18b20", "sht40", "apm", "status", "measurements")
     
    # Initializes all the sensors.

    # :param i2c: I2C bus object
    # :param softi2c: Soft I2C bus object
    # :param onewirePin: GPIO Pin object for 1-wire communication
    # :param run_apm: should initialize apm10 sensor or not
    def __init__(self, i2c=None,
                 softi2c=None,
                 onewirePin=None,
                 run_apm=False):    
        self.aht25 = None
        self.bmp280 = None
        self.ds18b20 = None
        self.sht40 = None
        self.apm = None
        
        self.status = 0 # start with all bits cleared assuming no sensor is ok
        
        self.measurements = [None] * 4 # pre-allocate [bmp, aht, ds18b20, sht]
        
        if i2c is not None:
            try:
                from aht25_sensor import AHT25 # lazy import
                self.aht25 = AHT25(i2c, i2c_address=0x38)
                self.status |= S_AHT      # set bit (Mark Presence and assumed healthy initially)
            except:
                self.aht25 = None # not present
            try:
                from bmp280_sensor import BMP280Driver
                self.bmp280 = BMP280Driver(i2c, i2c_address=0x76)
                self.status |= S_BMP        # set bit (Mark Presence and assumed healthy initially)
            except:
                self.bmp280 = None # not present
            if run_apm:
                try:
                    from apm10_sensor_i2c import APM10Driver # lazy import
                    self.apm = APM10Driver(i2c, i2c_address=0x08)
                    self.status |= S_APM        # set bit (Mark Presence and assumed healthy initially)
                except:
                    self.apm = None # not present
        if softi2c is not None:
            try:
                from sht40_sensor import SHT40 # lazy import
                self.sht40 = SHT40(softi2c, i2c_address=0x44)
                self.status |= S_SHT        # set bit (Mark Presence and assumed healthy initially)
            except:
                self.sht40 = None # not present
        if onewirePin is not None:
            try:
                from ds18b20_sensor import DS18B20 # lazy import
                self.ds18b20 = DS18B20(onewirePin)
                self.status |= S_DS        # set bit (Mark Presence and assumed healthy initially)
            except:
                self.ds18b20 = None # not present
         
    # Read Sensor measurements
    def read_measurements(self):
        try:
            if self.bmp280:
                self.measurements[0] = self.bmp280.read_measurements()
                self.status |= S_BMP          # success → healthy
            else:
                self.measurements[0] = _NONE2 # not present
        except Exception:
            self.measurements[0] = _NONE2
            self.status &= ~S_BMP         # failure → unhealthy
        try:
            if self.aht25:
                self.measurements[1] = self.aht25.read_measurements()
                self.status |= S_AHT          # success → healthy
            else:
                self.measurements[1] = _NONE2 # not present
        except Exception:
            self.measurements[1] = _NONE2
            self.status &= ~S_ANT         # failure → unhealthy
        try:
            if self.ds18b20:
                self.measurements[2] = self.ds18b20.read_temp()
                self.status |= S_DS          # success → healthy
            else:
                self.measurements[2] = None # not present
        except Exception:
            self.measurements[2] = None
            self.status &= ~S_DS         # failure → unhealthy
        try:
            if self.sht40:
                self.measurements[3] = self.sht40.read_measurements()
                self.status |= S_SHT          # success → healthy
            else:
                self.measurements[3] = _NONE2 # not present
        except Exception:
            self.measurements[3] = _NONE2
            self.status &= ~S_SHT         # failure → unhealthy
            
        return self.measurements
        
            
# Example Usage
if __name__ == "__main__":
    try:
        import config
        import time
        from machine import Pin, I2C, SoftI2C
        
        # initialize i2c, softi2c, onewire pin
        i2c = I2C(0, scl=Pin(config.sclPIN), sda=Pin(config.sdaPIN), freq=100000)
        softi2c = SoftI2C(scl=Pin(config.soft_sclPIN), sda=Pin(config.soft_sdaPIN), freq=100000)
        onewirePin = Pin(config.ONEWIRE_PIN)
        
        sensors = Sensors(i2c=i2c,
                          softi2c=None,
                          onewirePin=onewirePin)

        
        while True:
            readings = sensors.read_measurements()
            print(readings)
            
            time.sleep(10)
    
    except Exception as e:
        print('Err: ', e)
       

       
