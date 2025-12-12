# owm.py -- fetch OpenWeatherMap current weather (simple, cached)

import urequests
import config

class OWMClient:
    __slots__ = ("url", "url_aqi", "headers", "_cache")
    
    def __init__(self, url, url_aqi, headers):
        self.url = url
        self.url_aqi = url_aqi
        self.headers = headers
        self._cache = [None]*8 # temperature, feels_like_temp, humidity, pressure, aqi, pm2_5, pm10, no2

    def fetch(self):
        try:
            response = urequests.get(self.url, headers=self.headers, timeout=5)
            weather_data = response.json()
            response.close()
            if (response.status_code == 200 and 'main' in weather_data):
                self._cache[0] = weather_data['main']['temp'] # tempearture
                self._cache[1] = weather_data['main']['feels_like'] # feels_like_temp
                self._cache[2] = weather_data['main']['humidity'] # humidity
                self._cache[3] = weather_data['main']['grnd_level'] * 100 # pressure - Convert hPa to Pa [ground level pressure]
            
            # aqi information fetch
            response = urequests.get(self.url_aqi, timeout=5)
            aqi_data = response.json()
            response.close()
            if (response.status_code == 200 and 'list' in aqi_data):
                self._cache[4] = aqi_data['list'][0]['main']['aqi'] # 1: good, 2: fair, 3: moderate, 4: poor, 5: very poor
                self._cache[5] = aqi_data['list'][0]['components']['pm2_5'] # conc: μg/m3
                self._cache[6] = aqi_data['list'][0]['components']['pm10'] # conc: μg/m3
                self._cache[7] = aqi_data['list'][0]['components']['no2'] # conc: μg/m3
        
            return self._cache
        except Exception as e:
            if config.debug:
                print('OWM fetch failed', e)
            return self.cached()

    def cached(self):
        return self._cache