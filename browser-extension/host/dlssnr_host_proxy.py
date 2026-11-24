#!/usr/bin/env python3
"""Native-messaging proxy for the Firefox snap.

Firefox's snap confinement hides the real /tmp and the user's ~/.local and ~/.config, so the
full bridge cannot run as its native host. This tiny stdlib-only shim runs inside the snap and
forwards the native-messaging byte stream to the real bridge, which runs unconfined as a daemon
started by dlss5-firefox.
"""
import os
import socket
import sys
import threading
import traceback

SOCK = os.environ.get("DLSSNR_BRIDGE_SOCK", "/home/vthieu/dlssnr-runtime/bridge.sock")
LOG = "/home/vthieu/dlssnr-runtime/proxy.log"


def diag(text):
    try:
        with open(LOG, "a") as f:
            f.write(text + "\n")
    except Exception:
        pass


def main():
    diag("start sock=%s uid=%s" % (SOCK, os.getuid()))
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCK)
    except Exception as e:
        diag("connect failed: %s" % repr(e))
        print("DLSS5 bridge daemon unreachable at %s: %s" % (SOCK, e), file=sys.stderr)
        return 1
    diag("connected")

    def to_daemon():
        try:
            while True:
                chunk = sys.stdin.buffer.read(65536)
                if not chunk:
                    diag("stdin EOF")
                    break
                diag("forwarding %d bytes" % len(chunk))
                s.sendall(chunk)
        except Exception as e:
            diag("to_daemon error: %s" % repr(e))
        finally:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=to_daemon, daemon=True).start()
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                diag("daemon EOF")
                break
            diag("reply %d bytes" % len(chunk))
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except Exception as e:
        diag("down error: %s" % repr(e))
    diag("exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
