# ds3231_driver.py - Optimized DS3231 RTC Driver

# How DS3231 Alarms Work:
# 
# Alarm Flags: When an alarm condition matches, the DS3231 sets an "internal" flag (bit 0 for Alarm1, bit 1 for Alarm2 in register 0x0F). This keeps happening regardless if alarm is enabled or not. It is like the ds3231 clock ticking; keeps happening internally.
# INT/SQW Pin: If the alarm is enabled (via register 0x0E), the INT/SQW pin goes LOW when the flag is set. So enabling/disabling alarm means if we want to recieve the signal on INT/SQW Pin or not.
# Flag Persistence: The alarm flag stays TRUE until explicitly cleared, regardless of whether the alarm is enabled or disabled.
# Note: Also, if we enable an alarm and its flag is set already (which keeps happening internally anyway); then the INT/SQW Pin goes LOW (LOW means alarm fired) instantly because it assumes the alarm fired because its flag is set. So, before enabling an alarm, clear its flag so only new triggers get recognized.
# 
# Disabling an alarm stops INT/SQW signals but the alarm still keeps firing internally.
# The flag will still be set even if INT/SQW isn't triggered.
# You must clear flags to recognize new triggers.
# 
# SEE THE EXAMPLEs BELOW TO UNDERSTAND HOW TO USE ALARM IN PRACTICE IN BOTH SYNCHRONOUS AND ASYNCHRONOUS VERSIONS.


# A Note about using alarm/irq pin as the souce of wake from deepsleep:
# NOTE: DS3231 keeps ALARM/IRQ pin LOW until the flag is cleared.
#         Hence, clear the alarm flags immediately after wake;
#         otherwise esp32 will keep waking immediately after going
#         to deepsleep in the following cycles, because
#         ALARM/IRQ pin is already LOW.

from ds3231_gen import DS3231, EVERY_SECOND, EVERY_MINUTE, EVERY_HOUR, EVERY_DAY, EVERY_WEEK, EVERY_MONTH

import config

# Optimized driver for DS3231 RTC with alarm support
class DS3231Driver:
    # Re-export alarm modes (public API)
    EVERY_SECOND = EVERY_SECOND
    EVERY_MINUTE = EVERY_MINUTE
    EVERY_HOUR   = EVERY_HOUR
    EVERY_DAY    = EVERY_DAY
    EVERY_WEEK   = EVERY_WEEK
    EVERY_MONTH  = EVERY_MONTH
    
    __slots__ = ("_device", "_a1_en", "_a2_en")
    
    # Initialize DS3231 RTC driver.
    # Args:
    #     i2c: I2C bus object
    def __init__(self, i2c):
        try:
            # Initialize device
            self._device = DS3231(i2c)
            
            # ds3231 has two alarms
            self._a1_en = False # to track if alarm 1 is enabled or not
            self._a2_en = False # to track if alarm 2 is enabled or not
            
            if config.debug:
                print('DS3231 ok')
        except Exception:
            raise # raise if initialization failed to let the caller know about it
    
    # ==================== Time Management ====================
    
    # Get current time from DS3231.
    # Returns:
    #     Tuple: (year, month, day, hour, minute, second, weekday, 0) in 24 hour format
    def get_time(self):
        return self._device.get_time()
    
    # Set DS3231 time.
    # time_tuple: (year, month, day, hour, minute, second, weekday, 0) in 24 hour format
    def set_time(self, time_tuple):
        self._device.set_time(time_tuple)
    
    # Sync DS3231 with NTP server.
    # Args:
    #     timezone_offset: Seconds from UTC (default: 19800 for IST) # IST = UTC + 19800 (in sec)
    # Returns:
    #     bool: True on success, False on failure
    def sync_time_with_ntp(self, timezone_offset=19800):
        try:
            import ntptime
            import time
            ntptime.host = 'pool.ntp.org' # UTC
            # ntptime.time() returns seconds from epoch
            self._device.set_time(time.localtime(ntptime.time() + timezone_offset))
            return True
        except (OSError, ImportError):
            return False
    
    # Get DS3231 temperature in Celsius
    def temperature(self):
        return self._device.temperature()
    
    # ==================== Alarm Management ====================
    
    def set_alarm(self, alarm_num: int,
                  mode, *, day=0, hr=0, min=0, sec=0): # All parameters after * must be passed as keyword arguments.
