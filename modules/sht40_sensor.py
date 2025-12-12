# SHT40 sensor driver based on sht4x.py library

import sht4x  # Import the sht4x library
import config

class SHT40:
    __slots__ = ("sensor", "mode")
    
    def __init__(self, i2c, i2c_address=0x44):
        """
        Initializes the SHT40 sensor.

        :param i2c: I2C object
        :param i2c_address: I2C address of the SHT40 sensor (default address is 0x44)
        """
        try:            
            self.sensor = sht4x.SHT4X(i2c, address=i2c_address)
            self.mode = 1  # Default: No heater, high precision [by default this is the mode]
            if config.debug:
                print('sht40 initialized')
        except Exception as e:
            raise e # raise if initialization failed to let the caller know about it
        
    def set_mode(self, mode=1):
        """
        Sets the operation mode of the sensor.
        
                                    MODE                                   |   COMMAND   |   SENSOR MEASUREMENT DURATION (sec)
        1.  No Heater, High Precision (DEFAULT)                                          0xFD              0.01
        2.  No Heater, Medium Precision                                         0xF6              0.005
        3.  No Heater, Low Precision                                            0xE0              0.002
        4.  With Heater(Power 200mW), ON Time(1 sec), High Precision            0x39              1.1
        5.  With Heater(Power 200mW), ON Time(0.1 sec), High Precision          0x32              0.11
        6.  With Heater(Power 110mW), ON Time(1 sec), High Precision            0x2F              1.1
        7.  With Heater(Power 110mW), ON Time(0.1 sec), High Precision          0x24              0.11
        8.  With Heater(Power 20mW), ON Time(1 sec), High Precision             0x1E              1.1
        9.  With Heater(Power 20mW), ON Time(0.1 sec), High Precision           0x15              0.11
        NOTE: - Use heater very sparingly and only if absolutely needed because its duty cycle is only 10%

        :param mode: an 'int' from 1 to 9 (both included) to set the respective mode [see the table above for the corresponding mode].
        """
        if 9 < mode < 1:
            raise ValueError("Invalid mode. Choose from 1 to 9 corresponding to the above table.")
        if mode==1:
            self.sensor.temperature_precision = sht4x.HIGH_PRECISION
        elif mode==2:
            self.sensor.temperature_precision = sht4x.MEDIUM_PRECISION
        elif mode==3:
            self.sensor.temperature_precision = sht4x.LOW_PRECISION
        elif mode==4:
            self.sensor.heat_time = sht4x.TEMP_1
            self.sensor.heater_power = sht4x.HEATER200mW
        elif mode==5:
            self.sensor.heat_time = sht4x.TEMP_0_1
            self.sensor.heater_power = sht4x.HEATER200mW
        elif mode==6:
            self.sensor.heat_time = sht4x.TEMP_1
            self.sensor.heater_power = sht4x.HEATER110mW
        elif mode==7:
            self.sensor.heat_time = sht4x.TEMP_0_1
            self.sensor.heater_power = sht4x.HEATER110mW
        elif mode==8:
            self.sensor.heat_time = sht4x.TEMP_1
            self.sensor.heater_power = sht4x.HEATER20mW
        elif mode==9:
            self.sensor.heat_time = sht4x.TEMP_0_1
            self.sensor.heater_power = sht4x.HEATER20mW
            
        self.mode = mode
        
    def read_measurements(self):
        """
        Reads temperature and humidity measurements.

        :return: A tuple (temperature in °C, relative humidity in %)
        """
        try:
            return self.sensor.measurements # temperature, humidity
        except Exception as e:
            return None, None
        
    def reset_sensor(self):
        """
        Performs a reset on the SHT40 sensor.
        """
        try:
            self.sensor.reset()
        except Exception as e:
            if config.debug:
                print(f"Error resetting the sensor: {e}")
