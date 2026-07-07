#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREFIX="$SCRIPT_DIR/prefix"
DOWNLOADS="$SCRIPT_DIR/downloads"
CACHE="$SCRIPT_DIR/cache"
TMP="$SCRIPT_DIR/tmp"
LOGS="$SCRIPT_DIR/logs"
BIN="$PREFIX/bin"
PYTHON_SITE="$PREFIX/python_site"
APT_DOWNLOADS="$DOWNLOADS/apt"
INSTALL_MODE="${HDL_ENV_INSTALL_MODE:-apt_extract}"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"

mkdir -p "$PREFIX" "$DOWNLOADS" "$APT_DOWNLOADS" "$CACHE" "$TMP" "$LOGS" "$BIN"

log() {
  printf '[hdl-env] %s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

need_cmd curl
need_cmd python3
need_cmd tar
need_cmd gzip
need_cmd dpkg-deb

OS="$(uname -s)"
ARCH="$(uname -m)"
if [[ "$OS" != "Linux" || "$ARCH" != "x86_64" ]]; then
  printf 'This bootstrap currently supports Linux x86_64 only; got %s %s\n' "$OS" "$ARCH" >&2
  exit 1
fi

download_latest_asset_url() {
  local repo="$1"
  local regex="$2"
  GITHUB_REPO="$repo" ASSET_REGEX="$regex" python3 - <<'PY'
import json
import os
import re
import sys
import urllib.request

repo = os.environ["GITHUB_REPO"]
regex = re.compile(os.environ["ASSET_REGEX"])
url = f"https://api.github.com/repos/{repo}/releases/latest"
req = urllib.request.Request(
    url,
    headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "hdl-env-bootstrap",
    },
)
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.load(resp)

for asset in data.get("assets", []):
    name = asset.get("name", "")
    if regex.search(name):
        print(asset["browser_download_url"])
        print(f"{repo} {data.get('tag_name', '')} {name}", file=sys.stderr)
        break
else:
    names = ", ".join(a.get("name", "") for a in data.get("assets", []))
    raise SystemExit(f"No release asset matched {regex.pattern!r} for {repo}. Assets: {names}")
PY
}

download_file() {
  local url="$1"
  local out="$2"
  if [[ -s "$out" ]]; then
    log "Using cached $(basename "$out")"
    return
  fi
  log "Downloading $url"
  curl -fL --retry 5 --retry-delay 3 --connect-timeout 30 --speed-time 120 --speed-limit 1024 -o "$out" "$url"
}

extract_archive() {
  local archive="$1"
  local dest="$2"
  rm -rf "$dest"
  mkdir -p "$dest"
  case "$archive" in
    *.tar.gz|*.tgz)
      tar -xzf "$archive" -C "$dest"
      ;;
    *.zip)
      ARCHIVE="$archive" DEST="$dest" python3 - <<'PY'
import os
import zipfile

with zipfile.ZipFile(os.environ["ARCHIVE"]) as zf:
    zf.extractall(os.environ["DEST"])
PY
      ;;
    *)
      printf 'Unsupported archive format: %s\n' "$archive" >&2
      exit 1
      ;;
  esac
}

install_oss_cad_suite() {
  if [[ -x "$PREFIX/oss-cad-suite/bin/yosys" && -x "$PREFIX/oss-cad-suite/bin/verilator" ]]; then
    log "OSS CAD Suite already installed"
    return
  fi

  local url archive extract_dir suite_dir
  url="$(download_latest_asset_url "YosysHQ/oss-cad-suite-build" '^oss-cad-suite-linux-x64.*\.(tgz|tar\.gz)$')"
  archive="$DOWNLOADS/$(basename "$url")"
  extract_dir="$TMP/oss-cad-suite-extract"
  download_file "$url" "$archive"
  extract_archive "$archive" "$extract_dir"

  suite_dir="$(find "$extract_dir" -maxdepth 2 -type d -name 'oss-cad-suite' | head -n 1)"
  if [[ -z "$suite_dir" ]]; then
    printf 'Could not find oss-cad-suite directory after extracting %s\n' "$archive" >&2
    exit 1
  fi

  rm -rf "$PREFIX/oss-cad-suite"
  mv "$suite_dir" "$PREFIX/oss-cad-suite"
  log "Installed OSS CAD Suite"
}

