# DS18B20 sensor driver

import onewire
import ds18x20
import time
import config

class DS18B20:
    __slots__ = ("ds", "roms")
    
    def __init__(self, ds_pin):
        """
        Initialize the DS18B20 sensor(s).
        :param ds_pin: GPIO pin object where the DATA line is connected.
        """
        try:            
            self.ds = ds18x20.DS18X20(onewire.OneWire(ds_pin))
            self.roms = self.ds.scan()  # Scan for all ds18x20 devices on the bus and save their rom addresses
            if not self.roms:
                raise RuntimeError("No DS18B20 sensors found. Check connections.")
            if config.debug:
                print('ds18b20 initialized')
        except Exception as e:
            raise e # raise if initialization failed to let the caller know about it

    def read_temp(self, rom=None):
        """
        Read temperature from a specific sensor by its ROM.
        :param rom: ROM address of the sensor (bytes). If rom is None, read the first sensor or the only sensor.
        :return: Temperature in Celsius or None on error.
        """
        try:
            if rom is None:
                rom = self.roms[0]
            # Note that you must execute the convert_temp() function to
            # initiate a temperature reading, then wait at least 750ms before
            # reading the value.
            self.ds.convert_temp()
            time.sleep(1)  # Wait for conversion (at least 750ms for 12-bit resolution)
            return self.ds.read_temp(rom)
        except Exception as e:
            return None

    def scan(self):
        """
        Rescan the bus for sensors and update the ROM list.
        :return: Updated list of sensor ROMs.
        """
        self.roms = self.ds.scan()
        return self.roms
    
# Example Usage
if __name__ == "__main__":
    try:
        from machine import Pin
        # initialize pin
        ds_pin = Pin(config.ONEWIRE_PIN)
        
        ds18b20 = DS18B20(ds_pin)
        if ds18b20:
            while True:
                print(ds18b20.read_temp())
                time.sleep(10)  # Read every 10 seconds

    except Exception as e:
        print("Error: ", e)
        