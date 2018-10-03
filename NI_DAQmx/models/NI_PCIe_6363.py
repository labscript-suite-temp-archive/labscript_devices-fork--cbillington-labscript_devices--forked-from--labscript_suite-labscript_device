#####################################################################
#                                                                   #
# /NI_DAQmx/models/_subclass_template.py                            #
#                                                                   #
# Copyright 2018, Christopher Billington                            #
#                                                                   #
# This file is part of the module labscript_devices, in the         #
# labscript suite (see http://labscriptsuite.org), and is           #
# licensed under the Simplified BSD License. See the license.txt    #
# file in the root of the project for the full license.             #
#                                                                   #
#####################################################################

#####################################################################
#     WARNING                                                       #
#                                                                   #
# This file is auto-generated, any modifications may be             #
# overwritten. See README.txt in this folder for details            #
#                                                                   #
#####################################################################


from __future__ import division, unicode_literals, print_function, absolute_import
from labscript_utils import PY2

if PY2:
    str = unicode

from labscript_devices.NI_DAQmx.base_class import NI_DAQmx

CAPABILITIES = {
    'AO_range': [-10.0, 10.0],
    'max_AI_multi_chan_rate': 1000000.0,
    'max_AI_single_chan_rate': 2000000.0,
    'max_AO_sample_rate': 2857142.8571428573,
    'max_DO_sample_rate': 10000000.0,
    'num_AI': 32,
    'num_AO': 4,
    'num_CI': 4,
    'ports': {
        'port0': {'num_lines': 32, 'supports_buffered': True},
        'port1': {'num_lines': 8, 'supports_buffered': False},
        'port2': {'num_lines': 8, 'supports_buffered': False},
    },
    'supports_buffered_AO': True,
    'supports_buffered_DO': True,
}


class NI_PCIe_6363(NI_DAQmx):
    description = 'NI-PCIe-6363'

    def __init__(self, *args, **kwargs):
        # Any provided kwargs take precedent over capabilities
        combined_kwargs = CAPABILITIES.copy()
        combined_kwargs.update(kwargs)
        NI_DAQmx.__init__(self, *args, **combined_kwargs)
