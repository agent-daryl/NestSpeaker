#!/usr/bin/env python3
"""
Nest Mini / Google Cast TTS player v6.
Working protocol — CONNECT channel, LAUNCH receiver, LOAD media.
"""

import argparse
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time

from google.protobuf.message import DecodeError
from http.server import HTTPServer, SimpleHTTPRequestHandler
import logging

class QuietHTTPHandler(SimpleHTTPRequestHandler):
    def log_error(self, format, *args):
        exc_name = getattr(args[0], '__class__', lambda: None).__name__
        if exc_name in ('ConnectionResetError', 'BrokenPipeError'):
            return
        super().log_error(format, *args)
from gtts import gTTS

from pychromecast.cast_channel_pb2 import CastMessage

SENDER_ID = "sender-0"
RECEIVER_ID = "receiver-0"

MAIN_NS = "urn:x-cast:com.google.cast.main"
CONNECTION_NS = "urn:x-cast:com.google.cast.tp.connection"
RECEIVER_NS = "urn:x-cast:com.google.cast.receiver"
MEDIA_NS = "urn:x-cast:com.google.cast.media"

MEDIA_RECEIVER_APP_ID = "CC1AD845"
NEST_IP = "10.10.100.122"

_request_counters = {}


def _next_rid(ns):
    if ns not in _request_counters:
        _request_counters[ns] = 0
    _request_counters[ns] += 1
    return _request_counters[ns]


def _with_rid(payload_dict, namespace):
    p = dict(payload_dict)
    p["requestId"] = _next_rid(namespace)
    return json.dumps(p)


def build_msg(source_id, destination_id, namespace, payload_str):
    msg = CastMessage()
    msg.protocol_version = 0  # CASTV2_1_0
    msg.source_id = source_id
    msg.destination_id = destination_id
    msg.namespace = namespace
    msg.payload_type = 0  # STRING
    msg.payload_utf8 = payload_str
    raw = msg.SerializeToString()
    return struct.pack(">I", len(raw)) + raw


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            d = sock.recv(n - len(buf))
            if not d:
                return bytes(buf)
            buf.extend(d)
        except socket.timeout:
            break
    return bytes(buf)


def read_one_msg(sock):
    try:
        try:
            len_bytes = _recv_exact(sock, 4)
        except (socket.timeout, BlockingIOError):
            return None
        if not len_bytes or len(len_bytes) < 4:
            return None
        msg_len = struct.unpack(">I", len_bytes)[0]
        if msg_len > 65536:
            return None
        raw = _recv_exact(sock, msg_len)
        if not raw or len(raw) < msg_len:
            return None
        proto = CastMessage()
        proto.ParseFromString(raw)
        return proto
    except (DecodeError, Exception):
        return None


def _try_json(s):
    try:
        return json.loads(s)
    except:
        return None


def ns_short(ns):
    return ns.split(":")[-1] if ns else "?"


def send_raw(sock, src, dst, ns, payload_str, rid=0):
    sock.send(build_msg(src, dst, ns, payload_str))


def send_msg(sock, src, dst, ns, payload_dict):
    """Send a JSON payload with auto requestId."""
    send_raw(sock, src, dst, ns, _with_rid(payload_dict, ns))


def send_connect(sock, src, dst):
    """Channel-level CONNECT in CONNECTION_NS (no requestId)."""
    payload = {
        "type": "CONNECT",
        "userAgent": "NestPlayer",
        "senderInfo": {
            "sdkType": 2,
            "version": "15.605.1.3",
            "platform": 4,
            "systemVersion": "Linux"
        }
    }
    send_raw(sock, src, dst, CONNECTION_NS, json.dumps(payload))


def receive(sock, timeout=2):
    """Receive one message, print debug, return parsed dict."""
    sock.settimeout(timeout)
    msg = read_one_msg(sock)
    if msg is None:
        return None
    parsed = _try_json(msg.payload_utf8)
    msg_type = parsed.get("type", "?") if parsed else "?"
    rid = parsed.get("requestId", "?") if parsed else "?"
    payload_preview = msg.payload_utf8[:350] if msg.payload_utf8 else "[binary]"
    print(f"    <- [{ns_short(msg.namespace)}] rid={rid} type={msg_type} {payload_preview}")
    return parsed


