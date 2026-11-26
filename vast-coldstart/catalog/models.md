# HotAPI models snapshot

Source: `GET https://api.hotapi.ai/v1/models` (public, unauthenticated), captured 2026-11-26.
Total: **67 endpoint**, **34 families**. Raw JSON: `hotapi-models.json`.

> This is a data snapshot for choosing self-host targets. HotAPI itself is a hosted gateway;
> only open-weight equivalents can be deployed on Vast.ai. See `selfhost-candidates.md`.

## berry-1.0-pro-spicy (3)

- `berry-1.0-pro-spicy/image-to-video` — video/video.generate — $0.270/sec (1080p) — Uncensored first-frame and optional last-frame image-to-video generation (1080p/2k/4k, 2–30s, optional audio)
- `berry-1.0-pro-spicy/reference-to-video` — video/video.generate — $0.270/sec (1080p) — Uncensored image/audio reference-to-video generation (1080p/2k/4k, 2–30s, optional audio)
- `berry-1.0-pro-spicy/text-to-video` — video/video.generate — $0.270/sec (1080p) — Uncensored text-to-video generation (1080p/2k/4k, 2–30s, optional audio)

## berry-1.0-pro-turbo-spicy (3)

- `berry-1.0-pro-turbo-spicy/image-to-video` — video/video.generate — $0.390/sec (1080p) — Uncensored first-frame and optional last-frame image-to-video generation (1080p/2k/4k, 2–30s, optional audio)
- `berry-1.0-pro-turbo-spicy/reference-to-video` — video/video.generate — $0.390/sec (1080p) — Uncensored image/audio reference-to-video generation (1080p/2k/4k, 2–30s, optional audio)
- `berry-1.0-pro-turbo-spicy/text-to-video` — video/video.generate — $0.390/sec (1080p) — Uncensored text-to-video generation (1080p/2k/4k, 2–30s, optional audio)

## berry-1.0-spicy (3)

- `berry-1.0-spicy/image-to-video` — video/video.generate — $0.075/sec (480p) — Uncensored first-frame and optional last-frame image-to-video generation (480p/720p/1080p, 2–30s, optional audio)
- `berry-1.0-spicy/reference-to-video` — video/video.generate — $0.075/sec (480p) — Uncensored image/audio reference-to-video generation (480p/720p/1080p, 2–30s, optional audio)
- `berry-1.0-spicy/text-to-video` — video/video.generate — $0.075/sec (480p) — Uncensored text-to-video generation (480p/720p/1080p, 2–30s, optional audio)

## berry-1.0-turbo-spicy (3)

- `berry-1.0-turbo-spicy/image-to-video` — video/video.generate — $0.102/sec (480p) — Uncensored first-frame and optional last-frame image-to-video generation (480p/720p/1080p, 2–30s, optional audio)
- `berry-1.0-turbo-spicy/reference-to-video` — video/video.generate — $0.102/sec (480p) — Uncensored image/audio reference-to-video generation (480p/720p/1080p, 2–30s, optional audio)
- `berry-1.0-turbo-spicy/text-to-video` — video/video.generate — $0.102/sec (480p) — Uncensored text-to-video generation (480p/720p/1080p, 2–30s, optional audio)

## face-swap-spicy (1)

- `face-swap-spicy` — image/image.face_swap — $0.026/image — Uncensored face swap — replace the face in a target image with a source face

## head-swap-spicy (1)

- `head-swap-spicy` — image/image.face_swap — $0.020/image — Whole-head replacement from a reference image.

## lime-1.0-spicy (1)

- `lime-1.0-spicy` — video/video.generate — $0.045/sec (480p) — Lime 1.0 image-to-video with native audio; 5 or 10 requested seconds, 480p or upscaled 720p.

## minimax-h3-recast-spicy (1)

- `minimax-h3-recast-spicy` — video/video.generate — $0.020/sec (normal) — Uncensored character swap / video recast — replace 1–3 characters in a 5–16s MP4/MOV while preserving background, camera cuts, and audio (normal/planner modes, billed on ceil(source_seconds))

