# connect mqtt client to broker and other functions

from umqtt.simple import MQTTClient
import config

class MQTTClientSimple:
    __slots__ = ("_feeds", "client")
    
    def __init__(self, client_id, broker, port,
                 user, password, keepalive=60,
                 will_topic=None, will_msg=None,
                 will_qos=0, will_retain=False,
                 feeds=(), callback=None):
        
        self._feeds = feeds # tuple of subscribed feeds
        self.client = MQTTClient(client_id, broker, port,
                                user=user,
                                password=password,
                                keepalive=keepalive)
        
        if will_topic:
            self.client.set_last_will(
                will_topic, will_msg, will_retain, will_qos
                )
        
        self._set_callback(callback) # callback function for subscribed feeds
    
    # Connect to mqtt broker
    def _connect(self):
        try:
            self.client.connect()
            if config.debug:
                print('mqtt ok')
        except Exception:
            raise # raise so that main code knows that mqtt connection failed
    
    # Disconnect from mqtt broker
    def _disconnect(self):
        try:
            self.client.disconnect()
        except Exception:
            raise
                
    # Subscribe to mqtt feeds to receive messages and also remember them
    def _subscribe(self, qos=0):
        try:
            for feed in self._feeds:
                self.client.subscribe(feed, qos)
        except Exception:
            raise
        
    # connect mqtt client and subscribe to given feeds
    def _connect_and_subscribe(self, qos=0):
        try:
            self.client.connect()
            for feed in self._feeds:
                self.client.subscribe(feed, qos)
            if config.debug:
                print('mqtt ok')
        except Exception:
            raise
                
    # Set message callback => callback(topic, msg) - a callback function
    def _set_callback(self, callback=None):
        if callback:
            self.client.set_callback(callback)
        
    # Publish data to mqtt server
    # feeds: list of full topic strings, e.g. "user/feeds/bmp_temp"
    # msgs:  list of payloads strings/bytes, same length as feeds
    def publish_data(self, feeds, msgs):
        try:
            _publish = self.client.publish
            for feed, msg in zip(feeds, msgs):
                _publish(feed, msg)
        except Exception:
            raise # raise so that main code knows that mqtt connection failed

    # Non-blocking message check
    # Call this periodically in main loop to check if any message is received on any subscribed feed
    def check(self):
        try:
            self.client.check_msg() # it fails and raises error if connection has been disconnected
        except Exception:
            raise # raise so we know mqtt probably disconnected


# NOTE=> About callbacks - define them in your main program and pass the function here.
# 
# We cannot define the MQTT callback function as def feed_callback(feed, msg, arg1, arg2): directly, because the callback signature for MQTT messages is predefined by the MQTT client library you are using (e.g., umqtt.simple or paho.mqtt). In most MQTT libraries, the callback function signature accepts only two parameters:
# feed: The topic or feed to which the message was published.
# msg: The message content.
# 
# ===========
# 
# Why Adding Extra Arguments (arg1, arg2) Is Not Allowed:
# MQTT client callback functions are designed to be triggered by the library itself when a message is received on a subscribed topic. The library automatically passes the topic (feed) and the message (msg) as parameters, but it does not automatically handle additional custom arguments (arg1, arg2), which are not part of the library's expected signature.
# The MQTT client, when invoking the callback, does not know how to pass additional arguments unless you explicitly handle it within the function. If you try to define more parameters in the callback, you will likely encounter a TypeError because the callback function does not match the signature expected by the client library.
# 
# ===========
# 
# How to Handle Additional Arguments in the Callback:
# 1. Use a Global or Shared Variable-
# Use a global variable or an object that stores the additional arguments, which can be accessed within the callback.
# ---------------
# # Define global variables for additional arguments
# global_arg1 = "extra_arg1"
# global_arg2 = "extra_arg2"
# 
# def feed_callback(feed, msg):
#     #feed = feed.decode('utf-8')
#     #msg = msg.decode('utf-8')
#     print(f"Received message on {feed}: {msg}")
#     print(f"Additional args: {global_arg1}, {global_arg2}")
# ---------------
# 
# 2. Object-Oriented Approach-
# If your application is more complex, you can use an object-oriented approach and store the additional arguments as attributes of an object. Then, your callback function can refer to the object's attributes.
# ---------------
# class MQTTCallbackHandler:
#     def __init__(self, arg1, arg2):
#         self.arg1 = arg1
#         self.arg2 = arg2
# 
#     def feed_callback(self, feed, msg):
#         #feed = feed.decode('utf-8')
#         #msg = msg.decode('utf-8')
#         print(f"Received message on {feed}: {msg}")
#         print(f"Additional args: {self.arg1}, {self.arg2}")
# ----------------
