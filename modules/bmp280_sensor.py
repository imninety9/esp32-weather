# BMP280 sensor driver


import bmp280
import config

class BMP280Driver:
    __slots__ = ("sensor",)
    
    def __init__(self, i2c, i2c_address=0x76, use_case=bmp280.BMP280_CASE_WEATHER):
        """
        Initializes the BMP280 sensor.

        :param i2c: I2C object
        :param i2c_address: I2C address of the BMP280 sensor (default address is 0x76)
        :use_case: use case of bmp280
        # choose the preferred use case:
        # all available use cases-
        #BMP280_CASE_WEATHER
        #BMP280_CASE_HANDHELD_LOW
        #BMP280_CASE_HANDHELD_DYN
        #BMP280_CASE_WEATHER
        #BMP280_CASE_FLOOR
        #BMP280_CASE_DROP
        #BMP280_CASE_INDOOR
        """
        try:            
            self.sensor = bmp280.BMP280(i2c, addr=i2c_address, use_case=use_case)
            if config.debug:
                print('bmp280 initialized')
        except Exception as e:
            raise e # raise if initialization failed to let the caller know about it

    def read_measurements(self):
        """
        Reads temperature and pressure measurements.

        :return: A tuple (temperature in °C, pressure in Pa)
        """
        try:
            return self.sensor.temperature, self.sensor.pressure
        except Exception as e:
            return None, None
        
    def reset_sensor(self):
        """
        Performs a reset on the BMP280 sensor.
        """
        self.sensor.reset()

