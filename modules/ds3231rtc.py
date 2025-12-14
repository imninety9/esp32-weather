# ds3231 rtc

import ds3231_gen
import config

class ds3231:
    __slots__ = ("d", "alarm", "alarm1", "alarm2", "alarm_pin")
    
    def __init__(self, i2c, alarmPIN):
        """
        Initializes the ds3231 rtc.

        :param i2c: I2C object
        :param alarmPIN_address: GPIO pin object connected to alarm
        """
        try:            
            self.d = ds3231_gen.DS3231(i2c)
            self.alarm = [self.d.alarm1, self.d.alarm2] # ds3231 has two alarms
            # variiables to track, if an alarm is enabled (i.e. sending interrupts to the INT/SQW pin) or not
            self.alarm1 = False
            self.alarm2 = False
            
            # GPIO pin object for INT/SQW pin
            self.alarm_pin = alarmPIN
            
            if config.debug:
                print('ds3231 rtc initialized')
        except Exception as e:
            raise e # raise if initialization failed to let the caller know about it
        
    def get_time(self):
        '''get current time from ds3231'''
        ''' format: (year, month, day, hour, minutes, seconds, weekday, 0) '''
        ''' in 24 hour format '''
        try:
            return self.d.get_time()
        except Exception as e:
            return None
            
    def set_time(self, time):
        '''set ds3231 time'''
        ''' time tuple format: (year, month, day, hour, minutes, seconds, weekday, 0) '''
        ''' in 24 hour format '''
        try:
            return self.d.set_time(time)
        except Exception as e:
            if config.debug:
                print('Failed to set DS3231 time: ', e)
    
    def sync_time_with_ntp(self):
        '''sync ds3231 time with ntp server'''
        try:
            import ntptime
            import time
            ntptime.host = 'pool.ntp.org' # UTC
            # ntptime.time() returns seconds from epoch
            self.set_time(time.localtime(ntptime.time() + 19800)) # IST = UTC + 19800 (in sec)
            return True
        except Exception as e:
            return False

# Example Usage
if __name__ == "__main__":
    try:
        import time
        from machine import I2C, Pin
        # Initialize i2c bus and alarm pin
        i2c = I2C(0, scl=Pin(config.sclPIN), sda=Pin(config.sdaPIN), freq=100000)
        alarmPIN = Pin(config.alarmPIN, Pin.IN, Pin.PULL_UP)
        
        # initialize ds3231 rtc
        rtc = ds3231(i2c, alarmPIN)
        #rtc.sync_time_with_ntp()
        while True:
            print(rtc.get_time())
            time.sleep(10)  # Read every 10 seconds

    except Exception as e:
        print("Error: ", e)
        