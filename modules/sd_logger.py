# sd card

import os
import time
from machine import SDCard

import config

class SDLogger:
    __slots__ = ("sd", "vfs", "sd_mount_point",
                 "debug",
                 "data_path", "error_path", "health_path",
                 "error_row_limit", "error_retention", "error_avg_row_len",
                 "_current_date", "_error_row_count",
                 "_last_error_times", "_rows_since_flush")
    
    def __init__(self, spi_pin_miso, spi_pin_mosi, spi_pin_sck, spi_pin_cs,
                 sd_mount_point=config.SD_MOUNT_POINT,
                 data_path=config.DEFAULT_DATA_PATH,
                 error_path=config.DEFAULT_ERROR_PATH,
                 health_path=config.DEFAULT_HEALTH_PATH,
                 error_row_limit=config.DEFAULT_ERROR_ROW_LIMIT,
                 error_retention=config.DEFAULT_ERROR_RETENTION,
                 error_avg_row_len=config.ERROR_AVG_ROW,
                 debug=config.debug):
        try:
            # Initialize SD card object (using built-in SDCard class)
            self.sd = SDCard(slot=2, sck=spi_pin_sck, miso=spi_pin_miso, mosi=spi_pin_mosi, cs=spi_pin_cs, freq=10000000)
            # Mount the SD card
            self.vfs = os.VfsFat(self.sd)
            self.sd_mount_point = sd_mount_point
            os.mount(self.vfs, self.sd_mount_point)
            
            self.debug = debug
            if self.debug:
                print('sd card mounted')
                
            #---- logging parts ----
            self.data_path = data_path
            self.error_path = error_path
            self.health_path = health_path
            self.error_row_limit = error_row_limit # rotate every 500 error lines (tune as you like)
            self.error_retention = error_retention
            self.error_avg_row_len = error_avg_row_len   # average size of one error row - ~ 60  bytes
            
            # runtime state
            self._current_date = None  # for daily log file rotation (int of form YYYYMMDD)
            self._error_row_count = None          # in-memory row counter
            self._last_error_times = {}          # key -> epoch of last logged error (throttle)
            self._rows_since_flush = 0   # appended rows since last used flush
            
            # try to initialize current date from existing data file
            self._init_current_date()
            # try to initialize current rows from existing error file
            self._init_error_row_count()

            #-------------------------
        
        except Exception as e:
            # Optionally: mark a flag so the rest of the code stops trying to log
            self.data_path = None
            self.error_path = None
            self.health_path = None

    # mount the sd card
    def mount_sd_card(self):
        try:
            os.mount(self.vfs, self.sd_mount_point)
        except Exception as e:
            if self.debug:
                print("Error mounting SD card: ", e)
                
    # unmount the sd card
    def unmount_sd_card(self):
        try:
            os.umount(self.sd_mount_point)
            if self.debug:
                print("SD card unmounted successfully.")
        except Exception as e:
            if self.debug:
                print("Error unmounting SD card: ", e)
            
            
    # -----------------------------
    # Logging Operations
    # -----------------------------
    
    # ----------------- Helpers -----------------
    def _safe_sync(self):
        try:
            os.sync()   # some ports/FSes may not implement; ignore failures
        except Exception:
            pass
        
    def _exists(self, p):
        try:
            return os.stat(p)
        except Exception:
            return None
    
    def _iso_from_tup(self, t):
        """Return 'YYYY-MM-DD HH:MM:SS' from time tuple."""
        return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
    
    def _now_str_from_tup(self, t):
        """Return compact string YYYYMMDD_HHMMSS from tuple (for filenames)."""
        return "%04d%02d%02d_%02d%02d%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
    
    def _date_int_from_tup(self, t):
        """Return integer YYYYMMDD from time tuple (y,m,d,hh,mm,ss,...)."""
        # t is e.g. (2025,11,27,12,34,56,...)
        return t[0] * 10000 + t[1] * 100 + t[2]
    
    def _init_current_date(self):
        """Try to set self._current_date from existing data file's mtime."""
        try:
            st = self._exists(self.data_path) # metadata of data file
            if not st: # if data file does not even exist
                self._current_date = None
                return
            # else, data file exists
            t = time.localtime(st[8])  # MicroPython stat tuple: size usually index 6, mtime index 8
            self._current_date = self._date_int_from_tup(t)
        except Exception: 
            self._current_date = None
    
    # ----------------- Data log (daily rotate) -----------------
    def rotate_data_if_needed(self, ts):
        """If date changed since last write, rename current data log and set new date."""
        # Note that we are using the timestamp from the data row sent by sensor_task for daily file rotation
        # so that sensor readings of same day get logged in a single separate file
        date = self._date_int_from_tup(ts)
            
        if self._current_date is None: # data file does not exist
            # create file
            self.ensure_header(config.csv_fields)
            self._current_date = date
            return # no rotation needed
        if date != self._current_date: # if data file exists but is older (inetger comparision)
            # rotate: weather.csv -> weather.csv.YYYYMMDD
            try:
                archived = "%s.%d" % (self.data_path, self._current_date)
                if self._exists(self.data_path):
                    os.rename(self.data_path, archived)
                # create new file
                self.ensure_header(config.csv_fields)
            except Exception:
                # swallow: rotation is best-effort
                pass
            self._current_date = date
    # ----------------------
    
    def ensure_header(self, fieldnames):
        """Ensure data log exists; if missing create and write header."""
        if not self.data_path:
            return
        try:
            if not self._exists(self.data_path):
                with open(self.data_path, 'w') as f:
                    f.write(','.join(fieldnames) + '\n')
        except OSError as e:
            if self.debug:
                print("SD ensure_header error:", e)
            # Optionally: mark a flag so the rest of the code stops trying to log
            self.data_path = None
            
    def append_row(self, vals, ts): #  to be appended and ts is the timestamp tuple
        """
        vals: a list of ordered values (values are a str object)
        ts: rtc timestamp tuple.
        Append a CSV row to the data log.
        Performs daily rotation check before writing. If file is new,
        header will be written.
        """
        if not self.data_path:
            return
        
        # rotate first (cheap)
        self.rotate_data_if_needed(ts)
        
        _join = ",".join
        row = _join(vals) + "\n"
        try:
            if self.debug:
                print(row)
            with open(self.data_path, 'a') as f:
                f.write(row)
                self._rows_since_flush += 1
                if self._rows_since_flush >= 10: # flush every N rows
                    # we are implementing flush, os.sync to ensure file does not get corrupted if power outage during file I/O
                    f.flush()
                    self._safe_sync()
                    self._rows_since_flush = 0
                
        except Exception as e:
            if self.debug:
                print('SD write failed', e)
            # Optionally: mark a flag so the rest of the code stops trying to log
            #self.data_path = None
    
    
    # ----------------- Error log (size rotate + retention) -----------------
    def _should_log_error(self, key, min_interval):
        now = time.ticks_ms()
        last = self._last_error_times.get(key)
        if last and time.ticks_diff(now, last) < min_interval:
            return False
        self._last_error_times[key] = now
        return True
    
    def _init_error_row_count(self):
        """Estimate existing row count from file size / avg row length."""
        try:
            st = self._exists(self.error_path) # return metadata if errorr file exists
            if not st: # if error file does not even exist
                self._error_row_count = None
                return
            # else, data file exists
            self._error_row_count = st[6] // self.error_avg_row_len # st[6] = size
        except Exception:
            self._error_row_count = None

    def _rotate_error_if_needed(self, ts): # ts is the timestamp tuple
        """Rotate error log when approximate row count exceeds limit."""
        try:
            if self._error_row_count is None:  # error file does not exist
                #--- create error file with headers, and then return; no rotation needed ---
                try:
                    with open(self.error_path, "a") as f:
                        f.write("type,timestamp,key,message\n")
                except Exception:
                    # Optionally: mark a flag so the rest of the code stops trying to log
                    self.error_path = None
                self._error_row_count = 0 # set counter
                return
            
            if self._error_row_count < self.error_row_limit:
                return # no rotation needed
            
            # else, rotation needed
            # prepare timestamped name: errors.log.YYYYMMDD_HHMMSS.log
            stamp = self._now_str_from_tup(ts)
            newname = "%s.%s.log" % (self.error_path, stamp)
            os.rename(self.error_path, newname)
            # create new error file with headers
            try:
                with open(self.error_path, "a") as f:
                    f.write("type,timestamp,key,message\n")
            except Exception:
                # Optionally: mark a flag so the rest of the code stops trying to log
                self.error_path = None
            
            # reset counter
            self._error_row_count = 0
            
            # also, cleanup old rotated error logs
            self._cleanup_old_error_logs()
        except Exception:
            # swallow: rotation is best-effort
            pass

    def _cleanup_old_error_logs(self):
        """Keep only newest self.error_retention rotated error files, remove older ones."""
        try:
            dirpath, basename = os.path.split(self.error_path)
            if not dirpath:
                dirpath = self.sd_mount_point if hasattr(self, "sd_mount_point") and os.path.exists(self.sd_mount_point) else "."
                
            candidates = []
            # rotated files use pattern: <basename>.<stamp>.log  (e.g. errors.log.20251125_123045.log)
            prefix = basename + "."
            for fn in os.listdir(dirpath):
                if not fn.startswith(prefix) or not fn.endswith(".log"):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                    # MicroPython stat: mtime often at index 8
                    mtime = st[8] if len(st) > 8 else time.time()
                except Exception:
                    # If stat fails, treat as very new so it's pruned at last
                    mtime = time.time()
                candidates.append((mtime, full))
            
           # sort by key ascending (oldest first)
            candidates.sort(key=lambda x: x[0])
            
            # delete oldest until we have <= retention
            excess = len(candidates) - self.error_retention
            idx = 0
            while excess > 0 and idx < len(candidates):
                try:
                    os.remove(candidates[idx][1])
                except Exception:
                    # best-effort: ignore remove errors
                    pass
                idx += 1
                excess -= 1
                
        except Exception:
            # swallow any errors — cleanup is best-effort
            pass
        
    def append_error(self, key, message, ts=None, min_interval=config.DEFAULT_ERROR_THROTTLE_MS, force=False):
        """
        Append a short error record to SD (throttled).
        key: short string identifying error class (e.g. "wifi_fail", "mqtt_fail")
        message: free text (short)
        ts: timestamp tuple (Y, M, D, HH, MM, SS, ...) marking the error timing
        min_interval: milliseconds minimum between logs of same error key
        force: bypass throttling if True
        """
        if not self.error_path:
            return
        
        if not force and not self._should_log_error(key, min_interval):
            return # do not log and spam sd card because we are throttling error logging
        
        # rotate + cleanup if necessary
        self._rotate_error_if_needed(ts)
                
        try:
            ts = self._iso_from_tup(ts)
            # keep row compact: "ERR",ts,key,msg,etc
            row = "ERR,%s,%s,%s\n" % (ts, key, str(message))
            # reuse append_row but write to self.error_path (quick helper)
            if self.debug:
                print(row)
            with open(self.error_path, 'a') as f:
                f.write(row)
            
            # increase row count, if successfully appended
            self._error_row_count += 1
        except Exception:
            # best-effort only, never raise
            pass
            # Optionally: mark a flag so the rest of the code stops trying to log
            #self.error_path = None
    
    
    # ----------------- Health log -----------------
    def ensure_health_header(self):
        """Create health log file with header if it doesn't exist."""
        if not self.health_path:
            return
        try:
            if not self._exists(self.health_path):
                with open(self.health_path, "a") as f:
                    f.write("timestamp,uptime_s,mem_free,mem_alloc\n")
        except Exception as e:
            if config.debug:
                print("ensure_health_header error:", e)
            # Optionally: mark a flag so the rest of the code stops trying to log
            self.health_path = None
            
    def append_health(self, ts, uptime, mem_free, mem_alloc):
        """Append one health row: ISO time, uptime, mem_free, mem_alloc."""
        if not self.health_path:
            return

        try:
            ts = self._iso_from_tup(ts)
            row = "%s,%s,%s,%s\n" % (ts, uptime, mem_free, mem_alloc)
            if self.debug:
                print(row)
            with open(self.health_path, "a") as f:
                f.write(row)
        except Exception as e:
            # best-effort only, never raise
            pass
            # Optionally: mark a flag so the rest of the code stops trying to log
            #self.health_path = None


            
# Example Usage
if __name__ == "__main__":
    try:
        sd = SDLogger(
            spi_pin_miso=config.SPI_PIN_MISO,
            spi_pin_mosi=config.SPI_PIN_MOSI,
            spi_pin_sck=config.SPI_PIN_SCK,
            spi_pin_cs=config.SPI_PIN_CS
        )
        
        print("Files:", os.listdir("/sd"))
        '''
        st = sd._exists("/sd/weather.csv.20000101")
        t = time.localtime(st[8])
        print(t)
        '''
        

    except Exception as e:
        print("Error: ", e)
        