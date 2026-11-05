#!/usr/bin/env bash
#
# Deploy the Moana Viewer (Pacific Ocean Portal) with Docker on this server.
#
#   ./deploy/deploy.sh            check, build, start, verify   (default)
#   ./deploy/deploy.sh deploy fix same, and make the data readable by the container
#   ./deploy/deploy.sh check      only check config and data; no Docker needed
#   ./deploy/deploy.sh status     container state and health
#   ./deploy/deploy.sh logs       follow the app's logs
#   ./deploy/deploy.sh restart    restart without rebuilding
#   ./deploy/deploy.sh admin      create a Django admin user (for /admin/)
#   ./deploy/deploy.sh down       stop and remove the container (the state volume stays)
#
# Run it from anywhere inside the copied project. Settings live in deploy/.env,
# created from deploy/.env.example on the first run.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
ENV_FILE="$DEPLOY_DIR/.env"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
APP_UID=1001   # the container's unprivileged user; it must be able to read the data

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B= G= Y= R= N=; fi
step() { echo; echo "${B}==> $*${N}"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
die()  { echo "  ${R}✗ $*${N}" >&2; exit 1; }

# --------------------------------------------------------------------------

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
  else
    die "Docker Compose not found (install the docker-compose-plugin package)"
  fi
}

check_tools() {
  step "Checking tools"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  docker info >/dev/null 2>&1 || die "cannot talk to Docker (is it running? is $USER in the docker group?)"
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  if docker compose version >/dev/null 2>&1; then ok "$(docker compose version | head -1)";
  elif command -v docker-compose >/dev/null 2>&1; then ok "$(docker-compose version | head -1)";
  else die "Docker Compose not found (install the docker-compose-plugin package)"; fi
  command -v curl >/dev/null 2>&1 || die "curl is not installed (needed for the checks after deploy)"
  ok "curl"
}

