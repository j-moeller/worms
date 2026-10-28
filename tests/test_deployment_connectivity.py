"""Deployment regressions found by the read-only VM audit; isolated Linux only."""
import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from conftest import REPO, require, require_root

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux mount namespace / deployment checks')
APP = REPO / 'ansible/roles/applications/django_app'
MONITOR = 'ansible/roles/applications/network_monitor'


def run(*args, **kwargs):
    p = subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


@pytest.fixture(scope='module')
def deployment(tmp_path_factory):
    require('openssl', 'ansible-playbook', 'update-ca-certificates', 'unshare', 'mount')
    require_root()
    root = tmp_path_factory.mktemp('deployment-connectivity')
    ca = root / 'cluster.crt'
    key = root / 'cluster.key'
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=Fixture cluster CA', '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
        '-keyout', str(key), '-out', str(ca))
    groups = {name: yaml.safe_load((REPO / f'ansible/group_vars/{name}.yml').read_text())
              for name in ('dashboard', 'orchestrator')}
    assert 'requests' in groups['dashboard']['django_extra_pip_packages']
    hosts = root / 'hosts'
    hosts.write_text('127.0.0.1 localhost\n')
    localcerts, certs, hooks, empty = (root / name for name in ('local-certs', 'certs', 'hooks', 'empty'))
    for directory in (localcerts, certs, hooks, empty):
        directory.mkdir()
    dest = localcerts / 'worms-proxmox-ca.crt'
    tasks = yaml.safe_load((APP / 'tasks/connectivity.yml').read_text())
    tasks[0]['ansible.builtin.blockinfile']['path'] = str(hosts)
    # Same trust-store operation, confined to this fixture's directories.
    tasks[1]['block'][2]['ansible.builtin.command'] = {
        'argv': ['update-ca-certificates', '--certsconf', '/dev/null', '--certsdir', str(empty),
                 '--localcertsdir', str(localcerts), '--etccertsdir', str(certs), '--hooksdir', str(hooks)]}
    play = []
    handlers = yaml.safe_load((APP / 'handlers/main.yml').read_text())
    # Observe the production notification/listen/loop graph without systemd.
    for handler in handlers:
        service = handler.pop('ansible.builtin.systemd')
        handler['ansible.builtin.debug'] = {'msg': 'restart-fixture ' + service.get('name', 'systemd')}
    for name, variables in groups.items():
        play.append({'hosts': 'localhost', 'gather_facts': False,
                     'vars': {**variables, 'infrastructure_ips': {'orchestrator': '127.0.0.1'},
                              'proxmox': {'host': '127.0.0.1', 'node': 'worms-pve'},
                              'django_proxmox_ca_source': str(ca), 'django_proxmox_ca_dest': str(dest)},
                     'tasks': tasks, 'handlers': handlers})
    playfile, inventory = root / 'play.yml', root / 'inventory.yml'
    playfile.write_text(yaml.safe_dump(play))
    inventory.write_text(yaml.safe_dump({'all': {'children': {'proxmox_host': {
        'hosts': {'localhost': {'ansible_connection': 'local'}}}}}}))
    cfg = root / 'ansible.cfg'
    cfg.write_text('[defaults]\n')
    env = dict(os.environ, ANSIBLE_CONFIG=str(cfg), ANSIBLE_NOCOLOR='1')
    # Role defaults normally supply the optional dashboard CA setting.
    for p in play:
        p['vars'] = {**yaml.safe_load((APP / 'defaults/main.yml').read_text()), **p['vars']}
    playfile.write_text(yaml.safe_dump(play))
    command = ('ansible-playbook', '-i', str(inventory), str(playfile))
    first = run(*command, env=env)
    assert 'restart-fixture orchestrator-conduct-rounds' in first
    assert 'restart-fixture orchestrator-process-attempts' in first
    again = run(*command, env=env)
    assert 'changed=0' in again and 'failed=0' in again, again
    assert dest.read_bytes() == ca.read_bytes()
    assert '127.0.0.1 orchestrator.worms.internal' in hosts.read_text()
    assert '127.0.0.1 worms-pve' in hosts.read_text()
    return root, hosts, certs / 'ca-certificates.crt', groups, ca, key


