# rfsoc_nv

PC-side Python package for the lab's RFSoC4x2 running QICK with the qick-dawg photon counting firmware.
It is used by the qudi hardware modules in `qudi-iqo-modules/src/qudi/hardware/bocquellab/rfsoc_*.py` and
can be used on its own in a script or notebook.

| File | Content |
|---|---|
| `board.py` | `QickBoard`: connection to the board (Pyro4) or to the simulator, version check, hard gain limit, one user at a time, `outputs_off()` |
| `simulator.py` | `SimulatedSoc`: stands in for the board; `NVWorld`: simulated laser, microwave line, NV centres and APD |
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
```

Not simulated: generator envelopes (pulses are rectangular), the decimated / DDR4 / MR buffers, tProc input
ports, Nyquist images and everything analog between the board and the sample. Every instruction is charged
5 clock cycles (measured on the board for `mathi` and `loopnz`).
