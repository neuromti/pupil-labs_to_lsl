'''
Readme
------

Pupil LSL Relay Plugin

Plugin for .. _Pupil Capture: https://github.com/pupil-labs/pupil/wiki/Pupil-Capture that relays pupil and gaze data, as well as notification, to the [lab streaming layer](https://github.com/sccn/labstreaminglayer).

Installation
^^^^^^^^^^^^

1. Build the `LSL Python bindings <https://github.com/sccn/labstreaminglayer/tree/master/LSL/liblsl-Python>`_.
    1. Download liblsl-Python-1.11.zip <ftp://sccn.ucsd.edu/pub/software/LSL/SDK/liblsl-Python-1.11.zip>`_.
    2. Extract the zip file
    3. Execute `python setup.py build` in the extracted folder. This should generate a `build` folder.
    4. `build` should include another folder called `lib` that includes a folder called `pylsl`.
2. Copy the `pylsl` with all its content to the _user plugin directory*_.
3. Copy *pupil_lsl_relay.py* to the *user plugin directory*.

* Wiki link for the `user plugin directory <https://docs.pupil-labs.com/#plugin-guide>`_.

Usage
^^^^^

1. Start _Pupil Capture_.
2. `Open the _Pupil LSL Relay_ plugin <https://docs.pupil-labs.com/#open-a-plugin>`_.
3. Optional: Deselect relaying for pupil data, gaze data, or notifications.
4. Now the LSL outlets are ready to provide data to other inlets in the network.

LSL Outlets
^^^^^^^^^^^

All stream outlets are of type `Pupil Capture`.

Primitive data streams consist of 5 channels *lsl.cf_double64*:
    - `diameter` (`-1.0` for gaze streams)
    - `confidence`
    - `timestamp`
    - `norm_pos.x`
    - `norm_pos.y`

Python Representation streams consist of 1 channel containing the
Python repr() string of the datum.

The plugin provides following outlets:

- When relaying pupil data:
    - Pupil Primitive Data - Eye 0
    - Pupil Primitive Data - Eye 1
    - Pupil Python Representation - Eye 0
    - Pupil Python Representation - Eye 1
- When relaying gaze data:
    - Gaze Primitive Data
    - Gaze Python Representation
- When relaying notifications:
    - Notifications
    
License
-------

Pupil LSL Relay
Copyright (C) 2012-2016 Pupil Labs

Distributed under the terms of the GNU Lesser General Public License (LGPL v3.0).
License details are in the file license.txt, distributed as part of this software.

'''

from time import sleep
import logging

import zmq
import zmq_tools
from pyre import zhelper

from plugin import Plugin
from pyglui import ui

import pylsl as lsl

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

NOTIFY_SUB_TOPIC = 'notify.'
PUPIL_SUB_TOPIC = 'pupil.'

