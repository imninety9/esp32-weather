# sensor manager class to handle all sensors

class Sensors:
    __slots__ = ("aht25", "bmp280", "ds18b20", "sht40", "apm")
     
    def __init__(self, i2c=None,
                 softi2c=None,
                 onewirePin=None):
        """
        Initializes all the sensors.

        :param i2c: I2C bus object
        :param softi2c: Soft I2C bus object
        :param onewirePin: GPIO Pin object for 1-wire communication
        """     
        self.aht25 = None
        self.bmp280 = None
        self.ds18b20 = None
        self.sht40 = None
        self.apm = None
        
        if i2c is not None:
            try:
                from aht25_sensor import AHT25 # lazy import
                self.aht25 = AHT25(i2c, i2c_address=0x38)
            except:
                self.aht25 = None
            try:
                from bmp280_sensor import BMP280Driver
                self.bmp280 = BMP280Driver(i2c, i2c_address=0x76)
            except:
                self.bmp280 = None
            try:
                from apm10_sensor_i2c import APM10Driver # lazy import
                self.apm = APM10Driver(i2c, i2c_address=0x08)
            except:
                self.apm = None
        if softi2c is not None:
            try:
                from sht40_sensor import SHT40 # lazy import
                self.sht40 = SHT40(softi2c, i2c_address=0x44)
            except:
                self.sht40 = None
        if onewirePin is not None:
            try:
                from ds18b20_sensor import DS18B20 # lazy import
                self.ds18b20 = DS18B20(onewirePin)
            except:
                self.ds18b20 = None
            
    def read_measurements(self):
        """
        Read Sensor measurements.
        """
        measurements = []
        if self.bmp280:
            measurements.append(self.bmp280.read_measurements())
        else:
            measurements.append((None, None))
        if self.aht25:
            measurements.append(self.aht25.read_measurements())
        else:
            measurements.append((None, None))
        if self.ds18b20:
            measurements.append(self.ds18b20.read_temp())
        else:
            measurements.append(None)
        if self.sht40:
            measurements.append(self.sht40.read_measurements())
        else:
            measurements.append((None, None))
        
        return measurements
            
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
        print('Error occured: ', e)
       

       
