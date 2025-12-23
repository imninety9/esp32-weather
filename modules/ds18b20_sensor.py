# DS18B20 sensor driver

import onewire
import ds18x20
import time
import config

# Note: As we can connect and use  multiple DS18B20 sensors on the same data line,
# so if using multiple DS18B20 sensors, ensure unique ROM addresses and handle them
# accordingly in the code.
# The ROM address is a unique 64-bit identifier assigned to every DS18B20 sensor.
# It ensures that each sensor connected to a single 1-Wire bus can be individually
# identified and addressed.
# When you scan the 1-Wire bus using ds.scan() in the MicroPython code, the returned
# roms list contains the unique ROM addresses of all detected DS18B20 sensors.
# 
# Note: Note that you must execute the convert_temp() function to initiate a
# temperature reading, then wait at least 750ms (for 12-bit resolution) before reading the value.
# 
# Resolution Adjustment: The DS18B20 supports different resolutions (9, 10, 11, and 12 bits).
# For faster readings, you can lower the resolution. By default, it uses 12 bits.
# 
# Resolution table with the wait time taken for conversion and precision:
# 
# 9-bit: 93.75ms - 0.5°C
# 10-bit: 187.5ms - 0.25°C
# 11-bit: 375ms - 0.125°C
# 12-bit: 750ms - 0.0625°C

class DS18B20:
    __slots__ = ("ds", "roms")
    
    # Initialize the DS18B20 sensor(s).
    # :param ds_pin: GPIO pin object where the DATA line is connected.
    def __init__(self, ds_pin):
        try:            
            self.ds = ds18x20.DS18X20(onewire.OneWire(ds_pin))
            self.roms = self.ds.scan()  # Scan for all ds18x20 devices on the bus and save their rom addresses
            if not self.roms:
                raise RuntimeError("No DS18B20 found.")
            if config.debug:
                print('ds18b20 ok')
        except Exception:
            raise # raise if initialization failed to let the caller know about it

    # Read temperature from a specific sensor by its ROM.
    # :param rom: ROM address of the sensor (bytes). If rom is None, read the first sensor or the only sensor.
    # :return: Temperature in Celsius or None on error.
    def read_temp(self, rom=None):
        if rom is None:
            rom = self.roms[0]
        # Note that you must execute the convert_temp() function to
        # initiate a temperature reading, then wait at least 750ms before
        # reading the value.
        self.ds.convert_temp()
        time.sleep(1)  # Wait for conversion (at least 750ms for 12-bit resolution)
        return self.ds.read_temp(rom)
    
    # Read temperatures from all connected sensors.
    # :return: list of temperatures in same order as self.roms
    def read_all_temps(self):
        # Note that you must execute the convert_temp() function to
        # initiate a temperature reading, then wait at least 750ms before
        # reading the value.
        self.ds.convert_temp()
        time.sleep(1)  # Wait for conversion (at least 750ms for 12-bit resolution)
        temps = []
        for rom in self.roms:
            temps.append(self.ds.read_temp(rom))
        return temps
        
    # Get the number of connected sensors.
    # :return: Number of sensors detected.
    def get_sensor_count(self):
        return len(self.roms)
        
    # Rescan the bus for sensors and update the ROM list.
    # :return: Updated list of sensor ROMs.
    def scan(self):
        self.roms = self.ds.scan()
        return self.roms
    
    # set or get resolution of a sensor. If resolution_bits is None it returns the resolution of the sensor.
    # :param rom: ROM address of the sensor (bytes). If rom is None, read the first sensor or the only sensor.
    # :param resolution_bits: the resolution you want to set [9 to 12, both included]. If resolution_bits is None it gets and returns the resolution of the sensor.
    # :return: resolution_bits
    def resolution(self, rom=None, resolution_bits=None):
        # NOTE: The value set is not permanent and will be lost after a power cycle.
        #       After a power cycle it is reset to 12 which is the default set in sensor's EEPROM.
        #       If you want to make it permanent use copy scratch pad: Copies the
        #       scratchpad contents (TH, TL, and configuration register) to the sensor's EEPROM.
        #       EEPROM changes are stored permanently and will persist after power cycles.
        #       Code for this is:
        #                  # Save the changes to EEPROM
        #                  self.ds.ow.reset(True)
        #                  self.ds.ow.select_rom(rom)
        #                  self.ds.ow.writebyte(0x48)  # COPY SCRATCHPAD command
        if rom is None:
            rom = self.roms[0]
        cfg = bytearray(3)
        try:
            if resolution_bits is not None and 9 <= resolution_bits <= 12:
                cfg[2] = ((resolution_bits - 9) << 5) | 0x1f
                
                # cfg = {
                #     9: b'\x00\x00\x1f',   # 00011111
                #     10: b'\x00\x00\x3f',  # 00111111
                #     11: b'\x00\x00\x5f',  # 01011111
                #     12: b'\x00\x00\x7f',  # 01111111
                # }
                
                self.ds.write_scratch(rom, cfg)
                return resolution_bits
            else:
                data = self.ds.read_scratch(rom)
                return ((data[4] >> 5) & 0x03) + 9
        except Exception as e:
            if config.debug:
                print("DS18B20 resolution err: ", e)
        
    
# Example Usage
if __name__ == "__main__":
    try:
        from machine import Pin
        # initialize pin
        ds_pin = Pin(config.ONEWIRE_PIN)
        
        ds18b20 = DS18B20(ds_pin)
        
        res = ds18b20.resolution()
        print(f"Sensor resolution: {res} bits")
        
        if ds18b20:
            while True:
                print(ds18b20.read_temp())
                time.sleep(10)  # Read every 10 seconds

    except Exception as e:
        print("Err: ", e)
        