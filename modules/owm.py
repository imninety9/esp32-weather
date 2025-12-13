# owm.py -- fetch OpenWeatherMap current weather (simple, cached)

import urequests
import config

class OWMClient:
    __slots__ = ("url", "url_aqi", "headers", "_cache")
    
    def __init__(self, url, url_aqi, headers=None):
        self.url = url
        self.url_aqi = url_aqi
        self.headers = headers
        self._cache = [None]*7 # temperature, feels_like_temp, humidity, pressure, pm2_5, pm10, no2

    def fetch_weather(self):
        try:
            response = urequests.get(self.url, timeout=5)
            data = response.json()
            response.close()

            main = data.get('main')
            if main:
                self._cache[0] = main.get('temp') # tempearture
                self._cache[1] = main.get('feels_like') # feels_like_temp
                self._cache[2] = main.get('humidity') # humidity
                self._cache[3] = main.get('grnd_level') * 100 # pressure - Convert hPa to Pa [ground level pressure]
            del data
        except Exception as e:
            if config.debug:
                print("Weather fetch failed:", e)

    def fetch_aqi(self):
        try:
            response = urequests.get(self.url_aqi, timeout=5)
            data = response.json()
            response.close()

            lst = data.get('list')
            if lst:
                comp = lst[0].get('components')
                self._cache[4] = comp.get('pm2_5') # conc: μg/m3
                self._cache[5] = comp.get('pm10') # conc: μg/m3
                self._cache[6] = comp.get('no2') # conc: μg/m3
            del data
        except Exception as e:
            if config.debug:
                print("AQI fetch failed:", e)
    
    def fetch(self, fetch_aqi=True)
        self.fetch_weather()
        if fetch_aqi:
            self.fetch_aqi()
        return self._cache
    