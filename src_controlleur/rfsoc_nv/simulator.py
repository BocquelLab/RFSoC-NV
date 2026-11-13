# -*- coding: utf-8 -*-
"""
Simulated RFSoC4x2 / QICK board for tests without hardware.

``SimulatedSoc`` stands in for the Pyro proxy of the QickSoc that runs on the board. It offers the
methods the qick / qick-dawg program classes call (``load_bin_program``, ``config_avg``,
``start_readout``, ``poll_data``, ...), so the *real* program is built and compiled on the PC exactly
as for the board. The compiled words are then executed by a tProc v1 emulator (``tproc_v1.py``) and
the outputs it produces (generator pulses, PMOD levels, readout triggers) drive a small model of the
experiment (``NVWorld``): an NV centre ensemble excited by a laser gated by a PMOD pin, driven by
the microwave generator and seen by an APD on a readout with edge counter.

The board description comes from a ``soc.get_cfg()`` dump of the real board
(``board_cfg_qick_4x2_photon_counting.json``), so register maps, clocks, trigger bits and memory
sizes are the real ones.

Limits of the simulation (also listed by ``SimulatedSoc.sim_report()``):
- every tProc instruction is taken to need the same number of clock cycles (``cycles_per_instruction``,
  5 as measured for mathi / loopnz on the lab board);
- generator envelopes are not played back: every pulse is treated as rectangular with its gain;
- the decimated (time trace), DDR4 and MR buffers and the tProc input ports are not simulated;
- the microwave frequency is taken as programmed (no Nyquist images, no amplifier, no cables);
- a change of microwave frequency between two pulses does not rotate the spin frame.
"""

import copy
import json
import logging
import math
import os
import queue
import threading
import time

import numpy as np

from .tproc_v1 import TProcV1Emulator, TProcError

__all__ = ['SimulatedSoc', 'NVWorld', 'DEFAULT_BOARD_CFG']

logger = logging.getLogger(__name__)

DEFAULT_BOARD_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'board_cfg_qick_4x2_photon_counting.json')

_TWO_PI = 2 * math.pi
_INF = float('inf')


def _s16(val):
    val &= 0xFFFF
    return val - 0x10000 if val & 0x8000 else val