#         Sets and also enables an alarm.
#         Note: 1. Setting an alarm automatically enables it.
#               2. This function also clears any previous alarm trigger flag if set True, so that only new alarm triggers will get recognized.
#         
#         Args:
#             alarm_num: 1 or 2 (which alarm you want to set)
#             mode: one of following Alarm Modes:
#                EVERY_SECOND   (alarm 1 only; alarm2 does not support seconds resolution)
#                EVERY_MINUTE   (Every minute at specified second)
#                EVERY_HOUR     (Every hour at specified minute:second)
#                EVERY_DAY      (Every day at specified time)
#                EVERY_WEEK     (Every week on specified day) (day must be b/w 0-6 for every week alarm, Sunday=0)
#                EVERY_MONTH    (Specific day of month) (day = 1-31)
#             day: 0–6 or 1–31 [0–6: Day of the week (Sunday = 0). 1–31: Day of the month.]
#             hr: 0–23 [24-hour clock format.]
#             min: 0–59 [Minutes in an hour.]
#             sec: 0–59 [Seconds in a minute (used only for Alarm 1, as Alarm 2 doesn't support seconds and ignores them).]
        if alarm_num not in (1, 2):
            raise ValueError("Alarm number must be 1 or 2")
        # Alarm 2 hardware limitation
        if alarm_num == 2 and mode == EVERY_SECOND:
            raise ValueError("Alarm 2 does not support EVERY_SECOND")
            
        if alarm_num == 1:
            alarm_obj = self._device.alarm1
            # Clear alarm flag if set true
            if alarm_obj():
                alarm_obj.clear()
            # Set alarm
            alarm_obj.set(mode, day=day, hr=hr, min=min, sec=sec)
            self._a1_en = True # alarm 1 is enabled
        else:
            alarm_obj = self._device.alarm2
            # Clear alarm flag if set true
            if alarm_obj():
                alarm_obj.clear()
            # Set alarm
            alarm_obj.set(mode, day=day, hr=hr, min=min, sec=sec)
            self._a2_en = True # alarm 2 is enabled
    
    def enable_alarm(self, alarm_num=0):
#         Enable alarm interrupt(s).
#         Note: 1. Enabling an alarm enables signals on the INT/SQW pin at the preset interval.
#               2. This function also clears any previous alarm trigger flag, so that only new alarm triggers will get recognized.
#         Args:
#             alarm_num: 1, 2, or 0 for both
        if alarm_num != 1:
            alarm_obj = self._device.alarm2
            # Clear alarm flag if set true
            if alarm_obj():
                alarm_obj.clear()
            alarm_obj.enable(run=True)
            self._a2_en = True # alarm 2 is enabled
        if alarm_num != 2:
            alarm_obj = self._device.alarm1
            # Clear alarm flag if set true
            if alarm_obj():
                alarm_obj.clear()
            alarm_obj.enable(run=True)
            self._a1_en = True # alarm 1 is enabled
    
    def disable_alarm(self, alarm_num=0):
#         Disable alarm interrupt(s).
#         Args:
#             alarm_num: 1, 2, or 0 for both
#
#         NOTE: Disabling an alarm only disables the interrupt signal to the INT/SQW pin;
#         but the alarm keeps going on at the preset interval in the ds3231 hardware.
#         Meaning microcontroller won't know or get affected by the alarm but alarm keeps
#         firing just like the clock keeps ticking in the ds3231 internally. Hence, if we check the
#         alarm flag; it will read 'True' even though the INT/SQW pin doesn't get affected.
        if alarm_num != 1:
            self._device.alarm2.enable(run=False)
            self._a2_en = False # alarm 2 is disabled
        if alarm_num != 2:
            self._device.alarm1.enable(run=False)
            self._a1_en = False # alarm 1 is disabled
    
    def clear_alarm(self, alarm_num):
