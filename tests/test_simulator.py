# -*- coding: utf-8 -*-
"""
Tests of the board client and the simulated board. No hardware needed.

Run with pytest, or directly:  python tests/test_simulator.py
"""

import time
import warnings

import numpy as np

warnings.simplefilter('ignore')

import qickdawg as qd  # noqa: E402
from qick.asm_v1 import QickProgram  # noqa: E402
from rfsoc_nv import QickBoard, GainLimitError, BoardBusyError, decode_program  # noqa: E402

RESONANCE = 2870.0  # MHz
RABI_FULL = 10.0  # MHz at full gain
GAIN = 16383  # half scale -> 5 MHz Rabi frequency


def make_board(**world):
    params = dict(count_rate=5e6, dark_rate=0.0, resonances_mhz=(RESONANCE,), rabi_mhz_full_scale=RABI_FULL)
    params.update(world)
    board = QickBoard(max_gain=20000, simulate=True, sim_options=dict(seed=1, realtime=False, world=params))
    board.connect()
    return board


def base_cfg():
    c = qd.NVConfiguration()
    c.adc_channel = 0
    c.edge_counting = True
    c.high_threshold = 2000
    c.low_threshold = 500
    c.mw_channel = 0
    c.mw_nqz = 2
    c.mw_gain = GAIN
    c.laser_gate_pmod = 0
    c.pre_init = True
    c.relax_delay_tus = 1
    return c


def pulsed_cfg():
    c = base_cfg()
    c.mw_fMHz = RESONANCE
    c.readout_integration_tns = 300
    c.laser_on_tus = 3
    c.laser_readout_offset_tus = 0
    c.readout_reference_start_tus = 2
    c.mw_readout_delay_tns = 50
    return c


def fit_frequency(t_us, y):
    """ Frequency (MHz) of the strongest oscillation in y(t). """
    y = y - y.mean()
    freqs = np.linspace(0.05, 0.5 / (t_us[1] - t_us[0]), 4000)
    power = [abs(np.sum(y * np.exp(-2j * np.pi * f * t_us))) for f in freqs]
    return freqs[int(np.argmax(power))]


def test_decoder_round_trip():
    """ compile -> decode must give back the assembler listing of real qick-dawg programs. """
    board = make_board()
    try:
        c = base_cfg()
        c.readout_integration_tus = 100
        c.add_linear_sweep('mw', 'fMHz', start=2850, stop=2890, delta=2)
        c.reps = 3
        programs = [qd.LockinODMR(c)]
        c = pulsed_cfg()
        c.add_linear_sweep('mw', 'treg', start=3, stop=63, delta=3)
        c.reps = 3
        programs.append(qd.RabiSweep(c))
        for prog in programs:
            prog.compile()
            listing = [inst for inst in prog.prog_list if inst['name'] != 'comment']
            labels = {inst['label']: i for i, inst in enumerate(listing) if 'label' in inst}
            decoded = decode_program(prog.binprog)
            assert len(decoded) == len(listing)
            for (name, args), inst in zip(decoded, listing):
                expected = [labels.get(a, a) if isinstance(a, str) and a in labels else a for a in inst['args']]
                if QickProgram.instructions[name]['type'] == 'I':
                    expected[-1] = int(expected[-1])
                assert name == inst['name'], (name, inst)
                assert list(args) == [int(a) if not isinstance(a, str) else a for a in expected], (name, args, inst)
    finally:
        board.close()


def test_odmr_dip():
    board = make_board()
    try:
        c = base_cfg()
        c.mw_gain = 3000
        c.readout_integration_tus = 100
        c.add_linear_sweep('mw', 'fMHz', start=2850, stop=2890, delta=1)
        c.reps = 10
        prog = qd.LockinODMR(c)
        d = prog.acquire(progress=False)
        dip = d.frequencies[int(np.argmin(d.signal))]
        assert abs(dip - RESONANCE) <= 1.0, dip
        contrast = 1 - d.signal.min() / d.reference.mean()
        assert 0.05 < contrast < 0.35, contrast
        far = d.signal[:5].mean() / d.reference[:5].mean()
        assert abs(far - 1) < 0.03, far
        report = board.soc.sim_report()
        assert report['late_outputs'] == 0 and report['short_reads'] == 0, report
        assert report['shots'] == c.reps * c.nsweep_points
    finally:
        board.close()


def test_rabi_frequency():
    board = make_board()
    try:
        c = pulsed_cfg()
        c.add_linear_sweep('mw', 'treg', start=3, stop=303, delta=4)
        c.reps = 300
        prog = qd.RabiSweep(c)
        d = prog.acquire(progress=False)
        f_rabi = fit_frequency(d.sweep_tus, d.signal1.astype(float))
        expected = RABI_FULL * GAIN / 32766
        assert abs(f_rabi - expected) < 0.15, (f_rabi, expected)
        # without microwave (second half of the sequence) the signal equals its reference
        assert abs(d.signal2.mean() / d.reference2.mean() - 1) < 0.02
        # the first minimum (pi pulse) shows the full contrast set in the simulation (0.3)
        depth = 1 - d.signal1.min() / d.reference1.mean()
        assert 0.1 < depth < 0.35, depth
    finally:
        board.close()


