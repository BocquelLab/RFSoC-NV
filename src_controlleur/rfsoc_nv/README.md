# rfsoc_nv

PC-side Python package for the lab's RFSoC4x2 running QICK with the qick-dawg photon counting firmware.
It is used by the qudi hardware modules in `qudi-iqo-modules/src/qudi/hardware/bocquellab/rfsoc_*.py` and
can be used on its own in a script or notebook.

| File | Content |
|---|---|
| `board.py` | `QickBoard`: connection to the board (Pyro4) or to the simulator, version check, hard gain limit, one user at a time, `outputs_off()` |
| `cw.py` | `SteppedCW`: continuous microwave output with a frequency list stepped from the PC, one call per point (CW ODMR with a counter outside the board) |
| `pulses.py` | `PulseSequencer`: endless pattern of PMOD levels and microwave pulses (pulsed measurements with a counter outside the board) |
| `simulator.py` | `SimulatedSoc`: stands in for the board; `NVWorld`: simulated laser, microwave line, NV centres and APD |
| `../../tools/cw_tone.py` | continuous tone at a list of gains, to measure the DAC output power (amplifier disconnected) |
| `../../tests/instruction_timing.py` | measures the clock cycles per tProc instruction and the output queue behaviour on the board |
| `tproc_v1.py` | decoder and emulator of the tProc v1 instruction set (runs the compiled program words) |
| `board_cfg_qick_4x2_photon_counting.json` | `soc.get_cfg()` of the real board (qick 0.2.302, firmware built 2024-10-10), used by the simulator |
| `../../tests/test_simulator.py` | tests without hardware |
| `../../tests/board_check.py` | check against the real board with nothing connected (or `--simulate`) |

## Installation

In the Python environment that runs the measurements (qudi: `qudi-env`), with the three repositories cloned
next to each other and `qick` checked out at the version of the board (0.2.302, commit `1eba156`):

```
pip install --no-deps -e C:\Software\qick
pip install --no-deps -e C:\Software\qick-dawg
pip install --no-deps -e C:\Software\RFSoC-NV
pip install Pyro4 serpent tqdm itemattribute
```

`--no-deps` matters for qick-dawg: it pins `qick==0.2.302` and pip would replace the editable qick by the PyPI
package. qick-dawg also needs IPython, numpy, scipy and matplotlib.

## Use

```python
import qickdawg as qd
from rfsoc_nv import QickBoard

board = QickBoard(max_gain=2000)                 # real board at 192.168.0.2
# board = QickBoard(max_gain=2000, simulate=True)  # simulated board, no network
board.connect()                                  # raises BoardConnectionError if not reachable
board.outputs_off()                              # generators silent, PMOD outputs low

config = qd.NVConfiguration()                    # qick-dawg finds the board by itself after connect()
...
data = qd.LockinODMR(config).acquire(progress=False)
board.warn_if_zero(data.signal, 'ODMR')          # logs a warning if there were no counts at all

board.outputs_off()
board.close()
```

- `max_gain` is mandatory. A program that sets a generator gain above it, or changes a gain while it runs
  (on-board gain sweep), is refused with `GainLimitError` when it is loaded; nothing is clipped.
- `board.claim('odmr')` / `board.release('odmr')` reserve the board; a second user gets `BoardBusyError`.
- Plain qick programs take `board.soc` and `board.soccfg` (`prog.acquire(board.soc, ...)`).
- Nothing numpy is sent to the board (it runs numpy 1.x under Python 3.8 and cannot read numpy 2 pickles).

## Continuous output stepped from the PC

For CW ODMR where another instrument counts the photons (the NI card) and asks for the next frequency
before each point:

```python
from rfsoc_nv import SteppedCW

cw = SteppedCW(board, gen_ch=0, nqz=2)            # gate_pins=(0,): PMOD0_0 high while the output is on
cw.configure([2.86e9, 2.87e9, 2.88e9], gain=1000) # Hz, DAC units; refused above max_gain
cw.start()                                        # continuous output at the first frequency
cw.step()                                         # plays entry 0; the next step() entry 1, ...
cw.reset()                                        # back to entry 0; raises if the board missed steps
cw.stop()                                         # all outputs off
```

The list (up to 1016 points) is stored in the tProc data memory; `step()` restarts a 20-word program that
plays the next entry as a periodic pulse. Measured on the board (2026-11-13): 0.8 ms per step (network
call), the frequency changes about 1 us after the board has received it; `start()` takes 155 ms.
Nyquist zone 1 covers 0 .. 2457.6 MHz, zone 2 covers 2457.6 .. 4915.2 MHz; the DAC also emits the image
at 4915.2 MHz - f.

