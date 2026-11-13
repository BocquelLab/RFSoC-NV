# -*- coding: utf-8 -*-
"""
rfsoc_nv: PC-side tools for the BocquelLab RFSoC4x2 (QICK, qick-dawg photon counting firmware).

- ``QickBoard``     connection to the board (or to the simulator), gain limit, one user at a time
- ``SimulatedSoc``  simulated board: runs the compiled tProc program and returns synthetic counts
- ``SteppedCW``     continuous microwave output with a frequency list stepped from the PC (CW ODMR)
- ``PulseSequencer`` endless pattern of digital outputs and microwave pulses (pulsed measurements)
"""

from .board import QickBoard, BoardBusyError, BoardConnectionError, GainLimitError
from .simulator import SimulatedSoc, NVWorld
from .tproc_v1 import decode_program, TProcError
from .cw import SteppedCW
from .pulses import PulseSequencer

__all__ = ['QickBoard', 'BoardBusyError', 'BoardConnectionError', 'GainLimitError', 'SimulatedSoc', 'NVWorld',
           'decode_program', 'TProcError', 'SteppedCW', 'PulseSequencer']
__version__ = '0.3.0'
