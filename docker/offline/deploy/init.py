"""Initialize a new deployment without copying existing credentials or runtime data."""
import os
from pathlib import Path
import secrets
import shutil
import sys

import yaml

root = Path('/deployment-output')
mode = sys.argv[1]
host_root = sys.argv[2]
if any(c in host_root for c in '\n\r$"\\'):
    raise SystemExit('Deployment path must not contain newline, $, quote or backslash')
if mode not in ('local', 'aio'):
    raise SystemExit('Mode must be local or aio')
if (root / '.env').exists():
    raise SystemExit('Already initialized; refusing to overwrite settings')
for name in ('config', 'data'):
    (root / name).mkdir(exist_ok=True)
config_path = root / 'config/config.yaml'
if config_path.exists() or (root / 'runtime.env').exists():
    raise SystemExit('Existing config/runtime.env found; refusing to overwrite')
config = yaml.safe_load(Path('/app/config.example.yaml').read_text())
config['models'] = config.get('models') or []
config['database'] = {'backend': 'sqlite', 'sqlite_dir': '/data/database'}
config.pop('checkpointer', None)
config['run_events']['backend'] = 'db'
if mode == 'aio':
    config['sandbox'] = {
        'use': 'deerflow.community.aio_sandbox:AioSandboxProvider',
        'image': 'deerflow-offline-sandbox:local',
        'container_prefix': 'deerflow-offline-sandbox',
    }
config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
extensions = root / 'config/extensions_config.json'
if not extensions.exists():
    extensions.write_text('{"mcpServers": {}}\n')
if not (root / 'skills').exists():
    shutil.copytree('/app/skills', root / 'skills')
(root / 'runtime.env').write_text(
    'LANGSMITH_TRACING=false\n'
    f'BETTER_AUTH_SECRET={secrets.token_hex(32)}\n'
    f'DEER_FLOW_INTERNAL_AUTH_TOKEN={secrets.token_hex(32)}\n'
    '# Add model credentials and runtime environment variables here.\n'
)
os.chmod(root / 'runtime.env', 0o600)
(root / '.env').write_text(f'DEPLOY_ROOT="{host_root}"\nSANDBOX_MODE={mode}\nPORT=2026\n')
print(f'Initialized {mode} deployment; configure config/config.yaml and runtime.env before starting.')