class Pupil_Diameter_LSL_Relay(Plugin):
    """Plugin to relay Pupil Capture data to LSL

    All stream outlets are of type _Pupil Capture_.

    Primitive data streams consist of 5 channels (lsl.cf_double64):
        - diameter (-1.0 for gaze streams)
        - confidence
        - timestamp
        - norm_pos.x
        - norm_pos.y

    Python Representation streams consist of 1 channel containing the
    Python repr() string of the datum.

    The plugin provides following outlets:

        if self.relay_pupil:
            - Pupil Primitive Data - Eye 0
            - Pupil Primitive Data - Eye 1
            - Pupil Python Representation - Eye 0
            - Pupil Python Representation - Eye 1

        if self.relay_gaze:
            - Gaze Primitive Data
            - Gaze Python Representation

        if self.relay_notifications:
            - Notifications

    """
    def __init__(self, g_pool, relay_pupil=True,
                 relay_notifications=True):
        """Summary

        Args:
            relay_pupil (bool, optional): Relay pupil data
            relay_gaze (bool, optional): Relay gaze data
            relay_notifications (bool, optional): Relay notifications
        """
        super().__init__(g_pool)
        
        self.relay_pupil = relay_pupil
        self.relay_notifications = relay_notifications
        self.context = g_pool.zmq_ctx
        self.thread_pipe = zhelper.zthread_fork(self.context, self.thread_loop)

    def init_ui(self):
        """Initializes sidebar menu"""
        self.add_menu()
        self.menu.label = 'Pupil Diameter LSL Relay'

        def make_setter(sub_topic, attribute_name):
            def set_value(value):
                setattr(self, attribute_name, value)
                cmd = 'Subscribe' if value else 'Unsubscribe'
                self.thread_pipe.send_string(cmd, flags=zmq.SNDMORE)
                self.thread_pipe.send_string(sub_topic)
            return set_value

        help_str = ('Pupil LSL Relay subscribes to the Pupil ZMQ Backbone'
                    + ' and relays the incoming data using the lab'
                    + ' streaming layer.')
        self.menu.append(ui.Info_Text(help_str))
        self.menu.append(ui.Switch('relay_pupil', self,
                                   label='Relay pupil data',
                                   setter=make_setter(PUPIL_SUB_TOPIC,
                                                      'relay_pupil')))

        self.menu.append(ui.Switch('relay_notifications', self,
                                   label='Relay notifications',
                                   setter=make_setter(NOTIFY_SUB_TOPIC,
                                                      'relay_notifications')))
    def get_init_dict(self):
        """Store settings"""
        return {'relay_pupil': self.relay_pupil, 
                'relay_notifications': self.relay_notifications}

    def deinit_ui(self):
        self.remove_menu()

    def cleanup(self):
        """gets called when the plugin get terminated.
           This happens either voluntarily or forced.
        """
        self.shutdown_thread_loop()

    def shutdown_thread_loop(self):
        if self.thread_pipe:
            self.thread_pipe.send_string('Exit')
            while self.thread_pipe:
                sleep(.1)

    def thread_loop(self, context, pipe):
        """Background Relay Thread

        1. Connects to the ZMQ backbone
        2. Creates LSL outlets according to settings
        3. Subscibes to according topics
        4. Loop
        4.1. Relay data
        4.2. Handle un/subscription events
        4.3. Handle exit event
        5. Shutdown background thread
        """
        try:
            logger.info('Connecting to %s...' % self.g_pool.ipc_sub_url)
            inlet = zmq_tools.Msg_Receiver(context, self.g_pool.ipc_sub_url,
                                           block_until_connected=False)
            poller = zmq.Poller()
            poller.register(pipe, flags=zmq.POLLIN)
            poller.register(inlet.socket, flags=zmq.POLLIN)

            pupil_outlet = None
            notification_outlet = None
                        
            # initial subscription
            if self.relay_pupil:
                pupil_buffer = _create_buffer()
                pupil_outlet = _create_buffered_lsl_outlet(
                                    url=self.g_pool.ipc_sub_url,
                                    name='Buffered Pupil Diameter')
                inlet.subscribe(PUPIL_SUB_TOPIC)
            if self.relay_notifications:
                notification_outlet = _create_notify_lsl_outlet(
                                         url=self.g_pool.ipc_sub_url)
                inlet.subscribe(NOTIFY_SUB_TOPIC)

            while True:
                items = dict(poller.poll())

                if inlet.socket in items:
                    topic, payload = inlet.recv()
                    if (topic.startswith(PUPIL_SUB_TOPIC)
                            and pupil_outlet):
                        eyeid = payload['id']                        
                        sample = _generate_primitive_sample(payload)                    
                        pupil_buffer = _update_buffer(eyeid, 
                                                      pupil_buffer,
                                                      sample)
                        new_sample = buffer2sample(pupil_buffer)
                        pupil_outlet.push_sample(new_sample)
                        
                        logger.debug('send at ' + str(new_sample[-1]))
                        # del outlet  # delete reference

                    elif (topic.startswith(NOTIFY_SUB_TOPIC)
                          and notification_outlet):
                        sample = (payload['subject'], repr(payload))
                        notification_outlet.push_sample(sample)

                    else:
                        logger.debug('Did not handle topic "%s"' % topic)

                if pipe in items:
                    cmd = pipe.recv_string()
                    if cmd == 'Exit':
                        break

                    elif cmd == 'Subscribe':
                        topic = pipe.recv_string()
                        inlet.subscribe(topic)
                        if topic == PUPIL_SUB_TOPIC and not pupil_outlet:
                            pupil_outlets = self._create_pupil_lsl_outlets()
                        elif (topic == NOTIFY_SUB_TOPIC
                              and not notification_outlet):
                            notification_outlet = self._create_notify_lsl_outlet()
                        logger.debug('Subscribed to "%s"' % topic)

                    elif cmd == 'Unsubscribe':
                        topic = pipe.recv_string()
                        inlet.unsubscribe(topic)
                        if topic == PUPIL_SUB_TOPIC and pupil_outlets:
                            pupil_outlets = None
                        elif topic == NOTIFY_SUB_TOPIC and notification_outlet:
                            notification_outlet = None
                        logger.debug('Unubscribed from "%s"' % topic)

        except Exception as e:
            logger.error('Error during relaying data to LSL. '
                         + 'Unloading the plugin...')
            logger.error('Error details: %s' % e)
        finally:
            logger.debug('Shutting down background thread...')
            self.thread_pipe = None
            self.alive = False


