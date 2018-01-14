#!/usr/bin/env python

import subprocess
import time
import calendar
import signal
import sys
import logging
import numpy as np
import mqtt_poster
import config_logging

# Configure logging and log a restart of the app
config_logging.configure_logging(logging, '/var/log/rtl433_reader.log')
logging.warning('rtl433_reader has restarted')

# The settings file is installed in the FAT boot partition of the Pi SD card,
# so that it can be easily configured from the PC that creates the SD card.  
# Include that directory in the Path so the settings file can be found.
sys.path.insert(0, '/boot/pi_logger')
import settings

def shutdown(signum, frame):
    '''Kills the external processes that were started by this script
    '''
    rtl433.kill()
    sys.exit(0)

# If process is being killed, go through shutdown process
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Start up the object that will post the final readings to the MQTT
# broker.
mqtt = mqtt_poster.MQTTposter()
mqtt.start()

# determine the time when the next summarized post will occur
next_log_time = time.time() + settings.LOG_INTERVAL

# get the base Logger ID from the settings file
logger_id = settings.LOGGER_ID

# start the RTL 433 reading command line program
rtl433 = subprocess.Popen(['/usr/local/bin/rtl_433', '-R40', '-Fjson', '-U'], stdout=subprocess.PIPE)

# create a dictionary to hold all the readings collected during a
# logging interval.  The keys of the dictionary will be the reading ID,
# the values will be a list of readings, each reading being a tuple of the form:
# (timestamp, value)
final_read_data = {}

def add_readings(ts, id, values):
    """Adds a temperature and RH reading to the final reading dictionary.
    'ts' is a Unix timestamp (seconds past the Epoch)
    'id' is the ID number transmitted by the sensor.
    'values' is a two-tuple: (temperature reading deg F, RH in %)
    """
    val_temp, val_rh = values   # split into temp and RH readings

    # Add temperature reading
    final_id = '%s_%s_temp' % (logger_id, id)
    read_list = final_read_data.get(final_id, [])
    read_list.append( (ts, val_temp))
    final_read_data[final_id] = read_list

    # Add RH reading
    final_id = '%s_%s_rh' % (logger_id, id)
    read_list = final_read_data.get(final_id, [])
    read_list.append( (ts, val_rh))
    final_read_data[final_id] = read_list


# These hold the raw readings coming from the RTL-SDR receiver.  The keys
# are a two-tuple (date/time string, id).  The values are a list of two-tuples
# (temperature in deg F, humidity in %).
# These readings are transmitted three times in a row from the sensor.
# The code below adds all the received transmissions to the 'readings' dictionary
# and then considers them valid if there are two transmissions that match.
readings = {}

while True:
    lin = rtl433.stdout.readline().strip()
    flds = eval(lin)
    k = (flds['time'], flds['id'])
    val_list = readings.get(k, [])
    val_list.append( (flds['temperature_C'] * 1.8 + 32.0, flds['humidity']) )
    readings[k] = val_list

    # process any items in the reading list that have 3 values or are older than 3 seconds ago
    for ky, vals in readings.items():
        ts_str, id = ky
        ts = time.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        ts = calendar.timegm(ts)
        if len(vals) == 3 or (time.time() - ts >= 3.0):
            del readings[ky]
            
            # only process if there are at least two readings
            if len(vals) >= 2:
                # sort the readings
                vals = sorted(vals)
                # try to find two matching readings
                if vals[0] == vals[1]:
                    add_readings(ts, id, vals[0])
                    print ts_str, id, vals[0], len(vals)
                elif vals[-1] == vals[-2]:
                    add_readings(ts, id, vals[-1])
                    print ts_str, id, vals[-1], len(vals)

    # see if it is time to post summarized readings
    if time.time() > next_log_time:
        next_log_time += settings.LOG_INTERVAL

        lines_to_post = []
        for reading_id, reading_list in final_read_data.items():
            # make a separate numpy array of values and time stamps
            rd_arr = np.array(reading_list)
            ts_arr = rd_arr[:, 0]  # first column
            val_arr = rd_arr[:, 1]  # second column
            # calculate the average value to log
            val_avg = val_arr.mean()
            # limit this value to 5 significant figures
            val_avg = float('%.5g' % val_avg)
            # calculate the average timestamp to log and convert to
            # integer seconds
            ts_avg = int(ts_arr.mean())
            lines_to_post.append(
                '%s\t%s\t%s' % (ts_avg, reading_id, val_avg)

            )

        # clear out readings to prep for next logging period
        final_read_data = {}

        # Post the summarized readings
        if len(lines_to_post):
            mqtt.publish(
                'readings/final/rtl433_reader',
                '\n'.join(lines_to_post)
            )
            logging.info('%d readings posted.' % len(lines_to_post))
