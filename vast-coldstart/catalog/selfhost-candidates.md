# Self-host candidates on Vast.ai

HotAPI is a hosted gateway. Only models with openly published weights can be
run on a rented GPU. Do not copy HotAPI branding, endpoints orproduct into a
self-hosted service — use the public checkpoints under their own licenses.

| HotAPI endpoint | Open-weight equivalent | License status | First image |
|---|---|---|---|
| `z-image-spicy` / `z-image-spicy-pro` | `Tongyi-MAI/Z-Image-Turbo`, `Tongyi-MAI/Z-Image` | Apache-2.0 per HF card | ✅ this scaffold |
| `qwen-image-2.1-spicy` / `qwen-image-edit-spicy` | `Qwen/Qwen-Image`, `Qwen/Qwen-Image-Edit` | verify HF card before prod | next candidate |
| `wan-2.2-spicy` | `Wan-AI/Wan2.2-I2V-A14B` | verify HF card before prod | video, heavier |

## Why Z-Image first

- 6B single-stream DiT, 8 NFE Turbo variant, fits in 16 GB VRAM.
- `diffusers` integration: `ZImagePipeline`.
- Text-to-image only: no image/video inputs, no audio stack — smallest cold-start surface.
- Cheapest useful smoke test (one 768x512 PNG) before adding video templates.

Proprietary families (Seedance, Seedream, Berry, MiniMax H3, Siren, Silly,
W3.x, video-enhancer) have no published weights in this snapshot: keep them
as HotAPI upstream, not as Vast images.
