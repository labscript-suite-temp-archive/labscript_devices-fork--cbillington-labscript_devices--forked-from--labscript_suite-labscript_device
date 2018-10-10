#####################################################################
#                                                                   #
# /NI_DAQmx/workers.py                                              #
#                                                                   #
# Copyright 2018, Monash University, JQI, Christopher Billington    #
#                                                                   #
# This file is part of the module labscript_devices, in the         #
# labscript suite (see http://labscriptsuite.org), and is           #
# licensed under the Simplified BSD License. See the license.txt    #
# file in the root of the project for the full license.             #
#                                                                   #
#####################################################################
from __future__ import division, unicode_literals, print_function, absolute_import
from labscript_utils import PY2

if PY2:
    str = unicode

import time
import threading
import logging

from PyDAQmx import *
from PyDAQmx.DAQmxConstants import *
from PyDAQmx.DAQmxTypes import *
from PyDAQmx.DAQmxCallBack import *

import numpy as np
import labscript_utils.h5_lock
import h5py
from zprocess import Event

import labscript_utils.properties as properties
from labscript_utils import dedent
from labscript_utils.connections import _ensure_str
from labscript_utils.numpy_dtype_workaround import dtype_workaround

from blacs.tab_base_classes import Worker

from .utils import split_conn_port, split_conn_DO, split_conn_AI


