import struct
import logging
import asyncio
from amaranth import *
from amaranth.lib import io

from ....support.bits import *
from ....support.logging import *
from ....support.endpoint import *
from ....gateware.pads import *
from ... import *
from ..jtag_probe import JTAGProbeBus


class JTAGOpenOCDSubtarget(Elaboratable):
    def __init__(self, pads, out_fifo, in_fifo, period_cyc, us_cyc):
        self.pads       = pads
        self.out_fifo   = out_fifo
        self.in_fifo    = in_fifo
        self.period_cyc = period_cyc
        self.us_cyc     = us_cyc
        self.srst_z     = Signal(init=0)
        self.srst_o     = Signal(init=0)

    def elaborate(self, platform):
        m = Module()

        out_fifo = self.out_fifo
        in_fifo  = self.in_fifo

        m.submodules.bus = bus = JTAGProbeBus(self.pads)
        m.d.comb += [
            bus.trst_z.eq(0),
            self.srst_z.eq(0),
        ]
        if hasattr(self.pads, "srst_t"):
            m.d.sync += [
                self.pads.srst_t.oe.eq(~self.srst_z),
                self.pads.srst_t.o.eq(~self.srst_o)
            ]

        blink = Signal()
        try:
            m.submodules.io_blink = io_blink = io.Buffer("o", platform.request("led", dir="-"))
            m.d.comb += io_blink.o.eq(blink)
        except:
            pass

        timer = Signal(range(max(self.period_cyc, 1000 * self.us_cyc)))
        with m.If(timer != 0):
            m.d.sync += timer.eq(timer - 1)
        with m.Else():
            with m.If(out_fifo.r_rdy):
                m.d.comb += out_fifo.r_en.eq(1)
                with m.Switch(out_fifo.r_data):
                    # remote_bitbang_write(int tck, int tms, int tdi)
                    with m.Case(*b"01234567"):
                        m.d.sync += Cat(bus.tdi, bus.tms, bus.tck).eq(out_fifo.r_data[:3])
                        m.d.sync += timer.eq(self.period_cyc - 1)
                    # remote_bitbang_reset(int trst, int srst)
                    with m.Case(*b"rstu"):
                        m.d.sync += Cat(self.srst_o, bus.trst_o).eq(out_fifo.r_data - ord(b"r"))
                    # remote_bitbang_sample(void)
                    with m.Case(*b"R"):
                        m.d.comb += out_fifo.r_en.eq(in_fifo.w_rdy)
                        m.d.comb += in_fifo.w_en.eq(1)
                        m.d.comb += in_fifo.w_data.eq(b"0"[0] | Cat(bus.tdo))
                    # remote_bitbang_blink(int on)
                    with m.Case(*b"Bb"):
                        m.d.sync += blink.eq(~out_fifo.r_data[5])
                    # remote_bitbang_sleep(unsigned int microseconds)
                    with m.Case(*b"Z"):
                        m.d.sync += timer.eq(1000 * self.us_cyc - 1)
                    with m.Case(*b"z"):
                        m.d.sync += timer.eq(self.us_cyc - 1)
                    # remote_bitbang_quit(void)
                    with m.Case(*b"Q"):
                        pass
                    with m.Default():
                        # Hang if an unknown command is received.
                        m.d.comb += out_fifo.r_en.eq(0)

        return m


class JTAGOpenOCDApplet(GlasgowApplet):
    logger = logging.getLogger(__name__)
    help = "expose JTAG via OpenOCD remote bitbang interface"
    description = """
    Expose JTAG via a socket using the OpenOCD remote bitbang protocol.

    Usage with TCP sockets:

    ::
        glasgow run jtag-openocd tcp:localhost:2222
        openocd -c 'adapter driver remote_bitbang; transport select jtag' \\
            -c 'remote_bitbang port 2222'

    Usage with Unix domain sockets:

    ::
        glasgow run jtag-openocd unix:/tmp/jtag.sock
        openocd -c 'adapter driver remote_bitbang; transport select jtag' \\
            -c 'remote_bitbang host /tmp/jtag.sock'
    """

    __pins = ("tck", "tms", "tdi", "tdo", "trst", "srst")

    @classmethod
    def add_build_arguments(cls, parser, access):
        super().add_build_arguments(parser, access)

        for pin in ("tck", "tms", "tdi", "tdo"):
            access.add_pin_argument(parser, pin, default=True)
        for pin in ("trst", "srst"):
            access.add_pin_argument(parser, pin)

        parser.add_argument(
            "-f", "--frequency", metavar="FREQ", type=int, default=100,
            help="set TCK frequency to FREQ kHz (default: %(default)s)")

    def build(self, target, args):
        self.mux_interface = iface = target.multiplexer.claim_interface(self, args)
        iface.add_subtarget(JTAGOpenOCDSubtarget(
            pads=iface.get_pads(args, pins=self.__pins),
            out_fifo=iface.get_out_fifo(),
            in_fifo=iface.get_in_fifo(),
            period_cyc=int(target.sys_clk_freq // (args.frequency * 1000)),
            us_cyc=int(target.sys_clk_freq // 1_000_000),
        ))

    async def run(self, device, args):
        return await device.demultiplexer.claim_interface(self, self.mux_interface, args)

    @classmethod
    def add_interact_arguments(cls, parser):
        ServerEndpoint.add_argument(parser, "endpoint")

    async def interact(self, device, args, iface):
        endpoint = await ServerEndpoint("socket", self.logger, args.endpoint)
        async def forward_out():
            while True:
                try:
                    data = await endpoint.recv()
                    await iface.write(data)
                    await iface.flush()
                except asyncio.CancelledError:
                    pass
        async def forward_in():
            while True:
                try:
                    data = await iface.read()
                    await endpoint.send(data)
                except asyncio.CancelledError:
                    pass
        forward_out_fut = asyncio.ensure_future(forward_out())
        forward_in_fut  = asyncio.ensure_future(forward_in())
        await asyncio.wait([forward_out_fut, forward_in_fut],
                           return_when=asyncio.FIRST_EXCEPTION)

    @classmethod
    def tests(cls):
        from . import test
        return test.JTAGOpenOCDAppletTestCase
