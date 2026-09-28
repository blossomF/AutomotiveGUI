import socket, time, sys

t0 = time.time()
try:
    ip = socket.gethostbyname("iat-api.xfyun.cn")
    print("DNS OK:", ip, round(time.time() - t0, 2), "s", flush=True)
except Exception as e:
    print("DNS FAIL:", type(e).__name__, e, flush=True)
    sys.exit(1)

t1 = time.time()
try:
    s = socket.create_connection(("iat-api.xfyun.cn", 443), timeout=10)
    print("TCP OK in", round(time.time() - t1, 2), "s", flush=True)
    s.close()
except Exception as e:
    print("TCP FAIL:", type(e).__name__, e, "after", round(time.time() - t1, 2), "s", flush=True)
