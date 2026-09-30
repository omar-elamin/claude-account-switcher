"""Compare Codex gateway CPU cost: an older git revision against the working tree.

Each gateway runs alone in a child process behind local TLS, relaying to a
local TLS upstream that streams many small SSE events (like the Responses API).
Only the gateway process's CPU time is measured. Numbers are informational.

    PYTHONPATH=src uv run python scripts/bench_codex_gateway.py
    PYTHONPATH=src uv run python scripts/bench_codex_gateway.py --old-ref a496317 --clients 10
"""

import argparse
import asyncio
import os
import resource
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENT = ('event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
         '"sequence_number":%d,"item_id":"msg_68d0c0ffee0000000000000000000000",'
         '"output_index":1,"content_index":0,"delta":" token","logprobs":[],'
         '"obfuscation":"aBcDeFgH"}\n\n')


# --- child: one gateway process --------------------------------------------

def serve(args):
    sys.path.insert(0, args.module_dir)
    module = __import__(args.module)
    from claude_switcher import codex_tls

    paths = codex_tls.CertPaths(*(Path(args.cert_dir) / name
                                  for name in ('ca.pem', 'cert.pem', 'key.pem')),
                                regenerated=False, previous_fingerprint=None)
    server = module.Gateway('127.0.0.1', 0, lambda: ('bench@test.com', 'bench-token'),
                            lambda email: False, upstream=f'https://127.0.0.1:{args.upstream_port}',
                            ssl_context=codex_tls.ssl_context(paths))
    server.start()
    peak = [threading.active_count()]
    sampling = threading.Event()

    def sample():   # one extra thread; it wakes 10 times a second
        while not sampling.wait(0.1):
            peak[0] = max(peak[0], threading.active_count() - 1)

    threading.Thread(target=sample, daemon=True).start()
    print(f'PORT {server.port}', flush=True)
    for line in sys.stdin:
        command = line.strip()
        if command == 'cpu':
            usage = resource.getrusage(resource.RUSAGE_SELF)
            print(f'CPU {usage.ru_utime + usage.ru_stime} {peak[0]}', flush=True)
            peak[0] = threading.active_count() - 1
        elif command == 'stop':
            sampling.set()
            server.stop()
            return


# --- parent: upstream, clients, measurement ---------------------------------

async def upstream_handler(reader, writer, events, pace):
    try:
        while (line := await reader.readline()) not in (b'\r\n', b''):
            if line.lower().startswith(b'content-length:'):
                length = int(line.split(b':', 1)[1])
        await reader.readexactly(length)
        if not events:   # a small JSON call, such as a model list or plugin request
            body = b'{"models":[{"slug":"gpt-5","display_name":"GPT-5"}]}'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                         b'Content-Length: %d\r\n\r\n%b' % (len(body), body))
            await writer.drain()
            return
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n')
        for index in range(events):
            data = (EVENT % index).encode()
            writer.write(b'%x\r\n%b\r\n' % (len(data), data))
            if pace:
                await writer.drain()
                await asyncio.sleep(pace)
            elif index % 32 == 0:
                await writer.drain()
        writer.write(b'0\r\n\r\n')
        await writer.drain()
    finally:
        writer.close()


async def client(port, context, events, requests=1):
    reader, writer = await asyncio.open_connection('127.0.0.1', port, ssl=context,
                                                   server_hostname='127.0.0.1')
    body = b'{"model":"gpt-5","input":"bench"}'
    for _ in range(requests):   # sequential requests on one keep-alive connection
        writer.write(b'POST /backend-api/codex/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                     b'Authorization: Bearer stale\r\nContent-Type: application/json\r\n'
                     b'Content-Length: %d\r\n\r\n%b' % (len(body), body))
        await writer.drain()
        received = bytearray()
        while not received.endswith(b'\r\n0\r\n\r\n'):
            data = await reader.read(262144)
            if not data:
                raise RuntimeError('gateway closed the stream early')
            received += data
        if events:
            seen = received.count(b'event: response.output_text.delta')
            if seen != events:
                raise RuntimeError(f'expected {events} events, got {seen}')
        elif b'"slug":"gpt-5"' not in received:
            raise RuntimeError('small response was not relayed')
    writer.close()