def drain_all(sock, timeout=0.5):
    """Drain all pending messages."""
    while True:
        parsed = receive(sock, timeout=timeout)
        if parsed is None:
            break


def wait_for_type(sock, target, max_attempts=15, timeout=2):
    """Receive messages until one of target type."""
    results = []
    for _ in range(max_attempts):
        parsed = receive(sock, timeout=timeout)
        if parsed is None:
            break
        results.append(parsed)
        if parsed.get("type") == target:
            return results
    return results


# ---- HTTP server ----
def start_http_server(directory="/tmp", port=8001):
    os.chdir(directory)
    server = HTTPServer(("0.0.0.0", port), QuietHTTPHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"  HTTP server on :{port}")
    return server


# ---- Player ----
class NestPlayer:
    def __init__(self, ip="10.10.100.122"):
        self.ip = ip
        self.sock = None
        self.session_id = None
        self._stop_event = threading.Event()
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def connect(self):
        print(f"  [1] Connecting to {self.ip}:8009 ...")
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(10)
        raw.connect((self.ip, 8009))
        self.sock = self.ctx.wrap_socket(raw, server_hostname=self.ip)
        print("      Connected (TLS).")

    def handshake(self):
        print("  [2] Handshake ...")
        send_connect(self.sock, SENDER_ID, RECEIVER_ID)
        drain_all(self.sock, timeout=2)
        print("      Handshake complete.")

    def _get_session_from_status(self, parsed):
        """Extract session ID for the media receiver from a RECEIVER_STATUS message."""
        if not parsed or parsed.get("type") != "RECEIVER_STATUS":
            return None
        apps = parsed.get("status", {}).get("applications", [])
        for app in apps:
            if app.get("appId") == MEDIA_RECEIVER_APP_ID:
                return app.get("sessionId")
        return None

    def launch_media_receiver(self):
        print("  [3] Launching Media Receiver ...")

        # Always LAUNCH unconditionally — idempotent on modern firmware.
        # Gemini firmware delays the second RECEIVER_STATUS by 1.5-3 seconds
        # after LAUNCH_STATUS and MULTIZONE_STATUS. We must keep reading
        # until we get it.
        send_msg(self.sock, SENDER_ID, RECEIVER_ID, RECEIVER_NS, {
            "type": "LAUNCH",
            "appId": MEDIA_RECEIVER_APP_ID
        })

        deadline = time.time() + 8
        session_id = None
        transport_id = None

        while time.time() < deadline:
            parsed = receive(self.sock, timeout=min(2, max(0.5, deadline - time.time())))
            if parsed is None:
                continue
            if parsed.get("type") == "RECEIVER_STATUS":
                apps = parsed.get("status", {}).get("applications", [])
                for app in apps:
                    if app.get("appId") == MEDIA_RECEIVER_APP_ID:
                        session_id = app.get("sessionId")
                        transport_id = app.get("transportId")
                        break
                if session_id:
                    break
            # Ignore LAUNCH_STATUS and MULTIZONE_STATUS, keep reading

        if not session_id:
            print("      Launch failed — no sessionId from RECEIVER_STATUS.")
            return False

        self.session_id = session_id
        self._transport_id = transport_id
        print(f"      Launched, session = {self.session_id}, transport = {transport_id}")
        return True

    def open_media_channel(self):
        if not self.session_id:
            return False
        dest = self.session_id
        print(f"  [4] Open media channel to {dest} ...")
        send_connect(self.sock, SENDER_ID, dest)
        drain_all(self.sock, timeout=2)
        print("      Channel open.")
        return True

    def play_tts(self, text, server_ip="10.10.0.100", server_port=8001):
        ts = int(time.time())
        fname = f"nest_tts_{ts}.mp3"
        fpath = f"/tmp/{fname}"
        gTTS(text, lang="en").save(fpath)
        size = os.path.getsize(fpath)
        print(f"  [5] TTS audio: {fname} ({size}B)")
        url = f"http://{server_ip}:{server_port}/{fname}"
        self.play_media(url)
        # Auto-cleanup MP3 after playback finishes or times out
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
                print(f"      Cleaned up {fname}")
        except OSError:
            pass

    def _respond_to_heartbeats(self):
        """Background thread: respond to PING/HEARTBEAT so the Cast socket stays alive."""
        while self.sock and not self._stop_event.is_set():
            try:
                self.sock.settimeout(1)
                msg = read_one_msg(self.sock)
                if msg is None:
                    continue
                parsed = _try_json(msg.payload_utf8)
                if not parsed:
                    continue
                if parsed.get("type") == "PING":
                    rid = parsed.get("requestId", 0)
                    ts = parsed.get("timestamp", 0)
                    ns = msg.namespace
                    send_raw(self.sock, SENDER_ID, msg.source_id, ns,
                             json.dumps({"type": "PONG", "requestId": rid, "timestamp": ts}))
            except:
                break

    def play_media(self, url, content_type="audio/mp3"):
        import threading
        import time as _time
        dest = self.session_id
        if not dest:
            print("  [6] No session; aborting.")
            return
        print(f"  [6] LOAD media: {url}")

        send_msg(self.sock, SENDER_ID, dest, MEDIA_NS, {
            "type": "LOAD",
            "media": {
                "contentId": url,
                "contentType": content_type,
                "streamType": "BUFFERED",
                "metadata": {"type": 0}
            },
            "autoplay": True,
            "currentTime": 0
        })

        # Wait for MEDIA_STATUS with playerState BUFFERING or PLAYING
        # (First MEDIA_STATUS will be IDLE — we ignore it)
        for _ in range(30):
            parsed = receive(self.sock, timeout=2)
            if parsed is None:
                continue
            if parsed.get("type") != "MEDIA_STATUS":
                continue
            status_list = parsed.get("status", [])
            if not status_list:
                continue
            state = status_list[0].get("playerState", "?")
            idle = status_list[0].get("idleReason")
            print(f"    MEDIA_STATUS: state={state} idleReason={idle}")
            if state in ("BUFFERING", "PLAYING"):
                print("      Audio is playing — keep listening!")
                # Keep connection alive until playback finishes or ~20s timeout
                deadline = _time.time() + 20
                heartbeat_th = threading.Thread(target=self._respond_to_heartbeats, daemon=True)
                heartbeat_th.start()
                while _time.time() < deadline:
                    p = receive(self.sock, timeout=1)
                    if p is None:
                        continue
                    if p.get("type") == "MEDIA_STATUS":
                        sl = p.get("status", [{}])
                        s = sl[0].get("playerState", "?")
                        ir = sl[0].get("idleReason")
                        if s == "IDLE" and ir == "FINISHED":
                            print("      Playback finished.")
                            break
                print("      (timeout / finished)")
                return
            elif state == "IDLE" and idle == "ERROR":
                cid = status_list[0].get("errorId", "?")
                print(f"      Playback error: errorId={cid}")
                return
        print("      LOAD sent but no PLAYING state received.")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass


def main():
    parser = argparse.ArgumentParser(description="Play TTS on Google Nest Mini")
    parser.add_argument("message", nargs="?", help="Text to speak")
    parser.add_argument("--ip", default=NEST_IP, help="Nest/Chromecast IP address")
    parser.add_argument("--server-ip", default="", help="Host IP for file serving (auto-detect if empty)")
    parser.add_argument("--server-port", type=int, default=8001)
    args = parser.parse_args()

    if not args.server_ip:
        try:
            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tmp.connect((args.ip, 8009))
            args.server_ip = tmp.getsockname()[0]
        except:
            args.server_ip = "127.0.0.1"
        finally:
            tmp.close()

    start_http_server(port=args.server_port)
    p = NestPlayer(ip=args.ip)
    try:
        p.connect()
        p.handshake()
        if p.launch_media_receiver():
            p.open_media_channel()
            if args.message:
                p.play_tts(args.message, args.server_ip, args.server_port)
                time.sleep(10)
                print("  Done.")
            else:
                print("  No message. Try: python3 nest_speaker.py 'Hello!'")
        else:
            print("  FAIL: could not launch Media Receiver")
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        p.close()
        print("  Disconnected.")


if __name__ == "__main__":
    main()
