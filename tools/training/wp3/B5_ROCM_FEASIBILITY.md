# B5: ROCm Feasibility — gemma-4-12B-it QLoRA on AMD R9700

## Verdict: **NOT-FEASIBLE with current setup**

A 4-bit QLoRA fine-tune of gemma-4-12B-it on the R9700 is **not possible today**
with the installed software stack. The VRAM budget exists (~11.6 GB needed, ~18.6 GB
free) but **every 4-bit quantisation path fails** on Python 3.14 + ROCm 6.4.

---

## 1. bitsandbytes status

| Item | Result |
|------|--------|
| **Import** | ✅ `import bitsandbytes` works (v0.50.0) |
| **Pre-built ROCm libs** | ❌ Stub `.so` files (~0.9 MB vs 20+ MB for CUDA). No gfx1201 kernels. |
| **4-bit ops** | ❌ SIGABRT — HIP "Module not initialized" (no gfx1201 kernels) |
| **8-bit ops** | ❌ Same SIGABRT |

The pre-built wheel ships `libbitsandbytes_rocm*.so` for ROCm 6.4/7.0/7.1/7.14/7.2,
but each is a 0.9 MB stub that does not contain quantised matmul kernels for
the gfx1201 (R9700, RDNA 4) architecture.

---

## 2. All alternate 4-bit paths also fail

| Method | Failure mode |
|--------|-------------|
| **bitsandbytes `load_in_4bit`** | SIGABRT via HIP module init |
| **bitsandbytes `load_in_8bit`** | SIGABRT via HIP module init |
| **torchao** (0.10.0, 0.17.0) | `typing.Union` has no `__module__` — Python 3.14 incompatibility in `torch.ao.quantization` |
| **HQQ** (0.2.8) | `torch.compile` disabled on Python 3.14 — `RuntimeError("torch.compile is not supported on Python 3.14+")` |
| **bf16 (no quant)** | Model weights = 23.92 GB > 18.6 GB free VRAM — OOM |

---

## 3. Building bitsandbytes from source — success, then blocked by ABI mismatch

We **successfully compiled** `libbitsandbytes_rocm72.so` (13 MB) with HIP kernels
for gfx1201 from the bitsandbytes repo (commit `a2b90e6`, cmake with
`-DCOMPUTE_BACKEND=hip` and `rocm_agent_enumerator` targets including gfx1201).

```
4 warnings generated when compiling for gfx1201.
[100%] Built target bitsandbytes
```

**Blocked by ABI mismatch:**

```
/opt/rocm-7.2.4/lib/libamdhip64.so.7: undefined symbol: hsa_amd_memory_get_preferred_copy_engine, version ROCR_1
```

The compiled binary links against **ROCm 7.2** system libraries, but the PyTorch
in this venv (torch 2.9.1+rocm6.4) loads **ROCm 6.4** HSA runtime at process
start. The ROCm 7.2 HIP library needs an HSA symbol that doesn't exist in the
ROCm 6.4 runtime.

**Fix would require**: either building bitsandbytes against torch's bundled
ROCm 6.4 headers/libs, or upgrading torch to match system ROCm 7.2.

---

## 4. VRAM analysis

### Current state (verified before and after probe)

```
R9700 (device 0, gfx1201, 34.2 GB)
  Used:  15,633,350,656 B  (~15.6 GB — Ollama ~8.8 GB + MageZero ~5 GB)
  Free:  ~18.6 GB
```

GPU state **unchanged** after the probe (same used VRAM, temperature stable).

### VRAM estimate: 4-bit QLoRA (gemma-4-12B-it, LoRA r=8)

| Component | Est. VRAM |
|-----------|-----------|
| Weights (4-bit NF4) | ~6.0 GB |
| LoRA adapters (A+B) | ~0.1 GB |
| Optimizer states (AdamW, fp32) | ~3.0 GB |
| Gradients | ~1.5 GB |
| Activations (gradient checkpointing, bs=1, seq=512) | ~1.0 GB |
| **Total estimated** | **~11.6 GB** |
| Available | ~18.6 GB |
| **Margin** | **~7.0 GB** ✅ |

The VRAM budget is adequate IF quantisation can be made to work.

---

## 5. What would be needed

### Path A: Fix bitsandbytes on this machine (recommended)
1. **Create a Python 3.12 venv** — avoids the Python 3.14 `torch.compile` and
   `typing.Union` issues that kill torchao and HQQ.
2. **Install torch 2.9.1+rocm6.4** into the Python 3.12 venv.
3. **Build bitsandbytes from source** using torch's **bundled ROCm 6.4 headers**
   (at `<venv>/lib/python3.12/site-packages/torch/lib/libamdhip64*` and includes)
   instead of system ROCm 7.2. This avoids the ABI mismatch.

   Estimated build time: ~15-30 minutes for kernel compilation.

### Path B: Work without bitsandbytes
- **Merge into model via model surgery** — load bf16 on CPU, quantise weights
  with torchao (in Python 3.12), save quantised checkpoint, then load for
  LoRA fine-tuning. Avoids bitsandbytes entirely.
- **Use NVFP4** (gemma-4's native fp4 format) — the 12B-it model is available
  in fp4 from Google, but the R9700 may not support this format.

### Path C: Different hardware
- **NVIDIA RTX** (the two production cards in this machine) — CUDA support for
  bitsandbytes is mature. There are ~100 GB free on the two RTX cards combined
  (they run DeepSeek at 1M context so must not be touched without coordination).
- **Rented cloud GPU** (A100/H100) — trivial setup, fast training.

### Path D: Smaller model
- **gemma-4-12B-it → smaller variant** — gemma-4-2B or gemma-4-4B would fit
  easily in bf16 without quantisation on the R9700.

---

## 6. Wall-clock estimate (extrapolated)

Cannot run a real forward+backward pass to measure tokens/sec because the model
cannot be quantised. Based on similar RDNA 4 cards:

- R9700 INT8 compute: ~165 TFLOPS
- Typical tokens/sec for 12B 4-bit model: ~60-100 tok/s (inference), ~10-20 tok/s
  (training with LoRA + grad ckpt)
- 8,000 records × 512 tokens = 4,096,000 tokens
- **Estimated epoch time: ~57-114 hours** (2.4-4.8 days)

---

## 7. Summary

```
bitsandbytes import:            ✅ (0.50.0)
bitsandbytes 4-bit on ROCm:     ❌ (no gfx1201 kernels in pre-built wheel)
torchao on ROCm:                 ❌ (Python 3.14)
HQQ on ROCm:                     ❌ (Python 3.14 / torch.compile)
Build from source for gfx1201:   ✅ (but ABI mismatch w/ torch ROCm 6.4)
bf16 model fits in free VRAM:    ❌ (23.92 GB > 18.6 GB)

Verdict: NOT-FEASIBLE with current setup
```

The blocker is **not VRAM** (~11.6 GB needed vs ~18.6 GB free) but the
**quantisation software stack**: bitsandbytes lacks gfx1201 kernels, and
torchao/HQQ are broken on Python 3.14. A Python 3.12 venv + source build
against torch's bundled ROCm 6.4 would unblock this machine.
