# APM10 sensor driver based on apm10.py library

import time

import apm10
import config

'''
The basic functionality of the sensor is as following:
- enter the sensor's measurement mode, now sensor is active and you can take a reading any time you want
- keept taking readings as long as you want
- once done, exit the sensor's measurement mode, now the sensor is inactive and you cant take readings anymore

NOTE: APM10 has a minimum 1 sec data update period; so take readings at an interval greater than this.
'''

class APM10Driver:
    __slots__ = ("sensor",)
    
    def __init__(self, i2c, i2c_address=0x08):
        """
        Initialize the APM10 sensor.
        :param i2c: I2C object
        :param i2c_address: I2C address of the APM10 sensor (default address is 0x08)
        """
        try:            
            # Initialize the APM10 sensor
            self.sensor = apm10.APM10(i2c, address=i2c_address)
            # Sensor connection status
            if not self.check_connection():
                raise RuntimeError("APM10 sensor not connected. Please check wiring.")
            if config.debug:
                print('APM10 sensor initialized')
        except Exception as e:
            raise e # raise if initialization failed to let the caller know about it
    
    def check_connection(self):
        """Check if the sensor is properly connected and communicating."""
        if self.sensor.is_connected():
            return True
        else:
            return False
        
    def enter_measurement_mode(self):
        """Enter the sensor's measurement mode."""
        if config.debug:
            print("Starting the APM10 sensor...")
        self.sensor.enter_measurement_mode()
        time.sleep(0.5)  # Wait for the sensor to stabilize

    def exit_measurement_mode(self):
        """Exit the sensor's measurement process."""
        if config.debug:
            print("Stopping the APM10 sensor...")
        self.sensor.exit_measurement_mode()

    def read_measurements(self):
        """
        Read the air quality data from the sensor.
        :return: a tuple with (PM1.0, PM2.5, PM10) values in µg/m³.
        """
        try:
            return self.sensor.read_data() # pm1_0, pm2_5, pm10
        except Exception as e:
            return None, None, None

# Example Usage
if __name__ == "__main__":
    try:
        from machine import I2C, Pin
        # Initialize i2c bus
        i2c = I2C(0, scl=Pin(config.sclPIN), sda=Pin(config.sdaPIN), freq=100000)
        
        apm = APM10Driver(i2c)
        if apm:
            # Enter the sensor into the measurement mode
            apm.enter_measurement_mode()
            time.sleep(20)  # warm-up for stabilization

            # Continuously read air quality data
            while True:
                air_quality = apm.read_measurements()
                print(f"PM1.0: {air_quality[0]} µg/m³, PM2.5: {air_quality[1]} µg/m³, PM10: {air_quality[2]} µg/m³")
                time.sleep(2)  # Read every 10 seconds

    except Exception as e:
        print("Error: ", e)
        
    finally:
        if apm:
            # Exit the sensor from the measurement mode
            apm.exit_measurement_mode()