@pytest.mark.parametrize('variant', ['dashboard', 'orchestrator'])
def test_deployed_clients_verify_private_ca_and_hostname(deployment, variant):
    root, hosts, bundle, groups, ca, ca_key = deployment
    config = groups[variant]['django_extra_env']
    name = 'orchestrator.worms.internal' if variant == 'dashboard' else 'worms-pve'
    # Render the actual deployment URL/name (only the fixture port differs).
    if variant == 'dashboard':
        assert config['ORCHESTRATOR_URL'] == 'https://' + name
    else:
        assert config['PROXMOX_HOST'] == '{{ proxmox.node }}'
    cert, key, csr, ext = (root / f'{variant}.{suffix}' for suffix in ('crt', 'key', 'csr', 'ext'))
    ext.write_text(f'subjectAltName=DNS:{name}\nbasicConstraints=CA:FALSE\n')
    run('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + name,
        '-keyout', str(key), '-out', str(csr))
    run('openssl', 'x509', '-req', '-in', str(csr), '-CA', str(ca), '-CAkey', str(ca_key),
        '-CAcreateserial', '-days', '1', '-extfile', str(ext), '-out', str(cert))
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({'data': {'version': 'fixture'}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PROXMOX_', 'ORCHESTRATOR_', 'DB_', 'DJANGO_'))
           and k not in ('REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE')}
    port = server.server_port
    env.update(PRODUCTION='1', DJANGO_SETTINGS_MODULE='config.settings.' + variant,
               DJANGO_SECRET_KEY='fixture', DJANGO_ALLOWED_HOSTS=name,
               DB_NAME='unused', DB_HOST='unused', DB_USER='unused', DB_PASSWORD='unused',
               DB_MIGRATOR_USER='unused', DB_MIGRATOR_PASSWORD='unused',
               PROXMOX_HOST=name, PROXMOX_PORT=str(port), PROXMOX_USER='fixture@pve',
               PROXMOX_TOKEN_NAME='power', PROXMOX_TOKEN_VALUE='fixture',
               PROXMOX_READONLY_USER='fixture@pve', PROXMOX_READONLY_TOKEN_NAME='topology',
               PROXMOX_READONLY_TOKEN_VALUE='fixture', SSL_CERT_FILE=str(bundle),
               ORCHESTRATOR_URL=f'https://{name}:{port}', ORCHESTRATOR_API_TOKEN='fixture')
    client = ("from dashboard.orchestrator_client import post; assert post('/fixture/') == {'accepted': True}"
              if variant == 'dashboard' else
              "from orchestrator.proxmox_inventory import readonly_client; "
              "from orchestrator.rounds.power import ProxmoxPower; "
              "assert readonly_client().get('version')['version']=='fixture'; "
              "assert ProxmoxPower().api().version.get()['version']=='fixture'")
    child = """import subprocess,sys,os
subprocess.run(['mount','--bind',sys.argv[1],'/etc/hosts'],check=True)
os.chdir(sys.argv[2]); sys.path.insert(0,sys.argv[2])
exec(sys.argv[3])
"""
    try:
        # Same correctly resolved hostname, but without the fixture CA:
        # failure must come from chain verification, before any HTTP action.
        untrusted_env = dict(env, SSL_CERT_FILE=ssl.get_default_verify_paths().cafile)
        untrusted = subprocess.run(
            ['unshare', '--mount', '--propagation', 'private', sys.executable, '-c', child,
             str(hosts), str(REPO / 'projects/dashboard'), client],
            env=untrusted_env, capture_output=True, text=True, timeout=15)
        assert untrusted.returncode != 0 and 'CERTIFICATE_VERIFY_FAILED' in untrusted.stderr
        assert not seen
        run('unshare', '--mount', '--propagation', 'private', sys.executable, '-c', child,
            str(hosts), str(REPO / 'projects/dashboard'), client, env=env)
        assert seen
        # A trusted chain must still reject the IP, absent from the certificate.
        import requests
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(f'https://127.0.0.1:{port}/', verify=str(bundle), timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_zeek_rotation_uses_one_writable_mount(render, tmp_path):
    require_root()
    require('unshare', 'mount')
    data = tmp_path / 'zeek'
    (data / 'logs').mkdir(parents=True)
    (data / 'archive').mkdir()
    unit = render(MONITOR, 'zeek.service.j2', network_monitor_data_dir=str(data),
                  network_monitor_capture_iface='fixture0')
    writable = next(line.split('=', 1)[1].split() for line in unit.splitlines()
                    if line.startswith('ReadWritePaths='))
    child = """import sys,subprocess,pathlib,errno
root=pathlib.Path(sys.argv[1])
for path in sys.argv[2:]:subprocess.run(['mount','--bind',path,path],check=True)
for i in range(5):
 source=root/'logs'/'stats.log'; target=root/'archive'/f'stats.{i}.log'
 source.write_text(str(i));source.rename(target)
 assert target.read_text()==str(i)
"""
    run('unshare', '--mount', '--propagation', 'private', sys.executable, '-c', child,
        str(data), *writable)
    # Reproduce the old sandbox exactly: distinct bind mounts cause EXDEV.
    old = subprocess.run(['unshare', '--mount', '--propagation', 'private', sys.executable, '-c', child,
                          str(data), str(data / 'logs'), str(data / 'archive')],
                         capture_output=True, text=True, timeout=10)
    assert old.returncode != 0 and 'Invalid cross-device link' in old.stderr, old.stderr