env_get() {  # value of KEY in deploy/.env, empty if unset
  { grep -E "^[[:space:]]*$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

env_set() {  # set KEY=VALUE in deploy/.env
  local key=$1 value=$2
  if grep -qE "^[[:space:]]*$key=" "$ENV_FILE"; then
    sed -i.bak -E "s|^[[:space:]]*$key=.*|$key=$value|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    echo "$key=$value" >> "$ENV_FILE"
  fi
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

check_env() {
  step "Checking settings ($ENV_FILE)"
  if [ ! -f "$ENV_FILE" ]; then
    cp "$DEPLOY_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    env_set DJANGO_SECRET_KEY "$(random_secret)"
    warn "created deploy/.env from the example, with a random DJANGO_SECRET_KEY"
    die "fill in CESIUM_ION_TOKEN, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS and DATA_PATH in deploy/.env, then run this again"
  fi
  chmod 600 "$ENV_FILE"

  if [ -z "$(env_get DJANGO_SECRET_KEY)" ]; then
    env_set DJANGO_SECRET_KEY "$(random_secret)"
    ok "generated a random DJANGO_SECRET_KEY"
  fi

  local missing=()
  for key in CESIUM_ION_TOKEN DJANGO_SECRET_KEY ALLOWED_HOSTS DATA_PATH; do
    [ -n "$(env_get "$key")" ] || missing+=("$key")
  done
  [ ${#missing[@]} -eq 0 ] || die "set these in deploy/.env: ${missing[*]}"
  ok "token, secret, host names and data folder are set"

  local base; base="$(env_get BASE_PATH)"
  case "$base" in ""|/*) ;; *) die "BASE_PATH must start with / (e.g. /moana) or be empty" ;; esac
  [ -n "$(env_get CSRF_TRUSTED_ORIGINS)" ] || warn "CSRF_TRUSTED_ORIGINS is empty: opening files will fail over HTTPS; set it to e.g. https://$(env_get ALLOWED_HOSTS | cut -d, -f1)"
  [ "$(env_get APP_BIND)" = "127.0.0.1" ] || [ -z "$(env_get APP_BIND)" ] || warn "APP_BIND is not 127.0.0.1: the app is reachable without nginx"
  [ "$(env_get BEHIND_PROXY)" != "false" ] || warn "BEHIND_PROXY is false: set it to true once nginx serves HTTPS"
  return 0
}

data_dir() {  # DATA_PATH as an absolute path; relative paths are relative to deploy/
  local p
  p="$(env_get DATA_PATH)"
  case "$p" in /*) ;; *) p="$DEPLOY_DIR/$p" ;; esac
  (cd "$p" 2>/dev/null && pwd) || echo "$p"
}

check_data() {
  local dir fix_perms=${1:-}
  dir="$(data_dir)"
  step "Checking data ($dir)"
  [ -d "$dir" ] || die "data folder not found: $dir (set DATA_PATH in deploy/.env)"
  local count
  count=$(find "$dir" -type f \( -iname '*.tif' -o -iname '*.tiff' -o -iname '*.pmtiles' -o -iname '*.geojson' \
    -o -iname '*.json' -o -iname '*.shp' -o -iname '*.zip' -o -iname '*.nc' -o -iname '*.nc4' -o -iname '*.las' \
    -o -iname '*.laz' -o -name '.zgroup' -o -name 'zarr.json' \) 2>/dev/null | wc -l | tr -d ' ')
  if [ "$count" -gt 0 ]; then ok "$count viewable file(s) found"; else warn "no viewable files found (yet)"; fi

  # The container runs as uid $APP_UID, not as the file owner: files must be
  # world-readable and folders world-searchable.
  local unreadable
  unreadable=$(find "$dir" \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=x \) 2>/dev/null | head -5)
  if [ -n "$unreadable" ]; then
    if [ "$fix_perms" = "fix" ]; then
      chmod -R o+rX "$dir" && ok "made the data readable by the container (chmod -R o+rX)"
    else
      echo "$unreadable" | sed 's/^/    /'
      die "the container (uid $APP_UID) can't read the files above; run  chmod -R o+rX '$dir'  or  ./deploy/deploy.sh deploy fix"
    fi
  else
    ok "readable by the container (uid $APP_UID)"
  fi
  ok "will be mounted read-only at /data"
}

wait_healthy() {
  step "Waiting for the app to become healthy"
  local id state i
  id="$(compose ps -q web)"
  [ -n "$id" ] || die "container did not start; see: ./deploy/deploy.sh logs"
  for i in $(seq 1 60); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo gone)"
    case "$state" in
      healthy) ok "healthy"; return 0 ;;
      unhealthy|exited|dead|gone)
        compose logs --tail 40 web || true
        die "container is $state" ;;
    esac
    sleep 2
  done
  compose logs --tail 40 web || true
  die "not healthy after 2 minutes"
}

smoke_test() {
  step "Checking the running app"
  local port base host page code
  port="$(env_get APP_PORT)"; port="${port:-3130}"
  base="http://127.0.0.1:$port"   # the container's paths; nginx adds BASE_PATH in front
  host="$(env_get ALLOWED_HOSTS | cut -d, -f1)"
  page="$(mktemp)"

  code=$(curl -s -o "$page" -w '%{http_code}' -H "Host: $host" "$base/")
  [ "$code" = "200" ] && grep -q "Moana Viewer" "$page" && ok "viewer page opens for $host (200)" || die "viewer page returned $code"
  grep -q "$(env_get BASE_PATH)/static/viewer/viewer.js" "$page" && ok "page links its files under '$(env_get BASE_PATH)/static/'" || die "page doesn't link $(env_get BASE_PATH)/static/"

  code=$(curl -s -o /dev/null -w '%{http_code}' "$base/static/viewer/viewer.js")
  [ "$code" = "200" ] && ok "static files are served (200)" || die "static files returned $code"

  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: not-$host.invalid" "$base/")
  [ "$code" = "400" ] && ok "unknown host names are refused (400)" || die "an unknown host name returned $code, expected 400"

  code=$(curl -s -o "$page" -w '%{http_code}' "$base/api/browse/?path=/data")
  [ "$code" = "200" ] && ok "the data folder can be browsed (200)" || die "browsing /data returned $code: $(head -c 200 "$page")"

  code=$(curl -s -o /dev/null -w '%{http_code}' "$base/api/browse/?path=/etc")
  [ "$code" = "400" ] && ok "folders outside /data are refused (400)" || die "browsing /etc returned $code, expected 400"

  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/api/uploads/open/")
  [ "$code" = "403" ] && ok "posts without a CSRF token are refused (403)" || die "a post without CSRF returned $code, expected 403"

  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $host" "$base/admin/login/")
  [ "$code" = "200" ] && ok "admin login page opens (200)" || warn "admin login returned $code"
  rm -f "$page"
}

print_nginx() {
  local port base max; port="$(env_get APP_PORT)"; port="${port:-3130}"
  base="$(env_get BASE_PATH)"; max="$(env_get MAX_UPLOAD_MB)"; max="${max:-4096}"
  step "nginx"
  echo "  Add to the server { } block for $(env_get ALLOWED_HOSTS | cut -d, -f1), then:  sudo nginx -t && sudo systemctl reload nginx"
  echo
  if [ -n "$base" ]; then
    echo "  location = $base { return 301 $base/; }"
  fi
  cat <<EOF
  location ${base:-}/ {
    proxy_pass http://127.0.0.1:$port/;   # trailing slash: nginx strips '${base:-/}' before passing on

    proxy_set_header Host              \$host;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-For   \$remote_addr;
    proxy_set_header X-Real-IP         \$remote_addr;

    # Files sent from browsers (MAX_UPLOAD_MB=$max), streamed straight through.
    client_max_body_size ${max}m;
    proxy_request_buffering off;
    # Tiles stream; the first open of a point cloud converts it, which takes minutes.
    proxy_buffering off;
    proxy_read_timeout 1800s;
    proxy_send_timeout 1800s;

    # The viewer has no login of its own. To restrict it:
    #   auth_basic "Moana Viewer";
    #   auth_basic_user_file /etc/nginx/moana.htpasswd;   # htpasswd -c /etc/nginx/moana.htpasswd <user>
  }
EOF
}

# --------------------------------------------------------------------------

cmd="${1:-deploy}"
case "$cmd" in
  check)
    check_env
    check_data
    echo; ok "ready to deploy: ./deploy/deploy.sh"
    ;;
  deploy)
    echo "${B}Moana Viewer: deploying from $PROJECT_DIR${N}"
    check_tools
    check_env
    check_data "${2:-}"
    step "Building the image"
    compose build --pull
    step "Starting the container"
    compose up -d --remove-orphans
    wait_healthy
    smoke_test
    docker image prune -f >/dev/null 2>&1 || true
    print_nginx
    echo; echo "${G}${B}Deployed.${N} https://$(env_get ALLOWED_HOSTS | cut -d, -f1)$(env_get BASE_PATH)/"
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f --tail 100 web
    ;;
  restart)
    check_data
    compose restart web
    wait_healthy
    smoke_test
    ;;
  admin)
    compose exec web python manage.py createsuperuser
    ;;
  down)
    compose down
    ok "stopped (layers, caches and temporary files stay in the moana-state volume)"
    ;;
  -h|--help|help)
    sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "unknown command: $cmd (try: deploy, check, status, logs, restart, admin, down)"
    ;;
esac
