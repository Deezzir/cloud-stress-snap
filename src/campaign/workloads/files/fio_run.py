"""Run fio jobs in a guest, each from its scheduled start until its scheduled end."""

import json, os, signal, subprocess, sys, time

args = json.loads(sys.argv[1])
out, grace = args["out"], args["grace_s"]
deadline_codes = set(args["deadline_exit_codes"])


def result_problem(path, numjobs):
    """Why fio's JSON result does not show every job finishing without error."""
    try:
        text = open(path).read()
        jobs = json.loads(text[text.index("{"):])["jobs"]
    except (OSError, ValueError, KeyError):
        return "no readable fio result"
    if len(jobs) != numjobs:
        return f"{len(jobs)} of {numjobs} jobs in the fio result"
    failed = [job["jobname"] for job in jobs if job.get("error")]
    return f"fio jobs failed: {failed}" if failed else None


for job in args["jobs"]:
    name = job["name"]
    base = f"{out}/{name}"
    time.sleep(max(job["start"] - time.time(), 0))
    started = time.time()
    record = {"name": name, "planned_start": job["start"], "planned_end": job["end"],
              "start": started}
    if job["end"] - started < 1:
        record["problem"] = "started after its window ended"
        print(json.dumps(record), file=open(f"{base}.times", "w"))
        sys.exit(1)
    with open(f"{base}.fio", "w") as file:
        file.write(job["file"])
    # fio counts its runtime from after its own setup, so the runtime alone
    # would overrun the window; the deadline below ends the job on time.
    runtime_ms = int((job["end"] - started) * 1000)
    process = subprocess.Popen(
        ["fio", f"{base}.fio", "--output-format=json", f"--output={base}.json",
         f"--write_iops_log={base}", f"--write_bw_log={base}",
         f"--write_hist_log={base}"],
        env={**os.environ, "RUNTIME": f"{runtime_ms}ms"})
    record["stopped_at_deadline"] = False
    try:
        code = process.wait(timeout=max(job["end"] - time.time(), 0))
    except subprocess.TimeoutExpired:
        record["stopped_at_deadline"] = True
        process.send_signal(signal.SIGTERM)
        try:
            code = process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                code = process.wait(timeout=grace)
                record["problem"] = f"fio did not stop within {grace}s and was killed"
            except subprocess.TimeoutExpired:
                code = None
                record["problem"] = f"fio did not exit within {grace}s of SIGKILL"
    record.update({"end": time.time(), "exit_code": code})
    if "problem" not in record:
        expected = deadline_codes if record["stopped_at_deadline"] else {0}
        problem = None if code in expected else f"fio exited with {code}"
        problem = problem or result_problem(f"{base}.json", job["numjobs"])
        if problem:
            record["problem"] = problem
    print(json.dumps(record), file=open(f"{base}.times", "w"))
    if "problem" in record:
        sys.exit(1)
