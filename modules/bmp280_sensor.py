# BMP280 sensor driver


import bmp280
import config

class BMP280Driver:
    __slots__ = ("sensor",)
    
    # Initializes the BMP280 sensor.

    # :param i2c: I2C object
    # :param i2c_address: I2C address of the BMP280 sensor (default address is 0x76)
    # :use_case: use case of bmp280
    
    # choose the preferred use case:
    # all available use cases-
    #BMP280_CASE_WEATHER
    #BMP280_CASE_HANDHELD_LOW
    #BMP280_CASE_HANDHELD_DYN
    #BMP280_CASE_WEATHER
    #BMP280_CASE_FLOOR
    #BMP280_CASE_DROP
    #BMP280_CASE_INDOOR
    def __init__(self, i2c, i2c_address=0x76, use_case=bmp280.BMP280_CASE_WEATHER):        
        try:            
            self.sensor = bmp280.BMP280(i2c, addr=i2c_address, use_case=use_case)
            if config.debug:
                print('bmp280 ok')
        except Exception:
            raise # raise if initialization failed to let the caller know about it

    # Reads temperature and pressure measurements.
    # :return: A tuple (temperature in °C, pressure in Pa)
    def read_measurements(self):
        return self.sensor.temperature, self.sensor.pressure
    
    # Sleep mode to save power
    def sleep(self):
        self.sensor.sleep()
        
    # Performs a reset on the BMP280 sensor.
    def reset_sensor(self):
        self.sensor.reset()
