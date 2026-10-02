"""Container smoke test; requires Docker and Python 3, not a local .NET SDK."""
import concurrent.futures
import re
import subprocess
import time
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=120).strip()


suffix = uuid.uuid4().hex[:12]
app = f"demo2-smoke-{suffix}"
volume = f"demo2-diagnostics-{suffix}"


def request(path):
    with urllib.request.urlopen(f"{base}/{path}", timeout=30) as response:
        return response.read().decode()


try:
    docker("volume", "create", volume)
    docker("run", "-d", "--name", app, "-p", "127.0.0.1::8080",
           "-v", f"{volume}:/tmp", "-e", "BLOCK_MS=10000", "demo2-app:smoke")
    address = docker("port", app, "8080/tcp")
    base = f"http://{address}"
    for _ in range(60):
        try:
            assert request("healthz") == "OK"
            break
        except (OSError, AssertionError):
            time.sleep(0.5)
    else:
        raise AssertionError("healthz never became healthy")

    pid = re.search(r"PID=(\d+)", docker("logs", app)).group(1)
    debug_args = ["run", "--rm", "--pid", f"container:{app}",
                  "-v", f"{volume}:/tmp", "demo2-debug:smoke"]
    processes = docker(*debug_args, "dotnet-counters", "ps")
    assert "ThreadPoolDemo" in processes, processes
    docker(*debug_args, "dotnet-counters", "collect", "-p", pid,
           "--counters", "System.Runtime", "--refresh-interval", "1",
           "--duration", "00:00:03", "--format", "csv",
           "--output", "/tmp/demo2-counters.csv")
    metrics = docker(*debug_args, "grep", "-i", "threadpool", "/tmp/demo2-counters.csv")
    assert metrics, "No ThreadPool counter samples collected"

    with concurrent.futures.ThreadPoolExecutor() as executor:
        pending = executor.submit(request, "bad")
        time.sleep(0.5)
        stacks = docker(*debug_args, "dotnet-stack", "report", "-p", pid)
        assert "Thread.Sleep" in stacks, stacks
        assert pending.result() == "blocking work completed"
    assert request("good") == "async work completed"
    assert request("healthz") == "OK"

    invalid = docker("run", "--name", f"{app}-invalid", "-d",
                     "-e", "BLOCK_MS=invalid", "demo2-app:smoke")
    assert int(docker("wait", invalid)) != 0
    invalid_logs = subprocess.check_output(
        ["docker", "logs", invalid], stderr=subprocess.STDOUT, text=True)
    assert "BLOCK_MS must be" in invalid_logs
    print("PASS: health, bad/good endpoints, diagnostic PID/socket access, ThreadPool counter samples, blocked stack, invalid config")
finally:
    for name in (app, f"{app}-invalid"):
        subprocess.run(["docker", "rm", "-f", name], check=False)
    subprocess.run(["docker", "volume", "rm", volume], check=False)