def run_one(label, module_dir, module, cert_dir, upstream_port, upstream_ca, client_ca,
            clients, events, rounds, requests=1):
    env = dict(os.environ, SSL_CERT_FILE=str(upstream_ca),
               PYTHONPATH=os.pathsep.join([str(ROOT / 'src'), os.environ.get('PYTHONPATH', '')]))
    child = subprocess.Popen(
        [sys.executable, __file__, '--serve', '--module-dir', str(module_dir), '--module', module,
         '--cert-dir', str(cert_dir), '--upstream-port', str(upstream_port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    port = int(child.stdout.readline().split()[1])

    def cpu():
        child.stdin.write('cpu\n')
        child.stdin.flush()
        _, seconds, threads = child.stdout.readline().split()
        return float(seconds), int(threads)

    context = ssl.create_default_context(cafile=str(client_ca))

    async def batch(count):
        await asyncio.gather(*(client(port, context, events, requests) for _ in range(count)))

    asyncio.run(batch(1))   # warm-up: imports, first handshake
    results = []
    for _ in range(rounds):
        before, _ = cpu()
        started = time.perf_counter()
        asyncio.run(batch(clients))
        wall = time.perf_counter() - started
        after, threads = cpu()
        results.append((after - before, wall, threads))
    child.stdin.write('stop\n')
    child.stdin.flush()
    child.wait(10)
    return label, results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--module-dir', help=argparse.SUPPRESS)
    parser.add_argument('--module', help=argparse.SUPPRESS)
    parser.add_argument('--cert-dir', help=argparse.SUPPRESS)
    parser.add_argument('--upstream-port', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--old-ref', default='a496317',
                        help='git revision holding the old gateway (default: pre-asyncio main)')
    parser.add_argument('--clients', type=int, default=10)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.serve:
        serve(args)
        return

    sys.path.insert(0, str(ROOT / 'src'))
    from claude_switcher import codex_tls

    workloads = [   # (title, events per stream, seconds between events, requests per client)
        ('burst: 5000 events/stream, sent as fast as possible', 5000, 0, 1),
        ('paced: 1000 events/stream, 5 ms apart (~200 events/s)', 1000, 0.005, 1),
        ('calls: 20 small JSON requests per client on one keep-alive connection', 0, 0, 20),
    ]
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        old_source = subprocess.run(
            ['git', 'show', f'{args.old_ref}:src/claude_switcher/codex_gateway.py'],
            cwd=ROOT, check=True, capture_output=True, text=True).stdout
        (temp / 'gateway_old.py').write_text(old_source)
        (temp / 'gateway_new.py').write_text(
            (ROOT / 'src/claude_switcher/codex_gateway.py').read_text())
        gateway_paths = codex_tls.ensure_certificate(directory=temp / 'gateway')
        upstream_paths = codex_tls.ensure_certificate(directory=temp / 'upstream')

        for title, events, pace, requests in workloads:
            loop = asyncio.new_event_loop()
            upstream_context = codex_tls.ssl_context(upstream_paths)
            server = loop.run_until_complete(asyncio.start_server(
                lambda r, w: upstream_handler(r, w, events, pace), '127.0.0.1', 0,
                ssl=upstream_context))
            upstream_port = server.sockets[0].getsockname()[1]
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            print(f'\n{title}; {args.clients} concurrent clients; {args.rounds} rounds')
            print(f'{"impl":<5} {"gateway CPU s":>14} {"wall s":>8} {"CPU/wall":>9} '
                  f'{"CPU ms/client":>14} {"peak threads":>13}')
            for label, module in (('old', 'gateway_old'), ('new', 'gateway_new')):
                _, results = run_one(label, temp, module, temp / 'gateway', upstream_port,
                                     upstream_paths.ca, gateway_paths.ca,
                                     args.clients, events, args.rounds, requests)
                cpu = sorted(r[0] for r in results)[len(results) // 2]
                wall = sorted(r[1] for r in results)[len(results) // 2]
                threads = max(r[2] for r in results)
                print(f'{label:<5} {cpu:>14.3f} {wall:>8.3f} {cpu / wall:>8.0%} '
                      f'{1000 * cpu / args.clients:>14.1f} {threads:>13}')
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)


if __name__ == '__main__':
    main()
