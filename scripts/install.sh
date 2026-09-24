#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if [[ "${1:-}" == "--check" ]]; then
  for task_tool in uv terraform docker harbor ssh-keygen; do
    if command -v "$task_tool" >/dev/null 2>&1; then
      printf '%s: %s\n' "$task_tool" "$(command -v "$task_tool")"
    else
      printf '%s: missing\n' "$task_tool"
      exit 1
    fi
  done
  docker info >/dev/null 2>&1 || { echo 'Docker daemon: unavailable' >&2; exit 1; }
  [[ "$(terraform version | head -n 1)" == 'Terraform v1.16.4' ]] || {
    echo 'Terraform 1.16.4 is required' >&2; exit 1;
  }
  [[ "$(harbor --version)" == '0.5.0' ]] || {
    echo 'Harbor 0.5.0 is required' >&2; exit 1;
  }
  echo 'Toolchain ready'
  exit 0
fi
[[ $# -eq 0 ]] || { echo 'Usage: bash scripts/install.sh [--check]' >&2; exit 2; }

task_os=$(uname -s)
case "$task_os" in
  Darwin)
    if ! command -v brew >/dev/null 2>&1; then
      echo 'Installing Homebrew for Docker Desktop...'
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    fi
    if [[ ! -d /Applications/Docker.app ]]; then
      brew install --cask docker-desktop
    fi
    ;;
  Linux)
    # The automatic Linux path supports Ubuntu's official Docker packages.
    source /etc/os-release
    [[ "$ID" == ubuntu ]] || {
      echo 'Automatic Docker installation supports Ubuntu only; install Docker manually for this OS.' >&2
      exit 1
    }
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl git openssh-client unzip
    if ! command -v docker >/dev/null 2>&1; then
      sudo install -m 0755 -d /etc/apt/keyrings
      sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      sudo chmod a+r /etc/apt/keyrings/docker.asc
      task_codename=${UBUNTU_CODENAME:-$VERSION_CODENAME}
      task_arch=$(dpkg --print-architecture)
      sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $task_codename
Components: stable
Architectures: $task_arch
Signed-By: /etc/apt/keyrings/docker.asc
EOF
      sudo apt-get update
      sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      sudo systemctl enable --now docker
    fi
    ;;
  *)
    echo "Unsupported operating system: $task_os" >&2
    exit 1
    ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

task_tf_version=1.16.4
if [[ "$(terraform version 2>/dev/null | head -n 1 || true)" != "Terraform v$task_tf_version" ]]; then
  case "$(uname -m)" in
    arm64|aarch64) task_tf_arch=arm64 ;;
    x86_64|amd64) task_tf_arch=amd64 ;;
    *) echo 'Unsupported Terraform architecture' >&2; exit 1 ;;
  esac
  case "$task_os" in Darwin) task_tf_os=darwin ;; Linux) task_tf_os=linux ;; esac
  task_archive="terraform_${task_tf_version}_${task_tf_os}_${task_tf_arch}.zip"
  task_url="https://releases.hashicorp.com/terraform/${task_tf_version}"
  task_tmp=$(mktemp -d)
  trap 'rm -rf "$task_tmp"' EXIT
  curl -fsSLo "$task_tmp/$task_archive" "$task_url/$task_archive"
  curl -fsSLo "$task_tmp/SHA256SUMS" "$task_url/terraform_${task_tf_version}_SHA256SUMS"
  task_expected=$(awk -v archive="$task_archive" '$2 == archive {print $1}' "$task_tmp/SHA256SUMS")
  [[ -n "$task_expected" ]] || { echo 'Terraform checksum missing' >&2; exit 1; }
  if command -v shasum >/dev/null 2>&1; then
    task_actual=$(shasum -a 256 "$task_tmp/$task_archive" | awk '{print $1}')
  else
    task_actual=$(sha256sum "$task_tmp/$task_archive" | awk '{print $1}')
  fi
  [[ "$task_actual" == "$task_expected" ]] || { echo 'Terraform checksum mismatch' >&2; exit 1; }
  unzip -q "$task_tmp/$task_archive" terraform -d "$task_tmp"
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$task_tmp/terraform" "$HOME/.local/bin/terraform"
fi

if [[ "$(harbor --version 2>/dev/null || true)" != '0.5.0' ]]; then
  uv tool install --force 'harbor==0.5.0'
fi
uv python install 3.12
uv sync --frozen --python 3.12

if [[ "$task_os" == Darwin ]] && ! docker info >/dev/null 2>&1; then
  open -a Docker
  echo 'Waiting for Docker Desktop; complete its first-run setup if prompted.'
  for task_attempt in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
if [[ "$task_os" == Linux ]] && ! docker info >/dev/null 2>&1; then
  sudo systemctl enable --now docker
fi
if [[ "$task_os" == Linux ]] && ! docker info >/dev/null 2>&1; then
  sudo usermod -aG docker "$(id -un)"
  echo 'Docker was installed. Sign out and back in for group access, then rerun this command.' >&2
  exit 1
fi

bash scripts/install.sh --check
echo 'Installation complete. Add credentials to the ignored root .env before a cloud run.'
