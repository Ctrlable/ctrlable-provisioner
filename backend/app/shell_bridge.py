#!/usr/bin/env python3
"""PTY <-> websocket bridge for the portal remote shell.

Launched by run.sh when a heartbeat delivers an open_shell block. Dials the
portal OUTBOUND (wss, works behind CGNAT), forks a bash PTY, and relays bytes:
binary frames are PTY data, text frames are control (resize). Hard 30-min cap.
The session's auth token is carried in the ws_url query string, so this process
takes only CTRL_SHELL_WS from the environment.
"""
import asyncio
import fcntl
import json
import os
import pty
import signal
import ssl
import struct
import sys
import termios

WS_URL = os.environ.get("CTRL_SHELL_WS", "")
HARD_CAP_SECONDS = 1800  # 30-min ceiling matching the portal session TTL


def _read_fd(fd):
    try:
        return os.read(fd, 65536)
    except OSError:
        return b""


def _resize(fd, text_msg):
    try:
        m = json.loads(text_msg)
        if m.get("type") == "resize":
            rows = int(m.get("rows", 24))
            cols = int(m.get("cols", 80))
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


async def bridge():
    if not WS_URL:
        return
    try:
        import websockets
    except Exception:
        print("shell_bridge: websockets library missing", file=sys.stderr)
        return

    pid, fd = pty.fork()
    if pid == 0:
        # Child: become an interactive login shell.
        os.environ["TERM"] = "xterm-256color"
        try:
            os.execvp("bash", ["bash", "-l"])
        except Exception:
            os.execvp("sh", ["sh"])
        os._exit(1)

    loop = asyncio.get_event_loop()
    ssl_ctx = ssl.create_default_context() if WS_URL.startswith("wss") else None
    try:
        async with websockets.connect(
            WS_URL, ssl=ssl_ctx, max_size=None, ping_interval=20, ping_timeout=20
        ) as ws:
            async def pty_to_ws():
                while True:
                    data = await loop.run_in_executor(None, _read_fd, fd)
                    if not data:
                        break
                    await ws.send(data)

            async def ws_to_pty():
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        os.write(fd, msg)
                    else:
                        _resize(fd, msg)

            t1 = asyncio.create_task(pty_to_ws())
            t2 = asyncio.create_task(ws_to_pty())
            _, pending = await asyncio.wait(
                {t1, t2}, timeout=HARD_CAP_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
    except Exception as e:  # noqa: BLE001
        print(f"shell_bridge: {e}", file=sys.stderr)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(bridge())
    except KeyboardInterrupt:
        pass