apt_download_packages() {
  need_cmd apt-get
  local packages=("$@")
  local missing=()

  for pkg in "${packages[@]}"; do
    if ! compgen -G "$APT_DOWNLOADS/${pkg}_*.deb" >/dev/null; then
      missing+=("$pkg")
    fi
  done

  if [[ "${#missing[@]}" -eq 0 ]]; then
    log "Using cached apt packages"
    return
  fi

  log "Downloading apt packages: ${missing[*]}"
  (
    cd "$APT_DOWNLOADS"
    apt-get download "${missing[@]}"
  )
}

extract_debs_to_prefix() {
  local deb
  for deb in "$APT_DOWNLOADS"/*.deb; do
    [[ -e "$deb" ]] || continue
    log "Extracting $(basename "$deb")"
    dpkg-deb -x "$deb" "$PREFIX"
  done
}

localize_openjdk_config() {
  local java_home="$1"
  local link target rel local_target

  while IFS= read -r link; do
    target="$(readlink "$link")"
    case "$target" in
      /etc/java-17-openjdk/*)
        rel="${target#/etc/java-17-openjdk/}"
        local_target="$PREFIX/etc/java-17-openjdk/$rel"
        ;;
      /etc/ssl/certs/java/cacerts)
        local_target="/etc/ssl/certs/java/cacerts"
        if [[ ! -e "$local_target" ]]; then
          local_target="$PREFIX/etc/ssl/certs/java/cacerts"
        fi
        ;;
      *)
        continue
        ;;
    esac

    if [[ -e "$local_target" ]]; then
      rm -f "$link"
      ln -s "$local_target" "$link"
    fi
  done < <(find "$java_home" -type l \( -lname '/etc/java-17-openjdk/*' -o -lname '/etc/ssl/certs/java/cacerts' \))
}

ensure_java_cacerts() {
  local java_home="$1"
  local bundle="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
  local cacerts="$PREFIX/etc/ssl/certs/java/cacerts"
  local work_dir cert count

  if [[ ! -s "$bundle" ]]; then
    printf 'Could not find a system CA bundle at %s; set SSL_CERT_FILE to a PEM bundle path.\n' "$bundle" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$cacerts")"
  if [[ ! -s "$cacerts" ]]; then
    work_dir="$(mktemp -d "$TMP/cacerts.XXXXXX")"
    awk -v dir="$work_dir" '
      /-----BEGIN CERTIFICATE-----/ { n += 1; out = sprintf("%s/cert-%05d.pem", dir, n) }
      out { print > out }
      /-----END CERTIFICATE-----/ { close(out); out = "" }
    ' "$bundle"

    count=0
    for cert in "$work_dir"/*.pem; do
      [[ -s "$cert" ]] || continue
      count=$((count + 1))
      "$java_home/bin/keytool" \
        -importcert \
        -noprompt \
        -storepass changeit \
        -keystore "$cacerts" \
        -alias "ca-$count" \
        -file "$cert" >/dev/null 2>&1 || true
    done

    if [[ ! -s "$cacerts" || "$count" -eq 0 ]]; then
      printf 'Failed to create Java trust store at %s\n' "$cacerts" >&2
      exit 1
    fi
  fi

  ln -sfn "$cacerts" "$java_home/lib/security/cacerts"
}

install_apt_extract_tools() {
  local packages=(
    verilator
    yosys
    berkeley-abc
    libffi7
    libtcl8.6
    openjdk-17-jre-headless
    ca-certificates-java
    java-common
    libjpeg8
    liblcms2-2
    libasound2
    libharfbuzz0b
    libpcsclite1
    libcups2
  )

  apt_download_packages "${packages[@]}"
  extract_debs_to_prefix

  if [[ -x "$PREFIX/usr/bin/verilator" && ! -e "$BIN/verilator" ]]; then
    ln -s "$PREFIX/usr/bin/verilator" "$BIN/verilator"
  fi
  if [[ -x "$PREFIX/usr/bin/yosys" && ! -e "$BIN/yosys" ]]; then
    ln -s "$PREFIX/usr/bin/yosys" "$BIN/yosys"
  fi
  if [[ -x "$PREFIX/usr/bin/berkeley-abc" && ! -e "$BIN/berkeley-abc" ]]; then
    ln -s "$PREFIX/usr/bin/berkeley-abc" "$BIN/berkeley-abc"
  fi
  if [[ -x "$PREFIX/usr/bin/yosys-abc" && ! -e "$BIN/yosys-abc" ]]; then
    ln -s "$PREFIX/usr/bin/yosys-abc" "$BIN/yosys-abc"
  elif [[ -x "$PREFIX/usr/bin/berkeley-abc" && ! -e "$BIN/yosys-abc" ]]; then
    ln -s "$PREFIX/usr/bin/berkeley-abc" "$BIN/yosys-abc"
  fi
  if [[ -d "$PREFIX/usr/share/verilator/include" && ! -e "$PREFIX/usr/include" ]]; then
    ln -s "$PREFIX/usr/share/verilator/include" "$PREFIX/usr/include"
  fi
  if [[ -x "$PREFIX/usr/share/verilator/bin/verilator_includer" && ! -e "$PREFIX/usr/bin/verilator_includer" ]]; then
    ln -s "$PREFIX/usr/share/verilator/bin/verilator_includer" "$PREFIX/usr/bin/verilator_includer"
  fi

  local java_home
  java_home="$(find "$PREFIX/usr/lib/jvm" -maxdepth 1 -type d -name 'java-17-openjdk-*' | head -n 1 || true)"
  if [[ -z "$java_home" ]]; then
    printf 'Could not find extracted OpenJDK under %s/usr/lib/jvm\n' "$PREFIX" >&2
    exit 1
  fi
  localize_openjdk_config "$java_home"
  ensure_java_cacerts "$java_home"
  ln -sfn "$java_home" "$PREFIX/java"
  if [[ ! -e "$BIN/java" ]]; then
    ln -s "$PREFIX/java/bin/java" "$BIN/java"
  fi

  log "Installed apt-extracted Verilator, Yosys, Berkeley ABC, and OpenJDK"
}

install_maven() {
  local maven_version maven_url archive extract_dir maven_dir
  maven_version="${MAVEN_VERSION:-3.9.11}"
  maven_url="${MAVEN_URL:-https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$maven_version/apache-maven-$maven_version-bin.tar.gz}"
  archive="$DOWNLOADS/apache-maven-$maven_version-bin.tar.gz"

  if [[ -x "$PREFIX/apache-maven-$maven_version/bin/mvn" ]]; then
    log "Maven already installed"
  else
    download_file "$maven_url" "$archive"
    extract_dir="$TMP/maven-extract"
    extract_archive "$archive" "$extract_dir"
    maven_dir="$(find "$extract_dir" -maxdepth 1 -type d -name 'apache-maven-*' | head -n 1)"
    if [[ -z "$maven_dir" ]]; then
      printf 'Could not find apache-maven directory after extracting %s\n' "$archive" >&2
      exit 1
    fi
    rm -rf "$PREFIX/apache-maven-$maven_version"
    mv "$maven_dir" "$PREFIX/apache-maven-$maven_version"
  fi

  ln -sfn "$PREFIX/apache-maven-$maven_version" "$PREFIX/maven"
  cat > "$BIN/mvn" <<EOF
#!/usr/bin/env bash
set -euo pipefail
source "$SCRIPT_DIR/env.sh"
exec "$PREFIX/maven/bin/mvn" -Dmaven.repo.local="$CACHE/m2" "\$@"
EOF
  chmod +x "$BIN/mvn"

  log "Prewarming Maven"
  "$BIN/mvn" --version >/dev/null
}

install_pyslang() {
  mkdir -p "$PYTHON_SITE"
  if PYTHONPATH="$PYTHON_SITE:${PYTHONPATH:-}" python3 - <<'PY' >/dev/null 2>&1
import pyslang
PY
  then
    log "pyslang already installed"
    return
  fi

  log "Installing pyslang into isolated Python target"
  python3 -m pip install --target "$PYTHON_SITE" "pyslang==${PYSLANG_VERSION:-11.0.0}" >/dev/null
}

install_slang_if_needed() {
  if [[ -x "$PREFIX/oss-cad-suite/bin/slang" ]]; then
    log "Using slang from OSS CAD Suite"
    return
  fi

  install_pyslang

  cat > "$BIN/slang" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  python3 - <<'PY'
import importlib.metadata
try:
    print("pyslang", importlib.metadata.version("pyslang"))
except importlib.metadata.PackageNotFoundError:
    print("pyslang unknown")
PY
  exit 0
fi
python3 - "$@" <<'PY'
import sys

try:
    import pyslang
    from pyslang.ast import Compilation
    from pyslang.syntax import SyntaxTree
except Exception as exc:
    print(f"failed to import pyslang: {exc}", file=sys.stderr)
    raise SystemExit(2)


def _diag_is_error(diagnostic):
    is_error = getattr(diagnostic, "isError", False)
    return bool(is_error() if callable(is_error) else is_error)


def _format_diagnostics(source_manager, diagnostics):
    diagnostics = list(diagnostics)
    if not diagnostics:
        return ""
    try:
        return pyslang.DiagnosticEngine.reportAll(source_manager, diagnostics).rstrip()
    except Exception:
        engine = pyslang.DiagnosticEngine(source_manager)
        lines = []
        for diagnostic in diagnostics:
            location = getattr(diagnostic, "location", None)
            try:
                filename = source_manager.getFileName(location)
                line = source_manager.getLineNumber(location)
                column = source_manager.getColumnNumber(location)
            except Exception:
                filename, line, column = "<unknown>", 0, 0
            severity = "error" if _diag_is_error(diagnostic) else "warning"
            try:
                message = engine.formatMessage(diagnostic)
            except Exception:
                message = str(getattr(diagnostic, "code", diagnostic))
            lines.append(f"{filename}:{line}:{column}: {severity}: {message}")
        return "\n".join(lines)

args = sys.argv[1:]
if not args:
    print("usage: slang [options] <systemverilog files...>", file=sys.stderr)
    raise SystemExit(2)

unsupported = [arg for arg in args if arg.startswith("-")]
if unsupported:
    print(f"pyslang wrapper does not support options yet: {' '.join(unsupported)}", file=sys.stderr)
    raise SystemExit(2)

tree = SyntaxTree.fromFiles(args)
compilation = Compilation()
compilation.addSyntaxTree(tree)
diagnostics = list(compilation.getAllDiagnostics())
formatted = _format_diagnostics(compilation.sourceManager, diagnostics)
if formatted:
    print(formatted, file=sys.stderr)

has_error = any(_diag_is_error(diagnostic) for diagnostic in diagnostics)

raise SystemExit(1 if has_error else 0)
PY
EOF
  chmod +x "$BIN/slang"
  log "Installed slang wrapper backed by pyslang"
  return

  local url archive extract_dir slang_bin install_dir
  url="$(download_latest_asset_url "MikePopoloski/slang" '.*[Ll]inux.*(x86_64|x64|amd64).*\.(tgz|tar\.gz|zip)$')"
  archive="$DOWNLOADS/$(basename "$url")"
  extract_dir="$TMP/slang-extract"
  install_dir="$PREFIX/slang"
  download_file "$url" "$archive"
  extract_archive "$archive" "$extract_dir"

  slang_bin="$(find "$extract_dir" -type f -name slang -perm -u+x | head -n 1)"
  if [[ -z "$slang_bin" ]]; then
    printf 'Could not find executable named slang after extracting %s\n' "$archive" >&2
    exit 1
  fi

  rm -rf "$install_dir"
  mkdir -p "$install_dir"
  top_dir="$(dirname "$(dirname "$slang_bin")")"
  cp -a "$top_dir"/. "$install_dir"/

  cat > "$BIN/slang" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$install_dir/bin/slang" "\$@"
EOF
  chmod +x "$BIN/slang"
  log "Installed standalone slang"
}

write_env_file() {
  cat > "$SCRIPT_DIR/env.sh" <<EOF
# Source this file to use the isolated HDL environment.
export HDL_ENV_ROOT="$SCRIPT_DIR"
export HDL_ENV_PREFIX="$PREFIX"
export JAVA_HOME="$PREFIX/java"
export VERILATOR_ROOT="$PREFIX/usr"
export MAVEN_OPTS="\${MAVEN_OPTS:-} -Djavax.net.ssl.trustStore=$PREFIX/etc/ssl/certs/java/cacerts -Djavax.net.ssl.trustStorePassword=changeit -Djavax.net.ssl.trustStoreType=PKCS12"
export PYTHONPATH="$PYTHON_SITE:\${PYTHONPATH:-}"
export LD_LIBRARY_PATH="$PREFIX/usr/lib/x86_64-linux-gnu:$PREFIX/usr/lib:$PREFIX/lib/x86_64-linux-gnu:\${LD_LIBRARY_PATH:-}"
export PATH="$BIN:$PREFIX/oss-cad-suite/bin:$PREFIX/usr/bin:$PREFIX/java/bin:\$PATH"
EOF
}

write_env_file

case "$INSTALL_MODE" in
  apt_extract)
    install_apt_extract_tools
    ;;
  oss_cad_suite)
    install_oss_cad_suite
    ;;
  *)
    printf 'Unknown HDL_ENV_INSTALL_MODE=%s. Use apt_extract or oss_cad_suite.\n' "$INSTALL_MODE" >&2
    exit 1
    ;;
esac

write_env_file
source "$SCRIPT_DIR/env.sh"
install_maven
install_slang_if_needed
write_env_file

log "Tool versions:"
source "$SCRIPT_DIR/env.sh"
yosys -V
verilator --version
slang --version
mvn --version | head -n 1

log "Done. Run: bash hdl_env/smoke_test_hdl_env.sh"
