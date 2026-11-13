# -*- coding: utf-8 -*-
"""
Measures on the board (nothing needs to be connected, no signal is played):
- the clock cycles each tProc v1 instruction takes (loops of 10^5 and 1.1 * 10^6 iterations, timed from the PC),
- that the processor stalls while an output queue is full (40 outputs 10 ms apart, queued at once).

    python tests/instruction_timing.py [--simulate]

Result on the lab board, 2026-11-13 (307.2 MHz, +-0.5 cycle): regwi 2.2, memwi 2.4, synci 3.0, set 3.6,
memri 3.6, condj 3.6 (not taken), seti 3.7, bitwi 4.8, mathi 4.9, waiti 7.2, mathi + condj taken 9.8;
40 queued outputs: counter written after 248 ms (15 outputs wait in the queue).
The emulator (rfsoc_nv/tproc_v1.py, MEASURED_CYCLES) uses these numbers rounded up.
With --simulate the cycle numbers mean nothing (the emulator is slower than the board); it only shows
that the script runs.
"""
import os
import os
import sys
import time
import logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)
from qick.asm_v1 import QickProgram
from rfsoc_nv import QickBoard
from board_check import run_plain, COUNTER

simulate = '--simulate' in sys.argv
board = QickBoard(max_gain=2000, simulate=simulate)
board.connect()
soccfg = board.soccfg
f_time = soccfg['tprocs'][0]['f_time']

def body(prog, name):
    if name == 'mathi':
        prog.mathi(0, 2, 2, '+', 1)
    elif name == 'regwi':
        prog.regwi(0, 3, 12345)
    elif name == 'seti':
        prog.seti(7, 0, 0, 0)           # port 7, register 0 (= 0: all pins low), time 0 (past)
    elif name == 'set':
        prog.set(0, 0, 22, 23, 24, 25, 26, 0)   # generator 0, gain register = 0 -> silent; time register $0
    elif name == 'waiti':
        prog.waiti(0, 0)
    elif name == 'synci':
        prog.synci(0)
    elif name == 'condj':
        prog.condj(0, 0, '!=', 0, 'LOOP')   # never taken
    elif name == 'memwi':
        prog.memwi(0, 2, 5)
    elif name == 'memri':
        prog.memri(0, 4, 5)
    elif name == 'bitwi':
        prog.bitwi(0, 4, 4, '<<', 1)

def loop_program(name, n_loops, n_body):
    prog = QickProgram(soccfg)
    prog.set_pulse_registers(ch=0, style='const', mode='oneshot', freq=0, phase=0, gain=0, length=3)
    prog.regwi(0, 1, n_loops - 1)
    prog.label('LOOP')
    for _ in range(n_body):
        body(prog, name)
    prog.loopnz(0, 1, 'LOOP')
    prog.regwi(0, 15, 1)
    prog.memwi(0, 15, COUNTER)
    prog.end()
    return prog

try:
    board.outputs_off()
    for name in ('mathi', 'regwi', 'seti', 'set', 'waiti', 'synci', 'condj', 'memwi', 'memri', 'bitwi'):
        res = []
        for n_body in (1, 9):
            t1 = run_plain(board, loop_program(name, 100_000, n_body))
            t2 = run_plain(board, loop_program(name, 1_100_000, n_body))
            res.append((t2 - t1) * f_time * 1e6 / 1_000_000)
        per = (res[1] - res[0]) / 8
        print(f'{name:6s}: {per:6.2f} cycles   (loopnz {res[0] - per:5.2f})', flush=True)

    # jump taken: loop made with condj instead of loopnz
    def jump_program(n_loops):
        prog = QickProgram(soccfg)
        prog.regwi(0, 1, n_loops)
        prog.label('LOOP')
        prog.mathi(0, 1, 1, '-', 1)
        prog.condj(0, 1, '!=', 0, 'LOOP')
        prog.regwi(0, 15, 1); prog.memwi(0, 15, COUNTER); prog.end()
        return prog
    t1 = run_plain(board, jump_program(100_000)); t2 = run_plain(board, jump_program(1_100_000))
    print(f'mathi + condj taken: {(t2 - t1) * f_time * 1e6 / 1_000_000:.2f} cycles')

    # output queue: 40 outputs on port 7 (all pins low), 10 ms apart, issued at once; then the counter is written.
    # Stalling processor: counter written when only 15 are left = after about 250 ms. No stall: at once.
    for n_out in (10, 40):
        prog = QickProgram(soccfg)
        step = int(0.010 * f_time * 1e6)
        for i in range(n_out):
            prog.seti(7, 0, 0, (i + 1) * step)
        prog.regwi(0, 15, 1); prog.memwi(0, 15, COUNTER); prog.end()
        t = run_plain(board, prog)
        print(f'{n_out} outputs 10 ms apart queued at once: counter written after {t * 1e3:.0f} ms')
        time.sleep(0.5)
finally:
    board.outputs_off()
    board.close()
