"""TRex flood, run on the sender with the trex-generator snap's Python."""

# Every packet carries a new source address and UDP source port, so megaflow
# wildcarding cannot fold packets together. Ports are acquired without force:
# a port owned by another session makes the run fail instead of taking it over.

import json, signal, sys, time

from trex.stl.api import *

args = json.loads(sys.argv[1])
out = open(args["out"], "a", buffering=1)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

l3 = IPv6(src=args["src"], dst=args["dst"]) if args["ipv6"] else IP(
    src=args["src"], dst=args["dst"])
packet = Ether(dst=args["mac"]) / Dot1Q(vlan=args["vlan"]) / l3 / UDP(
    sport=1024, dport=args["dport"]) / Raw(load=b"\x00" * 18)
src_size = 8 if args["ipv6"] else 2
vm = STLScVmRaw([
    STLVmFlowVar(name="src", min_value=1, max_value=256 ** src_size - 1,
                 size=src_size, op="inc"),
    STLVmWrFlowVar(fv_name="src", pkt_offset="IPv6.src" if args["ipv6"] else "IP.src",
                   offset_fixup=8 if args["ipv6"] else 2),
    STLVmFlowVar(name="sport", min_value=1024, max_value=65535, size=2, op="inc"),
    STLVmWrFlowVar(fv_name="sport", pkt_offset="UDP.sport"),
    STLVmFixChecksumHw(l3_offset="IPv6" if args["ipv6"] else "IP", l4_offset="UDP",
                       l4_type=CTRexVmInsFixHwCs.L4_TYPE_UDP),
])
client = STLClient()
client.connect()
try:
    client.acquire(ports=[0])
    client.reset(ports=[0])
    client.add_streams(STLStream(packet=STLPktBuilder(pkt=packet, vm=vm),
                                 mode=STLTXCont()), ports=[0])
    client.clear_stats()
    started = False
    for step, (pps, seconds) in enumerate(args["steps"]):
        if started:
            client.update(ports=[0], mult=f"{pps}pps")
        else:
            client.start(ports=[0], mult=f"{pps}pps", duration=args["max_s"])
            started = True
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(1)
            stats = client.get_stats()[0]
            out.write(json.dumps({
                "t": time.time(), "step": step, "requested_pps": pps,
                "opackets": stats["opackets"], "tx_pps": stats["tx_pps"],
                "oerrors": stats["oerrors"]}) + "\n")
finally:
    try:
        client.stop(ports=[0])
        client.release(ports=[0])
    finally:
        client.disconnect()
