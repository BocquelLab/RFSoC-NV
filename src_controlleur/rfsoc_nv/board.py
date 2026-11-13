# -*- coding: utf-8 -*-
"""
Client for the RFSoC4x2 running QICK (Pyro4 server started by startup/startup.py on the board).

``QickBoard`` owns the connection to the board, or to a ``SimulatedSoc`` when ``simulate=True``, and
is the single place where
- the connection is opened and checked (activation fails if the board is not reachable, runs another
  qick version or another kind of firmware),
- the hard generator gain limit is enforced: every program is inspected when it is loaded and refused
  (never clipped) if a generator could play above ``max_gain``,
- the board is handed to one user at a time (``claim`` / ``release``),
- the outputs are switched off (``outputs_off``): generators silent, all PMOD outputs low.

qick-dawg programs use the module globals ``qickdawg.soc`` / ``qickdawg.soccfg``; ``connect`` points
them to this board, so there can be only one connected ``QickBoard`` per Python process.
"""

import logging
import threading
import time

import numpy as np

import qick
from qick import QickConfig
from qick.asm_v1 import QickProgram

from .tproc_v1 import decode_program

__all__ = ['QickBoard', 'BoardBusyError', 'GainLimitError', 'BoardConnectionError']

_REGISTER_WRITERS = {  # instruction name -> index of the register it writes, in the decoded arguments
    'regwi': 1, 'mathi': 1, 'math': 1, 'bitwi': 1, 'bitw': 1, 'memri': 1, 'memr': 1, 'popi': 1, 'pushi': 1,
    'read': 3}
_COUNTER_ADDR = 1


class BoardConnectionError(RuntimeError):
    """ The board could not be reached or is not the board this software was written for. """


class BoardBusyError(RuntimeError):
    """ The board is in use by somebody else. """


class GainLimitError(ValueError):
    """ A program would drive a generator above the gain limit; it was not loaded. """


def _s16(val):
    val &= 0xFFFF
    return val - 0x10000 if val & 0x8000 else val


