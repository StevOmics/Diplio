#!/bin/bash
set -e

# MediaBridge dev convenience script
# Usage: ./dev.sh [command]

COMMAND="${1:-help}"

case "$COMMAND" in
  start)
    echo "🚀 Starting development environment..."
    docker compose up -d
    echo "✓ Services started. Web UI: https://localhost:9443"
    ;;

  stop)
    echo "⛔ Stopping services..."
    docker compose down
    echo "✓ Services stopped"
    ;;

  restart)
    echo "🔄 Rebuilding and restarting web service..."
    docker compose up -d --build web
    echo "✓ Web service restarted"
    ;;

  logs)
    echo "📋 Tailing web service logs (Ctrl+C to exit)..."
    docker compose logs -f web
    ;;

  shell)
    echo "🐚 Opening web container shell..."
    docker compose exec web bash
    ;;

  test)
    echo "🧪 Running tests..."
    docker compose exec web pytest -xvs
    ;;

  clean)
    echo "🧹 Cleaning up containers and volumes..."
    docker compose down -v
    echo "✓ Cleaned"
    ;;

  status)
    echo "📊 Service status:"
    docker compose ps
    ;;

  help|*)
    cat << 'EOF'
MediaBridge Development Script

Usage: ./dev.sh [command]

Commands:
  start       Start all services
  stop        Stop all services
  restart     Restart web service (fast refresh, no rebuild)
  logs        Tail web service logs
  shell       Open shell in web container
  test        Run pytest in web container
  clean       Stop services and remove volumes
  status      Show service status
  help        Show this help message

Examples:
  ./dev.sh start              # Start dev environment
  ./dev.sh restart            # Quick refresh after code changes
  ./dev.sh logs              # Watch web service logs

Notes:
  - Code changes need a rebuild: ./dev.sh restart (docker compose up -d --build web)
  - For database schema changes, you may need to run migrations manually
EOF
    ;;
esac
