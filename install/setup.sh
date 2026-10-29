#!/usr/bin/env bash
# UglyCat OS 安装脚本（macOS / Linux）——幂等、用户级、无需 sudo。
#
#   ./install/setup.sh --check-only   只体检，不改动系统
#   ./install/setup.sh --dry-run      打印将执行的命令
#   ./install/setup.sh                正式安装
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHECK_ONLY=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --check-only) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] $*"
  else
    "$@"
  fi
}

# ── 1. 找 Python >= 3.11 ────────────────────────────────────────────────
PY=""
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  say "错误：未找到 Python >= 3.11。" >&2
  say "  macOS：brew install python@3.11   Linux：用发行版包管理器或 pyenv 安装" >&2
  exit 1
fi
say "Python：$PY（$("$PY" -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])')）"
say "目录：  $ROOT"

if command -v uv >/dev/null 2>&1; then
  say "包管理器：uv"
else
  say "包管理器：pip（未发现 uv，将用 venv + pip）"
fi

if [ "$CHECK_ONLY" = 1 ]; then
  say "体检完成（未安装）"
  exit 0
fi

# ── 2. 建虚拟环境 + 安装依赖 ────────────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
  run uv venv .venv
  run uv pip install --python .venv/bin/python -e ".[dev]"
else
  run "$PY" -m venv .venv
  run .venv/bin/python -m pip install -U pip
  run .venv/bin/python -m pip install -e ".[dev]"
fi

# ── 3. 初始化 .env / 目录 / 启动器 ──────────────────────────────────────
if [ ! -f .env ]; then
  run cp .env.example .env
  say "已生成 .env —— 请填入任意一个 LLM Key（推荐 ZHIPU_API_KEY）"
fi
run mkdir -p data logs

if [ "$DRY_RUN" != 1 ]; then
  cat > uglycat <<'LAUNCHER'
#!/usr/bin/env sh
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
exec "$ROOT/.venv/bin/python" "$ROOT/run.py" "$@"
LAUNCHER
  chmod +x uglycat
  say "已生成启动器：./uglycat"
fi

say ""
say "下一步："
say "  1) 编辑 .env 填入 LLM Key"
say "  2) ./uglycat doctor --json     # 判据 ok=true"
say "  3) ./uglycat news probe        # LLM 连通性"
say "  4) ./uglycat install-schedule --time 08:00   # 可选：每天自动跑"
