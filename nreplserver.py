import json
import uuid
from urllib.parse import parse_qs

import bencode2
import gevent
import gevent.socket
import uwsgi
from gevent.queue import Queue
from gevent.server import StreamServer

# Configuration
BASE_PORT = 7888
projects = {}


def get_or_create_project(name):
    if name not in projects:
        port = BASE_PORT + len(projects)
        projects[name] = {
            'port': port,
            'to_browser': Queue(),
            'sessions': {},  # Maps session_id -> TCP socket
            'active': False
            }
        spawn_nrepl_server(name, port)
    return projects[name]


def spawn_nrepl_server(name, port):
    def nrepl_handler(socket, address):
        ctx = projects[name]
        print(f"[{name}] Editor connected on port {port}")

        # Fallback session for clients that don't call 'clone'
        default_sid = str(uuid.uuid4())
        ctx['sessions'][default_sid] = socket

        try:
            while True:
                data = socket.recv(4096)
                if not data:
                    break

                msg = bencode2.bdecode(data)

                # Helper to handle bencode2 byte keys
                def get_val(k):
                    v = msg.get(k.encode() if isinstance(k, str) else k)
                    return v.decode('utf-8'
                                   ) if isinstance(v, bytes) else v

                op = get_val('op')
                mid = get_val('id')
                sid = get_val('session') or default_sid

                if op == 'clone':
                    new_sid = str(uuid.uuid4())
                    ctx['sessions'][new_sid] = socket
                    socket.sendall(
                        bencode2.bencode(
                            {
                                b"new-session": new_sid.encode(),
                                b"id": mid.encode() if mid else b"0",
                                b"status": [b"done"]
                                }
                            )
                        )

                elif op == 'eval':
                    code = get_val('code')
                    sid = get_val(
                        'session'
                        ) or default_sid  # Use the default if not provided
                    ctx['to_browser'].put(
                        {
                            "op": "eval",
                            "code": code,
                            "id": mid,
                            "session": sid  # Pass this along!
                            }
                        )

                elif op == 'close':
                    target_sid = get_val('session')
                    if target_sid:
                        ctx['sessions'].pop(target_sid, None)
                    socket.sendall(
                        bencode2.bencode(
                            {
                                b"id": mid.encode(),
                                b"status": [b"done"]
                                }
                            )
                        )

                else:
                    # Keep client alive for unknown ops
                    socket.sendall(
                        bencode2.bencode(
                            {
                                b"id": mid.encode() if mid else b"0",
                                b"status": [b"done"]
                                }
                            )
                        )
        except Exception as e:
            print(f"[{name}] TCP Error: {e}")

    server = StreamServer(('127.0.0.1', port), nrepl_handler)
    gevent.spawn(server.serve_forever)
    print(f"nREPL Bridge for '{name}' started on port {port}")


# def application(env, start_response):
#     query = parse_qs(env.get('QUERY_STRING', ''))
#     project_name = query.get('project', ['default'])[0]

#     uwsgi.websocket_handshake(
#         env['HTTP_SEC_WEBSOCKET_KEY'], env.get('HTTP_ORIGIN', '')
#         )
#     print(f"DEBUG: Browser [{project_name}] Connected")

#     ctx = get_or_create_project(project_name)
#     ctx['active'] = True
#     fd = uwsgi.connection_fd()

#     while True:
#         readable = False
#         try:
#             # We wait for data from the browser
#             gevent.socket.wait_read(fd, timeout=0.1)
#             readable = True
#         except gevent.timeout.Timeout:
#             # THIS IS NORMAL. Do not break.
#             # It just means the browser hasn't sent anything in 100ms.
#             pass
#         except Exception as e:
#             print(f"DEBUG: Critical Socket Error (Breaking Loop): {e}")
#             break

#         # 1. Editor -> Browser (Check this even if wait_read timed out!)
#         try:
#             while not ctx['to_browser'].empty():
#                 msg = ctx['to_browser'].get_nowait()
#                 print(f"DEBUG: Editor -> Browser: {msg.get('code')}")
#                 uwsgi.websocket_send(json.dumps(msg))
#         except Exception as e:
#             print(f"DEBUG: WS Send error: {e}")
#             break

#         # 2. Browser -> Editor
#         if readable:
#             try:
#                 msg = uwsgi.websocket_recv_nb()
#                 if msg:
#                     print(f"DEBUG: Browser -> Editor: {msg}")
#                     resp = json.loads(msg)
#                     sid = resp.get('session')
#                     target_socket = ctx['sessions'].get(sid)

#                     if target_socket:
#                         payload = {
#                             b"id":
#                                 str(resp.get('id', '')).encode(),
#                             b"session":
#                                 str(sid).encode(),
#                             b"value":
#                                 str(resp.get('value', 'nil')).encode(),
#                             b"status": [b"done"]
#                             }
#                         target_socket.sendall(bencode2.bencode(payload))
#                 elif msg is None:
#                     print("DEBUG: Browser connection closed")
#                     break
#             except Exception as e:
#                 print(f"DEBUG: WS Recv error: {e}")
#                 break

#     return [b""]

import json

import bencode2
import gevent
import gevent.socket
import gevent.timeout  # Explicitly import this
import uwsgi

# ... rest of your setup ...


def application(env, start_response):
    query = parse_qs(env.get('QUERY_STRING', ''))
    project_name = query.get('project', ['default'])[0]

    uwsgi.websocket_handshake(
        env['HTTP_SEC_WEBSOCKET_KEY'], env.get('HTTP_ORIGIN', '')
        )
    print(f"DEBUG: Browser [{project_name}] Connected")

    ctx = get_or_create_project(project_name)
    ctx['active'] = True
    fd = uwsgi.connection_fd()

    while True:
        readable = False
        try:
            # Block for 0.1 seconds waiting for browser input
            gevent.socket.wait_read(fd, timeout=0.1)
            readable = True
        except (gevent.timeout.Timeout, TimeoutError, Exception) as e:
            # We check the message: if it's a timeout, we just ignore it and move on
            if "timed out" in str(e).lower():
                pass
            else:
                # This is a real error (like a closed socket)
                print(f"DEBUG: Connection Closed or Fatal Error: {e}")
                break

        # 1. Check for code from Editor -> Forward to Browser
        try:
            # Process ALL pending messages in the queue
            while not ctx['to_browser'].empty():
                msg = ctx['to_browser'].get_nowait()
                print(
                    f"DEBUG: Forwarding to Browser -> {msg.get('code')}"
                    )
                uwsgi.websocket_send(json.dumps(msg))
        except Exception as e:
            print(f"DEBUG: WS Send Error: {e}")
            break

        # 2. If browser sent data -> Forward to Editor
        if readable:
            try:
                msg = uwsgi.websocket_recv_nb()
                if msg:
                    print(f"DEBUG: Response from Browser <- {msg}")
                    resp = json.loads(msg)
                    sid = resp.get('session')
                    target_socket = ctx['sessions'].get(sid)

                    if target_socket:
                        payload = {
                            b"id":
                                str(resp.get('id', '')).encode(),
                            b"session":
                                str(sid).encode(),
                            b"value":
                                str(resp.get('value', 'nil')).encode(),
                            b"status": [b"done"]
                            }
                        target_socket.sendall(bencode2.bencode(payload))
                elif msg is None:
                    print("DEBUG: Browser disconnected")
                    break
            except Exception as e:
                print(f"DEBUG: WS Recv/Routing Error: {e}")
                break

    return [b""]
