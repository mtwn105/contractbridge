"""Prepare an ignored BuildKit secret; never print or bake an upload token into images."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    token = os.getenv('SENTRY_AUTH_TOKEN', '')
    env = ROOT / '.env'
    if not token and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith('SENTRY_AUTH_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('\"\'')
                break
    if '\n' in token or '\r' in token:
        raise ValueError('Sentry upload token must be a single line')
    target = ROOT / '.local/sentry-auth-token'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(mode=0o600, exist_ok=True)
    target.chmod(0o600)
    target.write_text(token)
    print('Sentry source-map upload token:', 'configured' if token else 'not configured')


if __name__ == '__main__':
    main()
