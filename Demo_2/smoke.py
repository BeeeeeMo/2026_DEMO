"""API image smoke test; requires Docker and Python 3, not a local .NET SDK.

Runtime troubleshooting is a separate, manual kubectl debug exercise.
"""
import subprocess
import time
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=120).strip()


suffix = uuid.uuid4().hex[:12]
app = f"demo2-smoke-{suffix}"
created = []


def request(path):
    with urllib.request.urlopen(f"{base}/{path}", timeout=10) as response:
        return response.read().decode()


try:
    # The application must not ship diagnostic tools.
    docker("run", "--rm", "--entrypoint", "sh", "demo2-app:smoke", "-c",
           "! command -v dotnet-counters && ! command -v dotnet-stack")
    docker("run", "-d", "--name", app, "-p", "127.0.0.1::8080",
           "-e", "BLOCK_MS=1000", "demo2-app:smoke")
    created.append(app)
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

    for endpoint, expected in (("bad", "blocking work completed"),
                               ("good", "async work completed")):
        start = time.monotonic()
        assert request(endpoint) == expected
        assert time.monotonic() - start >= 0.8, f"/{endpoint} skipped its delay"
    assert request("healthz") == "OK"

    invalid = docker("run", "--name", f"{app}-invalid", "-d",
                     "-e", "BLOCK_MS=invalid", "demo2-app:smoke")
    created.append(invalid)
    assert int(docker("wait", invalid)) != 0
    invalid_logs = subprocess.check_output(
        ["docker", "logs", invalid], stderr=subprocess.STDOUT, text=True, timeout=30)
    assert "BLOCK_MS must be" in invalid_logs
    print("PASS: no diagnostic tools in API image, health, bad/good delays, invalid config")
finally:
    for name in created:
        subprocess.run(["docker", "rm", "-f", name], check=False, timeout=30)
