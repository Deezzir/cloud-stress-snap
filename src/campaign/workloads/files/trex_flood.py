"""Scheduled TRex UDP flood from a sender guest, run with the trex-generator snap's Python.

Each receiver gets one stream; streams have equal weight, so the requested rate
is the sender's total, shared among its receivers. Statistics are written once
per second as JSON lines. Ports are acquired without force: a port owned by
another session makes the run fail instead of taking it over.
"""

import json, signal, sys, time

from trex.stl.api import *

args = json.loads(sys.argv[1])
out = open(args["out"], "a", buffering=1)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

low, high = args["sports"]
l3 = IPv6 if args["ipv6"] else IP
header = len(Ether() / l3() / UDP())
streams = []
for peer in args["peers"]:
    packet = Ether(src=args["src_mac"], dst=peer["mac"]) / l3(
        src=args["src"], dst=peer["dst"]) / UDP(sport=low, dport=args["dport"]) / Raw(
        load=b"\xff\xff" + b"\x00" * (args["payload"] - 2))
    # The source port counts up while the first payload word counts down in
    # step, so their sum, and with it the UDP checksum, never changes.
    vm = STLScVmRaw([
        STLVmFlowVar(name="sport", min_value=low, max_value=high, size=2, op="inc"),
        STLVmWrFlowVar(fv_name="sport", pkt_offset="UDP.sport"),
        STLVmFlowVar(name="balance", min_value=65535 - (high - low), max_value=65535,
                     init_value=65535, size=2, op="dec"),
        STLVmWrFlowVar(fv_name="balance", pkt_offset=header),
    ])
    streams.append(STLStream(
        packet=STLPktBuilder(pkt=packet, vm=vm), mode=STLTXCont(pps=1000),
        mac_src_override_by_pkt=True, mac_dst_override_mode=STLStreamDstMAC_PKT))


def wait_until(moment):
    time.sleep(max(moment - time.time(), 0))


def sample(step, pps, **extra):
    stats = client.get_stats()[0]
    out.write(json.dumps({
        "t": time.time(), "step": step, "requested_pps": pps,
        "opackets": stats["opackets"], "obytes": stats["obytes"],
        "tx_pps": stats["tx_pps"], "oerrors": stats["oerrors"], **extra}) + "\n")


client = STLClient()
client.connect()
try:
    client.acquire(ports=[0])
    client.reset(ports=[0])
    client.add_streams(streams, ports=[0])
    client.clear_stats()
    steps = args["steps"]
    for step, (pps, start) in enumerate(steps):
        end = steps[step + 1][1] if step + 1 < len(steps) else args["end"]
        wait_until(start)
        if step:
            client.update(ports=[0], mult=f"{pps}pps")
        else:
            client.start(ports=[0], mult=f"{pps}pps")
        out.write(json.dumps({"t": time.time(), "event": "step", "step": step,
                              "requested_pps": pps}) + "\n")
        while time.time() < end:
            wait_until(min(int(time.time()) + 1, end))
            sample(step, pps)
    client.stop(ports=[0])
    time.sleep(1)
    sample(len(steps) - 1, 0, final=True)
finally:
    try:
        client.stop(ports=[0])
        client.release(ports=[0])
    finally:
        client.disconnect()