## Pulse patterns

For pulsed measurements (Rabi, Ramsey, echo, ...) where another instrument counts the photons, e.g. the NI
card gated by a marker on a PMOD pin:

```python
from rfsoc_nv import PulseSequencer

seq = PulseSequencer(board, gen_ch=0, nqz=2)
info = seq.compile(length=3072,                                # tProc cycles, 3.2552 ns each
                   pins={0: [(600, 1500)], 1: [(600, 1500)]},  # PMOD0_0 (laser), PMOD0_1 (marker) high from .. to ..
                   microwave=[(100, 131)],                     # generator pulse from cycle 100 to 131
                   frequency=2.87e9, gain=1000)                # refused above max_gain
seq.start()      # the pattern repeats without gap until stop()
seq.passes()     # repetitions done so far
seq.stop()       # all outputs off
```

- Every change of the PMOD pins is one instruction, every microwave pulse two or three; the pattern is the
  body of an endless loop (the tProc clock has 48 bits: 10 days). 1024 instructions hold about 200 Rabi
  points (a microwave pulse of its own length plus a laser pulse each).
- Time grid 1 clock cycle (3.2552 ns). Two changes of the PMOD pins at least 3 cycles apart. Microwave
  pulses at least 3 cycles long, constant frequency and gain, phase continuous from pulse to pulse; pulses
  longer than 65535 cycles are played as several pulses without gap.
- `compile()` runs the program in the emulator with pessimistic instruction times and refuses a pattern
  whose outputs would come late (too dense for the processor).
- Measured on the board (2026-11-13): repetition period 242.42 us for a programmed 242.43 us (50 Rabi
  points, 12 000 repetitions); compile 5 ms, start 62 ms, stop 64 ms.

What the firmware source (`qick/firmware/ip/axis_tproc64x32_x8_v1`) and the measurements say about the
tProc v1, and what every long-running program has to respect:
- each output port has a queue of 16 entries (15 usable); the processor stalls while a queue is full
  (measured: 40 outputs 10 ms apart, queued at once, hold it for 248 ms),
- the queues are NOT emptied when a program is stopped or started. Outputs left in a queue are played when
  the clock of the next program reaches their time, and block that port until then. `PulseSequencer` keeps
  the processor at most 1 ms ahead of its outputs (`waiti`), and `QickBoard.outputs_off()` waits 50 ms
  after stopping the processor; `outputs_off()` raises if a port is still blocked after 5 s,
- instruction times in clock cycles: regwi 2.2, memwi 2.4, synci 3.0, set 3.6, seti 3.7, memri 3.6,
  condj 3.6 (5 when taken), mathi 4.9, bitwi 4.8, loopnz 4.4 to 5.8, waiti 7.2.

## Simulator

`simulate=True` builds and compiles the real program and runs the compiled words in a tProc v1 emulator.
Generator pulses, PMOD levels and readout triggers drive a model of the experiment that returns photon counts
with shot noise: CW ODMR dip, Rabi oscillations, Ramsey fringes with T2*, echo. Parameters (see `NVWorld`):

```python
board = QickBoard(max_gain=2000, simulate=True, sim_options=dict(
    realtime=True,                       # programs take as long as on the board
    world=dict(resonances_mhz=[2870.0], count_rate=200e3, contrast=0.3, rabi_mhz_full_scale=10.0,
               apd_connected=True)))     # False: unplugged APD -> zero counts
board.soc.sim_report()                   # program size, late outputs, queue fill, shots, ...
board.soc.sim_outputs()                  # laser gate level, microwave being played
board.soc.sim_apd_counts(0.01)           # APD pulses a counter outside the board sees in 10 ms (between programs)
```

`world=dict(laser_gated=False)` simulates a laser that is always on, whatever its gate input.

A time-resolved gated counter outside the board (the NI fast counter) is simulated as well:
`sim_gated_counter_start(pin, bin_width, record_length, number_of_gates)` before the pulse program, then
`sim_gated_counter_read()` returns the expected counts per gate and bin (without shot noise) and the number of
records; every rising edge of digital output `pin` starts a record.

Not simulated: generator envelopes (pulses are rectangular), the decimated / DDR4 / MR buffers, tProc input
ports, Nyquist images, the delay between the generator output and the PMOD pins, and everything analog
between the board and the sample. Instructions are charged the clock cycles measured on the board, rounded
up (5 for those that were not measured); output queues stall the processor as on the board.
