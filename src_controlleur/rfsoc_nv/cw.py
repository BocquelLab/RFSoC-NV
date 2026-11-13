# -*- coding: utf-8 -*-
"""
Continuous microwave output with a frequency list that is stepped from the PC, one call per point.

Use: CW ODMR where something else counts the photons (the NI card) and asks for the next frequency
before each point, as it does with the SG384 (``*TRG``).

How it works (tProc v1):
- the frequency list is written once into the tProc data memory as generator frequency words,
- a small program (about 20 words) reads the entry at the present index, plays it as a periodic
  (= continuous) pulse on the generator, moves the index to the next entry and ends,
- ``step()`` only restarts that program: one network call per point. The generator keeps playing the
  periodic pulse after the program has ended, until the next step or ``stop()``.
- the program counts its runs in the data memory, so lost steps are detected (``check()``).

The gain is a constant written in the program, so the gain limit of the board client applies when it is
loaded. Nothing here needs a readout.

    cw = SteppedCW(board, gen_ch=0, nqz=2)
    cw.configure([2.86e9, 2.87e9, 2.88e9], gain=1000)
    cw.start()          # output on at the first frequency
    cw.step()           # plays entry 0, the next step() plays entry 1, ...
    cw.reset()          # next step() plays entry 0 again; raises if steps were lost
    cw.stop()           # all outputs off
"""

import time

from qick.asm_v1 import QickProgram

__all__ = ['SteppedCW']

_ADDR_INDEX = 2  # data memory: index of the entry the next program run plays
_ADDR_SIZE = 3  # number of entries
_ADDR_RUNS = 4  # number of program runs since start()
_ADDR_TABLE = 8  # first frequency word
_T_OUTPUT = 300  # tProc cycles after the program start at which the outputs change (about 1 us)


def _signed32(value):
    """ 32-bit word as the signed number the data memory transfer of the board expects. """
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