def test_ramsey_and_echo():
    detuning = 2.0  # MHz
    board = make_board(t2_star_us=0.5, t2_us=20.0, count_rate=1e8)  # high rate: little shot noise
    try:
        pi2 = 1 / (4 * RABI_FULL * GAIN / 32766)  # us
        c = pulsed_cfg()
        c.mw_fMHz = RESONANCE + detuning
        c.mw_pi2_tus = pi2
        c.add_linear_sweep('delay', 'treg', start=10, stop=310, delta=6)
        c.reps = 300
        d = qd.Ramsey(c).acquire(progress=False)
        fringe = fit_frequency(d.sweep_tus, d.signal1.astype(float))
        assert abs(fringe - detuning) < 0.3, fringe
        early = np.ptp(d.signal1[:12].astype(float))
        late = np.ptp(d.signal1[-12:].astype(float))
        assert late < 0.5 * early, (early, late)  # fringes decay with T2* = 0.5 us

        c = pulsed_cfg()
        c.mw_pi2_tus = pi2
        c.add_linear_sweep('delay', 'treg', start=30, stop=630, delta=30)
        c.reps = 300
        d = qd.HahnEchoDelaySweep(c).acquire(progress=False)
        # the echo refocuses the inhomogeneous dephasing: contrast survives far beyond T2*
        assert abs(d.contrast[-1]) > 0.5 * abs(d.contrast[0]), d.contrast
    finally:
        board.close()


def test_unplugged_apd_gives_zero_and_warning():
    messages = []

    class Log:
        def info(self, msg):
            pass

        def warning(self, msg):
            messages.append(msg)

    board = QickBoard(max_gain=20000, simulate=True, logger=Log(),
                      sim_options=dict(seed=1, realtime=False, world=dict(apd_connected=False)))
    board.connect()
    try:
        c = base_cfg()
        c.readout_integration_tus = 100
        c.add_linear_sweep('mw', 'fMHz', start=2860, stop=2880, delta=2)
        c.reps = 2
        d = qd.LockinODMR(c).acquire(progress=False)
        assert not d.signal.any() and not d.reference.any()
        assert board.warn_if_zero(d.signal, 'ODMR') is True
        assert messages and 'zero counts' in messages[0]
        assert board.warn_if_zero(np.array([0, 3]), 'ODMR') is False
    finally:
        board.close()


def test_gain_limit_refuses():
    board = make_board()
    try:
        c = pulsed_cfg()
        c.add_linear_sweep('mw', 'treg', start=3, stop=33, delta=3)
        c.reps = 2
        c.mw_gain = 20001
        prog = qd.RabiSweep(c)
        try:
            prog.acquire(progress=False)
        except GainLimitError:
            pass
        else:
            raise AssertionError('program above the gain limit was loaded')
        assert board.soc.sim_outputs()['mw'] is None  # nothing was played
        # a gain that changes while the program runs cannot be verified: refused as well
        plain = QickProgram(board.soccfg)
        plain.declare_gen(ch=0, nqz=2)
        plain.set_pulse_registers(ch=0, style='const', freq=100, phase=0, gain=100, length=10)
        plain.mathi(0, plain.sreg(0, 'gain'), plain.sreg(0, 'gain'), '+', 1)
        plain.pulse(ch=0, t=0)
        plain.end()
        plain.compile()
        try:
            board.soc.load_bin_program(plain.binprog)
        except GainLimitError:
            pass
        else:
            raise AssertionError('program with a run-time gain change was loaded')
        try:
            board.check_gain(-20001)
        except GainLimitError:
            pass
        else:
            raise AssertionError('negative gain above the limit accepted')
        assert board.check_gain(20000) == 20000
    finally:
        board.close()


def test_one_user_at_a_time():
    board = make_board()
    try:
        board.claim('odmr')
        board.claim('odmr')  # the owner may claim again
        try:
            board.claim('pulsed')
        except BoardBusyError:
            pass
        else:
            raise AssertionError('second user got the board')
        board.release('pulsed')  # not the owner: no effect
        assert board.owner == 'odmr'
        board.release('odmr')
        board.claim('pulsed')
        board.release('pulsed')
    finally:
        board.close()


def test_outputs_off_stops_cw_and_laser():
    board = make_board()
    try:
        soccfg = board.soccfg
        prog = QickProgram(soccfg)
        prog.declare_gen(ch=0, nqz=2)
        prog.set_pulse_registers(ch=0, style='const', mode='periodic', freq=soccfg.freq2reg(2870.0, gen_ch=0),
                                 phase=0, gain=1000, length=100)
        prog.pulse(ch=0, t=0)
        prog.regwi(0, 16, 1)
        prog.seti(7, 0, 16, 0)  # PMOD0_0 (laser gate) high
        prog.end()
        prog.config_all(board.soc)
        board.soc.start_tproc()
        assert board.soc.sim_wait(5)
        out = board.soc.sim_outputs()
        assert out['laser_on'] and out['mw'] is not None and out['mw'][2], out
        assert abs(out['mw'][0] - 2870.0) < 1e-3 and abs(out['mw'][1] - 1000 / 32766) < 1e-6, out
        board.outputs_off()
        out = board.soc.sim_outputs()
        assert not out['laser_on'] and out['mw'] is None, out
    finally:
        board.close()


def test_realtime_pacing():
    board = QickBoard(max_gain=20000, simulate=True, sim_options=dict(seed=1, realtime=True))
    board.connect()
    try:
        c = base_cfg()
        c.readout_integration_tus = 200
        c.add_linear_sweep('mw', 'fMHz', start=2860, stop=2880, delta=1)
        c.reps = 20
        prog = qd.LockinODMR(c)
        start = time.perf_counter()
        prog.acquire(progress=False)
        elapsed = time.perf_counter() - start
        assert elapsed >= 0.95 * prog.total_time(), (elapsed, prog.total_time())
    finally:
        board.close()


if __name__ == '__main__':
    import sys
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith('test_') and callable(func):
            start = time.perf_counter()
            try:
                func()
            except Exception:
                import traceback
                failed += 1
                print(f'FAIL  {name}')
                traceback.print_exc()
            else:
                print(f'ok    {name}  ({time.perf_counter() - start:.2f} s)')
    sys.exit(1 if failed else 0)
