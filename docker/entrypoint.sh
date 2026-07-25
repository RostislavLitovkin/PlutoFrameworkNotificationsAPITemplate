#!/bin/sh
#
# Container entrypoint: bring the instance up to date, then hand over to the
# process named by CMD (gunicorn by default).
#
# Both steps are opt-out because they are wrong in some topologies:
#   RUN_MIGRATIONS=0     when several replicas start at once, or the platform
#                        already runs migrations as a separate release step —
#                        concurrent `migrate` calls race each other.
#   RUN_COLLECTSTATIC=0  when static files are served by a CDN or a sidecar and
#                        the container filesystem is read-only.
#
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "[entrypoint] applying database migrations"
    python manage.py migrate --noinput
fi

if [ "${RUN_COLLECTSTATIC:-1}" = "1" ]; then
    echo "[entrypoint] collecting static files"
    python manage.py collectstatic --noinput
fi

# exec replaces the shell, so gunicorn becomes PID 1 and receives SIGTERM directly
# from `docker stop`. Without it, shutdown would wait out the timeout and be killed.
echo "[entrypoint] starting: $*"
exec "$@"