#         Clear alarm flag.
#         Args:
#             alarm_num: 1 or 2
#
#         Note: It doesn't matter if the alarm is enabled or not meaning
#         it doesn't matter if the INT/SQW pin is getting the signal of alarm
#         being fired at the preset interval or not. The flag will be set to True
#         once alarm fires and will stay True until explicitly cleared.
        if alarm_num not in (1, 2):
            raise ValueError("Alarm number must be 1 or 2")
        alarm_obj = self._device.alarm1 if alarm_num == 1 else self._device.alarm2
        # Clear alarm flag if set true
        if alarm_obj():
            alarm_obj.clear()
    
    def check_alarm(self, alarm_num):
#         Check if an alarm flag is set (i.e. it has fired) or not. [non-destructive read]
#         Args:
#             alarm_num: 1 or 2
#         Returns:
#             bool: True if alarm has fired, False otherwise or on error
        if alarm_num not in (1, 2):
            raise ValueError("Alarm number must be 1 or 2")
        alarm_obj = self._device.alarm1 if alarm_num == 1 else self._device.alarm2
        return alarm_obj()
    
    
    # ==================== Interrupt Handler ====================
    
    def poll(self):
#         Call this from the main loop, once alarm irq handler fires,
#         to know which alarms fired and then clear them.
#         Note: It can be called using polling or using an asyncio threadsafeflag/event based on the design of alarm irq handler.
#         Returns:
#             0  → no alarm fired
#             1  → alarm1 fired
#             2  → alarm2 fired
#             3  → both fired

        # -------------------------------------------------------------------------
        # IMPORTANT DS3231 BEHAVIOR (READ BEFORE MODIFYING ALARM LOGIC)
        #
        # The DS3231 has TWO independent alarm flags (A1F, A2F) in the STATUS register.
        # These flags are set by the hardware whenever the alarm condition matches,
        # REGARDLESS of whether the corresponding alarm interrupt is enabled or not.
        #
        # The INT/SQW pin, however, is asserted LOW only if:
        #   (A1F AND A1IE) OR (A2F AND A2IE)
        #  that is flag is set AS WELL AS alarm is enabled.
        #
        # Consequence:
        # - An alarm flag may be TRUE even if that alarm was disabled.
        # - Both alarm flags can be TRUE at the same time.
        # - Since both alarms share the SAME interrupt pin (meaning the SAME IRQ handler),
        #   when an IRQ occurs we cannot infer which alarm caused it solely from the flags.
        #
        # Therefore:
        # - We MUST track in software which alarms are enabled.
        # - When handling an interrupt, ONLY enabled alarms should be considered
        #   valid sources.
        # - Disabled alarms may have stale flags which must be ignored (or cleared).
        #
        # This is intentional DS3231 hardware behavior, not a driver bug.
        # -------------------------------------------------------------------------

        fired = 0 # bitmask

        if self._a1_en and self._device.alarm1(): # if alarm1 is enabled and its flag is set
            self._device.alarm1.clear() # clear its flag
            fired |= 1 # mark that it has fired

        if self._a2_en and self._device.alarm2(): # if alarm2 is enabled and its flag is set
            self._device.alarm2.clear() # clear its flag
            fired |= 2 # mark that it has fired

        return fired

# ==================== Example Usage ====================

if __name__ == '__main__':
    ###########################################################################################################
    ################## Synchronous Usage Example ##########################################################################
    import machine
    import time
    
    import esp32
    
    isr_fired = False # track if alarm isr ran or not
    
    def alarm_isr(pin): # alarm_pin is calling this handler so it will be passed as an argument; hence we are defining it in the function definition
#         ISR for alarm interrupts.
#         Called when INT/SQW pin goes LOW.
#         Note: Keep this very minimal (no prints, no allocations, super fast, etc) and ISR-safe!

