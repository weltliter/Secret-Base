#!/bin/sh
# ECS deployment and maintenance entry point.
set -eu
VAULT_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

usage() {
    cat <<'EOF'
Usage: sh manage.sh COMMAND [ARGS]
  deploy [--prepare-only]                 Prepare state and models, then build and start unless prepare-only
  start                                   Start the existing app and database containers
  stop                                    Stop the app and database
  ps                                      Show service status
  logs [OPTIONS] [SERVICE]                View service logs
  backup [--legacy]                       Create a database backup (legacy mode is for 1.0 only)
  restore /absolute/path --confirm-replace  Restore a .dump or legacy .tar.gz backup
  <compose-command> [ARGS]                Run other Compose commands using config.env
EOF
}

case "${1:-help}" in
    help|-h|--help) usage ;;
    deploy)
        shift
        . "$VAULT_PROJECT_ROOT/ops/deploy/common.sh"
        . "$VAULT_PROJECT_ROOT/ops/deploy/bootstrap.sh"
        deploy_vault "$@"
        ;;
    backup|restore)
        operation=$1
        shift
        . "$VAULT_PROJECT_ROOT/ops/deploy/common.sh"
        . "$VAULT_PROJECT_ROOT/ops/backup/maintenance.sh"
        case "$operation" in
            backup) backup_vault "$@" ;;
            restore) restore_vault "$@" ;;
        esac
        ;;
    *) exec python3 "$VAULT_PROJECT_ROOT/ops/config.py" compose "$@" ;;
esac