# %% Helper static functions
def _append_acquisition_info(streaminfo):
    """Appends acquisition information to stream description"""
    acquisition = streaminfo.desc().append_child("acquisition")
    acquisition.append_child_value("manufacturer", "Pupil Labs")
    acquisition.append_child_value("source", "Pupil LSL Relay Plugin")

def _append_channel_info(streaminfo, channels):
    """Appends channel information to stream description"""
    xml_channels = streaminfo.desc().append_child("channels")
    for channel in channels:
        xml_channels.append_child("channel").append_child_value("label", 
                                                                 channel)
def _create_notify_lsl_outlet(url):
    """Creates notification outlet"""
    notification_info = lsl.StreamInfo(
        name='Notifications',
        type='Pupil Capture',
        channel_count=2,
        channel_format=lsl.cf_string,
        source_id='Notifications @ %s' % url)
    _append_channel_info(notification_info, ("subject",
                                                  "Python Representation"))
    _append_acquisition_info(notification_info)
    return lsl.StreamOutlet(notification_info)

        
def _create_buffered_lsl_outlet(url, name):
    """Create 5 channel primitive data outlet
    
    the five channels are "diameter_0", "diameter_1", "confidence_0", 
    "confidence_1", "timestamp"
    """
    stream_info = lsl.StreamInfo(
        name=name,
        type='Pupil Capture',
        channel_count=5,
        channel_format=lsl.cf_double64,
        source_id='%s @ %s' % (name, url))
    _append_channel_info(stream_info,
                              ("diameter_0", "diameter_1", "confidence_0", 
                               "confidence_1", "timestamp"))
    _append_acquisition_info(stream_info)
    return lsl.StreamOutlet(stream_info) 

def _generate_primitive_sample(payload):
    """Combine payload's primitive fields into sample
    
    returns
    -------
    sample:tuple[diameter, confidence, timestamp]
        a three-channel sample of one of the two eyes
    """
    return (payload.get('diameter', -1.0),
            payload['confidence'],
            payload['timestamp'])       
    
def _create_buffer():
    '''create the initial buffer for the current pupil diameter
    
    returns
    -------
    buffer: list
        a list of two tuples, each tuple representing each eyes buffer
        each tuple contains the last diameter, its confidence and the timestamp
        of this buffered values
    '''
    
    t0 = lsl.local_clock()
    return [ (-1., 0., t0), (-1., 0., t0)]


def buffer2sample(buffer):
    sample = [buffer[0][0], buffer[1][0], # diametees
              buffer[0][1], buffer[1][1], #confidences
              max(buffer[0][2], buffer[1][2])] #most recent timestamp
    
    return sample

def _update_buffer(eyeid:int, buffer:list, sample:tuple):
    buffer[eyeid] = sample
    return buffer
    
    
    
'''
publish once after each update -> consequence is roughly double the 
sampling rate of ~<125 Hz to ~<250 Hz
1/ ( (timestamp[-1]-timestamp[0])/np.asanyarray(chunk).shape[0] )
'''
    