def _plain(obj):
    """ numpy scalars / arrays -> Python objects. The board runs numpy 1.x under Python 3.8 and cannot
    unpickle numpy 2 objects, so nothing numpy may be sent to it. """
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (list, tuple)):
        return type(obj)(_plain(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


class _GuardedSoc:
    """ What programs get as "soc": forwards every call to the board, one call at a time (a Pyro4 proxy
    may be shared between threads), and checks each program against the gain limit before it is loaded. """

    def __init__(self, board, raw, remote):
        self._board = board
        self._raw = raw
        self._remote = remote

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        board, raw, remote = self._board, self._raw, self._remote

        def call(*args, **kwargs):
            with board._io_lock:
                if name == 'load_bin_program':
                    board.check_program(args[0] if args else kwargs['binprog'])
                if remote:
                    args = _plain(args)
                    kwargs = _plain(kwargs)
                return getattr(raw, name)(*args, **kwargs)

        call.__name__ = name
        return call


class QickBoard:
    """ See the module docstring.

    @param float max_gain: mandatory hard limit of the generator gain (DAC units, 0 < max_gain <= 32766)
    @param str host: IP address or name of the board (Pyro4 name server)
    @param int port: port of the Pyro4 name server
    @param str proxy_name: name under which the QickSoc is registered
    @param bool simulate: use a SimulatedSoc instead of the board
    @param dict sim_options: keyword arguments of SimulatedSoc (world, realtime, seed, ...)
    @param float timeout: network timeout in seconds
    @param bool check_version: refuse a board that runs another qick version than this PC
    @param logger: logging.Logger-like object (default: logger of this module)
    """

    def __init__(self, max_gain, host='192.168.0.2', port=8888, proxy_name='myqick', simulate=False,
                 sim_options=None, timeout=10.0, check_version=True, logger=None):
        self.log = logger if logger is not None else logging.getLogger(__name__)
        self._max_gain = float(max_gain)
        self._host = host
        self._port = int(port)
        self._proxy_name = proxy_name
        self._simulate = bool(simulate)
        self._sim_options = dict(sim_options or {})
        self._timeout = float(timeout)
        self._check_version = bool(check_version)

        self._io_lock = threading.RLock()
        self._claim_lock = threading.Lock()
        self._owner = None
        self._raw = None
        self.soc = None
        self.soccfg = None
        self._gain_registers = {}
        if not 0 < self._max_gain <= 32766:
            raise ValueError(f'max_gain must be in ]0, 32766], got {max_gain}.')

    # ------------------------------------------------------------------ connection
    @property
    def simulate(self):
        return self._simulate

    @property
    def connected(self):
        return self.soc is not None

    @property
    def max_gain(self):
        return self._max_gain

    def connect(self):
        """ Open the connection and read the board description. Raises BoardConnectionError. """
        if self.connected:
            return
        if self._simulate:
            from .simulator import SimulatedSoc
            raw = SimulatedSoc(**self._sim_options)
            cfg = raw.get_cfg()
            where = 'simulated board'
        else:
            raw, cfg = self._connect_pyro()
            where = f'board at {self._host}:{self._port} ("{self._proxy_name}")'
        soccfg = QickConfig(cfg)
        try:
            self._check_board(cfg)
        except Exception:
            self._release_raw(raw)
            raise
        self._raw = raw
        self.soccfg = soccfg
        self.soc = _GuardedSoc(self, raw, remote=not self._simulate)
        self._gain_registers = self._find_gain_registers()

        import qickdawg as qd  # qick-dawg programs take the board from these module globals
        qd.soc = self.soc
        qd.soccfg = self.soccfg
        qd.max_int_time_treg = 2 ** 16 - 1
        qd.max_int_time_tus = soccfg.cycles2us(2 ** 16 - 1)
        qd.max_int_time_tns = qd.max_int_time_tus * 1000
        qd.min_time_tus = soccfg.cycles2us(1)
        qd.min_time_tns = qd.min_time_tus * 1000
        self.log.info(f'Connected to the {where}: {cfg["board"]}, qick {cfg["sw_version"]}, firmware built '
                      f'{cfg["fw_timestamp"]}. Generator gain limit {self._max_gain:.0f}.')

    def _connect_pyro(self):
        import Pyro4
        import Pyro4.errors
        Pyro4.config.SERIALIZER = 'pickle'
        Pyro4.config.PICKLE_PROTOCOL_VERSION = 4
        Pyro4.config.COMMTIMEOUT = self._timeout
        try:
            ns = Pyro4.locateNS(host=self._host, port=self._port)
            uri = ns.lookup(self._proxy_name)
            ns._pyroRelease()
            raw = Pyro4.Proxy(uri)
            raw._pyroTimeout = self._timeout
            cfg = raw.get_cfg()
        except Pyro4.errors.NamingError as err:
            raise BoardConnectionError(
                f'No QICK board found at {self._host}:{self._port} under the name "{self._proxy_name}" ({err}). '
                f'Is the board powered, booted and on the "Instruments" network?') from err
        except (Pyro4.errors.PyroError, OSError) as err:
            raise BoardConnectionError(
                f'No connection to the QICK board at {self._host}:{self._port} ({type(err).__name__}: {err}). '
                f'Is the board powered, booted and on the "Instruments" network?') from err
        return raw, cfg

    def _check_board(self, cfg):
        tproc = cfg['tprocs'][0]
        if tproc['type'] != 'axis_tproc64x32_x8':
            raise BoardConnectionError(f'The firmware has a "{tproc["type"]}" processor; this software is written '
                                       f'for the tProc v1 (axis_tproc64x32_x8).')
        if self._check_version and cfg['sw_version'] != qick.__version__:
            raise BoardConnectionError(f'The board runs qick {cfg["sw_version"]} but this PC has qick '
                                       f'{qick.__version__}. They must be the same version.')
        if not cfg['gens'] or not cfg['readouts']:
            raise BoardConnectionError('The firmware has no signal generator or no readout.')

    @staticmethod
    def _release_raw(raw):
        try:
            raw._pyroRelease()
        except AttributeError:
            pass
        except Exception:
            pass

    def close(self):
        """ Release the connection. Does not change the outputs: call outputs_off() first. """
        with self._io_lock:
            raw, self._raw = self._raw, None
            self.soc = None
            if raw is None:
                return
            if self._simulate:
                raw.stop_tproc()
            self._release_raw(raw)
        import qickdawg as qd
        if getattr(qd, 'soc', None) is not None and getattr(qd.soc, '_board', None) is self:
            qd.soc = None

    # ------------------------------------------------------------------ one user at a time
    @property
    def owner(self):
        return self._owner

    def claim(self, owner):
        """ Reserve the board for ``owner`` (any string). Raises BoardBusyError if somebody else has it. """
        with self._claim_lock:
            if self._owner is not None and self._owner != owner:
                raise BoardBusyError(f'The RFSoC board is in use by "{self._owner}"; "{owner}" has to wait until '
                                     f'that measurement is stopped.')
            self._owner = owner

    def release(self, owner):
        """ Give the board back. Only the present owner can release it. """
        with self._claim_lock:
            if self._owner == owner:
                self._owner = None

    # ------------------------------------------------------------------ gain limit
    def _find_gain_registers(self):
        """ {(page, register): generator channel} of the registers a generator takes its gain from. """
        prog = QickProgram(self.soccfg)
        registers = {}
        for (ch, name), (page, reg) in prog._gen_regmap.items():
            if name.startswith('gain'):
                registers[(page, reg)] = ch
        return registers

    def check_gain(self, gain):
        """ Raise GainLimitError if ``gain`` (DAC units) exceeds the limit. Returns the gain as int. """
        if abs(gain) > self._max_gain:
            raise GainLimitError(f'Generator gain {gain} exceeds the limit of {self._max_gain:.0f} (max_gain). '
                                 f'Nothing was changed.')
        return int(gain)

    def check_program(self, binprog):
        """ Inspect a compiled program before it is loaded. Raises
        - RuntimeError if it does not fit in the program memory,
        - GainLimitError if a generator gain register is loaded with more than max_gain, or is changed
          in a way whose result is not known before the program runs (e.g. an on-board gain sweep).
        @return int: number of program words
        """
        size = self.soccfg['tprocs'][0]['pmem_size']
        nwords = len(binprog)
        if nwords > size:
            raise RuntimeError(f'The program has {nwords} instructions, the tProc program memory holds {size}.')
        program = decode_program(binprog)
        # gain registers: the usual ones of qick programs, plus any register a "set" instruction on a
        # generator port takes its gain from (set port, page, $freq, $phase, $addr, $gain, $mode, $time)
        gain_registers = dict(self._gain_registers)
        gen_ports = {gen['tproc_ch']: ch for ch, gen in enumerate(self.soccfg['gens'])}
        for name, args in program:
            if name == 'set' and args[0] in gen_ports and args[5] != 0:
                gain_registers[(args[1], args[5])] = gen_ports[args[0]]
        for name, args in program:
            reg_index = _REGISTER_WRITERS.get(name)
            if reg_index is None:
                continue
            page = args[1] if name == 'read' else args[0]
            target = (page, args[reg_index])
            if target not in gain_registers:
                continue
            ch = gain_registers[target]
            if name != 'regwi':
                raise GainLimitError(f'The program changes the gain of generator {ch} while it runs ("{name}"), '
                                     f'so the gain limit of {self._max_gain:.0f} cannot be guaranteed. Not loaded.')
            gain = _s16(args[2])
            if abs(gain) > self._max_gain:
                raise GainLimitError(f'The program sets the gain of generator {ch} to {gain}, above the limit of '
                                     f'{self._max_gain:.0f} (max_gain). Not loaded.')
        return nwords

    # ------------------------------------------------------------------ outputs
    def outputs_off(self, timeout=5.0):
        """ Stop the tProc, silence all generators (also periodic pulses) and set all digital outputs low.
        Returns when the board has confirmed it. Raises RuntimeError if it does not within ``timeout``.
        """
        self._require_connection()
        soccfg = self.soccfg
        prog = QickProgram(soccfg)
        for ch in range(len(soccfg['gens'])):
            prog.set_pulse_registers(ch=ch, style='const', mode='oneshot', freq=0, phase=0, gain=0, length=3)
            prog.pulse(ch=ch, t=0)
        ports = {pin[1] for pin in soccfg['tprocs'][0]['output_pins']}
        ports |= {ro['trigger_port'] for ro in soccfg['readouts']}
        for port in sorted(ports):
            prog.seti(port, 0, 0, 0)  # register 0 reads 0: all bits of this output port low
        prog.synci(200)
        prog.waiti(0, 0)  # until the outputs above have happened
        prog.regwi(0, 15, 1)
        prog.memwi(0, 15, _COUNTER_ADDR)
        prog.end()
        with self._io_lock:
            soc = self.soc
            soc.start_src('internal')
            soc.stop_tproc(lazy=False)
            time.sleep(0.05)  # the tProc v1 needs some ms to reach its end state
            prog.compile()
            soc.load_bin_program(prog.binprog, load_mem=False)
            soc.set_tproc_counter(addr=_COUNTER_ADDR, val=0)
            soc.start_tproc()
            self.wait_counter(1, addr=_COUNTER_ADDR, timeout=timeout, what='switching the outputs off')

    def wait_counter(self, value, addr=_COUNTER_ADDR, timeout=5.0, what='the program'):
        """ Wait until data memory word ``addr`` of the tProc has reached ``value``. """
        t_end = time.monotonic() + timeout
        while True:
            count = self.soc.get_tproc_counter(addr=addr)
            if count >= value:
                return count
            if time.monotonic() > t_end:
                raise RuntimeError(f'The board did not finish {what} within {timeout} s (counter at {count}, '
                                   f'expected {value}).')
            time.sleep(0.005)

    def _require_connection(self):
        if not self.connected:
            raise BoardConnectionError('Not connected to the QICK board.')

    # ------------------------------------------------------------------ helpers for measurements
    def warn_if_zero(self, counts, what='RFSoC measurement'):
        """ Log a warning when a measurement returned no counts at all. @return bool: True if all zero """
        counts = np.asarray(counts)
        if counts.size and not np.any(counts):
            self.log.warning(f'{what}: zero counts in all {counts.size} readout windows. Is the APD connected to '
                             f'the RFSoC input (Swap C), the laser on, and are the edge counter thresholds right?')
            return True
        return False

    def describe(self):
        """ Text description of the board (same as print(soccfg)). """
        self._require_connection()
        return str(self.soccfg)