## minimax-h3-spicy (1)

- `minimax-h3-spicy` — video/video.generate — $0.020/sec (480p) — Uncensored video generation — MiniMax H3 (480p/768p/1080p/2k/4k, 5–15s, text / first-frame / first+last-frame / omni-reference (≤4 images + ≤1 video), generated audio)

## qwen-image-2.1-spicy (1)

- `qwen-image-2.1-spicy` — image/image.generate — $0.020/image — Uncensored text-to-image generation with up to 8 references, 1K/2K and 1–6 images

## qwen-image-edit-spicy (1)

- `qwen-image-edit-spicy` — image/image.edit — $0.080/image — Uncensored text-guided image editing (add/remove/modify, style transfer)

## qwen3.8-27b-spicy (1)

- `qwen3.8-27b-spicy` — text/chat.completion — see model page — Uncensored chat — Qwen3.8 27B (256K context, text in and text out)

## seedance-2.0-fast-spicy (3)

- `seedance-2.0-fast-spicy/image-to-video` — video/video.generate — $0.053/sec (480p) — Uncensored, faster, lower-cost image-to-video generation: first frame + optional last frame (480p/720p, 4–15s, synced audio)
- `seedance-2.0-fast-spicy/reference-to-video` — video/video.generate — $0.053/sec (480p) — Uncensored, faster, lower-cost reference-to-video generation: 1–9 reference images (480p/720p, 4–15s, synced audio)
- `seedance-2.0-fast-spicy/text-to-video` — video/video.generate — $0.053/sec (480p) — Uncensored, faster, lower-cost text-to-video generation (480p/720p, 4–15s, synced audio)

## seedance-2.0-mini-spicy (3)

- `seedance-2.0-mini-spicy/image-to-video` — video/video.generate — $0.030/sec (480p) — Uncensored, lowest-cost image-to-video generation: first frame + optional last frame (480p/720p, 4–15s, synced audio)
- `seedance-2.0-mini-spicy/reference-to-video` — video/video.generate — $0.030/sec (480p) — Uncensored, lowest-cost reference-to-video generation: 1–9 reference images (480p/720p, 4–15s, synced audio)
- `seedance-2.0-mini-spicy/text-to-video` — video/video.generate — $0.030/sec (480p) — Uncensored, lowest-cost text-to-video generation (480p/720p, 4–15s, synced audio)

## seedance-2.0-spicy (3)

- `seedance-2.0-spicy/image-to-video` — video/video.generate — $0.140/sec (480p) — Uncensored image-to-video generation: first frame + optional last frame (480p/720p/1080p/4k, 4–15s, synced audio)
- `seedance-2.0-spicy/reference-to-video` — video/video.generate — $0.140/sec (480p) — Uncensored reference-to-video generation: 1–9 reference images (480p/720p/1080p/4k, 4–15s, synced audio)
- `seedance-2.0-spicy/text-to-video` — video/video.generate — $0.140/sec (480p) — Uncensored text-to-video generation (480p/720p/1080p/4k, 4–15s, synced audio)

## seedance-2.5-flash-spicy (4)

- `seedance-2.5-flash-spicy/image-to-video` — video/video.generate — $0.136/sec (480p) — Uncensored, faster image-to-video generation: first frame + optional last frame (480p/720p, 4–30s, synced audio, MP4/MOV output)
- `seedance-2.5-flash-spicy/reference-to-video` — video/video.generate — $0.136/sec (480p) — Uncensored, faster reference-to-video generation: up to 10 images, 5 audio clips, and 5 reference videos (480p/720p, 4–30s, synced audio, MP4/MOV output)
- `seedance-2.5-flash-spicy/text-to-video` — video/video.generate — $0.136/sec (480p) — Uncensored, faster text-to-video generation (480p/720p, 4–30s, selectable aspect ratio, synced audio, MP4/MOV output)
- `seedance-2.5-flash-spicy/video-to-video` — video/video.generate — $0.136/sec (480p) — Faster video editing: one 4–30s source, optional image/audio references, adaptive ratio, source-based fixed billing, MP4/MOV