# ----------------------------------------------------------------------------------------------       
#         NOTE: An interrupt handler function receives a definite argument from the source/peripheral
#         which has called it and this argument is this source/peripheral itself (e.g., 'pin' for GPIO
#         interrupts or 'timer' for timer ISRs). The reasons for this are here:
#         1. When multiple peripherals or pins are configured to generate interrupts, the handler needs
#         to know which one triggered the interrupt.
#         2. Passing the triggering peripheral as an argument allows the same handler function to manage
#         multiple sources dynamically.
#         Without this argument, you'd need a separate handler for each peripheral, making the code less
#         flexible and harder to maintain.
#         3. The argument allows the handler to interact with the specific peripheral that triggered the
#         interrupt. For example:
#         Reading or writing to a GPIO pin state.
#         Checking timer flags or resetting them after the event.
#         4. Handlers can remain modular, as they don't rely on hardcoded references. The interrupt system
#         passes the necessary peripheral/context dynamically.
#         This improves portability and reusability across different projects or parts of the codebase.
# 
#         => Hence, when defining the interrupt handler function; always include this source/peripheral as
#         an argument in its definition even if you don't use it in the function.
# 
#         e.g. def interrupt_handler(timer):
#                 pass
#         or, def interrupt_handler(timer):
#                 timer.deinit()
#         or, def interrupt_handler(pin):
#                 pass
#         or, def interrupt_handler(pin):
#                 print(pin.value())
# ------------------------------------------------------------------------------------------------------
        global isr_fired
        isr_fired = True

    try:
        # Initialize i2c bus
        i2c = machine.I2C(0, scl=machine.Pin(config.sclPIN), sda=machine.Pin(config.sdaPIN), freq=100000)
        # initialize ds3231 rtc
        rtc = DS3231Driver(i2c)
        
        # initialize alarm pin
        alarm_pin = machine.Pin(config.alarmPIN, machine.Pin.IN, machine.Pin.PULL_UP)
        # attach irq to alarm pin
        alarm_pin.irq(handler=alarm_isr, trigger=machine.Pin.IRQ_FALLING)

        
        #--------------------------
        # Optional: Sync with NTP
        #rtc.sync_time_with_ntp()
        #--------------------------
        
        #--------------------------------------------------------
        current_time = rtc.get_time()
        if current_time:
            print(f'Time: {current_time[3]:02d}:{current_time[4]:02d}:{current_time[5]:02d}')
            
        temp = rtc.temperature()
        print(f'Temp: {temp:.2f}°C')
        #--------------------------------------------------------  
         
#         #--------------------------------------------------------
#         # Enable wake on LOW level
#         esp32.wake_on_ext0(pin=alarm_pin, level=esp32.WAKEUP_ALL_LOW)
#         
#         # Ensure alarm flags are clean before going to deepsleep
#         rtc.clear_alarm(1)
#         rtc.clear_alarm(2)
#         
#         # Set alarm
#         rtc.set_alarm(1, EVERY_MINUTE, sec=0)
# 
#         print("Going to deep sleep...")
#         time.sleep_ms(100) # little delay
# 
#         machine.deepsleep()
#         
#         # -------------------- Wake handling --------------------
#         print("Woke from deep sleep")
#         if rtc.check_alarm(1):
#             print("Alarm 1 woke the system")
#
#         # Ensure alarm flags are clean, before entering deepsleep again
#         # NOTE: DS3231 keeps ALARM/IRQ pin LOW until the flag is cleared.
#         # 		Hence, clear the alarm flags immediately after wake;
#         # 		otherwise esp32 will keep waking immediately after going
#         # 		to deepsleep in the following cycles, because
#         # 		ALARM/IRQ pin is already LOW.
#         rtc.clear_alarm(1)
#         rtc.clear_alarm(2)
#         # -------------------- Do work --------------------
#         print("Doing work")
# 
#             
#         #--------------------------------------------------------  
        
        #--------------------------------------------------------
        # Set Alarm 1: Every minute at 30 seconds
        rtc.set_alarm(1, EVERY_MINUTE, day=0, hr=0, min=0, sec=30)
        print('Alarm 1 set for every minute at :30 seconds')
        
        # Set Alarm 2: Every hour at 15 minutes
        rtc.set_alarm(2, EVERY_HOUR, day=0, hr=0, min=30, sec=0)
        print('Alarm 2 set for every hour at :30 minutes')
        
        # Main loop
        while True:
            fired = rtc.poll()
            if fired:
                if fired & 1:
                    print("Alarm 1 fired", rtc.get_time())
                if fired & 2:
                    print("Alarm 2 fired", rtc.get_time())
            
            time.sleep_ms(100)
        #--------------------------------------------------------
        
    except KeyboardInterrupt:
        print('\nExiting...')
    except Exception as e:
        print(f'Err: {e}')
    finally:
        rtc.disable_alarm()
    #########################################################################################################################
    ###################################################################################################################################
        
        
    
       
