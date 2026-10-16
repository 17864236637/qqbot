#!/usr/bin/env python3
"""QQ 机器人"大脑"：本地生成自然语言（监督话术 / 回复）。

后端优先级（由环境变量 QQ_BRAIN_MODEL 选择）：
1. MiniCPM5-2B（minicpm5_gguf/ GGUF 或 minicpm5_model/）—— 面壁智能端侧旗舰，
   2.5B 参数 + 纯 CPU 高速 + 中文原生优化，8GB 内存下的首选
2. llama.cpp / GGUF（qwen7b_gguf/）—— Qwen2.5-7B-Instruct 量化版，CPU 上最快的 7B 方案
3. Qwen2.5-1.5B-Instruct（transformers，qwen15_model/）—— 速度与质量均衡，稳定兜底主脑
4. Qwen2.5-0.5B-Instruct（transformers，qwen_model/）
5. Qwen2.5-7B-Instruct（transformers，qwen7b_model/）—— 仅当上述全部不可用时

性能教训（务必保留，别再踩）：
- 必须用 torch_dtype="auto"（原生 bf16）。强制 float32 会把 3.5GB 上转成 6GB，
  实测加载 207s→2s、生成 0.06→3.44 tok/s，差两个数量级。
- 线程数固定 8：批量为 1 的生成受内存带宽限制，线程过多反而调度开销大。
- 不用 DeepSeek-R1-Distill 系列：推理型模型对简单任务也要推演 250+ token
  （70 秒仍无正文），与"短消息 + 秒回"场景错配。
- 【重要】7B 走 transformers 在纯 CPU 上不可用：权重 15.2GB 被 mmap，
  每步回源读盘，实测 0.05 tok/s（生成 120 token 需 40 分钟）。
  要跑 7B 必须走 GGUF 量化 + llama.cpp。此路在 transformes 链里排最后，
  平时不会命中，避免误用把整个对话卡死。
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
# MiniCPM5-2B（面壁智能）：端侧/纯 CPU 优化，中文原生，2.5B 参数
MINICPM_GGUF_DIR = os.path.join(BASE, "minicpm5_gguf")
MINICPM_MODEL_DIR = os.path.join(BASE, "minicpm5_model")
QWEN_GGUF_DIR = os.path.join(BASE, "qwen7b_gguf")
QWEN7B_MODEL_DIR = os.path.join(BASE, "qwen7b_model")
QWEN15_MODEL_DIR = os.path.join(BASE, "qwen15_model")
QWEN05_MODEL_DIR = os.path.join(BASE, "qwen_model")

# 这些目录的模型实现随权重一起分发，transformers 加载需 trust_remote_code=True
_TRUST_REMOTE_DIRS = {MINICPM_MODEL_DIR}

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
# Ollama 模型名可配：默认 llama3.2:3b（q4）。等网络恢复拉到模型后，
# 把 .env 的 QQ_BRAIN_MODEL 改成 llama32 即可一键切到 Ollama。
OLLAMA_MODEL = os.environ.get("QQ_OLLAMA_MODEL", "llama3.2:3b")

TORCH_THREADS = 8

_LOCAL: dict[str, tuple] = {}  # 按目录缓存 (model, tokenizer)


def _clean(text: str) -> str:
    """清理输出：剥离可能的思考段与角色标记，只留正文。"""
    t = (text or "").strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    if "<think>" in t:
        t = t.split("<think>")[0]
    for tag in ("<think>", "</think>", "<|im_start|>", "<|im_end|>", "<|endoftext|>"):
        t = t.replace(tag, "")
    return t.strip()


def _get_local(model_dir: str):
    if model_dir not in _LOCAL:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import torch
        torch.set_num_threads(TORCH_THREADS)
        from transformers import AutoModelForCausalLM, AutoTokenizer
        # MiniCPM 等模型把 modeling 代码放在权重目录里，必须信任远程代码
        trust = model_dir in _TRUST_REMOTE_DIRS
        tok = AutoTokenizer.from_pretrained(
            model_dir, local_files_only=True, trust_remote_code=trust)
        # 关键：原生 dtype（bf16），切勿强制 float32
        model = AutoModelForCausalLM.from_pretrained(
            model_dir, torch_dtype="auto", low_cpu_mem_usage=True,
            local_files_only=True, trust_remote_code=trust,
        )
        model.eval()
        _LOCAL[model_dir] = (model, tok)
    return _LOCAL[model_dir]


def _gen_local(prompt: str, system: str, temperature: float,
               max_tokens: int, model_dir: str) -> str:
    model, tok = _get_local(model_dir)
    enc = tok.apply_chat_template(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        add_generation_prompt=True, return_tensors="pt",
    )
    ids = enc["input_ids"] if hasattr(enc, "get") else enc
    out = model.generate(
        ids, max_new_tokens=max_tokens, temperature=temperature,
        do_sample=True, top_p=0.9, pad_token_id=tok.eos_token_id,
    )
    return _clean(tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True))


def _gen_ollama(prompt: str, system: str, temperature: float, max_tokens: int,
                model: str | None = None) -> str:
    import json
    payload = {"model": model or OLLAMA_MODEL, "prompt": prompt, "system": system,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        obj = json.loads(resp.read().decode("utf-8") or "{}")
    return (obj.get("response") or "").strip()


def _ready(model_dir: str) -> bool:
    """模型目录是否可用。

    - transformers 目录：需要 config.json + 至少一个 .safetensors（支持分片）
    - llama.cpp GGUF 目录：需要至少一个 .gguf 文件
    """
    if not os.path.isdir(model_dir):
        return False
    files = os.listdir(model_dir)
    if any(f.endswith(".gguf") for f in files):
        return True
    if not os.path.exists(os.path.join(model_dir, "config.json")):
        return False
    return any(f.endswith(".safetensors") for f in files)


_GGUF_LLM = None
_GGUF_PATH = None
_GGUF_FORMAT = None


def _find_gguf(model_dir: str) -> str | None:
    """取目录下第一个 .gguf 文件（按文件名排序，保证分片顺序稳定）。"""
    if not os.path.isdir(model_dir):
        return None
    return next(
        (os.path.join(model_dir, f)
         for f in sorted(os.listdir(model_dir)) if f.endswith(".gguf")),
        None,
    )


def _gen_gguf(prompt: str, system: str = "", temperature: float = 0.85,
              max_tokens: int = 220, model_dir: str | None = None,
              chat_format: str | None = None) -> str:
    """用 llama.cpp 跑 GGUF 量化模型（CPU 上跑大模型的唯一实用路径）。

    model_dir   模型目录；缺省回退 QWEN_GGUF_DIR（保持旧行为不变）
    chat_format 对话模板；传 None 表示交给 llama.cpp 从 GGUF 内嵌元数据自动识别。
                实测该路径可行（7B GGUF 不指定也能正确对话），因此换任何新模型
                （如 MiniCPM5）都不用为它硬编码模板。
    """
    global _GGUF_LLM, _GGUF_PATH, _GGUF_FORMAT
    from llama_cpp import Llama  # 延迟导入，未安装也不影响其他后端

    model_dir = model_dir or QWEN_GGUF_DIR
    gguf = _find_gguf(model_dir)
    if gguf is None:
        raise RuntimeError(f"{model_dir} 下没有 .gguf 文件")

    # 模型或模板变化时重建实例
    if _GGUF_LLM is None or _GGUF_PATH != gguf or _GGUF_FORMAT != chat_format:
        # use_mmap=True（实测可行）：mmap 不一次性占满物理内存，8GB cgroup 下
        # 不会 OOM；缺点是慢磁盘按需 page-in 时偏慢。若已预热 page cache 则接近纯 CPU 速度。
        # （之前用 use_mmap=False 在沙箱 8GB 限制下会被 OOM killer 静默杀死。）
        _GGUF_LLM = Llama(
            model_path=gguf,
            n_ctx=2048,
            n_threads=max(1, (os.cpu_count() or 8) // 2),
            n_batch=512,
            use_mmap=True,
            verbose=False,
            chat_format=chat_format,   # None -> 自动识别 GGUF 内嵌模板
        )
        _GGUF_PATH = gguf
        _GGUF_FORMAT = chat_format

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    res = _GGUF_LLM.create_chat_completion(
        messages=messages,
        temperature=temperature,
        top_p=0.9,
        max_tokens=max_tokens,
    )
    text = (res["choices"][0]["message"]["content"] or "").strip()
    if not text and chat_format is None:
        # 自动模板没奏效（个别 GGUF 转换时丢了 template 元数据）：
        # 重建实例显式按 chatml 再来一次，仍失败则抛错交给上层降级。
        _GGUF_LLM, _GGUF_PATH, _GGUF_FORMAT = None, None, None
        return _gen_gguf(prompt, system, temperature, max_tokens,
                         model_dir=model_dir, chat_format="chatml")
    return _clean(text)


def generate(prompt: str, system: str = "", temperature: float = 0.85,
             max_tokens: int = 220) -> str:
    """生成回复，失败依次降级；全部失败抛 RuntimeError。"""
    errs = []

    # 模型优先级选择（环境变量 QQ_BRAIN_MODEL）：
    #   "minicpm"/"minicpm5" → MiniCPM5-2B 优先（端侧/纯CPU优化，中文原生，8GB 首选）
    #   "ollama"/"llama32"/"llama3.2" → Ollama 本地服务优先（如 llama3.2:3b），失败回退本地
    #   "7b"    → 7B GGUF 优先（质量最好，最慢、最吃内存；CPU 上约 2 tok/s）
    #   "0.5b"  → 0.5B 优先（最小最快，回复随缘，适合恶搞）
    #   其它/空 → 1.5B 优先（默认，质量与开销均衡）
    model_pref = os.environ.get("QQ_BRAIN_MODEL", "").strip().lower()

    # Ollama 优先模式：把本地 Ollama 服务（如 llama3.2:3b）作为首选大脑，
    # 失败再退回本地 transformers/GGUF 兜底。这样"拉到 3B 后"只需改 .env 即可。
    if model_pref in ("ollama", "llama32", "llama3.2", "llama3.2:3b"):
        try:
            out = _gen_ollama(prompt, system, temperature, max_tokens)
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            errs.append(f"ollama({OLLAMA_MODEL}): {exc}")
        # 不 return，继续走下面的本地模型兜底

    # MiniCPM5-2B 优先模式：GGUF → transformers → Ollama(minicpm5:2b) → 原有链路兜底
    if model_pref in ("minicpm", "minicpm5", "minicpm5:2b"):
        ollama_model = os.environ.get("QQ_OLLAMA_MODEL") or "minicpm5:2b"
        try:
            out = _gen_ollama(prompt, system, temperature, max_tokens,
                              model=ollama_model)
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            errs.append(f"ollama({ollama_model}): {exc}")

    if model_pref in ("minicpm", "minicpm5", "minicpm5:2b"):
        order = (
            (MINICPM_GGUF_DIR, "minicpm5-gguf"), (MINICPM_MODEL_DIR, "minicpm5"),
            (QWEN15_MODEL_DIR, "qwen1.5b"), (QWEN_GGUF_DIR, "qwen7b-gguf"),
        )
    elif model_pref == "7b":
        order = (
            (QWEN_GGUF_DIR, "qwen7b-gguf"), (QWEN15_MODEL_DIR, "qwen1.5b"),
            (QWEN05_MODEL_DIR, "qwen0.5b"), (QWEN7B_MODEL_DIR, "qwen7b")
        )
    elif model_pref == "0.5b":
        order = (
            (QWEN05_MODEL_DIR, "qwen0.5b"), (QWEN15_MODEL_DIR, "qwen1.5b"),
            (QWEN_GGUF_DIR, "qwen7b-gguf"), (QWEN7B_MODEL_DIR, "qwen7b")
        )
    else:  # 默认 1.5B 优先
        order = (
            (QWEN15_MODEL_DIR, "qwen1.5b"), (QWEN_GGUF_DIR, "qwen7b-gguf"),
            (QWEN05_MODEL_DIR, "qwen0.5b"), (QWEN7B_MODEL_DIR, "qwen7b")
        )
    for model_dir, tag in order:
        if not _ready(model_dir):
            continue
        try:
            if tag.endswith("-gguf"):
                # chat_format=None：让 llama.cpp 从 GGUF 内嵌元数据自动识别模板，
                # 换任何新模型都不用为它硬编码对话模板
                out = _gen_gguf(prompt, system, temperature, max_tokens,
                                model_dir=model_dir)
            else:
                out = _gen_local(prompt, system, temperature, max_tokens, model_dir)
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{tag}: {exc}")

    # 最后：ollama
    try:
        out = _gen_ollama(prompt, system, temperature, max_tokens)
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        errs.append(f"ollama: {exc}")

    raise RuntimeError("大脑不可用: " + "; ".join(errs))


if __name__ == "__main__":
    print(generate("用一句话提醒朋友去喝水", system="你是QQ监督员，语气简短有梗"))
