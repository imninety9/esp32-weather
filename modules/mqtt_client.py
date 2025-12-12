# connect mqtt client to broker and other functions

from umqtt.simple import MQTTClient
import config

class MQTTClientSimple:
    __slots__ = ("client",)
    
    def __init__(self, client_id, broker, port,
                 user, password, keepalive):
        self.client = MQTTClient(client_id, broker, port,
                                user=user, password=password,
                                keepalive=keepalive)        
        self._connect()  
    
    # Connect mqtt client
    def _connect(self):
        """Connect to MQTT broker"""
        try:
            self.client.connect()
            if config.debug:
                print('mqtt connected')
        except Exception as e:
            raise e # raise so that main code knows that mqtt connection failed
    
    # Publish data to mqtt server
    def publish_data(self, feeds, msgs):
        """
        publish the given data to their corresponding feeds.
        feeds: list of full topic strings, e.g. "user/feeds/bmp_temp"
        msgs:  list of payload strings/bytes, same length as feeds
        """
        try:
            for feed, msg in zip(feeds, msgs):
                self.client.publish(feed, msg)
        except Exception as e:
            raise e # raise so that main code knows that mqtt connection failed