class NVWorld:
    """ What is (virtually) connected to the board: laser, microwave line, NV centres, APD.

    All parameters can be given to ``SimulatedSoc`` (``world={...}``) or changed later with
    ``SimulatedSoc.sim_set_world``:

    mw_gen_ch (0)               generator channel that drives the NV (0 = DAC_B)
    laser_pin (0)               index in the board's output pin list that gates the laser (0 = PMOD0_0)
    apd_ro_ch (0)               readout channel the APD is connected to (0 = ADC_D)
    mw_connected, laser_connected, apd_connected (True)   unplug a cable in the simulation
    laser_gated (True)          False: the laser ignores its gate and is always on (constant power mode)
    resonances_mhz ((2870.0,))  spin resonance frequencies
    count_rate (200e3)          APD counts per second with the laser on, spin in ms = 0
    dark_rate (300.0)           APD counts per second without laser
    contrast (0.3)              relative fluorescence drop of the ms = +-1 state
    rabi_mhz_full_scale (10.0)  Rabi frequency at full generator gain (linear in gain)
    t2_star_us (1.0), t2_us (200.0), t1_us (5000.0)       coherence and relaxation times
    t_polarize_ns (250.0)       optical spin polarisation time with the laser on
    t_optical_dephase_ns (100.0)  coherence lifetime with the laser on (sets the CW ODMR linewidth)
    apd_pulse_amplitude (3000)  height of an APD pulse in ADC units, compared with the edge counter
                                thresholds (counts only if low_threshold < high_threshold <= amplitude)
    n_detunings (31)            size of the inhomogeneous ensemble per resonance
    """

    DEFAULTS = dict(mw_gen_ch=0, laser_pin=0, apd_ro_ch=0,
                    mw_connected=True, laser_connected=True, apd_connected=True, laser_gated=True,
                    resonances_mhz=(2870.0,), count_rate=200e3, dark_rate=300.0, contrast=0.3,
                    rabi_mhz_full_scale=10.0, t2_star_us=1.0, t2_us=200.0, t1_us=5000.0,
                    t_polarize_ns=250.0, t_optical_dephase_ns=100.0, apd_pulse_amplitude=3000,
                    n_detunings=31)
    _CACHE_MAX = 50000

    def __init__(self, cfg, **params):
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f'Unknown simulation parameter(s): {sorted(unknown)}. '
                             f'Valid: {sorted(self.DEFAULTS)}')
        self.params = dict(self.DEFAULTS)
        self.params.update(params)
        tproc = cfg['tprocs'][0]
        self.f_time = tproc['f_time']  # MHz
        self.cyc_us = 1.0 / self.f_time
        self.gens = cfg['gens']
        self.readouts = cfg['readouts']
        self.pins = [tuple(p) for p in tproc['output_pins']]  # (type, port, bit, name)
        self.gen_ports = {g['tproc_ch']: i for i, g in enumerate(self.gens)}
        self.digital_ports = {p[1] for p in self.pins} | {r['trigger_port'] for r in self.readouts}
        self.ro_len = {}  # readout channel -> window length in tProc cycles (set by config_avg)
        self.gate = None  # time-resolved counter outside the board: (port, bit, bin width in cycles, bins)
        self.n_retrigger = 0
        self._cache = {}
        self._setup()
        self.reset()

    # ------------------------------------------------------------------ setup
    def set_params(self, **params):
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f'Unknown simulation parameter(s): {sorted(unknown)}. '
                             f'Valid: {sorted(self.DEFAULTS)}')
        self.params.update(params)
        self._setup()
        self._s = self._polarized()

    def _setup(self):
        p = self.params
        if not 0 <= p['laser_pin'] < len(self.pins):
            raise ValueError(f'laser_pin {p["laser_pin"]} is not one of the {len(self.pins)} output pins.')
        self._laser_port, self._laser_bit = self.pins[p['laser_pin']][1], self.pins[p['laser_pin']][2]
        res = np.atleast_1d(np.asarray(p['resonances_mhz'], dtype=float))
        k = max(1, int(p['n_detunings']))
        # quasi-static detunings: FID envelope exp(-(t/T2*)^2)  <->  sigma_f = 1 / (sqrt(2) pi T2*)
        sigma = 1.0 / (math.sqrt(2) * math.pi * p['t2_star_us'])
        if k > 1:
            grid = np.linspace(-3 * sigma, 3 * sigma, k)
            weights = np.exp(-grid ** 2 / (2 * sigma ** 2))
        else:
            grid, weights = np.zeros(1), np.ones(1)
        self._f_res = (res[:, None] + grid[None, :]).ravel()
        self._w = np.tile(weights / weights.sum(), len(res)) / len(res)
        self._gamma_p = 1e3 / p['t_polarize_ns']  # 1/us
        self._gamma_c = 1e3 / p['t_optical_dephase_ns']
        self._cache.clear()

    def set_gate(self, pin=None, bin_width_s=1e-8, record_length_s=1e-6):
        """ Time-resolved gated counter outside the board (as the NI fast counter): every rising edge of
        digital output ``pin`` opens a record of ``record_length_s`` in bins of ``bin_width_s``.
        ``pin=None`` removes it. """
        self._cache.clear()
        self._windows = [w for w in self._windows if w[0] != 'gate']
        if pin is None:
            self.gate = None
            return
        if not 0 <= pin < len(self.pins):
            raise ValueError(f'gate pin {pin} is not one of the {len(self.pins)} output pins.')
        bin_cycles = bin_width_s * self.f_time * 1e6
        bins = max(1, int(round(record_length_s / bin_width_s)))
        self.gate = (self.pins[pin][1], self.pins[pin][2], bin_cycles, bins)

    def _polarized(self):
        s = np.zeros((len(self._f_res), 3))
        s[:, 2] = 1.0
        return s

    def reset(self):
        """ Power-on state: outputs low, generators silent, spins polarised. """
        self.t = 0
        self._do = {}
        self._mw = None  # (omega, phi, freq, end, period, start), times in cycles relative to self.t
        self._mw_queue = []
        self._windows = []  # [readout, end, expected counts so far]
        self._frame = float(self._f_res[0])
        self._s = self._polarized()

    def new_run(self, idle_us=1000.0):
        """ The tProc was (re)started: its master clock restarts at 0. One-shot pulses and open readout
        windows of the previous program are over; periodic pulses and output levels persist.
        The time between two programs is modelled as ``idle_us`` with the outputs as they were left.
        """
        self.t = 0
        self._windows = []
        self._mw_queue = []
        if self._mw is not None and self._mw[3] != _INF:
            self._mw = None
        if idle_us > 0:
            self._segment(idle_us * self.f_time)
        self._s = np.round(self._s, 5)

    def stop_generators(self):
        self._mw = None
        self._mw_queue = []

    # ------------------------------------------------------------------ state for the outside
    @property
    def laser_on(self):
        return bool((self._do.get(self._laser_port, 0) >> self._laser_bit) & 1)

    @property
    def mw_state(self):
        """ (frequency in MHz, gain fraction of full scale, periodic) of the pulse being played, or None. """
        if self._mw is None:
            return None
        omega, _, freq, end, _, _ = self._mw
        full = _TWO_PI * self.params['rabi_mhz_full_scale']
        return freq, (omega / full if full else 0.0), end == _INF

    def pending_end(self):
        """ Time (cycles, absolute) at which the last open readout window / one-shot pulse ends. """
        t_rel = 0.0
        for win in self._windows:
            t_rel = max(t_rel, win[1])
        if self._mw is not None and self._mw[3] != _INF:
            t_rel = max(t_rel, self._mw[3])
        return self.t + int(math.ceil(t_rel))

    def count(self, duration_us):
        """ Expected number of APD pulses during the next ``duration_us`` with the outputs as they are now:
        what a counter outside the board (the NI card) sees between two programs. Advances the time.
        """
        span = max(1, int(round(duration_us * self.f_time)))
        self._windows.append(['external', span, 0.0])
        return sum(counts for ro, counts in self.process((), self.t + span) if ro == 'external')

    # ------------------------------------------------------------------ time evolution
    def process(self, events, t_end):
        """ Advance to ``t_end`` (absolute tProc cycles) applying the emulator outputs ``events``
        (sorted, all earlier than t_end).

        @return list of (readout channel, expected number of APD counts) for the windows that closed
        """
        t0 = self.t
        span = t_end - t0
        if span <= 0:
            if events:
                raise RuntimeError('simulator: outputs scheduled in the past of the simulated experiment')
            return []
        rel = tuple((max(ev[0] - t0, 0),) + tuple(ev[2:]) for ev in events)
        key = (self._carry_key(), rel, span)
        hit = self._cache.get(key)
        if hit is None:
            results = tuple(self._evolve(rel, span))
            self._shift(span)
            if len(self._cache) >= self._CACHE_MAX:
                self._cache.clear()
            self._cache[key] = (results, self._snapshot())
        else:
            results, snap = hit
            self._restore(snap)
        self.t = t_end
        return results

    def _carry_key(self):
        self._s = np.round(self._s, 5)
        return (tuple(sorted(self._do.items())), self._mw, tuple(self._mw_queue),
                tuple((w[0], w[1], round(float(np.sum(w[2])), 6)) for w in self._windows), self._frame,
                self._s.tobytes())

    def _snapshot(self):
        return (dict(self._do), self._mw, list(self._mw_queue), [self._copy_window(w) for w in self._windows],
                self._frame, self._s.copy())

    @staticmethod
    def _copy_window(win):
        return [win[0], win[1], win[2].copy(), win[3]] if win[0] == 'gate' else list(win)

    def _restore(self, snap):
        do, mw, mw_queue, windows, frame, s = snap
        self._do = dict(do)
        self._mw = mw
        self._mw_queue = list(mw_queue)
        self._windows = [self._copy_window(w) for w in windows]
        self._frame = frame
        self._s = s.copy()

    def _shift(self, span):
        """ Make all carried times relative to the new interval start. """
        for win in self._windows:
            win[1] -= span
            if win[0] == 'gate':
                win[3] -= span
        if self._mw is not None:
            omega, phi, freq, end, period, start = self._mw
            if end == _INF:
                start = -((span - start) % period)  # keep only the position inside the period
            else:
                end -= span
                start -= span
            self._mw = (omega, phi, freq, end, period, start)

    def _evolve(self, rel, span):
        results = []
        t = 0.0
        i, n = 0, len(rel)
        while True:
            tb = span
            if i < n and rel[i][0] < tb:
                tb = rel[i][0]
            if self._mw is not None and self._mw[3] < tb:
                tb = self._mw[3]
            for win in self._windows:
                if win[1] < tb:
                    tb = win[1]
            if tb > t:
                self._segment(tb - t, t)
                t = tb
            if self._windows:
                still_open = []
                for win in self._windows:
                    if win[1] <= t:
                        results.append((win[0], win[2]))
                    else:
                        still_open.append(win)
                self._windows = still_open
            if self._mw is not None and self._mw[3] <= t:
                self._mw = None
                self._start_next_pulse(t)
            while i < n and rel[i][0] <= t:
                self._apply(rel[i], t)
                i += 1
            if t >= span and i >= n:
                return results

    def _apply(self, ev, t):
        port = ev[1]
        if port in self.digital_ports:
            word = ev[2]
            rising = word & ~self._do.get(port, 0)
            self._do[port] = word
            if rising:
                for ro, rocfg in enumerate(self.readouts):
                    if rocfg['trigger_port'] == port and (rising >> rocfg['trigger_bit']) & 1 and ro in self.ro_len:
                        if any(win[0] == ro for win in self._windows):
                            self.n_retrigger += 1  # trigger during an open window: ignored
                        else:
                            self._windows.append([ro, t + self.ro_len[ro], 0.0])
                gate = self.gate
                if gate is not None and port == gate[0] and (rising >> gate[1]) & 1:
                    self._windows.append(['gate', t + gate[2] * gate[3], np.zeros(gate[3]), t])
        elif port in self.gen_ports:
            gen = self.gen_ports[port]
            if gen != self.params['mw_gen_ch']:
                return
            gencfg = self.gens[gen]
            freq_reg, phase_reg, _, gain_reg, mode = ev[2:7]
            length = mode & 0xFFFF
            outsel = (mode >> 16) & 0b11
            periodic = bool((mode >> 18) & 1)
            gain = _s16(gain_reg)
            if length == 0:
                return
            # outsel 2 (envelope without carrier) and 3 (zero) give no microwave
            amp = abs(gain) / gencfg['maxv'] if outsel in (0, 1) else 0.0
            omega = _TWO_PI * self.params['rabi_mhz_full_scale'] * amp  # rad/us
            phi = _TWO_PI * (phase_reg & 0xFFFFFFFF) / 2 ** gencfg['b_phase'] + (math.pi if gain < 0 else 0.0)
            freq = (freq_reg & 0xFFFFFFFF) * gencfg['f_dds'] / 2 ** gencfg['b_dds']
            dur = length * self.f_time / gencfg['f_fabric']
            self._mw_queue.append((omega, phi, freq, dur, periodic))
            if self._mw is None:
                self._start_next_pulse(t)
            elif self._mw[3] == _INF:
                # a periodic pulse stops at the end of its current period, then the queue is played
                omega0, phi0, freq0, _, period, start = self._mw
                end = start + math.ceil((t - start) / period) * period
                self._mw = (omega0, phi0, freq0, max(end, t), period, start)

    def _start_next_pulse(self, t):
        if not self._mw_queue:
            return
        omega, phi, freq, dur, periodic = self._mw_queue.pop(0)
        end = _INF if (periodic and not self._mw_queue) else t + dur
        self._mw = (omega, phi, freq, end, dur, t)
        if omega > 0:
            self._frame = freq

    def _segment(self, dt_cycles, t0=0.0):
        """ Evolve the spins for dt with constant laser / microwave state; add counts to open windows.
        ``t0``: start of the segment in the time base of the open windows (cycles). """
        p = self.params
        dt = dt_cycles * self.cyc_us  # us
        s = self._s
        binned = None  # function(a, b): expected fluorescence counts from a to b (us after the segment start)
        laser = (self.laser_on or not p['laser_gated']) and p['laser_connected']
        mw = self._mw
        mw_on = mw is not None and mw[0] > 0 and p['mw_connected']
        fluorescence = 0.0
        if laser:
            if mw_on:
                delta = _TWO_PI * (self._f_res - mw[2])
                pump = mw[0] ** 2 * self._gamma_c / (self._gamma_c ** 2 + delta ** 2)
            else:
                pump = 0.0
            rate = self._gamma_p + pump
            z_ss = self._gamma_p / rate
            decay = np.exp(-rate * dt)
            z0 = s[:, 2]
            dark_fraction = (1 - z_ss) * dt - (z0 - z_ss) * (1 - decay) / rate  # integral of (1 - z) dt
            fluorescence = p['count_rate'] * 1e-6 * (dt - 0.5 * p['contrast'] * float(np.dot(self._w, dark_fraction)))
            if self.gate is not None and any(win[0] == 'gate' for win in self._windows):
                weights, scale = self._w, p['count_rate'] * 1e-6
                half_contrast, z_start = 0.5 * p['contrast'], z0.copy()
                z_ss_arr = np.broadcast_to(z_ss, z_start.shape)
                rate_arr = np.broadcast_to(rate, z_start.shape)

                def binned(a, b):
                    width = (b - a)[:, None]
                    dark = (1 - z_ss_arr) * width - (z_start - z_ss_arr) * (
                        np.exp(-rate_arr * a[:, None]) - np.exp(-rate_arr * b[:, None])) / rate_arr
                    return scale * ((b - a) - half_contrast * (dark @ weights))
            s[:, 2] = z_ss + (z0 - z_ss) * decay
            s[:, :2] *= math.exp(-self._gamma_c * dt)
        else:
            if mw_on:
                omega, phi = mw[0], mw[1]
                delta = _TWO_PI * (self._f_res - mw[2])
                w_eff = np.sqrt(omega ** 2 + delta ** 2)
                nx, ny, nz = omega * math.cos(phi) / w_eff, omega * math.sin(phi) / w_eff, delta / w_eff
                theta = w_eff * dt
                c, sn = np.cos(theta), np.sin(theta)
                x, y, z = s[:, 0], s[:, 1], s[:, 2]
                ndotv = nx * x + ny * y + nz * z
                s[:, 0], s[:, 1], s[:, 2] = (x * c + (ny * z - nz * y) * sn + nx * ndotv * (1 - c),
                                             y * c + (nz * x - nx * z) * sn + ny * ndotv * (1 - c),
                                             z * c + (nx * y - ny * x) * sn + nz * ndotv * (1 - c))
            else:
                theta = _TWO_PI * (self._f_res - self._frame) * dt
                c, sn = np.cos(theta), np.sin(theta)
                x, y = s[:, 0].copy(), s[:, 1].copy()
                s[:, 0] = x * c - y * sn
                s[:, 1] = x * sn + y * c
            s[:, :2] *= math.exp(-dt / p['t2_us'])
            # spin-lattice relaxation towards the equal mixture of the three ms levels
            s[:, 2] = -1 / 3 + (s[:, 2] + 1 / 3) * math.exp(-dt / p['t1_us'])
        if self._windows and p['apd_connected']:
            dark_rate = p['dark_rate'] * 1e-6
            counts = fluorescence + dark_rate * dt
            for win in self._windows:
                if win[0] != 'gate':
                    win[2] += counts
                    continue
                # time-resolved record: distribute the counts of this segment over its bins
                start, width, trace = win[3], self.gate[2], win[2]
                first = max(0, int((t0 - start) // width))
                last = min(len(trace), int(math.ceil((t0 + dt_cycles - start) / width)))
                if last <= first:
                    continue
                edges = np.clip(start + width * np.arange(first, last + 1), t0, t0 + dt_cycles)
                a, b = (edges[:-1] - t0) * self.cyc_us, (edges[1:] - t0) * self.cyc_us
                trace[first:last] += dark_rate * (b - a)
                if binned is not None:
                    trace[first:last] += binned(a, b)


class _ReadoutJob:
    def __init__(self, total_shots, counter_addr, ch_list, reads_per_shot, stride):
        self.total = total_shots
        self.counter_addr = counter_addr
        self.ch_list = ch_list
        self.reads = reads_per_shot
        self.stride = stride
        self.last = 0


class SimulatedSoc:
    """ Stand-in for the QickSoc Pyro proxy. See the module docstring.

    @param str cfg_file: JSON dump of ``soc.get_cfg()`` (default: the lab's RFSoC4x2 photon counting firmware)
    @param dict world: parameters of the simulated experiment, see ``NVWorld``
    @param bool realtime: let simulated programs take (at least) as long as on the board
    @param int cycles_per_instruction: clock cycles the tProc needs per instruction. Measured on the lab
        board with tests/board_check.py (2026-11-12): 5.0 per mathi, 5.5 per loopnz; other instructions
        are assumed to take the same.
    @param int fifo_depth: assumed depth of the tProc output queues (for the report only)
    @param seed: seed of the random generator for the photon shot noise
    """

    is_simulation = True

    def __init__(self, cfg_file=None, world=None, realtime=True, cycles_per_instruction=5, fifo_depth=16,
                 seed=None):
        with open(cfg_file or DEFAULT_BOARD_CFG, 'r') as file:
            self._cfg = json.load(file)
        tproc = self._cfg['tprocs'][0]
        if tproc['type'] != 'axis_tproc64x32_x8':
            raise RuntimeError(f'The simulator only knows the tProc v1, the board description has {tproc["type"]}.')
        self._f_time = tproc['f_time']
        self._realtime = bool(realtime)
        self._world = NVWorld(self._cfg, **(world or {}))
        self._emu = TProcV1Emulator(pmem_size=tproc['pmem_size'], dmem_size=tproc['dmem_size'],
                                    cycles_per_instruction=cycles_per_instruction, fifo_depth=fifo_depth)
        self._rng = np.random.default_rng(seed)
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._start_src = 'internal'
        self._binprog = None
        self._nqz = {}
        self._avg_cfg = {}
        self._hist = {ch: [] for ch in range(len(self._cfg['readouts']))}
        self._envelopes = {}
        self._data_queue = queue.Queue()
        self._error_queue = queue.Queue()
        self._total_count = 0
        self._count = 0
        self._stats = dict(runs=0, short_reads=0, zero_threshold=0)
        self._gate_lock = threading.Lock()
        self._gate_enabled = False
        self._gate_acc = None  # expected counts of the time-resolved counter outside the board, (gates, bins)
        self._gate_count = 0
        self._last_run = {}

    # ================================================================== simulation-only interface
    def sim_set_world(self, **params):
        """ Change parameters of the simulated experiment (see NVWorld). Not possible while a program runs. """
        with self._lock:
            if self._running():
                raise RuntimeError('simulator: stop the running program before changing the experiment')
            self._world.set_params(**params)

    def sim_world(self):
        return dict(self._world.params)

    def sim_outputs(self):
        """ Present state of the simulated outputs: laser gate level and microwave pulse being played. """
        return dict(laser_on=self._world.laser_on, mw=self._world.mw_state)

    def sim_apd_counts(self, duration):
        """ Number of APD pulses a counter outside the board (the NI card) gets during ``duration`` seconds
        with the outputs as they are now, e.g. during a continuous microwave pulse. With shot noise.
        Only between two programs; does not wait ``duration``.
        """
        with self._lock:
            thread = self._thread
            if thread is not None:
                thread.join(0.2)  # a program that just issued its last outputs
            if self._running():
                raise RuntimeError('simulator: counting outside the board is only simulated between two programs')
            expected = self._world.count(float(duration) * 1e6)
            return int(self._rng.poisson(expected)) if expected > 0 else 0

    def sim_gated_counter_start(self, pin, bin_width, record_length, number_of_gates):
        """ Simulate a time-resolved gated photon counter outside the board, as the NI fast counter: every
        rising edge of digital output ``pin`` (index in the output pin list, 1 = PMOD0_1) starts a record of
        ``record_length`` seconds in bins of ``bin_width`` seconds; record number i goes to row
        i % number_of_gates. Only while no program runs (the counter is started before the pulses).
        """
        with self._lock:
            thread = self._thread
            if thread is not None:
                thread.join(0.2)
            if self._running():
                raise RuntimeError('simulator: start the gated counter before the pulse program')
            self._world.set_gate(int(pin), float(bin_width), float(record_length))
            with self._gate_lock:
                self._gate_acc = np.zeros((max(1, int(number_of_gates)), self._world.gate[3]))
                self._gate_count = 0
                self._gate_enabled = True

    def sim_gated_counter_read(self):
        """ @return (expected counts so far as array (gates, bins) without shot noise, number of records) """
        with self._gate_lock:
            if self._gate_acc is None:
                raise RuntimeError('simulator: the gated counter was not started')
            return self._gate_acc.copy(), self._gate_count

    def sim_gated_counter_stop(self):
        """ Stop recording: later records are ignored. What was recorded can still be read. """
        with self._gate_lock:
            self._gate_enabled = False

    def sim_report(self):
        """ What the emulator saw during the last program: number of words and executed instructions,
        timed outputs issued too late, fill level of the output queues, data that was not ready, ... """
        emu = self._emu
        report = dict(self._last_run)
        report.update(program_words=len(self._binprog) if self._binprog is not None else 0,
                      program_memory=emu.pmem_size,
                      instructions_executed=emu.n_instructions,
                      timed_outputs=emu.n_timed,
                      late_outputs=emu.n_late,
                      worst_late_cycles=emu.worst_late,
                      max_queued_outputs=emu.max_queue,
                      processor_stalls=emu.n_stalls,
                      max_lead_cycles=emu.max_lead,
                      assumed_queue_depth=emu.fifo_depth,
                      cycles_per_instruction=emu.cycles_per_instruction,
                      unsupported_instructions=sorted(emu.unsupported),
                      ignored_retriggers=self._world.n_retrigger,
                      short_reads=self._stats['short_reads'],
                      simulated_time_s=emu.t_cpu / self._f_time * 1e-6,
                      running=self._running())
        return report

    def sim_wait(self, timeout=None):
        """ Wait until the running program has ended. @return bool: True if it ended """
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    # ================================================================== QickSoc interface: description
    def get_cfg(self):
        return copy.deepcopy(self._cfg)

    def get_avg_max_length(self, ch=0):
        return self._cfg['readouts'][ch]['avg_maxlen']

    # ================================================================== generators and readouts
    def set_nyquist(self, ch, nqz, force=False):
        if nqz not in (1, 2):
            raise RuntimeError('Nyquist zone must be 1 or 2')
        self._nqz[ch] = nqz

    def set_mixer_freq(self, ch, f, ro_ch=None, phase_reset=True):
        raise NotImplementedError('simulator: this firmware has no generator with a digital mixer')

    def config_mux_gen(self, ch, tones):
        raise NotImplementedError('simulator: this firmware has no multiplexed generator')

    def config_mux_readout(self, pfbpath, cfgs, sel=None):
        raise NotImplementedError('simulator: this firmware has no multiplexed readout')

    def load_pulse_data(self, ch, data, addr):
        data = np.array(data, dtype=np.int16)
        if addr + len(data) > self._cfg['gens'][ch]['maxlen']:
            raise RuntimeError('simulator: envelope does not fit in the generator memory')
        self._envelopes[(ch, addr)] = data

    def configure_readout(self, ch, ro_regs):
        pass  # down-conversion settings do not matter for photon counting

    def config_avg(self, ch, address=0, length=1, enable=True, edge_counting=False, high_threshold=1000,
                   low_threshold=0):
        rocfg = self._cfg['readouts'][ch]
        if edge_counting and not rocfg['has_edge_counter']:
            raise RuntimeError(f'readout {ch} has no edge counter')
        self._avg_cfg[ch] = dict(length=int(length), edge_counting=bool(edge_counting),
                                 high_threshold=high_threshold, low_threshold=low_threshold)
        if enable:
            self._world.ro_len[ch] = int(length) * self._f_time / rocfg['f_output']
        else:
            self._world.ro_len.pop(ch, None)

    def config_buf(self, ch, address=0, length=1, enable=True):
        if enable:
            raise NotImplementedError('simulator: the decimated (time trace) buffer is not simulated')

    def get_accumulated(self, ch, address=0, length=None):
        if length is None:
            length = self.get_avg_max_length(ch)
        rows = self._hist[ch][address:address + length]
        out = np.zeros((length, 2), dtype=np.int64)
        if rows:
            out[:len(rows), 0] = rows
        return out

    def get_decimated(self, ch, address=0, length=None):
        raise NotImplementedError('simulator: the decimated (time trace) buffer is not simulated')

    def arm_ddr4(self, ch, nt, force_overwrite=False):
        raise NotImplementedError('simulator: the DDR4 buffer is not simulated')

    def arm_mr(self, ch):
        raise NotImplementedError('simulator: the MR buffer is not simulated')

    def reset_gens(self):
        """ Stop the tProc and silence all generators (digital outputs keep their level). """
        self._stop_run()
        self._world.stop_generators()

    # ================================================================== tProc
    def load_bin_program(self, binprog, load_mem=True):
        # on the board, loading over a running program corrupts it: the simulator stops it first
        self._stop_run()
        self._emu.load([int(w) for w in binprog])
        self._binprog = [int(w) for w in binprog]

    def reload_mem(self):
        pass

    def load_mem(self, buff, mem_sel='dmem', addr=0):
        if mem_sel != 'dmem':
            raise RuntimeError(f'invalid mem_sel: {mem_sel}')
        for i, val in enumerate(buff):
            self._emu.dmem[addr + i] = int(val)

    def read_mem(self, length, mem_sel='dmem', addr=0):
        if mem_sel != 'dmem':
            raise RuntimeError(f'invalid mem_sel: {mem_sel}')
        return np.array(self._emu.dmem[addr:addr + length], dtype=np.int32)

    def start_src(self, src):
        if src not in ('internal', 'external'):
            raise KeyError(src)
        self._start_src = src

    def set_tproc_counter(self, addr, val):
        self._emu.dmem[addr] = int(val)

    def get_tproc_counter(self, addr):
        return self._emu.dmem[addr]

    def start_tproc(self):
        """ Start the loaded program (no effect while a program is running, as on the board). """
        with self._lock:
            if self._start_src != 'internal':
                return
            thread = self._thread
            if thread is not None and thread.is_alive():
                # a short program that has ended on the board long ago may still be emulated here
                thread.join(0.05)
                if thread.is_alive():
                    return
            self._launch(None)

    def stop_tproc(self, lazy=False):
        if not lazy:
            self._stop_run()

    # ================================================================== streaming readout
    def start_readout(self, total_shots, counter_addr=1, ch_list=None, reads_per_shot=1, stride=None):
        if ch_list is None:
            ch_list = [0, 1]
        ch_list = [int(ch) for ch in ch_list]
        if isinstance(reads_per_shot, int):
            reads_per_shot = [reads_per_shot] * len(ch_list)
        reads_per_shot = [int(n) for n in reads_per_shot]
        if self._start_src != 'internal':
            raise NotImplementedError('simulator: external start of the tProc is not simulated')
        with self._lock:
            self._stop_run()
            while not self._data_queue.empty():
                self._data_queue.get()
            while not self._error_queue.empty():
                self._error_queue.get()
            if stride is None:
                stride = int(0.1 * self.get_avg_max_length(0) / max(reads_per_shot))
            self._total_count = total_shots
            self._count = 0
            self._emu.dmem[counter_addr] = 0
            self._launch(_ReadoutJob(int(total_shots), int(counter_addr), ch_list, reads_per_shot, int(stride)))

    def poll_data(self, totaltime=0.1, timeout=None):
        time_end = time.time() + totaltime
        new_data = []
        while (totaltime < 0) or (self._count < self._total_count and time.time() < time_end):
            try:
                raise RuntimeError('exception in readout loop') from self._error_queue.get(block=False)
            except queue.Empty:
                pass
            try:
                length, data = self._data_queue.get(block=True, timeout=timeout)
                if data is None:
                    break
                self._count += length
                new_data.append((length, data))
            except queue.Empty:
                break
        return new_data

    # ================================================================== emulation thread
    def _running(self):
        return self._thread is not None and self._thread.is_alive()

    def _stop_run(self):
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            self._stop.set()
            thread.join()
        self._thread = None

    def _launch(self, job):
        if self._binprog is None:
            raise RuntimeError('simulator: no program loaded')
        self._stop.clear()
        self._emu.reset()
        self._world.new_run()
        self._world.n_retrigger = 0
        for hist in self._hist.values():
            hist.clear()
        self._stats['runs'] += 1
        self._stats['short_reads'] = 0
        self._last_run = {}
        self._thread = threading.Thread(target=self._run, args=(job,), daemon=True, name='qick-simulator')
        self._thread.start()

    def _run(self, job):
        emu, world = self._emu, self._world
        wall_start = time.perf_counter()
        # without a readout job, hand over at every write of the conventional counter (data memory word 1)
        counter_addr = job.counter_addr if job is not None else 1
        ended = False
        try:
            while not self._stop.is_set():
                reason = emu.run(counter_addr)
                self._advance_world(emu.t_cpu)
                self._pace(wall_start, emu.t_cpu)
                if reason == 'end':
                    # outputs already dispatched still happen after the processor has stopped
                    t_final = max([emu.t_cpu] + [ev[0] + 1 for ev in emu.events])
                    self._advance_world(t_final)
                    self._advance_world(max(t_final, world.pending_end()))
                    self._pace(wall_start, world.t)
                    ended = True
                    if job is not None:
                        self._send(job, final=True)
                    break
                if reason == 'count' and job is not None:
                    self._send(job, final=False)
        except Exception as err:  # as on the board: hand the error to poll_data
            logger.exception('simulator: the emulated program failed')
            self._error_queue.put(err)
            self._data_queue.put((0, (None, None)))
        finally:
            self._last_run = dict(ended=ended, stopped=self._stop.is_set(),
                                  wall_time_s=time.perf_counter() - wall_start,
                                  shots=emu.dmem[counter_addr] if counter_addr is not None else None)
            if emu.unsupported:
                logger.warning(f'simulator: instructions not simulated: {sorted(emu.unsupported)}')

    def _advance_world(self, t_end):
        emu, world = self._emu, self._world
        if t_end <= world.t:
            return
        if emu.events:
            events = sorted(ev for ev in emu.events if ev[0] < t_end)
            if events:
                emu.events = [ev for ev in emu.events if ev[0] >= t_end]
        else:
            events = ()
        for ro, expected in world.process(events, t_end):
            if ro == 'gate':
                with self._gate_lock:
                    if self._gate_enabled and self._gate_acc.shape[1] == len(expected):
                        self._gate_acc[self._gate_count % len(self._gate_acc)] += expected
                        self._gate_count += 1
            elif ro in self._hist:
                self._hist[ro].append(self._to_adc(ro, expected))

    def _to_adc(self, ro, expected):
        """ Value the accumulation buffer returns for a window with ``expected`` APD pulses. """
        cfg = self._avg_cfg.get(ro, {})
        counts = int(self._rng.poisson(expected)) if expected > 0 else 0
        amplitude = self._world.params['apd_pulse_amplitude']
        if cfg.get('edge_counting'):
            high, low = cfg['high_threshold'], cfg['low_threshold']
            if not (low < high <= amplitude):
                self._stats['zero_threshold'] += 1
                return 0
            return counts
        # without edge counting the buffer sums the ADC samples: about 5 samples per APD pulse
        return counts * amplitude * 5

    def _pace(self, wall_start, t_cycles):
        if not self._realtime:
            return
        ahead = t_cycles / self._f_time * 1e-6 - (time.perf_counter() - wall_start)
        if ahead > 0.002:
            self._stop.wait(ahead)

    def _send(self, job, final):
        shots = self._emu.dmem[job.counter_addr]
        if shots > job.total:
            shots = job.total
        if shots <= job.last or (shots < min(job.last + job.stride, job.total) and not final):
            if final and job.last < job.total:
                raise RuntimeError(f'simulator: the program ended after {shots} shots, {job.total} were expected')
            return
        newshots = shots - job.last
        acc_buf = []
        for ich, ch in enumerate(job.ch_list):
            need = newshots * job.reads[ich]
            if need >= self.get_avg_max_length(ch):
                raise RuntimeError(f'Overflowed the averages buffer ({need} unread samples >= buffer size '
                                   f'{self.get_avg_max_length(ch)}).')
            first = job.last * job.reads[ich]
            rows = self._hist[ch][first:first + need]
            data = np.zeros((need, 2), dtype=np.int64)
            if len(rows) < need:
                # the shot counter is ahead of the readout data: the program did not wait for the end of
                # its readout windows, or triggered the readout fewer times than reads_per_shot
                self._stats['short_reads'] += need - len(rows)
            if rows:
                data[:len(rows), 0] = rows
            acc_buf.append(data)
        job.last = shots
        self._data_queue.put((newshots, (acc_buf, (0.0, shots, 0, newshots))))
        if final and job.last < job.total:
            raise RuntimeError(f'simulator: the program ended after {shots} shots, {job.total} were expected')


__all__.append('TProcError')