#     #####################################################################################################################
#     ########### Asyncio Usage Example #############################################
#     import machine
#     import uasyncio as asyncio
#     import time
#     
#     import esp32
#     
#     alarm_flag = asyncio.ThreadSafeFlag() # set it once alarm isr runs
#     
#     def alarm_isr(pin): # alarm_pin is calling this handler so it will be passed as an argument; hence we are defining it in the function definition
#         ISR for alarm interrupts.
#         Called when INT/SQW pin goes LOW.
#         Note: Keep this very minimal (no prints, no allocations, super fast, etc) and ISR-safe!
#
#         alarm_flag.set()
#         
#     # ---------------- Async tasks ----------------
#     async def alarm_task(rtc):
#         while True:
#             await alarm_flag.wait() # Wait here until irq triggers => ThreadsafeFlag auto clears
# 
#             fired = rtc.poll()
#             if fired:
#                 if fired & 1:
#                     print("Alarm 1 fired", rtc.get_time())
#                 if fired & 2:
#                     print("Alarm 2 fired", rtc.get_time())
#             
#     async def main():
#         try:
#             # Initialize i2c bus
#             i2c = machine.I2C(0, scl=machine.Pin(config.sclPIN), sda=machine.Pin(config.sdaPIN), freq=100000)
#             # initialize ds3231 rtc
#             rtc = DS3231Driver(i2c)
#             
#             # initialize alarm pin
#             alarm_pin = machine.Pin(config.alarmPIN, machine.Pin.IN, machine.Pin.PULL_UP)
#             # attach irq to alarm pin
#             alarm_pin.irq(handler=alarm_isr, trigger=machine.Pin.IRQ_FALLING)
# 
#             
#             #--------------------------
#             # Optional: Sync with NTP
#             #rtc.sync_time_with_ntp()
#             #--------------------------
#             
#             #--------------------------------------------------------
#             current_time = rtc.get_time()
#             if current_time:
#                 print(f'Time: {current_time[3]:02d}:{current_time[4]:02d}:{current_time[5]:02d}')
#                 
#             temp = rtc.temperature()
#             print(f'Temp: {temp:.2f}°C')
#             #--------------------------------------------------------  
#             
#             #--------------------------------------------------------
#             # Set Alarm 1: Every minute at 30 seconds
#             rtc.set_alarm(1, EVERY_MINUTE, day=0, hr=0, min=0, sec=30)
#             print('Alarm 1 set for every minute at :30 seconds')
#             
#             # Set Alarm 2: Every hour at 15 minutes
#             rtc.set_alarm(2, EVERY_HOUR, day=0, hr=0, min=30, sec=0)
#             print('Alarm 2 set for every hour at :30 minutes')
#             
#             asyncio.create_task(alarm_task(rtc))
#             while True:
#                 await asyncio.sleep(600)
#             #--------------------------------------------------------
#             
#         except Exception as e:
#             print('Err: ', e)
#         finally:
#             rtc.disable_alarm()
#        
#     try:
#         asyncio.run(main())
#     except KeyboardInterrupt:
#         print('\nExiting...')
#     except Exception as e:
#         print('Err: ', e)
#     finally:
#         asyncio.new_event_loop()
#     ################################################################################################################
    
        