## seedance-2.5-spicy (5)

- `seedance-2.5-spicy/draft-to-video` — video/video.generate — $1.137/sec (1080p) — Finalize a Seedance 2.5 draft preview into the 1080p final video (reuses the draft's composition; billed at the 1080p rate)
- `seedance-2.5-spicy/image-to-video` — video/video.generate — $0.206/sec (480p) — Uncensored image-to-video generation: first frame + optional last frame (480p/720p/1080p, 4–30s, synced audio, draft preview)
- `seedance-2.5-spicy/reference-to-video` — video/video.generate — $0.206/sec (480p) — Uncensored reference-to-video generation: up to 30 images + 10 audio clips (480p/720p/1080p, 4–30s, synced audio, draft preview)
- `seedance-2.5-spicy/text-to-video` — video/video.generate — $0.206/sec (480p) — Uncensored text-to-video generation (480p/720p/1080p, 4–30s, selectable aspect ratio, synced audio, draft preview)
- `seedance-2.5-spicy/video-to-video` — video/video.generate — $0.206/sec (480p) — Video editing: one 4–30s source, optional image/audio references, adaptive ratio, source-based fixed billing, MP4/MOV, draft preview

## seedream-5.0-flash-spicy (2)

- `seedream-5.0-flash-spicy/image-edit` — image/image.generate — $0.024/image — Fast, low-cost uncensored image editing / multi-image blending: 1–10 free reference images (1K/1.5K/2K, JPEG/PNG)
- `seedream-5.0-flash-spicy/text-to-image` — image/image.generate — $0.024/image — Fast, low-cost uncensored text-to-image generation (1K/1.5K/2K, JPEG/PNG)

## seedream-5.0-lite-spicy (2)

- `seedream-5.0-lite-spicy/image-edit` — image/image.generate — $0.060/image — Uncensored high-resolution image editing / multi-image blending: 1–14 reference images (2K/3K/4K)
- `seedream-5.0-lite-spicy/text-to-image` — image/image.generate — $0.060/image — Uncensored high-resolution text-to-image generation (2K/3K/4K)

## seedream-5.0-pro-spicy (2)

- `seedream-5.0-pro-spicy/image-edit` — image/image.generate — $0.120/image — Uncensored Pro-tier high-resolution image editing / multi-image blending: 1–14 reference images (2K/3K/4K)
- `seedream-5.0-pro-spicy/text-to-image` — image/image.generate — $0.120/image — Uncensored Pro-tier high-resolution text-to-image generation (2K/3K/4K)

## seedream-5.5-spicy (2)

- `seedream-5.5-spicy/image-edit` — image/image.generate — $0.035/image — Uncensored image editing / multi-image blending with native transparent RGBA PNG support: 1–10 reference images (1K/1.5K/2K)
- `seedream-5.5-spicy/text-to-image` — image/image.generate — $0.035/image — Uncensored text-to-image generation with native transparent RGBA PNG background support (1K/1.5K/2K)

## silly-1.0-lite (1)

- `silly-1.0-lite` — text/chat.completion — see model page — Budget character chat with image understanding; independent from Silly 1.0. HotAPI context limit: 32K tokens.

## silly-2.0 (1)

- `silly-2.0` — text/chat.completion — see model page — Premium companion roleplay chat — GLM-5.3 base, uncensored, character-card friendly, long context (text in and text out)

## siren-1.0-spicy (1)

- `siren-1.0-spicy` — audio/audio.tts — see model page — Expressive cinematic speech — breaths, pauses and tone shifts

## video-enhancer-spicy (1)

- `video-enhancer-spicy` — video/video.generate — $0.070/sec (1080p) — Upscale and enhance a 1–30s MP4/MOV video to 1080p, 2k, or 4k with optional text sharpening

## w3.0-prime-pro-spicy (3)

- `w3.0-prime-pro-spicy/image-to-video` — video/video.generate — $0.390/sec (1080p) — Uncensored first-frame and optional last-frame image-to-video generation (W3.0 / Wan 3.0, 1080p/2k/4k, 2–30s, optional audio)
- `w3.0-prime-pro-spicy/reference-to-video` — video/video.generate — $0.390/sec (1080p) — Uncensored image/video/audio/document reference-to-video generation (W3.0 / Wan 3.0, 1080p/2k/4k, 2–30s, optional audio)
- `w3.0-prime-pro-spicy/text-to-video` — video/video.generate — $0.390/sec (1080p) — Uncensored text-to-video generation (W3.0 / Wan 3.0, 1080p/2k/4k, 2–30s, optional audio)

## w3.0-prime-spicy (3)

- `w3.0-prime-spicy/image-to-video` — video/video.generate — $0.102/sec (480p) — Uncensored first-frame and optional last-frame image-to-video generation (W3.0 / Wan 3.0, 480p/720p/1080p, 2–30s, optional audio)
- `w3.0-prime-spicy/reference-to-video` — video/video.generate — $0.102/sec (480p) — Uncensored image/video/audio/document reference-to-video generation (W3.0 / Wan 3.0, 480p/720p/1080p, 2–30s, optional audio)
- `w3.0-prime-spicy/text-to-video` — video/video.generate — $0.102/sec (480p) — Uncensored text-to-video generation (W3.0 / Wan 3.0, 480p/720p/1080p, 2–30s, optional audio)

## w3.0-pro-spicy (3)

- `w3.0-pro-spicy/image-to-video` — video/video.generate — $0.270/sec (1080p) — Uncensored first-frame and optional last-frame image-to-video generation (W3.0 / Wan 3.0, 1080p/2k/4k, 2–30s, optional audio)
- `w3.0-pro-spicy/reference-to-video` — video/video.generate — $0.270/sec (1080p) — Uncensored image/video/audio/document reference-to-video generation (W3.0 / Wan 3.0, 1080p/2k/4k, 2–30s, optional audio)
- `w3.0-pro-spicy/text-to-video` — video/video.generate — $0.270/sec (1080p) — Uncensored text-to-video generation (W3.0 / Wan 3.0, 1080p/2k/4k, 2–30s, optional audio)

## w3.0-spicy (3)

- `w3.0-spicy/image-to-video` — video/video.generate — $0.075/sec (480p) — Uncensored first-frame and optional last-frame image-to-video generation (W3.0 / Wan 3.0, 480p/720p/1080p, 2–30s, optional audio)
- `w3.0-spicy/reference-to-video` — video/video.generate — $0.075/sec (480p) — Uncensored image/video/audio/document reference-to-video generation (W3.0 / Wan 3.0, 480p/720p/1080p, 2–30s, optional audio)
- `w3.0-spicy/text-to-video` — video/video.generate — $0.075/sec (480p) — Uncensored text-to-video generation (W3.0 / Wan 3.0, 480p/720p/1080p, 2–30s, optional audio)

## wan-2.2-spicy (1)

- `wan-2.2-spicy` — video/video.generate — $0.080/sec (480p) — Uncensored image-to-video generation (480p/720p, 5s or 8s)

## wan-2.7-spicy (1)

- `wan-2.7-spicy` — video/video.generate — $0.260/sec (720p) — Uncensored image-to-video generation (720p/1080p, 2–15s, optional audio)

## wan-animate-spicy (1)

- `wan-animate-spicy` — video/video.generate — $0.080/sec (480p) — Motion transfer or character replacement; billed on server-probed reference seconds (1–15s), not rounded output duration.

## z-image-spicy (1)

- `z-image-spicy` — image/image.generate — $0.026/image — Uncensored text-to-image generation (256–1536px, custom size)

## z-image-spicy-pro (1)

- `z-image-spicy-pro` — image/image.generate — $0.030/image — Uncensored text-to-image generation, Pro tier (256–2560px, custom size)
