# AHT25 sensor driver

import aht  # Import the aht library
import config

class AHT25:
    __slots__ = ("sensor",)
    
    def __init__(self, i2c, i2c_address=0x38):
        """
        Initializes the AHT25 sensor.

        :param i2c: I2C object
        :param i2c_address: I2C address of the AHT25 sensor (default address is 0x38)
        """
        try:            
            self.sensor = aht.AHT2x(i2c, address=i2c_address, crc=True)
            if config.debug:
                print('aht25 initialized')
        except Exception as e:
            raise e # raise if initialization failed to let the caller know about it

    def read_measurements(self):
        """
        Reads temperature and humidity measurements.

        :return: A tuple (temperature in °C, relative humidity in %)
        """
        try:
            if self.sensor.is_ready:
                return self.sensor.temperature, self.sensor.humidity
            else:
                return None, None
        except Exception as e:
            return None, None

    def reset_sensor(self):
        """
        Performs a reset on the AHT25 sensor.
        """
        self.sensor.reset()

