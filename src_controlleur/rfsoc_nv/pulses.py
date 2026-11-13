# -*- coding: utf-8 -*-
"""
Pulse sequences on the tProc v1: a pattern of digital output levels (PMOD pins) and rectangular microwave
pulses (one generator), repeated without end until it is stopped.

Use: pulsed measurements (Rabi, Ramsey, echo, ...) where something else counts the photons, e.g. the NI
card gated by a marker pulse on a PMOD pin.

    seq = PulseSequencer(board, gen_ch=0, nqz=2)
    info = seq.compile(length=3072,                               # tProc cycles (3.2552 ns each)
                       pins={0: [(600, 1500)], 1: [(600, 700)]},  # PMOD0_0 and PMOD0_1 high from .. to ..
                       microwave=[(100, 131)],                    # generator pulse from cycle 100 to 131
                       frequency=2.87e9, gain=1000)
    seq.start()        # the pattern repeats until stop()
    seq.passes()       # number of repetitions done so far
    seq.stop()         # all outputs off

How the pattern becomes a program (see also the firmware source, qick/firmware/ip/axis_tproc64x32_x8_v1):
- Every change of the digital port is one ``seti`` instruction, every microwave pulse one ``set`` (plus one
  or two ``regwi`` for its time and length). The instructions are written in the order of their output
  times; the processor runs ahead of the outputs and each output port holds up to 15 waiting outputs (the
  processor stalls while a queue is full, nothing is lost).
- The pattern is the body of an endless loop. The tProc clock has 48 bits (10 days at 307.2 MHz), so there
  are no batches and no gaps between repetitions.
- ``waiti`` instructions keep the processor from running more than ``max_lead`` ahead of the outputs, so that
  the output queues are empty at most that long after the program has been stopped. This matters: the queues
  survive a stop, and outputs left in them would block the next program until their time has come.
- Before a pattern is accepted, the compiled program is run in the tProc v1 emulator with pessimistic
  instruction times: if an output would come later than asked for (too many changes in too short a time),
  the pattern is refused.

Limits (firmware qick_4x2 photon counting, tProc clock 307.2 MHz):
- time grid 1 clock cycle = 3.2552 ns; two changes of the digital port at least 3 cycles apart,
- microwave pulse length 3 cycles (9.8 ns) and up; pulses longer than 65535 cycles (213 us) are played
  as several pulses without gap; constant frequency and gain (the gain limit of the board applies),
- program memory 1024 instructions: about 3 per microwave pulse and 1 per change of the digital port.
"""

import collections

from qick.asm_v1 import QickProgram

from .cw import frequency_word
from .tproc_v1 import TProcV1Emulator

__all__ = ['PulseSequencer']

_ADDR_PASSES = 1  # data memory: number of completed repetitions (the conventional qick counter address)
_ADDR_STARTED = 5  # set to 1 when the program has started
_WORD_PAGE = 7  # register page that holds the digital port words
_T_START = 2000  # tProc cycles between the program start and the first repetition
_GAP_MIN = 200  # cycles: a wait is only placed in a gap between outputs at least this long
_GAP_KEEP = 64  # cycles after the previous output before the processor is released from a wait
_REBASE = 2 ** 28  # cycles: larger times relative to the time origin are avoided (immediate values)
_MAX_PULSE = 65535  # longest generator pulse in clock cycles
_MIN_PULSE = 3
_MODE_CONST_ONESHOT = 0b01001 << 16  # stdysel = zero, mode = oneshot, outsel = dds


