"""Private loopback SSH forward for the remote endpoint test lab (no voice server)."""
import argparse
from pathlib import Path
import select
import socketserver

import paramiko
from deploy_ssh import read_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-port', type=int, default=18876)
    parser.add_argument('--remote-port', type=int, default=8876)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent.parent / '.env.deploy')
    args = parser.parse_args()
    config = read_config(args.config)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(config['DEPLOY_HOST'], port=int(config.get('DEPLOY_PORT', '22')),
                   username=config.get('DEPLOY_USER', 'root'), password=config['DEPLOY_PASSWORD'],
                   look_for_keys=False, allow_agent=False, timeout=15, auth_timeout=15)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            channel = client.get_transport().open_channel(
                'direct-tcpip', ('127.0.0.1', args.remote_port), self.request.getpeername())
            try:
                while True:
                    ready, _, _ = select.select([self.request, channel], [], [], 1)
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (channel if source is self.request else self.request).sendall(data)
            finally:
                channel.close()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        with Server(('127.0.0.1', args.local_port), Handler) as server:
            print('Private remote lab forward ready (loopback only)', flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == '__main__':
    main()
