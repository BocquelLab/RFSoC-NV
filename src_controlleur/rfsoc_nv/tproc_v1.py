# -*- coding: utf-8 -*-
"""
Decoder and software emulator for the QICK tProcessor v1 (axis_tproc64x32_x8).

The emulator executes the *compiled* 64-bit program words (what ``soc.load_bin_program`` sends to the
board), following firmware/tProcessor_64_and_Signal_Generator_V4.pdf in the qick repository:

- 8 register pages x 32 registers of 32 bits, register 0 of every page reads 0;
- a master clock counting tProc cycles from program start and a time offset ``t_off``
  (``synci``/``sync`` add to it); timed instructions (``seti``/``set``) are dispatched to their
  output port for the absolute time ``t_off + t``; ``waiti``/``wait`` block the processor until
  the master clock reaches ``t_off + t``;
- ``loopnz`` jumps and decrements while the register is not zero.

The emulator charges the same number of clock cycles for every instruction (``cycles_per_instruction``;
5.0 per mathi and 5.5 per loopnz were measured on the lab board). It uses this to detect timed
instructions that are issued after their time has passed ("late") and to estimate the fill level of
the output queues.
"""

from qick.asm_v1 import QickProgram

__all__ = ['decode_program', 'decode_word', 'TProcV1Emulator', 'TProcError']

_INSTR = QickProgram.instructions
_OPCODES = {idef['bin']: name for name, idef in _INSTR.items() if 'bin' in idef}
_MATH_OPS = {0b1000: '+', 0b1001: '-', 0b1010: '*'}
_BITW_OPS = {0b0000: '&', 0b0001: '|', 0b0010: '^', 0b0011: '~', 0b0100: '<<', 0b0101: '>>'}
_COND_OPS = {0b0000: '>', 0b0001: '>=', 0b0010: '<', 0b0011: '<=', 0b0100: '==', 0b0101: '!='}

# instruction ids used by the emulator loop
(PUSHI, POPI, MATHI, SETI, SYNCI, WAITI, BITWI, MEMRI, MEMWI, REGWI, SETBI, LOOPNZ, END, CONDJ, MATH, SET,
 SYNC, READ, WAIT, BITW, MEMR, MEMW, SETB) = range(23)
_IDS = {'pushi': PUSHI, 'popi': POPI, 'mathi': MATHI, 'seti': SETI, 'synci': SYNCI, 'waiti': WAITI,
        'bitwi': BITWI, 'memri': MEMRI, 'memwi': MEMWI, 'regwi': REGWI, 'setbi': SETBI, 'loopnz': LOOPNZ,
        'end': END, 'condj': CONDJ, 'math': MATH, 'set': SET, 'sync': SYNC, 'read': READ, 'wait': WAIT,
        'bitw': BITW, 'memr': MEMR, 'memw': MEMW, 'setb': SETB}


class TProcError(RuntimeError):
    """ The emulated processor reached a state the real one cannot recover from. """


def _field_width(pos, itype):
    if pos == 0:
        return 16 if itype.startswith('J') else 31
    if pos in (53, 50):
        return 3
    if pos == 46:
        return 4
    return 5


def decode_word(word):
    """ Decode one 64-bit program word.

    @return (str, tuple): instruction name and its arguments in the order of the qick assembler
                          methods (QickProgram.instructions[name]['repr']). Immediate values are
                          sign-extended from 31 bits, operations are returned as their symbol.
    """
    word = int(word)
    opcode = (word >> 56) & 0xFF
    try:
        name = _OPCODES[opcode]
    except KeyError:
        raise TProcError(f'Unknown tProc v1 opcode 0b{opcode:08b} in word 0x{word:016x}') from None
    idef = _INSTR[name]
    fmt = idef['fmt']
    if not fmt:
        return name, ()
    args = [0] * (max(idx for idx, _ in fmt) + 1)
    for idx, pos in fmt:
        width = _field_width(pos, idef['type'])
        val = (word >> pos) & ((1 << width) - 1)
        if width == 31 and val >= 2 ** 30:
            val -= 2 ** 31
        args[idx] = val
    if name in ('math', 'mathi'):
        args[3] = _MATH_OPS[args[3]]
    elif name in ('bitw', 'bitwi'):
        args[3] = _BITW_OPS[args[3]]
    elif name == 'condj':
        args[2] = _COND_OPS[args[2]]
    return name, tuple(args)