class PulseSequencer:
    """ See the module docstring.

    @param QickBoard board: connected board client
    @param int gen_ch: generator that plays the microwave pulses (0 = DAC_B)
    @param int nqz: Nyquist zone of that generator
    @param float max_lead: seconds the processor may run ahead of the outputs (default 1 ms)
    """

    def __init__(self, board, gen_ch=0, nqz=2, max_lead=1e-3):
        self._board = board
        soccfg = board.soccfg
        if soccfg is None:
            raise RuntimeError('The board client is not connected.')
        self._gen_ch = int(gen_ch)
        if not 0 <= self._gen_ch < len(soccfg['gens']):
            raise ValueError(f'Generator channel {gen_ch} does not exist ({len(soccfg["gens"])} generators).')
        gencfg = soccfg['gens'][self._gen_ch]
        tproc = soccfg['tprocs'][0]
        if abs(gencfg['f_fabric'] - tproc['f_time']) > 1e-9:
            raise RuntimeError('The generator clock differs from the tProc clock: pulse lengths would not be in '
                               'tProc cycles. This firmware is not supported.')
        self._nqz = int(nqz)
        if self._nqz not in (1, 2):
            raise ValueError(f'Nyquist zone must be 1 or 2, got {nqz}.')
        self._f_clock = tproc['f_time'] * 1e6
        self._half_lead = max(_GAP_MIN, int(max_lead * self._f_clock / 2))
        if max_lead > 0.02:
            raise ValueError('max_lead must be at most 20 ms (the outputs are switched off 50 ms after a stop).')
        self._pins = [tuple(pin) for pin in tproc['output_pins']]  # (type, port, bit, name)
        self._prog = None
        self._info = {}
        self._running = False

    # ------------------------------------------------------------------ description
    @property
    def clock_frequency(self):
        """ tProc clock in Hz: the pattern times are in cycles of this clock. """
        return self._f_clock

    @property
    def pin_names(self):
        """ Names of the digital outputs; a pin is addressed by its index in this list. """
        return [pin[3] for pin in self._pins]

    @property
    def frequency_limits(self):
        fs = self._board.soccfg['gens'][self._gen_ch]['fs'] * 1e6
        return (self._nqz - 1) * fs / 2, self._nqz * fs / 2

    @property
    def info(self):
        """ Facts about the compiled pattern (see compile()). """
        return dict(self._info)

    @property
    def running(self):
        return self._running

    def round_frequency(self, frequency):
        return frequency_word(self._board.soccfg, self._gen_ch, self._nqz, frequency)[1]

    # ------------------------------------------------------------------ compile
    def compile(self, length, pins=None, microwave=(), frequency=None, gain=None):
        """ Turn a pattern into a program and check it. Nothing is sent to the board.

        @param int length: duration of the pattern in tProc cycles
        @param dict pins: {pin index: [(on, off), ...]} intervals in cycles, 0 <= on < off <= length, during
            which the pin is high. An interval that ends at ``length`` continues into the next repetition
            if one starts at 0.
        @param microwave: [(on, off), ...] intervals in cycles during which the generator plays
        @param float frequency: microwave frequency in Hz (needed if there are microwave pulses)
        @param gain: generator gain in DAC units (needed if there are microwave pulses)

        Raises ValueError (GainLimitError for the gain) if the pattern cannot be played as asked; the
        previously compiled pattern is kept in that case.
        @return dict: instructions, program_memory, outputs_per_pass, microwave_pulses, digital_changes,
            waits, pass_duration (s), max_lead (s), frequency (Hz as played), gain
        """
        if self._running:
            raise RuntimeError('The sequence is running: stop() before changing it.')
        length = int(length)
        if length < 1:
            raise ValueError('The pattern is empty.')
        memory = self._board.soccfg['tprocs'][0]['pmem_size']
        n_outputs = sum(len(intervals) for intervals in (pins or {}).values()) + len(microwave)
        if n_outputs > memory:
            raise ValueError(f'The pattern has {n_outputs} pulses; every pulse needs at least one of the {memory} '
                             f'instructions of the tProc program memory.')
        changes, first_word, last_word = self._digital_changes(length, pins or {})
        pulses = self._microwave_pulses(length, microwave)
        freq_word, played, gain_int = 0, None, 0
        if pulses:
            if frequency is None or gain is None:
                raise ValueError('The pattern has microwave pulses but no microwave frequency / gain was given.')
            gain_int = self._board.check_gain(int(round(gain)))
            if gain_int < 0:
                raise ValueError(f'The gain must be positive, got {gain_int}.')
            freq_word, played = frequency_word(self._board.soccfg, self._gen_ch, self._nqz, frequency)

        prog, n_waits = self._make_program(length, changes, first_word, last_word, pulses, freq_word, gain_int)
        nwords = self._board.check_program(prog.binprog)  # size and gain limit
        late, worst = self._dry_run(prog.binprog, extra_cycles=2)[:2]
        if late:
            raise ValueError(f'The pattern is too dense for the tProc: {late} outputs in the first 3 repetitions '
                             f'would come up to {worst} clock cycles ({worst / self._f_clock * 1e9:.0f} ns) late. '
                             f'Digital changes need 3 cycles between them, and the processor needs about 4 '
                             f'cycles per digital change and 10 per microwave pulse on average.')
        max_lead = self._dry_run(prog.binprog, extra_cycles=-1)[2] / self._f_clock
        if max_lead > 0.03:
            raise ValueError(f'The processor would run {max_lead * 1e3:.1f} ms ahead of the outputs; the limit is '
                             f'30 ms.')
        self._prog = prog
        self._info = dict(instructions=nwords, program_memory=self._board.soccfg['tprocs'][0]['pmem_size'],
                          outputs_per_pass=len(changes) + len(pulses), microwave_pulses=len(pulses),
                          digital_changes=len(changes), waits=n_waits, pass_duration=length / self._f_clock,
                          max_lead=max_lead, frequency=played, gain=gain_int)
        return self.info

    def _digital_changes(self, length, pins):
        """ -> ([(time, port, word), ...] sorted, word at time 0, word at the end of the pattern) """
        port = None
        edges = collections.defaultdict(int)  # time -> bits toggled
        first_word = last_word = 0
        for pin, intervals in pins.items():
            if not 0 <= int(pin) < len(self._pins):
                raise ValueError(f'Digital output {pin} does not exist ({len(self._pins)} outputs).')
            _, pin_port, bit, name = self._pins[int(pin)]
            if port not in (None, pin_port):
                raise ValueError('All digital outputs must be on the same tProc output port.')
            port = pin_port
            previous_off = 0
            for on, off in sorted((int(a), int(b)) for a, b in intervals):
                if not (0 <= on < off <= length) or on < previous_off:
                    raise ValueError(f'{name}: interval ({on}, {off}) is empty, overlaps the previous one or lies '
                                     f'outside the pattern (0 .. {length} cycles).')
                previous_off = off
                if on == 0:
                    first_word |= 1 << bit
                else:
                    edges[on] ^= 1 << bit
                if off == length:
                    last_word |= 1 << bit
                else:
                    edges[off] ^= 1 << bit
        changes = []
        word = last_word
        if first_word != last_word:
            changes.append((0, port, first_word))
            word = first_word
        for time in sorted(edges):
            if edges[time]:
                word ^= edges[time]
                changes.append((time, port, word))
        if word != last_word:
            raise AssertionError('digital pattern is not consistent')
        return changes, first_word, last_word

    @staticmethod
    def _microwave_pulses(length, intervals):
        """ -> [(time, length), ...] generator pulses, long intervals split into pulses without gap """
        pulses = []
        previous_off = 0
        for on, off in sorted((int(a), int(b)) for a, b in intervals):
            if not (0 <= on < off <= length) or on < previous_off:
                raise ValueError(f'Microwave pulse ({on}, {off}) is empty, overlaps the previous one or lies outside '
                                 f'the pattern (0 .. {length} cycles).')
            previous_off = off
            remaining = off - on
            if remaining < _MIN_PULSE:
                raise ValueError(f'Microwave pulse at cycle {on} is {remaining} clock cycles long; the generator '
                                 f'needs at least {_MIN_PULSE} (9.8 ns).')
            time = on
            while remaining > 0:
                part = min(remaining, _MAX_PULSE)
                if 0 < remaining - part < _MIN_PULSE:
                    part -= _MIN_PULSE  # keep the last part long enough
                pulses.append((time, part))
                time += part
                remaining -= part
        return pulses

    def _make_program(self, length, changes, first_word, last_word, pulses, freq_word, gain):
        soccfg = self._board.soccfg
        ch = self._gen_ch
        prog = QickProgram(soccfg)
        prog.declare_gen(ch=ch, nqz=self._nqz)
        page = prog.ch_page(ch)
        gen_port = soccfg['gens'][ch]['tproc_ch']
        r_freq, r_phase, r_addr, r_gain, r_mode, r_t = (prog.sreg(ch, name) for name in
                                                        ('freq', 'phase', 'addr', 'gain', 'mode', 't'))
        used = [reg for (_, _), (pg, reg) in list(prog._gen_regmap.items()) + list(prog._ro_regmap.items())
                if pg == page and reg > 0]
        free = list(range(1, min(used)))  # registers of this page below the generator / readout registers
        r_count, r_one = free.pop(), free.pop()

        # microwave pulse lengths used more than once get a register of their own
        mode_regs = {}
        if pulses:
            prog.set_pulse_registers(ch=ch, style='const', freq=freq_word, phase=0, gain=gain, length=pulses[0][1],
                                     mode='oneshot')
            counts = collections.Counter(part for _, part in pulses)
            for part, n in counts.most_common(len(free)):
                if n < 2:
                    break
                reg = free.pop(0)
                mode_regs[part] = reg
                prog.regwi(page, reg, _MODE_CONST_ONESHOT | part, f'pulse length {part}')
        # digital port words
        words = sorted({word for _, _, word in changes} | {first_word})
        if len(words) > 31:
            raise ValueError(f'The pattern uses {len(words)} different combinations of digital outputs; 31 are '
                             f'possible.')
        word_regs = {word: i + 1 for i, word in enumerate(words)}
        for word, reg in word_regs.items():
            prog.regwi(_WORD_PAGE, reg, word, f'digital outputs {word:#010b}')
        prog.regwi(page, r_count, 0)
        prog.regwi(page, r_one, 1)
        prog.memwi(page, r_one, _ADDR_STARTED)
        prog.synci(_T_START)
        digital_port = changes[0][1] if changes else self._pins[0][1]
        if first_word and first_word == last_word:
            prog.seti(digital_port, _WORD_PAGE, word_regs[first_word], 0)  # constant levels: set once

        events = sorted([(t, 0, port, word) for t, port, word in changes] + [(t, 1, part, None) for t, part in pulses])
        prog.label('LOOP')
        base = 0  # pattern time of the present time origin of the tProc
        last_wait = None
        previous = None
        current_mode = None  # the length register holds the last pulse of the previous repetition here
        n_waits = 0
        for time, kind, a, b in events:
            gap = _GAP_MIN if previous is None else time - previous
            if (last_wait is None or time - last_wait > self._half_lead) and gap >= _GAP_MIN and time > _GAP_KEEP:
                target = time - min(self._half_lead, gap - _GAP_KEEP, time)
                if target - base >= _REBASE:
                    prog.synci(target - base)
                    base = target
                prog.waiti(gen_port, target - base)
                last_wait = target
                n_waits += 1
            if time - base >= _REBASE:
                prog.synci(time - base)
                base = time
            if kind == 0:
                prog.seti(a, _WORD_PAGE, word_regs[b], time - base)
            else:
                reg = mode_regs.get(a)
                if reg is None:
                    reg = r_mode
                    if current_mode != a:
                        prog.regwi(page, r_mode, _MODE_CONST_ONESHOT | a, f'pulse length {a}')
                        current_mode = a
                prog.regwi(page, r_t, time - base)
                prog.set(gen_port, page, r_freq, r_phase, r_addr, r_gain, reg, r_t)
            previous = time
        # one wait at the end of every repetition, where there is room for it
        end_gap = length - (previous if previous is not None else 0)
        if end_gap >= _GAP_MIN or previous is None:
            target = max(length - self._half_lead, (previous + _GAP_KEEP) if previous is not None else 0, base)
            while target - base >= _REBASE:
                prog.synci(_REBASE)
                base += _REBASE
            prog.waiti(gen_port, target - base)
            n_waits += 1
        remaining = length - base
        while remaining >= _REBASE:
            prog.synci(_REBASE)
            remaining -= _REBASE
        prog.synci(remaining)
        prog.mathi(page, r_count, r_count, '+', 1)
        prog.memwi(page, r_count, _ADDR_PASSES)
        prog.condj(page, 0, '==', 0, 'LOOP')  # register 0 is always 0: jump always
        prog.end()
        prog.compile()
        return prog, n_waits

    def _dry_run(self, binprog, extra_cycles):
        """ Run 3 repetitions in the emulator. @return (late outputs, worst lateness, largest lead) in cycles """
        tproc = self._board.soccfg['tprocs'][0]
        emu = TProcV1Emulator(pmem_size=tproc['pmem_size'], dmem_size=tproc['dmem_size'], extra_cycles=extra_cycles)
        emu.load([int(word) for word in binprog])
        emu.dmem[_ADDR_PASSES] = 0
        for _ in range(100000):
            reason = emu.run(counter_addr=_ADDR_PASSES)
            del emu.events[:]
            if reason == 'end':
                raise AssertionError('the pulse program ended')
            if emu.dmem[_ADDR_PASSES] >= 3:
                break
        else:
            raise AssertionError('the pulse program does not repeat')
        return emu.n_late, emu.worst_late, emu.max_lead

    # ------------------------------------------------------------------ run
    def start(self, timeout=5.0):
        """ Start the compiled pattern from its beginning; it repeats until stop().
        Raises GainLimitError if the board client refuses the program, RuntimeError if the board does not
        confirm the start; the outputs are off in both cases.
        """
        if self._prog is None:
            raise RuntimeError('compile() first.')
        board = self._board
        with board._io_lock:
            board.outputs_off(timeout=timeout)  # known state: tProc at its end, queues empty, outputs off
            self._running = False
            soc = board.soc
            self._prog.config_gens(soc)  # Nyquist zone of the DAC
            soc.load_bin_program(self._prog.binprog, load_mem=False)  # size and gain limit are checked here
            soc.set_tproc_counter(addr=_ADDR_STARTED, val=0)
            soc.set_tproc_counter(addr=_ADDR_PASSES, val=0)
            self._running = True
            try:
                soc.start_tproc()
                board.wait_counter(1, addr=_ADDR_STARTED, timeout=timeout, what='starting the pulse sequence')
            except Exception:
                self.stop(timeout=timeout)
                raise

    def passes(self):
        """ Number of repetitions of the pattern completed since start(). """
        return int(self._board.soc.get_tproc_counter(addr=_ADDR_PASSES))

    def stop(self, timeout=5.0):
        """ Stop the pattern and switch all outputs off (generators silent, digital outputs low). The board
        client waits 50 ms between stopping the processor and loading its "outputs off" program: more than
        the time the processor ran ahead (max_lead), so no output of the pattern is left in a queue.
        """
        self._running = False
        self._board.outputs_off(timeout=timeout)
