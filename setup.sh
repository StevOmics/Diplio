#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--install] [--admin] [--configure-media] [--fs-root] [--configure-tls] [--username NAME]

Options:
  --install           Guided first-time install: checks dependencies, sets the media root, generates
                      secrets, confirms the server address, creates the admin user, starts the
                      containers and verifies they're reachable. Safe to re-run.
  --admin             Create or update the admin user (prompts for a password)
  --configure-media   Prompt for and set FILESYSTEM_ROOT in .env, then create the directory
  --fs-root           Migrate FILESYSTEM_ROOT to a new host directory, walking through any
                      existing libraries so their paths can be remapped, copied, or dropped
  --configure-tls     Generate a self-signed TLS cert/key into ./certs for the nginx
                      reverse proxy, if one doesn't already exist
  --username NAME     Admin username (default: admin)
  -h, --help          Show this help message
EOF
}

INSTALL=false
ADMIN=false
CONFIGURE_MEDIA=false
FS_ROOT=false
CONFIGURE_TLS=false
USERNAME="admin"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install)
      INSTALL=true
      shift
      ;;
    --admin)
      ADMIN=true
      shift
      ;;
    --configure-media)
      CONFIGURE_MEDIA=true
      shift
      ;;
    --fs-root|--fs_root)
      FS_ROOT=true
      shift
      ;;
    --configure-tls)
      CONFIGURE_TLS=true
      shift
      ;;
    --username)
      [[ $# -ge 2 ]] || { echo "--username requires a value" >&2; exit 1; }
      USERNAME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$INSTALL" != true && "$ADMIN" != true && "$CONFIGURE_MEDIA" != true && "$FS_ROOT" != true && "$CONFIGURE_TLS" != true ]]; then
  # A fresh clone (no .env yet) means a first-time install; otherwise show usage.
  if [[ ! -f .env ]]; then
    INSTALL=true
  else
    usage
    exit 1
  fi
fi

configure_tls() {
  mkdir -p certs
  if [[ -f certs/server.crt && -f certs/server.key ]]; then
    echo "certs/server.crt and certs/server.key already exist - leaving them in place."
    echo "Delete both under ./certs to regenerate."
  else
    # $1 (set by --install) skips the prompt.
    TLS_HOST="${1:-}"
    if [[ -z "$TLS_HOST" ]]; then
      read -rp "Hostname or IP the browser will use to reach this server [localhost]: " TLS_HOST
      TLS_HOST="${TLS_HOST:-localhost}"
    fi
    if [[ "$TLS_HOST" =~ ^[0-9.]+$ || "$TLS_HOST" == *:* ]]; then
      TLS_SAN="IP:$TLS_HOST,DNS:localhost,IP:127.0.0.1"
    else
      TLS_SAN="DNS:$TLS_HOST,DNS:localhost,IP:127.0.0.1"
    fi

    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
      -keyout certs/server.key -out certs/server.crt \
      -subj "/CN=$TLS_HOST" \
      -addext "subjectAltName=$TLS_SAN" \
      2>/dev/null

    chmod 600 certs/server.key
    cp certs/server.crt certs/self-signed.crt
    cp certs/server.key certs/self-signed.key
    chmod 600 certs/self-signed.key
    echo "Generated a self-signed cert for '$TLS_HOST' at certs/server.crt / certs/server.key."
    echo "Kept a backup copy at certs/self-signed.crt / self-signed.key for Settings > TLS's \"revert to self-signed\"."
    echo "Browsers will show a trust warning for this cert - that's expected for a self-signed, internally-hosted server."
  fi
}

configure_media() {
  ENV_PRE_EXISTED=true
  if [[ ! -f .env ]]; then
    ENV_PRE_EXISTED=false
    cp .env.example .env
    echo "Created .env from .env.example"
  fi

  # Only treat FILESYSTEM_ROOT as a real prior choice if .env already existed
  # before this run - otherwise it's just .env.example's own placeholder,
  # which would wrongly skip the ./data/mediafiles detection below.
  EXISTING_FILESYSTEM_ROOT=""
  if [[ "$ENV_PRE_EXISTED" == true ]]; then
    EXISTING_FILESYSTEM_ROOT="$(grep -m1 '^FILESYSTEM_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
  fi

  if [[ -n "$EXISTING_FILESYSTEM_ROOT" ]]; then
    DEFAULT_FILESYSTEM_ROOT="$EXISTING_FILESYSTEM_ROOT"
  elif [[ -d ./data/mediafiles ]]; then
    # An existing local dev setup already has data here - default to it instead
    # of ./mnt_ro so a first run of --configure-media doesn't silently point
    # the stack at an empty directory and orphan whatever's already cataloged.
    DEFAULT_FILESYSTEM_ROOT="./data/mediafiles"
  else
    DEFAULT_FILESYSTEM_ROOT="./mnt_ro"
  fi

  echo "This becomes the root for both the media mount and the Settings browse-folder dialog - anything under it is reachable from the UI."
  echo "  ./mnt_ro (default) - a project-local folder meant to hold read-only mounts, nothing else on the host is exposed"
  echo "  /mnt              - typical on Linux servers for external mounts, but exposes whatever else lives under /mnt"
  echo "  /                 - discouraged: exposes the entire host filesystem"
  FILESYSTEM_ROOT_INPUT="${MB_FILESYSTEM_ROOT:-}"
  if [[ -z "$FILESYSTEM_ROOT_INPUT" ]]; then
    read -rp "Host path to mount media from [$DEFAULT_FILESYSTEM_ROOT]: " FILESYSTEM_ROOT_INPUT || true
  fi
  FILESYSTEM_ROOT="${FILESYSTEM_ROOT_INPUT:-$DEFAULT_FILESYSTEM_ROOT}"

  # Create as the invoking user, before `docker compose up` ever touches the
  # mount - if Docker creates a missing bind-mount source itself, it does so
  # as root, which then breaks non-root access from the host.
  mkdir -p "$FILESYSTEM_ROOT"
  # Resolved *after* mkdir -p so this check works whether or not the path
  # already existed (cd+pwd -P needs a real, existing directory).
  RESOLVED_FILESYSTEM_ROOT="$(cd "$FILESYSTEM_ROOT" && pwd -P)"
  if [[ "$RESOLVED_FILESYSTEM_ROOT" == "/" ]]; then
    echo >&2
    echo "WARNING: '$FILESYSTEM_ROOT' resolves to '/', the root of the host filesystem." >&2
    echo "This exposes every file on the host to the web UI's storage-location browser - a security hole, not just a broad library." >&2
    read -rp "Type 'yes' to proceed anyway: " CONFIRM_ROOT
    if [[ "$CONFIRM_ROOT" != "yes" ]]; then
      echo "Aborted - .env was not changed." >&2
      exit 1
    fi
  fi

  grep -v '^FILESYSTEM_ROOT=' .env > .env.tmp || true
  mv .env.tmp .env
  echo "FILESYSTEM_ROOT=$FILESYSTEM_ROOT" >> .env

  echo "FILESYSTEM_ROOT set to '$FILESYSTEM_ROOT' in .env (directory ready)."
}

migrate_fs_root() {
  if [[ ! -f .env ]]; then
    echo "No .env found - run '$(basename "$0") --configure-media' first to set up FILESYSTEM_ROOT initially." >&2
    exit 1
  fi

  CURRENT_FS_ROOT="$(grep -m1 '^FILESYSTEM_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)"
  if [[ -z "$CURRENT_FS_ROOT" ]]; then
    echo "FILESYSTEM_ROOT isn't set yet - run '$(basename "$0") --configure-media' first." >&2
    exit 1
  fi
  if [[ ! -d "$CURRENT_FS_ROOT" ]]; then
    echo "Current FILESYSTEM_ROOT '$CURRENT_FS_ROOT' doesn't exist on disk - fix .env by hand before migrating." >&2
    exit 1
  fi
  OLD_ROOT="$(cd "$CURRENT_FS_ROOT" && pwd -P)"

  echo "Current FILESYSTEM_ROOT: $CURRENT_FS_ROOT (resolves to $OLD_ROOT)"
  echo "This moves the media mount to a new host directory. Any existing libraries under the old root will need to be dealt with individually below."
  read -rp "New host path to mount media from: " NEW_FS_ROOT_INPUT
  if [[ -z "$NEW_FS_ROOT_INPUT" ]]; then
    echo "A new path is required." >&2
    exit 1
  fi

  mkdir -p "$NEW_FS_ROOT_INPUT"
  NEW_ROOT="$(cd "$NEW_FS_ROOT_INPUT" && pwd -P)"

  if [[ "$NEW_ROOT" == "/" ]]; then
    echo >&2
    echo "WARNING: '$NEW_FS_ROOT_INPUT' resolves to '/', the root of the host filesystem." >&2
    echo "This exposes every file on the host to the web UI's storage-location browser - a security hole, not just a broad library." >&2
    read -rp "Type 'yes' to proceed anyway: " CONFIRM_ROOT
    if [[ "$CONFIRM_ROOT" != "yes" ]]; then
      echo "Aborted - .env was not changed." >&2
      exit 1
    fi
  fi

  if [[ "$NEW_ROOT" == "$OLD_ROOT" ]]; then
    echo "New root resolves to the same path as the current one ('$OLD_ROOT') - nothing to do."
    exit 0
  fi

  POSTGRES_USER_VAL="$(grep -m1 '^POSTGRES_USER=' .env | cut -d= -f2-)"
  POSTGRES_DB_VAL="$(grep -m1 '^POSTGRES_DB=' .env | cut -d= -f2-)"

  echo "Starting the db container..."
  docker compose up -d db >/dev/null </dev/null
  echo -n "Waiting for it to be healthy..."
  for _ in $(seq 1 30); do
    HEALTH="$(docker compose ps db --format '{{.Health}}' 2>/dev/null </dev/null || true)"
    [[ "$HEALTH" == "healthy" ]] && break
    echo -n "."
    sleep 2
  done
  echo

  psql_query() {
    docker compose exec -T db psql -U "$POSTGRES_USER_VAL" -d "$POSTGRES_DB_VAL" -t -A -F $'\t' -c "$1" </dev/null
  }
  psql_exec() {
    docker compose exec -T db psql -U "$POSTGRES_USER_VAL" -d "$POSTGRES_DB_VAL" -c "$1" >/dev/null </dev/null
  }

  # "Library" here matches catalog.scan_library's own definition: a local,
  # non-backup-target StorageLocation. Local archives (is_backup_target=true,
  # location_type=local) are rare/legacy (see docs/TODO.md) and aren't walked
  # here - their path needs fixing by hand on the Libraries page if affected.
  LIBRARIES="$(psql_query "SELECT id, name, path FROM storage_locations WHERE location_type='local' AND is_backup_target=false ORDER BY id;")"

  if [[ -n "$LIBRARIES" ]]; then
    LIBRARY_COUNT="$(echo "$LIBRARIES" | wc -l)"
    echo
    echo "Found $LIBRARY_COUNT local librar$([[ "$LIBRARY_COUNT" == 1 ]] && echo y || echo ies):"
    # Shown as absolute host paths, not the container-internal /mnt/... path
    # stored in the database - "/mnt/Music" alone doesn't say where on the
    # host that data actually lives, which is exactly the ambiguity a
    # migration prompt can't afford.
    echo "$LIBRARIES" | while IFS=$'\t' read -r ID NAME LPATH; do
      echo "  - [$ID] $NAME ($OLD_ROOT/${LPATH#/mnt/})"
    done
    echo
    echo "WARNING: changing FILESYSTEM_ROOT can leave a library's path pointing at nothing (inconsistent state) unless it's dealt with below. Nothing here deletes local files or cloud backups - stopping tracking just excludes a library from future scans (it stays visible, untracked, on the Libraries page) so its cloud backups remain restorable." >&2
    echo

    LIBRARY_NUM=0
    while IFS=$'\t' read -r ID NAME LPATH <&3; do
      [[ -z "$ID" ]] && continue
      LIBRARY_NUM=$((LIBRARY_NUM + 1))
      REL_PATH="${LPATH#/mnt/}"
      OLD_HOST_PATH="$OLD_ROOT/$REL_PATH"
      echo "Library $LIBRARY_NUM of $LIBRARY_COUNT: \"$NAME\" ($OLD_HOST_PATH)"
      echo "  [1] Select a folder in the new location"
      echo "  [2] Copy local data into the new location"
      echo "  [3] Create an empty folder in the new location (existing files become \"deleted locally\" - still restorable from backup)"
      echo "  [4] Stop tracking (stays listed as untracked on the Libraries page; cloud backups remain restorable)"
      echo "  [5] Skip for now (path may not resolve under the new root until fixed manually)"
      read -rp "Choice [5]: " CHOICE
      CHOICE="${CHOICE:-5}"

      case "$CHOICE" in
        4)
          read -rp "Type 'yes' to confirm: \"$NAME\" stops being scanned, but its catalog entries and cloud backups are NOT deleted - it'll show up under Untracked Libraries: " CONFIRM
          if [[ "$CONFIRM" == "yes" ]]; then
            psql_exec "UPDATE storage_locations SET untracked=true WHERE id=$ID;"
            psql_exec "UPDATE media_files SET is_missing=true WHERE storage_location_id=$ID;"
            echo "Stopped tracking \"$NAME\" - it's still listed (untracked) on the Libraries page, and its cloud backups are restorable from the Catalog page."
          else
            echo "Left \"$NAME\" as-is."
          fi
          ;;
        1)
          BASENAME="$(basename "$REL_PATH")"
          # Only the new root's top-level folders are offered as a picker - a
          # full recursive name search (an earlier version of this script) is
          # far too noisy on a real disk (a "Music" search turned up 15 hits
          # buried in AppData/Minecraft/backup folders). A top-level exact
          # name match still gets pre-selected as the default, covering the
          # common case (the folder just moved to a different mount point).
          TOP_LEVEL=()
          while IFS= read -r line; do
            [[ -n "$line" ]] && TOP_LEVEL+=("$line")
          done < <(find "$NEW_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)

          DEFAULT_NUM=""
          declare -A MENU=()
          echo "Folders under the new root ($NEW_ROOT):"
          NUM=1
          for d in "${TOP_LEVEL[@]}"; do
            MARKER=""
            if [[ "$d" == "$BASENAME" ]]; then
              MARKER="  <- matches \"$BASENAME\""
              DEFAULT_NUM="$NUM"
            fi
            echo "  [$NUM] $d$MARKER"
            MENU["$NUM"]="$d"
            NUM=$((NUM + 1))
          done
          echo "  [o] Enter your own path (relative to the new root, or an absolute path under it)"
          echo "  [Enter] Leave unmapped for now"

          read -rp "Choice${DEFAULT_NUM:+ [$DEFAULT_NUM]}: " PICK
          PICK="${PICK:-$DEFAULT_NUM}"

          if [[ -z "$PICK" ]]; then
            echo "Left unmapped - skipped \"$NAME\"."
            echo
            unset MENU
            continue
          elif [[ "$PICK" == "o" ]]; then
            read -rp "Path (relative to $NEW_ROOT, or an absolute path under it): " CUSTOM
            if [[ -z "$CUSTOM" ]]; then
              echo "No path given - skipped \"$NAME\"."
              echo
              unset MENU
              continue
            fi
            if [[ "$CUSTOM" == /* ]]; then
              case "$CUSTOM" in
                "$NEW_ROOT"/*) NEW_REL="${CUSTOM#"$NEW_ROOT"/}" ;;
                "$NEW_ROOT") NEW_REL="" ;;
                *)
                  echo "\"$CUSTOM\" is outside the new root ($NEW_ROOT), so it won't be reachable from the container - use --add-fs for a folder that has to live outside the root."
                  echo "Skipped \"$NAME\"."
                  echo
                  unset MENU
                  continue
                  ;;
              esac
            else
              NEW_REL="$CUSTOM"
            fi
          elif [[ "$PICK" =~ ^[0-9]+$ && -n "${MENU[$PICK]:-}" ]]; then
            NEW_REL="${MENU[$PICK]}"
          else
            echo "Not a valid choice - skipped \"$NAME\"."
            echo
            unset MENU
            continue
          fi
          unset MENU

          NEW_HOST_PATH="$NEW_ROOT/$NEW_REL"
          if [[ ! -d "$NEW_HOST_PATH" ]]; then
            echo "\"$NEW_HOST_PATH\" doesn't exist - skipped \"$NAME\"."
            echo
            continue
          fi

          OLD_HOST_PATH="$OLD_ROOT/$REL_PATH"
          OLD_COUNT=0
          if [[ -d "$OLD_HOST_PATH" ]]; then
            OLD_COUNT="$(find "$OLD_HOST_PATH" -type f 2>/dev/null | wc -l)"
          fi
          NEW_COUNT="$(find "$NEW_HOST_PATH" -type f 2>/dev/null | wc -l)"
          echo "Old location: $OLD_COUNT file(s). New location: $NEW_COUNT file(s)."

          MATCH_OK=true
          if [[ "$OLD_COUNT" -gt 0 ]]; then
            DIFF=$(( OLD_COUNT > NEW_COUNT ? OLD_COUNT - NEW_COUNT : NEW_COUNT - OLD_COUNT ))
            DIFF_PCT=$(( DIFF * 100 / OLD_COUNT ))
            [[ "$DIFF_PCT" -gt 5 ]] && MATCH_OK=false
          fi

          if [[ "$MATCH_OK" == false ]]; then
            echo "WARNING: file counts differ significantly between the old and new location - this may not be the same data."
            read -rp "Type 'yes' to use this path anyway: " CONFIRM
            if [[ "$CONFIRM" != "yes" ]]; then
              echo "Skipped \"$NAME\"."
              echo
              continue
            fi
          fi

          # Rewrite existing MediaFile rows' path prefix in place (same id,
          # so any BackupRecord stays attached to the right file) instead of
          # leaving them stale - the next scan would otherwise re-add every
          # file as a new row under the new path and flag the old row (and
          # its backup history) missing, silently duplicating the catalog.
          psql_exec "UPDATE media_files SET path='/mnt/$NEW_REL' || substring(path from ${#LPATH} + 1) WHERE storage_location_id=$ID AND starts_with(path, '$LPATH');"
          psql_exec "UPDATE storage_locations SET path='/mnt/$NEW_REL' WHERE id=$ID;"
          echo "Updated \"$NAME\" to /mnt/$NEW_REL"
          ;;
        2)
          read -rp "Path relative to the new root to copy into [$REL_PATH]: " NEW_REL
          NEW_REL="${NEW_REL:-$REL_PATH}"
          OLD_HOST_PATH="$OLD_ROOT/$REL_PATH"
          NEW_HOST_PATH="$NEW_ROOT/$NEW_REL"

          if [[ ! -d "$OLD_HOST_PATH" ]]; then
            echo "\"$OLD_HOST_PATH\" doesn't exist - nothing to copy, skipped \"$NAME\"."
            echo
            continue
          fi

          mkdir -p "$NEW_HOST_PATH"
          echo "Copying $OLD_HOST_PATH -> $NEW_HOST_PATH (this can take a while for a large library)..."
          if command -v rsync >/dev/null 2>&1; then
            rsync -a --info=progress2 "$OLD_HOST_PATH"/ "$NEW_HOST_PATH"/
          else
            cp -a "$OLD_HOST_PATH"/. "$NEW_HOST_PATH"/
          fi

          OLD_COUNT="$(find "$OLD_HOST_PATH" -type f 2>/dev/null | wc -l)"
          NEW_COUNT="$(find "$NEW_HOST_PATH" -type f 2>/dev/null | wc -l)"
          if [[ "$OLD_COUNT" != "$NEW_COUNT" ]]; then
            echo "WARNING: copy finished but file counts differ (old: $OLD_COUNT, new: $NEW_COUNT)."
            read -rp "Type 'yes' to point \"$NAME\" at the new path anyway: " CONFIRM
            if [[ "$CONFIRM" != "yes" ]]; then
              echo "Path left unchanged for \"$NAME\" - the copy is at $NEW_HOST_PATH if you want to fix it up and point it there manually later."
              echo
              continue
            fi
          fi
          psql_exec "UPDATE media_files SET path='/mnt/$NEW_REL' || substring(path from ${#LPATH} + 1) WHERE storage_location_id=$ID AND starts_with(path, '$LPATH');"
          psql_exec "UPDATE storage_locations SET path='/mnt/$NEW_REL' WHERE id=$ID;"
          echo "Copied and updated \"$NAME\" to /mnt/$NEW_REL"
          ;;
        3)
          # No data to bring over (that's the point - see --fs-root's option 2
          # for when it does exist) - just gives the library somewhere to live
          # under the new root. The next scan (catalog.py:_scan_location) will
          # naturally mark every one of its existing MediaFile rows is_missing,
          # since the root now resolves but is empty - no separate DB update
          # needed here for that part.
          read -rp "Path relative to the new root to create [$REL_PATH]: " NEW_REL
          NEW_REL="${NEW_REL:-$REL_PATH}"
          NEW_HOST_PATH="$NEW_ROOT/$NEW_REL"
          mkdir -p "$NEW_HOST_PATH"
          psql_exec "UPDATE storage_locations SET path='/mnt/$NEW_REL' WHERE id=$ID;"
          echo "Created $NEW_HOST_PATH and pointed \"$NAME\" at it. Its existing files will show as \"deleted locally\" (still restorable) after the next rescan."
          ;;
        *)
          echo "Skipped \"$NAME\" - its path may not resolve under the new root until fixed manually on the Libraries page."
          ;;
      esac
      echo
    done 3<<< "$LIBRARIES"
  fi

  grep -v '^FILESYSTEM_ROOT=' .env > .env.tmp || true
  mv .env.tmp .env
  echo "FILESYSTEM_ROOT=$NEW_FS_ROOT_INPUT" >> .env
  echo "FILESYSTEM_ROOT set to '$NEW_FS_ROOT_INPUT' in .env."

  echo "Recreating web and worker with the new mount..."
  docker compose up -d web worker </dev/null

  echo "Done. Run a rescan from the Catalog page (or Libraries) so remapped/copied libraries pick up their content again."
}

prompt_admin_password() {
  read -rsp "Password for '$USERNAME': " PASSWORD
  echo
  read -rsp "Confirm password: " PASSWORD_CONFIRM
  echo

  if [[ -z "$PASSWORD" ]]; then
    echo "Password cannot be empty" >&2
    exit 1
  fi

  if [[ "$PASSWORD" != "$PASSWORD_CONFIRM" ]]; then
    echo "Passwords do not match" >&2
    exit 1
  fi
}

create_admin() {
  [[ -n "${PASSWORD:-}" ]] || prompt_admin_password

  echo "Starting db and web containers..."
  docker compose up -d db web >/dev/null

  # Password is piped over stdin (never passed as an argv) so it never shows up
  # in `docker compose exec` process listings. web may still be booting (it runs
  # the startup migration), so retry for a while.
  local i
  for i in $(seq 1 30); do
    if printf '%s' "$PASSWORD" | docker compose exec -T web python -m app.manage create-admin \
        --username "$USERNAME" --password-stdin 2>/dev/null; then
      echo "Admin user '$USERNAME' is ready. Log in at https://${SERVER_ADDRESS:-$(env_get SERVER_ADDRESS)}:9443/login"
      return 0
    fi
    sleep 3
  done
  echo "Could not create the admin user - check 'docker compose logs web'." >&2
  return 1
}

# ---------------------------------------------------------------------------
# Guided install
# ---------------------------------------------------------------------------

env_get() {
  grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true
}

env_set() {
  # replace-or-append KEY=VALUE in .env
  grep -v "^$1=" .env > .env.tmp || true
  mv .env.tmp .env
  printf '%s=%s\n' "$1" "$2" >> .env
}

gen_secret() {
  openssl rand -hex "$1" 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex($1))"
}

# ask "prompt" default [ENV_OVERRIDE] - the env var (if set) answers non-interactively
ask() {
  local answer=""
  [[ -n "${3:-}" ]] && answer="${!3:-}"
  if [[ -z "$answer" ]]; then
    read -rp "$1 [$2]: " answer || true
  fi
  printf '%s' "${answer:-$2}"
}

ok()   { echo "  [ ok ] $*"; }
warn() { echo "  [warn] $*"; }
bad()  { echo "  [FAIL] $*"; }
step() { echo; echo "==> $*"; }

is_placeholder() {
  [[ -z "$1" || "$1" == changeme || "$1" == replace-with-* ]]
}

check_dependencies() {
  step "1/8 Checking dependencies"
  local failed=false cmd
  for cmd in docker openssl curl; do
    if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; else bad "$cmd not found - install it with your package manager"; failed=true; fi
  done
  if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
      ok "docker compose ($(docker compose version --short 2>/dev/null))"
    else
      bad "docker compose v2 plugin missing - see https://docs.docker.com/compose/install/"; failed=true
    fi
    if docker info >/dev/null 2>&1; then
      ok "docker daemon reachable"
    else
      bad "cannot talk to the docker daemon - is it running, and is your user in the 'docker' group? (sudo usermod -aG docker \$USER, then log in again)"; failed=true
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then ok "python3"; else warn "python3 not found (optional)"; fi

  if command -v ss >/dev/null 2>&1; then
    local port running
    # Ports already held by this project's own containers are fine on a re-run.
    running="$(docker compose ps -q 2>/dev/null | wc -l || true)"
    if [[ "$running" == 0 ]]; then
      for port in 80 9443 5555 15672 5672; do
        if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
          bad "port $port is already in use"; failed=true
        fi
      done
    fi
  fi

  if [[ "$failed" == true ]]; then
    echo >&2
    echo "Fix the items above and re-run ./setup.sh --install. Nothing has been changed." >&2
    exit 1
  fi
}

configure_secrets() {
  step "3/8 Configuring keys and passwords"
  local key val db_pw
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "  Created .env from .env.example"
  fi
  chmod 600 .env

  db_pw="$(env_get POSTGRES_PASSWORD)"
  if is_placeholder "$db_pw"; then
    db_pw="$(ask "Postgres password (Enter to generate a random one)" "$(gen_secret 16)" MB_POSTGRES_PASSWORD)"
    env_set POSTGRES_PASSWORD "$db_pw"
    ok "POSTGRES_PASSWORD set"
  else
    ok "POSTGRES_PASSWORD kept (already set)"
  fi
  env_set DATABASE_URL "postgresql+psycopg://$(env_get POSTGRES_USER):${db_pw}@db:5432/$(env_get POSTGRES_DB)"

  for key in RABBITMQ_DEFAULT_PASS:16 SECRET_KEY:32 TERMINATOR_API_KEY:32; do
    val="$(env_get "${key%%:*}")"
    if is_placeholder "$val"; then
      env_set "${key%%:*}" "$(gen_secret "${key##*:}")"
      ok "${key%%:*} generated"
    else
      ok "${key%%:*} kept (already set)"
    fi
  done
}

confirm_address() {
  step "4/8 Server address"
  local detected
  detected="$(env_get SERVER_ADDRESS)"
  if [[ -z "$detected" ]]; then
    detected="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  fi
  detected="${detected:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  detected="${detected:-localhost}"
  echo "  The address (IP or hostname) other devices will use to reach this server. It goes into the TLS certificate."
  SERVER_ADDRESS="$(ask "Address" "$detected" MB_ADDRESS)"
  env_set SERVER_ADDRESS "$SERVER_ADDRESS"
  ok "using $SERVER_ADDRESS"
}

wait_healthy() {
  # wait_healthy service [seconds] - running, and healthy if it has a healthcheck
  local svc="$1" i state health
  for i in $(seq 1 "${2:-120}"); do
    state="$(docker compose ps "$svc" --format '{{.State}}' 2>/dev/null </dev/null || true)"
    health="$(docker compose ps "$svc" --format '{{.Health}}' 2>/dev/null </dev/null || true)"
    if [[ "$state" == running && ( -z "$health" || "$health" == healthy ) ]]; then return 0; fi
    sleep 1
  done
  return 1
}

verify_install() {
  step "7/8 Verifying the install"
  local failed=false svc url i okay
  for svc in db rabbitmq web worker nginx; do
    if wait_healthy "$svc" 120; then
      ok "$svc running"
    else
      bad "$svc is not running/healthy"; docker compose logs --tail 40 "$svc" 2>&1 | sed 's/^/        /' >&2; failed=true
    fi
  done

  for url in "https://$SERVER_ADDRESS:9443/health" "https://$SERVER_ADDRESS:9443/health/db" "https://$SERVER_ADDRESS:9443/login"; do
    okay=false
    for i in $(seq 1 20); do
      if curl -fsk --max-time 5 -o /dev/null "$url"; then okay=true; break; fi
      sleep 3
    done
    if [[ "$okay" == true ]]; then ok "reachable: $url"; else bad "not reachable: $url"; failed=true; fi
  done

  if [[ "$failed" == true ]]; then
    echo >&2
    echo "Install finished but verification failed - see the output above, or run 'docker compose logs <service>'." >&2
    exit 1
  fi
}

report_install() {
  step "8/8 Done"
  cat <<EOF
  MediaBridge is running.

  Web UI:        https://$SERVER_ADDRESS:9443   (login: $USERNAME)
  Flower:        http://$SERVER_ADDRESS:5555
  RabbitMQ:      http://$SERVER_ADDRESS:15672
  Media root:    $(env_get FILESYSTEM_ROOT)  (shown in the containers as /mnt)
  Secrets:       .env (chmod 600) - back it up; losing it locks you out of the database

  Your browser will warn about the self-signed certificate - expected for an internal server.

  Next steps:
    1. Log in and open the Libraries page; add the folders under your media root
       (subfolders appear as one-click suggestions) and run a scan.
    2. Settings: connect Jellyfin (optional), add a Google Cloud Storage archive,
       and set a backup passphrase - store that passphrase somewhere safe.
    3. Point each library at an archive and run its first backup from Backups & Restores.
EOF
}

install_app() {
  check_dependencies

  step "2/8 Media folder"
  [[ -f .env ]] || cp .env.example .env
  configure_media

  configure_secrets
  confirm_address

  step "5/8 Admin account"
  USERNAME="$(ask "Admin username" "$USERNAME" MB_USERNAME)"
  if [[ -n "${MB_PASSWORD:-}" ]]; then PASSWORD="$MB_PASSWORD"; else prompt_admin_password; fi

  step "6/8 Installing (TLS certificate, images, containers) - the first build can take several minutes"
  configure_tls "$SERVER_ADDRESS"
  # A slow first boot (e.g. rabbitmq missing its health window) makes `up` fail
  # while the service goes on to become healthy - so retry once before giving up.
  if ! docker compose up -d --build >/tmp/mediabridge-install.log 2>&1; then
    warn "first attempt failed (often a slow first boot) - retrying once"
    if ! docker compose up -d >>/tmp/mediabridge-install.log 2>&1; then
      bad "docker compose up failed - last lines:"; tail -n 40 /tmp/mediabridge-install.log >&2
      exit 1
    fi
  fi
  ok "containers started"
  if create_admin >/dev/null 2>&1; then ok "admin user '$USERNAME' ready"; else bad "admin user not created"; exit 1; fi

  verify_install
  report_install
}

# ---------------------------------------------------------------------------

if [[ "$INSTALL" == true ]]; then
  install_app
  exit 0
fi
[[ "$CONFIGURE_TLS" == true ]] && configure_tls
[[ "$CONFIGURE_MEDIA" == true ]] && configure_media
[[ "$FS_ROOT" == true ]] && migrate_fs_root
[[ "$ADMIN" == true ]] && create_admin
exit 0