class NI_DAQmxOutputWorker(Worker):
    def init(self):
        self.check_version()
        # Reset Device: clears previously added routes etc. Note: is insufficient for
        # some devices, which require power cycling to truly reset.
        # DAQmxResetDevice(self.MAX_name)
        self.setup_manual_mode_tasks()

    def stop_and_clear_tasks(self):
        if self.AO_task is not None:
            self.AO_task.StopTask()
            self.AO_task.ClearTask()
            self.AO_task = None
        if self.DO_task is not None:
            self.DO_task.StopTask()
            self.DO_task.ClearTask()
            self.DO_task = None

    def shutdown(self):
        self.stop_and_clear_tasks()

    def check_version(self):
        """Check the version of PyDAQmx is high enough to avoid a known bug"""
        major = uInt32()
        minor = uInt32()
        patch = uInt32()
        DAQmxGetSysNIDAQMajorVersion(major)
        DAQmxGetSysNIDAQMinorVersion(minor)
        DAQmxGetSysNIDAQUpdateVersion(patch)

        if major.value == 14 and minor.value < 2:
            msg = """There is a known bug with buffered shots using NI DAQmx v14.0.0.
                This bug does not exist on v14.2.0. You are currently using v%d.%d.%d.
                Please ensure you upgrade to v14.2.0 or higher."""
            raise Exception(dedent(msg) % (major.value, minor.value, patch.value))

    def setup_manual_mode_tasks(self):
        # Create tasks:
        if self.num_AO > 0:
            self.AO_task = Task()
            self.AO_data = np.zeros((self.num_AO,), dtype=np.float64)
        else:
            self.AO_task = None

        if self.ports:
            num_DO = sum(port['num_lines'] for port in self.ports.values())
            self.DO_task = Task()
            self.DO_data = np.zeros(num_DO, dtype=np.uint8)
        else:
            self.DO_task = None

        # Setup AO channels
        for i in range(self.num_AO):
            con = self.MAX_name + "/ao%d" % i
            self.AO_task.CreateAOVoltageChan(
                con, "", self.Vmin, self.Vmax, DAQmx_Val_Volts, None
            )

        # Setup DO channels
        for port_str in sorted(self.ports, key=split_conn_port):
            num_lines = self.ports[port_str]["num_lines"]
            # need to create chans in multiples of 8:
            ranges = []
            for i in range(num_lines // 8):
                ranges.append((8 * i, 8 * i + 7))
            div, remainder = divmod(num_lines, 8)
            if remainder:
                ranges.append((div * 8, div * 8 + remainder - 1))
            for start, stop in ranges:
                con = '%s/%s/line%d:%d' % (self.MAX_name, port_str, start, stop)
                self.DO_task.CreateDOChan(con, "", DAQmx_Val_ChanForAllLines)

        # Start tasks:
        if self.AO_task is not None:
            self.AO_task.StartTask()
        if self.DO_task is not None:
            self.DO_task.StartTask()

    def program_manual(self, front_panel_values):
        written = int32()
        for i in range(self.num_AO):
            self.AO_data[i] = front_panel_values['ao%d' % i]
        if self.AO_task is not None:
            self.AO_task.WriteAnalogF64(
                1, True, 1, DAQmx_Val_GroupByChannel, self.AO_data, byref(written), None
            )
        for i, conn in enumerate(self.DO_hardware_names):
            self.DO_data[i] = front_panel_values[conn]
        if self.DO_task is not None:
            self.DO_task.WriteDigitalLines(
                1, True, 1, DAQmx_Val_GroupByChannel, self.DO_data, byref(written), None
            )
        # TODO: return coerced/quantised values
        return {}

    def get_output_tables(self, h5file, device_name):
        """Return the AO and DO tables rom the file, or None if they do not exist."""
        with h5py.File(h5file, 'r') as hdf5_file:
            group = hdf5_file['devices'][device_name]
            try:
                AO_table = group['AO'][:]
            except KeyError:
                AO_table = None
            try:
                DO_table = group['DO'][:]
            except KeyError:
                DO_table = None
        return AO_table, DO_table

    def set_mirror_clock_terminal_connected(self, connected):
        """Mirror the clock terminal on another terminal to allow daisy chaining of the
        clock line to other devices, if applicable"""
        if self.clock_mirror_terminal is None:
            return
        if connected:
            DAQmxConnectTerms(
                self.clock_terminal,
                self.clock_mirror_terminal,
                DAQmx_Val_DoNotInvertPolarity,
            )
        else:
            DAQmxDisconnectTerms(self.clock_terminal, self.clock_mirror_terminal)

    def program_buffered_DO(self, DO_table):
        """Create the DO task and program in the DO table for a shot. Return a
        dictionary of the final values of each channel in use"""
        if DO_table is None:
            return {}
        self.DO_task = Task()
        written = int32()
        ports = DO_table.dtype.names

        final_values = {}
        for port_str in ports:
            # Add each port to the task:
            con = '%s/%s' % (self.MAX_name, port_str)
            self.DO_task.CreateDOChan(con, "", DAQmx_Val_ChanForAllLines)

            # Collect the final values of the lines on this port:
            port_final_value = DO_table[port_str][-1]
            for line in range(self.ports[port_str]["num_lines"]):
                # Extract each digital value from the packed bits:
                line_final_value = bool((1 << line) & port_final_value)
                final_values['%s/line%d' % (port_str, line)] = int(line_final_value)

        # Methods for writing data to the task depending on the datatype of each port:
        write_methods = {
            np.uint8: self.DO_task.WriteDigitalU8,
            np.uint16: self.DO_task.WriteDigitalU16,
            np.uint32: self.DO_task.WriteDigitalU32,
        }

        if self.static_DO:
            # Static DO. Start the task and write data, no timing configuration.
            self.DO_task.StartTask()
            # Write data for each port:
            for port_str in ports:
                data = DO_table[port_str]
                write_method = write_methods[data.dtype.type]
                write_method(
                    1,  # npts
                    False,  # autostart
                    10.0,  # timeout
                    DAQmx_Val_GroupByChannel,
                    data,
                    byref(written),
                    None,
                )
        else:
            # We use all but the last sample (which is identical to the second last
            # sample) in order to ensure there is one more clock tick than there are
            # samples. This is required by some devices to determine that the task has
            # completed.
            npts = len(DO_table) - 1

            # Set up timing:
            self.DO_task.CfgSampClkTiming(
                self.clock_terminal,
                self.clock_limit,
                DAQmx_Val_Rising,
                DAQmx_Val_FiniteSamps,
                npts,
            )

            # Write data for each port:
            for port_str in ports:
                # All but the last sample as mentioned above
                data = DO_table[port_str][:-1]
                write_method = write_methods[data.dtype.type]
                write_method(
                    npts,
                    False,  # autostart
                    10.0,  # timeout
                    DAQmx_Val_GroupByChannel,
                    data,
                    byref(written),
                    None,
                )

            # Go!
            self.DO_task.StartTask()

        return final_values

    def program_buffered_AO(self, AO_table):
        if AO_table is None:
            return {}
        self.AO_task = Task()
        written = int32()
        channels = ', '.join(self.MAX_name + '/' + c for c in AO_table.dtype.names)
        self.AO_task.CreateAOVoltageChan(
            channels, "", self.Vmin, self.Vmax, DAQmx_Val_Volts, None
        )

        # Collect the final values of the analog outs:
        final_values = dict(zip(AO_table.dtype.names, AO_table[-1]))

        # Obtain a view that is a regular array:
        AO_table = AO_table.view((AO_table.dtype[0], len(AO_table.dtype.names)))
        # And convert to 64 bit floats:
        AO_table = AO_table.astype(np.float64)

        if self.static_AO:
            # Static AO. Start the task and write data, no timing configuration.
            self.AO_task.StartTask()
            self.AO_task.WriteAnalogF64(
                1, True, 10.0, DAQmx_Val_GroupByChannel, AO_table, byref(written), None
            )
        else:
            # We use all but the last sample (which is identical to the second last
            # sample) in order to ensure there is one more clock tick than there are
            # samples. This is required by some devices to determine that the task has
            # completed.
            npts = len(AO_table) - 1

            # Set up timing:
            self.AO_task.CfgSampClkTiming(
                self.clock_terminal,
                self.clock_limit,
                DAQmx_Val_Rising,
                DAQmx_Val_FiniteSamps,
                npts,
            )

            # Write data:
            self.AO_task.WriteAnalogF64(
                npts,
                False,  # autostart
                10.0,  # timeout
                DAQmx_Val_GroupByScanNumber,
                AO_table[:-1],  # All but the last sample as mentioned above
                byref(written),
                None,
            )

            # Go!
            self.AO_task.StartTask()

        return final_values

    def transition_to_buffered(self, device_name, h5file, initial_values, fresh):
        # Store the initial values in case we have to abort and restore them:
        self.initial_values = initial_values

        # Stop the manual mode output tasks, if any:
        self.stop_and_clear_tasks()

        # Get the data to be programmed into the output tasks:
        AO_table, DO_table = self.get_output_tables(h5file, device_name)

        # Mirror the clock terminal, if applicable:
        self.set_mirror_clock_terminal_connected(True)

        # Program the output tasks and retrieve the final values of each output:
        DO_final_values = self.program_buffered_DO(DO_table)
        AO_final_values = self.program_buffered_AO(AO_table)

        final_values = {}
        final_values.update(DO_final_values)
        final_values.update(AO_final_values)

        return final_values

    def transition_to_manual(self, abort=False):
        # Stop output tasks and call program_manual. Only call StopTask if not aborting.
        # Otherwise results in an error if output was incomplete. If aborting, call
        # ClearTask only.
        npts = uInt64()
        samples = uInt64()
        tasks = []
        if self.AO_task is not None:
            tasks.append([self.AO_task, self.static_AO, 'AO'])
            self.AO_task = None
        if self.DO_task is not None:
            tasks.append([self.DO_task, self.static_DO, 'DO'])
            self.DO_task = None

        for task, static, name in tasks:
            if not abort:
                if not static:
                    try:
                        # Wait for task completion with a 1 second timeout:
                        task.WaitUntilTaskDone(1)
                    finally:
                        # Log where we were up to in sample generation, regardless of
                        # whether the above succeeded:
                        task.GetWriteCurrWritePos(npts)
                        task.GetWriteTotalSampPerChanGenerated(samples)
                        # Detect -1 even though they're supposed to be unsigned ints, -1
                        # seems to indicate the task was not started:
                        current = samples.value if samples.value != 2 ** 64 - 1 else -1
                        total = npts.value if npts.value != 2 ** 64 - 1 else -1
                        msg = 'Stopping %s at sample %d of %d'
                        self.logger.info(msg, name, current, total)
                task.StopTask()
            task.ClearTask()

        # Remove the mirroring of the clock terminal, if applicable:
        self.set_mirror_clock_terminal_connected(False)

        # Set up manual mode tasks again:
        self.setup_manual_mode_tasks()
        if abort:
            # Reprogram the initial states:
            self.program_manual(self.initial_values)

        return True

    def abort_transition_to_buffered(self):
        return self.transition_to_manual(True)

    def abort_buffered(self):
        return self.transition_to_manual(True)


class NI_DAQmxAcquisitionWorker(Worker):
    MAX_READ_INTERVAL = 0.2
    MAX_READ_PTS = 10000

    def init(self):
        # Prevent interference between the read callback and the shutdown code:
        self.tasklock = threading.RLock()

        # Assigned on a per-task basis and cleared afterward:
        self.read_array = None
        self.task = None

        # Assigned on a per-shot basis and cleared afterward:
        self.buffered_mode = False
        self.h5_file = None
        self.acquired_data = None
        self.buffered_rate = None
        self.buffered_chans = None

        # Hard coded for now. Perhaps we will add functionality to enable
        # and disable inputs in manual mode, and adjust the rate:
        self.manual_mode_chans = ['ai%d' % i for i in range(self.num_AI)]
        self.manual_mode_rate = 1000

        # An event for knowing when the wait durations are known, so that we may use
        # them to chunk up acquisition data:
        self.wait_durations_analysed = Event('wait_durations_analysed')

        # Start task for manual mode
        self.start_task(self.manual_mode_chans, self.manual_mode_rate)

    def shutdown(self):
        if self.task is not None:
            self.stop_task()

    def read(self, task_handle, event_type, num_samples, callback_data=None):
        """Called as a callback by DAQmx while task is running. Also called by us to get
        remaining data just prior to stopping the task. Since the callback runs
        in a separate thread, we need to serialise access to instance variables"""
        samples_read = int32()
        with self.tasklock:
            if self.task is None or task_handle != self.task.taskHandle.value:
                # Task stopped already.
                return 0
            self.task.ReadAnalogF64(
                num_samples,
                -1,
                DAQmx_Val_GroupByScanNumber,
                self.read_array,
                self.read_array.size,
                samples_read,
                None,
            )
            # Select only the data read, and downconvert to 32 bit:
            data = self.read_array[: int(samples_read.value), :].astype(np.float32)
            if self.buffered_mode:
                # Append to the list of acquired data:
                self.acquired_data.append(data)
            else:
                # TODO: Send it to the broker thingy.
                pass
        return 0

    def start_task(self, chans, rate):
        """Set up a task that acquires data with a callback every MAX_READ_PTS points or
        MAX_READ_INTERVAL seconds, whichever is faster. NI DAQmx calls callbacks in a
        separate thread, so this method returns, but data acquisition continues until
        stop_task() is called. Data is appended to self.acquired_data if
        self.buffered_mode=True, or (TODO) sent to the [whatever the AI server broker is
        called] if self.buffered_mode=False."""

        if self.task is not None:
            raise RuntimeError('Task already running')

        num_chans = len(chans)

        # Get data MAX_READ_PTS points at a time or once every MAX_READ_INTERVAL
        # seconds, whichever is faster:
        num_samples = min(self.MAX_READ_PTS, int(rate * self.MAX_READ_INTERVAL))

        self.read_array = np.zeros((num_samples, num_chans), dtype=np.float64)
        self.task = Task()

        for chan in chans:
            self.task.CreateAIVoltageChan(
                self.MAX_name + '/' + chan,
                "",
                DAQmx_Val_RSE,
                self.AI_range[0],
                self.AI_range[1],
                DAQmx_Val_Volts,
                None,
            )

        self.task.CfgSampClkTiming(
            "", rate, DAQmx_Val_Rising, DAQmx_Val_ContSamps, num_samples
        )
        if self.buffered_mode:
            self.task.CfgDigEdgeStartTrig(self.clock_terminal, DAQmx_Val_Rising)

        # This must not be garbage collected until the task is:
        self.task.callback_ptr = DAQmxEveryNSamplesEventCallbackPtr(self.read)

        self.task.RegisterEveryNSamplesEvent(
            DAQmx_Val_Acquired_Into_Buffer, num_samples, 0, self.task.callback_ptr, 100
        )

        self.task.StartTask()

    def stop_task(self):
        with self.tasklock:
            if self.task is None:
                raise RuntimeError('Task not running')
            # Read remaining data:
            self.read(self.task, None, -1)
            # Stop the task:
            self.task.StopTask()
            self.task.ClearTask()
            self.task = None
            self.read_array = None

    def transition_to_buffered(self, device_name, h5file, initial_values, fresh):
        self.logger.debug('transition_to_buffered')

        # read channels, acquisition rate, etc from H5 file
        with h5py.File(h5file, 'r') as f:
            group = f['/devices/' + device_name]
            if 'AI' not in group:
                # No acquisition
                return {}
            AI_table = group['AI'][:]
            device_properties = properties.get(f, device_name, 'device_properties')

        chans = [_ensure_str(c) for c in AI_table['connection']]
        # Remove duplicates and sort:
        self.buffered_chans = sorted(set(chans), key=split_conn_AI)
        self.h5_file = h5file
        self.buffered_rate = device_properties['acquisition_rate']
        self.acquired_data = []
        # Stop the manual mode task and start the buffered mode task:
        self.stop_task()
        self.buffered_mode = True
        self.start_task(self.buffered_chans, self.buffered_rate)
        return {}

    def transition_to_manual(self, abort=False):
        self.logger.debug('transition_to_manual')
        #  If we were doing buffered mode acquisition, stop the buffered mode task and
        # start the manual mode task. We might not have been doing buffered mode
        # acquisition if abort() was called when we are not in buffered mode, or if
        # there were no acuisitions this shot.
        if not self.buffered_mode:
            return

        self.stop_task()
        self.buffered_mode = False
        self.logger.info('transitioning to manual mode, task stopped')
        self.start_task(self.manual_mode_chans, self.manual_mode_rate)
            
        if abort:
            self.acquired_data = None
            self.buffered_chans = None
            self.h5_file = None
            self.buffered_rate = None
            return

        with h5py.File(self.h5_file, 'a') as hdf5_file:
            data_group = hdf5_file['data']
            data_group.create_group(self.device_name)
            waits_in_use = len(hdf5_file['waits']) > 0

        # Concatenate our chunks of acquired data and recast them as a structured
        # array with channel names:
        start_time = time.time()
        dtypes = [(chan, np.float32) for chan in self.buffered_chans]
        raw_data = np.concatenate(self.acquired_data).view(dtype_workaround(dtypes))
        raw_data = raw_data.reshape((len(raw_data),))
        self.acquired_data = None
        self.buffered_chans = None
        self.extract_measurements(raw_data, waits_in_use)
        self.h5_file = None
        self.buffered_rate = None
        msg = 'data written, time taken: %ss' % str(time.time() - start_time)
        self.logger.info(msg)

        return True

    def extract_measurements(self, raw_data, waits_in_use):
        self.logger.debug('extract_measurements')
        if waits_in_use:
            # There were waits in this shot. We need to wait until the other process has
            # determined their durations before we proceed:
            self.wait_durations_analysed.wait(self.h5_file)

        with h5py.File(self.h5_file, 'a') as hdf5_file:
            if waits_in_use:
                # get the wait start times and durations
                waits = hdf5_file['/data/waits']
                wait_times = waits['time']
                wait_durations = waits['duration']
            try:
                acquisitions = hdf5_file['/devices/' + self.device_name + '/AI']
            except KeyError:
                # No acquisitions!
                return
            try:
                measurements = hdf5_file['/data/traces']
            except KeyError:
                # Group doesn't exist yet, create it:
                measurements = hdf5_file.create_group('/data/traces')

            t0 = self.AI_start_delay
            for connection, label, t_start, t_end, _, _, _ in acquisitions:
                connection = _ensure_str(connection)
                label = _ensure_str(label)
                if waits_in_use:
                    # add durations from all waits that start prior to t_start of
                    # acquisition
                    t_start += wait_durations[(wait_times < t_start)].sum()
                    # compare wait times to t_end to allow for waits during an
                    # acquisition
                    t_end += wait_durations[(wait_times < t_end)].sum()
                i_start = int(np.ceil(self.buffered_rate * (t_start - t0)))
                i_end = int(np.floor(self.buffered_rate * (t_end - t0)))
                # np.ceil does what we want above, but float errors can miss the
                # equality:
                if t0 + (i_start - 1) / self.buffered_rate - t_start > -2e-16:
                    i_start -= 1
                # We want np.floor(x) to yield the largest integer < x (not <=):
                if t_end - t0 - i_end / self.buffered_rate < 2e-16:
                    i_end -= 1
                t_i = t0 + i_start / self.buffered_rate
                t_f = t0 + i_end / self.buffered_rate
                times = np.linspace(t_i, t_f, i_end - i_start + 1, endpoint=True)
                values = raw_data[connection][i_start : i_end + 1]
                dtypes = [('t', np.float64), ('values', np.float32)]
                data = np.empty(len(values), dtype=dtype_workaround(dtypes))
                data['t'] = times
                data['values'] = values
                measurements.create_dataset(label, data=data)

    def abort_buffered(self):
        return self.transition_to_manual(True)

    def abort_transition_to_buffered(self):
        return self.transition_to_manual(True)

    def program_manual(self, values):
        return {}


class Ni_DAQmxWaitMonitorWorker(Worker):
    def init(self):
        self.h5_file = None
        self.CI_task = None
        self.DO_task = None
        self.abort = False
        self.all_waits_finished = Event('all_waits_finished', type='post')
        self.wait_durations_analysed = Event('wait_durations_analysed', type='post')
        self.wait_completed = Event('wait_completed', type='post')
    
    def shutdown(self):
        self.logger.info('Shutdown requested, stopping task')
        if self.CI_task is not None or self.DO_task is not None:
            self.stop_tasks()    
    
    def wait_for_rising_edge(self, timeout=None):
        """Wait up to the given timeout in seconds for a rising edge on the wait monitor
        and and return the duration since the last falling edge. Return None upon
        timeout."""
        samples_read = int32()
        # If no timeout, call read repeatedly with a 0.2 second timeout to ensure we don't
        # block indefinitely and can still abort.
        if timeout is None:
            read_timeout = 0.2
        else:
            read_timeout = timeout
        read_array = np.empty(1)
        while True:
            if self.abort:
                raise RuntimeError('Aborted')
            try:
                self.acquisition_task.ReadCounterF64(
                    1, read_timeout, read_array, 1, samples_read, None
                )
            except SamplesNotYetAvailableError:
                if timeout is None:
                    continue
                return None
            return read_array[0]

    def daqmx_read(self):
        self.logger.info('Starting counter read loop')
        with self.kill_lock:
            # Ignore the initial low-time 
            self.wait_for_rising_edge()
            # Alright, we're now a short way into the experiment.
            for wait in self.wait_table:
                # How long until this wait should time out?
                timeout = wait['time'] + wait['timeout'] - current_time
                timeout = max(timeout, 0)  # ensure non-negative
                # Wait that long for the next pulse:
                half_period = self.wait_for_edge(timeout)
                # Did the wait finish of its own accord, or time out?
                if half_period is None:
                    # It timed out. Better trigger the clock to resume!
                    msg = """Wait timed out; retriggering clock with {:.3e} s pulse
                        ({} edge)"""
                    msg = dedent(msg).format(pulse_width, self.timeout_trigger_type)
                    self.logger.info(msg)
                    self.send_resume_trigger(pulse_width)
                    # Wait for it to respond to that:
                    self.logger.info('Waiting for edge on WaitMonitor')
                    self.wait_for_edge()
                # Alright, now we're at the end of the wait.
                self.logger.info('Wait completed')
                current_time = wait['time']
                # Inform any interested parties that a wait has completed:
                postdata = _ensure_str(wait['label'])
                self.wait_completed.post(self.h5_file, data=postdata)
                # Wait for the end of the pulse:
                current_time += self.wait_for_edge()
            # Inform any interested parties that waits have all finished:
            self.logger.info('All waits finished')
            self.all_waits_finished.post(self.h5_file)
    
    def send_resume_trigger(self, pulse_width):
        written = int32()
        if self.timeout_trigger_type == 'rising':
            trigger_value = 1
            rearm_value = 0
        elif self.timeout_trigger_type == 'falling':
            trigger_value = 0
            rearm_value = 1
        else:
            raise ValueError('timeout_trigger_type of {}_{} must be either "rising" or "falling".'.format(self.device_name, self.worker_name))
        # Triggering edge:
        self.timeout_task.WriteDigitalLines(1, True, 1, DAQmx_Val_GroupByChannel, np.array([trigger_value], dtype=np.uint8), byref(written), None)
        assert written.value == 1
        # Wait however long we observed the first pulse of the experiment to be:
        time.sleep(pulse_width)
        # Rearm trigger
        self.timeout_task.WriteDigitalLines(1, True, 1, DAQmx_Val_GroupByChannel, np.array([rearm_value], dtype=np.uint8), byref(written), None)
        assert written.value == 1
        
    def stop_task(self):
        self.logger.debug('stop_task')
        with self.daqlock:
            self.logger.debug('stop_task got daqlock')
            if self.task_running:
                self.task_running = False
                self.acquisition_task.StopTask()
                self.acquisition_task.ClearTask()
                self.timeout_task.StopTask()
                self.timeout_task.ClearTask()
        self.logger.debug('finished stop_task')
        
    def transition_to_buffered(self,device_name,h5file,initial_values,fresh):
        self.logger.debug('transition_to_buffered')
        # Save h5file path (for storing data later!)
        self.h5_file = h5file
        self.is_wait_monitor_device = False # Will be set to true in a moment if necessary
        self.logger.debug('setup_task')
        with h5py.File(h5file, 'r') as hdf5_file:
            dataset = hdf5_file['waits']
            if len(dataset) == 0:
                # There are no waits. Do nothing.
                self.logger.debug('There are no waits, not transitioning to buffered')
                self.waits_in_use = False
                self.wait_table = np.zeros((0,))
                return {}
            self.waits_in_use = True
            acquisition_device = dataset.attrs['wait_monitor_acquisition_device']
            acquisition_connection = dataset.attrs['wait_monitor_acquisition_connection']
            timeout_device = dataset.attrs['wait_monitor_timeout_device']
            timeout_connection = dataset.attrs['wait_monitor_timeout_connection']
            try:
                self.timeout_trigger_type = dataset.attrs['wait_monitor_timeout_trigger_type']
            except KeyError:
                self.timeout_trigger_type = 'rising'
            self.wait_table = dataset[:]
        # Only do anything if we are in fact the wait_monitor device:
        if timeout_device == device_name or acquisition_device == device_name:
            if not timeout_device == device_name and acquisition_device == device_name:
                raise NotImplementedError("Ni_DAQmx worker must be both the wait monitor timeout device and acquisition device." +
                                          "Being only one could be implemented if there's a need for it, but it isn't at the moment")
            
            self.is_wait_monitor_device = True
            # The counter acquisition task:
            self.acquisition_task = Task()
            acquisition_chan = '/'.join([self.MAX_name,acquisition_connection])
            self.acquisition_task.CreateCISemiPeriodChan(acquisition_chan, '', 100e-9, 200, DAQmx_Val_Seconds, "")    
            self.acquisition_task.CfgImplicitTiming(DAQmx_Val_ContSamps, 1000)
            self.acquisition_task.StartTask()
            # The timeout task:
            self.timeout_task = Task()
            timeout_chan = '/'.join([self.MAX_name,timeout_connection])
            self.timeout_task.CreateDOChan(timeout_chan,"",DAQmx_Val_ChanForAllLines)
            # Ensure timeout trigger is armed
            if self.timeout_trigger_type == 'falling':
                written = int32()
                self.timeout_task.WriteDigitalLines(1, True, 1, DAQmx_Val_GroupByChannel, np.array([1], dtype=np.uint8), byref(written), None)
                assert written.value == 1
            self.task_running = True
                
            # An array to store the results of counter acquisition:
            self.half_periods = []
            self.read_thread = threading.Thread(target=self.daqmx_read)
            # Not a daemon thread, as it implements wait timeouts - we need it to stay alive if other things die.
            self.read_thread.start()
            self.logger.debug('finished transition to buffered')
            
        return {}
    
    def transition_to_manual(self, abort=False):
        self.logger.debug('transition_to_static')
        self.abort = abort
        self.stop_task()
        # Reset the abort flag so that unexpected exceptions are still raised:        
        self.abort = False
        self.logger.info('transitioning to static, task stopped')
        # save the data acquired to the h5 file
        if not abort:
            if self.is_wait_monitor_device and self.waits_in_use:
                # Let's work out how long the waits were. The absolute times of each edge on the wait
                # monitor were:
                edge_times = np.cumsum(self.half_periods)
                # Now there was also a rising edge at t=0 that we didn't measure:
                edge_times = np.insert(edge_times,0,0)
                # Ok, and the even-indexed ones of these were rising edges.
                rising_edge_times = edge_times[::2]
                # Now what were the times between rising edges?
                periods = np.diff(rising_edge_times)
                # How does this compare to how long we expected there to be between the start
                # of the experiment and the first wait, and then between each pair of waits?
                # The difference will give us the waits' durations.
                resume_times = self.wait_table['time']
                # Again, include the start of the experiment, t=0:
                resume_times =  np.insert(resume_times,0,0)
                run_periods = np.diff(resume_times)
                wait_durations = periods - run_periods
                waits_timed_out = wait_durations > self.wait_table['timeout']
            with h5py.File(self.h5_file,'a') as hdf5_file:
                # Work out how long the waits were, save em, post an event saying so 
                dtypes = [('label','a256'),('time',float),('timeout',float),('duration',float),('timed_out',bool)]
                data = np.empty(len(self.wait_table), dtype=dtype_workaround(dtypes))
                if self.is_wait_monitor_device and self.waits_in_use:
                    data['label'] = self.wait_table['label']
                    data['time'] = self.wait_table['time']
                    data['timeout'] = self.wait_table['timeout']
                    data['duration'] = wait_durations
                    data['timed_out'] = waits_timed_out
                if self.is_wait_monitor_device:
                    hdf5_file.create_dataset('/data/waits', data=data)
            if self.is_wait_monitor_device:
                self.wait_durations_analysed.post(self.h5_file)
        
        return True
    
    def abort_buffered(self):
        return self.transition_to_manual(True)
        
    def abort_transition_to_buffered(self):
        return self.transition_to_manual(True)   
    
    def program_manual(self,values):
        return {}