class SteppedCW:
    """ See the module docstring.

    @param QickBoard board: connected board client
    @param int gen_ch: generator channel (0 = DAC_B)
    @param int nqz: Nyquist zone of the generator, 1 (0 .. fs/2) or 2 (fs/2 .. fs)
    @param gate_pins: indices in the board's digital output list (0 = PMOD0_0) that are set high while the
        output is on, e.g. a laser gate. All other digital outputs are low.
    @param int period: length of the repeated pulse in generator clock cycles; a frequency step takes
        effect at the end of the period being played (16 cycles = 52 ns)
    """

    def __init__(self, board, gen_ch=0, nqz=2, gate_pins=(), period=16):
        self._board = board
        soccfg = board.soccfg
        if soccfg is None:
            raise RuntimeError('The board client is not connected.')
        self._gen_ch = int(gen_ch)
        if not 0 <= self._gen_ch < len(soccfg['gens']):
            raise ValueError(f'Generator channel {gen_ch} does not exist ({len(soccfg["gens"])} generators).')
        self._nqz = int(nqz)
        if self._nqz not in (1, 2):
            raise ValueError(f'Nyquist zone must be 1 or 2, got {nqz}.')
        self._period = int(period)
        if not 3 <= self._period <= 0xFFFF:
            raise ValueError(f'period must be 3 .. 65535 generator clock cycles, got {period}.')
        pins = soccfg['tprocs'][0]['output_pins']
        self._gate_port, self._gate_mask = None, 0
        for pin in gate_pins:
            if not 0 <= int(pin) < len(pins):
                raise ValueError(f'Digital output {pin} does not exist ({len(pins)} outputs).')
            _, port, bit, _ = pins[int(pin)]
            if self._gate_port not in (None, port):
                raise ValueError('All gate pins must be on the same tProc output port.')
            self._gate_port = port
            self._gate_mask |= 1 << bit
        self._gate_names = tuple(pins[int(pin)][3] for pin in gate_pins)
        self._prog = None
        self._words = []
        self._frequencies = []
        self._gain = 0
        self._runs = 0  # program runs expected since start()
        self._running = False

    # ------------------------------------------------------------------ description
    @property
    def frequency_limits(self):
        """ (min, max) output frequency in Hz for the configured Nyquist zone. """
        fs = self._board.soccfg['gens'][self._gen_ch]['fs'] * 1e6
        return (self._nqz - 1) * fs / 2, self._nqz * fs / 2

    @property
    def max_points(self):
        return self._board.soccfg['tprocs'][0]['dmem_size'] - _ADDR_TABLE

    @property
    def frequencies(self):
        """ Frequencies in Hz as the generator plays them (rounded to its resolution of about 1 Hz). """
        return list(self._frequencies)

    @property
    def gain(self):
        return self._gain

    @property
    def gate_names(self):
        return self._gate_names

    @property
    def running(self):
        return self._running

    def round_frequency(self, frequency):
        """ Frequency in Hz the generator really plays for ``frequency``. Raises ValueError outside the zone. """
        return self._word_and_frequency(frequency)[1]

    def _word_and_frequency(self, frequency):
        low, high = self.frequency_limits
        frequency = float(frequency)
        if not low < frequency < high:
            raise ValueError(f'Frequency {frequency / 1e6:.6f} MHz is outside Nyquist zone {self._nqz} of '
                             f'generator {self._gen_ch} (]{low / 1e6:.1f}, {high / 1e6:.1f}[ MHz).')
        soccfg = self._board.soccfg
        word = int(soccfg.freq2reg(frequency / 1e6, gen_ch=self._gen_ch))
        return word, float(soccfg.reg2freq(word, gen_ch=self._gen_ch)) * 1e6

    # ------------------------------------------------------------------ set up
    def configure(self, frequencies, gain):
        """ Prepare the frequency list (Hz) and the gain (DAC units). Nothing is sent to the board.
        Raises GainLimitError / ValueError; the present configuration is kept in that case.

        @return list: the frequencies as the generator will play them
        """
        if self._running:
            raise RuntimeError('The output is on: stop() before changing the frequency list or the gain.')
        gain = self._board.check_gain(int(round(gain)))
        if gain < 0:
            raise ValueError(f'The gain must be positive, got {gain}.')
        pairs = [self._word_and_frequency(f) for f in frequencies]
        if not 1 <= len(pairs) <= self.max_points:
            raise ValueError(f'{len(pairs)} frequencies: the data memory of the tProc holds 1 .. {self.max_points}.')
        self._prog = self._make_program(gain)
        self._words = [pair[0] for pair in pairs]
        self._frequencies = [pair[1] for pair in pairs]
        self._gain = gain
        return self.frequencies

    def _make_program(self, gain):
        prog = QickProgram(self._board.soccfg)
        ch = self._gen_ch
        prog.declare_gen(ch=ch, nqz=self._nqz)
        prog.set_pulse_registers(ch=ch, style='const', freq=0, phase=0, gain=gain, length=self._period,
                                 mode='periodic')
        page = prog.ch_page(ch)  # registers 1 .. 5 of this page are free (the generator uses the top ones)
        prog.memri(page, 1, _ADDR_INDEX, 'index of this point')
        prog.mathi(page, 2, 1, '+', _ADDR_TABLE)
        prog.memr(page, prog.sreg(ch, 'freq'), 2, 'frequency word of this point')
        prog.synci(_T_OUTPUT)
        if self._gate_mask:
            prog.regwi(page, 3, self._gate_mask, 'gate pins high')
            prog.seti(self._gate_port, page, 3, 0)
        prog.pulse(ch=ch, t=0)
        prog.waiti(0, 10)  # until the outputs above have happened
        prog.mathi(page, 1, 1, '+', 1)
        prog.memri(page, 4, _ADDR_SIZE)
        prog.condj(page, 1, '<', 4, 'SAVE')
        prog.regwi(page, 1, 0, 'back to the first point')
        prog.label('SAVE')
        prog.memwi(page, 1, _ADDR_INDEX)
        prog.memri(page, 5, _ADDR_RUNS)
        prog.mathi(page, 5, 5, '+', 1)
        prog.memwi(page, 5, _ADDR_RUNS)
        prog.end()
        prog.compile()
        return prog

    # ------------------------------------------------------------------ run
    def start(self, timeout=5.0):
        """ Switch the output on at the first frequency. The next step() plays the first entry again
        (as a signal generator in list mode does after a list reset).
        Raises GainLimitError if the board client refuses the program, RuntimeError if the board does
        not confirm; the outputs are off in both cases.
        """
        if self._prog is None:
            raise RuntimeError('configure() first.')
        board = self._board
        with board._io_lock:
            board.outputs_off(timeout=timeout)  # known state: tProc at its end, generators silent, pins low
            self._running = False
            soc = board.soc
            self._prog.config_gens(soc)  # Nyquist zone of the DAC
            soc.load_bin_program(self._prog.binprog, load_mem=False)  # the gain limit is checked here
            header = [0] * (_ADDR_TABLE - _ADDR_INDEX)
            header[_ADDR_SIZE - _ADDR_INDEX] = len(self._words)
            soc.load_mem(header + [_signed32(word) for word in self._words], 'dmem', _ADDR_INDEX)
            self._runs = 0
            self._running = True
            try:
                self.step()
                board.wait_counter(1, addr=_ADDR_RUNS, timeout=timeout, what='switching the microwave on')
                expected = 1 % len(self._words)
                index = int(soc.get_tproc_counter(addr=_ADDR_INDEX))
                if index != expected:
                    raise RuntimeError(f'The frequency list on the board is at entry {index} after the first '
                                       f'point, {expected} was expected.')
                soc.set_tproc_counter(addr=_ADDR_INDEX, val=0)
            except Exception:
                self.stop(timeout=timeout)
                raise

    def step(self):
        """ Play the next entry of the list (after the last one: the first again). One call to the board;
        the frequency changes about 1 us after the board has received it.
        """
        if not self._running:
            raise RuntimeError('The output is off: start() first.')
        self._board.soc.start_tproc()
        self._runs += 1

    def check(self):
        """ Raise RuntimeError if the board has not done as many steps as were asked for. """
        if not self._running:
            return
        done = int(self._board.soc.get_tproc_counter(addr=_ADDR_RUNS))
        if done != self._runs:
            expected, self._runs = self._runs, done
            raise RuntimeError(f'The board has done {done} frequency steps, {expected} were asked for: '
                               f'the frequency axis of this sweep is wrong.')

    def reset(self):
        """ The next step() plays the first entry. Checks the number of steps done so far (see check()). """
        if not self._running:
            return
        board = self._board
        with board._io_lock:
            time.sleep(0.001)  # a step sent just before must be finished (it takes about 1 us on the board)
            try:
                self.check()
            finally:
                board.soc.set_tproc_counter(addr=_ADDR_INDEX, val=0)

    def stop(self, timeout=5.0):
        """ All outputs off (generators silent, digital outputs low). """
        self._running = False
        self._board.outputs_off(timeout=timeout)
