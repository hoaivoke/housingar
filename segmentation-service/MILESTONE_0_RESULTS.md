# Mac Milestone 0 validation — October 24, 2026

Implemented the segmentation service, benchmark, Mac readiness check, and USB setup/log-pull helper. No files under `android/` were modified.

## Environment and model provenance

- Apple M5, 16 GiB RAM; macOS 27.0.1.
- Project virtual environment: Python 3.12.13; PyTorch 2.14.1.
- Official Meta `SAM-2` installed from `https://github.com/facebookresearch/sam2.git`, commit `2b90b9f5ceec907a1c18123530e92e794ad901a4`, with `SAM2_BUILD_CUDA=0`.
- Both models used `SAM2ImagePredictor.from_pretrained(...)` and actually ran on **MPS**. CPU SAM performance was not measured.

## Automated and live checks

- **56 pytest tests passed** in 1.31 seconds. Tests use floodfill and simulated adb commands, with no model download required. Coverage includes all HTTP endpoints, multimask encoding and dimensions, persistence, corrections/cache reuse, session isolation and eviction, invalid requests, health responsiveness during blocked inference, and USB selection/install/log-pull behavior.
- One dependency deprecation warning: the installed Starlette version warns about its httpx TestClient adapter. Tests still pass.
- Dependency consistency check passed; editable package installation and console entry point worked.
- Both shell scripts passed Bash syntax checks.
- Real localhost HTTP multipart checks passed for floodfill and SAM Tiny, including the complete frame/mask saves and diagnostics/drift endpoints.
- SAM Tiny live HTTP: **855.4 ms** for the first frame and **38.2 ms** for a subsequent click correction; correction reported `embedding_cached=true` and zero embedding time. These are two observed local HTTP requests, not sustained throughput or phone latency measurements.
- Floodfill live HTTP: **32.1 ms** first request and **3.8 ms** cached correction.

## Benchmark

Input: one real room photograph from the official Meta SAM 3D Objects example repository, reduced from 6720×4480 to **640×427** while retaining aspect ratio. Source and preprocessing are recorded in `data/benchmark-images/source.json`. This is not a photo captured by our app.

Tiny and Small each ran in a fresh process, with one warmup and **3 measured uncached runs**. Each measured run was followed by a cached click correction. Floodfill used the same photograph, one warmup, and 5 measured runs.

| Backend/model | Device | Mean embed ms | Mean predict ms | Mean engine total ms | Max engine total ms | Peak process RSS MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| SAM 2.1 Tiny | MPS | 211.0 | 9.7 | 223.2 | 280.9 | 773.9 |
| SAM 2.1 Small | MPS | 231.3 | 11.5 | 248.9 | 270.3 | 1023.2 |
| OpenCV floodfill | CPU | <0.001 | 0.2 | 1.9 | 2.0 | 69.8 |

Sampled peak MPS allocated tensor memory: Tiny **337.3 MiB**, Small **492.8 MiB**. Sampled peak MPS driver memory: Tiny **2236.0 MiB**, Small **2276.0 MiB**. Peak process RSS and GPU allocator figures measure different things and should not be added as independent physical-memory usage on this unified-memory Mac. Memory peaks include loading and warmup.

All measured uncached engine results were below the plan's 3000 ms target. Engine timing includes decode, embedding, prediction, and mask serialization; it excludes model loading, checkpoint downloads, network, and disk persistence. This small sample does not establish segmentation quality across rooms or phone end-to-end performance.

Raw results:

- `data/benchmark-sam2.json`
- `data/benchmark-floodfill.json`
- `data/http-smoke-sam2.json`
- `data/http-smoke-floodfill.json`

## Readiness and remaining physical-device checks

The actual Mac readiness script reported **12 PASS / 2 FAIL**:

- RealityKit probe compiled and executed: **`PhotogrammetrySession.isSupported = true`**.
- Supported Python, Node, Java, Xcode tools, Swift, port 8000, and live service health passed.
- **Android Studio was absent** from `/Applications` and `~/Applications`.
- **adb was absent** from PATH and the checked standard SDK location. The actual USB helper also exited with a clear missing-adb error.

No physical phone connection, APK installation, or actual phone log transfer could be verified without adb and an authorized attached device. USB helper behavior was tested with simulated devices, including zero/multiple/offline/unauthorized devices and paths containing spaces.

The service was left running on **0.0.0.0:8000**, using **SAM 2.1 Tiny on MPS**. The phone should use **http://127.0.0.1:8000** after `scripts/phone_usb_setup.sh` configures USB forwarding. This is an interactive development server and may need restarting after the session or app exits; see `README.md` for the command.
