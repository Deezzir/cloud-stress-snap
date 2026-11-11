"""Fixed operational and protocol constants of cloud-stress."""

# Run timeline and time budgets.
MAX_DURATION_S = 24 * 3600
WARMUP_S = 10
# The host and Ceph samplers report every 5-10 s.
MIN_WINDOW_S = 10
PREPARE_BUDGET_S = 1800
# Prefill must write each volume at 1 GiB per minute or faster.
PREFILL_S_PER_GIB = 60
FINALIZE_BUDGET_S = 1800
# Time to start every guest's workload units before the warm-up begins.
LEAD_S = 60
MONITOR_S = 30
# Receiver counters keep being sampled this long after the senders stop.
DRAIN_S = 5

# Cloud and guest operations.
BUILD_TIMEOUT_S = 600
BOOT_TIMEOUT_S = 600
PARALLEL = 16
SSH_TIMEOUT_S = 120
EXEC_TIMEOUT_S = 120
MAX_FETCH_BYTES = 64 * 1024 * 1024

# Worker control socket.
IDLE_EXIT_S = 60
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_CONNECTIONS = 8
READ_TIMEOUT_S = 30

# Samplers, in seconds between samples.
SAMPLE_INTERVAL_S = {"host": 5, "ceph": 10, "guest": 1}

# TRex.
TREX_SNAP = "trex-generator"
TREX_CHANNEL = "latest/edge"
TREX_PLUGS = (
    "network-control",
    "hardware-observe",
    "process-control",
    "system-observe",
)
TREX_RPC_PORT = 4501
TREX_ARCHITECTURES = {"amd64"}
# Master and RX threads plus one traffic core (-c 1).
TREX_MIN_VCPUS = 3
RECEIPT_PPS = 1000
RECEIPT_S = 10
RECEIPT_MIN = 0.8

# fio.
PREFILL_JOBS = 4
FIO_DIRECTIONS = {0: "read", 1: "write"}
# fio averages each log entry over this period.
FIO_LOG_MS = 1000
# Time fio gets to stop after the end of its window before it is killed.
FIO_STOP_GRACE_S = 30
# Exit codes fio returns when stopped by SIGTERM at the end of its window.
FIO_DEADLINE_EXIT_CODES = (0, 128)
# Latency histogram layout (FIO_IO_U_PLAT_BITS/VAL); bins are in ns.
FIO_PLAT_BITS = 6
FIO_PLAT_VAL = 64

# Reports.
MAX_REPORT_FILE_BYTES = 64 * 1024**2
MAX_CLOCK_OFFSET_S = 2.0

# Graphs.
PLOT_FAINT = {"linewidth": 0.6, "alpha": 0.35}
PLOT_COLORS = {"read": "tab:blue", "write": "tab:orange"}
