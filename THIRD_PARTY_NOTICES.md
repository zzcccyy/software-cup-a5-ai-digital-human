# Third-party notices

The root `LICENSE` covers the project's original source code only. Bundled or remotely loaded assets may have separate copyright and license terms. Before publishing a release or redistributing a build, verify and record the applicable license and attribution for each item below.

| Component or asset | Location/use | Release checklist |
|---|---|---|
| `@pixiv/three-vrm` 2.1.1 | Loaded from `unpkg.com` by `tourist-client/index.html` | Preserve the upstream license and attribution for the exact version. |
| `sentence-transformers/all-MiniLM-L6-v2` ONNX files | `backend/onnx_models/` | Confirm the model license and include its model-card attribution. |
| VRM avatar models | `景.vrm`, `灵.vrm`, `区.vrm`, `山.vrm` | Confirm redistribution and commercial-use permission for each model. |
| Images and scenic reference materials | `tourist-client/images/`, `admin/assets/`, `示范景区公开资料包/` | Confirm the photographer, source, and redistribution terms. |
| `tools/codex-unzip/unzip.exe` | Windows packaging helper | Record its build source and redistribution license, or distribute only a documented rebuild. |
| Python dependencies | `backend/requirements.txt` | Review the license of the resolved dependency versions before a packaged release. |

Do not assume that the project's MIT license grants rights to third-party assets. If an asset's provenance or license cannot be verified, remove it from the public distribution or replace it with a distributable alternative.