def decode_program(binprog):
    """ Decode a compiled program (list of 64-bit words) into a list of (name, args). """
    return [decode_word(w) for w in binprog]


def _s32(val):
    val &= 0xFFFFFFFF
    return val - 0x100000000 if val & 0x80000000 else val


def _s16(val):
    val &= 0xFFFF
    return val - 0x10000 if val & 0x8000 else val


def _math(op, a, b):
    if op == '+':
        return _s32(a + b)
    if op == '-':
        return _s32(a - b)
    # the product uses the lower 16 bits of both operands
    return _s32(_s16(a) * _s16(b))


def _bitw(op, a, b):
    if op == '&':
        return _s32(a & b)
    if op == '|':
        return _s32(a | b)
    if op == '^':
        return _s32(a ^ b)
    if op == '~':
        return _s32(~b)
    if op == '<<':
        return _s32(a << (b & 31))
    return _s32((a & 0xFFFFFFFF) >> (b & 31))


def _cond(op, a, b):
    if op == '>':
        return a > b
    if op == '>=':
        return a >= b
    if op == '<':
        return a < b
    if op == '<=':
        return a <= b
    if op == '==':
        return a == b
    return a != b


class TProcV1Emulator:
    """ Executes a compiled tProc v1 program and collects the timed outputs it dispatches.

    Timed outputs are appended to ``self.events`` as tuples
    ``(t_abs, seq, port, r0, r1, r2, r3, r4)`` (times in tProc cycles; ``seti`` fills r0 only, ``set``
    fills the 5 output registers). The owner removes the events it has consumed.
    """

    STACK_SIZE = 256

    def __init__(self, pmem_size=1024, dmem_size=1024, cycles_per_instruction=5, fifo_depth=16):
        self.pmem_size = int(pmem_size)
        self.dmem_size = int(dmem_size)
        self.cycles_per_instruction = int(cycles_per_instruction)
        self.fifo_depth = int(fifo_depth)
        self.yield_cycles = 100000  # run() returns after a wait longer than this (about 0.3 ms)
        self.dmem = [0] * self.dmem_size
        self.prog = []
        self.reset()

    def load(self, binprog):
        if len(binprog) > self.pmem_size:
            raise TProcError(f'Program has {len(binprog)} words, the program memory holds {self.pmem_size}.')
        prog = []
        for name, args in decode_program(binprog):
            prog.append((_IDS[name],) + args)
        self.prog = prog

    def reset(self):
        """ Processor state at program start (the data memory is kept, as on the board). """
        self.regs = [[0] * 32 for _ in range(8)]
        self.stack = []
        self.pc = 0
        self.t_off = 0
        self.t_cpu = 0
        self.ended = False
        self.events = []
        self._seq = 0
        # diagnostics
        self.n_instructions = 0
        self.n_timed = 0
        self.n_late = 0
        self.worst_late = 0
        self.max_queue = 0
        self.unsupported = set()

    def _emit(self, port, t_abs, r0, r1=0, r2=0, r3=0, r4=0):
        self.n_timed += 1
        if t_abs < self.t_cpu:
            # the real time controller outputs it as soon as it can: late by (t_cpu - t_abs) cycles
            self.n_late += 1
            self.worst_late = max(self.worst_late, self.t_cpu - t_abs)
            t_abs = self.t_cpu
        events = self.events
        events.append((t_abs, self._seq, port, r0, r1, r2, r3, r4))
        self._seq += 1
        if len(events) > self.max_queue:
            # outputs waiting in the queue of this port (they leave it when the master clock reaches them)
            t_cpu = self.t_cpu
            pending = sum(1 for ev in events if ev[2] == port and ev[0] > t_cpu)
            if pending > self.max_queue:
                self.max_queue = pending

    def run(self, counter_addr=None, max_instructions=20000):
        """ Execute until the program ends ('end'), the data memory word ``counter_addr`` is written
        ('count'), it has waited for a long time ('wait') or ``max_instructions`` have been executed
        ('budget').
        """
        if self.ended:
            return 'end'
        prog = self.prog
        nprog = len(prog)
        regs = self.regs
        dmem = self.dmem
        cost = self.cycles_per_instruction
        pc = self.pc
        t_off = self.t_off
        t_cpu = self.t_cpu
        executed = 0
        reason = 'budget'
        while executed < max_instructions:
            if pc >= nprog:
                raise TProcError(f'Program counter {pc} ran past the end of the program ({nprog} words): '
                                 f'missing "end" instruction?')
            ins = prog[pc]
            op = ins[0]
            pc += 1
            executed += 1
            t_cpu += cost
            if op == REGWI:
                if ins[2]:
                    regs[ins[1]][ins[2]] = _s32(ins[3])
            elif op == SETI:
                self.t_cpu = t_cpu
                self._emit(ins[1], t_off + ins[4], regs[ins[2]][ins[3]] & 0xFFFFFFFF)
            elif op == SET:
                page = regs[ins[2]]
                self.t_cpu = t_cpu
                self._emit(ins[1], t_off + page[ins[8]], page[ins[3]] & 0xFFFFFFFF, page[ins[4]] & 0xFFFFFFFF,
                           page[ins[5]] & 0xFFFFFFFF, page[ins[6]] & 0xFFFFFFFF, page[ins[7]] & 0xFFFFFFFF)
            elif op == SYNCI:
                t_off += ins[1]
            elif op == SYNC:
                t_off += regs[ins[1]][ins[2]]
            elif op == WAITI or op == WAIT:
                target = t_off + (ins[2] if op == WAITI else regs[ins[2]][ins[3]])
                if target > t_cpu:
                    long_wait = target - t_cpu > self.yield_cycles
                    t_cpu = target
                    if long_wait:
                        # let the owner catch up (real-time pacing) before later instructions take effect
                        reason = 'wait'
                        break
            elif op == LOOPNZ:
                page = regs[ins[1]]
                if page[ins[2]] != 0:
                    page[ins[2]] = _s32(page[ins[2]] - 1)
                    pc = ins[3]
            elif op == MATHI:
                if ins[2]:
                    page = regs[ins[1]]
                    page[ins[2]] = _math(ins[4], page[ins[3]], ins[5])
            elif op == MATH:
                if ins[2]:
                    page = regs[ins[1]]
                    page[ins[2]] = _math(ins[4], page[ins[3]], page[ins[5]])
            elif op == MEMWI:
                dmem[ins[3] % self.dmem_size] = regs[ins[1]][ins[2]]
                if ins[3] == counter_addr:
                    reason = 'count'
                    break
            elif op == MEMW:
                page = regs[ins[1]]
                addr = page[ins[3]] % self.dmem_size
                dmem[addr] = page[ins[2]]
                if addr == counter_addr:
                    reason = 'count'
                    break
            elif op == MEMRI:
                if ins[2]:
                    regs[ins[1]][ins[2]] = _s32(dmem[ins[3] % self.dmem_size])
            elif op == MEMR:
                if ins[2]:
                    page = regs[ins[1]]
                    page[ins[2]] = _s32(dmem[page[ins[3]] % self.dmem_size])
            elif op == BITWI:
                if ins[2]:
                    page = regs[ins[1]]
                    page[ins[2]] = _bitw(ins[4], page[ins[3]], ins[5])
            elif op == BITW:
                if ins[2]:
                    page = regs[ins[1]]
                    page[ins[2]] = _bitw(ins[4], page[ins[3]], page[ins[5]])
            elif op == CONDJ:
                page = regs[ins[1]]
                if _cond(ins[3], page[ins[2]], page[ins[4]]):
                    pc = ins[5]
            elif op == END:
                self.ended = True
                pc -= 1
                reason = 'end'
                break
            elif op == PUSHI:
                page = regs[ins[1]]
                if len(self.stack) >= self.STACK_SIZE:
                    raise TProcError('tProc stack overflow (pushi on a full stack).')
                # binary layout: bits 40:36 = register pushed, bits 45:41 = register loaded with imm
                self.stack.append(page[ins[3]])
                if ins[2]:
                    page[ins[2]] = _s32(ins[4])
            elif op == POPI:
                if not self.stack:
                    raise TProcError('tProc stack underflow (popi on an empty stack).')
                val = self.stack.pop()
                if ins[2]:
                    regs[ins[1]][ins[2]] = val
            elif op == READ:
                # tProc input ports (readout feedback) are not simulated: reads return 0
                self.unsupported.add('read')
                if ins[4]:
                    regs[ins[2]][ins[4]] = 0
            else:
                self.unsupported.add('setb/setbi')
        self.pc = pc
        self.t_off = t_off
        self.t_cpu = t_cpu
        self.n_instructions += executed
        return reason
