# Asair APM10 sensor library [i2c communication]

import time

'''
# I2C Commands and Constants
I2C_DEFAULT_ADDRESS = 0x08  # 7-bit I2C address for APM10
CMD_START_MEASUREMENT = bytearray([0x10, 0x00, 0x10, 0x05, 0x00, 0xF6])
CMD_STOP_MEASUREMENT = bytearray([0x10, 0x01, 0x04])
CMD_READ_DATA = bytearray([0x10, 0x03, 0x00, 0x11])
DATA_LENGTH = 30 # Expected response size in bytes
'''

# NOTE: The 0x10 byte in the original commands is likely a header that’s unnecessary for your specific sensor setup.
#       Removing it results in the sensor interpreting the commands correctly. Updating the library to reflect the
#       working commands should resolve the issue for your setup.

# I2C Commands and Constants
I2C_DEFAULT_ADDRESS = 0x08  # 7-bit I2C address for APM10
CMD_START_MEASUREMENT = bytearray([0x00, 0x10, 0x05, 0x00, 0xF6])
CMD_STOP_MEASUREMENT = bytearray([0x01, 0x04])
CMD_READ_DATA = bytearray([0x03, 0x00, 0x11])
DATA_LENGTH = 30 # Expected response size in bytes

class APM10:
    __slots__ = ("i2c", "address")
    def __init__(self, i2c, address=I2C_DEFAULT_ADDRESS):
        """
        Initialize the APM10 driver.
        :param i2c: I2C bus to communicate with the sensor.
        :param address: i2c adress of the sensor.
        """
        self.i2c = i2c
        self.address = address

    def is_connected(self):
        """Check if the sensor is connected via I2C."""
        try:
            self.i2c.writeto(self.address, b"") # write an empty byte string
            return True
        except OSError:
            return False

    def enter_measurement_mode(self):
        """Send the command to enter measurement mode."""
        try:
            self.i2c.writeto(self.address, CMD_START_MEASUREMENT)
            time.sleep(1)  # Allow sensor to stabilize
        except OSError as e:
            raise RuntimeError(f"Failed to enter measurement mode: {e}")

    def exit_measurement_mode(self):
        """Send the command to exit measurement mode."""
        try:
            self.i2c.writeto(self.address, CMD_STOP_MEASUREMENT)
        except OSError as e:
            raise RuntimeError(f"Failed to exit measurement mode: {e}")

    def read_data(self):
        """
        Read particulate matter data PM1.0, PM2.5, and PM10 from the sensor.
        :return: return: a tuple with (PM1.0, PM2.5, PM10) values in µg/m³.
        """
        try:
            self.i2c.writeto(self.address, CMD_READ_DATA)
            time.sleep(0.1)  # Allow sensor time to prepare data
            raw_data = self.i2c.readfrom(self.address, DATA_LENGTH)
        except OSError as e:
            raise RuntimeError(f"Failed to read data: {e}")

        if len(raw_data) != DATA_LENGTH:
            raise ValueError("Incomplete response received from sensor: {len(data)}")
        
        '''
        To read 30 bytes of valid data, the data description is shown below:
        -----|----------------|--------------------------------------------------
        Byte |   Data type    |    Description
        -----|----------------|--------------------------------------------------
        0~2  |   Each         |    PM1.0 concentration=byte0*256+byte1 (unit: μg/m3);
             |   particulate  |    Byte3 is the CRC checksumof byte0 andbyte1;
        -----|   measurement  |--------------------------------------------------
        3~5  |   data is 16   |    PM2.5 concentration=byte3*256+byte4 (unit: μg/m3);
             |   bits with    |    Byte5 is the CRC checksumof byte3 andbyte4;
        -----|   the sequence |--------------------------------------------------
        6~8  |   of high byte,|    Reserved
        -----|   low byte and |--------------------------------------------------
        9~11 |   a CRC check  |    PM1.0 concentration=byte9*256+byte10 (unit: μg/m3);
             |   value.       |    Byte11 is the CRC checksumof byte9andbyte10;
        -----|                |--------------------------------------------------
        12~14|                |    Reserved
        -----|                |--------------------------------------------------
        15~17|                |    Reserved
        -----|                |--------------------------------------------------
        18~20|                |    Reserved
        -----|                |--------------------------------------------------
        21~23|                |    Reserved
        -----|                |--------------------------------------------------
        24~26|                |    Reserved
        -----|                |--------------------------------------------------
        27~29|                |    Reserved
        -----|----------------|--------------------------------------------------
        '''
        # Parse and validate the response
        pm1_0 = self._validate_and_parse(raw_data, 0)
        pm2_5 = self._validate_and_parse(raw_data, 3)
        pm10 = self._validate_and_parse(raw_data, 9)

        return pm1_0, pm2_5, pm10

    def _validate_and_parse(self, data, offset):
        """
         Extract and Validate a PM value using CRC8.
        :param data: Raw data bytes.
        :param offset: Offset of the PM value in the data array.
        :return: Validated PM value in µg/m³.
        """
        high_byte = data[offset]
        low_byte = data[offset + 1]
        crc = data[offset + 2]

        # Combine high and low bytes to form the value
        value = (high_byte << 8) | low_byte

        # Validate CRC for these two bytes
        calculated_crc = APM10._calculate_crc8(data[offset:offset + 2])
        if crc != calculated_crc:
            raise ValueError(f"CRC mismatch: received {crc}, calculated {calculated_crc}")

        return value


    @staticmethod
    def _calculate_crc8(data):
        """
        Calculate CRC8 checksum using the polynomial 0x31.
        :param data: Data bytes to calculate CRC for.
        :return: CRC8 value.
        """
        crc = 0xFF
        polynomial = 0x31
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ polynomial
                else:
                    crc <<= 1
                crc &= 0xFF  # Ensure CRC remains 8-bit
        return crc

