"""번역에 사용할 LLM 모델을 이 머신의 GPU 성능에 맞춰 자동 선택.

원칙:
- 모두 **Qwen 계열(Apache-2.0)** → 회사/상업 사용에 라이선스 제약 없음.
- VRAM 에 '전부 올려' 안정적으로 돌릴 수 있는 티어를 고른다(오프로드로 느려지지 않도록 여유 확보).
- 각 티어는 (Qwen3 우선, Qwen2.5 대체)를 함께 갖는다.
  Qwen3 가 실제로 잘 도는 머신이면 Qwen3 를, 그렇지 않으면(구형 Ollama·불안정) 서버가
  런타임 헬스체크로 자동 감지해 Qwen2.5 로 폴백한다.

환경변수:
- PAPER_VIEWER_MODEL : 특정 모델을 강제 지정(예: "qwen2.5:14b-instruct").
- PAPER_VIEWER_ENGINE: "qwen2.5" 로 두면 Qwen3 를 건너뛰고 Qwen2.5 만 사용.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

# (필요 VRAM GiB 하한, Qwen3 우선 모델, Qwen2.5 대체 모델). 위에서부터 검사. 모두 Apache-2.0.
TIERS = [
    (140, "qwen3:235b-a22b", "qwen2.5:72b-instruct"),  # 서버급 다중 GPU
    (23,  "qwen3:32b",       "qwen2.5:32b-instruct"),  # 24GB+ (3090/4090/5090)
    (15,  "qwen3:14b",       "qwen2.5:14b-instruct"),  # 16GB+
    (7,   "qwen3:8b",        "qwen2.5:7b-instruct"),   # 8GB+ (예: RTX 3080 10GB)
    (4,   "qwen3:4b",        "qwen2.5:3b-instruct"),
    (0,   "qwen3:1.7b",      "qwen2.5:3b-instruct"),
]


def detect_vram_gb() -> float:
    """설치된 NVIDIA GPU 중 최대 VRAM(GiB). GPU 없거나 응답없으면 0.0."""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=12,
        )
        vals = [int(x) for x in re.findall(r"\d+", out.stdout)]
        if vals:
            return round(max(vals) / 1024.0, 1)  # MiB → GiB
    except Exception:
        pass
    return 0.0


def select_best_model() -> dict:
    """이 머신에 맞는 모델 후보를 반환.

    반환 dict:
      primary : 우선 시도 모델(보통 Qwen3)
      fallback: 대체 모델(Qwen2.5) — primary 가 실패하면 사용
      vram    : 감지된 VRAM(GiB)
      reason  : 사람이 읽는 선택 근거
    """
    vram = detect_vram_gb()
    override = os.environ.get("PAPER_VIEWER_MODEL")
    if override:
        return {"primary": override, "fallback": None, "vram": vram,
                "reason": f"환경변수 지정({override})"}

    tier = next(t for t in TIERS if vram >= t[0])
    _, q3, q25 = tier
    prefer = os.environ.get("PAPER_VIEWER_ENGINE", "").lower()
    if prefer == "qwen2.5":
        return {"primary": q25, "fallback": None, "vram": vram,
                "reason": f"VRAM {vram}GB · Qwen2.5 고정(설정)"}
    reason = (f"VRAM {vram}GB → {q3} (실패 시 {q25})" if vram > 0
              else "GPU 미감지 → 소형 모델(CPU, 느릴 수 있음)")
    return {"primary": q3, "fallback": q25, "vram": vram, "reason": reason}


if __name__ == "__main__":
    info = select_best_model()
    print(f"primary={info['primary']} | fallback={info['fallback']} | vram={info['vram']}GB | {info['reason']}")
