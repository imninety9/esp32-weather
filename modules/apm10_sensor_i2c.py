# APM10 sensor driver based on apm10.py library

import time

import apm10
import config

# The basic functionality of the sensor is as following:
# - enter the sensor's measurement mode, now sensor is active and you can take a reading any time you want
# - keept taking readings as long as you want
# - once done, exit the sensor's measurement mode, now the sensor is inactive and you cant take readings anymore
# 
# NOTE: APM10 has a minimum 1 sec data update period; so take readings at an interval greater than this.

class APM10Driver:
    __slots__ = ("sensor", "_running")
    
    # Initialize the APM10 sensor.
    # :param i2c: I2C object
    # :param i2c_address: I2C address of the APM10 sensor (default address is 0x08)
    def __init__(self, i2c, i2c_address=0x08):
        try:            
            # Initialize the APM10 sensor
            self.sensor = apm10.APM10(i2c, address=i2c_address)
            # Sensor connection status
            if not self.check_connection():
                raise RuntimeError("APM10 not detected.")
            
            # if apm10 is in measurement mode or not
            self._running = False # Prevents accidental double-starts
            
            if config.debug:
                print('APM10 ok')
        except Exception:
            raise # raise if initialization failed to let the caller know about it
    
    # Check if the sensor is properly connected and communicating
    def check_connection(self):
        return self.sensor.is_connected() # True/False
        
    # Enter the sensor's measurement mode.
    def enter_measurement_mode(self):
        if not self._running:
            if config.debug:
                print("Starting APM10...")
            self.sensor.enter_measurement_mode()
            self._running = True
            time.sleep(0.5)  # Wait for the sensor to stabilize

    # Exit the sensor's measurement process.
    def exit_measurement_mode(self):
        if self._running:
            if config.debug:
                print("Stopping APM10...")
            self.sensor.exit_measurement_mode()
            self._running = False

    # Read the air quality data from the sensor.
    # :return: a tuple with (PM1.0, PM2.5, PM10) values in µg/m³
    def read_measurements(self):
        return self.sensor.read_data() # pm1_0, pm2_5, pm10

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
                aq = apm.read_measurements()
                print(f"PM1.0: {aq[0]}, PM2.5: {aq[1]}, PM10: {aq[2]} [in µg/m³]")
                time.sleep(2)  # Read every 10 seconds

    except Exception as e:
        print("Err: ", e)
        
    finally:
        if apm:
            # Exit the sensor from the measurement mode
            apm.exit_measurement_mode()
