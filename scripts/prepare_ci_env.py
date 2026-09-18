"""Create a disposable CI .env without production credentials or external calls."""
from pathlib import Path


def main() -> None:
    target = Path('.env')
    # Never overwrite an existing developer or deployment configuration.
    with target.open('x', encoding='utf-8') as handle:
        handle.write('''APP_ENV=local
APP_VERSION=ci-e2e
IMAGE_REGISTRY=ai-guide-ci
LOG_LEVEL=INFO
MYSQL_DATABASE=ai_guide
MYSQL_USER=ai_guide
MYSQL_PASSWORD=ci-placeholder-password
MYSQL_ROOT_PASSWORD=ci-placeholder-root-password
DATABASE_URL=mysql+pymysql://ai_guide:ci-placeholder-password@mysql:3306/ai_guide?charset=utf8mb4
REDIS_URL=redis://redis:6379/0
SESSION_SECRET=ci-placeholder-session-secret
SESSION_COOKIE_SECURE=false
AUTH_COOKIE_SECURE=false
PROVIDER_MODE=mock
EMAIL_BACKEND=console
EMAIL_DUMP_PATH=
PUBLIC_BASE_URL=http://127.0.0.1:8080
SITE_ADDRESS=:80
WEB_HTTP_PORT=8080
WEB_HTTPS_PORT=8443
DEMO_SESSION_RUN_LIMIT=50
DEMO_DAILY_RUN_LIMIT=200
DEMO_IP_DAILY_LIMIT=100
''')
    print('Created isolated fixture configuration; no values printed.')


if __name__ == '__main__':
    main()
