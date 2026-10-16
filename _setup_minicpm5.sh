#!/usr/bin/env bash
# =============================================================================
# MiniCPM5-2B 一键就绪脚本
# -----------------------------------------------------------------------------
# 作用：把 MiniCPM5-2B（面壁智能，端侧/纯 CPU 优化，中文原生）拉到本地，
#       并把机器人切换到该模型、重启生效。
#
# 为什么选它（相对当前 7B Qwen）：
#   - 2.5B 参数，纯 CPU 推理速度约为 7B 的 5-7 倍（群里从"等 2 分钟"变"秒回"）
#   - 4bit 量化仅 3-4GB，8GB cgroup 下留足余量（7B 已吃到 6.8GB，快贴顶）
#   - 中文原生优化，不会出现翻译腔 / 中英混杂
#   - Apache-2.0，可商用
#
# 模型来源（按优先级）：
#   1. ModelScope（魔搭，国内快）：openbmb/MiniCPM5-2B-SFT
#   2. hf-mirror（HuggingFace 国内镜像）
#   3. HuggingFace 原站
#
# 用法：bash _setup_minicpm5.sh            # 自动探测可用源并下载
#       bash _setup_minicpm5.sh --force     # 已存在也重新下载
# =============================================================================
set -u

BASE="/root/qqbot"
cd "$BASE" || exit 1

MODEL_DIR="$BASE/minicpm5_model"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---------- 1. 探测下载源 ----------
log "探测可用下载源..."
SRC=""
probe() {  # $1=地址 $2=名称
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 8 --max-time 15 "$1" 2>/dev/null)
    [ "$code" = "200" ] || [ "$code" = "302" ] && { SRC="$2"; log "  ✅ $2 可用"; return 0; }
    log "  ❌ $2 不可用 (HTTP $code)"; return 1
}
probe "https://modelscope.cn" "modelscope" \
  || probe "https://hf-mirror.com" "hf-mirror" \
  || probe "https://huggingface.co" "huggingface"

if [ -z "$SRC" ]; then
    log "所有下载源均不可达（沙箱可能限制了大文件出网）。脚本退出，可稍后重跑。"
    exit 1
fi

# ---------- 2. 下载模型 ----------
mkdir -p "$MODEL_DIR"
if [ "$FORCE" = "0" ] && [ -n "$(ls -A "$MODEL_DIR" 2>/dev/null | grep -i 'safetensors\|gguf')" ]; then
    log "模型已存在，跳过下载（加 --force 可强制重下）"
else
    case "$SRC" in
        modelscope)
            # ModelScope 官方 CLI（最稳，自动处理分片与断点续传）
            if ! command -v modelscope >/dev/null 2>&1; then
                log "安装 modelscope CLI..."
                pip3 install -q modelscope 2>&1 | tail -2
            fi
            log "从 ModelScope 下载 openbmb/MiniCPM5-2B-SFT ..."
            modelscope download --model openbmb/MiniCPM5-2B-SFT --local_dir "$MODEL_DIR"
            ;;
        hf-mirror|huggingface)
            BASE_URL="https://hf-mirror.com"
            [ "$SRC" = "huggingface" ] && BASE_URL="https://huggingface.co"
            REPO="openbmb/MiniCPM5-2B-SFT"
            log "从 $SRC 下载 $REPO ..."
            # 先取文件清单，再逐个下（支持断点续传）
            files=$(curl -s --max-time 30 "$BASE_URL/api/models/$REPO" \
                    | python3 -c "import sys,json;[print(f['rfilename']) for f in json.load(sys.stdin).get('siblings',[])]" 2>/dev/null)
            if [ -z "$files" ]; then
                log "获取文件清单失败，改用 Git LFS 方式"
                GIT_LFS_SKIP_SMUDGE=0 git clone "https://hf-mirror.com/$REPO" "$MODEL_DIR" 2>&1 | tail -3
            else
                for f in $files; do
                    case "$f" in
                        *.safetensors|*.json|*.txt|*.model|tokenizer*)
                            log "  ↓ $f"
                            curl -fSL --retry 3 --retry-delay 3 -o "$MODEL_DIR/$f" \
                                "$BASE_URL/$REPO/resolve/main/$f" || log "  ⚠️ $f 下载失败"
                            ;;
                        *) log "  ⏭  跳过 $f" ;;
                    esac
                done
            fi
            ;;
    esac
fi

# ---------- 3. 校验 ----------
if ! ls "$MODEL_DIR"/*.safetensors >/dev/null 2>&1; then
    log "❌ 未找到 .safetensors 权重，下载可能不完整。请检查网络后重跑。"
    exit 1
fi
log "✅ 权重就绪：$(du -sh "$MODEL_DIR" | cut -f1)"

# ---------- 4. 切换模型 ----------
if grep -q '^QQ_BRAIN_MODEL=' "$BASE/.env"; then
    sed -i 's/^QQ_BRAIN_MODEL=.*/QQ_BRAIN_MODEL=minicpm5/' "$BASE/.env"
else
    echo 'QQ_BRAIN_MODEL=minicpm5' >> "$BASE/.env"
fi
log "✅ .env 已切换为 QQ_BRAIN_MODEL=minicpm5"

# ---------- 5. 重启 ----------
log "重启机器人..."
pkill -f qq_receiver.py 2>/dev/null && log "  已终止旧 receiver（watchdog 会自动重拉）"
sleep 3
if ! pgrep -f qq_watchdog.sh >/dev/null; then
    setsid bash "$BASE/qq_watchdog.sh" >> "$BASE/watchdog.log" 2>&1 < /dev/null &
    disown
    log "  watchdog 未在运行，已重新拉起"
fi
log "✅ 完成。约 30 秒后 receiver 会加载 MiniCPM5-2B（首条消息触发懒加载）。"
