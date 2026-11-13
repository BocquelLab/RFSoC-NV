# -*- coding: utf-8 -*-
"""
Check of the QickBoard client against the real board (or the simulator with --simulate).

Meant for a board with NOTHING connected to its outputs and inputs: it plays short, low-gain microwave
pulses on generator 0 and toggles PMOD0_0. Run it from the qudi environment:

    python tests/board_check.py                 # real board at 192.168.0.2
    python tests/board_check.py --simulate

It checks: connection and version, outputs off, a qick-dawg ODMR and Rabi program, the refusal of a
gain above the limit, and it measures how long the tProc takes per instruction.
"""

import argparse
import logging
import time
import warnings

import numpy as np

warnings.simplefilter('ignore')

import qickdawg as qd  # noqa: E402
from qick.asm_v1 import QickProgram  # noqa: E402
from rfsoc_nv import SteppedCW, PulseSequencer, QickBoard, GainLimitError, BoardConnectionError  # noqa: E402

COUNTER = 1


def run_plain(board, prog, timeout=60.0):
    """ Load and start a plain QickProgram that writes 1 to the counter when done; return the run time. """
    soc = board.soc
    prog.compile()
    soc.start_src('internal')
    soc.stop_tproc(lazy=False)
    time.sleep(0.05)
    soc.load_bin_program(prog.binprog, load_mem=False)
    soc.set_tproc_counter(addr=COUNTER, val=0)
    start = time.perf_counter()
    soc.start_tproc()
    board.wait_counter(1, addr=COUNTER, timeout=timeout)
    return time.perf_counter() - start


def loop_program(soccfg, n_loops, n_body):
    """ n_loops x (n_body mathi + 1 loopnz), no timed instruction. """
    prog = QickProgram(soccfg)
    prog.regwi(0, 1, n_loops - 1)
    prog.label('LOOP')
    for _ in range(n_body):
        prog.mathi(0, 2, 2, '+', 1)
    prog.loopnz(0, 1, 'LOOP')
    prog.regwi(0, 15, 1)
    prog.memwi(0, 15, COUNTER)
    prog.end()
    return prog


def wait_program(soccfg, cycles):
    prog = QickProgram(soccfg)
    prog.synci(cycles)
    prog.waiti(0, 0)
    prog.regwi(0, 15, 1)
    prog.memwi(0, 15, COUNTER)
    prog.end()
    return prog


