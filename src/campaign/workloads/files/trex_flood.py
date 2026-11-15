"""Scheduled TRex UDP flood from a sender guest, run with the trex-generator snap's Python.

Each receiver gets one stream; streams have equal weight, so the requested rate
is the sender's total, shared among its receivers. Statistics are written once
per second as JSON lines. Ports are acquired without force: a port owned by
another session makes the run fail instead of taking it over.
"""

import json, signal, sys, time
from contextlib import ExitStack
from pathlib import Path

from trex.stl.api import *

args = json.loads(sys.argv[1])
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


def streams_for(index, count):
    first, last = args["sports"]
    width = last - first + 1
    low = first + width * index // count
    high = first + width * (index + 1) // count - 1
    l3 = IPv6 if args["ipv6"] else IP
    header = len(Ether() / l3() / UDP())
    streams = []
    for peer in args["peers"]:
        packet = Ether(src=args["src_mac"], dst=peer["mac"]) / l3(
            src=args["src"], dst=peer["dst"]) / UDP(sport=low, dport=args["dport"]) / Raw(
            load=b"\xff\xff" + b"\x00" * (args["payload"] - 2))
        # The source port and first payload word change in opposite directions,
        # preserving the UDP checksum. Each process owns a disjoint port range.
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
    return streams


def wait_until(moment):
    time.sleep(max(moment - time.time(), 0))


def sample(step, pps, **extra):
    rows = []
    for index, client in enumerate(clients):
        stats = client.get_stats()[0]
        row = {
            "t": time.time(), "step": step, "requested_pps": pps / count,
            "opackets": stats["opackets"], "obytes": stats["obytes"],
            "tx_pps": stats["tx_pps"], "oerrors": stats["oerrors"], **extra}
        outputs[index].write(json.dumps(row) + "\n")
        rows.append(row)
    # Publish only complete samples; a missing process must never look like zero TX.
    aggregate = {
        "t": sum(r["t"] for r in rows) / count,
        "step": step, "requested_pps": pps, **extra,
        **{key: sum(r[key] for r in rows)
           for key in ("opackets", "obytes", "tx_pps", "oerrors")}}
    out.write(json.dumps(aggregate) + "\n")


def release(client):
    try:
        client.stop(ports=[0])
    finally:
        client.release(ports=[0])


count = args["processes"]
clients, outputs = [], []
with ExitStack() as cleanup:
    out = cleanup.enter_context(open(args["out"], "a", buffering=1))
    for index in range(count):
        path = Path(args["out"])
        outputs.append(cleanup.enter_context(
            open(path.parent / f"process-{index}" / path.name, "a", buffering=1)))
        rpc = args["rpc_port"] + 2 * index
        client = STLClient(sync_port=rpc, async_port=rpc - 1)
        cleanup.callback(client.disconnect)
        client.connect()
        client.acquire(ports=[0])
        cleanup.callback(release, client)
        client.reset(ports=[0])
        client.add_streams(streams_for(index, count), ports=[0])
        client.clear_stats()
        clients.append(client)
    steps = args["steps"]
    for step, (pps, start) in enumerate(steps):
        end = steps[step + 1][1] if step + 1 < len(steps) else args["end"]
        wait_until(start)
        if time.time() >= end:
            raise RuntimeError("TRex setup or rate update missed its scheduled window")
        for index, client in enumerate(clients):
            if step:
                client.update(ports=[0], mult=f"{pps / count}pps")
            else:
                client.start(ports=[0], mult=f"{pps / count}pps")
            outputs[index].write(json.dumps({
                "t": time.time(), "event": "step", "step": step,
                "requested_pps": pps / count}) + "\n")
        out.write(json.dumps({"t": time.time(), "event": "step", "step": step,
                              "requested_pps": pps}) + "\n")
        while time.time() < end:
            wait_until(min(int(time.time()) + 1, end))
            for client in clients:
                if not client.is_traffic_active(ports=[0]):
                    raise RuntimeError("a TRex process stopped transmitting early")
            sample(step, pps)
    for client in clients:
        client.stop(ports=[0])
    time.sleep(1)
    sample(len(steps) - 1, 0, final=True)
