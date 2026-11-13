# -*- coding: utf-8 -*-
"""
Continuous tone at a list of generator gains, one after the other, to measure the DAC output power
(spectrum analyser on DAC_B, AMPLIFIER NOT CONNECTED).

    python tools\\cw_tone.py --gains 500 1000 2000                      (gains up to 2000 need no extra option)
    python tools\\cw_tone.py --gains 1000 2000 5000 10000 20000 32766 --amplifier-disconnected
    python tools\\cw_tone.py --frequency 2870 --nqz 1 --gains 10000 --amplifier-disconnected

The tone stays on at each gain until Enter is pressed (or for --dwell seconds). All outputs are switched
off at the end, also after Ctrl+C or an error. Close qudi (or deactivate its RFSoC modules) first: the
board runs one program at a time.

For each gain, read on the analyser the power of the tone (--frequency) and of its image
(4915.2 MHz - frequency). The table printed at the end has the columns to fill in.
"""

import argparse
import logging
import math
import sys
import time

from rfsoc_nv import QickBoard, SteppedCW

SAFE_GAIN = 2000  # gains above this need --amplifier-disconnected


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--gains', type=int, nargs='+', required=True, help='generator gains, DAC units (1 .. 32766)')
    parser.add_argument('--frequency', type=float, default=2870.0, help='MHz (default 2870)')
    parser.add_argument('--nqz', type=int, default=2, choices=(1, 2), help='Nyquist zone setting of the DAC')
    parser.add_argument('--gen', type=int, default=0, help='generator channel (0 = DAC_B)')
    parser.add_argument('--dwell', type=float, default=None, help='seconds per gain instead of waiting for Enter')
    parser.add_argument('--amplifier-disconnected', action='store_true',
                        help=f'confirms that nothing but a measuring instrument is on the output; needed for '
                             f'gains above {SAFE_GAIN}')
    parser.add_argument('--host', default='192.168.0.2')
    parser.add_argument('--simulate', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    gains = list(args.gains)
    if min(gains) < 1 or max(gains) > 32766:
        sys.exit('Gains must be 1 .. 32766.')
    if max(gains) > SAFE_GAIN and not args.amplifier_disconnected:
        sys.exit(f'Gains above {SAFE_GAIN} are only played with --amplifier-disconnected (the amplifier input '
                 f'must not be on the DAC output: its absolute maximum is +5 dBm).')

    board = QickBoard(max_gain=max(gains), host=args.host, simulate=args.simulate)
    board.connect()
    fs = board.soccfg['gens'][args.gen]['fs']
    # the frequency word is the same for both settings of the DAC: --nqz only changes the DAC output mode
    cw = SteppedCW(board, gen_ch=args.gen, nqz=args.nqz, check_zone=False)
    played = cw.round_frequency(args.frequency * 1e6) / 1e6
    image = fs - played
    print(f'\nTone {played:.6f} MHz, image {image:.6f} MHz, DAC Nyquist zone setting {args.nqz}, generator {args.gen}.')
    done = []
    try:
        for gain in gains:
            if cw.running:
                cw.stop()  # off for about 0.2 s between two gains
            cw.configure([args.frequency * 1e6], gain=gain)
            cw.start()
            rel = 20 * math.log10(gain / gains[0])
            print(f'  gain {gain:5d} ON   ({rel:+6.2f} dB relative to gain {gains[0]} if the DAC is linear)', end='',
                  flush=True)
            if args.dwell is None:
                input('   - Enter for the next gain ')
            else:
                time.sleep(args.dwell)
                print()
            done.append(gain)
    except KeyboardInterrupt:
        print('\ninterrupted')
    finally:
        try:
            board.outputs_off()
            print('All outputs off.')
        finally:
            board.close()

    print(f'\n  gain    P({played:.0f} MHz) dBm    P({image:.0f} MHz) dBm     (nqz {args.nqz})')
    for gain in done:
        print(f'  {gain:5d}   ..........          ..........')


if __name__ == '__main__':
    main()