def base_cfg(gain):
    c = qd.NVConfiguration()
    c.adc_channel = 0
    c.edge_counting = True
    c.high_threshold = 2000
    c.low_threshold = 500
    c.mw_channel = 0
    c.mw_nqz = 2
    c.mw_gain = gain
    c.laser_gate_pmod = 0
    c.pre_init = True
    c.relax_delay_tus = 1
    return c


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='192.168.0.2')
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--max-gain', type=float, default=2000)
    parser.add_argument('--gain', type=int, default=1000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    # an address nobody answers on must fail quickly with a clear error
    if not args.simulate:
        start = time.perf_counter()
        try:
            QickBoard(max_gain=args.max_gain, host='192.168.0.251', timeout=3).connect()
        except BoardConnectionError as err:
            print(f'[ok] unreachable address refused after {time.perf_counter() - start:.1f} s: {err}')
        else:
            raise AssertionError('connected to an address without board')

    board = QickBoard(max_gain=args.max_gain, host=args.host, simulate=args.simulate)
    board.connect()
    try:
        soccfg = board.soccfg
        f_time = soccfg['tprocs'][0]['f_time']
        print(f'[ok] connected, tProc clock {f_time} MHz = {1e3 / f_time:.4f} ns per cycle')

        start = time.perf_counter()
        board.outputs_off()
        print(f'[ok] outputs off in {time.perf_counter() - start:.3f} s')

        # ---- tProc speed: two loop lengths, the difference removes the start / polling overhead
        results = []
        for n_body in (1, 9):
            t1 = run_plain(board, loop_program(soccfg, 200_000, n_body))
            t2 = run_plain(board, loop_program(soccfg, 2_000_000, n_body))
            cycles = (t2 - t1) * f_time * 1e6 / 1_800_000
            results.append((n_body, cycles))
            print(f'     loop of {n_body} mathi + loopnz: {cycles:.2f} clock cycles per iteration '
                  f'({t1:.3f} s / {t2:.3f} s)')
        per_mathi = (results[1][1] - results[0][1]) / 8
        per_loopnz = results[0][1] - per_mathi
        print(f'[ok] tProc: {per_mathi:.2f} cycles per mathi, {per_loopnz:.2f} cycles per loopnz')
        for cycles in (30_720_000, 307_200_000):
            t = run_plain(board, wait_program(soccfg, cycles))
            print(f'     synci({cycles}) + waiti: {t:.4f} s (expected {cycles / f_time * 1e-6:.4f} s + overhead)')

        # ---- qick-dawg ODMR
        c = base_cfg(args.gain)
        c.readout_integration_tus = 100
        c.add_linear_sweep('mw', 'fMHz', start=2850, stop=2890, delta=2)
        c.reps = 20
        prog = qd.LockinODMR(c)
        start = time.perf_counter()
        d = prog.acquire(progress=False)
        print(f'[ok] LockinODMR: {len(prog.binprog)} words, {c.reps * c.nsweep_points} shots in '
              f'{time.perf_counter() - start:.3f} s (program time {prog.total_time():.3f} s); '
              f'signal {d.signal.min()}..{d.signal.max()}, reference {d.reference.min()}..{d.reference.max()}')
        board.warn_if_zero(np.concatenate([d.signal, d.reference]), 'LockinODMR')

        # ---- qick-dawg Rabi
        c = base_cfg(args.gain)
        c.mw_fMHz = 2870
        c.readout_integration_tns = 300
        c.laser_on_tus = 3
        c.laser_readout_offset_tus = 0
        c.readout_reference_start_tus = 2
        c.mw_readout_delay_tns = 50
        c.add_linear_sweep('mw', 'treg', start=3, stop=153, delta=3)
        c.reps = 1000
        prog = qd.RabiSweep(c)
        start = time.perf_counter()
        d = prog.acquire(progress=False)
        shots = c.reps * c.nsweep_points
        print(f'[ok] RabiSweep: {len(prog.binprog)} words, {shots} shots in {time.perf_counter() - start:.3f} s; '
              f'signal1 {d.signal1.min()}..{d.signal1.max()}')
        board.warn_if_zero(d.signal1, 'RabiSweep')

        # ---- gain limit
        c.mw_gain = int(args.max_gain) + 1
        try:
            qd.RabiSweep(c).acquire(progress=False)
        except GainLimitError as err:
            print(f'[ok] refused: {err}')
        else:
            raise AssertionError('program above the gain limit was loaded')

        # ---- continuous output with a frequency list stepped from the PC (CW ODMR with the NI card)
        cw = SteppedCW(board, gen_ch=0, nqz=2)
        freqs = np.linspace(2.82e9, 2.92e9, 101)
        cw.configure(freqs, gain=args.gain)
        start = time.perf_counter()
        cw.start()
        t_start = time.perf_counter() - start
        durations = []
        for sweep in range(5):
            for _ in freqs:
                t0 = time.perf_counter()
                cw.step()
                durations.append(time.perf_counter() - t0)
            cw.reset()  # raises if the board did not do every step
        durations = np.array(durations) * 1e3
        index = int(board.soc.get_tproc_counter(addr=2))
        runs = int(board.soc.get_tproc_counter(addr=4))
        assert index == 0 and runs == 5 * len(freqs) + 1, (index, runs)
        start = time.perf_counter()
        cw.stop()
        print(f'[ok] stepped CW: {len(cw._prog.binprog)} words, switch-on {t_start * 1e3:.0f} ms, '
              f'{len(durations)} steps all done by the board, per step {np.median(durations):.2f} ms median, '
              f'{durations.max():.2f} ms max; switch-off {(time.perf_counter() - start) * 1e3:.0f} ms')
        try:
            cw.configure(freqs, gain=int(args.max_gain) + 1)
        except GainLimitError as err:
            print(f'[ok] refused: {err}')
        else:
            raise AssertionError('stepped CW above the gain limit was accepted')

        # ---- endless pulse pattern (pulsed measurements with a counter outside the board)
        seq = PulseSequencer(board, gen_ch=0, nqz=2)
        f_clock = seq.clock_frequency

        def rabi_pattern(n_points):
            t, pins, mw = 0, {0: [], 1: []}, []
            for i in range(n_points):
                t += int(0.5e-6 * f_clock)
                mw.append((t, t + 3 * (i + 1)))
                t += 3 * (i + 1) + 31
                pins[0].append((t, t + 922))
                pins[1].append((t, t + 922))
                t += 922 + 307
            return t, pins, mw

        length, pins, mw = rabi_pattern(50)
        start = time.perf_counter()
        info = seq.compile(length, pins, mw, frequency=2.87e9, gain=args.gain)
        t_compile = time.perf_counter() - start
        start = time.perf_counter()
        seq.start()
        t_start = time.perf_counter() - start
        time.sleep(0.2)
        t1, n1 = time.perf_counter(), seq.passes()
        time.sleep(3.0)
        t2, n2 = time.perf_counter(), seq.passes()
        period = (t2 - t1) / (n2 - n1)
        start = time.perf_counter()
        seq.stop()
        t_stop = time.perf_counter() - start
        assert abs(period / info['pass_duration'] - 1) < 5e-3, (period, info['pass_duration'])
        print(f'[ok] pulse pattern, 50 Rabi points: {info["instructions"]} instructions, repetition period '
              f'{period * 1e6:.2f} us measured, {info["pass_duration"] * 1e6:.2f} us programmed ({n2 - n1} '
              f'repetitions); compile {t_compile * 1e3:.0f} ms, start {t_start * 1e3:.0f} ms, stop {t_stop * 1e3:.0f} ms')
        length, pins, mw = rabi_pattern(200)
        info = seq.compile(length, pins, mw, frequency=2.87e9, gain=args.gain)
        seq.start()
        time.sleep(0.5)
        n = seq.passes()
        seq.stop()
        assert n > 0
        print(f'[ok] pulse pattern, 200 Rabi points: {info["instructions"]} of {info["program_memory"]} instructions, '
              f'runs ({n} repetitions in 0.5 s)')
        try:
            seq.compile(length, pins, mw, frequency=2.87e9, gain=int(args.max_gain) + 1)
        except GainLimitError as err:
            print(f'[ok] refused: {err}')
        else:
            raise AssertionError('pulse pattern above the gain limit was accepted')

        if args.simulate:
            print('     simulator report:', board.soc.sim_report())
    finally:
        try:
            board.outputs_off()
            print('[ok] outputs off')
        finally:
            board.close()


if __name__ == '__main__':
    main()
